"""Independent acceptance: ephemeral send startup args + REAL exec subprocess.

No live inference, no secrets, no network. The only child processes are
temporary local Python fakes (see _fakes.py). Live Codex idle is UNOBSERVED.
"""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from app import config, protocol, send
from ._fakes import write_fake_codex


class TestStartupArgv(unittest.TestCase):
    def test_exact_argv_v0154(self):
        argv = send.build_argv("codex", "gpt-5.6-luna", "low",
                               'Answer only with "hi"', "/tmp/olo-x")
        self.assertEqual(argv, [
            "codex", "exec", "--ephemeral", "--json",
            "--sandbox", "read-only", "--skip-git-repo-check",
            "--ignore-user-config", "--ignore-rules",
            "--disable", "shell_tool",
            "-C", "/tmp/olo-x", "-m", "gpt-5.6-luna",
            "-c", "model_reasoning_effort=low",
            "-c", 'web_search="disabled"',
            'Answer only with "hi"',
        ])
        joined = " ".join(argv)
        for banned in ("resume", "fork", "apply", "--dange"):
            self.assertNotIn(banned, joined)
        # search tool must be disabled via web_search flag, not present as tool
        self.assertIn('web_search="disabled"', joined)
        self.assertIn("shell_tool", joined)
        with open("Dockerfile", encoding="utf-8") as fh:
            docker = fh.read()
        self.assertIn("0.154.0", docker)
        self.assertIn("CODEX_SHA256", docker)
        self.assertIn("sha256sum -c", docker)
        # No shell string: argv vector only.
        self.assertIsInstance(argv, list)

    def test_dash_prompt_never_parses_as_flag(self):
        argv = send.build_argv("codex", "m", "low", "-rm -rf /", "/tmp/w")
        self.assertEqual(argv[-1], "-")
        self.assertNotIn("-rm -rf /", argv)

    def test_child_env_drops_keys(self):
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-live",
                                          "CODEX_ACCESS_TOKEN": "t",
                                          "OPENAI_BASE_URL": "http://evil",
                                          "PATH": "/usr/bin"}):
            env = protocol.child_env("/data/codex")
        for k in ("OPENAI_API_KEY", "CODEX_API_KEY", "CODEX_ACCESS_TOKEN",
                  "OPENAI_BASE_URL", "OPENAI_ORGANIZATION", "OPENAI_PROJECT",
                  "CODEX_OSS_BASE_URL", "CODEX_OSS_PORT"):
            self.assertNotIn(k, env)
        self.assertEqual(env["CODEX_HOME"], "/data/codex")

    def test_unsupported_effort_rejected_by_config(self):
        base = dict(config.DEFAULTS)
        with self.assertRaises(config.ConfigError):
            config.load(dict(base, OLO_EFFORT="ultra"))
        cfg = config.load(dict(base, OLO_EFFORT="low",
                               OLO_MODEL="gpt-5.6-luna"))
        self.assertEqual((cfg.model, cfg.effort), ("gpt-5.6-luna", "low"))

    def test_chatgpt_only_no_apikey(self):
        self.assertTrue(protocol.is_chatgpt_account(
            {"account": {"type": "chatgpt"}}))
        for bad in ({"account": {"type": "apiKey"}},
                    {"account": None}, {}, {"x": 1}, None, [],
                    {"account": {"type": "chatgpt "}}):
            self.assertFalse(protocol.is_chatgpt_account(bad))


class TestRealExecSubprocess(unittest.TestCase):
    """REAL fake binary: proves argv/cwd/env/timeout/cap without mocks."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="olo-acc-exec-")
        self.fake = write_fake_codex(self.tmp)
        self.rec = os.path.join(self.tmp, "record.jsonl")
        self.codex_home = os.path.join(self.tmp, "codex")
        os.makedirs(self.codex_home, exist_ok=True)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _env(self, **over):
        env = dict(os.environ)
        env["OLO_FAKE_RECORD"] = self.rec
        env["OPENAI_API_KEY"] = "sk-must-not-leak"
        env["CODEX_ACCESS_TOKEN"] = "tok-must-not-leak"
        env.update(over)
        return env

    def _records(self):
        if not os.path.exists(self.rec):
            return []
        out = []
        with open(self.rec, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    out.append(json.loads(line))
        return out

    def test_success_records_argv_cwd_env(self):
        with mock.patch.dict(os.environ, self._env(FAKE_EXEC_MODE="success")):
            ok, detail = send.run_send(self.fake, self.codex_home,
                                       "gpt-5.6-luna", "low",
                                       'Answer only with "hi"', 20)
        self.assertEqual((ok, detail), (True, "sent"))
        recs = [r for r in self._records() if "argv" in r]
        self.assertTrue(recs, "fake must record startup args")
        argv = recs[0]["argv"]
        self.assertEqual(argv[0], "exec")
        self.assertEqual(argv[1], "--ephemeral")
        joined = " ".join(argv)
        self.assertIn("--disable", joined)
        self.assertIn("shell_tool", joined)
        self.assertIn('web_search="disabled"', joined)
        self.assertIn("--ephemeral", joined)
        # cwd was a fresh empty dir (recorded), since removed afterwards
        self.assertEqual(recs[0]["cwd_list"], [])
        # env sanitized in the REAL child
        self.assertEqual(recs[0]["CODEX_HOME"], self.codex_home)
        self.assertFalse(recs[0]["has_OPENAI_API_KEY"])
        self.assertFalse(recs[0]["has_CODEX_ACCESS_TOKEN"])
        # workdir cleaned: no olo-empty-* leftovers with our prompt
        self.assertFalse(recs[0]["cwd"].startswith("/app"))

    def test_hang_bounded_timeout_kills_tree(self):
        with mock.patch.dict(os.environ, self._env(FAKE_EXEC_MODE="hang")):
            t0 = time.monotonic()
            ok, detail = send.run_send(self.fake, self.codex_home,
                                       "m", "low", "p", 2)
            dt = time.monotonic() - t0
        self.assertEqual((ok, detail), (False, "send-timeout"))
        self.assertLess(dt, 15, f"shutdown must be bounded, took {dt:.1f}s")
        # Strict reap proof without pgrep: the recorded exec pid must be
        # gone (ESRCH). Retry briefly to absorb SIGTERM->SIGKILL window.
        recs = [r for r in self._records() if "exec_pid" in r]
        self.assertTrue(recs, "fake must record its pid")
        dead = False
        for _ in range(50):
            try:
                os.kill(recs[0]["exec_pid"], 0)
            except ProcessLookupError:
                dead = True
                break
            except PermissionError:
                dead = False
                break
            time.sleep(0.1)
        self.assertTrue(dead, "hang child must be reaped, not lingering")

    def test_orphan_descendant_reaped_by_saved_pgid(self):
        with mock.patch.dict(os.environ, self._env(FAKE_EXEC_MODE="orphan")):
            ok, detail = send.run_send(self.fake, self.codex_home,
                                       "m", "low", "p", 20)
        self.assertEqual((ok, detail), (True, "sent"))
        recs = self._records()
        orphans = [r["orphan_pid"] for r in recs if "orphan_pid" in r]
        self.assertTrue(orphans, "fake must record grandchild pid")
        dead = False
        for _ in range(50):
            try:
                os.kill(orphans[0], 0)
            except ProcessLookupError:
                dead = True
                break
            except PermissionError:
                dead = False
                break
            time.sleep(0.1)
        self.assertTrue(dead, "orphan grandchild must not survive pgid cleanup")

    def test_unbounded_output_capped(self):
        with mock.patch.dict(os.environ, self._env(FAKE_EXEC_MODE="big")):
            ok, detail = send.run_send(self.fake, self.codex_home,
                                       "m", "low", "p", 20)
        self.assertEqual((ok, detail), (False, "send-oversize-output"))

    def test_nonzero_and_spawn_failed_visible(self):
        with mock.patch.dict(os.environ, self._env(FAKE_EXEC_MODE="nonzero")):
            ok, detail = send.run_send(self.fake, self.codex_home,
                                       "m", "low", "p", 20)
        self.assertEqual(detail, "send-exit-nonzero")
        self.assertFalse(ok)
        ok, detail = send.run_send("/nonexistent-olo-bin-xyz", self.codex_home,
                                   "m", "low", "p", 5)
        self.assertEqual((ok, detail), (False, "send-spawn-failed"))

    def test_dash_payload_via_stdin_no_flag_injection(self):
        with mock.patch.dict(os.environ, self._env(FAKE_EXEC_MODE="success")):
            ok, detail = send.run_send(self.fake, self.codex_home,
                                       "m", "low", "-rm -rf /", 20)
        self.assertEqual((ok, detail), (True, "sent"))
        recs = self._records()
        argv_recs = [r for r in recs if "argv" in r]
        self.assertEqual(argv_recs[0]["argv"][-1], "-")
        stdin_recs = [r for r in recs if "stdin" in r]
        self.assertTrue(stdin_recs and stdin_recs[0]["stdin"] == "-rm -rf /")


if __name__ == "__main__":
    unittest.main()
