"""Config self-checks: every invalid value must fail loudly."""

import unittest

from app import config


def env(**over):
    base = {k: v for k, v in config.DEFAULTS.items()}
    base.update(over)
    return base


class TestConfig(unittest.TestCase):
    def test_defaults_valid(self):
        cfg = config.load(env())
        self.assertEqual((cfg.poll_seconds, cfg.confirm_seconds,
                          cfg.model, cfg.effort, cfg.cooldown_seconds),
                         (600, 30, "gpt-5.6-luna", "low", 18000))

    def test_rejects(self):
        bad = [
            {"OLO_POLL_SECONDS": "10"},       # below min
            {"OLO_POLL_SECONDS": "huge"},     # not an int
            {"OLO_CONFIRM_SECONDS": "20"},    # not > 4*tolerance
            {"OLO_CONFIRM_SECONDS": "5"},
            {"OLO_MODEL": ""},                # empty
            {"OLO_MODEL": "gpt 5"},           # spaces
            {"OLO_EFFORT": "ultra"},          # unknown effort
            {"OLO_PROMPT": ""},               # empty prompt
            {"OLO_PROMPT": "x" * 501},        # too long
            {"OLO_RPC_TIMEOUT": "1"},
            {"OLO_SEND_TIMEOUT": "5"},
            {"OLO_COOLDOWN_SECONDS": "60"},     # below 5h floor
            {"OLO_COOLDOWN_SECONDS": "3600"},   # below 5h floor
            {"OLO_COOLDOWN_SECONDS": "17999"},  # just below floor
            {"OLO_STATE_FILE": "relative.json"},  # must be absolute
            {"OLO_STATE_FILE": "/data/x",
             "OLO_HEARTBEAT_FILE": "/data/x"},    # must differ
            {"OLO_STATE_FILE": "/data/codex/j",
             "OLO_HEARTBEAT_FILE": "/data/hb"},   # journal in CODEX_HOME
            {"OLO_HEARTBEAT_FILE": "/data/codex"},  # clashes with home
            {"OLO_STATE_FILE": "/data/codex"},      # clashes with home
        ]
        for over in bad:
            with self.assertRaises(config.ConfigError, msg=str(over)):
                config.load(env(**over))

    def test_cooldown_floor_boundary(self):
        self.assertEqual(config.load(env(OLO_COOLDOWN_SECONDS="18000"))
                         .cooldown_seconds, 18000)


if __name__ == "__main__":
    unittest.main()
