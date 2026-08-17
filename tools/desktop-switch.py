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
  desktop-switch.py capture <label>     stash the signed-in account's profile as <label>
  desktop-switch.py recapture [<label>] refresh a stash from the live profile (run after a
                                        forced sign-in; defaults to the installed account)
  desktop-switch.py list                stashes, with age and app version
  desktop-switch.py switch <label>      swap to that account (quits and reopens the app)
  desktop-switch.py switch <label> -n   dry run: show exactly what would move, change nothing
  desktop-switch.py undo                put back what the last switch overwrote
  desktop-switch.py repair              finish or roll back an interrupted operation
  desktop-switch.py rename <old> <new>  fix a mislabeled stash (no re-capture needed)
  desktop-switch.py forget <label>
"""
import os, sys, json, time, shutil, hashlib, subprocess

PROFILE = os.path.expanduser(os.environ.get("CU_DESKTOP_PROFILE")
                             or "~/Library/Application Support/Claude")
STATE    = os.path.expanduser(os.environ.get("CU_DESKTOP_STATE") or "~/.claude-usage")
STASHES  = os.path.join(STATE, "desktop-stashes")
ROLLBACK = os.path.join(STATE, "desktop-rollbacks")
# Displaced copies of stashes that were refreshed in place (see _refresh_dead_stash). Not under
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

def list_stashes():
    if not os.path.isdir(STASHES):
        return []
    # Dot-names are excluded so _write_stash's build directory never shows up as a stash.
    return sorted(d for d in os.listdir(STASHES)
                  if not d.startswith(".") and os.path.isdir(stash_dir(d)))

def _write_stash(label, keep_aside):
    """Replace <label>'s stash with the live profile's identity files.

    The new copy is built complete in a hidden sibling directory and swapped into place with
    renames, so an interruption at any point leaves the old stash or the new one under the
    label — never a half-written directory. The manifest is hashed from the copied bytes
    rather than the profile, so the record can never disagree with what the stash holds (the
    post-swap verification in _apply compares against it). keep_aside moves the displaced
    copy into ASIDES instead of deleting it.

    Returns (files copied, bytes copied, manifest of the copy).
    """
    dest = stash_dir(label)
    tmp = os.path.join(STASHES, f".building-{label}")
    if os.path.exists(tmp):
        shutil.rmtree(tmp)
    os.makedirs(tmp, mode=0o700, exist_ok=True)
    n, total = copy_identity(PROFILE, os.path.join(tmp, "files"))
    files = manifest(os.path.join(tmp, "files"))
    write_json(os.path.join(tmp, "manifest.json"), {
        "label": label, "captured_at": time.time(), "app_version": app_version(),
        "files": files})
    if os.path.exists(dest):
        if keep_aside:
            aside = os.path.join(ASIDES, time.strftime("%Y%m%d-%H%M%S") + "-" + label)
            os.makedirs(ASIDES, mode=0o700, exist_ok=True)
            os.rename(dest, aside)
        else:
            shutil.rmtree(dest)
    os.rename(tmp, dest)
    return n, total, files

def cmd_capture(label):
    if not label:
        sys.exit("usage: desktop-switch.py capture <label>")
    if app_running():
        sys.exit("Claude.app is running — quit it first.\n"
                 "Its session stores are flushed to disk on quit, so a capture now would stash\n"
                 "a partial profile that fails when installed later.")
    if next(walk_identity(PROFILE), None) is None:
        sys.exit(f"nothing to capture — no identity files found under {PROFILE}")
    replacing = label in list_stashes()
    n, total, files = _write_stash(label, keep_aside=replacing)
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

    The label defaults to the account this tool last installed, and anything else is refused:
    the ACTIVE record is the only identity available for a drifted profile, so recapturing
    under a different name — or with no record at all — could overwrite a good stash with
    another account's files. A hand sign-in as a different account still recaptures under the
    recorded label; `rename` fixes the name afterwards, and the aside copy keeps the history
    either way.
    """
    if app_running():
        sys.exit("Claude.app is running — quit it first.\n"
                 "Its session stores are flushed to disk on quit, so a recapture now would stash\n"
                 "a partial profile that fails when installed later.")
    if _journal():
        sys.exit("an earlier operation did not finish — run `desktop-switch.py repair` first")
    claimed = (read_json(ACTIVE) or {}).get("label")
    label = label or claimed
    if not claimed:
        sys.exit("the tool has no record of which account this profile holds (signout-local,\n"
                 "undo, and hand sign-ins clear it), so recapture cannot attribute it to a\n"
                 "stash safely. If you know which account is signed in, quit the app and run:\n"
                 "  desktop-switch.py capture <label>   (the label's old stash is kept aside)")
    if label not in list_stashes():
        sys.exit(f"unknown stash: {label}\nknown: {', '.join(list_stashes()) or '(none)'}\n"
                 "recapture refreshes an existing stash — use `capture` for a new label")
    if claimed != label:
        sys.exit(f"the profile here was last installed as {claimed!r}, not {label!r} — refusing\n"
                 f"to overwrite {label!r}'s stash with it. Run `recapture {claimed}`, then\n"
                 f"`rename` if that label is wrong.")
    if next(walk_identity(PROFILE), None) is None:
        sys.exit(f"nothing to recapture — no identity files found under {PROFILE}")
    with Lock():
        n, total, files = _write_stash(label, keep_aside=True)
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
        # The active row is the account in use, not a parked stash — no sign-in prediction there.
        stale = (f"  (parked >{STALE_DAYS:g}d — expect a sign-in)"
                 if age > STALE_DAYS and label != active else "")
        print(f" {mark} {label:<20} {len(m.get('files') or {}):>5} files  "
              f"captured {age:.1f}d ago  app v{m.get('app_version')}{stale}")

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

def identify_exact():
    """The stash the profile matches byte for byte, or None.

    Certainty, not a best guess — the caller overwrites a stash on the strength of it, and
    guessing wrong there costs the ability to switch to that account at all.
    """
    if not os.path.isdir(PROFILE):
        return None
    cur = manifest(PROFILE)
    if not cur:
        return None
    for label in list_stashes():
        if (stash_meta(label) or {}).get("files") == cur:
            return label
    return None

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

def cmd_switch(label, dry_run=False):
    if not label:
        sys.exit("usage: desktop-switch.py switch <label> [-n]")
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

    with Lock():
        print("· quitting Claude.app…")
        if not quit_app():
            sys.exit("Claude.app did not quit within 45s — nothing was changed.\n"
                     "Something is holding it open (a modal dialog?). Quit it by hand and retry.")

        # The account being displaced is worth keeping switchable-back-to. Refresh its stash
        # only when the profile still matches it exactly; a drifted profile belongs to whatever
        # happened in the app, and overwriting a good stash with it would lose the known-good copy.
        cur_label = identify_exact()
        if cur_label and cur_label != label:
            _write_stash(cur_label, keep_aside=False)
            print(f"  refreshed the stash for {cur_label!r} before replacing it")
        else:
            _refresh_dead_stash(label)

        op_id = time.strftime("%Y%m%d-%H%M%S")
        rb = os.path.join(ROLLBACK, op_id)
        os.makedirs(rb, mode=0o700, exist_ok=True)
        n, _ = copy_identity(PROFILE, rb)
        print(f"· saved {n} files to roll back to")

        write_json(JOURNAL, {"op": op_id, "phase": "staged", "target": label,
                             "rollback": rb, "staging": _staging(op_id), "moved": []})

        staging = _staging(op_id)
        if os.path.exists(staging):
            shutil.rmtree(staging)
        copy_identity(src, staging)
        print("· staged the incoming profile")

        _apply(op_id, staging, rb, label)

    print("· reopening Claude.app…")
    launch_app()
    print(f"\nswitched the desktop app to {label!r}.")
    print("If it opens on a sign-in screen: signing in is safe (device trust holds) — then run")
    print("`desktop-switch.py recapture` to stash the fresh session. Or undo:  desktop-switch.py undo\n")

def _refresh_dead_stash(target):
    """Replace the displaced account's stash with the live profile when its capture predates
    the installed app version.

    A pre-update capture cannot work as captured (installing it lands on a login screen),
    while the profile being displaced holds whatever session has actually been working under
    the new version. The byte-exact guard above is deliberately not required here: it protects
    known-good captures from drifted profiles, and this capture is known-dead — the worst a
    drifted profile can do is mislabel the stash (the ACTIVE record is a record, not a
    reading, so a hand sign-in as another account would be stashed under this label; `rename`
    fixes that). The displaced bytes go aside rather than being deleted, so even that case
    loses nothing. If the profile is itself a signed-out login screen (the user never logged
    in after a failed switch), its bytes are stashed and the version warning clears — the
    stash is no worse than before, and the aside copy keeps the history.
    """
    claimed = (read_json(ACTIVE) or {}).get("label")
    if not claimed or claimed == target or claimed not in list_stashes():
        return
    m = stash_meta(claimed) or {}
    cur_ver = app_version()
    if not (m.get("app_version") and cur_ver and m["app_version"] != cur_ver):
        return
    if next(walk_identity(PROFILE), None) is None:
        return
    _write_stash(claimed, keep_aside=True)
    print(f"  refreshed the stash for {claimed!r} from the live profile; its capture predated "
          f"app v{cur_ver} (old copy kept aside)")

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
    print(f"app:      {'RUNNING — quit before capturing or switching' if app_running() else 'not running'}"
          f"  (v{app_version()})")
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
    elif cmd == "signout-local": cmd_signout_local()
    elif cmd == "list":    cmd_list()
    elif cmd == "capture": cmd_capture(arg)
    elif cmd == "recapture":
        if flags:
            sys.exit("recapture takes no flags — it replaces the stash immediately, no dry run.\n"
                     "Preview the stash state with `list` or `status` instead.")
        cmd_recapture(arg)
    elif cmd == "switch":  cmd_switch(arg, dry_run=bool({"-n", "--dry-run"} & flags))
    elif cmd == "undo":    cmd_undo()
    elif cmd == "repair":  cmd_repair()
    elif cmd == "rename":  cmd_rename(arg, rest[1] if len(rest) > 1 else "")
    elif cmd == "forget":  cmd_forget(arg)
    else: sys.exit(__doc__)

if __name__ == "__main__":
    main()
