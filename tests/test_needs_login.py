#!/usr/bin/env python3
"""Unit tests for the needs-login latch: a parked account whose refresh token the server
revoked (invalid_grant) stops being retried until a real sign-in stores a new token.

Run:  python3 -m unittest discover -s tests
"""
import io
import json
import types
import unittest
import urllib.error
import urllib.request

from support import Patched, cu, raiser


def http_error(code, body=b""):
    return urllib.error.HTTPError("https://h.test", code, "err", {}, io.BytesIO(body))


REVOKED = cu.GrantRevoked("HTTP 400 (invalid_grant) at https://h.test")


class TestRefreshTokenErrors(Patched):
    def _patch_urlopen(self, urlopen):
        self.calls = []
        def counting(req, timeout=None):
            self.calls.append(req.full_url)
            return urlopen(req)
        self._patch("urllib", types.SimpleNamespace(
            request=types.SimpleNamespace(Request=urllib.request.Request,
                                          urlopen=counting),
            error=urllib.error))

    def test_invalid_grant_raises_grant_revoked_without_trying_more_hosts(self):
        # the verdict is on the grant, not the host: further POSTs of a dead token are
        # both wasted and unable to change the answer
        self._patch_urlopen(raiser(http_error(400, b'{"error":"invalid_grant"}')))
        with self.assertRaises(cu.GrantRevoked) as cm:
            cu.refresh_token("dead-token", cu.TOKEN_HOSTS[0])
        self.assertIn("invalid_grant", str(cm.exception))
        self.assertEqual(len(self.calls), 1)

    def test_nested_error_body_shape_is_recognized(self):
        self._patch_urlopen(raiser(http_error(400, b'{"error":{"type":"invalid_grant"}}')))
        with self.assertRaises(cu.GrantRevoked):
            cu.refresh_token("dead-token", cu.TOKEN_HOSTS[0])

    def test_5xx_body_is_not_a_grant_verdict(self):
        # a proxy or gateway body must not park the account; only a 400 speaks for OAuth
        self._patch_urlopen(raiser(http_error(500, b'{"error":"invalid_grant"}')))
        with self.assertRaises(RuntimeError) as cm:
            cu.refresh_token("t", cu.TOKEN_HOSTS[0])
        self.assertNotIsInstance(cm.exception, cu.GrantRevoked)
        self.assertNotIn("invalid_grant", str(cm.exception))

    def test_unparseable_400_body_still_reports_the_status(self):
        self._patch_urlopen(raiser(http_error(400, b"not json")))
        with self.assertRaises(RuntimeError) as cm:
            cu.refresh_token("t")
        self.assertNotIsInstance(cm.exception, cu.GrantRevoked)
        self.assertIn("HTTP 400", str(cm.exception))
        self.assertEqual(len(self.calls), len(cu.TOKEN_HOSTS))  # plain 400 still tries them all


class TestTokenForParkedLatch(Patched):
    SEC = {"refreshToken": "dead", "tokenHost": "https://h.test", "scopes": ["s"]}

    def test_latched_account_is_not_refreshed(self):
        self._patch("load_secret", lambda u: {**self.SEC, "needsLogin": True})
        self._patch("refresh_token", lambda *a, **k: self.fail("refreshed a latched account"))
        token, err = cu.token_for_parked("u1")
        self.assertIsNone(token)
        self.assertEqual(err, cu.NEEDS_LOGIN)

    def test_force_does_not_bypass_the_latch(self):
        self._patch("load_secret", lambda u: {**self.SEC, "needsLogin": True,
                                              "accessToken": "a", "expiresAt": 2**53})
        self._patch("refresh_token", lambda *a, **k: self.fail("refreshed a latched account"))
        token, err = cu.token_for_parked("u1", force=True)
        self.assertEqual((token, err), (None, cu.NEEDS_LOGIN))

    def test_grant_revoked_sets_the_latch(self):
        stored = []
        self._patch("load_secret", lambda u: dict(self.SEC))
        self._patch("refresh_token", raiser(REVOKED))
        self._patch("store_secret", lambda uuid, refresh, access=None, expires_at=None,
                    host=None, meta=None: stored.append((uuid, refresh, meta)) or True)
        token, err = cu.token_for_parked("u1")
        self.assertEqual((token, err), (None, cu.NEEDS_LOGIN))
        self.assertEqual(stored, [("u1", "dead", {"needsLogin": True})])

    def test_latch_skipped_when_a_concurrent_refresh_rotated_the_token(self):
        # another process rotated to a live token between our load and the failed refresh;
        # latching (or writing at all) would clobber the account's only good credential
        secs = [dict(self.SEC), {**self.SEC, "refreshToken": "fresh"}]
        self._patch("load_secret", lambda u: dict(secs.pop(0)))
        self._patch("refresh_token", raiser(REVOKED))
        self._patch("store_secret", lambda *a, **k: self.fail("wrote over a rotated token"))
        token, err = cu.token_for_parked("u1")
        self.assertIsNone(token)
        self.assertNotEqual(err, cu.NEEDS_LOGIN)
        self.assertIn("rotation", err)

    def test_failed_latch_write_is_not_reported_as_paused(self):
        self._patch("load_secret", lambda u: dict(self.SEC))
        self._patch("refresh_token", raiser(REVOKED))
        self._patch("store_secret", lambda *a, **k: False)
        token, err = cu.token_for_parked("u1")
        self.assertIsNone(token)
        self.assertNotEqual(err, cu.NEEDS_LOGIN)    # the exact-match row flag must stay off
        self.assertTrue(err.startswith(cu.NEEDS_LOGIN))
        self.assertIn("Keychain", err)

    def test_transient_failure_keeps_retrying(self):
        stored = []
        self._patch("load_secret", lambda u: dict(self.SEC))
        self._patch("refresh_token", raiser(RuntimeError("HTTP 500 at https://h.test")))
        self._patch("store_secret", lambda *a, **k: stored.append(a) or True)
        token, err = cu.token_for_parked("u1")
        self.assertIsNone(token)
        self.assertIn("refresh failed", err)
        self.assertEqual(stored, [])                # no latch: the next tick tries again


class TestStoreSecretClearsLatch(Patched):
    def _written(self, prev, *args, **kw):
        out = {}
        self._patch("load_secret", lambda u: dict(prev))
        self._patch("keychain_write", lambda svc, acct, secret:
                    out.update(json.loads(secret)) or True)
        self.assertTrue(cu.store_secret("u1", *args, **kw))
        return out

    def test_new_refresh_token_drops_the_latch(self):
        prev = {"refreshToken": "dead", "needsLogin": True, "scopes": ["s"]}
        rec = self._written(prev, "fresh", "acc", 123, "https://h.test")
        self.assertNotIn("needsLogin", rec)
        self.assertEqual(rec["refreshToken"], "fresh")

    def test_same_refresh_token_keeps_the_latch(self):
        prev = {"refreshToken": "dead", "scopes": ["s"]}
        rec = self._written(prev, "dead", None, None, "https://h.test",
                            meta={"needsLogin": True})
        self.assertTrue(rec["needsLogin"])


class TestRowFlag(Patched):
    def setUp(self):
        self._patch("load_index", lambda: [
            {"uuid": "latched", "email": "latched@x.test", "label": "latched"},
            {"uuid": "flaky", "email": "flaky@x.test", "label": "flaky"}])
        self._patch("active_uuid_only", lambda: None)
        self._patch("match_live_uuid", lambda: None)
        self._patch("read_live", lambda: None)
        self._patch("collect_codex", lambda persist=True: [])
        self._patch("token_for_parked", lambda uuid, force=False:
                    (None, cu.NEEDS_LOGIN if uuid == "latched"
                     else "refresh failed (HTTP 500 at h) — sign into it once and re-run"))

    def test_only_the_latched_row_is_flagged(self):
        rows = {r["uuid"]: r for r in cu._collect_live(ingest=False)}
        self.assertTrue(rows["latched"]["needs_login"])
        self.assertEqual(rows["latched"]["error"], cu.NEEDS_LOGIN)
        self.assertNotIn("needs_login", rows["flaky"])
        self.assertIn("refresh failed", rows["flaky"]["error"])


class TestCollectFreshness(Patched):
    """Latched rows are definitive answers: they must not push the render onto stale
    cached values, which would hide the sign-in state for as long as the latch holds."""

    def setUp(self):
        self._patch("mock_enabled", lambda: False)
        self._patch("append_history", lambda rows, ts: None)
        self._patch("maybe_auto_switch", lambda rows: rows)
        # collect()'s act path runs the real observe subprocess against real state otherwise
        self._patch("desktop_observe", lambda heal=False: None)
        self.saved = []
        self._patch("save_cache", lambda rows, ts: self.saved.append(rows))
        self._patch("load_cache", lambda: {"ts": 0, "rows": [
            {"provider": "claude", "uuid": "u1", "error": None}]})

    def test_all_latched_rows_render_fresh_not_stale(self):
        latched = [{"provider": "claude", "uuid": "u1",
                    "error": cu.NEEDS_LOGIN, "needs_login": True}]
        self._patch("_collect_live", lambda ingest=True: list(latched))
        rows = cu.collect()
        self.assertEqual(rows, latched)
        self.assertTrue(self.saved)

    def test_all_transient_errors_still_fall_back_to_cache(self):
        erry = [{"provider": "claude", "uuid": "u1", "error": "refresh failed (x)"}]
        self._patch("_collect_live", lambda ingest=True: erry)
        rows = cu.collect()
        self.assertTrue(rows and all(r.get("stale") for r in rows))
        self.assertFalse(self.saved)


if __name__ == "__main__":
    unittest.main()
