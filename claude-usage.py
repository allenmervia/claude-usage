#!/usr/bin/env python3
"""claude-usage — show 5-hour and weekly usage across several Claude accounts
without logging in and out. Codex (OpenAI) usage is shown alongside, read-only.

How it works
------------
Claude Code keeps the *currently logged-in* account's OAuth token in the macOS
Keychain item "Claude Code-credentials" (or, for a CLI configured without the
Keychain, in ~/.claude/.credentials.json). This tool reads that token to identify
the active account, then stashes each account's refresh token in its own Keychain
item ("claude-usage/<uuid>"). With a stored refresh token it can mint a
short-lived access token for a *parked* account and read its usage — no login
swap needed.

Account registration is automatic: every run ingests whichever account is
currently active in Claude Code. Rotate through your accounts once (the switching
you already do) and all of them become visible from then on.

Safety: the active account is always read live from Claude Code's own Keychain
item and is never independently refreshed, so this tool cannot desync the session
you're logged into. Only parked accounts get refreshed, and rotated refresh
tokens are written straight back to the Keychain.

Codex: identity is read from ~/.codex/auth.json and usage from the newest Codex
session rollout that records a rate limit — no API call, no writes, no switching.
A session log doesn't name its account, so the reading is attributed to whoever
is currently signed in, and each row shows how old it is (Codex usage can't be
refreshed without running codex). Accounts are keyed by account_id, so rotating
the auth.json slot accretes them the same way Claude accounts accrue.

Usage:
  claude-usage setup      guided first-time setup (register account, optional menu bar + PATH)
  claude-usage            table of all known accounts (default)
  claude-usage install    add the menu-bar view (installs xbar if needed, links + launches it)
  claude-usage doctor     check the setup and report what needs fixing
  claude-usage interval N set the menu-bar refresh cadence (1m / 5m / 10m / 30m)
  claude-usage --json     machine-readable JSON
  claude-usage --xbar     xbar/SwiftBar menu-bar format
  claude-usage capture    explicitly ingest the active account (same as a run)
  claude-usage list       list registered accounts
  claude-usage switch X   point the CLI at account X (email / label / uuid)
  claude-usage switch --undo   restore the account that was active before the last switch
  claude-usage forget X   drop account by email or uuid
"""
import sys, os, re, json, glob, time, getpass, subprocess, shutil, urllib.request, urllib.error
from datetime import datetime, timezone, timedelta

CLIENT_ID   = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
LIVE_SVC    = "Claude Code-credentials"       # Claude Code's own keychain item
STORE_SVC   = "claude-usage"                  # our per-account secret store
USAGE_URL   = "https://api.anthropic.com/api/oauth/usage"
PROFILE_URL = "https://api.anthropic.com/api/oauth/profile"
TOKEN_HOSTS = [
    "https://console.anthropic.com/v1/oauth/token",
    "https://claude.ai/v1/oauth/token",
    "https://api.anthropic.com/v1/oauth/token",
]
BETA = "oauth-2025-04-20"
STATE_DIR = os.path.expanduser("~/.claude-usage")
INDEX = os.path.join(STATE_DIR, "accounts.json")
CACHE = os.path.join(STATE_DIR, "cache.json")
# Codex (OpenAI). Read-only: identity from ~/.codex/auth.json, usage from the latest session rollout.
# CODEX_HOME lets a per-home multi-account setup point us at one home; multi-home isn't auto-discovered.
CODEX_HOME     = os.path.expanduser(os.environ.get("CODEX_HOME") or "~/.codex")
CODEX_AUTH     = os.path.join(CODEX_HOME, "auth.json")
CODEX_SESSIONS = os.path.join(CODEX_HOME, "sessions")
CODEX_INDEX    = os.path.join(STATE_DIR, "codex-accounts.json")   # opportunistic registry, keyed by account_id
CODEX_SCAN     = 60        # newest session files to search for a usable reading before giving up (see below)
COOLDOWN = 30  # s — rapid re-refreshes within this window reuse the last result, sparing the API
# The menu-bar host reads the refresh cadence from the "5m" in the plugin filename; we own the
# symlink in its folder, so changing the interval is a rename of that link. PLUGIN_FILE is the
# wrapper's fixed name in the repo — only the link's name carries the cadence.
PLUGIN_FILE = "claude-usage.5m.sh"
DEFAULT_INTERVAL = "5m"
INTERVALS = ["1m", "5m", "10m", "30m"]

# ---- keychain helpers -------------------------------------------------------

def _sec(args, inp=None):
    return subprocess.run(["security", *args], capture_output=True, text=True, input=inp)

def keychain_read(service, account=None):
    args = ["find-generic-password", "-s", service, "-w"]
    if account: args = ["find-generic-password", "-s", service, "-a", account, "-w"]
    r = _sec(args)
    return r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else None

def keychain_write(service, account, secret):
    """True if the secret landed. Callers must check: a silent failure (Keychain locked, the user
    denying the access prompt) would otherwise leave a rotated token unsaved and the account
    permanently unrefreshable, with nothing shown anywhere.

    The secret is passed as an argv value, which anything running as this user can read out of the
    process table. `security`'s only alternative is the interactive prompt behind a bare `-w`, and
    that reads through a 128-byte buffer — it silently truncates the ~530-byte credential blobs
    stored here, so it is not an option. Callers keep the exposure rare by writing only when the
    value actually changes (see store_secret).
    """
    return _sec(["add-generic-password", "-U", "-s", service, "-a", account, "-w", secret]).returncode == 0

def keychain_delete(service, account):
    return _sec(["delete-generic-password", "-s", service, "-a", account]).returncode == 0

# ---- index (non-secret account metadata) -----------------------------------

def load_index():
    try:
        with open(INDEX) as f: return json.load(f)
    except Exception:
        return []

def save_index(idx):
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = INDEX + ".tmp"
    with open(tmp, "w") as f: json.dump(idx, f, indent=2)
    os.replace(tmp, INDEX)

def upsert(idx, entry):
    for i, e in enumerate(idx):
        if e["uuid"] == entry["uuid"]:
            idx[i] = {**e, **entry}; return idx
    idx.append(entry); return idx

# ---- http -------------------------------------------------------------------

def api_get(url, token):
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}",
        "anthropic-beta": BETA,
        "Accept": "application/json",
        "User-Agent": "claude-usage/1.0",
    })
    with urllib.request.urlopen(req, timeout=25) as r:
        return json.loads(r.read())

def refresh_token(refresh, host_hint=None):
    """Exchange a refresh token for a new access token. Returns (data, host)."""
    body = json.dumps({"grant_type": "refresh_token",
                       "refresh_token": refresh,
                       "client_id": CLIENT_ID}).encode()
    hosts = ([host_hint] if host_hint else []) + [h for h in TOKEN_HOSTS if h != host_hint]
    last = None
    for h in hosts:
        try:
            req = urllib.request.Request(h, data=body, method="POST", headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "claude-usage/1.0",
            })
            with urllib.request.urlopen(req, timeout=25) as r:
                return json.loads(r.read()), h
        except urllib.error.HTTPError as e:
            last = f"HTTP {e.code} at {h}"
            # 4xx that isn't 404 means the host is right but the grant failed
            if e.code not in (404, 405, 400):
                raise RuntimeError(last)
        except Exception as e:
            last = f"{type(e).__name__} at {h}"
    raise RuntimeError(last or "refresh failed")

# ---- credential resolution --------------------------------------------------

def read_live():
    """The currently logged-in account's OAuth blob, from wherever Claude Code put it.

    Desktop app and CLI both use the macOS Keychain item by default; a CLI configured
    without Keychain writes ~/.claude/.credentials.json instead. Try both.
    """
    raw = keychain_read(LIVE_SVC)
    if not raw:
        try:
            with open(os.path.expanduser("~/.claude/.credentials.json")) as f:
                raw = f.read()
        except Exception:
            return None
    try:
        return json.loads(raw).get("claudeAiOauth")
    except Exception:
        return None

BLOB_META = ("scopes", "subscriptionType", "rateLimitTier")   # non-token fields a written blob needs

def store_secret(uuid, refresh, access=None, expires_at=None, host=None, meta=None):
    # merge over any existing record so BLOB_META survives token rotations
    prev = load_secret(uuid) or {}
    rec = dict(prev)
    # keep the existing refresh token if the caller has none: it is the account's only durable
    # credential, and overwriting it with None costs a re-login with no way back
    rec.update({"refreshToken": refresh or rec.get("refreshToken"), "accessToken": access,
                "expiresAt": expires_at, "tokenHost": host})
    if meta: rec.update({k: v for k, v in meta.items() if v is not None})
    if rec == prev:
        # nothing changed, so skip the write: every write puts the secret in argv where the process
        # table exposes it, and an unchanged re-ingest happens on every refresh tick
        return True
    return keychain_write(STORE_SVC, uuid, json.dumps(rec))

def load_secret(uuid):
    raw = keychain_read(STORE_SVC, uuid)
    if not raw: return None
    try: return json.loads(raw)
    except Exception: return None      # corrupt value: treat as uncaptured, don't take the tool down

def token_for_parked(uuid, force=False):
    """Valid access token for a parked account, refreshing + rotating if needed.
    force=True skips the cached access token (used to recover from a 401 on a token that
    looked unexpired but was invalidated server-side)."""
    sec = load_secret(uuid)
    if not sec: return None, "not captured — sign into it once and re-run"
    now_ms = time.time() * 1000
    if not force and sec.get("accessToken") and (sec.get("expiresAt") or 0) > now_ms + 60_000:
        return sec["accessToken"], None
    if not sec.get("refreshToken"):
        return None, "no refresh token — sign into it once and re-run"
    try:
        data, host = refresh_token(sec["refreshToken"], sec.get("tokenHost"))
    except Exception as e:
        return None, f"refresh failed ({e}) — sign into it once and re-run"
    access  = data["access_token"]
    newref  = data.get("refresh_token", sec["refreshToken"])
    exp     = int((time.time() + data.get("expires_in", 3600)) * 1000)
    if not store_secret(uuid, newref, access, exp, host):   # persist rotated refresh token
        # the server already rotated: our stored copy is now the dead one, so say so rather than
        # hand back a token that works once and leaves the account unrefreshable afterwards
        return access, "couldn't save the rotated token to the Keychain — unlock it and re-run"
    return access, None

def is_team_entry(e):
    return bool(e.get("seat_tier")) or e.get("org_type") in ("claude_team", "claude_enterprise")

def match_live_uuid():
    """Which known account holds the live credential, by matching stored refresh tokens.

    No network and no writes, so it still identifies the session when /profile can't be reached
    or its token has expired. That matters beyond the ·active label: an unidentified active
    account is treated as parked and refreshed from its stored token — the one thing reading
    must never do to the live session, since a rotation there invalidates Claude Code's own copy.
    """
    live = read_live()
    if not live: return None
    for e in load_index():
        sec = load_secret(e["uuid"]) or {}
        if sec.get("refreshToken") and sec["refreshToken"] == live.get("refreshToken"):
            return e["uuid"]
    return None

def active_uuid_only():
    """Which account is signed in, identified but not registered — /profile with the live token,
    without ingest_live's writes to the index and Keychain."""
    live = read_live()
    if not live: return None
    try:
        uuid = api_get(PROFILE_URL, live["accessToken"]).get("account", {}).get("uuid")
        if uuid and any(e["uuid"] == uuid for e in load_index()):
            return uuid
    except Exception:
        pass
    return match_live_uuid()

def ingest_live(idx):
    """Register/refresh whichever account is currently active in Claude Code."""
    live = read_live()
    if not live: return None
    try:
        prof = api_get(PROFILE_URL, live["accessToken"])
    except Exception:
        return None
    acct = prof.get("account", {}); org = prof.get("organization", {})
    uuid = acct.get("uuid")
    if not uuid: return None
    # Personal wins: signing into an account's TEAM context must not clobber a personal entry
    # for the same account (same uuid). Skip the capture — the personal one keeps showing as parked.
    team = org.get("organization_type") in ("claude_team", "claude_enterprise") or org.get("seat_tier")
    if team:
        existing = next((e for e in idx if e.get("uuid") == uuid), None)
        if existing and not is_team_entry(existing):
            return None
    entry = {
        "uuid": uuid,
        "email": acct.get("email", uuid),
        "label": acct.get("display_name") or acct.get("email", uuid),
        "tier":  org.get("rate_limit_tier"),          # e.g. default_claude_max_20x / _5x / pro
        "org_type": org.get("organization_type"),     # claude_max / claude_team / claude_enterprise
        "seat_tier": org.get("seat_tier"),            # non-null => a team/enterprise seat
    }
    # Keep a copy of Claude Code's own profile block for this account, so switching back to it can
    # restore the keys /profile doesn't carry. Read it only when we haven't got one for this
    # account: ~/.claude.json is the largest file either side touches and this runs on every
    # refresh tick, while what we want from it changes only at login. Only take it when it names
    # the account we just identified — the credential and the cached profile can disagree, and the
    # credential is the one that decides whose account this is.
    have = (next((e for e in idx if e.get("uuid") == uuid), None) or {}).get("profile") or {}
    if have.get("accountUuid") != uuid:
        prof_blob = read_live_profile()
        if prof_blob and prof_blob.get("accountUuid") == uuid:
            entry["profile"] = prof_blob
    upsert(idx, entry)
    # keep this account's stored credentials current from the live keychain (full blob, so we can
    # write a faithful one back when switching to it)
    prev = load_secret(uuid)
    store_secret(uuid, live.get("refreshToken"), live.get("accessToken"),
                 live.get("expiresAt"), prev.get("tokenHost") if prev else None,
                 meta={k: live.get(k) for k in BLOB_META})
    save_index(idx)
    return uuid

# ---- gathering usage --------------------------------------------------------

def parse_dt(s):
    if not s: return None
    try: return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception: return None

# ---- the weekly boundary ----------------------------------------------------
# The weekly window opens on first use, so between a reset and the next request the endpoint reports
# no reset time at all. The time it reports once the window does open is not seven days out — it is a
# fixed weekly boundary the account has kept across resets. So the last one seen predicts the next,
# and remembering it keeps an account's schedule on screen through the gap. A projection is always
# marked as one: the boundary is inferred from observed behaviour, not something the API promises.

WEEK = 7 * 86400
ANCHOR_MAX_AGE = 8 * WEEK   # past this, the account has been idle long enough that a boundary moved
                            # without us watching is likelier than the stale one still holding

def project_weekly(anchor):
    """The next occurrence of the weekly boundary `anchor` fell on, or None if it can't carry one."""
    dt = parse_dt(anchor)
    if not dt: return None
    now = datetime.now(timezone.utc)
    if (now - dt).total_seconds() > ANCHOR_MAX_AGE: return None
    if dt > now: return dt.isoformat()
    steps = int((now - dt).total_seconds() // WEEK) + 1
    return (dt + timedelta(seconds=steps * WEEK)).isoformat()

def apply_weekly_anchor(wk, entry):
    """Record on `entry` the weekly reset this account just reported, or fill `wk` with the
    projection when it reports none. True if the entry changed and the index needs saving."""
    if wk.get("resets_at"):
        if wk["resets_at"] == entry.get("weekly_anchor"):
            return False
        entry["weekly_anchor"] = wk["resets_at"]
        return True
    projected = project_weekly(entry.get("weekly_anchor"))
    if projected:
        wk["resets_at"], wk["projected"] = projected, True
    return False

def fetch_usage(uuid, token, active):
    """Return (usage_json, error). On a 401 for a parked account, force a token refresh and retry once."""
    try:
        return api_get(USAGE_URL, token), None
    except urllib.error.HTTPError as ex:
        if ex.code == 401 and not active:
            token2, err2 = token_for_parked(uuid, force=True)   # cached token was invalidated early
            if err2: return None, err2
            try:
                return api_get(USAGE_URL, token2), None
            except urllib.error.HTTPError as ex2:
                return None, ("session expired — sign into it again"
                              if ex2.code == 401 else f"usage HTTP {ex2.code}")
            except Exception as ex2:
                return None, type(ex2).__name__
        if ex.code == 429:
            return None, "rate-limited — refreshing too fast, wait a moment"
        return None, f"usage HTTP {ex.code}"
    except Exception as ex:
        return None, type(ex).__name__

def load_cache():
    try:
        with open(CACHE) as f: return json.load(f)
    except Exception:
        return None

def save_cache(rows, ts):
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(CACHE, "w") as f: json.dump({"ts": ts, "rows": rows}, f)
    except Exception:
        pass

def clear_cache():
    """Drop the debounce cache so the next render fetches fresh (e.g. right after a switch)."""
    try: os.remove(CACHE)
    except Exception: pass

def data_ts():
    """Epoch seconds of the last real fetch (cache timestamp), or None."""
    c = load_cache()
    return c.get("ts") if c else None

def collect(ingest=True):
    """Cached wrapper: debounce rapid refreshes, and fall back to last-known values on a rate-limit.

    ingest=False reports on the accounts already known without registering the live one — for
    diagnostics, which should describe the current state rather than change it.
    """
    now = time.time()
    cache = load_cache()
    if cache and 0 <= now - cache.get("ts", 0) < COOLDOWN:
        return cache["rows"]                                  # rapid re-refresh → reuse, don't hit the API
    rows = _collect_live(ingest)
    # Freshness is judged on Claude alone: only Claude hits the network, and a retained Codex snapshot
    # (never an error) must not mask a Claude 429 storm and clobber the last-known Claude values.
    claude = [r for r in rows if r.get("provider", "claude") == "claude"]
    codex  = [r for r in rows if r.get("provider") == "codex"]
    if not claude or any(not r.get("error") for r in claude):  # got fresh Claude data (or none to fetch)
        save_cache(rows, now)
        return rows
    if cache and cache.get("rows"):                           # every Claude read errored → last known Claude
        stale = [r for r in cache["rows"] if r.get("provider", "claude") == "claude"]
        for r in stale: r["stale"] = True
        return stale + codex                                  # keep this run's fresh Codex rows
    return rows

def _collect_live(ingest=True):
    idx = load_index()
    active_uuid = ingest_live(idx) if ingest else active_uuid_only()
    if active_uuid is None:
        # /profile couldn't place the session (expired live token, offline). Fall back to matching
        # the stored credential: leaving it unidentified would refresh the live account as parked.
        active_uuid = match_live_uuid()
    idx = load_index()
    live = read_live()
    rows = []
    anchors_moved = False
    for e in idx:
        uuid = e["uuid"]
        if uuid == active_uuid and live:
            token, err = live["accessToken"], None
        else:
            token, err = token_for_parked(uuid)
        row = {"provider": "claude", "uuid": uuid, "email": e["email"], "label": e["label"],
               "tier": e.get("tier"), "org_type": e.get("org_type"), "is_team": is_team_entry(e),
               "active": uuid == active_uuid, "error": err}
        if token and not err:
            u, uerr = fetch_usage(uuid, token, row["active"])
            if uerr:
                row["error"] = uerr
            if u is not None:
                # every window is `or {}`-guarded: these are undocumented endpoints, and a null or
                # absent window must degrade to an error row rather than crash the whole render
                fh_u, wk_u = (u or {}).get("five_hour") or {}, (u or {}).get("seven_day") or {}
                if not fh_u and not wk_u and not row.get("error"):
                    row["error"] = "usage response had no windows"
                row["five_hour"] = {"pct": fh_u.get("utilization"), "resets_at": fh_u.get("resets_at")}
                row["seven_day"] = {"pct": wk_u.get("utilization"), "resets_at": wk_u.get("resets_at")}
                anchors_moved |= apply_weekly_anchor(row["seven_day"], e)
                # dollar spend is a SEPARATE, opt-in thing (extra-usage credits / usage-based billing).
                # It is disabled on most plans incl. standard team seats, so only surface it when enabled.
                sp = (u or {}).get("spend") or {}
                row["spend"] = {"enabled": bool(sp.get("enabled")),
                                "used": (sp.get("used") or {}).get("amount_minor"),
                                "limit": (sp.get("limit") or {}).get("amount_minor"),
                                "percent": sp.get("percent")}
                # scoped weekly limits (e.g. Opus) if present
                scoped = []
                for lim in (u or {}).get("limits") or []:
                    if lim.get("kind") == "weekly_scoped" and lim.get("scope"):
                        m = (lim["scope"].get("model") or {}).get("display_name")
                        scoped.append({"model": m, "pct": lim.get("percent"),
                                       "resets_at": lim.get("resets_at")})
                row["scoped"] = scoped
        rows.append(row)
    if anchors_moved and ingest:   # ingest=False is diagnostic: report the state, don't advance it
        save_index(idx)
    rows += collect_codex(persist=ingest)   # Codex is read-only; persist mirrors Claude's ingest flag
    return rows

# ---- codex (openai) ---------------------------------------------------------
# Codex reports the same thing Claude does — percent of a rate-limit window used, and when it resets —
# but through different plumbing: no API, a single-account auth.json, and usage buried in session logs.
# Three facts shape the code below: (1) a session rollout doesn't record *which* account wrote it, so
# identity must come from auth.json and usage is attributed to whoever is currently signed in; (2) a
# window whose reset time has passed reports a stale percentage the file never clears — treat it as
# unknown; (3) the data is only as fresh as the last codex run, so every row carries its own age.

def _jwt_claims(tok):
    """Decode a JWT payload without verifying — it's a local file we already trust; we only read it."""
    try:
        import base64
        p = (tok or "").split(".")[1]; p += "=" * (-len(p) % 4)
        d = json.loads(base64.urlsafe_b64decode(p))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}

def codex_plan_name(plan):
    if not plan: return ""
    return {"free": "Free", "plus": "Plus", "pro": "Pro", "prolite": "Pro Lite",
            "business": "Business", "team": "Team", "enterprise": "Enterprise"}.get(plan.lower(), plan)

def codex_window_label(minutes):
    """Name a window by its duration — Codex places the 5-hour/weekly limit in whichever slot, so the
    minutes (not the primary/secondary position) are what identify it."""
    if not minutes: return "window"
    return {300: "5-hour", 1440: "daily", 10080: "weekly", 43200: "monthly"}.get(
        minutes, f"{minutes // 1440}d" if minutes >= 1440 else f"{minutes // 60}h")

def codex_latest_usage():
    """The newest rate_limits event that actually carries a window, as (epoch_ts, rate_limits).

    Each active session rewrites rate_limits every turn, so the most-recently-modified file holds the
    freshest figures. But Codex also logs window-less events (limit_id "premium" with primary and
    secondary both null — a separate stream, e.g. the desktop/computer-use runtime), which carry no
    utilization. Whole runs of recent sessions can be window-less, so we skip those events and scan
    back through the newest CODEX_SCAN files, stopping at the first that yields a windowed reading.
    Past that budget we return None and the caller keeps the last stored figure (aged, not erased)."""
    try:
        # Recursive so we don't depend on the YYYY/MM/DD layout — any rollout under sessions/ is found.
        files = sorted(glob.glob(os.path.join(CODEX_SESSIONS, "**", "*.jsonl"), recursive=True),
                       key=os.path.getmtime, reverse=True)
    except Exception:
        files = []
    for f in files[:CODEX_SCAN]:
        best = None
        try:
            with open(f) as fh:
                for line in fh:
                    if '"rate_limits"' not in line: continue
                    try:
                        d = json.loads(line); rl = d.get("payload", {}).get("rate_limits")
                        ts = parse_dt(d.get("timestamp"))
                    except Exception:
                        continue
                    if not isinstance(rl, dict) or not ts: continue
                    if not (_codex_window_ok(rl.get("primary")) or _codex_window_ok(rl.get("secondary"))):
                        continue                              # window-less event, or window with no real %
                    if best is None or ts.timestamp() > best[0]:
                        best = (ts.timestamp(), rl)
        except Exception:
            continue
        if best:
            return best
    return None

def _codex_window_ok(w):
    """A usable window: a dict carrying a real numeric utilization. A window object with a null/absent
    used_percent isn't a reading — accepting it would render as a bogus green 0%."""
    return isinstance(w, dict) and isinstance(w.get("used_percent"), (int, float))

def codex_windows(rl):
    """The present windows as raw {label, minutes, pct, resets_at}, sorted short→long. Expiry is NOT
    baked in here: a reading can sit in the registry across refreshes, so whether a window has rolled
    over is decided live at display time (codex_display_windows), never frozen at capture."""
    wins = []
    for slot in ("primary", "secondary"):
        w = rl.get(slot)
        if not _codex_window_ok(w): continue
        ra = w.get("resets_at")
        wins.append({"label": codex_window_label(w.get("window_minutes")),
                     "minutes": w.get("window_minutes"),
                     "pct": float(w["used_percent"]),
                     "resets_at": datetime.fromtimestamp(ra, timezone.utc).isoformat()
                                  if isinstance(ra, (int, float)) else None})
    wins.sort(key=lambda x: x.get("minutes") or 0)
    return wins

def codex_display_windows(row):
    """Windows resolved for display: a window whose reset has passed reports a percentage the session
    log never cleared, so we blank it (expired, pct None) rather than show a number that isn't true."""
    now = time.time(); out = []
    for w in row.get("windows", []):
        dt = parse_dt(w.get("resets_at"))
        expired = dt is not None and dt.timestamp() <= now
        out.append({**w, "expired": expired, "pct": None if expired else w.get("pct")})
    return out

def load_codex_index():
    try:
        with open(CODEX_INDEX) as f: d = json.load(f)
        return d if isinstance(d, dict) else {}      # a tampered file that parses as a list must not crash
    except Exception:
        return {}

def save_codex_index(idx):
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        tmp = CODEX_INDEX + ".tmp"
        with open(tmp, "w") as f: json.dump(idx, f, indent=2)
        os.replace(tmp, CODEX_INDEX)
    except Exception:
        pass

def codex_label(email, name, aid):
    if email and "@" in email: return email.split("@")[0]
    return name or (aid[:8] if aid else "codex")

def collect_codex(persist=True):
    """Codex rows for every known account. The signed-in account (auth.json) is refreshed from the
    latest session; accounts seen before but not currently signed in render from their last snapshot
    (marked stale by age). Registry is keyed by account_id, so rotating the auth.json slot accretes
    accounts the same way the Claude side does."""
    idx = load_codex_index()
    live_aid = None
    try:
        with open(CODEX_AUTH) as f: auth = json.load(f)
    except Exception:
        auth = None
    tokens = auth.get("tokens") if isinstance(auth, dict) else None
    if isinstance(tokens, dict):
        claims = _jwt_claims(tokens.get("id_token"))
        oauth  = claims.get("https://api.openai.com/auth")
        oauth  = oauth if isinstance(oauth, dict) else {}
        aid = tokens.get("account_id") or oauth.get("chatgpt_account_id")
        if aid:
            live_aid = aid
            prev = idx.get(aid) if isinstance(idx.get(aid), dict) else {}
            prev_as_of = prev.get("as_of") if isinstance(prev.get("as_of"), (int, float)) else 0
            # identity always refreshes from auth.json; usage only when the scan turns up a reading at
            # least as new as the stored one — a window-less stretch must not erase the last real figure.
            entry = {"account_id": aid, "email": claims.get("email") or "",
                     "name": claims.get("name") or "", "plan": oauth.get("chatgpt_plan_type") or prev.get("plan")}
            best = codex_latest_usage()
            if best and best[0] >= prev_as_of:
                ts, rl = best
                entry["windows"] = codex_windows(rl)
                entry["as_of"]   = ts
                entry["plan"]    = rl.get("plan_type") or entry["plan"]
                cr = rl.get("credits") or {}
                entry["credits"] = {"has": bool(cr.get("has_credits")),
                                    "unlimited": bool(cr.get("unlimited")), "balance": cr.get("balance")}
            idx[aid] = {**prev, **entry}
            if persist: save_codex_index(idx)
    rows = []
    for aid, e in idx.items():
        if not isinstance(e, dict): continue         # skip a tampered/foreign registry entry
        wins = e.get("windows") or []
        rows.append({
            "provider": "codex", "uuid": aid, "account_id": aid,
            "email": e.get("email") or aid, "label": codex_label(e.get("email"), e.get("name"), aid),
            "plan": e.get("plan"), "windows": wins, "as_of": e.get("as_of"),
            "credits": e.get("credits"), "active": aid == live_aid,
            "error": None if (wins or e.get("as_of")) else "no usage recorded yet — run codex once",
        })
    return rows

# ---- rendering --------------------------------------------------------------

def rel(dt):
    if not dt: return "?"
    secs = (dt - datetime.now(timezone.utc)).total_seconds()
    if secs <= 0: return "now"
    d, rem = divmod(int(secs), 86400); h, rem = divmod(rem, 3600); m = rem // 60
    if d: return f"{d}d {h}h"
    if h: return f"{h}h {m}m"
    return f"{m}m"

def local_short(dt):
    """Compact local time: 'Tue 5pm', 'Mon 6:59am' (drops :00 on the hour, lowercase am/pm)."""
    d = dt.astimezone()
    mins = f":{d.strftime('%M')}" if d.minute else ""
    return f"{d.strftime('%a')} {d.strftime('%-I')}{mins}{d.strftime('%p').lower()}"

def resets_phrase(resets_at, style="week"):
    """Reset text. style='short' (5-hour): countdown only. style='week': 'Tue 5pm · 2d 11h left'.
    A null resets_at means the window hasn't started (e.g. 5-hour at 0%)."""
    dt = parse_dt(resets_at)
    if not dt:
        return "idle" if style == "short" else "no active window"
    if style == "short":
        return f"{rel(dt)} left"
    return f"{local_short(dt)} · {rel(dt)} left"

def wk_phrase(wk, style="week"):
    """resets_phrase for the weekly window, marking a projected boundary with a leading ~ so it never
    reads as a time the endpoint reported."""
    wk = wk or {}
    s = resets_phrase(wk.get("resets_at"), style)
    return f"~{s}" if wk.get("projected") else s

def week_abs_label(wk):
    """The absolute weekly reset, labeled — shown on the first scoped (e.g. Fable) row, which shares it."""
    dt = parse_dt((wk or {}).get("resets_at"))
    if not dt: return ""
    return f"weekly resets {'~' if (wk or {}).get('projected') else ''}{local_short(dt)}"

def bar(pct, width=10):
    pct = pct or 0
    fill = int(round(pct / 100 * width))
    return "█" * fill + "░" * (width - fill)

C = {"g":"\033[32m","y":"\033[33m","r":"\033[31m","dim":"\033[2m","b":"\033[1m","x":"\033[0m","cyan":"\033[36m"}
def color(pct):
    if pct is None: return C["dim"]
    if pct >= 90: return C["r"]
    if pct >= 65: return C["y"]
    return C["g"]

def plan_name(row):
    """Human plan label from the profile fields — never assumes a plan."""
    if row.get("is_team"):
        return "Enterprise" if row.get("org_type") == "claude_enterprise" else "Team"
    t = (row.get("tier") or "").lower()
    if "max_20x" in t or "max20" in t: return "Max 20x"
    if "max_5x"  in t or "max5"  in t: return "Max 5x"
    if "max"     in t: return "Max"
    if "pro"     in t: return "Pro"
    return row.get("tier") or ""      # show raw tier if unrecognized, rather than guess

def usd(minor):
    return None if minor is None else f"${minor/100:.0f}"

def sort_rows(rows):
    # stable alphabetical order so the list doesn't reshuffle as reset times change
    return sorted(rows, key=lambda r: (r.get("label") or r.get("email") or "").lower())

PROVIDERS = [("claude", "Claude"), ("codex", "Codex")]

def by_provider(rows):
    """Rows grouped as [(key, name, rows)] in Claude-then-Codex order, empty groups dropped."""
    out = []
    for key, name in PROVIDERS:
        g = sort_rows([r for r in rows if r.get("provider", "claude") == key])
        if g: out.append((key, name, g))
    return out

def plan_of(row):
    return codex_plan_name(row.get("plan")) if row.get("provider") == "codex" else plan_name(row)

def codex_ring_spec(rows):
    """Menu-bar (ring pct, pie pct) for the Codex account you're signed into: the ring is the longest
    window (weekly), the centre pie the shortest (5-hour) when the account has more than one. Windows
    arrive sorted short→long, so the ends of the list are those two. Parked (non-active) Codex
    snapshots are shown in the dropdown but never drive the title, so the ring always reflects the
    provider you're actually on. None when neither has a live reading — callers rely on a returned
    pair carrying at least one number."""
    active = next((r for r in rows if r.get("provider") == "codex" and r.get("active")), None)
    if not active: return None
    wins = codex_display_windows(active)
    ring = wins[-1]["pct"] if wins else None
    pie = wins[0]["pct"] if len(wins) > 1 else None
    return None if ring is None and pie is None else (ring, pie)

def _col_widths(rows):
    """Label/email column widths that fit the actual names, so the plan column lines up down both
    sections. Capped so one long address can't shove everything off the right edge."""
    ok = [r for r in rows if not r.get("error")]
    lw = min(16, max([6] + [len(r.get("label") or "") for r in ok]))
    ew = min(26, max([6] + [len(r.get("email") or "") for r in ok]))
    return lw, ew

def _table_claude_row(r, w):
    lw, ew = w
    # Name at col 2 under the provider header, matching the Codex section.
    head = (f"  {C['b']}{r['label']:<{lw}}{C['x']} {C['dim']}{r['email']:<{ew}}{C['x']}"
            f"  {C['cyan']}{plan_of(r)}{C['x']}")
    if r.get("active"): head += f"  {C['cyan']}[active]{C['x']}"
    print(head)
    if r.get("error"):
        print(f"    {C['r']}{r['error']}{C['x']}\n"); return
    fh, wk = r["five_hour"], r["seven_day"]
    fp, wp = fh["pct"] or 0, wk["pct"] or 0
    scoped = [s for s in r.get("scoped", []) if s.get("pct") is not None]
    wk_meta = wk_phrase(wk, 'short') if scoped else wk_phrase(wk, 'week')
    print(f"    5-hour  {color(fp)}{bar(fp)} {str(int(fp)).rjust(3)}%{C['x']}   "
          f"{C['dim']}{resets_phrase(fh['resets_at'], 'short')}{C['x']}")
    print(f"    weekly  {color(wp)}{bar(wp)} {str(int(wp)).rjust(3)}%{C['x']}   "
          f"{C['dim']}{wk_meta}{C['x']}")
    for i, s in enumerate(scoped):
        lbl = f"   {week_abs_label(wk)}" if i == 0 else ""
        print(f"    {C['dim']}{(s['model'] or 'scoped'):<7} {bar(s['pct'])} {str(int(s['pct'])).rjust(3)}%{lbl}{C['x']}")
    sp = r.get("spend") or {}
    if sp.get("enabled") and sp.get("limit"):   # extra-usage credits, only when turned on
        print(f"    {C['dim']}extra   {usd(sp['used'])} / {usd(sp['limit'])} used{C['x']}")
    print()

def _table_codex_row(r, show_active, w):
    lw, ew = w
    head = (f"  {C['b']}{r['label']:<{lw}}{C['x']} {C['dim']}{r['email']:<{ew}}{C['x']}"
            f"  {C['cyan']}{plan_of(r)}{C['x']}")
    if show_active and r.get("active"): head += f"  {C['cyan']}[signed in]{C['x']}"
    print(head)
    if r.get("error"):
        print(f"    {C['r']}{r['error']}{C['x']}\n"); return
    for w in codex_display_windows(r):
        if w.get("expired"):
            print(f"    {C['dim']}{w['label']:<7} {bar(0)}   —   window reset — run codex to refresh{C['x']}")
        else:
            pct = w["pct"] or 0
            print(f"    {w['label']:<7} {color(pct)}{bar(pct)} {str(int(pct)).rjust(3)}%{C['x']}   "
                  f"{C['dim']}{resets_phrase(w['resets_at'], 'week')}{C['x']}")
    cr = r.get("credits") or {}
    if cr.get("has") and cr.get("balance") not in (None, "0"):
        print(f"    {C['dim']}credits {cr['balance']}{C['x']}")
    print()

def render_table(rows):
    if not rows:
        print(f"\n{C['b']}Usage{C['x']}\n")
        print(f"{C['y']}No accounts found.{C['x']} Log in with the `claude` CLI "
              f"(`claude` → /login), then run this again.")
        print(f"{C['dim']}It reads the account the CLI is signed into; log into each of your "
              f"accounts once to add them all.{C['x']}\n")
        return
    def is_claude(r): return r.get("provider", "claude") == "claude"   # pre-Codex cached rows have no field
    groups = by_provider(rows)
    print(f"\n{C['b']}Usage{C['x']}  {C['dim']}· {datetime.now().astimezone().strftime('%-I:%M %p')}{C['x']}\n")
    multi = len(groups) > 1   # only label the sections when there's more than one provider to tell apart
    w = _col_widths(rows)     # shared across sections so the plan column lines up throughout
    for key, name, grp in groups:
        if multi:
            print(f"{C['dim']}── {name} " + "─" * (56 - len(name)) + f"{C['x']}")
        if key == "codex":
            show_active = len(grp) > 1
            for r in grp: _table_codex_row(r, show_active, w)
        else:
            for r in grp: _table_claude_row(r, w)
    if any(r.get("stale") for r in rows if is_claude(r)):
        print(f"{C['y']}⚠ Showing last known values — the usage API rate-limited this refresh.{C['x']}\n")
    n = len([r for r in rows if is_claude(r) and not r.get("is_team")])
    if n <= 1:
        lead = "No personal accounts tracked yet" if n == 0 else "Only one account tracked so far"
        print(f"{C['dim']}{lead} — log into your other accounts with the `claude` CLI "
              f"(`claude` → /login) to add them.{C['x']}\n")

def render_json(rows):
    print(json.dumps({"accounts": sort_rows(rows),
                      "generated_at": datetime.now(timezone.utc).isoformat()}, indent=2))

# ---- menu-bar icon (dynamic ring gauges) ------------------------------------
# xbar/SwiftBar render a base64 PNG placed after `| image=` on the title line. We draw one gauge per
# active provider — Claude, then Codex. Each gauge carries the account's two windows separately: the
# ring is the weekly window, filled clockwise from 12 o'clock, and a pie in its centre is the 5-hour
# window, filled the same way — both tinted green/amber/red, so slow budget and burst budget each
# read at a glance before the numbers are. Pure stdlib: a hand-rolled PNG writer plus a supersampled rasteriser, no Pillow
# dependency. Any failure returns None and the title falls back to the emoji dot.

RING_RGB = {"g": (63, 185, 80), "y": (217, 161, 59), "r": (229, 83, 75), "dim": (130, 138, 148)}

def _ring_rgb(pct):
    if pct is None: return RING_RGB["dim"]
    if pct >= 90:   return RING_RGB["r"]
    if pct >= 65:   return RING_RGB["y"]
    return RING_RGB["g"]

def _png(w, h, rgba, ppm=0):
    import struct, zlib
    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xffffffff)
    raw, row = bytearray(), w * 4
    for y in range(h):
        raw.append(0)                                   # per-scanline filter: none
        raw += rgba[y * row:(y + 1) * row]
    out = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
    if ppm:                                             # pHYs: physical resolution, so a retina display
        out += chunk(b"pHYs", struct.pack(">IIB", ppm, ppm, 1))   # draws the extra pixels at half the points
    return out + chunk(b"IDAT", zlib.compress(bytes(raw), 9)) + chunk(b"IEND", b"")

def menu_icon_b64(specs, scale=4):
    """specs: one (ring pct, pie pct) pair per gauge — ring is the weekly window, centre pie the
    5-hour (each 0-100, or None: a dim ring / no pie). Returns a base64 PNG, or None on any failure. `scale`
    is the pixel density: higher = crisper on a retina menu bar (xbar draws the bitmap near 1:1, so it
    needs the extra pixels), at the cost of a larger bitmap."""
    if not specs: return None
    try:
        import math, base64
        SS = 3                                          # supersample, then box-downsample for smooth edges
        D, GAP, TH, RPIE, PAD = 10, 3, 1.7, 2.2, 1      # ring diameter / gap / thickness / centre-pie radius / padding, in points
        n = len(specs)
        W = (PAD * 2 + n * D + (n - 1) * GAP) * scale
        H = (PAD * 2 + D) * scale
        rw, rh = W * SS, H * SS
        px = bytearray(rw * rh * 4)
        def clamp01(p): return max(0.0, min(1.0, (p or 0) / 100.0))
        for i, (pct, pie_pct) in enumerate(specs):
            r0, g0, b0 = _ring_rgb(pct)
            frac = clamp01(pct)
            cx = (PAD + D / 2 + i * (D + GAP)) * scale * SS
            cy = (PAD + D / 2) * scale * SS
            rO = (D / 2) * scale * SS
            rI = (D / 2 - TH) * scale * SS
            rP = RPIE * scale * SS
            p0, p1, p2 = _ring_rgb(pie_pct)
            # rounded caps: a filled disc of radius TH/2 at each end of the arc (top, and the frac angle).
            rM, capR = (rO + rI) / 2, (rO - rI) / 2
            th = frac * 2 * math.pi
            sX, sY = cx, cy - rM
            eX, eY = cx + rM * math.sin(th), cy - rM * math.cos(th)
            pie_frac = clamp01(pie_pct)
            for y in range(max(0, int(cy - rO - 1)), min(rh, int(cy + rO + 2))):
                for x in range(max(0, int(cx - rO - 1)), min(rw, int(cx + rO + 2))):
                    dx, dy = x - cx, y - cy
                    d = math.hypot(dx, dy)
                    in_pie = pie_pct is not None and d <= rP
                    if not in_pie and (d > rO or d < rI): continue
                    a = (math.atan2(dx, -dy) % (2 * math.pi)) / (2 * math.pi)   # 0 at top, clockwise
                    o = (y * rw + x) * 4
                    if in_pie:
                        # centre pie: the 5-hour window — a wedge filled clockwise from 12 o'clock by
                        # its pct, the rest a faint track, mirroring the ring's fill language.
                        px[o], px[o + 1], px[o + 2], px[o + 3] = p0, p1, p2, (255 if a <= pie_frac else 55)
                        continue
                    fill = a <= frac
                    if not fill and 0 < frac < 1:        # round the two ends
                        fill = math.hypot(x - sX, y - sY) <= capR or math.hypot(x - eX, y - eY) <= capR
                    px[o], px[o + 1], px[o + 2], px[o + 3] = r0, g0, b0, (255 if fill else 55)
        out = bytearray(W * H * 4)                      # alpha-weighted downsample (clean anti-aliased edges)
        for y in range(H):
            for x in range(W):
                r = g = b = a = 0
                for sy in range(SS):
                    base = ((y * SS + sy) * rw + x * SS) * 4
                    for sx in range(SS):
                        o = base + sx * 4; aa = px[o + 3]
                        r += px[o] * aa; g += px[o + 1] * aa; b += px[o + 2] * aa; a += aa
                oo = (y * W + x) * 4
                if a:
                    out[oo], out[oo + 1], out[oo + 2] = r // a, g // a, b // a
                out[oo + 3] = a // (SS * SS)
        # Declare the bitmap at 36·scale DPI so the display size is scale-invariant (matching what a
        # plain 72-DPI scale-2 image showed) while the extra pixels land as retina sharpness.
        ppm = round(36 * scale / 0.0254)
        return base64.b64encode(_png(W, H, out, ppm)).decode()
    except Exception:
        return None

def xb(s):
    """Make server-supplied text safe to place in an xbar menu line.

    `|` separates a line's text from its parameters, and these lines carry `bash=`/`param1=` on a
    clickable item — so a `|` arriving in a display name or model name could extend the parameter
    list rather than the text. Newlines would split one row into several.
    """
    return re.sub(r"[|\r\n]", "/", str(s))

def render_xbar(rows):
    if not rows:
        print("Claude · —")
        print("---")
        print("No accounts found — run: claude → /login | color=#d9a13b font=Menlo size=12")
        print("Refresh now | refresh=true")
        return
    claude_rows = sort_rows([r for r in rows if r.get("provider", "claude") == "claude"])
    xlw = min(14, max([6] + [len(r.get("label") or "") for r in rows if not r.get("error")]))  # align plan col
    # xbar trims leading whitespace from a menu title, and its trim set is the Unicode Zs category —
    # which includes the regular space AND the non-breaking space, so neither indents. U+2800 (the blank
    # Braille cell) renders as empty space but is category So, not Zs, so it survives the trim. All
    # dropdown indentation is built from it, which is what finally makes the tabbed hierarchy show.
    NB = "\u2800"
    # menu-bar title: just the ring gauges — one per active provider (Claude, then Codex). Each ring is
    # that account's weekly window; the pie in its centre is the 5-hour window. No numbers; fill +
    # severity carry the state, position carries which provider. If the PNG can't be built, fall back
    # to one severity dot per provider, colored by its worst window (still no numbers).
    def has_usage(r): return not r.get("error") and (r.get("five_hour") or {}).get("pct") is not None
    def dot(p): return "🟢" if p < 65 else ("🟡" if p < 90 else "🔴")
    # the gauge tracks the account you're actually on, matching the Codex rule below
    head = next((r for r in claude_rows if r.get("active") and has_usage(r)), None)
    specs = []
    if head:
        specs.append((head["seven_day"]["pct"] or 0, head["five_hour"]["pct"] or 0))
    cs = codex_ring_spec(rows)
    if cs is not None:
        specs.append(cs)
    img = menu_icon_b64(specs)
    if img:
        print(f"|image={img}")
    elif specs:
        # worst window of each gauge's pair, same order as the rings
        print("".join(dot(max(v for v in s if v is not None)) for s in specs))
    else:
        print("🔴" if any(has_usage(r) for r in claude_rows) else "Claude · ⏳")
    print("---")
    def barline(label, pct, meta=""):
        # Severity belongs to the gauge, not the clock: the bar and % carry the red/amber/green,
        # while the label and the reset text stay the menu's default color. That needs two colors on
        # one line, which `color=` can't do (it paints the whole item) — hence ANSI spans.
        # Detail lines sit at the SAME indent as the account name (one cell under the provider header) —
        # the name row and its bars share a left edge.
        tail = f"  · {meta}" if meta else ""
        print(f"{NB}{label:<6} {color(pct)}{bar(pct)} {int(pct):>3}%{C['x']}{tail} "
              f"| font=Menlo size=12 ansi=true")
    def xbar_claude_row(r):
        # Account name one cell under the provider header, matching the Codex section.
        lead = NB
        act  = " ·active" if r.get("active") else ""
        # Click-to-switch, no confirm — the account row itself is the target.
        switchable = not r.get("active") and not r.get("error")
        hint = "  ⇄" if switchable else ""      # the only thing marking a row as clickable
        # macOS draws an actionless menu item at reduced alpha, so the active row reads dimmer than
        # the switchable ones. `·active` and the menu-bar title carry the signal instead.
        params = "font=Menlo size=13"
        if switchable:
            params += (f' bash="{os.path.realpath(__file__)}" param1=switch param2={r["uuid"]}'
                       f" terminal=false refresh=true")
        print(f"{lead}{xb(r['label']):<{xlw}}  {xb(plan_of(r))}{act}{hint} | {params}")
        if switchable:   # holding ⌥ swaps the row for what the click actually does
            print(f"{lead}⇄ Switch to {xb(r['label'])} | alternate=true {params}")
        if r.get("error"):
            print(f"{NB}{xb(r['error'])} | color=#e5534b font=Menlo size=12"); print("---"); return
        fh, wk = r["five_hour"], r["seven_day"]
        fp, wp = fh["pct"] or 0, wk["pct"] or 0
        scoped = [s for s in r.get("scoped", []) if s.get("pct") is not None]
        barline("5-hour", fp, resets_phrase(fh["resets_at"], "short"))
        barline("weekly", wp, wk_phrase(wk, "short") if scoped else wk_phrase(wk, "week"))
        for i, s in enumerate(scoped):
            barline(xb(s["model"] or "scoped")[:6], s["pct"], week_abs_label(wk) if i == 0 else "")
        sp = r.get("spend") or {}
        if sp.get("enabled") and sp.get("limit"):
            print(f"{NB}extra  {usd(sp['used'])} / {usd(sp['limit'])} used | color=#8b949e font=Menlo size=12")
        print("---")
    def xbar_codex_row(r, show_active):
        # No switch affordance: this tool reads Codex, it doesn't drive `codex login`. The absence of ⇄
        # under the CODEX header reads as a property of the group, which is why the grouping earns its line.
        # Name and window rows share the same one-cell indent, matching the Claude section.
        act = " ·signed in" if show_active and r.get("active") else ""
        print(f"{NB}{xb(r['label']):<{xlw}}  {xb(plan_of(r))}{act} | font=Menlo size=13")
        if r.get("error"):
            print(f"{NB}{xb(r['error'])} | color=#e5534b font=Menlo size=12"); print("---"); return
        for w in codex_display_windows(r):
            if w.get("expired"):
                print(f"{NB}{xb(w['label'])[:6]:<6} window reset — run codex to refresh | color=#8b949e font=Menlo size=12")
            else:
                barline(xb(w["label"])[:6], w["pct"] or 0, resets_phrase(w["resets_at"], "week"))
        cr = r.get("credits") or {}
        if cr.get("has") and cr.get("balance") not in (None, "0"):
            print(f"{NB}credits {xb(cr['balance'])} | color=#8b949e font=Menlo size=12")
        print("---")
    groups = by_provider(rows)
    multi = len(groups) > 1
    for key, name, grp in groups:
        if multi:
            print(f"{name.upper()} | color=#8b949e font=Menlo size=11")
            print("---")   # divider under the header, matching the one after every account below it
        if key == "codex":
            show_active = len(grp) > 1
            for r in grp: xbar_codex_row(r, show_active)
        else:
            for r in grp: xbar_claude_row(r)
    if any(r.get("stale") for r in claude_rows):
        print("⚠ last known values — rate-limited; updates on the next refresh | color=#d9a13b font=Menlo size=12")
    if len([r for r in claude_rows if not r.get("is_team")]) <= 1:
        print("＋ Log into another account with the claude CLI (claude → /login) to track it | color=#8b949e font=Menlo size=12")
    prob = last_problem()      # only failures reach the menu; success is visible in the bar itself
    if prob:
        print(f"⚠ {xb(prob.get('msg',''))} | color=#d9a13b font=Menlo size=11")
    ts = data_ts()
    upd = datetime.fromtimestamp(ts).strftime("%-I:%M:%S %p") if ts else "—"
    _, iv = find_plugin_link()
    cadence = f"every {iv}" if iv else "on a timer"
    print(f"Updated {upd} · auto-refreshes {cadence} | color=#8b949e font=Menlo size=11")
    print("↻ Refresh now | refresh=true")
    if iv:
        print(f"⏱ Refresh every · {iv} | font=Menlo size=11 refresh=true")
        for opt in INTERVALS:
            mark = "✓" if opt == iv else "  "
            print(f'--{mark} {opt} | bash="{os.path.realpath(__file__)}" param1=interval param2={opt}'
                  f" terminal=false refresh=true font=Menlo size=11")

# ---- account switching ------------------------------------------------------

# the pre-switch credential lives in the Keychain like every other secret — never in a file
PREV_KEY = "__previous__"
# A switch moves the credential and the profile together. When only the credential lands, the CLI
# spends one account under another's name, so the partial result is reported rather than announced
# as a clean success — including through the menu, which has no stdout to print to.
MISMATCH_NOTE = " — but ~/.claude.json still names the other account, so the CLI will show that name"
ACTION   = os.path.join(STATE_DIR, "last-problem.json")   # non-secret: just an error message
LEGACY_ACTION = os.path.join(STATE_DIR, "last-action.json")   # pre-rename name; swept on write

def record_problem(msg, kind="action"):
    """Clicked menu items can't print and notifications may be suppressed — leave a *failure*
    where the next render can show it (the click refreshes, so it appears immediately).

    Only failures: a success already shows in the bar itself — the account row gains ·active,
    the title changes, the interval tick moves — so echoing it adds nothing.
    """
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(ACTION, "w") as f: json.dump({"ts": time.time(), "kind": kind, "msg": msg}, f)
    except Exception:
        pass
    try: os.remove(LEGACY_ACTION)      # left by versions that wrote successes here too
    except Exception: pass

def clear_problem(kind="action"):
    """Drop the stale note once *that same* action succeeds. Scoped by kind: a working interval
    change says nothing about a failed switch, and clearing it would erase a message the user
    hasn't read yet."""
    cur = last_problem(max_age=float("inf"))
    if cur and cur.get("kind", "action") != kind:
        return
    try: os.remove(ACTION)
    except Exception: pass

def last_problem(max_age=90):
    try:
        with open(ACTION) as f: a = json.load(f)
        return a if time.time() - a.get("ts", 0) < max_age else None
    except Exception:
        return None

def _fail(msg, kind="action"):
    record_problem(msg, kind)          # surfaced in the menu on the next render
    print(msg, file=sys.stderr); sys.exit(1)

def _report_switch(msg, note):
    """Announce a switch that went through. A note means it went through only in part, which the
    menu has to be told about — a clicked item can't print."""
    if note:
        record_problem(msg + note, "switch")
    else:
        clear_problem("switch")
    print(msg + note)

ACCT_RE = re.compile(r'^\s*"acct"<blob>=(?:0x([0-9A-Fa-f]+)\s+)?"(.*)"\s*$')

def live_account_attr():
    """The Keychain 'account' attribute on Claude Code's item, so a write updates the SAME item.

    `security` prints a non-ASCII value as `0x<hex>  "escaped"`, so the hex form is decoded when
    present. Getting this wrong doesn't fail loudly: `-U` would match no existing item and add a
    *second* credential under the same service, after which reads by service alone resolve to
    either one.
    """
    r = _sec(["find-generic-password", "-s", LIVE_SVC])
    for line in (r.stdout + "\n" + r.stderr).splitlines():
        m = ACCT_RE.match(line)
        if not m: continue
        if m.group(1):
            try: return bytes.fromhex(m.group(1)).decode()
            except Exception: pass
        return m.group(2)
    return getpass.getuser()      # Claude Code uses the macOS username

CRED_FILE = os.path.expanduser("~/.claude/.credentials.json")

# ---- the live profile -------------------------------------------------------
# The OAuth token says who you *are*; ~/.claude.json's oauthAccount is a cached copy of the profile
# that Claude Code shows and reads its plan from. They are written independently, so switching only
# the credential leaves the CLI spending the new account under the old account's name. Both move
# together here.

CLAUDE_JSON = os.path.expanduser("~/.claude.json")
PROFILE_KEY = "oauthAccount"
PREV_PROFILE = os.path.join(STATE_DIR, "previous-profile.json")   # non-secret: profile fields only

def read_live_profile():
    """Claude Code's cached profile for the live account, or None."""
    try:
        with open(CLAUDE_JSON) as f:
            p = json.load(f).get(PROFILE_KEY)
        return p if isinstance(p, dict) and p.get("accountUuid") else None
    except Exception:
        return None

def write_live_profile(profile):
    """Replace only oauthAccount, leaving the rest of ~/.claude.json byte-for-byte intact in value.

    Read-modify-write on a file that running sessions also write is a race we can lose, so the
    window is kept to one read and one atomic replace, and the file is never created from nothing:
    a missing or unparseable ~/.claude.json means Claude Code owns a state we can't reconstruct.
    """
    try:
        with open(CLAUDE_JSON) as f:
            cur = json.load(f)
        if not isinstance(cur, dict):
            return False
        mode = os.stat(CLAUDE_JSON).st_mode & 0o777
    except Exception:
        return False
    cur[PROFILE_KEY] = profile
    tmp = CLAUDE_JSON + ".claude-usage.tmp"
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
        os.fchmod(fd, mode)           # the open() mode is masked by umask; this is what preserves it
        with os.fdopen(fd, "w") as f: json.dump(cur, f, indent=2)
        os.replace(tmp, CLAUDE_JSON)
        return True
    except Exception:
        try: os.remove(tmp)
        except Exception: pass
        return False

# /profile's fields, in Claude Code's spelling: (oauthAccount key, section, section key)
PROFILE_MAP = [
    ("accountUuid",                 "account",      "uuid"),
    ("emailAddress",                "account",      "email"),
    ("displayName",                 "account",      "display_name"),
    ("accountCreatedAt",            "account",      "created_at"),
    ("organizationUuid",            "organization", "uuid"),
    ("organizationName",            "organization", "name"),
    ("organizationType",            "organization", "organization_type"),
    ("organizationRateLimitTier",   "organization", "rate_limit_tier"),
    ("seatTier",                    "organization", "seat_tier"),
    ("billingType",                 "organization", "billing_type"),
    ("hasExtraUsageEnabled",        "organization", "has_extra_usage_enabled"),
    ("subscriptionCreatedAt",       "organization", "subscription_created_at"),
    ("ccOnboardingFlags",           "organization", "cc_onboarding_flags"),
    ("claudeCodeTrialEndsAt",       "organization", "claude_code_trial_ends_at"),
    ("claudeCodeTrialDurationDays", "organization", "claude_code_trial_duration_days"),
]

def derive_profile(prof, base=None):
    """An oauthAccount block for the account /profile just described. Every key /profile carries is
    taken from it, so a snapshot can't reinstate an old plan or org; a snapshot fills in only the
    keys /profile omits (organizationRole, workspaceRole), and only while it still describes the
    same org — those are org-scoped, so once the org differs they describe nothing. Returns None if
    the response has no account uuid to key it on."""
    out = {}
    for key, section, field in PROFILE_MAP:
        sec = prof.get(section) or {}
        if field in sec:
            out[key] = sec[field]
    if not out.get("accountUuid"):
        return None
    base = base or {}
    if base.get("organizationUuid") == out.get("organizationUuid"):
        for k, v in base.items():
            out.setdefault(k, v)
    out["profileFetchedAt"] = int(time.time() * 1000)
    return out

def live_store():
    """Where Claude Code keeps the live credential on this machine: 'keychain' or 'file'.

    Writing to the store it does *not* read is worse than not writing at all — it would leave the
    CLI on the old account while this tool reports the new one as active, permanently.
    """
    if keychain_read(LIVE_SVC): return "keychain"
    if os.path.exists(CRED_FILE): return "file"
    return None

def read_live_raw():
    if keychain_read(LIVE_SVC): return keychain_read(LIVE_SVC)
    try:
        with open(CRED_FILE) as f: return f.read()
    except Exception:
        return None

def write_live(blob):
    """Replace only the claudeAiOauth key — anything else Claude Code keeps survives. Writes back
    to whichever store the credential was read from. Returns True on success."""
    store = live_store()
    if store is None:
        return False
    try:
        cur = json.loads(read_live_raw() or "{}")
        if not isinstance(cur, dict): cur = {}
    except Exception:
        cur = {}
    cur["claudeAiOauth"] = blob
    if store == "keychain":
        return keychain_write(LIVE_SVC, live_account_attr(), json.dumps(cur))
    try:                              # 0600: the file holds a live OAuth token
        fd = os.open(CRED_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f: json.dump(cur, f)
        return True
    except Exception:
        return False

def resolve_account(key):
    k = (key or "").lower()
    if not k: return None
    for e in load_index():
        if k in (e["uuid"].lower(), e["email"].lower(), (e.get("label") or "").lower()):
            return e
    return None

def cmd_switch(target):
    if target == "--undo":
        prev = keychain_read(STORE_SVC, PREV_KEY)
        if not prev:
            print("nothing to undo", file=sys.stderr); sys.exit(1)
        try:
            blob = json.loads(prev).get("claudeAiOauth")
        except Exception:
            blob = None
        if not blob or not write_live(blob):
            _fail("couldn't restore the previous account — no writable credential store", "switch")
        keychain_delete(STORE_SVC, PREV_KEY)
        try:
            with open(PREV_PROFILE) as f: prev_prof = json.load(f)
        except Exception:
            prev_prof = None
        # A credential restored without its profile is the mismatch this pairing exists to prevent,
        # so a missing backup is as much a failure to report as a failed write.
        if not prev_prof:
            note = "" if not read_live_profile() else MISMATCH_NOTE
        elif write_live_profile(prev_prof):
            note = ""
            try: os.remove(PREV_PROFILE)
            except Exception: pass
        else:
            note = MISMATCH_NOTE
        clear_cache()
        _report_switch(f"restored the previous account", note); return

    e = resolve_account(target)
    if not e:
        _fail(f"unknown account: {target}", "switch")
    sec = load_secret(e["uuid"])
    if not sec or not sec.get("refreshToken"):
        _fail(f"{e['email']} isn't captured — log into it once with the claude CLI", "switch")
    if not sec.get("scopes"):
        # captured before we stored the full blob; writing a partial one could break the CLI login
        _fail(f"{e['email']} needs one login with the claude CLI to store its full credentials, "
              f"then switching will work", "switch")

    token, err = token_for_parked(e["uuid"])          # refreshes if the cached token is stale
    if err:
        _fail(f"can't switch to {e['email']}: {err}", "switch")
    sec = load_secret(e["uuid"])                      # re-read: the refresh may have rotated it

    blob = {"accessToken": sec.get("accessToken") or token,
            "refreshToken": sec.get("refreshToken"),
            "expiresAt": sec.get("expiresAt")}
    blob.update({k: sec[k] for k in BLOB_META if sec.get(k) is not None})   # never write nulls

    if live_store() is None:
        _fail("no Claude Code credential store found — sign in with the claude CLI first", "switch")
    cur = read_live_raw()                             # back up (to the Keychain) before overwriting
    if cur: keychain_write(STORE_SVC, PREV_KEY, cur)
    cur_prof = read_live_profile()                    # ditto for the profile, so --undo restores both
    if cur_prof:
        try:
            os.makedirs(STATE_DIR, exist_ok=True)
            with open(PREV_PROFILE, "w") as f: json.dump(cur_prof, f, indent=2)
        except Exception:
            pass
    if not write_live(blob):
        _fail(f"couldn't write the credential — switch to {e['email']} did not happen", "switch")

    # The credential is the switch; the profile is the label on it. A failure here leaves a working
    # switch that reads as the wrong account, which is worse to discover silently than to be told.
    note = ""
    if cur_prof:                      # nothing cached to correct means nothing to write
        try:
            new_prof = derive_profile(api_get(PROFILE_URL, token), e.get("profile"))
        except Exception:
            new_prof = e.get("profile")
        if not new_prof or not write_live_profile(new_prof):
            note = MISMATCH_NOTE

    clear_cache()                                     # so the post-switch refresh shows the new active account
    _report_switch(f"switched to {e['email']}", note)

# ---- menu-bar install -------------------------------------------------------

def _app_installed(bundle):
    return any(os.path.isdir(os.path.join(base, bundle))
               for base in ("/Applications", os.path.expanduser("~/Applications")))

def _read_default(domain, key):
    r = subprocess.run(["defaults", "read", domain, key], capture_output=True, text=True)
    v = r.stdout.strip()
    return os.path.expanduser(v) if r.returncode == 0 and v else None

def ensure_xbar():
    """Make sure a menu-bar host exists, offering to `brew install --cask xbar` if not. Returns bool."""
    if _app_installed("xbar.app") or _app_installed("SwiftBar.app"):
        return True
    if not shutil.which("brew"):
        print("  No menu-bar host found, and Homebrew isn't installed.")
        print("  Install xbar from https://xbarapp.com, then re-run `claude-usage install`.")
        return False
    if sys.stdin.isatty() and not _ask("  xbar isn't installed. Install it now with Homebrew?", True):
        print("  Skipped. Install later with:  brew install --cask xbar")
        return False
    print("  Installing xbar (brew install --cask xbar)…")
    if subprocess.run(["brew", "install", "--cask", "xbar"]).returncode != 0:
        print("  brew install failed — install xbar manually from https://xbarapp.com.")
        return False
    return True

_HOST_CACHE = []      # memo: the lookup forks `defaults`, and a render asks for it repeatedly

def host_and_plugin_dir():
    """(host name, its plugin folder) for the installed menu-bar host. Either may be None.

    Memoized per process: neither the installed host nor its plugin folder changes during a
    run, and the `defaults` fork is otherwise repeated on every render.
    """
    if _HOST_CACHE:
        return _HOST_CACHE[0]
    if _app_installed("xbar.app"):
        res = ("xbar", (_read_default("com.xbarapp.app", "pluginsDirectory")
                        or os.path.expanduser("~/Library/Application Support/xbar/plugins")))
    elif _app_installed("SwiftBar.app"):
        res = ("SwiftBar", _read_default("com.ameba.SwiftBar", "PluginDirectory"))
    else:
        res = (None, None)
    _HOST_CACHE.append(res)
    return res

def plugin_source():
    here = os.path.dirname(os.path.realpath(__file__))     # realpath: works via a PATH symlink too
    return os.path.join(here, PLUGIN_FILE)

def wrapper_path():
    """The PATH the plugin wrapper exports, read from the wrapper itself. The menu-bar host runs
    the plugin with a bare environment, so that line — not this process's PATH — decides which
    python3 the bar gets. Reading it keeps the check honest if the wrapper is ever edited."""
    try:
        with open(plugin_source()) as f:
            m = re.search(r'^export PATH="([^"]*)"', f.read(), re.M)
        return m.group(1).split(":") if m else None
    except Exception:
        return None

def plugin_python():
    """The python3 the wrapper will actually find, or None."""
    for d in wrapper_path() or []:
        p = os.path.join(d, "python3")
        if os.path.exists(p):
            return p
    return None

def find_plugin_links():
    """Every (link, interval) in the host's folder pointing at our plugin. The interval is the
    '.5m.' in the *link's* name, which is what the host reads. More than one means the host is
    running the plugin twice — two icons in the bar."""
    _, d = host_and_plugin_dir()
    if not d or not os.path.isdir(d):
        return []
    src = os.path.realpath(plugin_source())
    out = []
    for p in sorted(glob.glob(os.path.join(d, "claude-usage.*.sh"))):
        if os.path.realpath(p) == src:
            m = re.match(r"claude-usage\.([^.]+)\.sh$", os.path.basename(p))
            out.append((p, m.group(1) if m else None))
    return out

def find_plugin_link():
    """(link path, interval) of our plugin, or (None, None)."""
    links = find_plugin_links()
    return links[0] if links else (None, None)

def install_plugin():
    """Symlink the plugin into the host's folder (host must already exist). Returns 'xbar'/'SwiftBar' or None."""
    plugin = plugin_source()
    if not os.path.exists(plugin):
        print(f"  plugin not found next to the script: {plugin}"); return None
    os.chmod(plugin, 0o755)

    app, target = host_and_plugin_dir()
    if not app:
        print("  No menu-bar host found."); return None
    if not target:
        print(f"  {app} is installed but no plugin folder is set.")
        print(f"  Open {app} → Preferences, choose a plugin folder, then re-run.")
        return None

    os.makedirs(target, exist_ok=True)
    existing, _ = find_plugin_link()      # keep whatever interval the user already chose
    link = existing or os.path.join(target, f"claude-usage.{DEFAULT_INTERVAL}.sh")
    if os.path.islink(link) or os.path.exists(link):
        if os.path.realpath(link) == os.path.realpath(plugin):
            print(f"  ✓ already linked → {link}")
        else:
            os.remove(link); os.symlink(plugin, link); print(f"  ✓ relinked → {link}")
    else:
        os.symlink(plugin, link); print(f"  ✓ linked plugin → {link}")
    return app

def setup_menu_bar():
    """Install a host if needed, link the plugin, launch the host. Returns True if the bar is set up."""
    if not ensure_xbar():
        return False
    app = install_plugin()
    if not app:
        return False
    subprocess.run(["open", "-a", app], capture_output=True)
    print(f"  ✓ launched {app} — the bar should appear shortly (allow plugins if {app} prompts).")
    return True

def cmd_install():
    sys.exit(0 if setup_menu_bar() else 1)

def restart_host(app):
    """Bounce the menu-bar host so it re-scans the plugin folder.

    Detached and in its own session: this usually runs as a child of the host itself (a menu
    click), so it has to outlive the quit it just issued.

    The relaunch waits for the process to actually be gone rather than sleeping a fixed
    interval — `open -a` against a still-quitting app can be swallowed as "already running",
    which would leave no bar and, since the plugin link is already renamed, no menu to fix it
    from. The final `open` is unconditional so a hung quit still gets a running host.
    """
    script = (f'osascript -e \'quit app "{app}"\' >/dev/null 2>&1; '
              f'for i in $(seq 30); do pgrep -x {app} >/dev/null 2>&1 || break; sleep 0.2; done; '
              f'open -a "{app}"')
    subprocess.Popen(["/bin/sh", "-c", script], start_new_session=True,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

def cmd_interval(val):
    if val not in INTERVALS:
        _fail(f"interval must be one of: {', '.join(INTERVALS)}", "interval")
    app, _ = host_and_plugin_dir()
    links = find_plugin_links()
    if not links:
        _fail("the menu-bar plugin isn't linked — run `claude-usage install` first", "interval")
    if len(links) > 1:
        # renaming one of several would leave the rest running at their own cadences
        _fail("the plugin is linked more than once (" +
              ", ".join(os.path.basename(p) for p, _ in links) +
              ") — delete all but one, then set the interval", "interval")
    link, cur = links[0]
    if cur == val:
        clear_problem("interval")
        print(f"already refreshing every {val}"); return
    dest = os.path.join(os.path.dirname(link), f"claude-usage.{val}.sh")
    if os.path.lexists(dest):          # lexists: a dangling link still occupies the name
        if os.path.realpath(dest) != os.path.realpath(plugin_source()):
            _fail(f"{dest} already exists and isn't ours — move it aside and retry", "interval")
        os.remove(dest)                # a duplicate link of our own; the rename replaces it
    os.rename(link, dest)
    # The host keeps the plugin's path in memory and doesn't notice a rename — it would keep
    # exec'ing the old name and show a ⚠ icon. Restart it so it picks the new cadence up.
    clear_problem("interval")
    print(f"refreshing every {val} (restarting {app})")
    restart_host(app)      # a link was found, so a host exists

# ---- doctor -----------------------------------------------------------------

def cmd_doctor():
    """Check everything that has to line up for the bar to work, and name the fix for whatever doesn't."""
    counts = {"warn": 0, "bad": 0}
    def say(state, text, hint=None):
        icon, col = {"ok": ("✓", C["g"]), "warn": ("⚠", C["y"]), "bad": ("✗", C["r"])}[state]
        counts[state] = counts.get(state, 0) + 1
        print(f"  {col}{icon}{C['x']} {text}")
        if hint: print(f"      {C['dim']}{hint}{C['x']}")
    def section(name): print(f"\n{C['b']}{name}{C['x']}")

    print(f"\n{C['b']}claude-usage doctor{C['x']}")

    section("Environment")
    if shutil.which("security"):
        say("ok", "macOS Keychain (`security`) available")
    else:
        say("bad", "`security` not found — Keychain access won't work", "claude-usage is macOS-only.")
    v = sys.version_info
    if v >= (3, 8):
        say("ok", f"python3 {v.major}.{v.minor}.{v.micro}")
    else:
        say("bad", f"python3 {v.major}.{v.minor} is too old", "3.8 or newer is required.")

    section("Claude Code session")
    live = read_live()
    if not live:
        say("bad", "no signed-in account found",
            "run `claude` → /login, then re-run this.")
    else:
        src = ('Keychain item "Claude Code-credentials"' if keychain_read(LIVE_SVC)
               else "~/.claude/.credentials.json")
        say("ok", f"signed-in account found in {src}")
        if (live.get("expiresAt") or 0) < time.time() * 1000:
            say("warn", "its access token has expired",
                "Claude Code mints a new one on your next `claude` run.")

    section("Accounts")
    # ingest=False: a diagnostic reports the current state, it doesn't register the live account
    # or rewrite the index. Reading a parked account can still rotate its refresh token — that is
    # inherent to reading its usage at all.
    rows = collect(ingest=False)
    claude_rows = [r for r in rows if r.get("provider", "claude") == "claude"]   # Codex has its own section
    if not claude_rows:
        say("warn", "no accounts registered yet",
            "sign into each account once with `claude` → /login.")
    for r in sort_rows(claude_rows):
        if r.get("error"):
            say("bad", f"{r['email']}: {r['error']}")
        elif r.get("active"):
            say("ok", f"{r['email']}: usage reads OK (active)")
        elif not (load_secret(r["uuid"]) or {}).get("scopes"):
            say("warn", f"{r['email']}: usage reads OK, but switching to it won't work",
                "sign into it once with `claude` → /login to store its full credentials.")
        else:
            say("ok", f"{r['email']}: usage + switching OK")
    ts = data_ts()
    if ts and time.time() - ts > 3600:
        age = int((time.time() - ts) // 60)
        say("warn", f"usage numbers last fetched {age // 60}h {age % 60}m ago",
            "the menu-bar host may not be running.")

    section("Codex")
    if not os.path.exists(CODEX_AUTH):
        say("warn", "no Codex sign-in found",
            "optional — sign in with the codex CLI to track it here, or ignore if you don't use Codex.")
    else:
        codex_rows = [r for r in rows if r.get("provider") == "codex"]
        live = next((r for r in codex_rows if r.get("active")), None)
        if not live:
            say("warn", "Codex auth found but its account couldn't be read",
                "the codex CLI may be signed out; run `codex login`.")
        elif live.get("error"):
            say("warn", f"{live['email']}: {live['error']}", "run codex once so it logs a usage figure.")
        else:
            say("ok", f"{live['email']}: usage reads OK")
        for r in codex_rows:
            if not r.get("active") and not r.get("error"):
                say("warn", f"{r['email']}: shown from a past snapshot",
                    "sign back into it with codex to refresh.")

    section("Menu bar")
    app, pdir = host_and_plugin_dir()
    if not app:
        say("warn", "no menu-bar host installed", "run `claude-usage install` to add xbar.")
    elif not pdir:
        say("warn", f"{app} is installed but has no plugin folder set",
            f"open {app} → Preferences and choose one, then run `claude-usage install`.")
    else:
        links = find_plugin_links()
        if not links:
            say("warn", f"{app} is installed but the plugin isn't linked", "run `claude-usage install`.")
        elif len(links) > 1:
            say("warn", f"{len(links)} links to the plugin — {app} is running it once per link",
                "delete all but one: " + ", ".join(os.path.basename(p) for p, _ in links))
        else:
            say("ok", f"{app}: plugin linked, refreshing every {links[0][1]}", links[0][0])
        if subprocess.run(["pgrep", "-x", app], capture_output=True).returncode != 0:
            say("warn", f"{app} isn't running — the bar won't update", f"run: open -a {app}")
    py = plugin_python()
    if py:
        say("ok", f"the plugin's python3 resolves to {py}")
    elif wrapper_path() is None:
        say("bad", f"can't read the plugin's PATH from {PLUGIN_FILE}",
            "the wrapper is missing or unreadable; re-clone or restore it.")
    else:
        say("bad", "no python3 on the plugin's PATH — the bar will render empty")

    section("Shell")
    cu = os.path.expanduser("~/.local/bin/claude-usage")
    if os.path.islink(cu) and os.path.realpath(cu) == os.path.realpath(__file__):
        if os.path.dirname(cu) in os.environ.get("PATH", "").split(os.pathsep):
            say("ok", f"`claude-usage` on PATH → {cu}")
        else:
            say("warn", f"{cu} exists but ~/.local/bin isn't on PATH",
                'add to your shell rc:  export PATH="$HOME/.local/bin:$PATH"')
    else:
        say("warn", "`claude-usage` isn't linked into ~/.local/bin",
            "run `claude-usage setup` to add it.")

    bad, warn = counts["bad"], counts["warn"]
    tail = f", {warn} warning(s)" if warn else ""
    print()
    if bad:
        print(f"{C['r']}{bad} problem(s){tail}.{C['x']}\n"); sys.exit(1)
    if warn:
        print(f"{C['y']}No problems{tail}.{C['x']}\n")
    else:
        print(f"{C['g']}Everything checks out.{C['x']}\n")

# ---- guided setup -----------------------------------------------------------

def _ask(prompt, default=True):
    d = "Y/n" if default else "y/N"
    try:
        r = input(f"{prompt} [{d}] ").strip().lower()
    except EOFError:
        return default
    return default if not r else r.startswith("y")

def cmd_setup():
    if not sys.stdin.isatty():
        print("Run `claude-usage setup` in an interactive terminal.", file=sys.stderr); sys.exit(1)
    print("claude-usage setup\n")
    print("This will:")
    print("  • read the Claude account you're signed into (read-only) and register it")
    print("  • store each account's refresh token in your macOS Keychain — never in files or the repo")
    print("  • optionally add a menu-bar view (xbar / SwiftBar)")
    print("It never changes or signs out your Claude Code session.")
    print("If you use the Codex CLI, its usage shows automatically alongside — nothing to set up.\n")
    if not _ask("Proceed?", True):
        print("Aborted."); return

    # optional: put `claude-usage` on PATH
    self_path = os.path.realpath(__file__)
    local_bin = os.path.expanduser("~/.local/bin")
    link = os.path.join(local_bin, "claude-usage")
    already = os.path.islink(link) and os.path.realpath(link) == self_path
    if not already and _ask(f"Add `claude-usage` to {local_bin} so you can run it from anywhere?", True):
        os.makedirs(local_bin, exist_ok=True)
        if os.path.islink(link) or os.path.exists(link): os.remove(link)
        os.symlink(self_path, link)
        print(f"  ✓ linked {link}")
        if local_bin not in os.environ.get("PATH", "").split(os.pathsep):
            print('  add to your shell rc:  export PATH="$HOME/.local/bin:$PATH"')

    # optional: menu bar (the primary experience — offers to install xbar if missing)
    if _ask("Add the menu-bar view? (installs xbar if needed)", True):
        print("Menu bar:")
        setup_menu_bar()

    print("\nRegistering the account you're signed into…")
    rows = collect()
    render_table(rows)
    if rows:
        print("From now on, sign into another Claude account (CLI or desktop app) and it appears on")
        print("the next run — and in the menu bar on its next refresh. No need to run setup again.\n")

# ---- commands ---------------------------------------------------------------

def main():
    arg = sys.argv[1] if len(sys.argv) > 1 else ""
    if arg == "setup":
        cmd_setup(); return
    if arg == "install":
        cmd_install(); return
    if arg == "doctor":
        cmd_doctor(); return
    if arg == "interval":
        cmd_interval(sys.argv[2] if len(sys.argv) > 2 else ""); return
    if arg == "switch":
        cmd_switch(sys.argv[2] if len(sys.argv) > 2 else ""); return
    if arg == "capture":
        idx = load_index(); u = ingest_live(idx)
        if u:
            e = next(x for x in load_index() if x["uuid"] == u)
            print(f"captured {e['email']}")
        else:
            print("no active Claude Code account found in keychain", file=sys.stderr); sys.exit(1)
        return
    if arg == "list":
        for e in load_index():
            print(f"{e['email']:<28} {e.get('tier','')}  {e['uuid']}")
        return
    if arg == "forget":
        key = sys.argv[2] if len(sys.argv) > 2 else ""
        idx = load_index()
        k = (key or "").lower()
        gone = [e for e in idx if k and k in (e["uuid"].lower(), e["email"].lower())]
        save_index([e for e in idx if e not in gone])
        stuck = [e for e in gone if not keychain_delete(STORE_SVC, e["uuid"])]
        print(f"forgot {len(gone)} account(s)")
        for e in stuck:   # index entry is gone; say so rather than leave an orphan token unmentioned
            print(f"  warning: couldn't delete {e['email']}'s Keychain item — remove it by hand",
                  file=sys.stderr)
        return
    rows = collect()
    if arg == "--json": render_json(rows)
    elif arg == "--xbar": render_xbar(rows)
    else: render_table(rows)

if __name__ == "__main__":
    main()
