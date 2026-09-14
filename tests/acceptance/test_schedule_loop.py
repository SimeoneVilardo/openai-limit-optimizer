"""Strengthened daemon-loop proof: real planner/wait/cycle/poll/policy.

Fake transport/send only. Continuation ses_f6040d764ffeYBIL4sBnBnEHIA.
Owns only this new acceptance file; production untouched.
"""
import datetime as dt
import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

from app import config, main, schedule, state

UTC = dt.timezone.utc


def epoch(s):
    return int(dt.datetime.fromisoformat(s).replace(tzinfo=UTC).timestamp())


A = epoch("2026-09-14T04:30:00")
START = epoch("2026-09-14T03:00:00")

RESET = "09:30"


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
    def __init__(self, wall, mono=1000.0):
        self.wall = float(wall)
        self.mono = float(mono)


class FakeClient:
    def __init__(self, clock):
        self.clock = clock
        self.reads = 0

    def account_read(self):
        return {"account": {"type": "chatgpt"}}

    def ensure_model(self, *a, **k):
        return True

    def rate_limits_read(self):
        self.reads += 1
        return moving_payload(int(self.clock.wall) + 18000)


class MixedClient(FakeClient):
    """Reads 1..2 straddle upstream reset at A: deny, then idle allows."""

    def __init__(self, clock):
        super().__init__(clock)
        self.reads = 0

    def rate_limits_read(self):
        self.reads += 1
        if self.reads == 1:
            return moving_payload(A)
        return moving_payload(int(self.clock.wall) + 18000)


class FakeServer:
    def __init__(self, client):
        self._c = client

    def __enter__(self):
        return self._c

    def __exit__(self, *exc):
        return False


def run_daemon(cfg, clock, client, max_wall=None, fix_at=None, fix_fn=None,
               max_waits=30000):
    send_walls = []
    waits = [0]
    fixed = [False]

    def fake_wait(timeout):
        waits[0] += 1
        if waits[0] > max_waits:
            main.STOP.set()
            return True
        adv = max(0.0, float(timeout))
        clock.wall += adv
        clock.mono += adv
        if fix_fn is not None and not fixed[0] and clock.wall >= fix_at:
            fix_fn()
            fixed[0] = True
        if max_wall is not None and clock.wall >= max_wall:
            main.STOP.set()
            return True
        return main.STOP.is_set()

    def fake_send(*a, **k):
        send_walls.append(int(clock.wall))
        main.STOP.set()
        return (True, "sent")

    main.STOP.clear()
    with mock.patch("app.main.time.time", side_effect=lambda: clock.wall), \
            mock.patch("app.main.time.monotonic", side_effect=lambda: clock.mono), \
            mock.patch.object(main.STOP, "wait", side_effect=fake_wait), \
            mock.patch("app.main.signal.signal", return_value=None), \
            mock.patch("app.main.protomod.AppServerClient",
                       return_value=FakeServer(client)), \
            mock.patch("app.send.run_send", side_effect=fake_send), \
            redirect_stdout(io.StringIO()):
        rc = main.cmd_daemon(cfg, once=False, dry_run=False)
    with open(cfg.state_file, encoding="utf-8") as fh:
        rec = json.load(fh)
    return rc, send_walls, rec, waits[0]


class TestDaemonLoopExact(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="olo-loop-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_loop_confirm30_sends_exactly_at_A(self):
        cfg = cfg_for(self.tmp, OLO_RESET_TIMES=RESET,
                      OLO_POLL_SECONDS="600", OLO_CONFIRM_SECONDS="30")
        clock = FakeClock(START)
        rc, walls, rec, nwaits = run_daemon(cfg, clock, FakeClient(clock),
                                            max_wall=A + 300)
        self.assertEqual(rc, 0)
        self.assertEqual(len(walls), 1)
        self.assertEqual(walls[0], A)  # strict: not A-60
        self.assertEqual(rec["last_attempt_wall"], A)
        self.assertEqual(rec["outcome"], "sent")
        self.assertGreater(nwaits, 50)  # traversed poll600/wake loop, not one _cycle

    def test_loop_confirm600_sends_exactly_at_A(self):
        cfg = cfg_for(self.tmp, OLO_RESET_TIMES=RESET,
                      OLO_POLL_SECONDS="600", OLO_CONFIRM_SECONDS="600")
        clock = FakeClock(START)
        rc, walls, rec, nwaits = run_daemon(cfg, clock, FakeClient(clock),
                                            max_wall=A + 300)
        self.assertEqual(rc, 0)
        self.assertEqual(len(walls), 1)
        self.assertEqual(walls[0], A)
        self.assertEqual(rec["last_attempt_wall"], A)
        self.assertGreater(nwaits, 50)

    def test_loop_corrupt_fixed_during_wait_recovers(self):
        cfg = cfg_for(self.tmp, OLO_RESET_TIMES="", OLO_POLL_SECONDS="600",
                      OLO_CONFIRM_SECONDS="30")
        with open(cfg.schedule_file, "w", encoding="utf-8") as fh:
            json.dump({"version": 1, "times": ["bad"], "timezone": "UTC"}, fh)
        clock = FakeClock(START)

        def fix():
            schedule.ScheduleStore(cfg.schedule_file).write((RESET,), "UTC")

        rc, walls, rec, nwaits = run_daemon(
            cfg, clock, FakeClient(clock), max_wall=A + 300,
            fix_at=START + 10, fix_fn=fix)
        self.assertEqual(rc, 0)
        self.assertEqual(len(walls), 1)
        self.assertEqual(walls[0], A)
        self.assertEqual(rec["last_attempt_wall"], A)
        self.assertLess(nwaits, 30000)  # fail-fast guard, no hang

    def test_loop_mixed_denial_retries_within_grace(self):
        cfg = cfg_for(self.tmp, OLO_RESET_TIMES=RESET,
                      OLO_POLL_SECONDS="600", OLO_CONFIRM_SECONDS="30")
        clock = FakeClock(START)
        client = MixedClient(clock)
        rc, walls, rec, nwaits = run_daemon(cfg, clock, client,
                                            max_wall=A + 700)
        self.assertEqual(client.reads >= 4, True)  # deny then fresh retry
        self.assertEqual(len(walls), 1)
        self.assertGreaterEqual(walls[0], A)
        self.assertLessEqual(walls[0], A + 60)
        self.assertEqual(rec["last_attempt_wall"], walls[0])


class AlwaysMixedClient(FakeClient):
    """Every policy pair straddles the upstream reset: deny, deny, stop."""

    def rate_limits_read(self):
        self.reads += 1
        if self.reads % 2 == 1:
            return moving_payload(A)
        return moving_payload(int(self.clock.wall) + 18000)


class AuthDeniedClient(FakeClient):
    def __init__(self, clock):
        super().__init__(clock)
        self.accounts = 0

    def account_read(self):
        self.accounts += 1
        return {"account": {"type": "apikey"}}

    def rate_limits_read(self):
        self.reads += 1
        raise AssertionError("no transport after auth denial")


class BoomClient(FakeClient):
    def rate_limits_read(self):
        from app import protocol as protomod

        self.reads += 1
        raise protomod.ProtocolError("boom")


class MalformedClient(FakeClient):
    def rate_limits_read(self):
        self.reads += 1
        return {"ordinaryUsageAllowed": True}


class FixedActiveClient(FakeClient):
    """Upstream window actively used; both reads full but nonzero usage."""

    def rate_limits_read(self):
        self.reads += 1
        prim = {"usedPercent": 100, "windowDurationMins": 300,
                "resetsAt": int(self.clock.wall) + 18000}
        sec = {"usedPercent": 16, "windowDurationMins": 10080,
               "resetsAt": 9999999999}
        cb = {"primary": prim, "secondary": sec, "planType": "plus",
              "spendControlReached": False, "rateLimitReachedType": None,
              "limitId": "codex"}
        return {"ordinaryUsageAllowed": True, "rateLimits": cb,
                "rateLimitsByLimitId": {"codex": cb}}


def run_cycle(cfg, clock, client, dry_run=False, send_result=(True, "sent")):
    """One real _cycle with advancing fake clocks; returns (disp, code, sends)."""
    sends = []
    j = state.Journal(cfg.state_file, cfg.cooldown_seconds)
    j.acquire()
    try:
        main.STOP.clear()

        def fake_wait(timeout):
            adv = max(0.0, float(timeout))
            clock.wall += adv
            clock.mono += adv
            return main.STOP.is_set()

        def fake_send(*a, **k):
            sends.append(int(clock.wall))
            return send_result

        with mock.patch("app.main.time.time", side_effect=lambda: clock.wall), \
                mock.patch("app.main.time.monotonic",
                           side_effect=lambda: clock.mono), \
                mock.patch.object(main.STOP, "wait", side_effect=fake_wait), \
                mock.patch("app.main.protomod.AppServerClient",
                           return_value=FakeServer(client)), \
                mock.patch("app.send.run_send", side_effect=fake_send), \
                redirect_stdout(io.StringIO()):
            disp, code = main._cycle(cfg, j, dry_run)
        return disp, code, sends
    finally:
        j.release()


class TestBoundaryRetryBounds(unittest.TestCase):
    """Repair limits: one fresh pair max, errors never accelerated."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="olo-loop-bounds-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_second_deny_stops_no_third_probe(self):
        cfg = cfg_for(self.tmp, OLO_RESET_TIMES=RESET,
                      OLO_POLL_SECONDS="600", OLO_CONFIRM_SECONDS="30")
        clock = FakeClock(A - 30)
        client = AlwaysMixedClient(clock)
        disp, _, sends = run_cycle(cfg, clock, client)
        self.assertEqual(disp, "skip")
        self.assertEqual(client.reads, 4)  # first pair + exactly one retry
        self.assertEqual(sends, [])
        self.assertFalse(os.path.exists(cfg.state_file))

    def test_auth_denial_not_accelerated(self):
        cfg = cfg_for(self.tmp, OLO_RESET_TIMES=RESET,
                      OLO_POLL_SECONDS="600", OLO_CONFIRM_SECONDS="30")
        clock = FakeClock(A - 30)
        client = AuthDeniedClient(clock)
        disp, _, sends = run_cycle(cfg, clock, client)
        self.assertEqual(disp, "blocked_auth")
        self.assertEqual(client.accounts, 1)
        self.assertEqual(client.reads, 0)
        self.assertEqual(sends, [])
        with open(cfg.heartbeat_file, encoding="utf-8") as fh:
            hb = json.load(fh)
        self.assertEqual(hb["status"], "blocked_auth")

    def test_protocol_denial_not_accelerated(self):
        cfg = cfg_for(self.tmp, OLO_RESET_TIMES=RESET,
                      OLO_POLL_SECONDS="600", OLO_CONFIRM_SECONDS="30")
        clock = FakeClock(A - 30)
        client = BoomClient(clock)
        disp, _, sends = run_cycle(cfg, clock, client)
        self.assertEqual(disp, "degraded")
        self.assertEqual(client.reads, 1)
        self.assertEqual(sends, [])

    def test_schema_denial_not_accelerated(self):
        cfg = cfg_for(self.tmp, OLO_RESET_TIMES=RESET,
                      OLO_POLL_SECONDS="600", OLO_CONFIRM_SECONDS="30")
        clock = FakeClock(A - 30)
        client = MalformedClient(clock)
        disp, _, sends = run_cycle(cfg, clock, client)
        self.assertEqual(disp, "degraded")
        self.assertEqual(client.reads, 1)
        self.assertEqual(sends, [])

    def test_failed_send_holds_durable_cooldown(self):
        cfg = cfg_for(self.tmp, OLO_RESET_TIMES=RESET,
                      OLO_POLL_SECONDS="600", OLO_CONFIRM_SECONDS="30")
        clock = FakeClock(A)
        j = state.Journal(cfg.state_file, cfg.cooldown_seconds)
        j.acquire()
        try:
            main.STOP.clear()
            sends = []
            client = FakeClient(clock)

            def fake_wait(timeout):
                adv = max(0.0, float(timeout))
                clock.wall += adv
                clock.mono += adv
                return main.STOP.is_set()

            def boom(*a, **k):
                sends.append(int(clock.wall))
                return (False, "boom")

            with mock.patch("app.main.time.time",
                            side_effect=lambda: clock.wall), \
                    mock.patch("app.main.time.monotonic",
                               side_effect=lambda: clock.mono), \
                    mock.patch.object(main.STOP, "wait", side_effect=fake_wait), \
                    mock.patch("app.main.protomod.AppServerClient",
                               return_value=FakeServer(client)), \
                    mock.patch("app.send.run_send", side_effect=boom), \
                    redirect_stdout(io.StringIO()):
                disp, _ = main._cycle(cfg, j, False)
            self.assertEqual(disp, "degraded")
            self.assertEqual(sends, [A + 30])  # full confirm elapses pre-send
            with open(cfg.state_file, encoding="utf-8") as fh:
                rec = json.load(fh)
            self.assertEqual((rec["last_attempt_wall"], rec["outcome"]),
                             (A + 30, "failed"))
            # immediate re-cycle: durable cooldown, no reattempt
            with mock.patch("app.main.time.time",
                            side_effect=lambda: clock.wall), \
                    mock.patch("app.main.time.monotonic",
                               side_effect=lambda: clock.mono), \
                    mock.patch.object(main.STOP, "wait", side_effect=fake_wait), \
                    mock.patch("app.main.protomod.AppServerClient",
                               return_value=FakeServer(client)), \
                    mock.patch("app.send.run_send", side_effect=boom) as s2, \
                    redirect_stdout(io.StringIO()):
                disp2, _ = main._cycle(cfg, j, False)
            self.assertEqual(disp2, "cooldown")
            s2.assert_not_called()
            with open(cfg.heartbeat_file, encoding="utf-8") as fh:
                hb = json.load(fh)
            self.assertEqual(hb["status"], "degraded")
        finally:
            j.release()

    def test_insufficient_grace_retry_not_forced(self):
        cfg = cfg_for(self.tmp, OLO_RESET_TIMES=RESET,
                      OLO_POLL_SECONDS="600", OLO_CONFIRM_SECONDS="30")
        clock = FakeClock(A + 50)  # 10s of grace left, confirm needs 30
        client = MixedClient(clock)
        disp, _, sends = run_cycle(cfg, clock, client)
        self.assertEqual(disp, "skip")
        self.assertEqual(client.reads, 2)
        self.assertEqual(sends, [])

    def test_schedule_edit_cancels_stale_retry(self):
        cfg = cfg_for(self.tmp, OLO_RESET_TIMES=RESET,
                      OLO_POLL_SECONDS="600", OLO_CONFIRM_SECONDS="30")
        clock = FakeClock(A - 30)
        store = schedule.ScheduleStore(cfg.schedule_file)

        def plant():
            store.write(("10:30",), "UTC")

        client = MixedClient(clock)
        orig = client.rate_limits_read

        def hooked():
            if client.reads == 0:
                plant()
            return orig()

        client.rate_limits_read = hooked
        disp, _, sends = run_cycle(cfg, clock, client)
        self.assertEqual(disp, "skip")
        self.assertEqual(client.reads, 2)  # retry cancelled, no fresh pair
        self.assertEqual(sends, [])

    def test_no_target_legacy_unchanged(self):
        cfg = cfg_for(self.tmp, OLO_POLL_SECONDS="600",
                      OLO_CONFIRM_SECONDS="30")
        clock = FakeClock(A - 30)
        client = FakeClient(clock)

        def malformed_once():
            client.reads += 1
            return {"ordinaryUsageAllowed": True}

        client.rate_limits_read = malformed_once
        disp, _, sends = run_cycle(cfg, clock, client)
        self.assertEqual(disp, "degraded")
        self.assertEqual(client.reads, 1)  # single pair, never retried
        self.assertEqual(sends, [])

    def test_intermediate_active_deny_no_retry_wait(self):
        # 23:00 prior day, target 09:30 (A 04:30): intermediate send allowed
        # but upstream window is actively used. The denial must return after
        # exactly one pair; the old retry waited ~5.5h for A-confirm.
        t0 = epoch("2026-09-13T23:00:00")
        cfg = cfg_for(self.tmp, OLO_RESET_TIMES=RESET,
                      OLO_POLL_SECONDS="600", OLO_CONFIRM_SECONDS="30")
        clock = FakeClock(t0)
        client = FixedActiveClient(clock)
        waits = []
        sends = []
        j = state.Journal(cfg.state_file, cfg.cooldown_seconds)
        j.acquire()
        try:
            main.STOP.clear()

            def fake_wait(timeout):
                waits.append(float(timeout))
                # watchdog: fail the hours-long retry promptly in virtual time
                if sum(waits) > 120.0:
                    main.STOP.set()
                    return True
                adv = max(0.0, float(timeout))
                clock.wall += adv
                clock.mono += adv
                return main.STOP.is_set()

            def fake_send(*a, **k):
                sends.append(int(clock.wall))
                return (True, "sent")

            with mock.patch("app.main.time.time",
                            side_effect=lambda: clock.wall), \
                    mock.patch("app.main.time.monotonic",
                               side_effect=lambda: clock.mono), \
                    mock.patch.object(main.STOP, "wait", side_effect=fake_wait), \
                    mock.patch("app.main.protomod.AppServerClient",
                               return_value=FakeServer(client)), \
                    mock.patch("app.send.run_send", side_effect=fake_send), \
                    redirect_stdout(io.StringIO()):
                disp, _ = main._cycle(cfg, j, False)
        finally:
            j.release()
        self.assertEqual(disp, "skip")
        self.assertEqual(client.reads, 2)  # one pair, no boundary retry
        self.assertEqual(sends, [])
        self.assertEqual(int(clock.wall), t0 + 30)  # only the confirm elapsed
        self.assertLessEqual(sum(waits), 30.0 + 1e-9)
        for w in waits:
            self.assertLessEqual(w, 1.0 + 1e-9)  # poll chunks, no 5s retry wait
        self.assertFalse(os.path.exists(cfg.state_file))


if __name__ == "__main__":
    unittest.main()
