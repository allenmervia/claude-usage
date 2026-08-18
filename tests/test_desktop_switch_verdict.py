#!/usr/bin/env python3
"""Unit tests for the desktop-switch failure verdict the menu bar shows: the wrapper
must surface the tool's whole refusal, not a fragment of it, and must not let the
pre-switch warnings that share stderr stand in for the reason.

Run:  python3 -m unittest discover -s tests
"""
import types
import unittest

from support import Patched, cu, run_capturing


class TestSwitchVerdict(unittest.TestCase):
    """_switch_verdict: the reason, whole, as one wrappable sentence."""

    def test_multiline_refusal_is_joined_into_one_sentence(self):
        err = ("the stash 'Allen-2' holds a signed-out session — installing it can only\n"
               "land on the sign-in banner. Sign into the account in the app, quit it,\n"
               "and `recapture Allen-2` — or `forget Allen-2`.")
        self.assertEqual(
            cu._switch_verdict(err),
            "the stash 'Allen-2' holds a signed-out session — installing it can only "
            "land on the sign-in banner. Sign into the account in the app, quit it, "
            "and `recapture Allen-2` — or `forget Allen-2`.")

    def test_pre_switch_warnings_are_dropped(self):
        err = ("warning: 'Allen-2' has been parked 5.3d, and the server invalidates\n"
               "         sessions idle for about a day — expect the sign-in banner.\n"
               "         desktop-switch.py recapture\n"
               "Claude.app did not quit within 45s — nothing was changed.\n"
               "Something is holding it open (a modal dialog?). Quit it by hand and retry.")
        self.assertEqual(
            cu._switch_verdict(err),
            "Claude.app did not quit within 45s — nothing was changed. "
            "Something is holding it open (a modal dialog?). Quit it by hand and retry.")

    def test_warning_only_output_yields_nothing(self):
        err = ("warning: 'Allen-2' was captured under app v1.0, now v2.0.\n"
               "         If the app has migrated its stores since, this may land on a login screen.")
        self.assertEqual(cu._switch_verdict(err), "")

    def test_indented_verdict_lines_survive_outside_a_warning(self):
        err = ("recapture cannot attribute this profile to a stash safely. Quit the app and run:\n"
               "  desktop-switch.py capture <label>   (the label's old stash is kept aside)")
        self.assertIn("desktop-switch.py capture <label>", cu._switch_verdict(err))

    def test_a_blank_line_does_not_end_a_warning_block(self):
        err = ("warning: 'X' has been parked 5.3d\n"
               "\n"
               "         desktop-switch.py recapture\n"
               "the real verdict")
        self.assertEqual(cu._switch_verdict(err), "the real verdict")

    def test_a_traceback_reports_only_its_exception_line(self):
        err = ("Traceback (most recent call last):\n"
               '  File "tools/desktop-switch.py", line 524, in cmd_switch\n'
               "    n, _ = copy_identity(PROFILE, rb)\n"
               "OSError: [Errno 28] No space left on device")
        self.assertEqual(cu._switch_verdict(err),
                         "OSError: [Errno 28] No space left on device")

    def test_a_traceback_after_warnings_still_reports_its_exception_line(self):
        err = ("warning: 'X' has been parked 5.3d\n"
               "         desktop-switch.py recapture\n"
               "Traceback (most recent call last):\n"
               '  File "tools/desktop-switch.py", line 514, in cmd_switch\n'
               "PermissionError: [Errno 13] Permission denied")
        self.assertEqual(cu._switch_verdict(err),
                         "PermissionError: [Errno 13] Permission denied")

    def test_rollback_diagnostics_are_kept_with_the_verdict(self):
        err = ("! OSError while installing — rolling back\n"
               "the switch was rolled back; the profile is as it was.")
        self.assertEqual(cu._switch_verdict(err),
                         "! OSError while installing — rolling back "
                         "the switch was rolled back; the profile is as it was.")

    def test_progress_lines_are_dropped(self):
        out = ("· quitting Claude.app…\n"
               "· saved 12 files to roll back to\n"
               "· staged the incoming profile")
        self.assertEqual(cu._switch_verdict(out), "")


class TestCmdDesktopSwitch(Patched):
    """The wrapper forwards the whole verdict to the bar."""

    def _run_result(self, returncode, stderr, stdout=""):
        stub = types.SimpleNamespace(
            run=lambda *a, **k: types.SimpleNamespace(
                returncode=returncode, stderr=stderr, stdout=stdout))
        self._patch("subprocess", stub)

    def _fail_text(self):
        code, _, err = run_capturing(cu.cmd_desktop_switch, "Allen-2")
        self.assertEqual(code, 1)
        return err.strip()

    def test_failure_forwards_the_whole_verdict(self):
        self._run_result(1, "an earlier operation did not finish — run "
                            "`desktop-switch.py repair` first")
        self.assertEqual(self._fail_text(),
                         "an earlier operation did not finish — run "
                         "`desktop-switch.py repair` first")

    def test_failure_with_warnings_reports_only_the_verdict(self):
        self._run_result(1, ("warning: 'X' has been parked 5.3d, and the server invalidates\n"
                             "         sessions idle for about a day.\n"
                             "the stash 'X' holds a signed-out session — installing it can only\n"
                             "land on the sign-in banner."))
        self.assertEqual(self._fail_text(),
                         "the stash 'X' holds a signed-out session — installing it can only "
                         "land on the sign-in banner.")

    def test_warning_only_failure_falls_back_to_the_last_line(self):
        self._run_result(1, ("warning: 'X' has been parked 5.3d, and the server invalidates\n"
                             "         sessions idle for about a day. Refresh the stash:\n"
                             "         desktop-switch.py recapture"))
        self.assertEqual(self._fail_text(), "desktop-switch.py recapture")

    def test_progress_only_failure_falls_back_to_the_last_line(self):
        self._run_result(1, "", stdout=("· quitting Claude.app…\n"
                                        "· saved 12 files to roll back to"))
        self.assertEqual(self._fail_text(), "· saved 12 files to roll back to")

    def test_empty_failure_output_gets_the_generic_message(self):
        self._run_result(1, "")
        self.assertEqual(self._fail_text(), "desktop switch failed")


if __name__ == "__main__":
    unittest.main()
