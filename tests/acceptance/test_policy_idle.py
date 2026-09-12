"""Independent acceptance: idle-only moving-full-window policy (simulated).

All vectors are SIMULATED payloads; no live inference, no secrets, no
network. Live idle is UNOBSERVED by these tests: they prove the gate
would allow/deny given such readings, not that the backend is idle now.
"""

import unittest

from app import policy

BASE = 1790000000
CONFIRM = 30


def bucket(primary_used=0, duration=300, resets_at=None,
            secondary_used=16, spend=False, reached=None, indiv=None,
            plan="plus"):
    prim = {"usedPercent": primary_used, "windowDurationMins": duration,
            "resetsAt": resets_at}
    sec = {"usedPercent": secondary_used, "windowDurationMins": 10080,
           "resetsAt": 9999999999}
    b = {"primary": prim, "secondary": sec, "planType": plan,
         "spendControlReached": spend, "rateLimitReachedType": reached,
         "limitId": "codex"}
    if indiv is not None:
        b["individualLimit"] = {"remainingPercent": indiv}
    return b


def raw(ordinary=True, with_map=True, codex_kwargs=None, other_exhausted=False):
    codex_kwargs = codex_kwargs or {}
    if "resets_at" not in codex_kwargs:
        codex_kwargs = dict(codex_kwargs, resets_at=BASE + 18000)
    cb = bucket(**codex_kwargs)
    if with_map:
        by_id = {"codex": cb}
        if other_exhausted:
            by_id["other"] = {"primary": {"usedPercent": 100,
                                          "windowDurationMins": 60,
                                          "resetsAt": 9999999999},
                              "secondary": None}
        return {"ordinaryUsageAllowed": ordinary, "rateLimits": cb,
                "rateLimitsByLimitId": by_id}
    return {"ordinaryUsageAllowed": ordinary, "rateLimits": cb}


def allow_pair(r1_resets, r2_resets, wall1=BASE, wall2=None, mono1=1000.0,
               mono2=None, **kw):
    wall2 = BASE + CONFIRM if wall2 is None else wall2
    mono2 = mono1 + (wall2 - wall1) if mono2 is None else mono2
    k1 = dict(kw, resets_at=r1_resets)
    k2 = dict(kw, resets_at=r2_resets)
    s1 = policy.parse(raw(codex_kwargs=k1))
    s2 = policy.parse(raw(codex_kwargs=k2))
    return policy.decide(s1, wall1, s2, wall2, mono1, mono2, CONFIRM)


class TestMovingFullWindow(unittest.TestCase):
    def test_happy_allow(self):
        ok, reason = allow_pair(BASE + 18000, BASE + 30 + 18000)
        self.assertEqual((ok, reason), (True, "allow"))

    def test_zero_alone_insufficient_mid_window(self):
        # used==0 but resets in ~2h on BOTH reads: active window, must SKIP.
        ok, r = allow_pair(BASE + 7000, BASE + 30 + 7000)
        self.assertEqual((ok, r), (False, "window-not-full"))

    def test_both_readings_must_be_full(self):
        # First full, second drifted mid-window (only 17900 ahead).
        s1 = policy.parse(raw(codex_kwargs={"resets_at": BASE + 18000}))
        s2 = policy.parse(raw(codex_kwargs={"resets_at": BASE + 30 + 17900}))
        # wall2 sees rem = 17900 -> outside +/-5 -> deny. The gate checks
        # resets-fixed before per-window fullness, so either deny reason
        # is acceptable; both are fail-closed.
        ok, r = policy.decide(s1, BASE, s2, BASE + 30, 100.0, 130.0, CONFIRM)
        self.assertFalse(ok)
        self.assertIn(r, ("window-not-full", "resets-fixed"))

    def test_future_boundary_plus_minus_5(self):
        for delta in (-5, -4, 0, 4, 5):
            with self.subTest(delta=delta):
                ok, r = allow_pair(BASE + 18000 + delta,
                                   BASE + 30 + 18000 + delta)
                self.assertEqual((ok, r), (True, "allow"),
                                 msg=f"delta {delta}")

    def test_future_outside_tolerance(self):
        ok, r = allow_pair(BASE + 18000 - 6, BASE + 30 + 18000 - 6)
        self.assertEqual((ok, r), (False, "window-not-full"))
        # Beyond 5h side maps to resets-beyond-5h (>18005).
        ok, r = allow_pair(BASE + 18000 + 6, BASE + 30 + 18000 + 6)
        self.assertEqual((ok, r), (False, "resets-beyond-5h"))

    def test_separation_exactly_30_allows_29_denies(self):
        ok, _ = allow_pair(BASE + 18000, BASE + 30 + 18000,
                           wall2=BASE + 30, mono2=1000.0 + 30)
        self.assertTrue(ok)
        ok, r = allow_pair(BASE + 18000, BASE + 29 + 18000,
                           wall1=BASE, wall2=BASE + 29,
                           mono1=1000.0, mono2=1029.0)
        self.assertEqual((ok, r), (False, "separation-too-short"))

    def test_reset_must_advance_with_elapsed(self):
        ok, r = allow_pair(BASE + 18000, BASE + 18000)  # frozen target
        self.assertEqual((ok, r), (False, "resets-fixed"))
        # Off by 6s from elapsed -> still fixed.
        ok, r = allow_pair(BASE + 18000, BASE + 18000 + 24)
        self.assertEqual((ok, r), (False, "resets-fixed"))

    def test_300min_exact_zero_gate(self):
        ok, r = allow_pair(BASE + 18000, BASE + 30 + 18000,
                           primary_used=1)
        self.assertEqual((ok, r), (False, "used-nonzero"))
        ok, r = allow_pair(BASE + 18000, BASE + 30 + 18000, duration=60)
        self.assertEqual((ok, r), (False, "window-not-300"))

    def test_expired_null_beyond(self):
        ok, r = allow_pair(BASE - 5, BASE + 30 - 5)
        self.assertEqual((ok, r), (False, "resets-expired"))
        s1 = policy.parse(raw(codex_kwargs={"resets_at": None}))
        s2 = policy.parse(raw(codex_kwargs={"resets_at": None}))
        ok, r = policy.decide(s1, BASE, s2, BASE + 30, 1.0, 31.0, CONFIRM)
        self.assertEqual((ok, r), (False, "resets-null"))
        ok, r = allow_pair(BASE + 25000, BASE + 30 + 25000)
        self.assertEqual((ok, r), (False, "resets-beyond-5h"))


class TestAuthAndBlocking(unittest.TestCase):
    def test_ordinary_must_be_true_both(self):
        s1 = policy.parse(raw(ordinary=False))
        s2 = policy.parse(raw(ordinary=False))
        self.assertEqual(policy.decide(s1, BASE, s2, BASE + 30, 1.0, 31.0,
                                       CONFIRM), (False, "ordinary-not-true"))
        r = dict(raw())
        del r["ordinaryUsageAllowed"]
        s = policy.parse(r)
        self.assertEqual(policy.decide(s, BASE, s, BASE + 30, 1.0, 31.0,
                                       CONFIRM), (False, "ordinary-not-true"))

    def test_missing_codex_bucket_denies(self):
        r = {"ordinaryUsageAllowed": True,
             "rateLimits": {"primary": {"usedPercent": 0,
                                        "windowDurationMins": 300,
                                        "resetsAt": BASE + 18000}},
             "rateLimitsByLimitId": {"other": {"primary": None,
                                               "secondary": None}}}
        s = policy.parse(r)
        self.assertEqual(policy.decide(s, BASE, s, BASE + 30, 1.0, 31.0,
                                       CONFIRM),
                         (False, "codex-bucket-missing"))

    def test_weekly_spend_reached_deny(self):
        ok, r = allow_pair(BASE + 18000, BASE + 30 + 18000,
                           secondary_used=100)
        self.assertEqual((ok, r), (False, "weekly-exhausted"))
        ok, r = allow_pair(BASE + 18000, BASE + 30 + 18000, spend=True)
        self.assertEqual((ok, r), (False, "spend-reached"))
        ok, r = allow_pair(BASE + 18000, BASE + 30 + 18000,
                           reached="rate_limit_reached")
        self.assertEqual((ok, r), (False, "reached-type"))
        ok, r = allow_pair(BASE + 18000, BASE + 30 + 18000, indiv=0)
        self.assertEqual((ok, r), (False, "spend-reached"))
        s1 = policy.parse(raw(other_exhausted=True))
        s2 = policy.parse(raw(codex_kwargs={"resets_at": BASE + 30 + 18000},
                              other_exhausted=True))
        # Rebuild with moving resets for the other-exhausted pair.
        r1 = raw(other_exhausted=True,
                 codex_kwargs={"resets_at": BASE + 18000})
        r2 = raw(other_exhausted=True,
                 codex_kwargs={"resets_at": BASE + 30 + 18000})
        s1, s2 = policy.parse(r1), policy.parse(r2)
        self.assertEqual(policy.decide(s1, BASE, s2, BASE + 30, 1.0, 31.0,
                                       CONFIRM),
                         (False, "other-limit-exhausted"))

    def test_clock_skew_denies(self):
        s1 = policy.parse(raw(codex_kwargs={"resets_at": BASE + 18000}))
        s2 = policy.parse(raw(codex_kwargs={"resets_at": BASE + 30 + 18000}))
        ok, r = policy.decide(s1, BASE, s2, BASE + 30, 100.0, 105.0, CONFIRM)
        self.assertEqual((ok, r), (False, "clock-jump"))

    def test_malformed_nan_bool_types_raise(self):
        bad_payloads = [
            {"ordinaryUsageAllowed": 1, "rateLimits": {"primary": None}},
            {"ordinaryUsageAllowed": float("nan"),
             "rateLimits": {"primary": None}},
            {"ordinaryUsageAllowed": True},  # missing rateLimits
            {"ordinaryUsageAllowed": True, "rateLimits": {},
             "rateLimitsByLimitId": "codex"},
            {"ordinaryUsageAllowed": True,
             "rateLimits": {"primary": {"usedPercent": 0.0,
                                        "windowDurationMins": 300,
                                        "resetsAt": BASE + 18000}},
             "rateLimitsByLimitId": {"codex": {"primary": {
                 "usedPercent": 0.0, "windowDurationMins": 300,
                 "resetsAt": BASE + 18000}}}},
            {"ordinaryUsageAllowed": True,
             "rateLimits": {"primary": {"usedPercent": True,
                                        "windowDurationMins": 300,
                                        "resetsAt": BASE + 18000}},
             "rateLimitsByLimitId": {"codex": {"primary": {
                 "usedPercent": True, "windowDurationMins": 300,
                 "resetsAt": BASE + 18000}}}},
            {"ordinaryUsageAllowed": True,
             "rateLimits": {"primary": {"usedPercent": float("nan"),
                                        "windowDurationMins": 300,
                                        "resetsAt": BASE + 18000}}},
            None, "string", [],
        ]
        for p in bad_payloads:
            with self.subTest(payload=str(p)[:60]):
                with self.assertRaises(policy.PolicyError):
                    policy.parse(p)

    def test_strict_spend_and_fallback_buckets(self):
        # string spend is malformed (must be bool), not silently accepted
        r = raw()
        r["rateLimitsByLimitId"]["codex"]["spendControlReached"] = "true"
        with self.assertRaises(policy.PolicyError):
            policy.parse(r)
        r = raw()
        r["rateLimitsByLimitId"]["codex"]["spendControlReached"] = 1
        with self.assertRaises(policy.PolicyError):
            policy.parse(r)
        # fallback bucket from another meter is not ours -> deny
        r = {"ordinaryUsageAllowed": True,
             "rateLimits": {"limitId": "other",
                            "primary": {"usedPercent": 0,
                                        "windowDurationMins": 300,
                                        "resetsAt": BASE + 18000}}}
        s = policy.parse(r)
        self.assertEqual(policy.decide(s, BASE, s, BASE + 30, 1.0, 31.0,
                                       CONFIRM),
                         (False, "codex-bucket-missing"))
        # conflicting map limitId is malformed
        r = raw()
        r["rateLimitsByLimitId"]["codex"]["limitId"] = "other"
        with self.assertRaises(policy.PolicyError):
            policy.parse(r)

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
                      "resetsAt": 2 ** 41}):
            r = {"ordinaryUsageAllowed": True,
                 "rateLimits": {"primary": prim}}
            with self.subTest(prim=prim):
                with self.assertRaises(policy.PolicyError):
                    policy.parse(r)


if __name__ == "__main__":
    unittest.main()
