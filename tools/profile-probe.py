#!/usr/bin/env python3
"""profile-probe — find which files in the Claude desktop app's profile carry account identity.

The app's account lives somewhere in ~/Library/Application Support/Claude. This measures
*where*, so a switch can copy the smallest correct set of files instead of the whole profile.

Method: the app rewrites plenty of files just by running, so one before/after pair can't
separate "changed because the account changed" from "changed because the app ran". Two pairs
can. A control pair brackets a plain launch-and-quit; a switch pair brackets a sign-in as
another account.
Whatever changed in the switch pair but NOT in the control pair is an identity candidate.

Only names, sizes, modes and SHA-256 digests are recorded — never file contents.

The app must be quit for a snapshot to mean anything: its cookie jar lives in memory and is
flushed to disk on quit, so a snapshot of a running app captures a half-written state.

Run this from Terminal, NOT from a Claude Code session inside the desktop app — quitting the
app kills those sessions mid-protocol.

  profile-probe.py run                    DO THE EXPERIMENT — drives the whole protocol
  profile-probe.py steps                  describe the protocol without running it
  profile-probe.py status                 app running? which snapshots exist?
  profile-probe.py backup                 copy the profile aside before anything risky
  profile-probe.py snap <name>            record a snapshot (refuses while the app runs)
  profile-probe.py diff <a> <b>           what changed between two snapshots
  profile-probe.py identity c1 c2 s1 s2   changes in the switch pair that the control pair lacks
"""
import os, sys, json, time, hashlib, shutil, subprocess

# CU_PROBE_PROFILE points the probe at a throwaway directory so its diff arithmetic can be
# exercised without quitting the app or touching the real profile.
PROFILE = os.path.expanduser(os.environ.get("CU_PROBE_PROFILE") or "~/Library/Application Support/Claude")
# Snapshots and backups outlive the app restarts the protocol requires — and the Claude Code
# session that created them, since quitting the app kills any session hosted inside it. They
# live beside the tool's own state rather than in a session scratchpad for that reason.
STATE   = os.path.expanduser("~/.claude-usage")
SNAPS   = os.path.join(STATE, "desktop-snapshots")
BACKUPS = os.path.join(STATE, "desktop-backups")
APP_EXEC = "/Applications/Claude.app/Contents/MacOS/Claude"   # the Electron main process
MAX_BYTES = 8 * 1024 * 1024  # bigger files are recorded as present but not hashed

# Caches, telemetry and downloaded binaries: rewritten constantly, never account identity.
# Excluding them keeps a diff readable and a backup small. Everything else is hashed, so a
# surprise identity file can't hide in a directory we forgot to think about.
SKIP = {
    "Cache", "Code Cache", "GPUCache", "DawnGraphiteCache", "DawnWebGPUCache",
    "Crashpad", "blob_storage", "sentry", "VideoDecodeStats", "Shared Dictionary",
    "shared_proto_db", "claude-code", "claude-code-vm", "vm_bundles", "logs",
    "claude-code-sessions", "claude-code-sessions.bak", "local-agent-mode-sessions",
}

APP_BUNDLE = "com.anthropic.claudefordesktop"

def quit_app(timeout=45):
    """Ask the app to quit and wait until its process is gone. True once it is.

    A graceful quit rather than a kill: the cookie jar we are about to snapshot lives in memory
    until the app flushes it on the way out, so killing the process would capture a profile that
    never existed at rest.
    """
    if not app_running():
        return True
    subprocess.run(["osascript", "-e", 'quit app id "%s"' % APP_BUNDLE], capture_output=True)
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not app_running():
            time.sleep(1.5)       # let the last writes land before anything reads the profile
            return True
        time.sleep(0.5)
    return False

def launch_app(timeout=45):
    """Start the app and wait until its process exists. True once it does."""
    subprocess.run(["open", "-b", APP_BUNDLE], capture_output=True)
    deadline = time.time() + timeout
    while time.time() < deadline:
        if app_running():
            return True
        time.sleep(0.5)
    return False

def app_running():
    """True while the app's main process is alive.

    Matched by full executable path via `ps`, because pgrep does not see this process at all:
    it lists the "Claude Helper" children and misses the main one, so a pgrep check reports the
    app as stopped while it is running — and every guard built on it would wave through a
    snapshot, or later a file swap, against a live profile.
    """
    r = subprocess.run(["ps", "-Ao", "comm="], capture_output=True, text=True)
    return any(line.strip() == APP_EXEC for line in r.stdout.splitlines())

def walk():
    """(relative path, stat) for every regular file worth hashing, symlinks not followed."""
    for root, dirs, files in os.walk(PROFILE):
        rel_root = os.path.relpath(root, PROFILE)
        top = rel_root.split(os.sep)[0]
        if top in SKIP or any(p.startswith(".") for p in rel_root.split(os.sep) if p != "."):
            dirs[:] = []
            continue
        dirs[:] = [d for d in dirs if d not in SKIP and os.path.join(rel_root, d) != "."]
        for name in files:
            if name in SKIP: continue
            full = os.path.join(root, name)
            if os.path.islink(full): continue
            rel = os.path.relpath(full, PROFILE)
            try:
                st = os.stat(full)
            except OSError:
                continue
            yield rel, full, st

def digest(path, size):
    if size > MAX_BYTES:
        return "large:not-hashed"
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
    except OSError as e:
        return f"unreadable:{type(e).__name__}"
    return h.hexdigest()

def take_snapshot():
    files = {}
    for rel, full, st in walk():
        files[rel] = {"size": st.st_size, "mode": st.st_mode & 0o777,
                      "sha256": digest(full, st.st_size)}
    return {"taken_at": time.time(), "app_running": app_running(),
            "app_version": app_version(), "profile": PROFILE, "files": files}

def app_version():
    try:
        import plistlib
        with open("/Applications/Claude.app/Contents/Info.plist", "rb") as f:
            return plistlib.load(f).get("CFBundleShortVersionString")
    except Exception:
        return None

def snap_path(name):
    return os.path.join(SNAPS, f"{name}.json")

def load_snap(name):
    try:
        with open(snap_path(name)) as f:
            return json.load(f)
    except FileNotFoundError:
        sys.exit(f"no snapshot named {name!r} — `profile-probe.py status` lists what exists")

def changed_paths(a, b):
    """(added, removed, modified) between two snapshots, by content digest."""
    fa, fb = a["files"], b["files"]
    added   = sorted(set(fb) - set(fa))
    removed = sorted(set(fa) - set(fb))
    modified = sorted(p for p in set(fa) & set(fb) if fa[p]["sha256"] != fb[p]["sha256"])
    return added, removed, modified

def cmd_snap(name):
    if not name:
        sys.exit("usage: profile-probe.py snap <name>")
    if app_running() and not os.environ.get("CU_PROBE_PROFILE"):
        # A running app has un-flushed state in memory; a snapshot now would record a file set
        # that never existed at rest, and every later diff would inherit the noise. A throwaway
        # profile has no app behind it, so the check applies only to the real one.
        sys.exit("Claude.app is running — quit it first (a snapshot of a running app is meaningless).\n"
                 "  osascript -e 'quit app \"Claude\"'")
    os.makedirs(SNAPS, exist_ok=True)
    snap = take_snapshot()
    with open(snap_path(name), "w") as f:
        json.dump(snap, f, indent=2)
    hashed = sum(1 for v in snap["files"].values() if len(v["sha256"]) == 64)
    print(f"snapshot {name!r}: {len(snap['files'])} files ({hashed} hashed), app v{snap['app_version']}")

def cmd_diff(a, b):
    sa, sb = load_snap(a), load_snap(b)
    added, removed, modified = changed_paths(sa, sb)
    for label, items in (("added", added), ("removed", removed), ("modified", modified)):
        print(f"\n{label} ({len(items)})")
        for p in items:
            size = (sb["files"].get(p) or sa["files"].get(p) or {}).get("size")
            print(f"  {p}  ({size} bytes)")
    print()

def cmd_identity(c1, c2, s1, s2):
    """Changes unique to the account switch: the switch pair's changes minus the control's."""
    ca, cr, cm = changed_paths(load_snap(c1), load_snap(c2))
    sa, sr, sm = changed_paths(load_snap(s1), load_snap(s2))
    churn  = set(ca) | set(cr) | set(cm)
    switch = set(sa) | set(sr) | set(sm)
    only   = sorted(switch - churn)
    both   = sorted(switch & churn)
    snap_b = load_snap(s2)
    print(f"\ncontrol pair changed {len(churn)} paths; switch pair changed {len(switch)}.\n")
    print(f"IDENTITY CANDIDATES — changed on the account switch, untouched by a plain run ({len(only)}):")
    for p in only:
        print(f"  {p}  ({(snap_b['files'].get(p) or {}).get('size')} bytes)")
    print(f"\nAMBIGUOUS — changed in both, so a switch may or may not depend on them ({len(both)}):")
    for p in both:
        print(f"  {p}")
    print("\nA file that changed in the control run is rewritten by merely running the app; it can\n"
          "still carry identity, so treat AMBIGUOUS as 'test before excluding', not 'exclude'.\n")

def cmd_backup():
    if app_running():
        print("warning: Claude.app is running, so this copies a partially-flushed profile.\n"
              "         Good enough as a safety net; quit the app for a faithful copy.\n")
    dest = os.path.join(BACKUPS, time.strftime("%Y%m%d-%H%M%S"))
    os.makedirs(dest, mode=0o700, exist_ok=True)
    n = bytes_copied = 0
    for rel, full, st in walk():
        out = os.path.join(dest, rel)
        os.makedirs(os.path.dirname(out), mode=0o700, exist_ok=True)
        try:
            shutil.copy2(full, out)
        except OSError as e:
            print(f"  skipped {rel}: {type(e).__name__}")
            continue
        n += 1; bytes_copied += st.st_size
    print(f"backed up {n} files ({bytes_copied/1e6:.1f} MB) to\n  {dest}")
    print("restore with:  rsync -a <that dir>/ \"$HOME/Library/Application Support/Claude/\"   (app quit)")

def cmd_status():
    print(f"\nprofile:     {PROFILE}")
    print(f"app:         {'RUNNING — quit before snapping' if app_running() else 'not running — ready to snap'}"
          f"  (v{app_version()})")
    names = sorted(f[:-5] for f in os.listdir(SNAPS)) if os.path.isdir(SNAPS) else []
    print(f"snapshots:   {', '.join(names) if names else '(none yet)'}")
    if os.path.isdir(BACKUPS):
        print(f"backups:     {', '.join(sorted(os.listdir(BACKUPS)))}")
    print()

def _pause(prompt):
    print(f"\n>>> {prompt}")
    try:
        input(">>> press Enter when done: ")
    except (EOFError, KeyboardInterrupt):
        sys.exit("\naborted — nothing was changed; the app may be left quit, reopen it normally.")

def _quit_or_die(why):
    print(f"\n· quitting Claude.app ({why})…")
    if not quit_app():
        sys.exit("Claude.app did not quit within 45s — something is holding it open (a modal dialog?).\n"
                 "Nothing was recorded. Quit it by hand and re-run.")
    print("  quit.")

def _launch_or_die():
    print("· reopening Claude.app…")
    if not launch_app():
        sys.exit("Claude.app did not start within 45s. Open it by hand and re-run.")
    print("  running.")

def cmd_run():
    """Drive the whole protocol, pausing only where a human is actually required."""
    if os.environ.get("CLAUDE_CODE_ENTRYPOINT") == "claude-desktop":
        sys.exit("This is running inside a Claude Code session hosted by the desktop app, and the\n"
                 "protocol quits that app — which would kill this session mid-run.\n"
                 "Run it from Terminal or iTerm instead.")
    print("\nThis quits and reopens Claude.app several times. Any Claude Code session running\n"
          "inside the app will be killed — let them reach a stopping point first.\n")
    _pause("Ready? Sessions parked, nothing mid-turn.")

    _quit_or_die("for a clean backup and the first snapshot")
    cmd_backup()
    cmd_snap("ctrl-a")

    # Control bracket: launch, ordinary use, quit. Whatever changes here changes from merely
    # running the app, so the switch bracket's changes get measured against it.
    _launch_or_die()
    _pause("Use the app normally for a moment — open a chat and send one short message.")
    _quit_or_die("closing the control bracket")
    cmd_snap("ctrl-b")

    # Switch bracket: the same shape — launch, activity, quit — with the account change as the
    # activity. ctrl-b is its opening snapshot: the app stayed quit in between, so the state that
    # ended the control run is the state that begins this one, and the two brackets stay symmetric.
    _launch_or_die()
    _pause("Now sign in as your OTHER account, and send one short message as it so this bracket\n"
           "    matches the control's activity.\n"
           "    To get to a login screen, run `desktop-switch.py signout-local` rather than using\n"
           "    the app's log-out: logging out revokes the session and destroys any saved copy of it.")
    _quit_or_die("closing the switch bracket")
    cmd_snap("sw-b")

    print("\n" + "=" * 72)
    cmd_identity("ctrl-a", "ctrl-b", "ctrl-b", "sw-b")
    print("=" * 72)
    _launch_or_die()
    print("\nDone. Snapshots kept in", SNAPS)
    print("Re-read this any time with:  profile-probe.py identity ctrl-a ctrl-b ctrl-b sw-b\n")

STEPS = """
To actually run the experiment:      profile-probe.py run
(This page only describes it — `run` is what does the work.)

`run` handles the mechanical parts: it quits Claude.app, snapshots, reopens it, and reads
out the answer at the end. It stops and waits for you twice — once to use the app normally,
once to sign in as your other account. Takes about five minutes.

Run it from Terminal, not from a Claude Code session inside the desktop app: the protocol
quits that app, which would kill the session driving it. `run` refuses if it detects this.

Every Claude Code session hosted in the app dies when it quits, so park them first.

What it measures, in case you want to drive it by hand instead:

  CONTROL BRACKET   quit → snap ctrl-a → open → use normally → quit → snap ctrl-b
  SWITCH BRACKET    open → sign in as the other account → quit → snap sw-b
  ANSWER            profile-probe.py identity ctrl-a ctrl-b ctrl-b sw-b

Both brackets have the same shape — launch, activity, quit — so subtracting the first from
the second leaves the files that changed because the *account* changed. ctrl-b closes the
control bracket and opens the switch one: the app stays quit in between, so it is the same
state twice. Keep the activity in each bracket similar; one short message in each is enough.

That sign-in is the last one you should ever need. Once the identity file set is known, a
switch copies those files with the app quit and never signs in again.
"""

def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "steps"
    arg = sys.argv[2:]
    if   cmd == "steps":    print(STEPS)
    elif cmd == "run":      cmd_run()
    elif cmd == "status":   cmd_status()
    elif cmd == "backup":   cmd_backup()
    elif cmd == "snap":     cmd_snap(arg[0] if arg else "")
    elif cmd == "diff":
        if len(arg) != 2: sys.exit("usage: profile-probe.py diff <a> <b>")
        cmd_diff(*arg)
    elif cmd == "identity":
        if len(arg) != 4: sys.exit("usage: profile-probe.py identity <ctrl-a> <ctrl-b> <sw-a> <sw-b>")
        cmd_identity(*arg)
    else:
        sys.exit(__doc__)

if __name__ == "__main__":
    main()
