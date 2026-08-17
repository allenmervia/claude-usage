#!/usr/bin/env python3
"""Unit tests for desktop-switch identity reading, displaced-stash preservation, and the
identity guards on capture/switch.

Run:  python3 -m unittest discover -s tests
"""
import contextlib
import importlib.util
import io
import os
import tempfile
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_TOOL = os.path.join(_HERE, "..", "tools", "desktop-switch.py")

UUID_A = "aaaaaaaa-1111-2222-3333-444444444444"
UUID_B = "bbbbbbbb-1111-2222-3333-444444444444"

BLOCK = 32768


def load_tool(case, profile, state):
    """Import the tool against throwaway dirs (it reads its env at import time)."""
    for key, val in (("CU_DESKTOP_PROFILE", profile), ("CU_DESKTOP_STATE", state),
                     ("CU_SKIP_APP_CONTROL", "1")):
        case.addCleanup(os.environ.pop, key, None)
        os.environ[key] = val
    spec = importlib.util.spec_from_file_location("dsw", _TOOL)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def record(payload, rtype=1):
    """One leveldb log record: [crc:4][length:2][type:1][payload]."""
    return b"\x00\x00\x00\x00" + len(payload).to_bytes(2, "little") + bytes([rtype]) + payload


def account_json(uuid):
    return ('{"accountUuid":"%s"}' % uuid).encode()


def write_store_file(root, name, data):
    d = os.path.join(root, "Local Storage", "leveldb")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, name), "wb") as f:
        f.write(data)


def write_log(root, chunks, name="000003.log"):
    write_store_file(root, name, b"".join(chunks))


class _ToolCase(unittest.TestCase):
    """Throwaway profile + state dirs and the helpers the test classes share."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.profile = os.path.join(self.tmp, "profile")
        os.makedirs(self.profile)
        self.state = os.path.join(self.tmp, "state")
        self.ds = load_tool(self, self.profile, self.state)

    def touch_cookie(self):
        with open(os.path.join(self.profile, "Cookies"), "wb") as f:
            f.write(b"live-cookie-bytes")

    def sign_in(self, uuid):
        write_log(self.profile, [record(account_json(uuid))])

    def make_stash(self, label, uuid, app_version="1.0.0"):
        d = os.path.join(self.state, "desktop-stashes", label, "files")
        os.makedirs(d)
        with open(os.path.join(d, "Cookies"), "wb") as f:
            f.write(f"old-{label}".encode())
        self.ds.write_json(os.path.join(self.state, "desktop-stashes", label, "manifest.json"),
                           {"label": label, "captured_at": 1.0, "app_version": app_version,
                            "account_uuid": uuid, "files": {"Cookies": "stale"}})

    def stash_cookie(self, label):
        p = os.path.join(self.state, "desktop-stashes", label, "files", "Cookies")
        with open(p, "rb") as f:
            return f.read()

    def preserve(self, target):
        with contextlib.redirect_stdout(io.StringIO()):
            self.ds._preserve_displaced(target)


class TestProfileIdentity(_ToolCase):
    def test_full_record(self):
        write_log(self.profile, [record(account_json(UUID_A))])
        self.assertEqual(self.ds.profile_identity(self.profile), UUID_A)

    def test_value_split_across_blocks(self):
        # The uuid straddles a 32KB block boundary as FIRST/LAST fragments — invisible to a
        # flat scan of the file, which is the case the log parser exists for.
        js = account_json(UUID_A)
        filler = record(b"x" * 32000)
        head, tail = js[:len(js) // 2], js[len(js) // 2:]
        pad = BLOCK - len(filler) - 7 - len(head)
        write_log(self.profile, [filler,
                                 record(b"A" * pad + head, rtype=2),
                                 record(tail, rtype=4)])
        self.assertEqual(self.ds.profile_identity(self.profile), UUID_A)

    def test_utf16_value_with_interleaved_nuls(self):
        js = account_json(UUID_A).decode().encode("utf-16-le")
        write_log(self.profile, [record(js)])
        self.assertEqual(self.ds.profile_identity(self.profile), UUID_A)

    def test_nul_runs_do_not_assemble_a_uuid(self):
        # UTF-16 interleaves single NULs; a run of two or more is a gap between entries and
        # must not let fragments concatenate into a uuid nobody owns.
        js = account_json(UUID_A)
        write_log(self.profile, [record(js[:-3] + b"\x00\x00" + js[-3:])])
        self.assertIsNone(self.ds.profile_identity(self.profile))

    def test_last_signin_wins(self):
        write_log(self.profile, [record(account_json(UUID_A)),
                                 record(account_json(UUID_B))])
        self.assertEqual(self.ds.profile_identity(self.profile), UUID_B)

    def test_log_evidence_outranks_newer_table(self):
        # The live log is numbered BELOW tables flushed after it was created; its content is
        # the freshest writes regardless of the numbers.
        write_log(self.profile, [record(account_json(UUID_A))], name="000006.log")
        write_store_file(self.profile, "000007.ldb", account_json(UUID_B))
        self.assertEqual(self.ds.profile_identity(self.profile), UUID_A)

    def test_tables_answer_when_logs_are_silent(self):
        write_store_file(self.profile, "000007.ldb", account_json(UUID_B))
        self.assertEqual(self.ds.profile_identity(self.profile), UUID_B)

    def test_bookkeeping_files_are_ignored(self):
        write_store_file(self.profile, "MANIFEST-000009", account_json(UUID_A))
        write_store_file(self.profile, "LOG", account_json(UUID_B))
        self.assertIsNone(self.ds.profile_identity(self.profile))

    def test_no_storage_is_none(self):
        self.assertIsNone(self.ds.profile_identity(self.profile))

    def test_corrupt_log_is_survivable(self):
        write_log(self.profile, [b"\xff" * 100])
        self.assertIsNone(self.ds.profile_identity(self.profile))


class TestPreserveDisplaced(_ToolCase):
    def setUp(self):
        super().setUp()
        self.touch_cookie()

    def test_refreshes_the_identity_owner_not_the_recorded_label(self):
        self.sign_in(UUID_A)
        self.make_stash("right", UUID_A)
        self.make_stash("wrong", UUID_B)
        self.ds.write_json(self.ds.ACTIVE, {"label": "wrong", "at": 1.0, "files": {}})
        self.preserve("elsewhere")
        self.assertEqual(self.stash_cookie("right"), b"live-cookie-bytes")
        self.assertEqual(self.stash_cookie("wrong"), b"old-wrong")
        aside = os.listdir(os.path.join(self.state, "desktop-stash-asides"))
        self.assertEqual(len(aside), 1)          # the overwritten copy is kept, not deleted
        self.assertTrue(aside[0].endswith("-right"))

    def test_target_stash_is_never_touched(self):
        # Switching to the account you are already on means "install the capture" — the
        # capture must not be replaced by the live bytes it is about to displace.
        self.sign_in(UUID_A)
        self.make_stash("mine", UUID_A)
        self.preserve("mine")
        self.assertEqual(self.stash_cookie("mine"), b"old-mine")
        self.assertFalse(os.path.isdir(os.path.join(self.state, "desktop-stash-asides")))

    def test_adopts_recorded_label_when_it_cannot_disagree(self):
        self.sign_in(UUID_A)
        self.make_stash("claimed", None)
        self.ds.write_json(self.ds.ACTIVE, {"label": "claimed", "at": 1.0, "files": {}})
        self.preserve("other")
        self.assertEqual(self.stash_cookie("claimed"), b"live-cookie-bytes")
        self.assertEqual((self.ds.stash_meta("claimed") or {}).get("account_uuid"), UUID_A)

    def test_unknown_identity_quarantines_instead_of_overwriting(self):
        self.sign_in(UUID_B)
        self.make_stash("only", UUID_A)
        self.preserve("only")
        unclaimed = [l for l in self.ds.list_stashes() if l.startswith("unclaimed-")]
        self.assertEqual(len(unclaimed), 1)
        self.assertEqual(self.stash_cookie(unclaimed[0]), b"live-cookie-bytes")
        self.assertEqual(self.stash_cookie("only"), b"old-only")
        meta = self.ds.stash_meta(unclaimed[0]) or {}
        self.assertEqual(meta.get("account_uuid"), UUID_B)
        self.assertTrue(meta.get("unclaimed"))

    def test_unreadable_identity_heals_pre_update_recorded_label(self):
        self.make_stash("claimed", None, app_version="0.0.1")
        self.ds.app_version = lambda: "9.9.9"
        self.ds.write_json(self.ds.ACTIVE, {"label": "claimed", "at": 1.0, "files": {}})
        self.preserve("other")
        self.assertEqual(self.stash_cookie("claimed"), b"live-cookie-bytes")
        # bytes nobody identified must not be stamped with a uuid
        self.assertIsNone((self.ds.stash_meta("claimed") or {}).get("account_uuid"))

    def test_unreadable_identity_leaves_current_version_stash_alone(self):
        self.make_stash("claimed", None, app_version="9.9.9")
        self.ds.app_version = lambda: "9.9.9"
        self.ds.write_json(self.ds.ACTIVE, {"label": "claimed", "at": 1.0, "files": {}})
        self.preserve("other")
        self.assertEqual(self.stash_cookie("claimed"), b"old-claimed")

    def test_byte_exact_stash_gets_bookkeeping_only(self):
        self.make_stash("here", None, app_version="0.0.1")
        meta_path = os.path.join(self.state, "desktop-stashes", "here", "manifest.json")
        m = self.ds.stash_meta("here")
        m["files"] = self.ds.manifest(self.profile)
        self.ds.write_json(meta_path, m)
        self.ds.app_version = lambda: "9.9.9"
        self.preserve("other")
        m2 = self.ds.stash_meta("here")
        self.assertEqual(m2["app_version"], "9.9.9")               # bookkeeping freshened
        self.assertEqual(self.stash_cookie("here"), b"old-here")   # files untouched

    def test_signed_out_profile_preserves_nothing(self):
        os.remove(os.path.join(self.profile, "Cookies"))
        self.make_stash("only", UUID_A)
        self.preserve("elsewhere")
        self.assertEqual(self.ds.list_stashes(), ["only"])
        self.assertEqual(self.stash_cookie("only"), b"old-only")


class TestIdentityGuards(_ToolCase):
    def setUp(self):
        super().setUp()
        self.touch_cookie()

    def test_stash_uuid_never_writes(self):
        d = os.path.join(self.state, "desktop-stashes", "bare", "files")
        os.makedirs(d)
        with open(os.path.join(d, "Cookies"), "wb") as f:
            f.write(b"x")
        self.assertIsNone(self.ds._stash_uuid("bare"))
        self.assertFalse(os.path.exists(
            os.path.join(self.state, "desktop-stashes", "bare", "manifest.json")))

    def test_capture_refuses_a_second_label_for_one_account(self):
        self.sign_in(UUID_A)
        self.make_stash("work", UUID_A)
        with contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit) as cm:
                self.ds.cmd_capture("personal")
        self.assertIn("work", str(cm.exception))

    def test_capture_refreshes_its_own_label_and_keeps_an_aside(self):
        self.sign_in(UUID_A)
        self.make_stash("work", UUID_A)
        with contextlib.redirect_stdout(io.StringIO()):
            self.ds.cmd_capture("work")
        self.assertEqual(self.stash_cookie("work"), b"live-cookie-bytes")
        aside = os.listdir(os.path.join(self.state, "desktop-stash-asides"))
        self.assertEqual(len(aside), 1)
        self.assertTrue(aside[0].endswith("-work"))

    def test_switch_refuses_stash_whose_files_belie_the_manifest(self):
        self.make_stash("liar", UUID_A)
        write_log(os.path.join(self.state, "desktop-stashes", "liar", "files"),
                  [record(account_json(UUID_B))])
        with contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit) as cm:
                self.ds.cmd_switch("liar")
        self.assertIn("records one account", str(cm.exception))

    def test_rename_clears_the_unclaimed_flag(self):
        self.make_stash("unclaimed-x", UUID_A)
        meta_path = os.path.join(self.state, "desktop-stashes", "unclaimed-x", "manifest.json")
        m = self.ds.stash_meta("unclaimed-x")
        m["unclaimed"] = True
        self.ds.write_json(meta_path, m)
        with contextlib.redirect_stdout(io.StringIO()):
            self.ds.cmd_rename("unclaimed-x", "named")
        m2 = self.ds.stash_meta("named") or {}
        self.assertNotIn("unclaimed", m2)
        self.assertEqual(m2["label"], "named")


if __name__ == "__main__":
    unittest.main()
