#!/usr/bin/env python3
"""desktop-switch — change which account the Claude desktop app is signed into, without logging in.

The app's account is a set of files under ~/Library/Application Support/Claude. Each account's
copy is stashed once and written back on the way in, so switching is a file swap with the app
quit — no logout, no login, and no verification email.

Device registration (ant-did, ant-device-registry.json) is deliberately NOT part of the swap:
it is machine-level, identical across accounts, and leaving it in place is what keeps the
device trusted. MCP config and app preferences stay put for the same reason — they are yours,
not the account's.

Recovery is the point of the design, not an afterthought:

  * Every switch copies what it is about to overwrite into a rollback directory first, so
    `undo` restores the exact bytes that were there rather than an approximation.
  * A journal records the operation before anything moves. A run interrupted at any point
    leaves a journal entry that `repair` (or the next command) rolls forward or back, so the
    profile is never left half one account and half another.
  * Files are staged inside the profile directory and moved into place with renames, which are
    atomic on the same filesystem, so no individual file is ever observed half-written.
  * After a swap the installed files are hashed against the stash manifest, so "it worked" is
    verified rather than assumed.

The worst realistic failure is a stash whose session was revoked server-side (logging out in
the app does that). The app then shows a login screen — recoverable by hand, and no worse than
today. So: switch by swapping, never log out.

  desktop-switch.py add                 START HERE — capture another account, safely
  desktop-switch.py signout-local       clear the session files without logging out
  desktop-switch.py status              which account is installed, plus stashes and health
  desktop-switch.py observe [--heal]    reconcile the record with the profile's own identity
                                        (hand sign-ins heal the record; a hand logout marks
                                        the displaced stash for a sign-in + recapture)
  desktop-switch.py capture <label>     stash the signed-in account's profile as <label>
  desktop-switch.py recapture [<label>] refresh a stash from the live profile (run after a
                                        forced sign-in; defaults to the installed account)
  desktop-switch.py list                stashes, with age and app version
  desktop-switch.py switch <label>      swap to that account (quits and reopens the app)
  desktop-switch.py switch <label> -n   dry run: show exactly what would move, change nothing
  desktop-switch.py switch <label> --no-launch   swap without reopening the app afterwards
  desktop-switch.py undo                put back what the last switch overwrote
  desktop-switch.py repair              finish or roll back an interrupted operation
  desktop-switch.py rename <old> <new>  fix a mislabeled stash (no re-capture needed)
  desktop-switch.py forget <label>
"""
import os, re, sys, json, time, shutil, hashlib, subprocess

PROFILE = os.path.expanduser(os.environ.get("CU_DESKTOP_PROFILE")
                             or "~/Library/Application Support/Claude")
STATE    = os.path.expanduser(os.environ.get("CU_DESKTOP_STATE") or "~/.claude-usage")
STASHES  = os.path.join(STATE, "desktop-stashes")
ROLLBACK = os.path.join(STATE, "desktop-rollbacks")
# Displaced copies of stashes that were refreshed in place (_write_stash's keep_aside). Not under
# ROLLBACK: `undo` restores the newest rollback wholesale, and these are not profile rollbacks.
ASIDES   = os.path.join(STATE, "desktop-stash-asides")
JOURNAL  = os.path.join(STATE, "desktop-journal.json")
ACTIVE   = os.path.join(STATE, "desktop-active.json")
LOCK     = os.path.join(STATE, "desktop-switch.lock")

APP_EXEC   = "/Applications/Claude.app/Contents/MacOS/Claude"
APP_BUNDLE = "com.anthropic.claudefordesktop"

# A stash survives short parking, not long: since mid-Aug 2026 the server invalidates sessions
# left idle for roughly a day, so a stash parked past this age usually comes back to the
# "sign in again" banner even though its cookies are unexpired.
STALE_DAYS = 1.0

# The account-bearing stores: a sign-in rewrites these and leaves the rest of the profile alone.
# Directories are swapped whole. tools/profile-probe.py re-derives this set if an app update
# moves the account somewhere else.
IDENTITY = ["Cookies", "Cookies-journal", "Local Storage", "Session Storage",
            "WebStorage", "IndexedDB"]

# These move when the account does, but they are machine- or user-level rather than
# account-level, so swapping them would move settings that should follow the person:
#   ant-did, ant-device-registry.json   device trust — swapping these invites re-verification
#   claude_desktop_config.json          your MCP servers
#   plan-usage-history.json             the app's own usage record
# If a swap ever lands on a login screen, buddy-tokens.json and config.json are the next
# candidates to add above: both move when the account does, but also on ordinary runs, so
# neither could be attributed to the account with certainty.

def app_running():
    """True while the app's main process is alive.

    Matched on the full executable path via `ps`: pgrep does not report this process at all
    (it sees only the "Claude Helper" children), so a pgrep-based check would wave a swap
    through against a live profile and corrupt it.
    """
    if os.environ.get("CU_SKIP_APP_CONTROL"):
        return False
    r = subprocess.run(["ps", "-Ao", "comm="], capture_output=True, text=True)
    return any(line.strip() == APP_EXEC for line in r.stdout.splitlines())

def hosted_by_app():
    """True when this process would die with the desktop app, i.e. it is running inside it.

    Answered by walking our own parent chain, because the question is about this process and
    nothing else. Environment markers such as CLAUDE_CODE_ENTRYPOINT cannot answer it: they are
    inherited by every descendant, so a menu-bar app launched from a session inside the app
    carries the marker while belonging to launchd — it outlives the app and must be free to
    switch.
    """
    r = subprocess.run(["ps", "-Ao", "pid=,ppid=,comm="], capture_output=True, text=True)
    parent, comm = {}, {}
    for line in r.stdout.splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) < 3:
            continue
        try:
            pid, ppid = int(parts[0]), int(parts[1])
        except ValueError:
            continue
        parent[pid], comm[pid] = ppid, parts[2]
    app_support = os.path.expanduser("~/Library/Application Support/Claude/")
    pid, hops = os.getpid(), 0
    while pid > 1 and hops < 40:
        c = comm.get(pid, "")
        if c == APP_EXEC or c.startswith("/Applications/Claude.app/") or c.startswith(app_support):
            return True
        pid = parent.get(pid, 0)
        hops += 1
    return False

def quit_app(timeout=45):
    """Ask the app to quit and wait for it to be gone. Graceful, because the stores being
    swapped are flushed from memory on the way out; killing it would strand that state."""
    if os.environ.get("CU_SKIP_APP_CONTROL"):
        return True
    if not app_running():
        return True
    subprocess.run(["osascript", "-e", f'quit app id "{APP_BUNDLE}"'], capture_output=True)
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not app_running():
            time.sleep(1.5)
            return True
        time.sleep(0.5)
    return False

def launch_app(timeout=45):
    if os.environ.get("CU_SKIP_APP_CONTROL"):
        return True
    subprocess.run(["open", "-b", APP_BUNDLE], capture_output=True)
    deadline = time.time() + timeout
    while time.time() < deadline:
        if app_running():
            return True
        time.sleep(0.5)
    return False

# ---- hashing / manifests ----------------------------------------------------

def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()

def walk_identity(root):
    """(relative path, absolute path) for every regular file in the identity set under `root`."""
    for item in IDENTITY:
        full = os.path.join(root, item)
        if os.path.isfile(full):
            yield item, full
        elif os.path.isdir(full):
            for dirpath, dirnames, filenames in os.walk(full):
                dirnames[:] = [d for d in dirnames if not os.path.islink(os.path.join(dirpath, d))]
                for name in filenames:
                    p = os.path.join(dirpath, name)
                    if os.path.islink(p):
                        continue
                    yield os.path.relpath(p, root), p

def manifest(root):
    out = {}
    for rel, full in walk_identity(root):
        try:
            out[rel] = sha256(full)
        except OSError:
            out[rel] = "unreadable"
    return out

def app_version():
    try:
        import plistlib
        with open("/Applications/Claude.app/Contents/Info.plist", "rb") as f:
            return plistlib.load(f).get("CFBundleShortVersionString")
    except Exception:
        return None

def copy_identity(src_root, dst_root):
    """Copy the identity set from one tree to another, preserving structure. Returns (files, bytes)."""
    n = total = 0
    for rel, full in walk_identity(src_root):
        dst = os.path.join(dst_root, rel)
        os.makedirs(os.path.dirname(dst), mode=0o700, exist_ok=True)
        shutil.copy2(full, dst)
        n += 1
        total += os.path.getsize(full)
    return n, total

# ---- whose profile is this ---------------------------------------------------
# The app records the signed-in account's uuid in claude.ai's Local Storage, and that uuid is
# the same one the usage API reports for the account. Reading it turns "which account is
# installed" from a record this tool keeps into a fact it can check — which is what makes
# refreshing a drifted profile into the right stash safe. The storage location is the app's
# private business, so None (not found) must always be a survivable answer; profile-probe.py
# re-derives the location if an update moves it.

# localStorage values may be stored as UTF-16LE, which puts a NUL after each of what are
# otherwise ASCII JSON bytes. One pattern with an optional NUL after every literal byte
# matches both encodings in a single pass over the raw buffer — and, unlike scanning a
# NUL-stripped copy, it cannot assemble a "match" out of fragments that sat on opposite
# sides of a run of padding NULs between adjacent leveldb entries.
def _either_width(s):
    return b"".join(re.escape(bytes([c])) + b"\x00?" for c in s.encode())

_HEX = b"[0-9a-f]\x00?"
_UUID_BODY = (_HEX * 8 + _either_width("-") + _HEX * 4 + _either_width("-") + _HEX * 4
              + _either_width("-") + _HEX * 4 + _either_width("-") + _HEX * 12)
UUID_RE = re.compile(_either_width('"accountUuid"') + b"[\\s\x00]*" + _either_width(":")
                     + b"[\\s\x00]*" + _either_width('"') + b"(" + _UUID_BODY + b")"
                     + _either_width('"'))

def _scan_for_uuid(data, found):
    for m in UUID_RE.finditer(data):
        found.append(m.group(1).replace(b"\x00", b"").decode())

def _log_records(data):
    """Reassembled record payloads from a leveldb write-ahead log.

    The log is 32KB blocks of [crc:4][length:2][type:1][payload] records, and one logical
    record may be split FIRST/MIDDLE/.../LAST across blocks — a value that straddles a block
    boundary is invisible to a flat scan of the file, which is why this parser exists.
    Corruption resyncs at the next block boundary: better to miss one record than to error
    out of reading identity at all.
    """
    BLOCK = 32768
    buf, pos = b"", 0
    while pos + 7 <= len(data):
        room = BLOCK - (pos % BLOCK)
        if room < 7:
            pos += room                      # zero-padded block trailer
            continue
        length = int.from_bytes(data[pos + 4:pos + 6], "little")
        rtype = data[pos + 6]
        if rtype not in (1, 2, 3, 4) or 7 + length > room:
            pos += room                      # trailer or corruption: resync
            buf = b""                        # a pending fragment must not survive the resync —
            continue                         # gluing it to a later LAST would forge a record
        payload = data[pos + 7:pos + 7 + length]
        pos += 7 + length
        if rtype == 1:                       # FULL
            yield payload; buf = b""
        elif rtype == 2:                     # FIRST
            buf = payload
        elif rtype == 3:                     # MIDDLE
            buf += payload
        else:                                # LAST
            yield buf + payload; buf = b""

# profile_identity's third answer: the log affirmatively shows the session was signed out.
# Truthy on purpose — every consumer must decide what a dead session means for it rather
# than fall through a None check into "unknown".
SIGNED_OUT = "signed-out"

def _is_uuid(u):
    return bool(u) and u != SIGNED_OUT

def _varint(data, pos):
    shift = val = 0
    while pos < len(data) and shift < 35:
        b = data[pos]; pos += 1
        val |= (b & 0x7F) << shift
        if not b & 0x80:
            return val, pos
        shift += 7
    raise ValueError("bad varint")

def _batch_ops(payload):
    """The (op, key, value) entries of a leveldb WriteBatch record, in write order.

    [sequence:8][count:4] then per entry: tag 0x01 = put (varint klen, key, varint vlen,
    value), tag 0x00 = deletion (varint klen, key). Strict — anything malformed raises
    ValueError and the caller falls back to a raw value scan of the record."""
    if len(payload) < 12:
        raise ValueError("short batch")
    count = int.from_bytes(payload[8:12], "little")
    pos, out = 12, []
    for _ in range(count):
        if pos >= len(payload):
            raise ValueError("truncated batch")
        tag = payload[pos]; pos += 1
        klen, pos = _varint(payload, pos)
        key = payload[pos:pos + klen]; pos += klen
        if len(key) != klen:
            raise ValueError("truncated key")
        if tag == 1:
            vlen, pos = _varint(payload, pos)
            val = payload[pos:pos + vlen]; pos += vlen
            if len(val) != vlen:
                raise ValueError("truncated value")
            out.append(("put", bytes(key), bytes(val)))
        elif tag == 0:
            out.append(("del", bytes(key), b""))
        else:
            raise ValueError("unknown tag")
    if pos != len(payload):
        raise ValueError("trailing bytes")
    return out

def _log_identity(d, logs):
    """uuid | SIGNED_OUT | None from the write-ahead logs, deletion-aware.

    Records parse as write batches, so a sign-out is visible as what it is: a deletion of
    (or an accountless overwrite to) a key that previously carried the account — without
    this, a revoked session keeps reading as its last account and dead bytes masquerade
    as a sign-in. A record that isn't a parseable batch degrades to the raw value scan,
    which can only ever say "uuid" — never a false sign-out."""
    identity_keys, last = set(), None
    for _, name in sorted(logs):
        try:
            with open(os.path.join(d, name), "rb") as f:
                data = f.read()
        except OSError:
            continue
        for rec in _log_records(data):
            try:
                ops = _batch_ops(rec)
            except ValueError:
                found = []
                _scan_for_uuid(rec, found)
                if found:
                    last = found[-1]
                continue
            for op, key, val in ops:
                found = []
                _scan_for_uuid(val, found)
                if found:
                    identity_keys.add(key)
                    last = found[-1]
                elif key in identity_keys:
                    last = SIGNED_OUT        # tombstoned, or rewritten without an account
    return last

def profile_identity(root, with_source=False):
    """The signed-in account's uuid in a profile tree (or a stash's files/ tree),
    SIGNED_OUT when the log shows the session was ended, or None (unknown). with_source
    returns (answer, "log"|"table"|None) instead — a caller that WRITES on the answer
    needs to know its evidence class, because only the logs are trustworthy enough to
    act on (see below).

    Meaningful only at rest — a running app holds newer state in memory. Evidence quality
    decides the answer, not file numbers alone: every write lands in the write-ahead .log
    before anything else and the log format parses completely, so the logs' verdict —
    sign-in or sign-out — wins outright. (File numbers cannot be compared across kinds:
    the live log is numbered before tables flushed later.) Compacted .ldb tables are
    consulted only when the logs are silent; their data blocks are usually compressed and
    deletions are invisible to a raw scan, so a table answer can be an earlier account's
    residue rather than the current sign-in — a possible stale answer accepted over a
    useless None. The pre-install identity check in cmd_switch and the aside copies are
    the backstops for that case. Only NNNN.log and NNNN.ldb can carry localStorage
    values; MANIFEST/CURRENT/LOG are leveldb bookkeeping.
    """
    d = os.path.join(root, "Local Storage", "leveldb")
    if not os.path.isdir(d):
        return (None, None) if with_source else None
    logs, tables = [], []
    for name in os.listdir(d):
        m = re.fullmatch(r"(\d+)\.(log|ldb)", name)
        if m:
            (logs if m.group(2) == "log" else tables).append((int(m.group(1)), name))
    ans = _log_identity(d, logs)
    if ans:
        return (ans, "log") if with_source else ans
    found = []
    for _, name in sorted(tables):
        try:
            with open(os.path.join(d, name), "rb") as f:
                _scan_for_uuid(f.read(), found)
        except OSError:
            continue
    uid = found[-1] if found else None
    return (uid, "table" if uid else None) if with_source else uid

# ---- small json helpers -----------------------------------------------------

def read_json(path, default=None):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default

def write_json(path, obj):
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=2)
    os.replace(tmp, path)

# ---- lock -------------------------------------------------------------------

class Lock:
    """Keeps a menu click and an automatic switch from interleaving. A lock whose process is
    gone is stale by definition — the operation it guarded is finished or crashed, and the
    journal, not the lock, is what records unfinished work."""
    def __enter__(self):
        os.makedirs(STATE, mode=0o700, exist_ok=True)
        cur = read_json(LOCK)
        if cur and _pid_alive(cur.get("pid")):
            sys.exit(f"another switch is running (pid {cur['pid']}) — wait for it to finish")
        write_json(LOCK, {"pid": os.getpid(), "at": time.time()})
        return self
    def __exit__(self, *exc):
        try:
            os.remove(LOCK)
        except OSError:
            pass

def _pid_alive(pid):
    if not isinstance(pid, int):
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False

# ---- stashes ----------------------------------------------------------------

def stash_dir(label):
    return os.path.join(STASHES, label)

def stash_meta(label):
    return read_json(os.path.join(stash_dir(label), "manifest.json"))

def _stash_uuid(label):
    """The account uuid a stash holds: the manifest's record when present, else read from
    the stash's own files.

    Never writes. This runs from status/capture paths that hold no lock, so a write here
    could race a switch mid-rename — and a failed manifest read must not become a rewrite.
    Manifests gain their account_uuid only at capture/_write_stash time; until then a
    uuid-less stash pays a rescan of its few MB per call.
    """
    m = stash_meta(label) or {}
    if m.get("account_uuid"):
        return m["account_uuid"]
    if m.get("identity_unreadable"):
        return None      # scanned before, nothing readable — don't pay the rescan every call
    u = profile_identity(os.path.join(stash_dir(label), "files"))
    return u if _is_uuid(u) else None    # a signed-out capture is attributable to no account

def _uuid_owners(uid):
    """The stashes holding this account, best candidate first: a christened label over an
    unclaimed one, then name order."""
    if not _is_uuid(uid):
        return []
    return sorted((l for l in list_stashes() if _stash_uuid(l) == uid),
                  key=lambda l: (bool((stash_meta(l) or {}).get("unclaimed")), l))

def list_stashes():
    if not os.path.isdir(STASHES):
        return []
    # Dot-names are excluded so _write_stash's build directory never shows up as a stash.
    return sorted(d for d in os.listdir(STASHES)
                  if not d.startswith(".") and os.path.isdir(stash_dir(d)))

def _write_stash(label, uid=None, keep_aside=False, unclaimed=False):
    """Replace <label>'s stash with the live profile's identity files.

    The new copy is built complete in a hidden sibling directory and swapped into place with
    renames, so an interruption at any point leaves the old stash or the new one under the
    label — never a half-written directory. The manifest is hashed from the copied bytes
    rather than the profile, so the record can never disagree with what the stash holds (the
    post-swap verification in _apply compares against it). uid is the account the bytes
    provably hold — None when identity couldn't be read, never a guess — so a manifest's
    account_uuid can be trusted wherever it is present. keep_aside moves the displaced copy
    into ASIDES (with _aside_stash's retention) instead of deleting it; unclaimed marks a
    preserved sign-in still waiting for the user to name it.

    Returns (files copied, bytes copied, manifest of the copy).
    """
    dest = stash_dir(label)
    tmp = os.path.join(STASHES, f".building-{label}")
    if os.path.exists(tmp):
        shutil.rmtree(tmp)
    os.makedirs(tmp, mode=0o700, exist_ok=True)
    n, total = copy_identity(PROFILE, os.path.join(tmp, "files"))
    files = manifest(os.path.join(tmp, "files"))
    meta = {"label": label, "captured_at": time.time(), "app_version": app_version(),
            "account_uuid": uid, "files": files}
    # The captured bytes get their own verdict at birth, so a dead capture is knowable
    # BEFORE a click tries to install it — the switch-time refusal must be the last net,
    # not the first signal. Log-sourced identity also backfills a uuid the caller didn't
    # have; table residue backfills nothing (it can name an earlier account).
    verdict, source = profile_identity(os.path.join(tmp, "files"), with_source=True)
    meta["bytes_checked"] = True               # the verdict below is recorded; never re-scan
    if verdict == SIGNED_OUT:
        meta["revoked"] = True
        meta["revoked_reason"] = "captured-signed-out"
    elif meta["account_uuid"] is None and _is_uuid(verdict) and source == "log":
        meta["account_uuid"] = verdict
    elif verdict is None:
        meta["identity_unreadable"] = True     # nothing readable in there
    if unclaimed:
        meta["unclaimed"] = True
    write_json(os.path.join(tmp, "manifest.json"), meta)
    if os.path.exists(dest):
        if keep_aside:
            _aside_stash(label)
        else:
            shutil.rmtree(dest)
    os.rename(tmp, dest)
    return n, total, files

def cmd_capture(label):
    if not label:
        sys.exit("usage: desktop-switch.py capture <label>")
    if label.startswith("unclaimed-"):
        # the quarantine namespace: only `rename` moves a stash out of it, so a capture into
        # it would mint a christened stash under a name that promises the opposite
        sys.exit("labels starting with 'unclaimed-' are reserved for preserved sign-ins —\n"
                 "pick a real name, or `rename` the unclaimed stash you mean")
    if app_running():
        sys.exit("Claude.app is running — quit it first.\n"
                 "Its session stores are flushed to disk on quit, so a capture now would stash\n"
                 "a partial profile that fails when installed later.")
    if next(walk_identity(PROFILE), None) is None:
        sys.exit(f"nothing to capture — no identity files found under {PROFILE}")
    uid = profile_identity(PROFILE)
    if uid == SIGNED_OUT:
        sys.exit("the profile's storage shows a sign-out — there is no live session here to\n"
                 "capture. Sign into the account in the app, quit it, then capture.")
    others = [l for l in _uuid_owners(uid) if l != label]
    if others:
        # Downstream, owner lookups assume one stash per account; a second label would make
        # every refresh and pairing pick between them arbitrarily.
        sys.exit(f"this profile is signed into the account already stashed as {others[0]!r}.\n"
                 f"Refresh that stash instead:   desktop-switch.py capture {others[0]}\n"
                 f"or fix its label first:       desktop-switch.py rename {others[0]} {label}")
    replacing = label in list_stashes()
    n, total, files = _write_stash(label, uid, keep_aside=replacing)
    _set_active(label, files)
    print(f"captured {n} files ({total/1e6:.1f} MB) as {label!r}"
          + (" — the previous copy is kept aside" if replacing else ""))

def cmd_recapture(label=""):
    """Refresh an existing stash from the live profile, keeping the displaced copy aside.

    This is the recovery step after a switch lands on the sign-in banner: the sign-in the user
    just did lives only in the profile, while the stash still holds the invalidated session —
    left alone, every future switch to that label reinstalls the dead copy and demands another
    sign-in. Recapturing puts the fresh session in the stash, so one sign-in per long parking
    is the whole cost instead of one per revisit.

    The stash to refresh is the one that provably holds this profile's account — identity is
    read from the profile's own files, and a label naming any other stash is refused rather
    than let one account's files overwrite another's capture. When the files don't yield an
    identity (older app layouts), the ACTIVE record — the account this tool last installed —
    stands in, with the same refusals. A hand sign-in as a different account still recaptures
    under the provable owner; `rename` fixes the name afterwards, and the aside copy keeps
    the history either way.
    """
    if app_running():
        sys.exit("Claude.app is running — quit it first.\n"
                 "Its session stores are flushed to disk on quit, so a recapture now would stash\n"
                 "a partial profile that fails when installed later.")
    if _journal():
        sys.exit("an earlier operation did not finish — run `desktop-switch.py repair` first")
    if next(walk_identity(PROFILE), None) is None:
        sys.exit(f"nothing to recapture — no identity files found under {PROFILE}")
    uid = profile_identity(PROFILE)
    if uid == SIGNED_OUT:
        sys.exit("the profile's storage shows a sign-out — recapturing it would replace a\n"
                 "stash's session with a dead one. Sign into the account in the app, quit\n"
                 "it, then recapture.")
    owner = next(iter(_uuid_owners(uid)), None)   # the stash provably holding these bytes
    claimed = (read_json(ACTIVE) or {}).get("label")
    if not owner and not claimed:
        # No evidence at all of whose bytes these are. An explicit label is a name, not
        # evidence — accepting it here would overwrite a good stash on say-so alone.
        sys.exit("neither the profile's files nor this tool's records say which account is\n"
                 "signed in here (signout-local, undo, and hand sign-ins clear the record),\n"
                 "so recapture cannot attribute it to a stash safely. If you know which\n"
                 "account it is, quit the app and run:\n"
                 "  desktop-switch.py capture <label>   (the label's old stash is kept aside)")
    label = label or owner or claimed
    if label not in list_stashes():
        sys.exit(f"unknown stash: {label}\nknown: {', '.join(list_stashes()) or '(none)'}\n"
                 "recapture refreshes an existing stash — use `capture` for a new label")
    if (stash_meta(label) or {}).get("unclaimed"):
        sys.exit(f"{label!r} is a preserved sign-in still waiting for a name — name it first:\n"
                 f"  desktop-switch.py rename {label} <label>")
    if owner and owner != label:
        sys.exit(f"the profile here is signed into the account stashed as {owner!r}, not\n"
                 f"{label!r} — refusing to overwrite {label!r}'s stash with it. Run\n"
                 f"`recapture {owner}`, then `rename` if that label is wrong.")
    if not owner and claimed and claimed != label:
        sys.exit(f"the profile here was last installed as {claimed!r}, not {label!r} — refusing\n"
                 f"to overwrite {label!r}'s stash with it. Run `recapture {claimed}`, then\n"
                 f"`rename` if that label is wrong.")
    if uid and not owner and _stash_uuid(label):
        # no stash holds these bytes' account, and the target stash has its own identity —
        # which therefore disagrees; overwriting would replace a known account's capture
        # with a different account's files
        sys.exit(f"the profile here holds a different account than {label!r}'s stash does —\n"
                 f"refusing to overwrite it. If this sign-in should become a new stash, run:\n"
                 f"  desktop-switch.py capture <label>")
    with Lock():
        n, total, files = _write_stash(label, uid, keep_aside=True)
        _set_active(label, files)
    print(f"recaptured {label!r} from the live profile ({n} files, {total/1e6:.1f} MB)")
    print("the previous copy is kept aside; switching back to this account is free again")

def cmd_list():
    stashes = list_stashes()
    if not stashes:
        print("no stashes yet — quit the app and run:  desktop-switch.py capture <label>")
        return
    active = (read_json(ACTIVE) or {}).get("label")
    for label in stashes:
        m = stash_meta(label) or {}
        age = (time.time() - m.get("captured_at", 0)) / 86400
        mark = "▶" if label == active else " "
        # The active row is the account in use, not a parked stash — no sign-in prediction
        # there. Its saved copy still ages, so mark that plainly and leave the advice to
        # doctor, which knows whether a switch away would refresh the copy by itself.
        if age <= STALE_DAYS:
            note = ""
        elif label == active:
            note = f"  (saved copy >{STALE_DAYS:g}d old)"
        else:
            note = f"  (parked >{STALE_DAYS:g}d — expect a sign-in)"
        print(f" {mark} {label:<20} {len(m.get('files') or {}):>5} files  "
              f"captured {age:.1f}d ago  app v{m.get('app_version')}{note}")

def cmd_forget(label):
    if label not in list_stashes():
        sys.exit(f"unknown stash: {label}")
    shutil.rmtree(stash_dir(label))
    print(f"forgot {label}")

def cmd_rename(old, new):
    """Fix a mislabeled stash. The label is bookkeeping — the files inside decide which account
    the app comes up as — so a wrong name never needs a re-capture, just a rename."""
    if old not in list_stashes():
        sys.exit(f"unknown stash: {old}\nknown: {', '.join(list_stashes()) or '(none)'}")
    if not new or "/" in new or new.startswith("."):
        sys.exit("the new name must be a short label, no slashes")
    if new in list_stashes():
        sys.exit(f"a stash named {new!r} already exists — forget it first if it is wrong")
    os.rename(stash_dir(old), stash_dir(new))
    meta = stash_meta(new) or {}
    meta["label"] = new
    meta.pop("unclaimed", None)   # being named is exactly what an unclaimed stash was waiting for
    write_json(os.path.join(stash_dir(new), "manifest.json"), meta)
    active = read_json(ACTIVE) or {}
    if active.get("label") == old:      # the record of what is installed follows the rename
        active["label"] = new
        write_json(ACTIVE, active)
    print(f"renamed {old!r} -> {new!r}")

# ---- which account is installed --------------------------------------------

def _set_active(label, files=None):
    # files: a manifest the caller just computed for these same bytes, to skip re-hashing.
    write_json(ACTIVE, {"label": label, "at": time.time(),
                        "files": manifest(PROFILE) if files is None else files})

def _try_lock():
    """The Lock's acquisition without its exit: observe heals opportunistically, and a busy
    lock just means report-only this round."""
    cur = read_json(LOCK)
    if cur and _pid_alive(cur.get("pid")):
        return False
    write_json(LOCK, {"pid": os.getpid(), "at": time.time()})
    return True

def _release_try_lock():
    try:
        os.remove(LOCK)
    except OSError:
        pass

def _mark_revoked(label, reason="hand-logout"):
    """Stamp a stash as holding a dead session — one that cannot come up signed in. The flag
    lives in the manifest so any recapture — which rewrites the manifest — clears it; the
    reason ("hand-logout" or "captured-signed-out") only picks the wording surfaces use."""
    if label not in list_stashes():
        return False
    m = stash_meta(label) or {}
    if m.get("revoked"):
        return False
    m["revoked"] = True
    m["revoked_reason"] = reason
    write_json(os.path.join(stash_dir(label), "manifest.json"), m)
    return True

def cmd_observe(heal=False):
    """Reconcile the ACTIVE record with what the profile actually holds, and say so as JSON.

    The record tracks the switches THIS tool makes; a hand sign-in or logout inside the app
    changes the account without telling it, after which every surface points at the wrong
    card and the displaced account's stash silently holds a revoked session (an in-app
    account change starts with a logout, which kills the parked session server-side).
    Observing closes that gap: the profile's own storage says who is really signed in.

    Report-only by default; --heal also repairs: point the record at the stash that provably
    holds the observed account, mark a displaced stash revoked, and on an at-rest sign-out
    clear the record. Healing acts only on evidence that cannot mislead:

      * log-sourced identity — a compacted table's answer can be an earlier account's
        residue, so table answers are reported but never acted on;
      * a sign-out only with the app closed — mid-login the log passes through a
        signed-out state, and a transient must not revoke anything;
      * no open journal — a half-applied switch looks exactly like a hand logout, and
        `repair`, not healing, owns that state;
      * under the Lock, with the record re-read there — a click switch must not
        interleave with the writes; a busy lock means report-only this round.
    """
    uid, source = profile_identity(PROFILE, with_source=True)
    claimed = (read_json(ACTIVE) or {}).get("label")
    stash_ids = {l: _stash_uuid(l) for l in list_stashes()}   # one scan per stash, reused
    owner = None
    if _is_uuid(uid):
        owned = sorted((l for l, u in stash_ids.items() if u == uid),
                       key=lambda l: (bool((stash_meta(l) or {}).get("unclaimed")), l))
        owner = owned[0] if owned else None
    out = {"observed": uid, "signed_out": uid == SIGNED_OUT, "source": source,
           "recorded": claimed,
           # drift is reported in BOTH modes: a report-only consumer (doctor) must be able
           # to see what a heal would do, or it asserts the stale record as truth
           "drifted": owner if owner and owner != claimed else None,
           "healed": None, "cleared": False, "revoked": [],
           "untracked": False, "unconfirmed": False}
    if _is_uuid(uid) and not owner:
        if claimed and stash_ids.get(claimed) is None and claimed in stash_ids:
            # the recorded stash exists but has no identity of its own to contradict the
            # reading (a pre-identity capture) — the record may well be right, so no alarm
            # and no action; the next recapture backfills the identity and settles it
            out["unconfirmed"] = True
        else:
            out["untracked"] = True
    actionable = ((uid == SIGNED_OUT and not app_running())
                  or (_is_uuid(uid) and source == "log"
                      and (out["drifted"] or out["untracked"])))
    # Stashes that never got a verdict at capture (pre-identity captures) get one here, once:
    # a dead capture must warn from the manifest like version_mismatch does, not first from a
    # refused click. The outcome is always written — uuid, revoked, or identity_unreadable —
    # so no stash is scanned twice.
    backfill = [l for l in stash_ids
                if not (stash_meta(l) or {}).get("bytes_checked")
                and not (stash_meta(l) or {}).get("revoked")]
    if not heal or not (actionable or backfill) or _journal():
        print(json.dumps(out)); return
    if not _try_lock():
        print(json.dumps(out)); return
    try:
        for l in backfill:
            v, s = profile_identity(os.path.join(stash_dir(l), "files"), with_source=True)
            if v == SIGNED_OUT and _mark_revoked(l, reason="captured-signed-out"):
                out["revoked"].append(l)
            m = stash_meta(l) or {}
            m["bytes_checked"] = True
            if _is_uuid(v) and s == "log" and not m.get("account_uuid"):
                m["account_uuid"] = v
            elif v is None:
                m["identity_unreadable"] = True
            write_json(os.path.join(stash_dir(l), "manifest.json"), m)
        if not actionable:
            print(json.dumps(out)); return
        if claimed != (read_json(ACTIVE) or {}).get("label"):
            print(json.dumps(out)); return   # a switch beat us to it; its record wins
        if uid == SIGNED_OUT:
            if claimed:
                if _mark_revoked(claimed):
                    out["revoked"].append(claimed)
                _set_active(None, files={})
                out["cleared"] = True
        elif out["drifted"]:
            # a same-account duplicate stash must not be revoked: only a stash that
            # provably holds a DIFFERENT account was displaced by the hand sign-in
            if claimed and stash_ids.get(claimed) not in (None, uid) \
                    and _mark_revoked(claimed):
                out["revoked"].append(claimed)
            _set_active(out["drifted"])
            out["healed"] = out["drifted"]
        elif out["untracked"] and claimed and stash_ids.get(claimed):
            # the recorded stash provably holds a different account than the profile
            # (no stash holds the observed uid), so a hand sign-in displaced it; its
            # session ended with the in-app logout that preceded that sign-in
            if _mark_revoked(claimed):
                out["revoked"].append(claimed)
    finally:
        _release_try_lock()
    print(json.dumps(out))
def identify():
    """(label, note) for the profile on disk: which stash it matches, if any.

    Recomputed from file hashes rather than trusted from our own record, so an account
    switched by hand in the app is reported as a mismatch instead of a stale certainty.
    """
    if not os.path.isdir(PROFILE):
        return None, "no profile directory"
    cur = manifest(PROFILE)
    if not cur:
        return None, "no identity files in the profile"
    for label in list_stashes():
        m = stash_meta(label) or {}
        if m.get("files") == cur:
            return label, None
    if _journal():
        # An open journal already explains the mismatch; blaming the app would send the reader
        # off to re-capture a profile that is mid-swap and about to be repaired.
        return None, "a switch is unfinished — run `desktop-switch.py repair`"
    active = read_json(ACTIVE)
    claimed = (active or {}).get("label")
    if claimed:
        # Running the app rewrites these stores constantly, so an exact match only holds right
        # after a swap. Drift is the normal case and says nothing about which account is signed
        # in — report what was last installed, and be clear it is a record rather than a reading.
        return claimed, "from this tool's record; the files have drifted since, which is normal"
    if active is not None:
        return None, "record cleared (signout-local or undo) — a switch or capture re-establishes it"
    return None, "not captured yet"

# ---- the switch -------------------------------------------------------------

def _journal():
    return read_json(JOURNAL)

def _staging(op_id):
    # Inside the profile so a move into place is a same-filesystem rename, which is atomic.
    return os.path.join(PROFILE, f".cu-staging-{op_id}")

def cmd_switch(label, dry_run=False, no_launch=False):
    if not label:
        sys.exit("usage: desktop-switch.py switch <label> [-n|--no-launch]")
    if label not in list_stashes():
        sys.exit(f"unknown stash: {label}\nknown: {', '.join(list_stashes()) or '(none)'}")
    if _journal():
        sys.exit("an earlier operation did not finish — run `desktop-switch.py repair` first")
    meta = stash_meta(label) or {}
    src = os.path.join(stash_dir(label), "files")
    age_days = (((time.time() - meta["captured_at"]) / 86400)
                if meta.get("captured_at") is not None else None)

    cur_ver = app_version()
    # Both warnings print for dry runs too — the preview's job is to predict the real run, and
    # these are the two lines that predict a switch landing on a sign-in screen.
    # claude-usage.py's _switch_verdict strips warnings from failure text by this exact
    # shape — the "warning:" prefix and space-indented continuation lines — so keep it.
    if meta.get("app_version") and cur_ver and meta["app_version"] != cur_ver:
        # stderr, not stdout: wrappers keep stdout for results. Both versions must be
        # readable: with either unknown there is no basis to warn, and the doctor/bar
        # surfaces (which share this predicate) stay silent on unknown too.
        print(f"warning: {label!r} was captured under app v{meta['app_version']}, now v{cur_ver}.\n"
              f"         If the app has migrated its stores since, this may land on a login screen.",
              file=sys.stderr)
    if age_days is not None and age_days > STALE_DAYS:
        print(f"warning: {label!r} has been parked {age_days:.1f}d, and the server invalidates\n"
              f"         sessions idle for about a day — expect the sign-in banner. Signing in\n"
              f"         there is safe (device trust holds). Afterwards, refresh the stash:\n"
              f"         desktop-switch.py recapture",
              file=sys.stderr)

    if dry_run:
        cur_label, note = identify()
        age = f", {age_days:.1f}d ago" if age_days is not None else ""
        print(f"\nwould switch to {label!r} (captured under app v{meta.get('app_version')}{age})")
        print(f"currently installed: {cur_label or 'unknown'}{' — ' + note if note else ''}")
        print(f"app version now:     {cur_ver}")
        files = list(walk_identity(src))
        size = sum(os.path.getsize(p) for _, p in files)
        print(f"\nwould replace {len(files)} files ({size/1e6:.1f} MB) under:")
        for item in IDENTITY:
            n = sum(1 for rel, _ in files if rel == item or rel.startswith(item + os.sep))
            if n:
                print(f"  {item:<20} {n:>5} files")
        print("\nwould leave untouched: device registration, MCP config, app preferences,")
        print("                       usage history, and everything else in the profile.")
        print("\nnothing was changed (dry run).\n")
        return

    if hosted_by_app() and not os.environ.get("CU_SKIP_APP_CONTROL"):
        sys.exit("This is running inside the desktop app, and switching quits that app — which\n"
                 "would kill this process mid-swap. Run it from Terminal.")

    # Byte-verification later proves the install copied faithfully; only this proves the
    # bytes are the account the manifest claims. A disagreement means the label or the
    # manifest is lying, and installing it would sign the app into a different account
    # than every surface reports.
    files_uid = profile_identity(src)
    if files_uid == SIGNED_OUT:
        sys.exit(f"the stash {label!r} holds a signed-out session — installing it can only\n"
                 f"land on the sign-in banner. Sign into the account in the app, quit it,\n"
                 f"and `recapture {label}` — or `forget {label}`.")
    if files_uid and meta.get("account_uuid") and files_uid != meta["account_uuid"]:
        # rename can't fix this — it changes neither operand. Name the real ways out.
        sys.exit(f"the stash {label!r} records one account but its files hold another\n"
                 f"(…{files_uid[-12:]}) — its manifest can't be trusted. Either sign into that\n"
                 f"account in the app and `recapture {label}` to rebuild it consistently, or\n"
                 f"`forget {label}` and capture it fresh.")

    with Lock():
        if no_launch:
            # The unattended caller decided while the app was closed; if it opened since, the
            # decision is void — an unattended swap must never quit a session out from under
            # the user, so this mode refuses rather than quits.
            if app_running():
                sys.exit("Claude.app is open — an unattended swap must not quit it.")
        else:
            print("· quitting Claude.app…")
            if not quit_app():
                sys.exit("Claude.app did not quit within 45s — nothing was changed.\n"
                         "Something is holding it open (a modal dialog?). Quit it by hand and retry.")

        _preserve_displaced(label)

        op_id = time.strftime("%Y%m%d-%H%M%S")
        rb = os.path.join(ROLLBACK, op_id)
        os.makedirs(rb, mode=0o700, exist_ok=True)
        n, _ = copy_identity(PROFILE, rb)
        print(f"· saved {n} files to roll back to")
        for old in sorted(os.listdir(ROLLBACK))[:-KEEP_COPIES]:
            shutil.rmtree(os.path.join(ROLLBACK, old), ignore_errors=True)

        write_json(JOURNAL, {"op": op_id, "phase": "staged", "target": label,
                             "rollback": rb, "staging": _staging(op_id), "moved": []})

        staging = _staging(op_id)
        if os.path.exists(staging):
            shutil.rmtree(staging)
        copy_identity(src, staging)
        print("· staged the incoming profile")

        _apply(op_id, staging, rb, label)

    if not no_launch:
        print("· reopening Claude.app…")
        launch_app()
    # no_launch never reopens: the app was closed, and opening it un-asked would put a window
    # on the user's screen for a switch they only find out about from the notification
    tail = "; it will be on it when next opened" if no_launch else ""
    print(f"\nswitched the desktop app to {label!r}{tail}.")
    print("If it opens on a sign-in screen: signing in is safe (device trust holds) — then run")
    print("`desktop-switch.py recapture` to stash the fresh session. Or undo:  desktop-switch.py undo\n")

KEEP_COPIES = 12   # rollbacks and asides each keep this many; undo/recovery only ever needs the newest

def _aside_stash(label):
    """Move a stash's current content aside before it is overwritten, and bound the pile.

    The newest aside of each label always survives the pruning — it may be the only good
    copy of an account whose stash a bad refresh displaced, and refreshes of OTHER labels
    must not be able to age it out."""
    if not os.path.isdir(stash_dir(label)):
        return
    os.makedirs(ASIDES, mode=0o700, exist_ok=True)
    dest = os.path.join(ASIDES, time.strftime("%Y%m%d-%H%M%S") + "-" + label)
    n = 0
    while os.path.exists(dest):                   # same label twice in one second: uniquify
        n += 1
        dest = os.path.join(ASIDES, time.strftime("%Y%m%d-%H%M%S") + "-" + label + f"~{n}")
    os.rename(stash_dir(label), dest)
    entries = sorted(os.listdir(ASIDES))          # names start with the timestamp: oldest first
    keep = set(entries[-KEEP_COPIES:])
    keep.update({e[16:]: e for e in entries}.values())   # label starts after "YYYYMMDD-HHMMSS-"
    for e in entries:
        if e not in keep:
            shutil.rmtree(os.path.join(ASIDES, e), ignore_errors=True)

def _preserve_displaced(target):
    """Keep the account being displaced switchable-back-to.

    Identity decides where the profile's bytes belong: they refresh the stash holding the
    same account uuid, whatever the labels or this tool's own records claim — which is what
    catches a login screen that got a different account typed into it. The target's own
    stash is never touched: switching to an account you are already on means "install the
    capture", and rewriting it first would just install the live bytes back while
    destroying the capture (the rollback preserves the live bytes instead). A sign-in that
    matches no stash refreshes the recorded label only when that stash has no identity of
    its own to contradict the reading AND its capture predates the installed app version —
    a same-version capture may be perfectly good with merely-unreadable identity, and
    overwriting it on a record's say-so is how a good account stops being switchable-to.
    Everything else is kept as an unclaimed stash for the user to name — never written
    over a labeled stash. When identity can't be read at all, records fill in under the
    same version gate, recorded with no account_uuid, because bytes nobody identified
    must not be stamped as verified. A byte-exact match is left entirely alone: the app
    never ran on those bytes, so even its bookkeeping still describes the capture.
    """
    cur = manifest(PROFILE)
    if not cur:
        return                                # signed-out shell: nothing worth keeping
    uid = profile_identity(PROFILE)
    if uid == SIGNED_OUT:
        # a revoked or ended session: refreshing any stash with these bytes would replace
        # a working capture with a dead one that still names the account
        print("  the profile's session was signed out; nothing worth keeping")
        return
    exact = next((l for l in list_stashes()
                  if (stash_meta(l) or {}).get("files") == cur), None)
    claimed = (read_json(ACTIVE) or {}).get("label")

    def capture_predates_app(label):
        m, cur_ver = stash_meta(label) or {}, app_version()
        return bool(m.get("app_version") and cur_ver and m["app_version"] != cur_ver)

    if uid:
        owners = _uuid_owners(uid)
        owner = exact if exact in owners else (owners[0] if owners else None)
        if owner == target:
            print(f"  the profile already belongs to {target!r}; its capture is what gets installed")
            return
        if owner:
            if exact == owner:
                print(f"  stash for {owner!r}: already current")
            else:
                # a quarantined stash stays quarantined through a refresh — only rename
                # ends the quarantine
                _write_stash(owner, uid, keep_aside=True,
                             unclaimed=bool((stash_meta(owner) or {}).get("unclaimed")))
                print(f"  stash for {owner!r}: refreshed, identity-verified")
            return
        if claimed and claimed != target and claimed in list_stashes() \
                and _stash_uuid(claimed) is None and capture_predates_app(claimed):
            # The record says this label was installed, its stash has no identity to
            # disagree, and its capture predates the app version — what would be
            # overwritten could not have worked anyway.
            _write_stash(claimed, uid, keep_aside=True)
            print(f"  stash for {claimed!r}: refreshed from the live profile; its account "
                  f"is now recorded")
            return
        q = "unclaimed-" + time.strftime("%Y%m%d-%H%M%S")
        _write_stash(q, uid, unclaimed=True)
        print(f"  this profile's account has no stash — kept it as {q!r}; name it with:\n"
              f"    desktop-switch.py rename {q} <label>")
        return
    if exact and exact != target:
        print(f"  stash for {exact!r}: already current")
        return
    if not claimed or claimed == target or claimed not in list_stashes():
        return
    if not capture_predates_app(claimed):
        return
    _write_stash(claimed, keep_aside=True)
    print(f"  refreshed the stash for {claimed!r} from the live profile; its capture predated "
          f"app v{app_version()} (old copy kept aside)")

def _apply(op_id, staging, rb, label):
    """Move the staged identity items into place, recording each move before making it."""
    j = _journal() or {}
    j["phase"] = "applying"
    write_json(JOURNAL, j)
    moved = []
    try:
        for item in IDENTITY:
            staged = os.path.join(staging, item)
            target = os.path.join(PROFILE, item)
            if not os.path.exists(staged):
                # Absent upstream means absent: an account with no such store must not inherit
                # the displaced account's copy.
                if os.path.exists(target):
                    aside = f"{target}.cu-old-{op_id}"
                    os.rename(target, aside)
                    moved.append({"item": item, "aside": aside, "installed": False})
                    j["moved"] = moved; write_json(JOURNAL, j)
                continue
            aside = None
            if os.path.exists(target):
                aside = f"{target}.cu-old-{op_id}"
                os.rename(target, aside)
            moved.append({"item": item, "aside": aside, "installed": True})
            j["moved"] = moved; write_json(JOURNAL, j)
            os.rename(staged, target)
    except Exception as e:
        print(f"! {type(e).__name__} while installing — rolling back", file=sys.stderr)
        _rollback(rb, moved, op_id)
        _clear_journal()
        sys.exit("the switch was rolled back; the profile is as it was.")

    installed = manifest(PROFILE)
    expected = (stash_meta(label) or {}).get("files") or {}
    if installed != expected:
        print("! the installed profile does not match the stash — rolling back", file=sys.stderr)
        _rollback(rb, moved, op_id)
        _clear_journal()
        sys.exit("the switch was rolled back; the profile is as it was.")

    for m in moved:                       # only now are the displaced copies redundant
        if m.get("aside") and os.path.exists(m["aside"]):
            _rm(m["aside"])
    if os.path.exists(staging):
        shutil.rmtree(staging, ignore_errors=True)
    _clear_journal()
    _set_active(label)
    print("· verified the installed files against the stash")

def _rm(path):
    if os.path.isdir(path):
        shutil.rmtree(path, ignore_errors=True)
    else:
        try:
            os.remove(path)
        except OSError:
            pass

def _rollback(rb, moved, op_id):
    """Put back exactly what was displaced, preferring the aside copies and falling back to
    the rollback tree for anything already replaced."""
    for m in reversed(moved):
        target = os.path.join(PROFILE, m["item"])
        if os.path.exists(target):
            _rm(target)
        if m.get("aside") and os.path.exists(m["aside"]):
            os.rename(m["aside"], target)
    for rel, full in walk_identity(rb):
        dst = os.path.join(PROFILE, rel)
        if not os.path.exists(dst):
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(full, dst)
    staging = _staging(op_id)
    if os.path.exists(staging):
        shutil.rmtree(staging, ignore_errors=True)

def _clear_journal():
    try:
        os.remove(JOURNAL)
    except OSError:
        pass

def cmd_repair():
    j = _journal()
    if not j:
        print("nothing to repair — no operation is open.")
        return
    if app_running():
        sys.exit("Claude.app is running — quit it before repairing the profile it is using.")
    print(f"found an unfinished switch to {j.get('target')!r} (phase: {j.get('phase')})")
    if j.get("phase") == "staged":
        # Nothing was moved into place, so discarding the staging area is the whole repair.
        staging = j.get("staging")
        if staging and os.path.exists(staging):
            shutil.rmtree(staging, ignore_errors=True)
        _clear_journal()
        print("nothing had been installed yet — discarded the staged copy. Profile untouched.")
        return
    _rollback(j.get("rollback"), j.get("moved") or [], j.get("op"))
    _clear_journal()
    print("rolled the profile back to what it was before that switch.")

def cmd_undo():
    if _journal():
        sys.exit("an earlier operation did not finish — run `desktop-switch.py repair` first")
    if not os.path.isdir(ROLLBACK):
        sys.exit("nothing to undo")
    rbs = sorted(os.listdir(ROLLBACK))
    if not rbs:
        sys.exit("nothing to undo")
    if app_running():
        print("· quitting Claude.app…")
        if not quit_app():
            sys.exit("Claude.app did not quit within 45s — nothing was changed.")
    rb = os.path.join(ROLLBACK, rbs[-1])
    for item in IDENTITY:                 # clear the current copies before restoring
        _rm(os.path.join(PROFILE, item))
    n, _ = copy_identity(rb, PROFILE)
    label, note = identify()
    # A restored profile is pre-switch, drifted bytes, so identify() usually falls through to
    # the ACTIVE record — which still names the account the undone switch installed. Writing
    # that here would be a wrong guess that recapture later trusts enough to overwrite a stash
    # on, so record a label only on a byte-exact match and otherwise clear it.
    if label and note is None:
        _set_active(label)
    else:
        write_json(ACTIVE, {"label": None, "at": time.time(), "files": {}})
        label = None
    print(f"restored {n} files from {rbs[-1]} — the desktop app is back on "
          f"{label or 'the previous account'}.")
    if not label:
        print("which account that is goes unrecorded after an undo; the next switch or a")
        print("`capture <label>` with the app quit re-establishes it.")
    launch_app()

def _ask(prompt, default=None):
    try:
        v = input(f">>> {prompt}: ").strip()
    except (EOFError, KeyboardInterrupt):
        sys.exit("\naborted — nothing was changed.")
    return v or (default or "")

def _label(prompt):
    while True:
        v = _ask(prompt)
        if v and "/" not in v and not v.startswith("."):
            return v
        print("    a short name, no slashes — e.g. work or personal")

def _pause(msg):
    print(f"\n>>> {msg}")
    try:
        input(">>> press Enter when done: ")
    except (EOFError, KeyboardInterrupt):
        sys.exit("\naborted — nothing was changed.")

def _save_rollback(reason):
    """Copy the identity set aside so `undo` can put it back. Returns the directory."""
    op_id = time.strftime("%Y%m%d-%H%M%S")
    rb = os.path.join(ROLLBACK, op_id)
    os.makedirs(rb, mode=0o700, exist_ok=True)
    n, _ = copy_identity(PROFILE, rb)
    print(f"· saved {n} files to roll back to ({reason})")
    return rb

def cmd_signout_local():
    """Clear the session files so the app opens on a login screen — without logging out.

    Logging out inside the app revokes that session server-side, which kills any stash of it:
    the files can be restored but the session behind them is gone, and the app lands on a login
    screen. Removing the files locally leaves the session intact, so the account stays
    switchable-back-to. This is what makes capturing a second account possible at all.
    """
    if app_running():
        sys.exit("Claude.app is running — quit it first.")
    _save_rollback("before clearing the local session")
    for item in IDENTITY:
        _rm(os.path.join(PROFILE, item))
    write_json(ACTIVE, {"label": None, "at": time.time(), "files": {}})
    print("cleared the local session — the app will open on a login screen.")
    print("The account you were signed into was NOT logged out, so its stash still works.")

def cmd_add():
    """Capture another account without revoking the one already signed in."""
    if hosted_by_app() and not os.environ.get("CU_SKIP_APP_CONTROL"):
        sys.exit("This is running inside the desktop app, and this quits that app — which would\n"
                 "kill the process driving it. Run it from Terminal or iTerm.")
    if _journal():
        sys.exit("an earlier operation did not finish — run `desktop-switch.py repair` first")

    print("\nThis adds another account to the ones you can switch between.")
    print("\nIt does NOT log you out. Logging out inside the app revokes that account's session")
    print("and makes its stash useless — instead this clears the session files locally, which")
    print("leaves the account you are on intact and still switchable-back-to.\n")
    print("The app quits and reopens; Claude Code sessions inside it will be killed.\n")

    current = _label("Name for the account you are signed into RIGHT NOW (e.g. work)")
    incoming = _label("Name for the account you are about to add (e.g. personal)")
    if incoming == current:
        sys.exit("those are the same name — pick two different ones")
    _pause("Ready? Sessions parked, nothing mid-turn.")

    print("\n· quitting Claude.app…")
    if not quit_app():
        sys.exit("Claude.app did not quit within 45s — nothing was changed.")
    cmd_capture(current)
    cmd_signout_local()

    print("\n· reopening Claude.app…")
    launch_app()
    _pause(f"The app should be on a login screen. Log in as {incoming!r}.\n"
           f"    Do NOT use any 'log out' button at any point — that is what breaks a stash.")

    print("\n· quitting Claude.app…")
    if not quit_app():
        sys.exit(f"Claude.app did not quit within 45s. {current!r} is captured; rerun to continue.")
    cmd_capture(incoming)

    print(f"\nNow the test: switching back to {current!r} without logging in.\n")
    _pause("Ready to try it?")
    cmd_switch(current)
    _pause(f"Look at the app. Is it signed in as {current!r}?")
    if _ask(f"Did it come up as {current!r}? [y/N]").lower().startswith("y"):
        print(f"\nIt works. From now on:\n")
        print(f"  desktop-switch.py switch {current}")
        print(f"  desktop-switch.py switch {incoming}")
        print("\nAdd more accounts with `desktop-switch.py add`. Never use the app's log-out.\n")
    else:
        print("\nRestoring what was there before that switch.\n")
        cmd_undo()
        print("\nBoth captures are kept. If the app had been logged out of that account at any")
        print("point since it was captured, its session is revoked and the stash cannot work —")
        print(f"re-add it with:  desktop-switch.py add\n")

def cmd_status():
    print(f"\nprofile:  {PROFILE}")
    running = app_running()
    print(f"app:      {'RUNNING — quit before capturing or switching' if running else 'not running'}"
          f"  (v{app_version()})")
    if running or _journal():
        # A running app holds newer state in memory, and a half-swapped profile mid-repair
        # can carry one account's Local Storage next to another's cookies — in both cases a
        # confident read off the disk would be wrong, so the record answers instead.
        label, note = identify()
        print(f"account:  {label or 'unknown'}" + (f"  — {note}" if note else ""))
    else:
        # At rest the profile can be read outright, which beats any record.
        uid = profile_identity(PROFILE)
        owners = _uuid_owners(uid)
        if owners:
            print(f"account:  {owners[0]}  — read from the profile itself")
        elif uid:
            print(f"account:  {uid[:8]}…  — signed in, but no stash holds this account")
        else:
            # Nothing readable (an app update may have moved the store): the record still
            # knows what was installed.
            label, note = identify()
            print(f"account:  {label or 'unknown'}" + (f"  — {note}" if note else ""))
    j = _journal()
    if j:
        print(f"\n  ⚠ an unfinished switch to {j.get('target')!r} is open (phase {j.get('phase')}).")
        print("    run:  desktop-switch.py repair")
    print("\nstashes:")
    cmd_list()
    if os.path.isdir(ROLLBACK):
        rbs = sorted(os.listdir(ROLLBACK))
        print(f"\nrollbacks: {len(rbs)}" + (f" (newest {rbs[-1]})" if rbs else ""))
    print()

def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    rest = [a for a in sys.argv[2:] if not a.startswith("-")]
    flags = {a for a in sys.argv[2:] if a.startswith("-")}
    arg = rest[0] if rest else ""
    if   cmd == "add":     cmd_add()
    elif cmd == "status":  cmd_status()
    elif cmd == "observe": cmd_observe(heal="--heal" in flags)
    elif cmd == "signout-local": cmd_signout_local()
    elif cmd == "list":    cmd_list()
    elif cmd == "capture": cmd_capture(arg)
    elif cmd == "recapture":
        if flags:
            sys.exit("recapture takes no flags — it replaces the stash immediately, no dry run.\n"
                     "Preview the stash state with `list` or `status` instead.")
        cmd_recapture(arg)
    elif cmd == "switch":  cmd_switch(arg, dry_run=bool({"-n", "--dry-run"} & flags),
                                      no_launch="--no-launch" in flags)
    elif cmd == "undo":    cmd_undo()
    elif cmd == "repair":  cmd_repair()
    elif cmd == "rename":  cmd_rename(arg, rest[1] if len(rest) > 1 else "")
    elif cmd == "forget":  cmd_forget(arg)
    else: sys.exit(__doc__)

if __name__ == "__main__":
    main()
