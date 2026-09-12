"""Send self-checks: exact ephemeral argv + safe settings (mocked spawn)."""

import io
import os
import subprocess
import unittest
from unittest import mock

from app import send


class CapturingStdin(io.BytesIO):
    def __init__(self):
        super().__init__()
        self.captured = b""

    def write(self, data):
        self.captured += bytes(data)
        return super().write(data)


class FakeProc:
    def __init__(self, out=b"", err=b"", code=0, hang=False):
        self.stdout = io.BytesIO(out)
        self.stderr = io.BytesIO(err)
        self.stdin = CapturingStdin()
        self.pid = 999999
        self.returncode = code
        self._hang = hang
        self.terminated = False

    def poll(self):
        if self._hang and not self.terminated:
            return None
        return self.returncode

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.terminated = True

    def wait(self, timeout=None):
        return self.returncode


class TestSendArgv(unittest.TestCase):
    def test_exact_argv(self):
        argv = send.build_argv("codex", "gpt-5.6-luna", "low",
                               'Answer only with "hi"', "/tmp/olo-empty-x")
        self.assertEqual(argv, [
            "codex", "exec",
            "--ephemeral", "--json",
            "--sandbox", "read-only",
            "--skip-git-repo-check",
            "--ignore-user-config", "--ignore-rules",
            "--disable", "shell_tool",
            "-C", "/tmp/olo-empty-x",
            "-m", "gpt-5.6-luna",
            "-c", "model_reasoning_effort=low",
            "-c", 'web_search="disabled"',
            'Answer only with "hi"',
        ])
        for banned in ("resume", "fork", "--dange", "apply"):
            self.assertNotIn(banned, " ".join(argv))

    def test_dash_prompt_via_stdin(self):
        argv = send.build_argv("codex", "m", "low", "-rm -rf", "/tmp/w")
        self.assertEqual(argv[-1], "-")
        self.assertNotIn("-rm -rf", argv)

        seen = {}

        def fake_popen(argv, **kw):
            seen["argv"] = argv
            proc = FakeProc()
            seen["proc"] = proc
            return proc

        with mock.patch("app.send.subprocess.Popen", side_effect=fake_popen):
            with mock.patch("app.send.tempfile.mkdtemp",
                            return_value="/tmp/olo-empty-dash"):
                os.makedirs("/tmp/olo-empty-dash", exist_ok=True)
                ok, detail = send.run_send("codex", "/data/codex", "m",
                                           "low", "-rm -rf", 30)
        self.assertEqual((ok, detail), (True, "sent"))
        self.assertEqual(seen["argv"][-1], "-")
        self.assertEqual(seen["proc"].stdin.captured, b"-rm -rf")

    def test_run_send_settings(self):
        seen = {}

        def fake_popen(argv, **kw):
            seen["argv"] = argv
            seen["env"] = kw["env"]
            seen["cwd"] = kw["cwd"]
            seen["shell"] = kw.get("shell", False)
            seen["stdin"] = kw.get("stdin")
            self.assertTrue(os.path.isdir(kw["cwd"]))
            self.assertEqual(os.listdir(kw["cwd"]), [])
            return FakeProc(out=b'{"a":1}\n')

        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-live-x"}):
            with mock.patch("app.send.subprocess.Popen",
                            side_effect=fake_popen):
                with mock.patch("app.send.tempfile.mkdtemp",
                                return_value="/tmp/olo-empty-test"):
                    os.makedirs("/tmp/olo-empty-test", exist_ok=True)
                    ok, detail = send.run_send("codex", "/data/codex",
                                               "gpt-5.6-luna", "low",
                                               'Answer only with "hi"', 300)
        self.assertEqual((ok, detail), (True, "sent"))
        self.assertEqual(seen["env"].get("CODEX_HOME"), "/data/codex")
        self.assertNotIn("OPENAI_API_KEY", seen["env"])
        self.assertNotIn("CODEX_ACCESS_TOKEN", seen["env"])
        self.assertEqual(seen["cwd"], "/tmp/olo-empty-test")
        self.assertFalse(seen["shell"])
        self.assertFalse(os.path.exists("/tmp/olo-empty-test"))

    def test_spawn_failure_visible(self):
        with mock.patch("app.send.subprocess.Popen",
                        side_effect=OSError("noexec")):
            ok, detail = send.run_send("codex", "/data/codex", "m", "low",
                                       "p", 300)
        self.assertEqual((ok, detail), (False, "send-spawn-failed"))

    def test_nonzero_exit_visible(self):
        with mock.patch("app.send.subprocess.Popen",
                        return_value=FakeProc(code=3)):
            ok, detail = send.run_send("codex", "/data/codex", "m", "low",
                                       "p", 300)
        self.assertEqual((ok, detail), (False, "send-exit-nonzero"))

    def test_timeout_and_streaming_cap(self):
        proc = FakeProc(hang=True)
        with mock.patch("app.send.subprocess.Popen", return_value=proc):
            ok, detail = send.run_send("codex", "/data/codex", "m", "low",
                                       "p", 1)
        self.assertEqual((ok, detail), (False, "send-timeout"))
        self.assertTrue(proc.terminated)

        big = FakeProc(out=b"x" * (send.MAX_OUTPUT_BYTES + 8))
        with mock.patch("app.send.subprocess.Popen", return_value=big):
            ok, detail = send.run_send("codex", "/data/codex", "m", "low",
                                       "p", 30)
        self.assertEqual((ok, detail), (False, "send-oversize-output"))


    def test_group_signalled_by_saved_pgid(self):
        import signal as _sig
        from unittest import mock as _mock
        proc = FakeProc(hang=True)
        with _mock.patch("app.send.subprocess.Popen", return_value=proc), \
             _mock.patch("os.killpg") as kg:
            ok, detail = send.run_send("codex", "/data/codex", "m", "low",
                                       "p", 1)
        self.assertEqual((ok, detail), (False, "send-timeout"))
        sigs = [call.args[1] for call in kg.call_args_list]
        self.assertIn(_sig.SIGTERM, sigs)
        self.assertIn(_sig.SIGKILL, sigs)
        for call in kg.call_args_list:
            self.assertEqual(call.args[0], proc.pid)

    def test_success_path_still_reaps_group(self):
        from unittest import mock as _mock
        with _mock.patch("app.send.subprocess.Popen",
                         return_value=FakeProc()), \
             _mock.patch("os.killpg") as kg:
            ok, _ = send.run_send("codex", "/data/codex", "m", "low",
                                  "p", 30)
        self.assertTrue(ok)
        self.assertTrue(kg.called)  # group cleanup independent of exit state


if __name__ == "__main__":
    unittest.main()
