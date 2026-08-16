#!/usr/bin/env python3
"""Unit tests for the needs-login latch: a parked account whose refresh token the server
revoked (invalid_grant) stops being retried until a real sign-in stores a new token.

Run:  python3 -m unittest discover -s tests
"""
import importlib.util
import io
import json
import os
import unittest
import urllib.error

_HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location(
    "claude_usage_needs_login", os.path.join(_HERE, "..", "claude-usage.py"))
cu = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cu)


def http_error(code, body=b""):
    return urllib.error.HTTPError("https://h.test", code, "err", {}, io.BytesIO(body))


class _Patched(unittest.TestCase):
    def _patch(self, name, stub):
        orig = getattr(cu, name)
        setattr(cu, name, stub)
        self.addCleanup(setattr, cu, name, orig)


class TestRefreshTokenErrorParsing(_Patched):
    def test_invalid_grant_survives_a_later_hosts_404(self):
        # the grant's home host names the failure; the fallback hosts 404 — the terminal
        # invalid_grant must be what the caller sees, not the last host's 404
        calls = []
        def urlopen(req, timeout=None):
            calls.append(req.full_url)
            raise http_error(400, b'{"error":"invalid_grant"}') if len(calls) == 1 \
                else http_error(404)
        self._patch("urllib", type("U", (), {
            "request": type("R", (), {"Request": urllib.request.Request,
                                      "urlopen": staticmethod(urlopen)}),
            "error": urllib.error})())
        with self.assertRaises(RuntimeError) as cm:
            cu.refresh_token("dead-token", cu.TOKEN_HOSTS[0])
        self.assertIn("invalid_grant", str(cm.exception))
        self.assertEqual(len(calls), len(cu.TOKEN_HOSTS))

    def test_unparseable_body_still_reports_the_status(self):
        def urlopen(req, timeout=None):
            raise http_error(400, b"not json")
        self._patch("urllib", type("U", (), {
            "request": type("R", (), {"Request": urllib.request.Request,
                                      "urlopen": staticmethod(urlopen)}),
            "error": urllib.error})())
        with self.assertRaises(RuntimeError) as cm:
            cu.refresh_token("t")
        self.assertIn("HTTP 400", str(cm.exception))
        self.assertNotIn("invalid_grant", str(cm.exception))


class TestTokenForParkedLatch(_Patched):
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

    def test_invalid_grant_sets_the_latch(self):
        stored = []
        self._patch("load_secret", lambda u: dict(self.SEC))
        self._patch("refresh_token", lambda *a, **k: (_ for _ in ()).throw(
            RuntimeError("HTTP 400 (invalid_grant) at https://h.test")))
        self._patch("store_secret", lambda uuid, refresh, access=None, expires_at=None,
                    host=None, meta=None: stored.append((uuid, refresh, meta)) or True)
        token, err = cu.token_for_parked("u1")
        self.assertEqual((token, err), (None, cu.NEEDS_LOGIN))
        self.assertEqual(stored, [("u1", "dead", {"needsLogin": True})])

    def test_transient_failure_keeps_retrying(self):
        stored = []
        self._patch("load_secret", lambda u: dict(self.SEC))
        self._patch("refresh_token", lambda *a, **k: (_ for _ in ()).throw(
            RuntimeError("HTTP 500 at https://h.test")))
        self._patch("store_secret", lambda *a, **k: stored.append(a) or True)
        token, err = cu.token_for_parked("u1")
        self.assertIsNone(token)
        self.assertIn("refresh failed", err)
        self.assertEqual(stored, [])                # no latch: the next tick tries again


class TestStoreSecretClearsLatch(_Patched):
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


class TestRowFlag(_Patched):
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


if __name__ == "__main__":
    unittest.main()
