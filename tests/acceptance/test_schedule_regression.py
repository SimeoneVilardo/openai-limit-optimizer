"""Independent regressions for main-review gaps: real poll_once + advancing fake clocks.

No live Codex/network/build. Owns only this new acceptance file.
Continuation ses_f6040d764ffeYBIL4sBnBnEHIA.
"""
import datetime as dt
import io
import json
import os
import shutil
import stat
import tempfile
import time as _realtime
import unittest
from contextlib import redirect_stdout
from unittest import mock

from app import config, main, policy, schedule, state

UTC = dt.timezone.utc


def epoch(s):
    return int(dt.datetime.fromisoformat(s).replace(tzinfo=UTC).timestamp())


def cfg_for(path, **extra):
    raw = dict(config.DEFAULTS)
    raw.update({
        "OLO_STATE_FILE": os.path.join(path, "state.json"),
        "OLO_HEARTBEAT_FILE": os.path.join(path, "heartbeat.json"),
        "OLO_CODEX_HOME": os.path.join(path, "codex"),
        "OLO_SCHEDULE_FILE": os.path.join(path, "schedule.json"),
    })
    raw.update(extra)
    return config.load(raw)


def moving_payload(resets_at):
    prim = {"usedPercent": 0, "windowDurationMins": 300, "resetsAt": resets_at}
    sec = {"usedPercent": 16, "windowDurationMins": 10080, "resetsAt": 9999999999}
    cb = {"primary": prim, "secondary": sec, "planType": "plus",
          "spendControlReached": False, "rateLimitReachedType": None,
          "limitId": "codex"}
    return {"ordinaryUsageAllowed": True, "rateLimits": cb,
            "rateLimitsByLimitId": {"codex": cb}}


class FakeClock:
    """Advancing wall+mono; wait() advances instantly (no real sleep)."""

    def __init__(self, wall, mono=1000.0):
        self.wall = float(wall)
        self.mono = float(mono)

    def time(self):
        return self.wall

    def monotonic(self):
        return self.mono

    def wait(self, timeout):
        adv = max(0.0, float(timeout))
        self.wall += adv
        self.mono += adv
        return False


class FakeClient:
    """Moving-window client: resets track the fake wall, so policy allows."""

    def __init__(self, clock, frozen=False, on_first_read=None):
        self.clock = clock
        self.frozen = frozen
        self.on_first_read = on_first_read
        self.reads = 0
        self._frozen_resets = None

    def account_read(self):
        return {"account": {"type": "chatgpt"}}

    def ensure_model(self, *a, **k):
        return True

    def rate_limits_read(self):
        self.reads += 1
        if self.on_first_read is not None and self.reads == 1:
            self.on_first_read()
        if self.frozen:
            if self._frozen_resets is None:
                self._frozen_resets = int(self.clock.wall) + 18000
            return moving_payload(self._frozen_resets)
        return moving_payload(int(self.clock.wall) + 18000)


class FakeServer:
    def __init__(self, client):
        self._client = client

    def __enter__(self):
        return self._client

    def __exit__(self, *exc):
        return False


def run_cycle_real_poll(cfg, clock, sched_times="09:30", tz="UTC",
                        client=None, dry_run=False, journal=None,
                        confirm=None):
    """One _cycle with real poll_once/policy, fake advancing clocks+RPC."""
    if confirm is not None:
        assert cfg.confirm_seconds == confirm
    store = schedule.ScheduleStore(cfg.schedule_file)
    if sched_times is not None:
        store.write((sched_times,) if sched_times else (), tz)
    if journal is None:
        journal = state.Journal(cfg.state_file, cfg.cooldown_seconds)
        journal.acquire()
        close = True
    else:
        close = False
    client = client or FakeClient(clock)
    main.STOP.clear()
    try:
        with mock.patch("app.main.time.time", side_effect=clock.time), \
                mock.patch("app.main.time.monotonic", side_effect=clock.monotonic), \
                mock.patch.object(main.STOP, "wait", side_effect=clock.wait), \
                mock.patch("app.main.protomod.AppServerClient",
                            return_value=FakeServer(client)), \
                mock.patch("app.send.run_send",
                            return_value=(True, "sent")) as send:
            with redirect_stdout(io.StringIO()):
                disp, code = main._cycle(cfg, journal, dry_run)
        return disp, code, client, send
    finally:
        if close:
            journal.release()


class TestStartupRecovery(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="olo-reg-start-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_daemon_once_corrupt_then_set_recovery_no_unbound(self):
        cfg = cfg_for(self.tmp)
        with open(cfg.schedule_file, "w", encoding="utf-8") as fh:
            json.dump({"version": 1, "times": ["bad"], "timezone": "UTC"}, fh)
        main.STOP.clear()
        with redirect_stdout(io.StringIO()):
            rc = main.cmd_daemon(cfg, once=True, dry_run=True)
        self.assertEqual(rc, 2)  # visible error, no traceback raise
        with open(cfg.heartbeat_file, encoding="utf-8") as fh:
            hb = json.load(fh)
        self.assertEqual(hb["status"], "error")
        with redirect_stdout(io.StringIO()):
            self.assertEqual(main.cmd_schedule(cfg, "set", ["09:30"], "UTC"), 0)
        # recovered daemon starts with no UnboundLocalError; use empty
        # schedule + mocked allow so the assertion is wall-clock independent
        with redirect_stdout(io.StringIO()):
            self.assertEqual(main.cmd_schedule(cfg, "reset"), 0)
        with mock.patch("app.main.protomod.AppServerClient") as cli, \
                mock.patch("app.main.poll_once", return_value=(False, "window-not-full")), \
                redirect_stdout(io.StringIO()):
            rc2 = main.cmd_daemon(cfg, once=True, dry_run=True)
        self.assertEqual(rc2, 0)

    def test_journal_corrupt_visible_error_not_traceback(self):
        cfg = cfg_for(self.tmp)
        with open(cfg.state_file, "w", encoding="utf-8") as fh:
            json.dump({}, fh)
        main.STOP.clear()
        with redirect_stdout(io.StringIO()):
            rc = main.cmd_daemon(cfg, once=True, dry_run=True)
        self.assertEqual(rc, 2)
        with open(cfg.heartbeat_file, encoding="utf-8") as fh:
            hb = json.load(fh)
        self.assertEqual(hb["status"], "error")


class TestWaitHeartbeatPreservation(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="olo-reg-wait-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.cfg = cfg_for(self.tmp, OLO_RESET_TIMES="09:30")

    def test_blocked_auth_not_turned_healthy(self):
        main.write_heartbeat(self.cfg.heartbeat_file, "blocked_auth", "auth-missing")
        with open(self.cfg.heartbeat_file, encoding="utf-8") as fh:
            before = json.load(fh)
        j = state.Journal(self.cfg.state_file, self.cfg.cooldown_seconds)
        main._write_wait_heartbeat(self.cfg, j, 0)
        with open(self.cfg.heartbeat_file, encoding="utf-8") as fh:
            after = json.load(fh)
        self.assertEqual(after["status"], "blocked_auth")
        self.assertEqual(after["detail"], "auth-missing")
        self.assertGreaterEqual(after["ts_wall"], before["ts_wall"])

    def test_failed_cooldown_stays_degraded_auth_and_poll(self):
        for exc, disp in [("auth", "blocked_auth"), ("poll", "degraded"),
                          ("schema", "degraded")]:
            with self.subTest(kind=exc):
                shutil.rmtree(self.tmp, ignore_errors=True)
                os.makedirs(self.tmp, exist_ok=True)
                cfg = cfg_for(self.tmp)
                j = state.Journal(cfg.state_file, cfg.cooldown_seconds)
                j.acquire()
                try:
                    now = 1790000000
                    j.record_attempt_before(now, 1.0)
                    j.record_outcome("failed", "boom")
                    if exc == "auth":
                        err = __import__("app.protocol", fromlist=["x"]).AuthError("a")
                    elif exc == "poll":
                        err = __import__("app.protocol", fromlist=["x"]).ProtocolError("p")
                    else:
                        err = policy.PolicyError("s")
                    with mock.patch("app.main.protomod.AppServerClient",
                                    side_effect=err), \
                            mock.patch("app.main.time.time", return_value=now + 10), \
                            redirect_stdout(io.StringIO()):
                        disp2, _ = main._cycle(cfg, j, False)
                    with open(cfg.heartbeat_file, encoding="utf-8") as fh:
                        hb = json.load(fh)
                    self.assertEqual(hb["status"], "degraded")
                    self.assertEqual(hb["detail"], "cooldown-after-failure")
                    self.assertIn(disp2, ("blocked_auth", "degraded", "cooldown"))
                finally:
                    j.release()

    def test_stop_clean(self):
        main.STOP.set()
        try:
            j = state.Journal(self.cfg.state_file, self.cfg.cooldown_seconds)
            store = schedule.ScheduleStore(self.cfg.schedule_file)
            got = main._wait_for_daemon(self.cfg, store, j, _realtime.time() + 600,
                                        None, None)
            self.assertIsNone(got)
        finally:
            main.STOP.clear()


class TestRuntimeEditDuringPoll(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="olo-reg-live-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_check_runtime_edit_during_rpc_gates_effective_allow(self):
        cfg = cfg_for(self.tmp)  # env empty -> legacy
        clock = FakeClock(epoch("2026-09-14T03:00:00"))

        def plant():
            schedule.ScheduleStore(cfg.schedule_file).write(("09:30",), "UTC")

        client = FakeClient(clock, on_first_read=plant)
        main.STOP.clear()
        with mock.patch("app.main.time.time", side_effect=clock.time), \
                mock.patch("app.main.time.monotonic", side_effect=clock.monotonic), \
                mock.patch.object(main.STOP, "wait", side_effect=clock.wait), \
                mock.patch("app.main.protomod.AppServerClient",
                            return_value=FakeServer(client)), \
                redirect_stdout(io.StringIO()) as out:
            rc = main.cmd_check(cfg, False)
        self.assertEqual(rc, 0)
        doc = json.loads(out.getvalue())
        self.assertTrue(doc["allow"])  # backend idle
        self.assertFalse(doc["effective_allow"])  # gated by planted schedule
        self.assertFalse(doc["schedule_allow"])
        self.assertFalse(os.path.exists(cfg.state_file))

    def test_dryrun_preattempt_gate_matches_real_send_gate(self):
        cfg = cfg_for(self.tmp)
        clock = FakeClock(epoch("2026-09-14T03:00:00"))

        def plant():
            schedule.ScheduleStore(cfg.schedule_file).write(("09:30",), "UTC")

        j = state.Journal(cfg.state_file, cfg.cooldown_seconds)
        j.acquire()
        try:
            main.STOP.clear()
            sched0 = schedule.Schedule((), "UTC")
            plan0 = schedule.Plan(sched0, None, True, False, False, "disabled")
            store = schedule.ScheduleStore(cfg.schedule_file)
            client = FakeClient(clock, on_first_read=plant)
            with mock.patch("app.main.time.time", side_effect=clock.time), \
                    mock.patch("app.main.time.monotonic", side_effect=clock.monotonic), \
                    mock.patch.object(main.STOP, "wait", side_effect=clock.wait), \
                    mock.patch("app.main.protomod.AppServerClient",
                                return_value=FakeServer(client)), \
                    mock.patch("app.send.run_send") as send, \
                    redirect_stdout(io.StringIO()):
                disp, _ = main._cycle(cfg, j, True, sched0, plan0, store)
            self.assertEqual(disp, "skip")
            send.assert_not_called()
            self.assertFalse(os.path.exists(cfg.state_file))
            # same planting with dry_run=False must also refuse to send
            shutil.rmtree(self.tmp, ignore_errors=True)
            os.makedirs(self.tmp, exist_ok=True)
            cfg2 = cfg_for(self.tmp)
            clock2 = FakeClock(epoch("2026-09-14T03:00:00"))
            j2 = state.Journal(cfg2.state_file, cfg2.cooldown_seconds)
            j2.acquire()
            try:
                main.STOP.clear()
                store2 = schedule.ScheduleStore(cfg2.schedule_file)
                c2 = FakeClient(clock2,
                                on_first_read=lambda: store2.write(("09:30",), "UTC"))
                with mock.patch("app.main.time.time", side_effect=clock2.time), \
                        mock.patch("app.main.time.monotonic", side_effect=clock2.monotonic), \
                        mock.patch.object(main.STOP, "wait", side_effect=clock2.wait), \
                        mock.patch("app.main.protomod.AppServerClient",
                                    return_value=FakeServer(c2)), \
                        mock.patch("app.send.run_send") as send2, \
                        redirect_stdout(io.StringIO()):
                    disp2, _ = main._cycle(cfg2, j2, False, sched0, plan0, store2)
                self.assertEqual(disp2, "skip")
                send2.assert_not_called()
            finally:
                j2.release()
        finally:
            try:
                j.release()
            except Exception:
                pass


class TestRealSendBoundary(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="olo-reg-send-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_03_waits_0430_sends_confirm30(self):
        cfg = cfg_for(self.tmp, OLO_RESET_TIMES="09:30")
        # 03:00 must wait with zero RPC
        clock = FakeClock(epoch("2026-09-14T03:00:00"))
        j = state.Journal(cfg.state_file, cfg.cooldown_seconds)
        j.acquire()
        try:
            main.STOP.clear()
            with mock.patch("app.main.time.time", side_effect=clock.time), \
                    mock.patch("app.main.time.monotonic", side_effect=clock.monotonic), \
                    mock.patch.object(main.STOP, "wait", side_effect=clock.wait), \
                    mock.patch("app.main.protomod.AppServerClient",
                                side_effect=AssertionError("early RPC")), \
                    redirect_stdout(io.StringIO()):
                disp, _ = main._cycle(cfg, j, False)
            self.assertEqual(disp, "scheduled-wait")
        finally:
            j.release()
        shutil.rmtree(self.tmp, ignore_errors=True)
        os.makedirs(self.tmp, exist_ok=True)
        cfg = cfg_for(self.tmp, OLO_RESET_TIMES="09:30")
        clock = FakeClock(epoch("2026-09-14T04:30:00"))
        disp, code, client, send = run_cycle_real_poll(cfg, clock)
        self.assertEqual(disp, "sent")
        self.assertEqual(code, 0)
        send.assert_called_once()
        with open(cfg.state_file, encoding="utf-8") as fh:
            rec = json.load(fh)
        # exact send timestamp inside 60s grace
        self.assertGreaterEqual(rec["last_attempt_wall"], epoch("2026-09-14T04:30:00"))
        self.assertLessEqual(rec["last_attempt_wall"], epoch("2026-09-14T04:31:00"))
        self.assertEqual(rec["outcome"], "sent")
        # not starved: next target is tomorrow
        nxt = schedule.plan_next(schedule.Schedule(("09:30",), "UTC"),
                                 rec["last_attempt_wall"] + 1,
                                 17999)
        self.assertEqual(nxt.target.date, dt.date(2026, 9, 15))

    def test_confirm600_sends_at_boundary(self):
        cfg = cfg_for(self.tmp, OLO_RESET_TIMES="09:30", OLO_CONFIRM_SECONDS="600")
        clock = FakeClock(epoch("2026-09-14T04:20:00"))
        disp, code, client, send = run_cycle_real_poll(
            cfg, clock, confirm=600)
        self.assertEqual(disp, "sent")
        send.assert_called_once()
        with open(cfg.state_file, encoding="utf-8") as fh:
            rec = json.load(fh)
        # strict: full confirm finishes exactly at activation, never early
        self.assertEqual(rec["last_attempt_wall"], epoch("2026-09-14T04:30:00"))

    def test_frozen_resets_safety_not_bypassed_at_activation(self):
        cfg = cfg_for(self.tmp, OLO_RESET_TIMES="09:30")
        clock = FakeClock(epoch("2026-09-14T04:30:00"))
        client = FakeClient(clock, frozen=True)
        disp, code, _, send = run_cycle_real_poll(cfg, clock, client=client)
        self.assertEqual(disp, "skip")
        send.assert_not_called()

    def test_cooldown_covering_activation_defers(self):
        cfg = cfg_for(self.tmp, OLO_RESET_TIMES="09:30")
        clock = FakeClock(epoch("2026-09-14T03:00:00"))
        j = state.Journal(cfg.state_file, cfg.cooldown_seconds)
        j.acquire()
        try:
            j.record_attempt_before(epoch("2026-09-14T03:00:00"), 1.0)
            j.record_outcome("sent", "ok")
            main.STOP.clear()
            with mock.patch("app.main.time.time", side_effect=clock.time), \
                    mock.patch("app.main.time.monotonic", side_effect=clock.monotonic), \
                    mock.patch.object(main.STOP, "wait", side_effect=clock.wait), \
                    mock.patch("app.main.protomod.AppServerClient",
                                side_effect=AssertionError("must not poll")), \
                    mock.patch("app.send.run_send") as send, \
                    redirect_stdout(io.StringIO()):
                disp, _ = main._cycle(cfg, j, False)
            # cooldown 5h from 03:00 ends 08:00 > grace 04:31 -> next-day target, no send
            self.assertIn(disp, ("scheduled-wait", "cooldown"))
            send.assert_not_called()
        finally:
            j.release()


class TestDenseAndAliases(unittest.TestCase):
    def test_1440_dense_bounded(self):
        times = tuple(f"{h:02d}:{m:02d}" for h in range(24) for m in range(60))
        self.assertEqual(len(times), 1440)
        s = schedule.Schedule(times, "UTC")
        start = _realtime.monotonic()
        p = schedule.plan_next(s, epoch("2026-09-14T03:00:00"), 0)
        elapsed = _realtime.monotonic() - start
        self.assertIsNotNone(p.target)
        self.assertLess(elapsed, 2.0)
        # multiple DST targets keep UTC order
        rome = schedule.Schedule(("00:30", "12:30", "23:30"), "Europe/Rome")
        pr = schedule.plan_next(rome, epoch("2026-09-14T03:00:00"), 0)
        self.assertIn(pr.target.time, ("00:30", "12:30", "23:30"))

    def test_symlink_and_hardlink_aliases_rejected(self):
        tmp = tempfile.mkdtemp(prefix="olo-reg-alias-")
        self.addCleanup(shutil.rmtree, tmp, True)
        real = os.path.join(tmp, "real")
        os.makedirs(real)
        state_p = os.path.join(real, "state.json")
        linkdir = os.path.join(tmp, "linkdir")
        os.symlink(real, linkdir)
        alias_sched = os.path.join(linkdir, "state.json")
        base = dict(config.DEFAULTS)
        base.update({
            "OLO_STATE_FILE": state_p,
            "OLO_HEARTBEAT_FILE": os.path.join(real, "hb.json"),
            "OLO_CODEX_HOME": os.path.join(real, "codex"),
            "OLO_SCHEDULE_FILE": alias_sched,
        })
        with self.assertRaises(config.ConfigError):
            config.load(base)
        # hardlink alias
        with open(state_p, "w", encoding="utf-8") as fh:
            fh.write("{}")
        hard = os.path.join(real, "sched.json")
        try:
            os.link(state_p, hard)
        except OSError:
            self.skipTest("hardlinks unavailable")
        base2 = dict(base)
        base2["OLO_SCHEDULE_FILE"] = hard
        with self.assertRaises(config.ConfigError):
            config.load(base2)


if __name__ == "__main__":
    unittest.main()
