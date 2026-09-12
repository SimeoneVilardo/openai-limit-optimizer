"""Policy self-checks: every ambiguous shape must SKIP (fail closed)."""

import unittest

from app import policy

BASE = 1789241400


def payload(ordinary=True, used=0, resets_in="full", duration=300,
            with_map=True, secondary_used=16, extra="deny"):
    if resets_in == "full":
        resets_in = BASE + 18000
    if resets_in is None:
        prim = {"usedPercent": used, "windowDurationMins": duration,
                "resetsAt": None}
    else:
        prim = {"usedPercent": used, "windowDurationMins": duration,
                "resetsAt": resets_in}
    sec = {"usedPercent": secondary_used, "windowDurationMins": 10080,
           "resetsAt": 9999999999}
    bucket = {"primary": prim, "secondary": sec, "planType": "plus",
              "spendControlReached": False, "rateLimitReachedType": None,
              "limitId": "codex"}
    if with_map:
        by_id = {"codex": bucket}
        if extra == "exhausted":
            by_id["other"] = {"primary": {"usedPercent": 100,
                                          "windowDurationMins": 60,
                                          "resetsAt": 9999999999},
                              "secondary": None}
        return {"ordinaryUsageAllowed": ordinary, "rateLimits": bucket,
                "rateLimitsByLimitId": by_id}
    return {"ordinaryUsageAllowed": ordinary, "rateLimits": bucket}


def moving_pair(**kw):
    """Two full-window snapshots 30s apart with resets moved by 30s."""
    p1 = payload(resets_in=BASE + 18000, **kw)
    p2 = payload(resets_in=BASE + 30 + 18000, **kw)
    return policy.parse(p1), policy.parse(p2)


class TestPolicy(unittest.TestCase):
    def test_allow_moving_full_window(self):
        s1, s2 = moving_pair()
        ok, reason = policy.decide(s1, BASE, s2, BASE + 30, 100.0, 130.0, 30)
        self.assertEqual((ok, reason), (True, "allow"))

    def test_fixed_resets_denied(self):
        s1 = policy.parse(payload(resets_in=BASE + 18000))
        s2 = policy.parse(payload(resets_in=BASE + 18000))  # frozen target
        ok, reason = policy.decide(s1, BASE, s2, BASE + 30, 100.0, 130.0, 30)
        self.assertEqual((ok, reason), (False, "resets-fixed"))

    def test_rounded_nonzero_denied(self):
        s1, s2 = moving_pair(used=1)
        ok, reason = policy.decide(s1, BASE, s2, BASE + 30, 100.0, 130.0, 30)
        self.assertEqual((ok, reason), (False, "used-nonzero"))

    def test_active_mid_window_zero_denied(self):
        s1 = policy.parse(payload(resets_in=BASE + 7000))
        s2 = policy.parse(payload(resets_in=BASE + 30 + 7000))
        ok, reason = policy.decide(s1, BASE, s2, BASE + 30, 100.0, 130.0, 30)
        self.assertEqual((ok, reason), (False, "window-not-full"))

    def test_expired_denied(self):
        s1 = policy.parse(payload(resets_in=BASE - 10))
        s2 = policy.parse(payload(resets_in=BASE + 30 - 10))
        ok, reason = policy.decide(s1, BASE, s2, BASE + 30, 100.0, 130.0, 30)
        self.assertEqual((ok, reason), (False, "resets-expired"))

    def test_null_resets_denied(self):
        s1 = policy.parse(payload(resets_in=None))
        s2 = policy.parse(payload(resets_in=None))
        ok, reason = policy.decide(s1, BASE, s2, BASE + 30, 100.0, 130.0, 30)
        self.assertEqual((ok, reason), (False, "resets-null"))

    def test_beyond_5h_denied(self):
        s1 = policy.parse(payload(resets_in=BASE + 25000))
        s2 = policy.parse(payload(resets_in=BASE + 30 + 25000))
        ok, reason = policy.decide(s1, BASE, s2, BASE + 30, 100.0, 130.0, 30)
        self.assertEqual((ok, reason), (False, "resets-beyond-5h"))

    def test_weekly_exhausted_denied(self):
        s1, s2 = moving_pair(secondary_used=100)
        ok, reason = policy.decide(s1, BASE, s2, BASE + 30, 100.0, 130.0, 30)
        self.assertEqual((ok, reason), (False, "weekly-exhausted"))

    def test_other_exhausted_denied(self):
        s1, s2 = moving_pair(extra="exhausted")
        ok, reason = policy.decide(s1, BASE, s2, BASE + 30, 100.0, 130.0, 30)
        self.assertEqual((ok, reason), (False, "other-limit-exhausted"))

    def test_spend_reached_denied(self):
        raw1 = payload(resets_in=BASE + 18000)
        raw2 = payload(resets_in=BASE + 30 + 18000)
        for raw in (raw1, raw2):
            raw["rateLimitsByLimitId"]["codex"]["spendControlReached"] = True
        s1, s2 = policy.parse(raw1), policy.parse(raw2)
        ok, reason = policy.decide(s1, BASE, s2, BASE + 30, 100.0, 130.0, 30)
        self.assertEqual((ok, reason), (False, "spend-reached"))

    def test_reached_type_denied(self):
        raw1 = payload(resets_in=BASE + 18000)
        raw2 = payload(resets_in=BASE + 30 + 18000)
        for raw in (raw1, raw2):
            raw["rateLimitsByLimitId"]["codex"]["rateLimitReachedType"] = \
                "rate_limit_reached"
        s1, s2 = policy.parse(raw1), policy.parse(raw2)
        ok, reason = policy.decide(s1, BASE, s2, BASE + 30, 100.0, 130.0, 30)
        self.assertEqual((ok, reason), (False, "reached-type"))

    def test_ordinary_false_denied(self):
        s1 = policy.parse(payload(ordinary=False))
        s2 = policy.parse(payload(ordinary=False))
        ok, reason = policy.decide(s1, BASE, s2, BASE + 30, 100.0, 130.0, 30)
        self.assertEqual((ok, reason), (False, "ordinary-not-true"))

    def test_ordinary_unknown_denied(self):
        raw = payload()
        del raw["ordinaryUsageAllowed"]
        s = policy.parse(raw)
        ok, reason = policy.decide(s, BASE, s, BASE + 30, 100.0, 130.0, 30)
        self.assertEqual((ok, reason), (False, "ordinary-not-true"))

    def test_missing_codex_key_denied(self):
        raw = {"ordinaryUsageAllowed": True,
               "rateLimits": {"primary": {"usedPercent": 0,
                                          "windowDurationMins": 300,
                                          "resetsAt": BASE + 18000}},
               "rateLimitsByLimitId": {"other": {"primary": None,
                                                 "secondary": None}}}
        s = policy.parse(raw)
        ok, reason = policy.decide(s, BASE, s, BASE + 30, 100.0, 130.0, 30)
        self.assertEqual((ok, reason), (False, "codex-bucket-missing"))

    def test_fallback_other_bucket_denied(self):
        raw = {"ordinaryUsageAllowed": True,
               "rateLimits": {"limitId": "other",
                              "primary": {"usedPercent": 0,
                                          "windowDurationMins": 300,
                                          "resetsAt": BASE + 18000}}}
        s = policy.parse(raw)
        ok, reason = policy.decide(s, BASE, s, BASE + 30, 100.0, 130.0, 30)
        self.assertEqual((ok, reason), (False, "codex-bucket-missing"))

    def test_conflicting_map_limit_id_malformed(self):
        raw = payload()
        raw["rateLimitsByLimitId"]["codex"]["limitId"] = "other"
        with self.assertRaises(policy.PolicyError):
            policy.parse(raw)

    def test_separation_too_short(self):
        s1, s2 = moving_pair()
        ok, reason = policy.decide(s1, BASE, s2, BASE + 10, 100.0, 110.0, 30)
        self.assertEqual((ok, reason), (False, "separation-too-short"))

    def test_clock_jump_denied(self):
        s1, s2 = moving_pair()
        ok, reason = policy.decide(s1, BASE, s2, BASE + 30, 100.0, 105.0, 30)
        self.assertEqual((ok, reason), (False, "clock-jump"))

    def test_wrong_window_denied(self):
        s1, s2 = moving_pair(duration=60)
        ok, reason = policy.decide(s1, BASE, s2, BASE + 30, 100.0, 130.0, 30)
        self.assertEqual((ok, reason), (False, "window-not-300"))

    def test_malformed_schema_raises(self):
        with self.assertRaises(policy.PolicyError):
            policy.parse({"ordinaryUsageAllowed": True})
        with self.assertRaises(policy.PolicyError):
            policy.parse({"ordinaryUsageAllowed": "yes",
                          "rateLimits": {"primary": None}})
        with self.assertRaises(policy.PolicyError):
            policy.parse(None)

    def test_strict_optional_fields(self):
        bad_buckets = [
            {"spendControlReached": "true"},          # string, not bool
            {"spendControlReached": 1},
            {"rateLimitReachedType": 42},
            {"planType": False},
            {"limitId": 7},
            {"individualLimit": {"remainingPercent": True}},
            {"individualLimit": {"remainingPercent": "50"}},
            {"individualLimit": {"remainingPercent": -1}},
            {"individualLimit": "none"},
        ]
        for patch in bad_buckets:
            raw = payload()
            raw["rateLimitsByLimitId"]["codex"].update(patch)
            with self.assertRaises(policy.PolicyError, msg=str(patch)):
                policy.parse(raw)

    def test_negative_and_bad_numbers_rejected(self):
        for prim in ({"usedPercent": -1, "windowDurationMins": 300,
                      "resetsAt": BASE + 18000},
                     {"usedPercent": True, "windowDurationMins": 300,
                      "resetsAt": BASE + 18000},
                     {"usedPercent": 0, "windowDurationMins": -5,
                      "resetsAt": BASE + 18000},
                     {"usedPercent": 0, "windowDurationMins": 300,
                      "resetsAt": -3},
                     {"usedPercent": 0, "windowDurationMins": 300,
                      "resetsAt": True},
                     {"usedPercent": 0, "windowDurationMins": 300,
                      "resetsAt": 2 ** 41}):
            raw = {"ordinaryUsageAllowed": True,
                   "rateLimits": {"primary": prim}}
            with self.assertRaises(policy.PolicyError, msg=str(prim)):
                policy.parse(raw)

    def test_malformed_other_bucket_raises(self):
        raw = payload(extra="exhausted")
        raw["rateLimitsByLimitId"]["other"] = {"primary": "x"}
        with self.assertRaises(policy.PolicyError):
            policy.parse(raw)


if __name__ == "__main__":
    unittest.main()
