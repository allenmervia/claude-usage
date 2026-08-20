"""The live credential's lifecycle: who renews it, when, and what happens when nobody does.

`switch` writes Claude Code's Keychain item and then leaves it there. On a machine whose
sessions are hosted by the desktop app, nothing else ever writes that item, so the token it
was given is the whole life the account gets. These pin both halves of the rule that keeps it
alive — renew it once it has lapsed, and never before — plus the waiting and the fallbacks
that decide what the panel shows while an account can't be read.
"""
import time
import unittest

from support import Patched, cu, raiser


def live_blob(**over):
    blob = {"accessToken": "live-tok", "refreshToken": "live-ref",
            "expiresAt": (time.time() + 3600) * 1000, "scopes": ["user:inference"]}
    blob.update(over)
    return blob


class Recorder(Patched):
    """Stubs that remember they were called, so order and abstention are both assertable."""

    def record(self, name, ret):
        def stub(*a, **k):
            self.calls.append((name, a, k))
            return ret
        return stub

    def named(self):
        return [c[0] for c in self.calls]

    def setUp(self):
        self.calls = []


class TestTokenForLive(Recorder):
    """Expiry is the whole trigger. A credential still inside its lifetime belongs to whoever
    wrote it, and renewing it there is the desync this tool has always refused to risk."""

    def setUp(self):
        super().setUp()
        self._patch("refresh_live", self.record("refresh_live", ("renewed", None)))

    def test_an_unexpired_token_is_handed_back_untouched(self):
        self.assertEqual(cu.token_for_live("u1", live_blob()), ("live-tok", None))
        self.assertEqual(self.named(), [])

    def test_an_expired_token_is_renewed(self):
        blob = live_blob(expiresAt=(time.time() - 1) * 1000)
        self.assertEqual(cu.token_for_live("u1", blob), ("renewed", None))
        self.assertEqual(self.named(), ["refresh_live"])

    def test_a_blob_with_no_expiry_is_taken_at_face_value(self):
        """We can't prove it dead, and a needless rotation costs more than the 401 that catches it."""
        self.assertEqual(cu.token_for_live("u1", live_blob(expiresAt=None)), ("live-tok", None))
        self.assertEqual(self.named(), [])

    def test_a_blob_with_only_a_refresh_token_is_renewed(self):
        blob = live_blob(accessToken=None)
        self.assertEqual(cu.token_for_live("u1", blob), ("renewed", None))
        self.assertEqual(self.named(), ["refresh_live"])

    def test_a_blob_with_neither_asks_for_a_login(self):
        tok, err = cu.token_for_live("u1", live_blob(accessToken=None, refreshToken=None))
        self.assertIsNone(tok)
        self.assertIn("run `claude` once", err)
        self.assertEqual(self.named(), [])


class TestRefreshLive(Recorder):
    def setUp(self):
        super().setUp()
        self._patch("load_secret", lambda uuid: {"tokenHost": "https://h.test/token"})
        self._patch("refresh_token", lambda ref, host=None: (
            {"access_token": "fresh", "refresh_token": "rotated", "expires_in": 28800},
            "https://h.test/token"))
        self._patch("write_live", self.record("write_live", True))
        self._patch("store_secret", self.record("store_secret", True))

    def test_the_renewed_pair_reaches_claude_codes_item_first(self):
        """Both records name one grant. Claude Code's is the copy a session reads, so it is the
        one that must not be the casualty if the second write fails."""
        self.assertEqual(cu.refresh_live("u1", live_blob()), ("fresh", None))
        self.assertEqual(self.named(), ["write_live", "store_secret"])

    def test_the_written_blob_carries_the_rotation_and_keeps_the_rest(self):
        cu.refresh_live("u1", live_blob())
        blob = self.calls[0][1][0]
        self.assertEqual(blob["accessToken"], "fresh")
        self.assertEqual(blob["refreshToken"], "rotated")
        self.assertGreater(blob["expiresAt"], time.time() * 1000)
        self.assertEqual(blob["scopes"], ["user:inference"])   # non-token fields survive

    def test_an_unsaved_rotation_is_reported_not_spent(self):
        """The server may already have rotated the grant, which makes the token in hand the only
        copy of it: returning it would work for one call and strand the account after."""
        self._patch("write_live", self.record("write_live", False))
        tok, err = cu.refresh_live("u1", live_blob())
        self.assertIsNone(tok)
        self.assertIn("couldn't save", err)
        self.assertEqual(self.named(), ["write_live"])         # nothing written anywhere else

    def test_a_revoked_grant_latches_instead_of_retrying(self):
        self._patch("refresh_token", raiser(cu.GrantRevoked("invalid_grant")))
        tok, err = cu.refresh_live("u1", live_blob())
        self.assertIsNone(tok)
        self.assertEqual(err, cu.NEEDS_LOGIN)
        self.assertEqual(self.named(), ["store_secret"])       # latched, and never written live
        self.assertTrue(self.calls[0][2]["meta"]["needsLogin"])

    def test_an_unwritten_latch_does_not_claim_the_paused_state(self):
        """A bare NEEDS_LOGIN is what every surface renders as \"signed out and no longer
        refreshing\". Nothing was recorded, so the next tick has to re-check."""
        self._patch("refresh_token", raiser(cu.GrantRevoked("invalid_grant")))
        self._patch("store_secret", self.record("store_secret", False))
        tok, err = cu.refresh_live("u1", live_blob())
        self.assertIsNone(tok)
        self.assertNotEqual(err, cu.NEEDS_LOGIN)
        self.assertIn("unlock the Keychain", err)

    def test_a_diverged_stored_grant_is_not_overwritten(self):
        """store_secret reads a changed refresh token as a new grant and drops the latch, so
        latching with the live one would destroy our copy and leave nothing latched."""
        self._patch("refresh_token", raiser(cu.GrantRevoked("invalid_grant")))
        self._patch("load_secret", lambda uuid: {"refreshToken": "a-different-grant"})
        tok, err = cu.refresh_live("u1", live_blob())
        self.assertIsNone(tok)
        self.assertIn("out of step", err)
        self.assertEqual(self.named(), [])                     # nothing written anywhere

    def test_an_unsaved_stored_copy_is_reported_with_the_token(self):
        """The session's copy landed, so the token works — but ours now names a rotated-away
        grant, and the next switch to this account would refresh with a dead token."""
        self._patch("store_secret", self.record("store_secret", False))
        tok, err = cu.refresh_live("u1", live_blob())
        self.assertEqual(tok, "fresh")
        self.assertIn("our own Keychain item", err)

    def test_a_blob_with_no_refresh_token_never_reaches_the_network(self):
        tok, err = cu.refresh_live("u1", live_blob(refreshToken=None))
        self.assertIsNone(tok)
        self.assertIn("sign in again", err)
        self.assertEqual(self.named(), [])


class TestSwitchTokenLife(Patched):
    """A read only has to outlive its own call. `switch` gives the token away, so it has to ask
    for enough life that the account is still usable long after this process is gone."""

    def setUp(self):
        self.refreshes = []
        self._patch("load_secret", lambda uuid: {
            "refreshToken": "r", "accessToken": "cached",
            "expiresAt": (time.time() + 42 * 60) * 1000})
        self._patch("store_secret", lambda *a, **k: True)

        def refresh(ref, host=None):
            self.refreshes.append(ref)
            return {"access_token": "fresh", "expires_in": 28800}, "h"
        self._patch("refresh_token", refresh)

    def test_a_read_reuses_a_token_with_minutes_left(self):
        self.assertEqual(cu.token_for_parked("u1"), ("cached", None))
        self.assertEqual(self.refreshes, [])

    def test_a_switch_will_not_hand_over_a_token_with_minutes_left(self):
        tok, err = cu.token_for_parked("u1", min_life_ms=cu.SWITCH_MIN_LIFE_MS)
        self.assertEqual((tok, err), ("fresh", None))
        self.assertEqual(self.refreshes, ["r"])


class TestRetryAfter(unittest.TestCase):
    def test_the_seconds_form_becomes_a_deadline(self):
        until = cu.retry_after_ts({"Retry-After": "1639"})
        self.assertAlmostEqual(until, time.time() + 1639, delta=5)

    def test_anything_else_yields_no_deadline(self):
        for headers in ({}, {"Retry-After": ""}, {"Retry-After": "0"},
                        {"Retry-After": "Thu, 20 Aug 2026 22:07:17 GMT"}, None):
            self.assertIsNone(cu.retry_after_ts(headers))

    def test_the_message_names_the_time_it_clears(self):
        msg = cu.rate_limited_msg(time.time() + 1639)
        self.assertIn("clears at", msg)
        self.assertNotIn("too fast", msg)          # the cadence is not what a 429 is evidence of

    def test_a_deadlineless_limit_still_reads_as_a_wait(self):
        self.assertIn("rate-limited", cu.rate_limited_msg(None))


class TestRateLimitHold(Patched):
    """A 429 answers with its own deadline, and a call before it passes cannot succeed."""

    def setUp(self):
        self._patch("load_index", lambda: [{"uuid": "u1", "email": "a@x.test", "label": "a"}])
        self._patch("active_uuid_only", lambda: None)
        self._patch("match_live_uuid", lambda: None)
        self._patch("read_live", lambda: None)
        self._patch("collect_codex", lambda persist=True: [])
        self._patch("token_for_parked", lambda uuid, **kw: ("tok", None))
        self.fetched = []

        def fetch(uuid, token, active, live=None):
            self.fetched.append(uuid)
            return {"five_hour": {"utilization": 5}}, None, None
        self._patch("fetch_usage", fetch)

    def test_a_standing_deadline_skips_the_call(self):
        until = time.time() + 600
        rows = cu._collect_live(ingest=False, cache={
            "rows": [{"provider": "claude", "uuid": "u1", "retry_after": until}]})
        self.assertEqual(self.fetched, [])
        self.assertEqual(rows[0]["retry_after"], until)        # carried, so the wait survives
        self.assertIn("rate-limited", rows[0]["error"])

    def test_a_passed_deadline_does_not(self):
        cu._collect_live(ingest=False, cache={
            "rows": [{"provider": "claude", "uuid": "u1", "retry_after": time.time() - 1}]})
        self.assertEqual(self.fetched, ["u1"])

    def test_no_cache_does_not(self):
        cu._collect_live(ingest=False, cache=None)
        self.assertEqual(self.fetched, ["u1"])


class TestMergeLastKnown(unittest.TestCase):
    """One account's failed read is one account's problem."""

    CACHE = {"rows": [
        {"provider": "claude", "uuid": "u1", "error": None, "five_hour": {"pct": 34.0}},
        {"provider": "codex", "uuid": "c1"}]}

    def test_a_failed_read_keeps_its_own_numbers(self):
        fresh = [{"provider": "claude", "uuid": "u1", "error": "rate-limited", "active": True}]
        row, = cu.merge_last_known(fresh, self.CACHE)
        self.assertEqual(row["five_hour"], {"pct": 34.0})
        self.assertTrue(row["stale"])
        self.assertEqual(row["stale_reason"], "rate-limited")
        self.assertTrue(row["active"])            # identity is this sweep's fact, not the old one's

    def test_a_healthy_read_is_never_replaced(self):
        fresh = [{"provider": "claude", "uuid": "u1", "error": None, "five_hour": {"pct": 40.0}}]
        row, = cu.merge_last_known(fresh, self.CACHE)
        self.assertEqual(row["five_hour"], {"pct": 40.0})
        self.assertNotIn("stale", row)

    def test_a_latched_row_is_never_masked(self):
        """Pre-revocation numbers would hide the one state the user has to act on."""
        fresh = [{"provider": "claude", "uuid": "u1",
                  "error": cu.NEEDS_LOGIN, "needs_login": True}]
        row, = cu.merge_last_known(fresh, self.CACHE)
        self.assertEqual(row["error"], cu.NEEDS_LOGIN)
        self.assertNotIn("stale", row)

    def test_an_account_with_nothing_cached_shows_its_error(self):
        fresh = [{"provider": "claude", "uuid": "new", "error": "usage HTTP 500"}]
        row, = cu.merge_last_known(fresh, self.CACHE)
        self.assertEqual(row["error"], "usage HTTP 500")
        self.assertNotIn("stale", row)

    def test_one_failure_does_not_disturb_the_accounts_that_read(self):
        fresh = [{"provider": "claude", "uuid": "u1", "error": "rate-limited"},
                 {"provider": "claude", "uuid": "u2", "error": None, "five_hour": {"pct": 9.0}}]
        held, ok = cu.merge_last_known(fresh, self.CACHE)
        self.assertTrue(held["stale"])
        self.assertNotIn("stale", ok)
        self.assertEqual(ok["five_hour"], {"pct": 9.0})


if __name__ == "__main__":
    unittest.main()
