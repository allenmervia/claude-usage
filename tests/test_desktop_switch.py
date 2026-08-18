#!/usr/bin/env python3
"""Unit tests for desktop-switch identity reading, displaced-stash preservation, and the
identity guards on capture/switch.

Run:  python3 -m unittest discover -s tests
"""
import contextlib
import importlib.util
import io
import json
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
        prev = os.environ.get(key)      # restore, don't just delete: a developer running the
        if prev is None:                # suite with these exported must get them back
            case.addCleanup(os.environ.pop, key, None)
        else:
            case.addCleanup(os.environ.__setitem__, key, prev)
        os.environ[key] = val
    spec = importlib.util.spec_from_file_location("dsw", _TOOL)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def record(payload, rtype=1):
    """One leveldb log record: [crc:4][length:2][type:1][payload]."""
    return b"\x00\x00\x00\x00" + len(payload).to_bytes(2, "little") + bytes([rtype]) + payload


def _vi(n):
    """leveldb varint32."""
    out = b""
    while True:
        b_, n = n & 0x7F, n >> 7
        out += bytes([b_ | (0x80 if n else 0)])
        if not n:
            return out


KEY = b"_https://claude.ai\x00\x01account"


def batch(*ops, seq=1):
    """A leveldb WriteBatch payload: [seq:8][count:4] then put/del entries."""
    out = seq.to_bytes(8, "little") + len(ops).to_bytes(4, "little")
    for op in ops:
        if op[0] == "put":
            _, k, v = op
            out += b"\x01" + _vi(len(k)) + k + _vi(len(v)) + v
        else:
            _, k = op
            out += b"\x00" + _vi(len(k)) + k
    return out


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
        import shutil
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
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

    def test_tombstone_after_signin_reads_signed_out(self):
        write_log(self.profile, [record(batch(("put", KEY, account_json(UUID_A)))),
                                 record(batch(("del", KEY)))])
        self.assertEqual(self.ds.profile_identity(self.profile), self.ds.SIGNED_OUT)

    def test_tombstone_in_a_later_log_still_counts(self):
        write_log(self.profile, [record(batch(("put", KEY, account_json(UUID_A))))],
                  name="000003.log")
        write_log(self.profile, [record(batch(("del", KEY)))], name="000005.log")
        self.assertEqual(self.ds.profile_identity(self.profile), self.ds.SIGNED_OUT)

    def test_resignin_after_tombstone_wins(self):
        write_log(self.profile, [record(batch(("put", KEY, account_json(UUID_A)))),
                                 record(batch(("del", KEY))),
                                 record(batch(("put", KEY, account_json(UUID_B))))])
        self.assertEqual(self.ds.profile_identity(self.profile), UUID_B)

    def test_accountless_overwrite_reads_signed_out(self):
        write_log(self.profile, [record(batch(("put", KEY, account_json(UUID_A)))),
                                 record(batch(("put", KEY, b'{"loggedOut":true}')))])
        self.assertEqual(self.ds.profile_identity(self.profile), self.ds.SIGNED_OUT)

    def test_unrelated_deletion_does_not_sign_out(self):
        write_log(self.profile, [record(batch(("put", KEY, account_json(UUID_A)))),
                                 record(batch(("del", b"some-other-key")))])
        self.assertEqual(self.ds.profile_identity(self.profile), UUID_A)

    def test_fragments_across_corruption_do_not_assemble(self):
        # a FIRST fragment followed by corruption then an orphan LAST from another record
        # must not be glued into a uuid the log never stored as one value
        js = account_json(UUID_A)
        head, tail = js[:len(js) // 2], js[len(js) // 2:]
        first = record(head, rtype=2)
        pad = BLOCK - len(first)
        write_log(self.profile, [first, b"\xff" * pad, record(tail, rtype=4)])
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

    def test_adopts_recorded_label_only_when_its_capture_predates_the_app(self):
        self.sign_in(UUID_A)
        self.make_stash("claimed", None, app_version="0.0.1")
        self.ds.app_version = lambda: "9.9.9"
        self.ds.write_json(self.ds.ACTIVE, {"label": "claimed", "at": 1.0, "files": {}})
        self.preserve("other")
        self.assertEqual(self.stash_cookie("claimed"), b"live-cookie-bytes")
        self.assertEqual((self.ds.stash_meta("claimed") or {}).get("account_uuid"), UUID_A)

    def test_same_version_recorded_stash_is_quarantined_not_adopted(self):
        # a same-version capture may be perfectly good with merely-unreadable identity;
        # the sign-in is preserved as unclaimed instead of overwriting it
        self.sign_in(UUID_A)
        self.make_stash("claimed", None, app_version="9.9.9")
        self.ds.app_version = lambda: "9.9.9"
        self.ds.write_json(self.ds.ACTIVE, {"label": "claimed", "at": 1.0, "files": {}})
        self.preserve("other")
        self.assertEqual(self.stash_cookie("claimed"), b"old-claimed")
        unclaimed = [l for l in self.ds.list_stashes() if l.startswith("unclaimed-")]
        self.assertEqual(len(unclaimed), 1)
        self.assertEqual(self.stash_cookie(unclaimed[0]), b"live-cookie-bytes")

    def test_refreshing_an_unclaimed_owner_keeps_the_quarantine_flag(self):
        self.sign_in(UUID_A)
        self.make_stash("unclaimed-20260813-101112", UUID_A)
        meta_path = os.path.join(self.state, "desktop-stashes", "unclaimed-20260813-101112",
                                 "manifest.json")
        m = self.ds.stash_meta("unclaimed-20260813-101112")
        m["unclaimed"] = True
        self.ds.write_json(meta_path, m)
        self.preserve("other")
        m2 = self.ds.stash_meta("unclaimed-20260813-101112") or {}
        self.assertEqual(self.stash_cookie("unclaimed-20260813-101112"), b"live-cookie-bytes")
        self.assertTrue(m2.get("unclaimed"))      # refresh must not christen it

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

    def test_byte_exact_stash_is_left_entirely_alone(self):
        # byte-identity proves the app never ran on these bytes, so even the bookkeeping
        # still describes the capture — freshening captured_at would mask real session age
        self.make_stash("here", None, app_version="0.0.1")
        meta_path = os.path.join(self.state, "desktop-stashes", "here", "manifest.json")
        m = self.ds.stash_meta("here")
        m["files"] = self.ds.manifest(self.profile)
        self.ds.write_json(meta_path, m)
        self.ds.app_version = lambda: "9.9.9"
        self.preserve("other")
        m2 = self.ds.stash_meta("here")
        self.assertEqual(m2["app_version"], "0.0.1")               # untouched
        self.assertEqual(m2["captured_at"], 1.0)                   # staleness clock intact
        self.assertEqual(self.stash_cookie("here"), b"old-here")   # files untouched

    def test_revoked_session_does_not_refresh_its_owners_stash(self):
        # the residue case: the profile still NAMES account A in old records, but the log
        # shows the session ended — A's working capture must not be replaced with dead bytes
        write_log(self.profile, [record(batch(("put", KEY, account_json(UUID_A)))),
                                 record(batch(("del", KEY)))])
        self.make_stash("mine", UUID_A)
        self.preserve("elsewhere")
        self.assertEqual(self.stash_cookie("mine"), b"old-mine")
        self.assertEqual(self.ds.list_stashes(), ["mine"])   # and nothing quarantined

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

    def test_capture_refuses_a_signed_out_profile(self):
        write_log(self.profile, [record(batch(("put", KEY, account_json(UUID_A)))),
                                 record(batch(("del", KEY)))])
        with contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit) as cm:
                self.ds.cmd_capture("work")
        self.assertIn("sign-out", str(cm.exception))

    def test_switch_refuses_a_stash_captured_signed_out(self):
        self.make_stash("dead", UUID_A)
        write_log(os.path.join(self.state, "desktop-stashes", "dead", "files"),
                  [record(batch(("put", KEY, account_json(UUID_A)))),
                   record(batch(("del", KEY)))])
        with contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit) as cm:
                self.ds.cmd_switch("dead")
        self.assertIn("signed-out session", str(cm.exception))

    def test_no_launch_refuses_a_running_app(self):
        self.make_stash("work", UUID_A)
        self.ds.app_running = lambda: True
        with contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit) as cm:
                self.ds.cmd_switch("work", no_launch=True)
        self.assertIn("must not quit", str(cm.exception))

    def test_recapture_refuses_without_any_attribution(self):
        # explicit label is a name, not evidence: with no readable identity and no ACTIVE
        # record, nothing says whose bytes these are
        self.touch_cookie()
        self.make_stash("work", UUID_A)
        with contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit) as cm:
                self.ds.cmd_recapture("work")
        self.assertIn("cannot attribute", str(cm.exception))

    def test_recapture_refuses_an_unclaimed_target(self):
        self.touch_cookie()
        self.sign_in(UUID_A)
        self.make_stash("unclaimed-x", UUID_A)
        meta_path = os.path.join(self.state, "desktop-stashes", "unclaimed-x", "manifest.json")
        m = self.ds.stash_meta("unclaimed-x")
        m["unclaimed"] = True
        self.ds.write_json(meta_path, m)
        with contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit) as cm:
                self.ds.cmd_recapture("unclaimed-x")
        self.assertIn("name it first", str(cm.exception))

    def test_capture_refuses_the_unclaimed_namespace(self):
        with contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit) as cm:
                self.ds.cmd_capture("unclaimed-20260817-000000")
        self.assertIn("reserved", str(cm.exception))

    def test_same_second_asides_get_unique_names(self):
        self.touch_cookie()
        d = os.path.join(self.state, "desktop-stashes", "twice", "files")
        os.makedirs(d)
        self.ds._aside_stash("twice")     # consumes the stash dir
        os.makedirs(d)
        self.ds._aside_stash("twice")     # same wall-clock second: must not collide
        asides = os.listdir(os.path.join(self.state, "desktop-stash-asides"))
        self.assertEqual(len(asides), 2)

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


class TestObserve(_ToolCase):
    """cmd_observe: reconcile the record with the profile's own identity."""

    def setUp(self):
        super().setUp()
        self.touch_cookie()

    def observe(self, heal=True):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.ds.cmd_observe(heal=heal)
        return json.loads(buf.getvalue())

    def test_hand_signin_heals_the_record_and_revokes_the_displaced_stash(self):
        # the incident: hand logout of the recorded account, hand sign-in to another
        self.sign_in(UUID_A)
        self.make_stash("allen", UUID_A)
        self.make_stash("allen-2", UUID_B)
        self.ds.write_json(self.ds.ACTIVE, {"label": "allen-2", "at": 1.0, "files": {}})
        out = self.observe()
        self.assertEqual(out["healed"], "allen")
        self.assertEqual(out["revoked"], ["allen-2"])
        self.assertEqual((self.ds.read_json(self.ds.ACTIVE) or {}).get("label"), "allen")
        self.assertTrue((self.ds.stash_meta("allen-2") or {}).get("revoked"))
        self.assertFalse((self.ds.stash_meta("allen") or {}).get("revoked"))

    def test_matching_record_is_left_alone(self):
        self.sign_in(UUID_A)
        self.make_stash("allen", UUID_A)
        self.ds.write_json(self.ds.ACTIVE, {"label": "allen", "at": 1.0, "files": {}})
        out = self.observe()
        self.assertIsNone(out["healed"])
        self.assertEqual(out["revoked"], [])

    def test_report_only_never_writes(self):
        self.sign_in(UUID_A)
        self.make_stash("allen", UUID_A)
        self.make_stash("allen-2", UUID_B)
        self.ds.write_json(self.ds.ACTIVE, {"label": "allen-2", "at": 1.0, "files": {}})
        out = self.observe(heal=False)
        self.assertIsNone(out["healed"])
        self.assertEqual((self.ds.read_json(self.ds.ACTIVE) or {}).get("label"), "allen-2")
        self.assertFalse((self.ds.stash_meta("allen-2") or {}).get("revoked"))

    def test_at_rest_signout_clears_the_record_and_revokes(self):
        write_log(self.profile, [record(batch(("put", KEY, account_json(UUID_A)))),
                                 record(batch(("del", KEY)))])
        self.make_stash("allen", UUID_A)
        self.ds.write_json(self.ds.ACTIVE, {"label": "allen", "at": 1.0, "files": {}})
        out = self.observe()
        self.assertTrue(out["cleared"])
        self.assertEqual(out["revoked"], ["allen"])
        self.assertIsNone((self.ds.read_json(self.ds.ACTIVE) or {}).get("label"))

    def test_signout_while_app_runs_is_report_only(self):
        write_log(self.profile, [record(batch(("put", KEY, account_json(UUID_A)))),
                                 record(batch(("del", KEY)))])
        self.make_stash("allen", UUID_A)
        self.ds.write_json(self.ds.ACTIVE, {"label": "allen", "at": 1.0, "files": {}})
        self.ds.app_running = lambda: True     # mid-login transients must not revoke anything
        out = self.observe()
        self.assertFalse(out["cleared"])
        self.assertEqual(out["revoked"], [])
        self.assertEqual((self.ds.read_json(self.ds.ACTIVE) or {}).get("label"), "allen")

    def test_untracked_signin_is_surfaced_and_displaced_stash_revoked(self):
        self.sign_in(UUID_B)                   # no stash holds B
        self.make_stash("allen", UUID_A)
        self.ds.write_json(self.ds.ACTIVE, {"label": "allen", "at": 1.0, "files": {}})
        out = self.observe()
        self.assertTrue(out["untracked"])
        self.assertIsNone(out["healed"])
        self.assertEqual(out["revoked"], ["allen"])

    def test_uuidless_recorded_stash_is_unconfirmed_not_untracked(self):
        # the live-fleet case: the record names a pre-identity stash that cannot confirm
        # nor deny — no alarm, no revoke
        self.sign_in(UUID_A)
        self.make_stash("allen", None)
        self.ds.write_json(self.ds.ACTIVE, {"label": "allen", "at": 1.0, "files": {}})
        out = self.observe()
        self.assertTrue(out["unconfirmed"])
        self.assertFalse(out["untracked"])
        self.assertEqual(out["revoked"], [])
        self.assertFalse((self.ds.stash_meta("allen") or {}).get("revoked"))

    def test_unknown_identity_changes_nothing(self):
        self.make_stash("allen", UUID_A)
        self.ds.write_json(self.ds.ACTIVE, {"label": "allen", "at": 1.0, "files": {}})
        out = self.observe()
        self.assertIsNone(out["observed"])
        self.assertIsNone(out["healed"])
        self.assertEqual(out["revoked"], [])

    def test_healing_steps_aside_while_a_switch_holds_the_lock(self):
        self.sign_in(UUID_A)
        self.make_stash("allen", UUID_A)
        self.ds.write_json(self.ds.ACTIVE, {"label": "allen-2", "at": 1.0, "files": {}})
        self.make_stash("allen-2", UUID_B)
        self.ds.write_json(self.ds.LOCK, {"pid": os.getpid(), "at": 1.0})   # a live pid
        out = self.observe()
        self.assertIsNone(out["healed"])
        self.assertEqual((self.ds.read_json(self.ds.ACTIVE) or {}).get("label"), "allen-2")

    def test_drift_is_reported_even_in_report_only_mode(self):
        self.sign_in(UUID_A)
        self.make_stash("allen", UUID_A)
        self.make_stash("allen-2", UUID_B)
        self.ds.write_json(self.ds.ACTIVE, {"label": "allen-2", "at": 1.0, "files": {}})
        out = self.observe(heal=False)
        self.assertEqual(out["drifted"], "allen")     # doctor can see what a heal would do
        self.assertIsNone(out["healed"])

    def test_open_journal_blocks_healing(self):
        # a half-applied switch looks exactly like a hand logout; repair owns that state
        self.sign_in(UUID_A)
        self.make_stash("allen", UUID_A)
        self.make_stash("allen-2", UUID_B)
        self.ds.write_json(self.ds.ACTIVE, {"label": "allen-2", "at": 1.0, "files": {}})
        self.ds.write_json(self.ds.JOURNAL, {"op": "x", "phase": "applying"})
        out = self.observe()
        self.assertEqual(out["drifted"], "allen")
        self.assertIsNone(out["healed"])
        self.assertEqual(out["revoked"], [])
        self.assertEqual((self.ds.read_json(self.ds.ACTIVE) or {}).get("label"), "allen-2")

    def test_table_residue_is_reported_but_never_healed(self):
        # compacted tables can hold an earlier account's residue; only log evidence acts
        write_store_file(self.profile, "000007.ldb", account_json(UUID_A))
        self.make_stash("allen", UUID_A)
        self.make_stash("allen-2", UUID_B)
        self.ds.write_json(self.ds.ACTIVE, {"label": "allen-2", "at": 1.0, "files": {}})
        out = self.observe()
        self.assertEqual(out["source"], "table")
        self.assertEqual(out["drifted"], "allen")
        self.assertIsNone(out["healed"])
        self.assertFalse((self.ds.stash_meta("allen-2") or {}).get("revoked"))

    def test_record_naming_a_deleted_stash_reads_untracked(self):
        self.sign_in(UUID_B)                          # no stash holds B
        self.ds.write_json(self.ds.ACTIVE, {"label": "gone", "at": 1.0, "files": {}})
        out = self.observe()
        self.assertTrue(out["untracked"])
        self.assertFalse(out["unconfirmed"])

    def test_same_account_duplicate_stash_is_not_revoked_on_heal(self):
        self.sign_in(UUID_A)
        self.make_stash("allen", UUID_A)
        self.make_stash("allen-copy", UUID_A)         # legacy duplicate of the same account
        self.ds.write_json(self.ds.ACTIVE, {"label": "allen-copy", "at": 1.0, "files": {}})
        out = self.observe()
        self.assertEqual(out["healed"], "allen")
        self.assertEqual(out["revoked"], [])          # its account is still signed in

    def test_recapture_clears_the_revoked_flag(self):
        self.sign_in(UUID_A)
        self.make_stash("allen", UUID_A)
        meta_path = os.path.join(self.state, "desktop-stashes", "allen", "manifest.json")
        m = self.ds.stash_meta("allen")
        m["revoked"] = True
        self.ds.write_json(meta_path, m)
        self.ds.write_json(self.ds.ACTIVE, {"label": "allen", "at": 1.0, "files": {}})
        with contextlib.redirect_stdout(io.StringIO()):
            self.ds.cmd_recapture("allen")
        self.assertFalse((self.ds.stash_meta("allen") or {}).get("revoked"))


if __name__ == "__main__":
    unittest.main()
