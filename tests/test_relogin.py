#!/usr/bin/env python3
"""Unit tests for `relogin`: the recovery that signs a server-revoked account back in,
captures the credential it writes, and hands the CLI back to the account it displaced.

Run:  python3 -m unittest discover -s tests
"""
import contextlib
import io
import unittest

from support import Patched, cu

TARGET = {"uuid": "u-target", "email": "target@x.test"}
PREV = {"uuid": "u-prev", "email": "prev@x.test"}


class TestAwaitNewLive(Patched):
    """The wait for a credential that isn't the one we started with."""

    def setUp(self):
        import tempfile
        self._patch("LOGIN_POLL_S", 0)
        self._patch("time", _NoSleep(cu.time))
        self._patch("LOGIN_STATUS", cu.os.path.join(tempfile.mkdtemp(), "relogin.status"))

    def _finished(self, status):
        with open(cu.LOGIN_STATUS, "w") as f:
            f.write(f"{status}\n")

    def _lives(self, *blobs):
        seq = list(blobs)
        self._patch("read_live", lambda: seq.pop(0) if seq else blobs[-1])

    def test_returns_the_uuid_of_a_credential_that_replaced_the_old_one(self):
        self._lives({"accessToken": "a", "refreshToken": "old"},
                    {"accessToken": "b", "refreshToken": "new"})
        self._patch("api_get", lambda url, tok: {"account": {"uuid": "u-target"}})
        self.assertEqual(cu.await_new_live("old", deadline=cu.time.time() + 10), "u-target")

    def test_unchanged_credential_is_not_a_sign_in(self):
        # the window is open and nothing has happened yet: waiting is the whole job
        self._lives({"accessToken": "a", "refreshToken": "old"})
        self._patch("api_get", lambda url, tok: self.fail("identified an unchanged credential"))
        self.assertIsNone(cu.await_new_live("old", deadline=cu.time.time() + 0.05))

    def test_profile_failure_keeps_waiting_then_resolves(self):
        # a blob caught mid-write, or a token the API hasn't caught up with, is not a verdict
        self._lives({"accessToken": "b", "refreshToken": "new"})
        calls = []
        def flaky(url, tok):
            calls.append(tok)
            if len(calls) < 3:
                raise RuntimeError("not yet")
            return {"account": {"uuid": "u-target"}}
        self._patch("api_get", flaky)
        self.assertEqual(cu.await_new_live("old", deadline=cu.time.time() + 10), "u-target")
        self.assertEqual(len(calls), 3)

    def test_missing_live_credential_never_resolves(self):
        self._patch("read_live", lambda: None)
        self.assertIsNone(cu.await_new_live("old", deadline=cu.time.time() + 0.05))

    def test_a_sign_in_that_failed_ends_the_wait_early(self):
        # otherwise an abandoned sign-in holds the app's controls for the whole window
        self._lives({"accessToken": "a", "refreshToken": "old"})
        self._finished(1)
        self.assertIsNone(cu.await_new_live("old", deadline=cu.time.time() + 3600))

    def test_a_clean_exit_keeps_waiting_for_the_credential(self):
        # the credential is written before the CLI exits, but identifying it can need a retry
        self._lives({"accessToken": "a", "refreshToken": "old"},
                    {"accessToken": "b", "refreshToken": "new"})
        self._finished(0)
        self._patch("api_get", lambda url, tok: {"account": {"uuid": "u-target"}})
        self.assertEqual(cu.await_new_live("old", deadline=cu.time.time() + 10), "u-target")


class TestRelogin(Patched):
    """The command around that wait, with every IO seam stubbed."""

    def setUp(self):
        self.launched = []
        self.switched = []
        self.captured = []
        self.secret = {"needsLogin": None}
        self._patch("load_index", lambda: [dict(TARGET), dict(PREV)])
        self._patch("resolve_target", lambda k: ("claude", dict(TARGET), None))
        self._patch("active_uuid_only", lambda: "u-prev")
        self._patch("read_live", lambda: {"refreshToken": "old"})
        self._patch("_launch_login", lambda email: (self.launched.append(email), (True, ""))[1])
        self._patch("await_new_live", lambda before: "u-target")
        self._patch("ingest_live", lambda idx: self.captured.append(idx) or "u-target")
        self._patch("load_secret", lambda u: dict(self.secret))
        self._patch("clear_cache", lambda: None)
        self._patch("record_last_switch", lambda p, auto=False: None)
        def fake_switch(e):
            self.switched.append(e["uuid"])
            return True, f"switched to {e['email']}", ""
        self._patch("_switch_claude", fake_switch)

    def _run(self, target="target@x.test"):
        """(exit_code, stdout, stderr) — 0 when the command returned without failing."""
        out, err = io.StringIO(), io.StringIO()
        code = 0
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                cu.cmd_relogin(target)
            except SystemExit as ex:
                code = ex.code
        return code, out.getvalue(), err.getvalue()

    def test_success_captures_and_hands_the_cli_back(self):
        code, out, _ = self._run()
        self.assertEqual(code, 0)
        self.assertEqual(self.launched, ["target@x.test"])
        self.assertEqual(len(self.captured), 1)              # the capture is what drops the latch
        self.assertEqual(self.switched, ["u-prev"])
        self.assertIn("target@x.test is signed in again", out)
        self.assertIn("back on prev@x.test", out)

    def test_signing_into_the_account_that_was_already_active_leaves_the_cli_alone(self):
        self._patch("active_uuid_only", lambda: "u-target")
        code, out, _ = self._run()
        self.assertEqual(code, 0)
        self.assertEqual(self.switched, [])                  # nothing was displaced
        self.assertNotIn("back on", out)

    def test_an_unidentified_previous_account_still_says_where_the_cli_landed(self):
        # offline, or no credential at all: there is nowhere to hand the CLI back to, and
        # leaving that silent means finding out on the next `claude` run
        self._patch("active_uuid_only", lambda: None)
        code, out, _ = self._run()
        self.assertEqual(code, 0)
        self.assertEqual(self.switched, [])
        self.assertIn("CLI is now on target@x.test", out)

    def test_signing_in_the_wrong_account_is_a_failure_that_still_restores(self):
        # the credential that landed is captured either way, but the account we were sent to
        # recover is still signed out, and saying otherwise would be a lie the card repeats
        self._patch("await_new_live", lambda before: "u-prev")
        code, _, err = self._run()
        self.assertEqual(code, 1)
        self.assertEqual(len(self.captured), 1)
        self.assertIn("signed in prev@x.test, not target@x.test", err)

    def test_a_latch_that_survives_the_capture_is_reported(self):
        # ingest_live can decline the capture (a team context over a personal entry); the
        # account is then still parked on the dead grant, however well the sign-in went
        self.secret = {"needsLogin": True}
        code, _, err = self._run()
        self.assertEqual(code, 1)
        self.assertIn("couldn't be captured", err)
        self.assertEqual(self.switched, ["u-prev"])

    def test_no_sign_in_within_the_window_changes_nothing(self):
        self._patch("await_new_live", lambda before: None)
        code, _, err = self._run()
        self.assertEqual(code, 1)
        self.assertEqual(self.captured, [])
        self.assertEqual(self.switched, [])
        self.assertIn("still signed out", err)

    def test_a_terminal_that_will_not_open_fails_before_waiting(self):
        self._patch("_launch_login", lambda email: (False, "couldn't open a Terminal window"))
        self._patch("await_new_live", lambda before: self.fail("waited without a sign-in running"))
        code, _, err = self._run()
        self.assertEqual(code, 1)
        self.assertIn("Terminal", err)

    def test_a_failed_hand_back_is_named_not_swallowed(self):
        self._patch("_switch_claude", lambda e: (False, "dead token", ""))
        code, out, _ = self._run()
        self.assertEqual(code, 0)                            # the recovery itself did work
        self.assertIn("couldn't move it back", out)

    def test_codex_accounts_are_turned_away(self):
        self._patch("resolve_target", lambda k: ("codex", {"account_id": "c"}, None))
        code, _, err = self._run("codex:target@x.test")
        self.assertEqual(code, 1)
        self.assertIn("codex login", err)
        self.assertEqual(self.launched, [])

    def test_unknown_account_is_turned_away(self):
        self._patch("resolve_target", lambda k: (None, None, "unknown account: nope"))
        code, _, err = self._run("nope")
        self.assertEqual(code, 1)
        self.assertIn("unknown account", err)


class TestLoginScript(Patched):
    """The script handed to Terminal — the one place the sign-in's environment is decided."""

    def setUp(self):
        import tempfile
        self.tmp = tempfile.mkdtemp()
        self._patch("STATE_DIR", self.tmp)
        self._patch("LOGIN_SCRIPT", cu.os.path.join(self.tmp, "relogin.command"))
        self._patch("LOGIN_STATUS", cu.os.path.join(self.tmp, "relogin.status"))
        self.opened = []
        self._patch("subprocess", _RecordingRun(self.opened))

    def _script(self):
        with open(cu.LOGIN_SCRIPT) as f:
            return f.read()

    def _run_script(self):
        """Run the generated script with a stub `claude` on PATH, and return its argv."""
        import subprocess
        bin_dir = cu.os.path.join(self.tmp, "bin")
        cu.os.makedirs(bin_dir, exist_ok=True)
        argv = cu.os.path.join(self.tmp, "argv")
        stub = cu.os.path.join(bin_dir, "claude")
        with open(stub, "w") as f:
            f.write(f'#!/bin/sh\nfor a in "$@"; do echo "$a" >> {argv}; done\n')
        cu.os.chmod(stub, 0o700)
        subprocess.run(["/bin/sh", cu.LOGIN_SCRIPT], check=True, capture_output=True,
                       env={"PATH": bin_dir + ":/usr/bin:/bin", "HOME": self.tmp})
        with open(argv) as f:
            return f.read().splitlines()

    def test_it_strips_the_host_auth_the_desktop_app_injects(self):
        # a claude that authenticates through app-injected host auth never writes the Keychain
        # item this tool reads, so the sign-in would land nowhere we can see
        ok, msg = cu._launch_login("target@x.test")
        self.assertTrue(ok, msg)
        for var in cu.HOST_AUTH_ENV:
            self.assertIn(f"-u {var}", self._script())
        self.assertEqual(self._run_script(),
                         ["auth", "login", "--email", "target@x.test"])
        self.assertEqual(self.opened, [["open", "-a", "Terminal", cu.LOGIN_SCRIPT]])

    def test_an_address_carrying_shell_syntax_stays_one_argument(self):
        # the address comes from the accounts API and this file is run as a shell script
        payload = f"a'b$(touch {self.tmp}/pwned)@x.test"
        cu._launch_login(payload)
        self.assertEqual(self._run_script(), ["auth", "login", "--email", payload])
        self.assertFalse(cu.os.path.exists(cu.os.path.join(self.tmp, "pwned")))

    def test_a_terminal_that_will_not_open_is_reported(self):
        self._patch("subprocess", _RecordingRun(self.opened, fail=True))
        ok, msg = cu._launch_login("target@x.test")
        self.assertFalse(ok)
        self.assertIn("Terminal", msg)

    def test_the_script_records_its_exit_status(self):
        cu._launch_login("target@x.test")
        self._run_script()
        with open(cu.LOGIN_STATUS) as f:
            self.assertEqual(f.read().strip(), "0")

    def test_a_previous_run_s_status_is_cleared_before_launching(self):
        # left in place it reads as this sign-in finishing the instant it starts
        with open(cu.LOGIN_STATUS, "w") as f:
            f.write("1\n")
        cu._launch_login("target@x.test")
        self.assertFalse(cu.os.path.exists(cu.LOGIN_STATUS))


class _NoSleep:
    """The time module with sleep removed — the poll loop's cadence isn't under test."""
    def __init__(self, real):
        self._real = real
    def time(self):
        return self._real.time()
    def sleep(self, _s):
        pass


class _RecordingRun:
    def __init__(self, log, fail=False):
        self.log, self.fail = log, fail
    def run(self, args, **kw):
        self.log.append(args)
        if self.fail:
            raise OSError("no Terminal")


if __name__ == "__main__":
    unittest.main()
