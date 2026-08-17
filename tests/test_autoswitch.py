#!/usr/bin/env python3
"""Unit tests for the auto-switch decision core (pure functions, no Keychain, no network).

Run:  python3 -m unittest discover -s tests
"""
import os
import unittest
from datetime import datetime, timezone

from support import Patched, cu

NOW = 1_800_000_000.0

def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()

def row(label, fh=0.0, wk=0.0, tier="default_claude_max_20x", active=False,
        error=None, is_team=False, stale=False, fh_reset=NOW + 3600,
        wk_reset=NOW + 86400, uuid=None, scoped=None):
    r = {"provider": "claude", "uuid": uuid or label, "email": f"{label}@x.test",
         "label": label, "tier": tier, "active": active, "error": error,
         "is_team": is_team,
         "five_hour": {"pct": fh, "resets_at": iso(fh_reset) if fh_reset else None},
         "seven_day": {"pct": wk, "resets_at": iso(wk_reset) if wk_reset else None}}
    if scoped is not None:
        r["scoped"] = [{"model": "Fable", "pct": scoped, "resets_at": iso(wk_reset)}]
    if stale:
        r["stale"] = True
    return r

def creds_all(_uuid):
    return True


class TestExhausted(unittest.TestCase):
    def test_five_hour_cap(self):
        self.assertTrue(cu.account_exhausted(row("a", fh=100)))

    def test_weekly_cap(self):
        self.assertTrue(cu.account_exhausted(row("a", fh=40, wk=100)))

    def test_below_cap_is_not_exhausted(self):
        self.assertFalse(cu.account_exhausted(row("a", fh=99.9, wk=94)))

    def test_missing_windows_are_not_exhausted(self):
        r = row("a")
        r["five_hour"] = {"pct": None, "resets_at": None}
        r["seven_day"] = {}
        self.assertFalse(cu.account_exhausted(r))


class TestAutoPick(unittest.TestCase):
    def test_excludes_active_error_team_and_full(self):
        rows = [row("active", active=True),
                row("erry", error="boom"),
                row("team", is_team=True),
                row("wkfull", wk=cu.WEEKLY_REFUGE_MAX),
                row("fhfull", fh=100),
                row("ok", fh=10)]
        got = [r["label"] for r in cu.auto_pick(rows, creds_all)]
        self.assertEqual(got, ["ok"])

    def test_excludes_rows_missing_window_numbers(self):
        r = row("nofh")
        r["five_hour"] = {"pct": None, "resets_at": None}
        self.assertEqual(cu.auto_pick([r], creds_all), [])

    def test_excludes_uncaptured_accounts(self):
        rows = [row("a"), row("b")]
        got = cu.auto_pick(rows, lambda u: u == "b")
        self.assertEqual([r["label"] for r in got], ["b"])

    def test_drain_prefers_soonest_weekly_reset_among_roomy(self):
        rows = [row("late_reset", fh=10, wk_reset=NOW + 5 * 86400),
                row("expiring", fh=30, wk_reset=NOW + 3600),
                row("mid_reset", fh=10, wk_reset=NOW + 2 * 86400)]
        got = [r["label"] for r in cu.auto_pick(rows, creds_all)]
        self.assertEqual(got, ["expiring", "mid_reset", "late_reset"])

    def test_drain_ties_break_by_tier_then_headroom(self):
        rows = [row("pro", fh=10, tier="default_claude_pro"),
                row("max_thinner", fh=50),
                row("max_roomier", fh=10)]
        got = [r["label"] for r in cu.auto_pick(rows, creds_all)]
        self.assertEqual(got, ["max_roomier", "max_thinner", "pro"])

    def test_thin_headroom_ranks_after_roomy_regardless_of_reset(self):
        rows = [row("thin_expiring", fh=100 - cu.DRAIN_MIN_HEADROOM + 1, wk_reset=NOW + 3600),
                row("roomy_late", fh=10, wk_reset=NOW + 6 * 86400)]
        got = [r["label"] for r in cu.auto_pick(rows, creds_all)]
        self.assertEqual(got, ["roomy_late", "thin_expiring"])

    def test_all_thin_falls_back_to_most_headroom(self):
        rows = [row("thinner", fh=90, wk_reset=NOW + 3600),
                row("less_thin", fh=70, wk_reset=NOW + 6 * 86400)]
        got = [r["label"] for r in cu.auto_pick(rows, creds_all)]
        self.assertEqual(got, ["less_thin", "thinner"])


class TestDecision(unittest.TestCase):
    def test_healthy_active_is_idle(self):
        rows = [row("a", fh=80, active=True), row("b")]
        self.assertEqual(cu.auto_decision(rows, {}, NOW, creds_all), ("idle", None))

    def test_no_active_is_idle(self):
        self.assertEqual(cu.auto_decision([row("b")], {}, NOW, creds_all), ("idle", None))

    def test_stale_active_is_idle(self):
        rows = [row("a", fh=100, active=True, stale=True), row("b")]
        self.assertEqual(cu.auto_decision(rows, {}, NOW, creds_all), ("idle", None))

    def test_exhausted_with_candidates_switches_in_order(self):
        rows = [row("a", fh=100, active=True), row("worse", fh=60), row("best", fh=5)]
        kind, cands = cu.auto_decision(rows, {}, NOW, creds_all)
        self.assertEqual(kind, "switch")
        self.assertEqual([r["label"] for r in cands], ["best", "worse"])

    def test_recent_manual_switch_holds_off(self):
        rows = [row("a", fh=100, active=True), row("b")]
        last = {"provider": "claude", "ts": NOW - cu.MANUAL_HOLDOFF_S + 60, "auto": False}
        self.assertEqual(cu.auto_decision(rows, last, NOW, creds_all), ("cooldown", None))

    def test_recent_auto_switch_cools_down(self):
        rows = [row("a", fh=100, active=True), row("b")]
        last = {"provider": "claude", "ts": NOW - cu.AUTO_COOLDOWN_S + 60, "auto": True}
        self.assertEqual(cu.auto_decision(rows, last, NOW, creds_all), ("cooldown", None))

    def test_old_switch_does_not_block(self):
        rows = [row("a", fh=100, active=True), row("b")]
        last = {"provider": "claude", "ts": NOW - cu.AUTO_COOLDOWN_S - 1, "auto": True}
        self.assertEqual(cu.auto_decision(rows, last, NOW, creds_all)[0], "switch")

    def test_codex_manual_switch_also_holds(self):
        # the record is single-slot, so a Codex switch must hold too — filtering by provider
        # would let it erase a Claude switch's holdoff
        rows = [row("a", fh=100, active=True), row("b")]
        last = {"provider": "codex", "ts": NOW - 60, "auto": False}
        self.assertEqual(cu.auto_decision(rows, last, NOW, creds_all), ("cooldown", None))

    def test_all_spent_is_stranded_with_earliest_relief(self):
        rows = [row("a", fh=100, active=True, fh_reset=NOW + 7200),
                row("b", fh=100, fh_reset=NOW + 3600),
                row("c", wk=100, wk_reset=NOW + 4 * 86400)]
        kind, relief = cu.auto_decision(rows, {}, NOW, creds_all)
        self.assertEqual(kind, "stranded")
        self.assertAlmostEqual(relief, NOW + 3600, delta=1)

    def test_pre_autoswitch_record_without_ts_does_not_block(self):
        rows = [row("a", fh=100, active=True), row("b")]
        self.assertEqual(cu.auto_decision(rows, {"provider": "claude"}, NOW, creds_all)[0],
                         "switch")


class TestScoped(unittest.TestCase):
    def test_scoped_cap_is_exhaustion_only_when_enabled(self):
        r = row("a", fh=40, wk=60, scoped=100)
        self.assertFalse(cu.account_exhausted(r))
        self.assertTrue(cu.account_exhausted(r, scoped=True))

    def test_scoped_below_cap_is_not_exhaustion(self):
        self.assertFalse(cu.account_exhausted(row("a", scoped=99), scoped=True))

    def test_reason_names_the_scoped_model(self):
        r = row("a", fh=40, wk=60, scoped=100)
        self.assertEqual(cu.exhaustion_reason(r, scoped=True), "Fable weekly limit")
        self.assertEqual(cu.exhaustion_reason(row("a", fh=100)), "5-hour limit")
        self.assertIsNone(cu.exhaustion_reason(row("a", fh=99, wk=99)))

    def test_scoped_full_candidate_is_no_refuge_when_enabled(self):
        rows = [row("scoped_full", fh=10, scoped=96), row("ok", fh=30, scoped=10)]
        self.assertEqual([r["label"] for r in cu.auto_pick(rows, creds_all, scoped=True)],
                         ["ok"])
        # with the scoped trigger off, the same account is a fine refuge
        self.assertEqual([r["label"] for r in cu.auto_pick(rows, creds_all)][0], "scoped_full")

    def test_decision_switches_on_scoped_cap(self):
        rows = [row("a", fh=40, scoped=100, active=True), row("b", scoped=5)]
        self.assertEqual(cu.auto_decision(rows, {}, NOW, creds_all)[0], "idle")
        kind, cands = cu.auto_decision(rows, {}, NOW, creds_all, scoped=True)
        self.assertEqual(kind, "switch")
        self.assertEqual(cands[0]["label"], "b")

    def test_scoped_stranded_relief_uses_scoped_reset(self):
        rows = [row("a", fh=40, scoped=100, active=True, wk_reset=NOW + 7200),
                row("b", fh=100, scoped=100, fh_reset=NOW + 3600, wk_reset=NOW + 4 * 86400)]
        kind, relief = cu.auto_decision(rows, {}, NOW, creds_all, scoped=True)
        self.assertEqual(kind, "stranded")
        self.assertAlmostEqual(relief, NOW + 7200, delta=1)


class TestUsableAt(unittest.TestCase):
    def test_blocked_on_both_windows_waits_for_the_later(self):
        r = row("a", fh=100, wk=100, fh_reset=NOW + 3600, wk_reset=NOW + 86400)
        self.assertAlmostEqual(cu._usable_at(r), NOW + 86400, delta=1)

    def test_unblocked_account_has_no_relief_time(self):
        self.assertIsNone(cu._usable_at(row("a", fh=10)))

    def test_unparseable_reset_returns_none(self):
        r = row("a", fh=100, fh_reset=None)
        self.assertIsNone(cu._usable_at(r))


class TestMaybeAutoSwitch(Patched):
    """The impure shell around the decision core, with every IO seam stubbed."""

    def setUp(self):
        import tempfile
        self.tmp = tempfile.mkdtemp()
        self._patch("AUTOSWITCH", os.path.join(self.tmp, "autoswitch.json"))
        self._patch("AUTOSWITCH_LOCK", os.path.join(self.tmp, "autoswitch.lock"))
        self._patch("mock_enabled", lambda: False)
        self._patch("last_switch_info", lambda: {})
        self._patch("_has_full_creds", lambda u: True)
        self._patch("load_index", lambda: [{"uuid": "best", "email": "best@x.test"},
                                           {"uuid": "worse", "email": "worse@x.test"}])
        self.switched = []
        def fake_switch(e):
            self.switched.append(e["uuid"])
            return True, f"switched to {e['email']}", ""
        self._patch("_switch_claude", fake_switch)
        self._patch("record_last_switch", lambda p, auto=False: None)
        self._patch("clear_cache", lambda: None)
        self.cached = []
        self._patch("save_cache", lambda rows, ts: self.cached.append(rows))
        self.notices = []
        self._patch("_notify", lambda title, body: self.notices.append((title, body)))
        self._patch("desktop_state", lambda: None)
        # notifications must never carry an em dash: check every one this test produces
        self.addCleanup(lambda: [self.assertNotIn("—", t + b) for t, b in self.notices])

    def _enable(self):
        cu.save_autoswitch({"enabled": True})

    def test_disabled_is_inert(self):
        rows = [row("a", fh=100, active=True), row("b")]
        self.assertIs(cu.maybe_auto_switch(rows), rows)
        self.assertEqual(self.switched, [])

    def test_switches_records_and_notifies(self):
        self._enable()
        rows = [row("a", fh=100, active=True), row("best", fh=5, uuid="best"),
                row("worse", fh=50, uuid="worse")]
        out = cu.maybe_auto_switch(rows)
        self.assertEqual(self.switched, ["best"])
        self.assertIs(out, rows)
        actives = [r["label"] for r in out if r.get("active")]
        self.assertEqual(actives, ["best"])           # the tick renders the switch it just made
        self.assertEqual(self.cached, [rows])
        st = cu.load_autoswitch()
        self.assertEqual(st["last_auto"]["to"], "best@x.test")
        self.assertEqual(st["last_auto"]["reason"], "5-hour limit")
        self.assertFalse(st["last_auto"]["partial"])
        self.assertEqual(len(self.notices), 1)
        self.assertIn("best@x.test", self.notices[0][1])
        self.assertFalse(os.path.exists(cu.AUTOSWITCH_LOCK))   # lock released

    def test_partial_switch_is_recorded_and_notified(self):
        self._enable()
        cu._switch_claude = lambda e: (True, f"switched to {e['email']}", cu.MISMATCH_NOTE)
        rows = [row("a", fh=100, active=True), row("best", fh=5, uuid="best")]
        cu.maybe_auto_switch(rows)
        self.assertTrue(cu.load_autoswitch()["last_auto"]["partial"])
        self.assertIn("old account's name", self.notices[0][1])

    def test_failed_candidate_falls_through_to_next(self):
        self._enable()
        def flaky_switch(e):
            if e["uuid"] == "best":
                return False, "dead token", ""
            self.switched.append(e["uuid"])
            return True, f"switched to {e['email']}", ""
        cu._switch_claude = flaky_switch
        rows = [row("a", fh=100, active=True), row("best", fh=5, uuid="best"),
                row("worse", fh=50, uuid="worse")]
        cu.maybe_auto_switch(rows)
        self.assertEqual(self.switched, ["worse"])
        self.assertEqual(cu.load_autoswitch()["last_auto"]["to"], "worse@x.test")

    def test_all_candidates_failing_notifies(self):
        self._enable()
        cu._switch_claude = lambda e: (False, "dead token", "")
        rows = [row("a", fh=100, active=True), row("best", fh=5, uuid="best")]
        out = cu.maybe_auto_switch(rows)
        self.assertIs(out, rows)
        self.assertEqual(len(self.notices), 1)
        self.assertIn("no account could be switched", self.notices[0][1])
        cu.maybe_auto_switch(rows)                     # hourly-limited, not per tick
        self.assertEqual(len(self.notices), 1)
        self.assertFalse(os.path.exists(cu.AUTOSWITCH_LOCK))

    def test_crashing_switch_is_contained(self):
        self._enable()
        def boom(e):
            raise RuntimeError("keychain locked")
        cu._switch_claude = boom
        rows = [row("a", fh=100, active=True), row("best", fh=5, uuid="best")]
        out = cu.maybe_auto_switch(rows)               # must not raise
        self.assertIs(out, rows)
        self.assertFalse(os.path.exists(cu.AUTOSWITCH_LOCK))

    def test_held_lock_skips_the_tick(self):
        self._enable()
        os.close(os.open(cu.AUTOSWITCH_LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
        rows = [row("a", fh=100, active=True), row("best", fh=5, uuid="best")]
        cu.maybe_auto_switch(rows)
        self.assertEqual(self.switched, [])            # the other switcher's tick wins

    def test_stranded_notifies_once_per_hour(self):
        self._enable()
        rows = [row("a", fh=100, active=True, fh_reset=NOW + 3600),
                row("b", fh=100, fh_reset=NOW + 7200, uuid="best")]
        cu.maybe_auto_switch(rows)
        self.assertEqual(len(self.notices), 1)
        self.assertIn("Earliest reset", self.notices[0][1])
        cu.maybe_auto_switch(rows)          # immediately again — still one notification
        self.assertEqual(len(self.notices), 1)

    def test_stranded_with_unknown_resets_still_notifies(self):
        self._enable()
        rows = [row("a", fh=100, active=True, fh_reset=None),
                row("b", fh=100, fh_reset=None, uuid="best")]
        cu.maybe_auto_switch(rows)
        self.assertEqual(len(self.notices), 1)
        self.assertIn("Reset times unknown", self.notices[0][1])

    def test_stranded_names_the_signed_out_accounts(self):
        # a signed-out refuge is cured by a login, not a reset; the copy must say so
        self._enable()
        dead = row("b", error=cu.NEEDS_LOGIN, uuid="best")
        dead["needs_login"] = True
        rows = [row("a", fh=100, active=True), dead]
        cu.maybe_auto_switch(rows)
        self.assertEqual(len(self.notices), 1)
        self.assertIn("1 parked account needs a fresh sign-in", self.notices[0][1])

    def test_off_toggle_during_refresh_survives_the_record_write(self):
        self._enable()
        def switch_and_disable(e):
            self.switched.append(e["uuid"])
            cu.update_autoswitch({"enabled": False})   # the user's `autoswitch off` lands mid-switch
            return True, f"switched to {e['email']}", ""
        cu._switch_claude = switch_and_disable
        rows = [row("a", fh=100, active=True), row("best", fh=5, uuid="best")]
        cu.maybe_auto_switch(rows)
        self.assertFalse(cu.load_autoswitch()["enabled"])   # the toggle is not reverted


class TestDesktopVersionMismatch(Patched):
    """_desktop_state flags a stash captured under a different app version."""

    def setUp(self):
        import json
        import tempfile
        self.tmp = tempfile.mkdtemp()
        stashes = os.path.join(self.tmp, "stashes")
        for label, ver in (("old", "1.0.0"), ("current", "2.0.0"), ("unversioned", None)):
            d = os.path.join(stashes, label)
            os.makedirs(d)
            with open(os.path.join(d, "manifest.json"), "w") as f:
                json.dump({"label": label, "app_version": ver,
                           "files": {"Cookies": "x"}}, f)
        self._patch("DESKTOP_APP", self.tmp)
        self._patch("DESKTOP_STASHES", stashes)
        self._patch("DESKTOP_ACTIVE", os.path.join(self.tmp, "active.json"))
        self._patch("DESKTOP_JOURNAL", os.path.join(self.tmp, "journal.json"))
        self._patch("desktop_app_version", lambda: "2.0.0")
        self._patch("desktop_app_running", lambda: False)

    def test_flags_only_the_pre_update_stash(self):
        st = {s["label"]: s for s in cu._desktop_state()["stashes"]}
        self.assertTrue(st["old"]["version_mismatch"])
        self.assertFalse(st["current"]["version_mismatch"])
        # a capture with no recorded version gives no basis to warn
        self.assertFalse(st["unversioned"]["version_mismatch"])

    def test_unknown_installed_version_never_flags(self):
        self._patch("desktop_app_version", lambda: None)
        st = {s["label"]: s for s in cu._desktop_state()["stashes"]}
        self.assertFalse(any(s["version_mismatch"] for s in st.values()))

    def test_missing_captured_at_reads_as_stale_not_fresh(self):
        # no manifest timestamp means unknown age; unknown must not pass the freshness
        # gate an unattended swap rides on
        st = {s["label"]: s for s in cu._desktop_state()["stashes"]}
        self.assertTrue(all(s["stale"] for s in st.values()))
        self.assertTrue(all(s["age_days"] is None for s in st.values()))


class TestDesktopPairsTies(unittest.TestCase):
    def test_uuid_claim_does_not_resolve_a_name_tie(self):
        # two accounts share the display label; a mislabeled stash uuid-claims one of them.
        # The name tie stays a tie — resolving it by elimination would pair the uuid-less
        # stash with whichever account the mislabeled stash didn't take.
        rows = [dict(row("allen", uuid="u-x"), label="allen"),
                dict(row("allen2", uuid="u-y"), label="allen")]
        ds = {"stashes": [{"label": "mislabeled", "account_uuid": "u-x"},
                          {"label": "allen", "account_uuid": None}]}
        pairs = cu.desktop_pairs(rows, ds)
        self.assertEqual(set(pairs), {"mislabeled"})   # the name tie attached nothing

    def test_unclaimed_stash_never_name_pairs(self):
        rows = [row("allen", uuid="u-x")]
        ds = {"stashes": [{"label": "allen", "account_uuid": None, "unclaimed": True}]}
        self.assertEqual(cu.desktop_pairs(rows, ds), {})


class TestMatchDesktopIdentity(unittest.TestCase):
    """Stash-to-account pairing: recorded identity outranks the label."""

    STASH_KEYS = {"active": False, "can_switch": True, "stale": False,
                  "version_mismatch": False}

    def _rows(self):
        return [{"provider": "claude", "uuid": "u-a", "email": "a@x.test",
                 "label": "A", "display": {}},
                {"provider": "claude", "uuid": "u-b", "email": "b@x.test",
                 "label": "B", "display": {}}]

    def _match(self, stashes, rows):
        cu.rows_desktop_state["ds"] = {"stashes": stashes}
        try:
            cu._match_desktop(rows)
        finally:
            cu.rows_desktop_state.pop("ds", None)

    def test_identity_outranks_a_misleading_label(self):
        st = dict(self.STASH_KEYS, label="b@x.test", account_uuid="u-a")
        rows = self._rows()
        self._match([st], rows)
        self.assertEqual(st["matched_uuid"], "u-a")
        self.assertEqual(rows[0]["display"]["desktop"]["label"], "b@x.test")
        self.assertNotIn("desktop", rows[1]["display"])

    def test_known_identity_never_falls_back_to_name(self):
        st = dict(self.STASH_KEYS, label="b@x.test", account_uuid="u-elsewhere")
        rows = self._rows()
        self._match([st], rows)
        self.assertNotIn("matched_uuid", st)
        self.assertNotIn("desktop", rows[1]["display"])

    def test_uuidless_stash_still_pairs_by_name(self):
        st = dict(self.STASH_KEYS, label="b@x.test", account_uuid=None)
        rows = self._rows()
        self._match([st], rows)
        self.assertEqual(st["matched_uuid"], "u-b")

    def test_unclaimed_stash_never_pairs(self):
        st = dict(self.STASH_KEYS, label="unclaimed-20260813-101112",
                  account_uuid="u-a", unclaimed=True)
        rows = self._rows()
        self._match([st], rows)
        self.assertNotIn("matched_uuid", st)
        self.assertNotIn("desktop", rows[0]["display"])


if __name__ == "__main__":
    unittest.main()
