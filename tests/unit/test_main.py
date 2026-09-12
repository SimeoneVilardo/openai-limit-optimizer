"""Main self-checks: poll wiring, once/check codes, login, health (faked)."""

import json
import os
import subprocess
import unittest
from unittest import mock

from app import config, main, protocol
from app import state as statemod


def make_cfg(tmp, **over):
    base = dict(config.DEFAULTS)
    base["OLO_STATE_FILE"] = os.path.join(tmp, "state.json")
    base["OLO_HEARTBEAT_FILE"] = os.path.join(tmp, "hb.json")
    base["OLO_CODEX_HOME"] = os.path.join(tmp, "codex")
    base["OLO_CONFIRM_SECONDS"] = "21"
    base.update(over)
    return config.load(base)


class FakeClient:
    """Yields a moving full-window pair; records requested methods."""

    BASE = 1789241400

    def __init__(self, cfg):
        self.cfg = cfg
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def account_read(self):
        self.calls.append("account/read")
        return {"account": {"type": "chatgpt"}, "requiresOpenaiAuth": False}

    def ensure_model(self, model_id, effort):
        self.calls.append(f"model/ensure:{model_id}:{effort}")
        if model_id != self.cfg.model or effort != self.cfg.effort:
            raise protocol.ProtocolError("model-unknown")
        return True

    def rate_limits_read(self):
        self.calls.append("rateLimits/read")
        n = self.calls.count("rateLimits/read")
        resets = self.BASE + (n - 1) * 21 + 18000
        bucket = {"primary": {"usedPercent": 0, "windowDurationMins": 300,
                              "resetsAt": resets},
                  "secondary": {"usedPercent": 16, "windowDurationMins": 10080,
                                "resetsAt": 9999999999},
                  "limitId": "codex"}
        return {"ordinaryUsageAllowed": True, "rateLimits": bucket,
                "rateLimitsByLimitId": {"codex": bucket}}


def patched_time(walls, monos):
    return (mock.patch("time.sleep", return_value=None),
            mock.patch("time.time", side_effect=walls),
            mock.patch("time.monotonic", side_effect=monos),
            mock.patch("app.main.STOP"))


class TestMain(unittest.TestCase):
    def setUp(self):
        main.reset_stop()
        self.tmp = os.path.join("/tmp", f"olo-main-{os.getpid()}-{id(self)}")
        os.makedirs(self.tmp, exist_ok=True)
        self._stop_patcher = mock.patch("app.main.STOP")
        self.stop = self._stop_patcher.start()
        self.stop.is_set.return_value = False
        self.stop.wait.return_value = False
        self.addCleanup(self._stop_patcher.stop)

    def tearDown(self):
        main.reset_stop()
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_poll_once_allow_no_inference(self):
        cfg = make_cfg(self.tmp)
        client = FakeClient(cfg)
        walls = [FakeClient.BASE, FakeClient.BASE + 21]
        monos = [500.0, 521.0, 521.0]
        with mock.patch("time.sleep", return_value=None), \
             mock.patch("time.time", side_effect=walls), \
             mock.patch("time.monotonic", side_effect=monos):
            allow, reason = main.poll_once(cfg, client)
        self.assertEqual((allow, reason), (True, "allow"))
        self.assertNotIn("exec", " ".join(client.calls))
        self.assertIn(f"model/ensure:{cfg.model}:{cfg.effort}", client.calls)

    def test_poll_once_rejects_wrong_effort(self):
        cfg = make_cfg(self.tmp, OLO_EFFORT="high")
        client = FakeClient(make_cfg(self.tmp))  # advertises low only
        with mock.patch("time.sleep", return_value=None), \
             mock.patch("time.time",
                        side_effect=[FakeClient.BASE, FakeClient.BASE + 21]), \
             mock.patch("time.monotonic", side_effect=[500.0, 521.0, 521.0]):
            with self.assertRaises(protocol.ProtocolError):
                main.poll_once(cfg, client)

    def test_once_success_and_skip_codes(self):
        cfg = make_cfg(self.tmp)
        B = FakeClient.BASE
        walls = [B, B, B, B + 21, B + 21, B + 21, B + 21, B + 21] + [B + 21] * 10
        monos = [500.0, 500.0, 521.0, 521.0, 600.0]
        with mock.patch("app.main.protomod.AppServerClient",
                        return_value=FakeClient(cfg)), \
             mock.patch("app.send.run_send",
                        return_value=(True, "sent")) as rs, \
             mock.patch("time.sleep", return_value=None), \
             mock.patch("time.time", side_effect=walls), \
             mock.patch("time.monotonic", side_effect=monos):
            rc = main.cmd_daemon(cfg, once=True, dry_run=False)
        self.assertEqual(rc, 0)
        rs.assert_called_once()

    def test_once_poll_failure_nonzero_and_degraded(self):
        cfg = make_cfg(self.tmp)

        class BadClient(FakeClient):
            def __enter__(self):
                raise protocol.ProtocolError("timeout")

        with mock.patch("app.main.protomod.AppServerClient",
                        return_value=BadClient(cfg)):
            rc = main.cmd_daemon(cfg, once=True, dry_run=False)
        self.assertEqual(rc, 2)
        with open(cfg.heartbeat_file, encoding="utf-8") as fh:
            hb = json.load(fh)
        self.assertEqual(hb["status"], "degraded")

    def test_once_send_failure_nonzero(self):
        cfg = make_cfg(self.tmp)
        B = FakeClient.BASE
        walls = [B, B, B, B + 21, B + 21, B + 21, B + 21, B + 21] + [B + 21] * 10
        monos = [500.0, 500.0, 521.0, 521.0, 600.0]
        with mock.patch("app.main.protomod.AppServerClient",
                        return_value=FakeClient(cfg)), \
             mock.patch("app.send.run_send",
                        return_value=(False, "send-timeout")), \
             mock.patch("time.sleep", return_value=None), \
             mock.patch("time.time", side_effect=walls), \
             mock.patch("time.monotonic", side_effect=monos):
            rc = main.cmd_daemon(cfg, once=True, dry_run=False)
        self.assertEqual(rc, 2)

    def test_cooldown_after_failure_stays_degraded(self):
        cfg = make_cfg(self.tmp)
        journal = statemod.Journal(cfg.state_file, cfg.cooldown_seconds)
        with journal:
            journal.record_attempt_before(1789241400, 1.0)
            journal.record_outcome("failed", "send-timeout")
            with mock.patch("time.time", return_value=1789241400 + 100):
                disp, code = main._cycle(cfg, journal, False)
        self.assertEqual(disp, "cooldown")
        self.assertEqual(code, 1)  # degraded cooldown is still a failure
        with open(cfg.heartbeat_file, encoding="utf-8") as fh:
            hb = json.load(fh)
        self.assertEqual((hb["status"], hb["detail"]),
                         ("degraded", "cooldown-after-failure"))

    def test_cooldown_after_sent_is_quiet(self):
        cfg = make_cfg(self.tmp)
        journal = statemod.Journal(cfg.state_file, cfg.cooldown_seconds)
        with journal:
            journal.record_attempt_before(1789241400, 1.0)
            journal.record_outcome("sent", "sent")
            with mock.patch("time.time", return_value=1789241400 + 100):
                disp, code = main._cycle(cfg, journal, False)
        self.assertEqual((disp, code), ("cooldown", 0))

    def test_fresh_home_auth_missing_not_poll_failed(self):
        import shutil as _sh
        cfg = make_cfg(self.tmp)
        _sh.rmtree(cfg.codex_home, ignore_errors=True)

        class NoAuthClient(FakeClient):
            def account_read(self):
                raise protocol.AuthError("auth-required")

        journal = statemod.Journal(cfg.state_file, cfg.cooldown_seconds)
        with journal, \
             mock.patch("app.main.protomod.AppServerClient",
                        return_value=NoAuthClient(cfg)):
            disp, code = main._cycle(cfg, journal, False)
        self.assertEqual((disp, code), ("blocked_auth", 1))
        self.assertTrue(os.path.isdir(cfg.codex_home))  # bootstrapped 0700
        import stat as _st
        self.assertEqual(_st.S_IMODE(os.stat(cfg.codex_home).st_mode), 0o700)

    def test_unwritable_home_fails_visible(self):
        cfg = make_cfg(self.tmp)
        with mock.patch("app.main.protomod.ensure_codex_home",
                        side_effect=protocol.HomeError("unwritable")):
            journal = statemod.Journal(cfg.state_file, cfg.cooldown_seconds)
            with journal:
                disp, code = main._cycle(cfg, journal, False)
        self.assertEqual((disp, code), ("error", 2))

    def test_daemon_holds_lock_lifetime(self):
        cfg = make_cfg(self.tmp)
        held = {}
        B = FakeClient.BASE
        walls = [B, B, B, B + 21, B + 21, B + 21, B + 21, B + 21] + [B + 21] * 10
        monos = [500.0, 500.0, 521.0, 521.0, 600.0]

        class LockedClient(FakeClient):
            def __enter__(self):
                other = statemod.Journal(cfg.state_file,
                                         cfg.cooldown_seconds)
                try:
                    other.acquire()
                except statemod.LockedError:
                    held["locked"] = True
                else:
                    held["locked"] = False
                    other.release()
                return self

        with mock.patch("app.main.protomod.AppServerClient",
                        return_value=LockedClient(cfg)), \
             mock.patch("time.sleep", return_value=None), \
             mock.patch("time.time", side_effect=walls), \
             mock.patch("time.monotonic", side_effect=monos):
            rc = main.cmd_daemon(cfg, once=True, dry_run=True)
        self.assertEqual(rc, 0)
        self.assertTrue(held.get("locked"))

    def test_check_reports_effective_allow_no_writes(self):
        import io as _io
        import contextlib as _cl
        cfg = make_cfg(self.tmp)
        journal = statemod.Journal(cfg.state_file, cfg.cooldown_seconds)
        with journal:
            journal.record_attempt_before(int(__import__("time").time()), 1.0)
            journal.record_outcome("sent", "sent")
        before = open(cfg.state_file, "rb").read()
        buf = _io.StringIO()
        with mock.patch("app.main.protomod.AppServerClient",
                        return_value=FakeClient(cfg)), \
             mock.patch("time.sleep", return_value=None), \
             mock.patch("app.main.time") as _mt:
            _mt.time.side_effect = [FakeClient.BASE, FakeClient.BASE,
                                    FakeClient.BASE + 21]
            _mt.monotonic.side_effect = [500.0, 521.0, 521.0]
            with _cl.redirect_stdout(buf):
                rc = main.cmd_check(cfg, False)
        self.assertEqual(rc, 0)
        out = json.loads(buf.getvalue())
        self.assertEqual(out["allow"], True)
        self.assertEqual(out["effective_allow"], False)  # cooling down
        self.assertGreater(out["cooldown_remaining_s"], 0)
        self.assertEqual(open(cfg.state_file, "rb").read(), before)

    def test_check_corrupt_journal_rc2(self):
        import io as _io
        import contextlib as _cl
        cfg = make_cfg(self.tmp)
        with open(cfg.state_file, "w", encoding="utf-8") as fh:
            fh.write("{corrupt")
        buf = _io.StringIO()
        with _cl.redirect_stdout(buf):
            rc = main.cmd_check(cfg, False)
        self.assertEqual(rc, 2)
        self.assertEqual(json.loads(buf.getvalue())["reason"], "state-corrupt")

    def test_check_home_unwritable_rc2(self):
        import io as _io
        import contextlib as _cl
        cfg = make_cfg(self.tmp)
        buf = _io.StringIO()
        with mock.patch("app.main.protomod.ensure_codex_home",
                        side_effect=protocol.HomeError("unwritable")), \
             _cl.redirect_stdout(buf):
            rc = main.cmd_check(cfg, False)
        self.assertEqual(rc, 2)
        self.assertIn("home", json.loads(buf.getvalue())["reason"])

    def test_check_never_sends(self):
        cfg = make_cfg(self.tmp)
        with mock.patch("app.main.protomod.AppServerClient",
                        return_value=FakeClient(cfg)), \
             mock.patch("time.sleep", return_value=None), \
             mock.patch("app.main.time") as _mt:
            _mt.time.side_effect = [FakeClient.BASE, FakeClient.BASE,
                                    FakeClient.BASE + 21]
            _mt.monotonic.side_effect = [500.0, 521.0, 521.0]
            rc = main.cmd_check(cfg, False)
        self.assertEqual(rc, 0)

    def test_check_holds_lock_during_rpc(self):
        cfg = make_cfg(self.tmp)
        held = {}

        class LockedClient(FakeClient):
            def rate_limits_read(self):
                other = statemod.Journal(cfg.state_file,
                                         cfg.cooldown_seconds)
                try:
                    other.acquire()
                except statemod.LockedError:
                    held["locked"] = True
                else:
                    held["locked"] = False
                    other.release()
                return super().rate_limits_read()

        with mock.patch("app.main.protomod.AppServerClient",
                        return_value=LockedClient(cfg)), \
             mock.patch("time.sleep", return_value=None), \
             mock.patch("app.main.time") as _mt:
            _mt.time.side_effect = [FakeClient.BASE] * 6
            _mt.monotonic.side_effect = [500.0, 521.0, 521.0]
            main.cmd_check(cfg, False)
        self.assertTrue(held.get("locked"))

    def test_login_argv_lock_and_env(self):
        cfg = make_cfg(self.tmp)
        seen = {"argv": []}
        held = {}

        def fake_run(argv, **kw):
            seen["argv"].append(argv)
            seen["env"] = kw.get("env", {})
            other = statemod.Journal(cfg.state_file, cfg.cooldown_seconds)
            try:
                other.acquire()
            except statemod.LockedError:
                held["locked"] = True
            else:
                held["locked"] = False
                other.release()
            return subprocess.CompletedProcess(argv, 0)

        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-x"}), \
             mock.patch("app.main.subprocess.run", side_effect=fake_run):
            rc = main.cmd_login(cfg)
        self.assertEqual(rc, 0)
        self.assertEqual(seen["argv"][0],
                         [cfg.codex_bin, "login", "--device-auth",
                          "-c", 'cli_auth_credentials_store="file"'])
        self.assertEqual(seen["argv"][1], [cfg.codex_bin, "login", "status"])
        self.assertTrue(held.get("locked"))
        self.assertNotIn("OPENAI_API_KEY", seen["env"])
        self.assertEqual(seen["env"].get("CODEX_HOME"), cfg.codex_home)

    def test_healthcheck_codes(self):
        cfg = make_cfg(self.tmp)
        self.assertEqual(main.cmd_healthcheck(cfg), 2)  # missing
        with mock.patch("time.time", return_value=1789241400):
            main.write_heartbeat(cfg.heartbeat_file, "healthy", "loop")
            self.assertEqual(main.cmd_healthcheck(cfg), 0)
            main.write_heartbeat(cfg.heartbeat_file, "blocked_auth", "x")
            self.assertEqual(main.cmd_healthcheck(cfg), 1)
            main.write_heartbeat(cfg.heartbeat_file, "degraded", "poll-failed")
            self.assertEqual(main.cmd_healthcheck(cfg), 1)
        with open(cfg.heartbeat_file, "w") as fh:
            fh.write("[1,2]")  # malformed: no traceback, rc 2
        self.assertEqual(main.cmd_healthcheck(cfg), 2)
        with open(cfg.heartbeat_file, "w") as fh:
            json.dump({"status": "healthy", "ts_wall": 9999999999,
                       "detail": ""}, fh)
        self.assertEqual(main.cmd_healthcheck(cfg), 2)  # future ts
        hb = {"status": "healthy", "detail": "", "ts_wall": 1000}
        with open(cfg.heartbeat_file, "w") as fh:
            json.dump(hb, fh)
        self.assertEqual(main.cmd_healthcheck(cfg), 2)  # stale


if __name__ == "__main__":
    unittest.main()
