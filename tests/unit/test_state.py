"""State journal self-checks: cooldown survival, strict schema, perms."""

import json
import os
import stat
import unittest

from app import state


class TestJournal(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.join("/tmp", f"olo-test-{os.getpid()}-{id(self)}")
        os.makedirs(self.tmp, exist_ok=True)
        self.path = os.path.join(self.tmp, "state.json")

    def tearDown(self):
        for name in os.listdir(self.tmp):
            try:
                os.remove(os.path.join(self.tmp, name))
            except OSError:
                pass
        try:
            os.rmdir(self.tmp)
        except OSError:
            pass

    def test_failed_send_survives_restart(self):
        now = 1789241400
        with state.Journal(self.path, 18000) as j:
            j.record_attempt_before(now, 100.0)
            j.record_outcome("failed", "send-timeout")
        # "Restart": brand-new Journal instance on the same file.
        with state.Journal(self.path, 18000) as j2:
            self.assertEqual(j2.cooldown_remaining(now + 100), 17900)
            self.assertGreater(j2.cooldown_remaining(now + 17999), 0)
            self.assertEqual(j2.cooldown_remaining(now + 18000), 0)
            self.assertEqual(j2.last_outcome(), "failed")

    def test_unknown_outcome_still_cools_down(self):
        now = 1789241400
        with state.Journal(self.path, 18000) as j:
            j.record_attempt_before(now, 100.0)
            # crash before record_outcome: pending entry still blocks.
        with state.Journal(self.path, 18000) as j2:
            self.assertGreater(j2.cooldown_remaining(now + 10), 0)
            self.assertEqual(j2.last_outcome(), "pending")

    def test_absent_is_uninitialized(self):
        with state.Journal(self.path, 18000) as j:
            self.assertEqual(j.cooldown_remaining(1789241400), 0)
            self.assertIsNone(j.last_outcome())

    def test_strict_schema_rejects(self):
        bad = [
            "{not json",
            "{}",
            json.dumps({"outcome": "sent"}),  # outcome-only, no attempt
            json.dumps({"last_attempt_wall": True, "outcome": "sent",
                        "last_attempt_mono": 1.0}),
            json.dumps({"last_attempt_wall": "yesterday", "outcome": "sent"}),
            json.dumps({"last_attempt_wall": 5, "outcome": ""}),
            json.dumps({"last_attempt_wall": 5, "outcome": 5}),
            json.dumps({"last_attempt_wall": 5, "outcome": "sent",
                        "detail": 42}),
            "[1,2]",
        ]
        for content in bad:
            with open(self.path, "w", encoding="utf-8") as fh:
                fh.write(content)
            before = open(self.path, "rb").read()
            with state.Journal(self.path, 18000) as j:
                with self.assertRaises(state.CorruptedError, msg=content):
                    j.cooldown_remaining(1789241400)
            # Never silently repaired/overwritten.
            self.assertEqual(open(self.path, "rb").read(), before)

    def test_atomic_permissions_0600(self):
        with state.Journal(self.path, 18000) as j:
            j.record_attempt_before(1789241400, 1.0)
        mode = stat.S_IMODE(os.stat(self.path).st_mode)
        self.assertEqual(mode, 0o600)

    def test_concurrency_no_double_holder(self):
        j1 = state.Journal(self.path, 18000)
        j1.acquire()
        try:
            j2 = state.Journal(self.path, 18000)
            with self.assertRaises(state.LockedError):
                j2.acquire()
        finally:
            j1.release()
        # After release the second command may proceed.
        with state.Journal(self.path, 18000):
            pass

    def test_writes_without_lock_rejected(self):
        j = state.Journal(self.path, 18000)
        with self.assertRaises(state.StateError):
            j.record_attempt_before(1, 1.0)


if __name__ == "__main__":
    unittest.main()
