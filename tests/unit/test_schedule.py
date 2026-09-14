"""Scheduler self-checks: pure timing, DST, persistence, and live edits."""

import contextlib
import datetime as dt
import io
import json
import os
import shutil
import stat
import tempfile
import unittest
from unittest import mock

from app import config, main, schedule, state


UTC = dt.timezone.utc


def epoch(value):
    return int(dt.datetime.fromisoformat(value).replace(tzinfo=UTC).timestamp())


def cfg_for(path, **extra):
    raw = dict(config.DEFAULTS)
    raw.update({
        "OLO_STATE_FILE": os.path.join(path, "state.json"),
        "OLO_HEARTBEAT_FILE": os.path.join(path, "heartbeat.json"),
        "OLO_CODEX_HOME": os.path.join(path, "codex"),
        "OLO_CONFIRM_SECONDS": "21",
    })
    raw.update(extra)
    return config.load(raw)


class TestPureSchedule(unittest.TestCase):
    def test_idle_03_target_0930_waits_for_0430_activation(self):
        s = schedule.Schedule(("09:30",), "UTC")
        p = schedule.plan_next(s, epoch("2026-09-14T03:00:00"), 0)
        self.assertEqual(p.target.time, "09:30")
        self.assertEqual(p.activation_epoch, epoch("2026-09-14T04:30:00"))
        self.assertFalse(p.allowed)
        self.assertEqual(p.wake_epoch, epoch("2026-09-14T04:29:30"))

    def test_disabled_schedule_is_legacy_allow(self):
        p = schedule.plan_next(schedule.Schedule((), "UTC"), epoch("2026-09-14T03:00:00"))
        self.assertTrue(p.allowed)
        self.assertEqual(p.reason, "disabled")
        self.assertIsNone(p.target)

    def test_multiple_times_sorted_and_earliest_reachable_wins(self):
        s = schedule.Schedule(("09:30", "09:00", "09:00"), "UTC")
        p = schedule.plan_next(s, epoch("2026-09-14T03:00:00"), 0)
        self.assertEqual((p.target.time, p.activation_epoch),
                         ("09:00", epoch("2026-09-14T04:00:00")))
        # A cooldown that misses both same-day activations moves to tomorrow,
        # rather than pretending either target was attained.
        p2 = schedule.plan_next(s, epoch("2026-09-14T03:00:00"), 18061)
        self.assertEqual(p2.target.date, dt.date(2026, 9, 15))
        self.assertEqual(p2.target.time, "09:00")

    def test_intermediate_send_requires_full_cooldown_gap(self):
        s = schedule.Schedule(("09:30",), "UTC")
        early = schedule.plan_next(s, epoch("2026-09-13T23:00:00"), 0)
        late = schedule.plan_next(s, epoch("2026-09-14T00:01:00"), 0)
        self.assertTrue(early.allowed)
        self.assertFalse(late.allowed)

    def test_target_never_activates_early_and_grace_is_bounded(self):
        s = schedule.Schedule(("09:30",), "UTC")
        activation = epoch("2026-09-14T04:30:00")
        before = schedule.plan_next(s, activation - 1, 0)
        self.assertFalse(before.due)
        at = schedule.plan_next(s, activation, 0)
        self.assertTrue(at.due)
        missed = schedule.plan_next(s, activation + schedule.LATENESS_GRACE_SECONDS + 1, 0)
        self.assertEqual(missed.target.date, dt.date(2026, 9, 15))

    def test_confirmation_can_start_at_activation_minus_confirm(self):
        path = tempfile.mkdtemp(prefix="olo-schedule-poll-")
        self.addCleanup(shutil.rmtree, path, True)
        cfg = cfg_for(path)

        class Client:
            def account_read(self):
                return {"account": {"type": "chatgpt"}}

            def ensure_model(self, *_args):
                return True

            def rate_limits_read(self):
                return {}

        # The first moving-window read is immediate when the daemon wakes at
        # A-confirm_seconds; monotonic and wall clocks have unrelated epochs.
        observation = {}
        with mock.patch("app.main.time.time",
                        side_effect=[1000, 1030]), \
                mock.patch("app.main.time.monotonic",
                           side_effect=[500, 530, 530]), \
                mock.patch("app.main.protomod.is_chatgpt_account",
                           return_value=True), \
                mock.patch("app.main.policymod.parse", return_value=object()), \
                mock.patch("app.main.policymod.decide",
                           return_value=(True, "allow")):
            got = main.poll_once(cfg, Client(), observation=observation)
        self.assertEqual(got, (True, "allow"))
        self.assertEqual((observation["wall1"], observation["wall2"]),
                         (1000, 1030))

    def test_midnight_and_dst_gap(self):
        midnight = schedule.Schedule(("00:30",), "UTC")
        p = schedule.plan_next(midnight, epoch("2026-09-13T12:00:00"), 0)
        self.assertEqual(p.target.date, dt.date(2026, 9, 14))
        self.assertIsNone(schedule.local_occurrence(
            dt.date(2026, 3, 29), "02:30", "Europe/Rome"
        ))
        rome = schedule.Schedule(("02:30",), "Europe/Rome")
        p2 = schedule.plan_next(rome, epoch("2026-03-28T12:00:00"), 0)
        self.assertEqual(p2.target.date, dt.date(2026, 3, 30))

    def test_ambiguous_dst_chooses_first_occurrence(self):
        got = schedule.local_occurrence(dt.date(2026, 10, 25), "02:30", "Europe/Rome")
        self.assertEqual(got, epoch("2026-10-25T00:30:00"))

    def test_strict_parse(self):
        self.assertEqual(schedule.parse_reset_times(" 09:30,09:30, 00:00 "),
                         ("00:00", "09:30"))
        for raw in ("9:30", "24:00", "09:60", "09:30,", ",09:30"):
            with self.assertRaises(ValueError):
                schedule.parse_reset_times(raw)


class TestScheduleStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="olo-schedule-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_override_persists_atomic_and_reset_restores_env(self):
        path = os.path.join(self.tmp, "schedule.json")
        store = schedule.ScheduleStore(path)
        written = store.write(("10:00", "09:30"), "Europe/Rome")
        self.assertEqual(written.times, ("09:30", "10:00"))
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        self.assertFalse(os.path.exists(path + ".tmp"))
        self.assertEqual(store.effective(("08:00",), "UTC").source, "override")
        store.write((), "UTC")
        self.assertEqual(store.effective(("08:00",), "UTC").times, ())
        store.reset()
        restored = store.effective(("08:00",), "UTC")
        self.assertEqual((restored.times, restored.timezone, restored.source),
                         (("08:00",), "UTC", "env"))

    def test_corrupt_override_fails_closed_without_env_fallback(self):
        path = os.path.join(self.tmp, "schedule.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"version": 1, "times": ["bad"], "timezone": "UTC"}, fh)
        store = schedule.ScheduleStore(path)
        with self.assertRaises(schedule.ScheduleCorruptError):
            store.effective(("08:00",), "UTC")

    def test_config_derives_path_and_rejects_sidecar_collisions(self):
        cfg = cfg_for(self.tmp)
        self.assertEqual(cfg.schedule_file,
                         os.path.join(self.tmp, "schedule.json"))
        for bad in (cfg.state_file + ".lock", cfg.state_file + ".tmp",
                    cfg.heartbeat_file, cfg.codex_home):
            with self.assertRaises(config.ConfigError):
                cfg_for(self.tmp, OLO_SCHEDULE_FILE=bad)
        with self.assertRaises(config.ConfigError):
            cfg_for(self.tmp, OLO_RESET_TIMES="09:30,25:00")
        with self.assertRaises(config.ConfigError):
            cfg_for(self.tmp, OLO_RESET_TIMEZONE="Not/AZone")

    def test_runtime_schedule_does_not_need_journal_lock(self):
        cfg = cfg_for(self.tmp)
        journal = state.Journal(cfg.state_file, cfg.cooldown_seconds)
        journal.acquire()
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                rc = main.cmd_schedule(cfg, "set", ["09:30"], "UTC")
            self.assertEqual(rc, 0)
        finally:
            journal.release()
        self.assertEqual(schedule.ScheduleStore(cfg.schedule_file).override().times,
                         ("09:30",))

    def test_schedule_show_never_calls_codex_or_journal_lock(self):
        cfg = cfg_for(self.tmp, OLO_RESET_TIMES="09:30")
        journal = state.Journal(cfg.state_file, cfg.cooldown_seconds)
        journal.acquire()
        try:
            with mock.patch("app.main.protomod.AppServerClient",
                            side_effect=AssertionError("RPC forbidden")), \
                    contextlib.redirect_stdout(io.StringIO()) as output:
                rc = main.cmd_schedule(cfg, "show")
        finally:
            journal.release()
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(output.getvalue())["source"], "env")

    def test_runtime_recheck_blocks_stale_allow(self):
        cfg = cfg_for(self.tmp)
        initial = schedule.Schedule((), "UTC")
        latest = schedule.Schedule(("09:30",), "UTC")
        blocked = schedule.Plan(latest, None, False, False, False,
                                "schedule-raced")
        initial_plan = schedule.Plan(initial, None, True, False, False,
                                     "disabled")
        journal = state.Journal(cfg.state_file, cfg.cooldown_seconds)
        with journal, mock.patch("app.main.poll_once", return_value=(True, "allow")), \
                mock.patch("app.main.protomod.AppServerClient"), \
                mock.patch("app.main._load_schedule",
                           side_effect=[initial, latest]), \
                mock.patch("app.main._schedule_plan",
                           return_value=blocked), \
                mock.patch("app.send.run_send") as send:
            disp, code = main._cycle(cfg, journal, False, initial,
                                     initial_plan)
        self.assertEqual((disp, code), ("skip", 0))
        send.assert_not_called()
        self.assertFalse(os.path.exists(cfg.state_file))


if __name__ == "__main__":
    unittest.main()
