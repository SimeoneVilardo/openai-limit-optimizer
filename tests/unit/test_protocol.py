"""Protocol self-checks with a fake stdio transport (no live Codex).

Covers: happy-path sequence + initialized notification, timeout,
coalesced/partial frames, infinite stderr drain, error sanitization
(no raw server text), spawn failure, cancellation, ChatGPT-only gate
and exact model+effort validation with pagination.
"""

import io
import json
import os
import threading
import unittest

from app import protocol


class FakeStdin:
    def __init__(self):
        self.writes = []

    def write(self, data):
        self.writes.append(bytes(data))

    def flush(self):
        pass

    def close(self):
        pass


class FakeProc:
    def __init__(self, rfd):
        self.stdin = FakeStdin()
        self.stdout = os.fdopen(rfd, "rb", buffering=0)
        self.stderr = io.BytesIO(b"")
        self.pid = 424242
        self._alive = True

    def poll(self):
        return None if self._alive else 0

    def terminate(self):
        self._alive = False

    def kill(self):
        self._alive = False

    def wait(self, timeout=None):
        return 0


def feed(wfd, chunks, delay=0.05):
    import time
    for chunk in chunks:
        time.sleep(delay)
        if isinstance(chunk, str):
            chunk = (chunk + "\n").encode()
        try:
            os.write(wfd, chunk)
        except OSError:
            return


def _fd_open(fd):
    try:
        os.fstat(fd)
        return True
    except OSError:
        return False


class TestProtocol(unittest.TestCase):
    def _client(self, chunks, timeout=5, **kw):
        rfd, wfd = os.pipe()
        proc = FakeProc(rfd)
        t = threading.Thread(target=feed, args=(wfd, chunks), daemon=True)
        t.start()
        self.addCleanup(lambda: (os.close(wfd) if _fd_open(wfd) else None))
        client = protocol.AppServerClient(spawn=lambda: proc, timeout=timeout,
                                          **kw)
        return client, proc

    def test_initialize_then_call_and_notified(self):
        c, proc = self._client([
            json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"ok": True}}),
            json.dumps({"jsonrpc": "2.0", "id": 2, "result": {"rateLimits": {}}}),
        ])
        with c:
            res = c.call("account/rateLimits/read", None)
        self.assertEqual(res, {"rateLimits": {}})
        init, notified = proc.stdin.writes[0], proc.stdin.writes[1]
        self.assertEqual(json.loads(init)["method"], "initialize")
        self.assertEqual(json.loads(notified)["method"], "initialized")

    def test_timeout(self):
        c, _ = self._client([
            json.dumps({"jsonrpc": "2.0", "id": 1, "result": {}}),
        ], timeout=1)
        with c:
            with self.assertRaises(protocol.ProtocolError) as ctx:
                c.call("account/rateLimits/read", None)
        self.assertEqual(str(ctx.exception), "timeout")

    def test_coalesced_and_partial_frames(self):
        c, _ = self._client([
            json.dumps({"jsonrpc": "2.0", "id": 1, "result": {}}),
            # Two frames coalesced in one write ...
            (json.dumps({"jsonrpc": "2.0", "method": "n", "params": {}})
             + "\n"
             + json.dumps({"jsonrpc": "2.0", "id": 2,
                           "result": {"v": 1}}) + "\n").encode(),
        ])
        with c:
            self.assertEqual(c.call("x", None), {"v": 1})
        self.assertEqual(c.notifications_seen, 1)

        c2, _ = self._client([
            json.dumps({"jsonrpc": "2.0", "id": 1, "result": {}}),
            # ... and one frame split across two writes (partial line).
            '{"jsonrpc": "2.0", "id": 2, '.encode(),
            '"result": {"v": 2}}\n'.encode(),
        ])
        with c2:
            self.assertEqual(c2.call("x", None), {"v": 2})

    def test_infinite_stderr_drained(self):
        c, proc = self._client([
            json.dumps({"jsonrpc": "2.0", "id": 1, "result": {}}),
            json.dumps({"jsonrpc": "2.0", "id": 2, "result": {"v": 1}}),
        ])
        proc.stderr = io.BytesIO(b"n" * (3 * protocol.COUNT_CAP_BYTES))
        with c:
            self.assertEqual(c.call("x", None), {"v": 1})
        self.assertGreater(c._stderr_total[0], protocol.COUNT_CAP_BYTES)

    def test_malformed_frame(self):
        c, _ = self._client([
            json.dumps({"jsonrpc": "2.0", "id": 1, "result": {}}),
            "{{{not json",
        ])
        with c:
            with self.assertRaises(protocol.ProtocolError) as ctx:
                c.call("account/read", {})
        self.assertEqual(str(ctx.exception), "malformed-frame")

    def test_auth_error_sanitized(self):
        secret = "Bearer sk-super-secret-xyz"
        c, _ = self._client([
            json.dumps({"jsonrpc": "2.0", "id": 1, "result": {}}),
            json.dumps({"jsonrpc": "2.0", "id": 2, "error":
                        {"code": -32000, "message": f"chatgpt auth {secret}"}}),
        ])
        with c:
            with self.assertRaises(protocol.AuthError) as ctx:
                c.call("account/rateLimits/read", None)
        self.assertEqual(str(ctx.exception), "auth-required")
        self.assertNotIn("secret", str(ctx.exception))

    def test_rpc_error_sanitized(self):
        c, _ = self._client([
            json.dumps({"jsonrpc": "2.0", "id": 1, "result": {}}),
            json.dumps({"jsonrpc": "2.0", "id": 2, "error":
                        {"code": 7, "message": "token abc123 leaked?"}}),
        ])
        with c:
            with self.assertRaises(protocol.ProtocolError) as ctx:
                c.call("account/rateLimits/read", None)
        self.assertEqual(str(ctx.exception), "rpc-error")

    def test_spawn_failure_sanitized(self):
        def boom():
            raise OSError(13, "Permission denied", "/sensitive/path")
        c = protocol.AppServerClient(spawn=boom)
        with self.assertRaises(protocol.ProtocolError) as ctx:
            with c:
                pass
        self.assertEqual(str(ctx.exception), "spawn-failed")

    def test_cancel(self):
        import threading as _t
        ev = _t.Event()
        c, _ = self._client([
            json.dumps({"jsonrpc": "2.0", "id": 1, "result": {}}),
            json.dumps({"jsonrpc": "2.0", "id": 2, "result": {"v": 1}}),
        ], cancel_event=ev)
        with c:
            ev.set()
            with self.assertRaises(protocol.CancelledError):
                c.call("account/rateLimits/read", None)

    def test_model_available_single_page(self):
        res = {"data": [{"id": "gpt-5.6-luna"}], "nextCursor": None}
        self.assertTrue(protocol.model_available(res, "gpt-5.6-luna"))
        self.assertFalse(protocol.model_available(res, "gpt-5.6-luna-pro"))
        self.assertFalse(protocol.model_available({}, "gpt-5.6-luna"))

    def test_chatgpt_only(self):
        self.assertTrue(protocol.is_chatgpt_account(
            {"account": {"type": "chatgpt"}, "requiresOpenaiAuth": False}))
        self.assertFalse(protocol.is_chatgpt_account(
            {"account": {"type": "apiKey"}, "requiresOpenaiAuth": True}))
        self.assertFalse(protocol.is_chatgpt_account({"account": None}))
        self.assertFalse(protocol.is_chatgpt_account({}))

    def _model_client(self, pages):
        chunks = [json.dumps({"jsonrpc": "2.0", "id": 1, "result": {}})]
        rid = 2
        for page in pages:
            chunks.append(json.dumps({"jsonrpc": "2.0", "id": rid,
                                      "result": page}))
            rid += 1
        c, _ = self._client(chunks)
        return c

    def test_ensure_model_effort_paginated(self):
        pages = [
            {"data": [{"id": "gpt-5.5",
                       "supportedReasoningEfforts": [
                           {"reasoningEffort": "low", "description": "d"}]}],
             "nextCursor": "c1"},
            {"data": [{"id": "gpt-5.6-luna",
                       "supportedReasoningEfforts": [
                           {"reasoningEffort": "LOW", "description": "d"}]}],
             "nextCursor": None},
        ]
        c = self._model_client(pages)
        with c:
            self.assertTrue(c.ensure_model("gpt-5.6-luna", "low"))

    def test_ensure_model_failures(self):
        mk = lambda models, cursor=None: {"data": models, "nextCursor": cursor}
        full = {"id": "gpt-5.6-luna", "supportedReasoningEfforts": [
            {"reasoningEffort": "medium", "description": "d"}]}
        # unknown id
        c = self._model_client([mk([full])])
        with c:
            with self.assertRaises(protocol.ProtocolError) as ctx:
                c.ensure_model("gpt-9", "low")
        self.assertEqual(str(ctx.exception), "model-unknown")
        # effort not advertised
        c = self._model_client([mk([full])])
        with c:
            with self.assertRaises(protocol.ProtocolError) as ctx:
                c.ensure_model("gpt-5.6-luna", "low")
        self.assertEqual(str(ctx.exception), "effort-unsupported")
        # effort catalogue missing
        c = self._model_client([mk([{"id": "gpt-5.6-luna"}])])
        with c:
            with self.assertRaises(protocol.ProtocolError) as ctx:
                c.ensure_model("gpt-5.6-luna", "low")
        self.assertEqual(str(ctx.exception), "effort-unknown")

    def test_child_env_sanitized(self):
        import unittest.mock as mock
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-x",
                                          "CODEX_ACCESS_TOKEN": "t",
                                          "OPENAI_BASE_URL": "http://evil",
                                          "PATH": "/usr/bin"}):
            env = protocol.child_env("/data/codex")
        for k in ("OPENAI_API_KEY", "CODEX_API_KEY", "CODEX_ACCESS_TOKEN",
                  "OPENAI_BASE_URL", "OPENAI_ORGANIZATION", "OPENAI_PROJECT",
                  "CODEX_OSS_BASE_URL", "CODEX_OSS_PORT"):
            self.assertNotIn(k, env)
        self.assertEqual(env["CODEX_HOME"], "/data/codex")
        self.assertEqual(env["PATH"], "/usr/bin")


class TestHomeBootstrap(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.join("/tmp", f"olo-home-{os.getpid()}-{id(self)}")
        os.makedirs(self.tmp, exist_ok=True)

    def tearDown(self):
        import shutil as _sh
        _sh.rmtree(self.tmp, ignore_errors=True)

    def test_creates_0700(self):
        import stat as _st
        home = os.path.join(self.tmp, "codex")
        protocol.ensure_codex_home(home)
        self.assertTrue(os.path.isdir(home))
        self.assertEqual(_st.S_IMODE(os.stat(home).st_mode), 0o700)
        protocol.ensure_codex_home(home)  # idempotent

    def test_file_in_way_fails(self):
        busy = os.path.join(self.tmp, "busy")
        with open(busy, "w") as fh:
            fh.write("x")
        with self.assertRaises(protocol.HomeError):
            protocol.ensure_codex_home(os.path.join(busy, "sub"))

    def test_mkdir_failure_visible(self):
        import unittest.mock as mock
        with mock.patch("os.makedirs", side_effect=OSError("denied")):
            with self.assertRaises(protocol.HomeError):
                protocol.ensure_codex_home(os.path.join(self.tmp, "nope"))


class TestGroupCleanup(unittest.TestCase):
    def _client(self, chunks, timeout=5):
        rfd, wfd = os.pipe()
        proc = FakeProc(rfd)
        t = threading.Thread(target=feed, args=(wfd, chunks), daemon=True)
        t.start()
        self.addCleanup(lambda: (os.close(wfd) if _fd_open(wfd) else None))
        return protocol.AppServerClient(spawn=lambda: proc, timeout=timeout), proc

    def test_cleanup_kills_saved_group(self):
        import signal as _sig
        import unittest.mock as mock
        c, proc = self._client([
            json.dumps({"jsonrpc": "2.0", "id": 1, "result": {}}),
        ])
        with mock.patch("os.killpg") as kg:
            with c:
                pass
        sigs = [call.args[1] for call in kg.call_args_list]
        self.assertIn(_sig.SIGTERM, sigs)
        self.assertIn(_sig.SIGKILL, sigs)
        # Group addressed by the pid captured at spawn, not re-resolved.
        for call in kg.call_args_list:
            self.assertEqual(call.args[0], proc.pid)


class TestModelPages(unittest.TestCase):
    def _paged(self, pages):
        c = protocol.AppServerClient.__new__(protocol.AppServerClient)
        it = {"pages": list(pages)}

        def fake_list(limit=50, cursor=None):
            return it["pages"].pop(0)

        c.model_list = fake_list
        return c

    def test_repeated_cursor_fails(self):
        page = {"data": [], "nextCursor": "c1"}
        c = self._paged([page, page])
        with self.assertRaises(protocol.ProtocolError) as ctx:
            list(c.iter_models())
        self.assertEqual(str(ctx.exception), "model-pages-loop")

    def test_page_cap_fails(self):
        pages = [{"data": [], "nextCursor": f"c{i}"} for i in range(30)]
        c = self._paged(pages)
        with self.assertRaises(protocol.ProtocolError) as ctx:
            list(c.iter_models())
        self.assertEqual(str(ctx.exception), "model-pages-exceeded")


if __name__ == "__main__":
    unittest.main()
