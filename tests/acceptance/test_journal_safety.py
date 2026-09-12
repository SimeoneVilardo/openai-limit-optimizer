"""Independent acceptance: journal strictness, flock lifetime, heartbeat/signal.

Simulated/filesystem only except one REAL fake binary for login args.
No live Codex, no secrets, no network. Cooldown floor is 18000s.
"""
import json
import os
import stat
import subprocess
import tempfile
import unittest
from unittest import mock

from app import config, main, state
from ._fakes import write_fake_codex


def make_cfg(tmpdir, **over):
    base = dict(config.DEFAULTS)
    base["OLO_STATE_FILE"] = os.path.join(tmpdir, "state.json")
    base["OLO_HEARTBEAT_FILE"] = os.path.join(tmpdir, "hb.json")
    base["OLO_CODEX_HOME"] = os.path.join(tmpdir, "codex")
    base["OLO_CONFIRM_SECONDS"] = "21"
    base.update(over)
    return config.load(base)


class TestJournalStrict(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="olo-acc-j-")
        self.path = os.path.join(self.tmp, "state.json")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_before_attempt_blocks_restart_all_outcomes(self):
        now = 1790000000
        for outcome in ("sent", "failed", "send-timeout", None):
            with self.subTest(outcome=outcome):
                p = os.path.join(self.tmp, f"s-{outcome}.json")
                with state.Journal(p, 18000) as j:
                    j.record_attempt_before(now, 1.0)
                    if outcome is not None:
                        j.record_outcome(outcome, "d")
                with state.Journal(p, 18000) as j2:
                    self.assertGreater(j2.cooldown_remaining(now + 10), 0)
                    self.assertEqual(j2.cooldown_remaining(now + 18000), 0)

    def test_cooldown_exact_boundary(self):
        now = 1790000000
        with state.Journal(self.path, 18000) as j:
            j.record_attempt_before(now, 1.0)
        with state.Journal(self.path, 18000) as j2:
            self.assertGreater(j2.cooldown_remaining(now + 17999), 0)
            self.assertEqual(j2.cooldown_remaining(now + 18000), 0)

    def test_empty_outcomeonly_booltime_rejected_no_overwrite(self):
        bad = [
            "{corrupt",
            "{}",
            json.dumps({"outcome": "sent"}),
            json.dumps({"last_attempt_wall": True, "outcome": "sent",
                        "last_attempt_mono": 1.0}),
            json.dumps({"last_attempt_wall": "yesterday",
                        "outcome": "sent"}),
            json.dumps({"last_attempt_wall": 5, "outcome": ""}),
            "[1,2]",
        ]
        for content in bad:
            with self.subTest(content=content[:30]):
                with open(self.path, "w", encoding="utf-8") as fh:
                    fh.write(content)
                with open(self.path, "rb") as fh:
                    before = fh.read()
                with state.Journal(self.path, 18000) as j:
                    with self.assertRaises(state.CorruptedError):
                        j.cooldown_remaining(1790000000)
                with open(self.path, "rb") as fh:
                    self.assertEqual(fh.read(), before)

    def test_absent_is_uninitialized(self):
        with state.Journal(self.path, 18000) as j:
            self.assertEqual(j.cooldown_remaining(1790000000), 0)
            self.assertIsNone(j.last_outcome())

    def test_atomic_no_tmp_perms_0600(self):
        with state.Journal(self.path, 18000) as j:
            j.record_attempt_before(1790000000, 1.0)
        self.assertTrue(os.path.exists(self.path))
        self.assertFalse(os.path.exists(self.path + ".tmp"))
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)
        with open(self.path, encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["outcome"], "pending")

    def test_flock_no_overlap_and_lock_required(self):
        j1 = state.Journal(self.path, 18000)
        j1.acquire()
        try:
            with self.assertRaises(state.LockedError):
                state.Journal(self.path, 18000).acquire()
        finally:
            j1.release()
        with state.Journal(self.path, 18000):
            pass
        with self.assertRaises(state.StateError):
            state.Journal(self.path, 18000).record_attempt_before(1, 1.0)

    def test_cooldown_floor_not_configurable_below_5h(self):
        base = dict(config.DEFAULTS)
        for v in ("60", "3600", "17999"):
            with self.assertRaises(config.ConfigError):
                config.load(dict(base, OLO_COOLDOWN_SECONDS=v,
                                 OLO_STATE_FILE="/tmp/a.json",
                                 OLO_HEARTBEAT_FILE="/tmp/b.json",
                                 OLO_CODEX_HOME="/tmp/c"))
        self.assertEqual(config.load(dict(
            base, OLO_COOLDOWN_SECONDS="18000",
            OLO_STATE_FILE="/tmp/a.json",
            OLO_HEARTBEAT_FILE="/tmp/b.json",
            OLO_CODEX_HOME="/tmp/c")).cooldown_seconds, 18000)


class TestLockLifetimeDryRunHeartbeat(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="olo-acc-d-")
        main.reset_stop()

    def tearDown(self):
        main.reset_stop()
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_dry_run_never_sends_nor_journals(self):
        cfg = make_cfg(self.tmp)
        with mock.patch("app.main.poll_once",
                         return_value=(True, "allow")) as pp, \
             mock.patch("app.main.protomod.AppServerClient"), \
             mock.patch("app.send.run_send") as rs:
            rc = main.cmd_daemon(cfg, once=True, dry_run=True)
        self.assertEqual(rc, 0)
        pp.assert_called_once()
        rs.assert_not_called()
        if os.path.exists(cfg.state_file):
            with open(cfg.state_file, encoding="utf-8") as fh:
                self.assertNotIn("last_attempt_wall", json.load(fh))

    def test_failures_degraded_and_once_nonzero(self):
        cfg = make_cfg(self.tmp)
        from app import protocol as _p

        class BadClient:
            def __init__(self, *a, **k):
                pass

            def __enter__(self):
                raise _p.ProtocolError("timeout")

            def __exit__(self, *e):
                return False

        with mock.patch("app.main.protomod.AppServerClient", BadClient):
            rc = main.cmd_daemon(cfg, once=True, dry_run=False)
        self.assertEqual(rc, 2)
        with open(cfg.heartbeat_file, encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["status"], "degraded")
        # cooldown after failure stays degraded (never hidden as healthy)
        # and reports nonzero so --once/supervisors still see the failure.
        journal = state.Journal(cfg.state_file, cfg.cooldown_seconds)
        with journal:
            journal.record_attempt_before(1789241400, 1.0)
            journal.record_outcome("failed", "send-timeout")
            with mock.patch("time.time", return_value=1789241400 + 100):
                disp, code = main._cycle(cfg, journal, False)
        self.assertEqual(disp, "cooldown")
        self.assertEqual(code, 1)
        with open(cfg.heartbeat_file, encoding="utf-8") as fh:
            hb = json.load(fh)
        self.assertEqual((hb["status"], hb["detail"]),
                         ("degraded", "cooldown-after-failure"))
        # cooldown after a past sent is quiet healthy/0.
        journal2 = state.Journal(cfg.state_file, cfg.cooldown_seconds)
        with journal2:
            journal2.record_attempt_before(1789241400, 1.0)
            journal2.record_outcome("sent", "sent")
            with mock.patch("time.time", return_value=1789241400 + 100):
                disp2, code2 = main._cycle(cfg, journal2, False)
        self.assertEqual((disp2, code2), ("cooldown", 0))

    def test_healthcheck_meaningful_and_signal(self):
        cfg = make_cfg(self.tmp)
        self.assertEqual(main.cmd_healthcheck(cfg), 2)
        with mock.patch("time.time", return_value=1789241400):
            main.write_heartbeat(cfg.heartbeat_file, "healthy", "loop")
            self.assertEqual(main.cmd_healthcheck(cfg), 0)
            main.write_heartbeat(cfg.heartbeat_file, "blocked_auth", "x")
            self.assertEqual(main.cmd_healthcheck(cfg), 1)
            main.write_heartbeat(cfg.heartbeat_file, "degraded", "poll-failed")
            self.assertEqual(main.cmd_healthcheck(cfg), 1)
        with open(cfg.heartbeat_file, "w") as fh:
            fh.write("[1,2]")
        self.assertEqual(main.cmd_healthcheck(cfg), 2)
        with open(cfg.heartbeat_file, "w") as fh:
            json.dump({"status": "healthy", "ts_wall": 9999999999,
                       "detail": ""}, fh)
        self.assertEqual(main.cmd_healthcheck(cfg), 2)  # future
        hb = {"status": "healthy", "detail": "", "ts_wall": 1000}
        with open(cfg.heartbeat_file, "w") as fh:
            json.dump(hb, fh)
        self.assertEqual(main.cmd_healthcheck(cfg), 2)  # stale
        # signal handling flips STOP
        main.reset_stop()
        self.assertFalse(main.STOP.is_set())
        main._on_term(15, None)
        self.assertTrue(main.STOP.is_set())
        main.reset_stop()

    def test_login_real_argv_lock_env(self):
        cfg = make_cfg(self.tmp)
        fake = write_fake_codex(self.tmp)
        cfg = make_cfg(self.tmp, OLO_CODEX_BIN=fake)
        rec = os.path.join(self.tmp, "login-record.jsonl")
        held = {}
        orig_run = subprocess.run

        def fake_run(argv, **kw):
            other = state.Journal(cfg.state_file, cfg.cooldown_seconds)
            try:
                other.acquire()
            except state.LockedError:
                held["locked"] = True
            else:
                held["locked"] = False
                other.release()
            return orig_run(argv, **kw)

        with mock.patch.dict(os.environ, {"OLO_FAKE_RECORD": rec,
                                          "OPENAI_API_KEY": "sk-x",
                                          "FAKE_LOGIN_MODE": "ok"}), \
             mock.patch("app.main.subprocess.run", side_effect=fake_run):
            rc = main.cmd_login(cfg)
        self.assertEqual(rc, 0)
        self.assertTrue(held.get("locked"), "login must hold flock")
        with open(rec, encoding="utf-8") as fh:
            recs = [json.loads(l) for l in fh if l.strip() and "argv" in l]
        self.assertEqual(recs[0]["argv"],
                         ["login", "--device-auth", "-c",
                          'cli_auth_credentials_store="file"'])
        self.assertEqual(recs[1]["argv"], ["login", "status"])
        self.assertFalse(recs[0]["has_OPENAI_API_KEY"])
        self.assertEqual(recs[0]["CODEX_HOME"], cfg.codex_home)


class TestFirstStartHomeAndCheckAuthority(unittest.TestCase):
    """REAL filesystem: home bootstrap 0700, check never writes, authority."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="olo-acc-h-")
        main.reset_stop()

    def tearDown(self):
        main.reset_stop()
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_ensure_home_first_start_0700(self):
        from app import protocol as _p
        home = os.path.join(self.tmp, "codex")
        _p.ensure_codex_home(home)
        self.assertTrue(os.path.isdir(home))
        self.assertEqual(stat.S_IMODE(os.stat(home).st_mode), 0o700)
        _p.ensure_codex_home(home)  # idempotent
        busy = os.path.join(self.tmp, "busy")
        with open(busy, "w") as fh:
            fh.write("x")
        with self.assertRaises(_p.HomeError):
            _p.ensure_codex_home(os.path.join(busy, "sub"))

    def test_cycle_bootstraps_missing_home_under_lock(self):
        import shutil as _sh
        cfg = make_cfg(self.tmp)
        _sh.rmtree(cfg.codex_home, ignore_errors=True)
        self.assertFalse(os.path.exists(cfg.codex_home))
        journal = state.Journal(cfg.state_file, cfg.cooldown_seconds)
        with journal:
            with mock.patch("app.main.protomod.AppServerClient",
                             side_effect=RuntimeError("stop-after-home")):
                try:
                    main._cycle(cfg, journal, False)
                except RuntimeError:
                    pass
        self.assertTrue(os.path.isdir(cfg.codex_home))
        self.assertEqual(stat.S_IMODE(os.stat(cfg.codex_home).st_mode), 0o700)

    def test_check_corrupt_journal_rc2_no_send(self):
        import io as _io
        import contextlib as _cl
        cfg = make_cfg(self.tmp)
        with open(cfg.state_file, "w", encoding="utf-8") as fh:
            fh.write("{corrupt")
        buf = _io.StringIO()
        with mock.patch("app.send.run_send") as rs, \
             _cl.redirect_stdout(buf):
            rc = main.cmd_check(cfg, False)
        self.assertEqual(rc, 2)
        self.assertEqual(json.loads(buf.getvalue())["reason"], "state-corrupt")
        rs.assert_not_called()

    def test_check_effective_allow_false_on_cooldown_no_writes(self):
        import io as _io
        import contextlib as _cl
        import time as _t
        cfg = make_cfg(self.tmp)
        journal = state.Journal(cfg.state_file, cfg.cooldown_seconds)
        with journal:
            journal.record_attempt_before(int(_t.time()), 1.0)
            journal.record_outcome("sent", "sent")
        before = open(cfg.state_file, "rb").read()
        mtime = os.stat(cfg.state_file).st_mtime_ns
        buf = _io.StringIO()

        class AllowClient:
            def __init__(self, *a, **k):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *e):
                return False

            def account_read(self):
                return {"account": {"type": "chatgpt"}}

            def ensure_model(self, *a, **k):
                return True

            def rate_limits_read(self):
                import time as _tt
                base = 1790000000
                # moving full-window pair is irrelevant here: cooldown
                # gates authority, but return a valid shape anyway.
                b = {"primary": {"usedPercent": 0, "windowDurationMins": 300,
                                 "resetsAt": base + 18000},
                     "secondary": {"usedPercent": 5,
                                   "windowDurationMins": 10080,
                                   "resetsAt": 9999999999}}
                return {"ordinaryUsageAllowed": True, "rateLimits": b,
                        "rateLimitsByLimitId": {"codex": b}}

        with mock.patch("app.main.protomod.AppServerClient", AllowClient), \
             mock.patch("app.send.run_send") as rs, \
             _cl.redirect_stdout(buf):
            rc = main.cmd_check(cfg, False)
        out = json.loads(buf.getvalue())
        # Authoritative: check never sends and never writes state, and its
        # allow is gated by the observed cooldown.
        rs.assert_not_called()
        self.assertEqual(open(cfg.state_file, "rb").read(), before)
        self.assertEqual(os.stat(cfg.state_file).st_mtime_ns, mtime)
        self.assertIn("effective_allow", out)
        self.assertFalse(out["effective_allow"])
        self.assertGreater(out["cooldown_remaining_s"], 0)
        self.assertEqual(rc, 0)


if __name__ == "__main__":
    unittest.main()
