"""Independent acceptance: REAL app-server subprocess (no mocks, no live).

Proves transport hardening against a real child: partial/coalesced frames,
stderr flood, sanitized errors, bounded timeouts, init-failure cleanup,
cancellation, and model+effort pagination. No live inference/network.
"""
import json
import os
import tempfile
import time
import unittest
from unittest import mock

from app import protocol
from ._fakes import write_fake_codex


class TestRealAppServer(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="olo-acc-rpc-")
        self.fake = write_fake_codex(self.tmp)
        self.home = os.path.join(self.tmp, "codex")
        os.makedirs(self.home, exist_ok=True)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _client(self, timeout=5, **env):
        base = dict(os.environ)
        base.update(env)
        patcher = mock.patch.dict(os.environ, base, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        return protocol.AppServerClient(codex_bin=self.fake,
                                        codex_home=self.home,
                                        timeout=timeout)

    def test_initialize_handshake_and_call(self):
        c = self._client()
        with c:
            res = c.call("account/rateLimits/read", {})
        self.assertIn("ordinaryUsageAllowed", res)
        self.assertEqual(c.notifications_seen, 0)

    def test_coalesced_and_partial_frames(self):
        c = self._client(FAKE_SERVER_MODE="coalesced")
        with c:
            self.assertEqual(c.call("account/rateLimits/read", None), {"v": 1})
        self.assertEqual(c.notifications_seen, 1)
        c2 = self._client(FAKE_SERVER_MODE="partial")
        with c2:
            self.assertEqual(c2.call("account/rateLimits/read", None), {"v": 2})

    def test_stderr_flood_never_deadlocks(self):
        c = self._client(FAKE_SERVER_MODE="happy", FAKE_FLOOD_STDERR="1")
        with c:
            t0 = time.monotonic()
            res = c.call("account/rateLimits/read", {})
            dt = time.monotonic() - t0
        self.assertIn("ordinaryUsageAllowed", res)
        self.assertLess(dt, 15)
        self.assertGreater(c._stderr_total[0], protocol.COUNT_CAP_BYTES)

    def test_errors_sanitized_no_secret_leak(self):
        c = self._client(FAKE_SERVER_MODE="secret-error")
        with c:
            with self.assertRaises(protocol.AuthError) as ctx:
                c.call("account/rateLimits/read", None)
        self.assertEqual(str(ctx.exception), "auth-required")
        self.assertNotIn("sk-super-secret", str(ctx.exception))
        c2 = self._client(FAKE_SERVER_MODE="rpc-error")
        with c2:
            with self.assertRaises(protocol.ProtocolError) as ctx2:
                c2.call("account/rateLimits/read", None)
        self.assertEqual(str(ctx2.exception), "rpc-error")
        self.assertNotIn("abc123", str(ctx2.exception))

    def test_malformed_frame_fail_closed(self):
        c = self._client(FAKE_SERVER_MODE="malformed")
        with c:
            with self.assertRaises(protocol.ProtocolError) as ctx:
                c.call("account/rateLimits/read", {})
        self.assertEqual(str(ctx.exception), "malformed-frame")

    def test_timeout_bounded_and_reaps(self):
        c = self._client(timeout=1, FAKE_SERVER_MODE="hang")
        with c:
            t0 = time.monotonic()
            with self.assertRaises(protocol.ProtocolError) as ctx:
                c.call("account/rateLimits/read", None)
            dt = time.monotonic() - t0
        self.assertEqual(str(ctx.exception), "timeout")
        self.assertLess(dt, 10)

    def test_spawn_failure_sanitized(self):
        c = protocol.AppServerClient(codex_bin="/nonexistent-olo-bin-xyz",
                                     codex_home=self.home, timeout=2)
        with self.assertRaises(protocol.ProtocolError) as ctx:
            with c:
                pass
        self.assertEqual(str(ctx.exception), "spawn-failed")

    def test_init_failure_cleans_up_no_leak(self):
        for mode in ("init-error", "init-exit"):
            c = self._client(FAKE_SERVER_MODE=mode)
            with self.assertRaises(protocol.ProtocolError):
                with c:
                    pass
            self.assertIsNone(c._proc, "failed __enter__ must not leak proc")

    def test_cancel_reacts_promptly(self):
        import threading as _t
        ev = _t.Event()
        with mock.patch.dict(os.environ, {"FAKE_SERVER_MODE": "hang"}):
            ch = protocol.AppServerClient(codex_bin=self.fake,
                                          codex_home=self.home, timeout=10,
                                          cancel_event=ev)
            with ch:
                ev.set()
                with self.assertRaises(protocol.CancelledError):
                    ch.call("account/rateLimits/read", None)

    def test_ensure_model_effort_actual_schema_paginated(self):
        # Build pages via env JSON instead (clearer).
        pages = [
            {"data": [{"id": "gpt-5.5", "supportedReasoningEfforts": [
                {"reasoningEffort": "low", "description": "d"}]}],
             "nextCursor": "c1"},
            {"data": [{"id": "gpt-5.6-luna", "supportedReasoningEfforts": [
                {"reasoningEffort": "LOW", "description": "d"}]}],
             "nextCursor": None},
        ]
        c = self._client(FAKE_MODEL_PAGES=json.dumps(pages))
        with c:
            self.assertTrue(c.ensure_model("gpt-5.6-luna", "low"))
        # effort not advertised -> effort-unsupported
        pages2 = [{"data": [{"id": "gpt-5.6-luna", "supportedReasoningEfforts": [
            {"reasoningEffort": "medium"}]}],
                   "nextCursor": None}]
        c2 = self._client(FAKE_MODEL_PAGES=json.dumps(pages2))
        with c2:
            with self.assertRaises(protocol.ProtocolError) as ctx:
                c2.ensure_model("gpt-5.6-luna", "low")
        self.assertEqual(str(ctx.exception), "effort-unsupported")
        # unknown id -> model-unknown
        c3 = self._client(FAKE_MODEL_PAGES=json.dumps(pages2))
        with c3:
            with self.assertRaises(protocol.ProtocolError) as ctx3:
                c3.ensure_model("gpt-9", "low")
        self.assertEqual(str(ctx3.exception), "model-unknown")
        # missing catalogue -> effort-unknown
        pages4 = [{"data": [{"id": "gpt-5.6-luna"}], "nextCursor": None}]
        c4 = self._client(FAKE_MODEL_PAGES=json.dumps(pages4))
        with c4:
            with self.assertRaises(protocol.ProtocolError) as ctx4:
                c4.ensure_model("gpt-5.6-luna", "low")
        self.assertEqual(str(ctx4.exception), "effort-unknown")

    def test_pagination_loop_and_cap_deny_real(self):
        c = self._client(FAKE_MODEL_LOOP="1",
                         FAKE_MODEL_PAGES=json.dumps(
                             [{"data": [], "nextCursor": "c1"}]))
        with c:
            with self.assertRaises(protocol.ProtocolError) as ctx:
                list(c.iter_models())
        self.assertEqual(str(ctx.exception), "model-pages-loop")
        pages = [{"data": [], "nextCursor": f"c{i}"} for i in range(30)]
        c2 = self._client(FAKE_MODEL_LOOP="0",
                          FAKE_MODEL_PAGES=json.dumps(pages))
        with c2:
            with self.assertRaises(protocol.ProtocolError) as ctx2:
                list(c2.iter_models())
        self.assertEqual(str(ctx2.exception), "model-pages-exceeded")

    def test_group_cleanup_kills_saved_pgid_real(self):
        import signal as _sig
        c = self._client()
        with c:
            leader = c._leader
            self.assertIsNotNone(leader)
            child_pid = c._proc.pid
        # After exit the direct child is reaped; the saved group must have
        # been signalled (TERM then KILL). Prove liveness via os.kill.
        try:
            os.kill(child_pid, 0)
            alive = True
        except ProcessLookupError:
            alive = False
        except PermissionError:
            alive = True
        self.assertFalse(alive, "app-server child must be reaped")


if __name__ == "__main__":
    unittest.main()
