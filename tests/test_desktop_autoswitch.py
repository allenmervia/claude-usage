#!/usr/bin/env python3
"""Unit tests for the desktop auto-switch tier: the pure decision and the tick around it.

Run:  python3 -m unittest discover -s tests
"""
import os
import tempfile
import unittest

from support import Patched, cu
from test_autoswitch import NOW, row


def stash(label, active=False, stale=False, version_mismatch=False, unclaimed=False,
          uuid=None):
    return {"label": label, "active": active, "can_switch": not active, "stale": stale,
            "version_mismatch": version_mismatch, "unclaimed": unclaimed,
            "account_uuid": uuid}


def dstate(stashes, active, app_running=False, needs_repair=False):
    return {"active": active, "stashes": stashes, "app_running": app_running,
            "needs_repair": needs_repair, "needs_confirm": app_running}


def fleet(cur_fh=100):
    """Desktop on 'cur' (exhausted by default); 'fresh' and 'old' are parked with stashes."""
    rows = [row("cur", fh=cur_fh, uuid="cur"),
            row("fresh", fh=5, uuid="fresh"),
            row("old", fh=10, uuid="old")]
    ds = dstate([stash("cur", active=True, uuid="cur"),
                 stash("fresh", uuid="fresh"),
                 stash("old", stale=True, uuid="old")], active="cur")
    return rows, ds


class TestDesktopDecision(unittest.TestCase):
    def decide(self, rows, ds, last_swap=None, manual_at=None, scoped=False):
        return cu.desktop_auto_decision(rows, ds, NOW, last_swap, manual_at, scoped)

    def test_no_desktop_setup_is_idle(self):
        self.assertEqual(self.decide([row("a", fh=100)], None), ("idle", None))

    def test_healthy_desktop_account_is_idle(self):
        rows, ds = fleet(cur_fh=50)
        self.assertEqual(self.decide(rows, ds), ("idle", None))

    def test_needs_repair_is_broken_not_silent(self):
        rows, ds = fleet()
        ds["needs_repair"] = True
        self.assertEqual(self.decide(rows, ds), ("broken", None))

    def test_unmatched_active_stash_is_unwatched_not_silent(self):
        rows, ds = fleet()
        ds["active"] = "mystery"
        ds["stashes"].append(stash("mystery", active=True))
        self.assertEqual(self.decide(rows, ds), ("unwatched", None))

    def test_no_active_stash_at_all_is_idle(self):
        rows, ds = fleet()
        ds["active"] = None
        self.assertEqual(self.decide(rows, ds), ("idle", None))

    def test_stale_usage_data_never_acts(self):
        rows, ds = fleet()
        rows[0]["stale"] = True
        self.assertEqual(self.decide(rows, ds), ("idle", None))

    def test_swap_only_fresh_skips_stale_and_mismatched_stashes(self):
        rows, ds = fleet()
        kind, payload = self.decide(rows, ds)
        self.assertEqual(kind, "swap")
        self.assertEqual([lbl for _r, lbl in payload["cands"]], ["fresh"])  # 'old' is stale

    def test_revoked_stash_is_not_fresh(self):
        rows, ds = fleet()
        ds["stashes"][1]["revoked"] = True
        self.assertEqual(self.decide(rows, ds)[0], "stuck")

    def test_version_mismatch_is_not_fresh(self):
        rows, ds = fleet()
        ds["stashes"][1]["version_mismatch"] = True
        self.assertEqual(self.decide(rows, ds)[0], "stuck")

    def test_cli_active_account_is_a_fine_desktop_target(self):
        rows, ds = fleet()
        rows[1]["active"] = True          # CLI currently on 'fresh'
        kind, payload = self.decide(rows, ds)
        self.assertEqual(kind, "swap")
        self.assertEqual(payload["cands"][0][1], "fresh")

    def test_desktop_account_itself_is_never_a_target(self):
        rows, ds = fleet(cur_fh=100)
        ds["stashes"][0]["can_switch"] = True    # even if a surface said otherwise
        kind, payload = self.decide(rows, ds)
        self.assertNotIn("cur", [lbl for _r, lbl in payload["cands"]])

    def test_app_running_queues_instead_of_swapping(self):
        rows, ds = fleet()
        ds["app_running"] = True
        kind, payload = self.decide(rows, ds)
        self.assertEqual(kind, "pending")
        self.assertEqual(payload["cands"][0][1], "fresh")
        self.assertEqual(payload["from"], "cur")
        self.assertEqual(payload["reason"], "5-hour limit")

    def test_no_fresh_candidate_is_stuck_with_reason(self):
        rows, ds = fleet()
        ds["stashes"][1]["stale"] = True
        self.assertEqual(self.decide(rows, ds), ("stuck", "5-hour limit"))

    def test_recent_auto_swap_cools_down(self):
        rows, ds = fleet()
        self.assertEqual(self.decide(rows, ds, last_swap=NOW - cu.AUTO_COOLDOWN_S + 60),
                         ("cooldown", None))

    def test_recent_manual_desktop_action_holds_off(self):
        rows, ds = fleet()
        self.assertEqual(self.decide(rows, ds, manual_at=NOW - 60), ("cooldown", None))

    def test_exhausted_target_accounts_are_excluded(self):
        rows, ds = fleet()
        rows[1]["five_hour"]["pct"] = 100.0
        self.assertEqual(self.decide(rows, ds)[0], "stuck")

    def test_scoped_cap_triggers_when_enabled(self):
        rows, ds = fleet(cur_fh=40)
        rows[0]["scoped"] = [{"model": "Fable", "pct": 100, "resets_at": None}]
        self.assertEqual(self.decide(rows, ds)[0], "idle")
        kind, payload = self.decide(rows, ds, scoped=True)
        self.assertEqual(kind, "swap")
        self.assertEqual(payload["reason"], "Fable weekly limit")


class TestDesktopTick(Patched):
    """_desktop_auto_tick with every IO seam stubbed."""

    def setUp(self):
        import shutil
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self._patch("AUTOSWITCH", os.path.join(self.tmp, "autoswitch.json"))
        self._patch("AUTOSWITCH_LOCK", os.path.join(self.tmp, "autoswitch.lock"))
        self._patch("mock_enabled", lambda: False)
        self.rows, self.ds = fleet()
        self._patch("_desktop_state", lambda: self.ds)   # the tick reads reality, not the mock wrapper
        self.swaps = []
        self._patch("_desktop_swap", lambda label: (self.swaps.append(label) or True, "ok"))
        self.notices = []
        self._patch("_notify", lambda title, body: self.notices.append((title, body)))
        cu.save_autoswitch({"desktop": True})
        # notifications must never carry an em dash: check every one this test produces
        self.addCleanup(lambda: [self.assertNotIn("—", t + b) for t, b in self.notices])

    def tick(self):
        cu._desktop_auto_tick(self.rows, cu.load_autoswitch(), NOW)

    def test_swaps_records_and_notifies_when_app_closed(self):
        self.tick()
        self.assertEqual(self.swaps, ["fresh"])
        st = cu.load_autoswitch()
        self.assertEqual(st["last_desktop_auto"]["to"], "fresh")
        self.assertEqual(st["last_desktop_auto"]["from"], "cur")
        self.assertIsNone(st.get("desktop_pending"))
        self.assertEqual(len(self.notices), 1)
        self.assertIn("fresh", self.notices[0][1])

    def test_app_open_queues_pending_and_notifies_once(self):
        self.ds["app_running"] = True
        self.tick()
        self.tick()
        self.assertEqual(self.swaps, [])
        self.assertEqual(cu.load_autoswitch()["desktop_pending"]["to"], "fresh")
        self.assertEqual(len(self.notices), 1)          # hourly-limited

    def test_pending_executes_on_the_tick_that_finds_the_app_closed(self):
        self.ds["app_running"] = True
        self.tick()
        self.ds["app_running"] = False
        self.tick()
        self.assertEqual(self.swaps, ["fresh"])
        self.assertIsNone(cu.load_autoswitch().get("desktop_pending"))

    def test_pending_clears_when_the_limit_recovers(self):
        self.ds["app_running"] = True
        self.tick()
        self.rows[0]["five_hour"]["pct"] = 10.0
        self.tick()
        self.assertIsNone(cu.load_autoswitch().get("desktop_pending"))
        self.assertEqual(self.swaps, [])

    def test_failed_swap_falls_through_then_reports(self):
        self._patch("_desktop_swap", lambda label: (False, "boom"))
        self.tick()
        self.assertEqual(cu.load_autoswitch().get("last_desktop_auto"), None)
        self.assertEqual(len(self.notices), 1)
        self.assertIn("failed", self.notices[0][1])

    def test_app_reopening_during_lock_wait_aborts_the_swap(self):
        calls = {"n": 0}
        def state():
            calls["n"] += 1
            if calls["n"] > 1:               # the post-lock re-decision finds the app open again
                self.ds["app_running"] = True
            return self.ds
        self._patch("_desktop_state", state)
        self.tick()
        self.assertEqual(self.swaps, [])

    def test_stuck_notifies_hourly(self):
        self.ds["stashes"][1]["stale"] = True
        self.tick()
        self.tick()
        self.assertEqual(len(self.notices), 1)
        self.assertIn("fresh enough", self.notices[0][1])

    def test_stuck_clears_a_queued_promise(self):
        self.ds["app_running"] = True
        self.tick()                                       # queue the pending swap
        self.assertIsNotNone(cu.load_autoswitch().get("desktop_pending"))
        self.ds["app_running"] = False
        self.ds["stashes"][1]["stale"] = True             # the target rotted meanwhile
        self.tick()
        self.assertIsNone(cu.load_autoswitch().get("desktop_pending"))

    def test_recovery_resets_the_pending_notify_key(self):
        self.ds["app_running"] = True
        self.tick()                                       # episode 1 notified
        self.rows[0]["five_hour"]["pct"] = 10.0           # limit reset
        self.tick()
        self.rows[0]["five_hour"]["pct"] = 100.0          # episode 2, still within the hour
        self.tick()
        self.assertEqual(len(self.notices), 2)            # episode 2 notified, not suppressed

    def test_failed_swap_and_stuck_use_separate_hourly_keys(self):
        self._patch("_desktop_swap", lambda label: (False, "boom"))
        self.tick()                                       # failed notification
        self.ds["stashes"][1]["stale"] = True
        self.tick()                                       # stuck notification, own key
        self.assertEqual(len(self.notices), 2)
        self.assertIn("failed", self.notices[0][1])
        self.assertIn("fresh enough", self.notices[1][1])

    def test_timeout_aborts_the_candidate_loop(self):
        rows_extra = self.rows
        self.ds["stashes"].append(stash("extra", uuid="extra"))
        rows_extra.append(row("extra", fh=20, uuid="extra"))
        attempts = []
        self._patch("_desktop_swap", lambda label: (attempts.append(label) or False, "timed out"))
        self.tick()
        self.assertEqual(len(attempts), 1)                # no second 90s block on a stalled disk

    def test_broken_state_notifies_instead_of_going_silent(self):
        self.ds["needs_repair"] = True
        self.tick()
        self.tick()
        self.assertEqual(len(self.notices), 1)
        self.assertIn("repair", self.notices[0][1])

    def test_unwatched_active_stash_notifies(self):
        self.ds["active"] = "mystery"
        self.ds["stashes"].append(stash("mystery", active=True))
        self.tick()
        self.assertEqual(len(self.notices), 1)
        self.assertIn("can't watch", self.notices[0][1])

    def test_manual_switch_between_candidates_aborts(self):
        self.ds["stashes"].append(stash("extra", uuid="extra"))
        self.rows.append(row("extra", fh=20, uuid="extra"))
        def first_fails_then_manual(label):
            self.ds["active"] = "somewhere-else"          # a manual switch raced us
            return False, "another switch is running"
        self._patch("_desktop_swap", first_fails_then_manual)
        self.tick()
        st = cu.load_autoswitch()
        self.assertIsNone(st.get("last_desktop_auto"))    # no second-candidate override


if __name__ == "__main__":
    unittest.main()
