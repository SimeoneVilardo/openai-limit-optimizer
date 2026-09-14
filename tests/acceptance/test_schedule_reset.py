"""Independent verification: daily reset-target scheduling (mocked clock + real temp files).

Covers the contract without live Codex/network: legacy empty env, strict
HH:MM/IANA, R-18000 activation, earliest-reachable + 60s grace, canonical
03:00/09:30 deferral, CLI persistence/isolation, fail-closed corrupt,
path collisions, check/dry-run gating, and compose/env/Docker statics.
"""
import datetime as dt
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from app import config, main, schedule, state

UTC = dt.timezone.utc
REPO_ROOT = Path(__file__).resolve().parents[2]


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


class TestLegacyAndParsing(unittest.TestCase):
    def test_empty_env_is_legacy_disabled(self):
        cfg = cfg_for(tempfile.mkdtemp(prefix="olo-v-"))
        self.addCleanup(shutil.rmtree, os.path.dirname(cfg.state_file), True)
        self.assertEqual(cfg.reset_times, ())
        self.assertEqual(cfg.reset_timezone, "UTC")
        p = schedule.plan_next(schedule.Schedule((), "UTC"), epoch("2026-09-14T03:00:00"), 0)
        self.assertTrue(p.allowed)
        self.assertEqual(p.reason, "disabled")
        self.assertIsNone(p.target)

    def test_strict_times_and_iana(self):
        self.assertEqual(schedule.parse_reset_times(" 09:30, 09:30,00:00 "),
                         ("00:00", "09:30"))
        for bad in ("9:30", "24:00", "09:60", "09:30,", "09:30,,10:00", "  "):
            if bad.strip() == "":
                self.assertEqual(schedule.parse_reset_times(bad), ())
                continue
            with self.assertRaises(ValueError, msg=bad):
                schedule.parse_reset_times(bad)
        with self.assertRaises(ValueError):
            schedule.validate_timezone("Not/AZone")
        with self.assertRaises(ValueError):
            schedule.validate_timezone("/etc/passwd")
        with self.assertRaises(ValueError):
            schedule.validate_timezone("../UTC")
        self.assertEqual(schedule.validate_timezone("Europe/Rome"), "Europe/Rome")
        # config fail-closed
        tmp = tempfile.mkdtemp(prefix="olo-v-")
        self.addCleanup(shutil.rmtree, tmp, True)
        with self.assertRaises(config.ConfigError):
            cfg_for(tmp, OLO_RESET_TIMES="09:30,25:00")
        with self.assertRaises(config.ConfigError):
            cfg_for(tmp, OLO_RESET_TIMEZONE="Mars/Olympus")
        with self.assertRaises(config.ConfigError):
            cfg_for(tmp, OLO_COOLDOWN_SECONDS="17999")
        ok = cfg_for(tmp, OLO_COOLDOWN_SECONDS="18001")
        self.assertEqual(ok.cooldown_seconds, 18001)


class TestActivationSemantics(unittest.TestCase):
    def test_canonical_0300_defers_to_0430_no_early_send(self):
        s = schedule.Schedule(("09:30",), "UTC")
        p = schedule.plan_next(s, epoch("2026-09-14T03:00:00"), 0)
        self.assertEqual(p.activation_epoch, epoch("2026-09-14T04:30:00"))
        self.assertEqual(p.target_epoch, epoch("2026-09-14T09:30:00"))
        self.assertFalse(p.allowed)
        self.assertFalse(p.due)
        self.assertEqual(p.reason, "waiting-for-target")

    def test_full_cooldown_gap_gates_intermediate_send(self):
        s = schedule.Schedule(("09:30",), "UTC")
        # 23:00 prior day: 5.5h to activation -> full 5h fits
        early = schedule.plan_next(s, epoch("2026-09-13T23:00:00"), 0,
                                   cooldown_seconds=18000)
        self.assertTrue(early.allowed)
        # 00:01: only ~4.5h left -> must wait
        late = schedule.plan_next(s, epoch("2026-09-14T00:01:00"), 0,
                                  cooldown_seconds=18000)
        self.assertFalse(late.allowed)
        # configured longer cooldown is honoured
        big = schedule.plan_next(s, epoch("2026-09-13T22:00:00"), 0,
                                 cooldown_seconds=36000)
        self.assertFalse(big.allowed)

    def test_grace_bounded_60s(self):
        s = schedule.Schedule(("09:30",), "UTC")
        act = epoch("2026-09-14T04:30:00")
        self.assertFalse(schedule.plan_next(s, act - 1, 0).due)
        self.assertTrue(schedule.plan_next(s, act, 0).due)
        self.assertTrue(schedule.plan_next(s, act + 60, 0).due)
        nxt = schedule.plan_next(s, act + 61, 0)
        self.assertEqual(nxt.target.date, dt.date(2026, 9, 15))

    def test_earliest_reachable_skips_blocked(self):
        s = schedule.Schedule(("09:00", "09:30"), "UTC")
        p = schedule.plan_next(s, epoch("2026-09-14T03:00:00"), 0)
        self.assertEqual(p.target.time, "09:00")
        # cooldown covering both same-day activations -> tomorrow 09:00
        p2 = schedule.plan_next(s, epoch("2026-09-14T03:00:00"), 18061)
        self.assertEqual((p2.target.date, p2.target.time),
                         (dt.date(2026, 9, 15), "09:00"))

    def test_midnight_dst_gap_and_ambiguous_first(self):
        m = schedule.Schedule(("00:30",), "UTC")
        p = schedule.plan_next(m, epoch("2026-09-13T12:00:00"), 0)
        self.assertEqual((p.target.date, p.target.time),
                         (dt.date(2026, 9, 14), "00:30"))
        self.assertIsNone(schedule.local_occurrence(
            dt.date(2026, 3, 29), "02:30", "Europe/Rome"))
        rome = schedule.Schedule(("02:30",), "Europe/Rome")
        p2 = schedule.plan_next(rome, epoch("2026-03-28T12:00:00"), 0)
        self.assertEqual(p2.target.date, dt.date(2026, 3, 30))
        self.assertEqual(schedule.local_occurrence(
            dt.date(2026, 10, 25), "02:30", "Europe/Rome"),
                         epoch("2026-10-25T00:30:00"))


class TestStoreAndCLI(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="olo-vsch-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.cfg = cfg_for(self.tmp)

    def test_cli_roundtrip_atomic_and_isolated(self):
        before_journal = None
        if os.path.exists(self.cfg.state_file):
            with open(self.cfg.state_file, "rb") as fh:
                before_journal = fh.read()
        j = state.Journal(self.cfg.state_file, self.cfg.cooldown_seconds)
        j.acquire()
        try:
            with redirect_stdout(io.StringIO()):
                self.assertEqual(main.cmd_schedule(self.cfg, "set", ["10:00", "09:30"], "Europe/Rome"), 0)
            # usable while journal locked; override file is separate lock
            st = schedule.ScheduleStore(self.cfg.schedule_file).override()
            self.assertEqual((st.times, st.timezone), (("09:30", "10:00"), "Europe/Rome"))
            with redirect_stdout(io.StringIO()) as out:
                self.assertEqual(main.cmd_schedule(self.cfg, "show"), 0)
            doc = json.loads(out.getvalue())
            self.assertEqual(doc["source"], "override")
            with redirect_stdout(io.StringIO()):
                self.assertEqual(main.cmd_schedule(self.cfg, "clear"), 0)
            self.assertEqual(schedule.ScheduleStore(self.cfg.schedule_file).override().times, ())
            with redirect_stdout(io.StringIO()):
                self.assertEqual(main.cmd_schedule(self.cfg, "reset"), 0)
            self.assertIsNone(schedule.ScheduleStore(self.cfg.schedule_file).override())
        finally:
            j.release()
        # journal untouched (absent or byte-identical), perms atomic
        if before_journal is None:
            self.assertFalse(os.path.exists(self.cfg.state_file))
        p = self.cfg.schedule_file
        # after reset file gone; re-set to check perms then clean
        with redirect_stdout(io.StringIO()):
            main.cmd_schedule(self.cfg, "set", ["09:30"], "UTC")
        self.assertEqual(stat.S_IMODE(os.stat(p).st_mode), 0o600)
        self.assertFalse(os.path.exists(p + ".tmp"))

    def test_invalid_set_fail_closed_and_show_no_rpc(self):
        with redirect_stdout(io.StringIO()) as out:
            rc = main.cmd_schedule(self.cfg, "set", ["25:00"], "UTC")
        self.assertEqual(rc, 2)
        self.assertIn("schedule-error", out.getvalue())
        j = state.Journal(self.cfg.state_file, self.cfg.cooldown_seconds)
        j.acquire()
        try:
            with mock.patch("app.main.protomod.AppServerClient",
                            side_effect=AssertionError("RPC forbidden")), \
                    redirect_stdout(io.StringIO()) as out2:
                rc = main.cmd_schedule(self.cfg, "show")
            self.assertEqual(rc, 0)
            json.loads(out2.getvalue())
        finally:
            j.release()

    def test_corrupt_override_fail_closed_but_recoverable(self):
        with open(self.cfg.schedule_file, "w", encoding="utf-8") as fh:
            json.dump({"version": 1, "times": ["bad"], "timezone": "UTC"}, fh)
        store = schedule.ScheduleStore(self.cfg.schedule_file)
        with self.assertRaises(schedule.ScheduleCorruptError):
            store.effective(("08:00",), "UTC")
        with redirect_stdout(io.StringIO()) as out:
            rc = main.cmd_schedule(self.cfg, "show")
        self.assertEqual(rc, 2)
        self.assertIn("schedule-corrupt", out.getvalue())
        # set/reset usable as recovery (write path does not read prior)
        with redirect_stdout(io.StringIO()):
            self.assertEqual(main.cmd_schedule(self.cfg, "set", ["09:30"], "UTC"), 0)
        self.assertEqual(store.override().times, ("09:30",))
        # schema edge: extra keys / wrong types fail closed
        for bad in ({"version": 1, "times": ["09:30"]},
                    {"version": 1, "times": "09:30", "timezone": "UTC"},
                    {"version": "1", "times": ["09:30"], "timezone": "UTC"},
                    {"version": 2, "times": ["09:30"], "timezone": "UTC"}):
            with open(self.cfg.schedule_file, "w", encoding="utf-8") as fh:
                json.dump(bad, fh)
            with self.assertRaises(schedule.ScheduleCorruptError, msg=str(bad)):
                store.override()

    def test_path_collision_protected(self):
        for bad in (self.cfg.state_file + ".lock", self.cfg.state_file + ".tmp",
                    self.cfg.heartbeat_file, self.cfg.codex_home):
            with self.assertRaises(config.ConfigError, msg=bad):
                cfg_for(self.tmp, OLO_SCHEDULE_FILE=bad)

    def test_subprocess_show_live(self):
        env = dict(os.environ)
        env.update({
            "OLO_STATE_FILE": self.cfg.state_file,
            "OLO_HEARTBEAT_FILE": self.cfg.heartbeat_file,
            "OLO_CODEX_HOME": self.cfg.codex_home,
            "OLO_SCHEDULE_FILE": self.cfg.schedule_file,
            "OLO_RESET_TIMES": "09:30",
            "OLO_RESET_TIMEZONE": "UTC",
        })
        proc = subprocess.run([sys.executable, "-m", "app.main", "schedule", "show"],
                              capture_output=True, text=True, env=env, timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        doc = json.loads(proc.stdout)
        self.assertEqual(doc["times"], ["09:30"])
        self.assertEqual(doc["source"], "env")


class TestDaemonGating(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="olo-vgate-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.cfg = cfg_for(self.tmp)

    def test_stale_allow_blocked_by_fresh_reread(self):
        initial = schedule.Schedule((), "UTC")
        latest = schedule.Schedule(("09:30",), "UTC")
        blocked = schedule.Plan(latest, None, False, False, False, "schedule-raced")
        initial_plan = schedule.Plan(initial, None, True, False, False, "disabled")
        j = state.Journal(self.cfg.state_file, self.cfg.cooldown_seconds)
        with j, mock.patch("app.main.poll_once", return_value=(True, "allow")), \
                mock.patch("app.main.protomod.AppServerClient"), \
                mock.patch("app.main._load_schedule", side_effect=[initial, latest]), \
                mock.patch("app.main._schedule_plan", return_value=blocked), \
                mock.patch("app.send.run_send") as send:
            disp, code = main._cycle(self.cfg, j, False, initial, initial_plan)
        self.assertEqual((disp, code), ("skip", 0))
        send.assert_not_called()

    def test_check_effective_allow_gated_by_schedule(self):
        with mock.patch("app.main.protomod.AppServerClient") as cli, \
                mock.patch("app.main.poll_once", return_value=(True, "allow")):
            inst = cli.return_value.__enter__.return_value
            inst.account_read.return_value = {"account": {"type": "chatgpt"}}
            with redirect_stdout(io.StringIO()) as out:
                rc = main.cmd_check(self.cfg, False)
        self.assertEqual(rc, 0)
        doc = json.loads(out.getvalue())
        self.assertTrue(doc["allow"])
        self.assertTrue(doc["effective_allow"])  # empty schedule: legacy
        # now with waiting schedule, same poll allow must gate to False
        cfg2 = cfg_for(self.tmp, OLO_RESET_TIMES="09:30")
        with mock.patch("app.main.protomod.AppServerClient") as cli2, \
                mock.patch("app.main.poll_once", return_value=(True, "allow")), \
                mock.patch("app.main.time.time",
                            return_value=epoch("2026-09-14T03:00:00")):
            cli2.return_value.__enter__.return_value.account_read.return_value = \
                {"account": {"type": "chatgpt"}}
            with redirect_stdout(io.StringIO()) as out2:
                rc2 = main.cmd_check(cfg2, False)
        self.assertEqual(rc2, 0)
        doc2 = json.loads(out2.getvalue())
        self.assertFalse(doc2["effective_allow"])
        self.assertFalse(doc2["schedule_allow"])

    def test_dry_run_never_sends(self):
        j = state.Journal(self.cfg.state_file, self.cfg.cooldown_seconds)
        with j, mock.patch("app.main.protomod.AppServerClient"), \
                mock.patch("app.main.poll_once", return_value=(True, "allow")), \
                mock.patch("app.send.run_send") as send:
            disp, _ = main._cycle(self.cfg, j, True, schedule.Schedule((), "UTC"),
                                  schedule.Plan(schedule.Schedule((), "UTC"),
                                                None, True, False, False, "disabled"))
        self.assertEqual(disp, "skip")
        send.assert_not_called()
        self.assertFalse(os.path.exists(self.cfg.state_file))

    def test_wait_reload_bounded_5s(self):
        seen = []

        orig_wait = main.STOP.wait
        main.STOP.clear()
        try:
            def spy(timeout):
                seen.append(timeout)
                return True  # simulate stop so loop exits promptly
            with mock.patch.object(main.STOP, "wait", side_effect=spy):
                j = state.Journal(self.cfg.state_file, self.cfg.cooldown_seconds)
                store = schedule.ScheduleStore(self.cfg.schedule_file)
                main._wait_for_daemon(self.cfg, store, j,
                                      next_poll_at=9999999999.0,
                                      handled_activation=None,
                                      observed_schedule=None)
        finally:
            main.STOP.clear()
        self.assertTrue(seen)
        self.assertLessEqual(max(seen), 5.0 + 1e-9)


class TestStaticTimezoneSupport(unittest.TestCase):
    def test_compose_env_docker(self):
        repo = REPO_ROOT
        comp = (repo / "compose.yaml").read_text(encoding="utf-8")
        self.assertIn("OLO_RESET_TIMES", comp)
        self.assertIn("OLO_RESET_TIMEZONE", comp)
        self.assertIn("OLO_SCHEDULE_FILE", comp)
        env = (repo / "config/example.env").read_text(encoding="utf-8")
        self.assertIn("OLO_RESET_TIMES=", env)
        self.assertIn("OLO_RESET_TIMEZONE=UTC", env)
        self.assertIn("OLO_SCHEDULE_FILE=", env)
        dock = (repo / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn("tzdata", dock)


if __name__ == "__main__":
    unittest.main()
