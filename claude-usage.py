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

Safety: the active account is read live from Claude Code's own Keychain item, and
renewed only once its access token has already expired. Claude Code renews its own
credential before it lapses, and a desktop-hosted session authenticates through the
app instead of touching that item at all, so a token found past its expiry is one
nothing else is maintaining — there is no refresh to race and no working session to
desync. Rotated refresh tokens are written straight back to the Keychain, Claude
Code's copy first.

Codex: identity is read from ~/.codex/auth.json and usage from the newest Codex
session rollout that records a rate limit — no API call. Each row shows how old
its reading is, since Codex usage can't be refreshed without running codex.
Accounts are keyed by account_id, so rotating the auth.json slot accretes them
the same way Claude accounts accrue.

Codex switching works the same way from the outside, but the whole credential is
that one file: each account's auth.json is stashed in the Keychain as it is seen
and written back on the way in, with no refresh of our own — the codex CLI
refreshes the tokens on its next run. A session log doesn't name its account, so
usage is attributed to whoever is signed in; readings written before an account
took the auth.json slot are excluded, which is what keeps that attribution true
once accounts rotate.

Usage:
  claude-usage setup      guided first-time setup (register account, optional menu bar + PATH)
  claude-usage            table of all known accounts (default)
  claude-usage app        build + launch the menu-bar app (needs the Xcode Command Line Tools)
  claude-usage insights   trailing-week tokens + API-equivalent cost by model, from local transcripts
  claude-usage doctor     check the setup and report what needs fixing
  claude-usage --json     machine-readable JSON
  claude-usage capture    explicitly ingest the active account (same as a run)
  claude-usage list       list registered accounts
  claude-usage switch X   point the CLI at account X (email / label / uuid; Claude or Codex)
  claude-usage switch --undo   restore the account that was active before the last switch
  claude-usage relogin X  sign account X back in after the server signed it out
  claude-usage forget X   drop account by email or uuid
"""
import sys, os, re, json, glob, time, getpass, shlex, subprocess, shutil, urllib.request, urllib.error
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
# Re-renders inside this window reuse the last result rather than hitting the API. The panel
# refreshes every time it opens and a sweep costs one /usage call per registered account plus
# /profile, so the window has to be long enough that opening the menu a few times in a minute
# cannot multiply into account-count × opens requests and earn a 429. Usage moves slowly enough
# that two minutes of staleness is invisible.
COOLDOWN = 120

# ---- keychain helpers -------------------------------------------------------

def _sec(args, inp=None):
    return subprocess.run(["security", *args], capture_output=True, text=True, input=inp)

def keychain_read(service, account=None):
    args = ["find-generic-password", "-s", service, "-w"]
    if account: args = ["find-generic-password", "-s", service, "-a", account, "-w"]
    r = _sec(args)
    if r.returncode != 0 or not r.stdout.strip(): return None
    return _unhex(r.stdout.strip())

def _unhex(v):
    """`security -w` prints the secret as bare hex when it holds a newline, and verbatim otherwise.
    Every secret stored here is JSON, so a value that is pure hex — no braces, no quotes — is the
    encoded form and nothing else could be."""
    if len(v) % 2 or not all(c in "0123456789abcdefABCDEF" for c in v): return v
    try: return bytes.fromhex(v).decode()
    except Exception: return v

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

class GrantRevoked(RuntimeError):
    """The token endpoint rejected the refresh token itself (OAuth invalid_grant): revoked or
    expired server-side. Terminal — only a new sign-in can mint a replacement, so callers
    must not retry the same token."""

def refresh_token(refresh, host_hint=None):
    """Exchange a refresh token for a new access token. Returns (data, host).
    Raises GrantRevoked when the server says the token is dead; any other exception is
    transient (wrong host, outage, network) and worth retrying later."""
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
            # Only a 400 carries an OAuth error code (RFC 6749 §5.2). Bodies on other
            # statuses can come from proxies and gateways and must not be read as a
            # verdict on the grant.
            oauth_err = None
            if e.code == 400:
                try:
                    oauth_err = json.loads(e.read()).get("error")
                    if isinstance(oauth_err, dict):        # nested {"error": {"type": ...}}
                        oauth_err = oauth_err.get("type")
                except Exception:
                    pass
            last = f"HTTP {e.code}{f' ({oauth_err})' if oauth_err else ''} at {h}"
            if oauth_err == "invalid_grant":
                # the grant itself is dead: every host fronts the same OAuth service, so no
                # later host can answer differently — and none may bury this verdict
                raise GrantRevoked(last)
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
    if rec.get("refreshToken") != prev.get("refreshToken"):
        # the needs-login latch describes one dead grant; a different refresh token is a new
        # grant (a real sign-in or a rotation), so the latch must not outlive it
        rec.pop("needsLogin", None)
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

# The state only. Each surface names the remedy in the terms it can offer — a button in the
# menu bar, a command in the table and in doctor — and a remedy baked in here would be wrong
# wherever it isn't the one within reach.
NEEDS_LOGIN = "signed out by the server"

def token_for_parked(uuid, force=False, min_life_ms=60_000):
    """Valid access token for a parked account, refreshing + rotating if needed.
    force=True skips the cached access token (used to recover from a 401 on a token that
    looked unexpired but was invalidated server-side).

    min_life_ms is how much life a cached token must have left to be handed back. A read
    needs only enough to finish the call it is about to make; a caller that gives the token
    away — `switch`, writing it into the live credential — has to ask for enough that the
    account is still usable long after this process is gone."""
    sec = load_secret(uuid)
    if not sec: return None, "not captured — sign into it once and re-run"
    if sec.get("needsLogin"):
        # the stored grant is known dead (see below); only a fresh sign-in can revive the
        # account, so don't spend a network call finding that out again — force included,
        # since a retry with the same dead token can never come back different
        return None, NEEDS_LOGIN
    now_ms = time.time() * 1000
    if not force and sec.get("accessToken") and (sec.get("expiresAt") or 0) > now_ms + min_life_ms:
        return sec["accessToken"], None
    if not sec.get("refreshToken"):
        return None, "no refresh token — sign into it once and re-run"
    try:
        data, host = refresh_token(sec["refreshToken"], sec.get("tokenHost"))
    except GrantRevoked:
        # The server revoked this grant, so retrying it every tick can never succeed: latch
        # the account into needs-login until a real sign-in stores a new refresh token
        # (store_secret drops the latch when the token changes). Transient failures take
        # the branch below and keep retrying.
        cur = load_secret(uuid)
        if not cur or cur.get("refreshToken") != sec["refreshToken"]:
            # a concurrent refresh rotated the token while this one was in flight: the
            # account lives on under its new token, and a write here would clobber it
            # with the dead one
            return None, "refresh lost a race with a concurrent rotation — re-run"
        if not store_secret(uuid, sec["refreshToken"], None, None, sec.get("tokenHost"),
                            meta={"needsLogin": True}):
            # unlatched, so the next tick re-checks; the distinct string keeps the row
            # from claiming the paused state doctor describes for a persisted latch
            # the remedy has to ride along here: this string deliberately never matches
            # NEEDS_LOGIN, so no surface adds the sign-in step to it
            return None, (NEEDS_LOGIN + " — unlock the Keychain, then sign it back in "
                          "with `claude-usage relogin`")
        return None, NEEDS_LOGIN
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

# How much life the live credential must have left for `switch` to hand it over. A read only has
# to outlive its own call, but this token is written into Claude Code's item and then left there,
# so it has to carry the account until something renews it — which, on a machine where the CLI
# never runs, is the next expiry-triggered renewal below.
SWITCH_MIN_LIFE_MS = 60 * 60 * 1000

def refresh_live(uuid, live):
    """Renew the live credential in place. Returns (access token, error).

    Called only once the live access token has expired or been rejected. That is what makes
    renewing it safe: Claude Code renews its own credential ahead of expiry, and a session
    hosted by the desktop app never writes that item at all, so a lapsed token belongs to
    nobody — there is no concurrent refresh to lose a race with, and no session still running
    on it to break.

    The rotated pair goes to Claude Code's item first, because that is the copy a session
    reads. Our own record follows: for the active account the two name one grant, and a
    rotation that landed in only one of them would leave the other holding a dead token.
    """
    ref = (live or {}).get("refreshToken")
    if not ref:
        return None, "the live credential has no refresh token — sign in again with the claude CLI"
    sec = load_secret(uuid) or {}
    try:
        data, host = refresh_token(ref, sec.get("tokenHost"))
    except GrantRevoked:
        # same terminal state a parked account latches into: only a real sign-in can mint a
        # replacement, so record it rather than spend a call every tick rediscovering it
        cur = load_secret(uuid) or {}
        if cur.get("refreshToken") and cur["refreshToken"] != ref:
            # our copy names a different grant than the one that was just refused: latching it
            # would write the dead token over a live one, and store_secret would read the changed
            # token as a new grant and drop the latch in the same breath
            return None, "the live credential is out of step with its stored copy — re-run `switch`"
        if not store_secret(uuid, ref, None, None, sec.get("tokenHost"), meta={"needsLogin": True}):
            # unlatched, so the next tick re-checks; the distinct string keeps the row from
            # claiming the paused state every surface reads a bare NEEDS_LOGIN as
            return None, (NEEDS_LOGIN + " — unlock the Keychain, then sign it back in "
                          "with `claude-usage relogin`")
        return None, NEEDS_LOGIN
    except Exception as ex:
        return None, f"couldn't renew the live credential ({ex}) — try again in a moment"
    blob = dict(live)
    blob["accessToken"]  = data["access_token"]
    blob["refreshToken"] = data.get("refresh_token", ref)
    blob["expiresAt"]    = int((time.time() + data.get("expires_in", 3600)) * 1000)
    if not write_live(blob):
        # the server may already have rotated the grant, which makes the token we are holding
        # the only copy of it: handing it back would work for one call and strand the account
        return None, "couldn't save the renewed live credential — unlock the Keychain and re-run"
    if not store_secret(uuid, blob["refreshToken"], blob["accessToken"], blob["expiresAt"], host):
        # the session is fine — its copy landed — but ours now names a grant the server has
        # rotated away, and the next `switch` to this account would refresh with a dead token
        # and strand it behind a sign-in
        return blob["accessToken"], "couldn't save the renewed token to our own Keychain item — unlock it and re-run"
    return blob["accessToken"], None

def token_for_live(uuid, live):
    """Usable access token for the active account, renewing the credential if it has lapsed.

    A token still inside its lifetime is handed back untouched. A blob carrying no expiry at
    all is taken at face value — we can't prove it dead, and a needless rotation costs more
    than the 401 that would catch it (see fetch_usage).
    """
    token, exp = live.get("accessToken"), live.get("expiresAt")
    if token and (exp is None or exp > time.time() * 1000):
        return token, None
    if not token and not live.get("refreshToken"):
        return None, "live credential has no access token — run `claude` once"
    return refresh_live(uuid, live)

def is_team_entry(e):
    return bool(e.get("seat_tier")) or e.get("org_type") in ("claude_team", "claude_enterprise")

def is_claude(r):
    return r.get("provider", "claude") == "claude"    # pre-Codex cached rows have no field

def match_live_uuid():
    """Which known account holds the live credential, by matching stored refresh tokens.

    No network and no writes, so it still identifies the session when /profile can't be reached
    or its token has expired. That matters beyond the ▶ marker: an unidentified active
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

def retry_after_ts(headers):
    """When the server's Retry-After says a rate limit clears, as an epoch — or None.

    Seconds form only. The HTTP-date form is legal but not what this API sends, and a
    deadline guessed from a shape we have never seen would be worse than no deadline.
    """
    try:
        secs = int((headers or {}).get("Retry-After", ""))
    except (TypeError, ValueError):
        return None
    return time.time() + secs if secs > 0 else None

def rate_limited_msg(until):
    """What a 429 says: the limit, and the server's own deadline for it. The deadline is the
    only part of it the user can act on."""
    # comma, not a dash: the table's stale banner quotes this sentence after a dash of its own,
    # and two in one line read as two separate thoughts
    if not until:
        return "rate-limited by the usage API, try again shortly"
    return f"rate-limited by the usage API, clears at {clock_short(datetime.fromtimestamp(until, timezone.utc))}"

def fetch_usage(uuid, token, active, live=None):
    """Return (usage_json, error, retry_until).

    A 401 buys one renewal and one retry: a parked account from its stored refresh token, the
    active one in place. retry_until carries a 429's deadline back to the caller so the next
    sweep can wait it out instead of spending a call that cannot succeed.
    """
    try:
        return api_get(USAGE_URL, token), None, None
    except urllib.error.HTTPError as ex:
        if ex.code == 401:
            # the token looked usable and was not: invalidated server-side, or spent so close to
            # its expiry that it lapsed in flight
            token2, err2 = (refresh_live(uuid, live or {}) if active
                            else token_for_parked(uuid, force=True))
            if err2: return None, err2, None
            try:
                return api_get(USAGE_URL, token2), None, None
            except urllib.error.HTTPError as ex2:
                if ex2.code == 401:
                    return None, "session expired — sign into it again", None
                until2 = retry_after_ts(ex2.headers) if ex2.code == 429 else None
                return None, (rate_limited_msg(until2) if ex2.code == 429
                              else f"usage HTTP {ex2.code}"), until2
            except Exception as ex2:
                return None, type(ex2).__name__, None
        if ex.code == 429:
            until = retry_after_ts(ex.headers)
            return None, rate_limited_msg(until), until
        return None, f"usage HTTP {ex.code}", None
    except Exception as ex:
        return None, type(ex).__name__, None

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

# ---- usage history ----------------------------------------------------------
# One JSON line per fresh fetch: {"ts": ..., "a": {"<uuid>": {"fh": pct, "wk": pct}}}. This exists to
# answer the rate questions — trend, pace, cap forecast — that a point-in-time reading can't. It holds
# percentages keyed by the uuids the index already carries: no secrets, no identity, so it lives in
# the state dir like the cache. Current state is still always the endpoint's answer, never read back
# from here — an out-of-band usage reset simply appears, and only the trend line remembers it.

HISTORY = os.path.join(STATE_DIR, "history.jsonl")
HISTORY_KEEP_S = 14 * 86400           # two weeks: the trend window with room for longer-range views

def append_history(rows, ts):
    entry = {}
    for r in rows:
        if r.get("error") or r.get("stale"):
            continue                   # last-known values would flatten the very trend they'd enter
        if r.get("provider") == "codex":
            wins = codex_display_windows(r)
            if wins:
                # positional longest, matching codex_ring_spec's ring — filtering blanks first would
                # let a 5-hour reading stand in for an expired weekly and corrupt the series. Only a
                # window longer than a day is a weekly sample at all.
                w = wins[-1]
                if w.get("pct") is not None and (w.get("minutes") or 0) > 1440:
                    entry[r["uuid"]] = {"wk": w["pct"]}
        else:
            fh = (r.get("five_hour") or {}).get("pct")
            wk = (r.get("seven_day") or {}).get("pct")
            if fh is not None or wk is not None:
                entry[r["uuid"]] = {"fh": fh, "wk": wk}
    if not entry:
        return
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(HISTORY, "a") as f:
            f.write(json.dumps({"ts": round(ts, 1), "a": entry}, separators=(",", ":")) + "\n")
        # Trim only when there is actually something to drop: the size check alone would rewrite the
        # whole file on every append once past the threshold, and the day of slack past the horizon
        # keeps back-to-back rewrites from chasing the boundary.
        if os.path.getsize(HISTORY) > 512 * 1024 and _history_oldest() < ts - HISTORY_KEEP_S - 86400:
            kept = [s for s in load_history() if s["ts"] >= ts - HISTORY_KEEP_S]
            _replace_file(HISTORY, "".join(json.dumps(s, separators=(",", ":")) + "\n" for s in kept))
    except Exception:
        pass                           # history is an enrichment; a full disk must not break the render

def _history_oldest():
    """ts of the first (oldest) line, cheaply — the trim decision must not parse the whole file."""
    try:
        with open(HISTORY) as f:
            return json.loads(f.readline())["ts"]
    except Exception:
        return float("-inf")           # unreadable first line: the rewrite is the repair

def load_history(max_age_s=None):
    if mock_enabled():
        return mock_history(max_age_s)
    out = []
    try:
        with open(HISTORY) as f:
            for line in f:
                try:
                    s = json.loads(line)
                except Exception:
                    continue           # a half-written last line (crash mid-append) skips cleanly
                if isinstance(s, dict) and isinstance(s.get("ts"), (int, float)) and isinstance(s.get("a"), dict):
                    out.append(s)
    except Exception:
        return []
    if max_age_s is not None:
        cut = time.time() - max_age_s
        out = [s for s in out if s["ts"] >= cut]
    out.sort(key=lambda s: s["ts"])    # appends are chronological until a clock step; series code isn't
    return out

def weekly_series(hist, uuid):
    pts = []
    for s in hist:
        e = s["a"].get(uuid)
        if isinstance(e, dict) and isinstance(e.get("wk"), (int, float)):
            pts.append((s["ts"], e["wk"]))
    return pts

def merge_last_known(claude, cache):
    """Each account's best current answer: this sweep's reading where it succeeded, its own
    last-known numbers where it didn't.

    Per account, because a failed read is a fact about the account it failed on and about no
    other: one account's rate limit says nothing about the three beside it that answered, and
    trading its gauges for an error row on their account would be the panel agreeing with it.

    A latched needs-login row is exempt: it is a definitive answer about the account, and the
    numbers that would stand in for it date from before the grant was revoked.
    """
    prev = {r.get("uuid"): r for r in (cache or {}).get("rows") or []
            if r.get("provider", "claude") == "claude"}
    out = []
    for r in claude:
        old = prev.get(r.get("uuid"))
        if not r.get("error") or r.get("needs_login") or not old or old.get("error"):
            out.append(r)
            continue
        keep = dict(old)
        keep["stale"], keep["stale_reason"] = True, r["error"]
        # the numbers are the old sweep's, but which account is live and how long this one has
        # to wait are facts about this one
        keep["active"], keep["retry_after"] = r.get("active"), r.get("retry_after")
        out.append(keep)
    return out

def collect(ingest=True, act=None):
    """Cached wrapper: debounce rapid refreshes, and hold each account's last-known values
    through a read that fails on it.

    ingest=False reports on the accounts already known without registering the live one — for
    diagnostics, which should describe the current state rather than change it. act says whether
    this refresh may auto-switch the live credential; it defaults to following ingest, but a
    caller that reads for its own narration (setup) opts out — registering accounts must never
    swap them as a side effect.
    """
    if act is None:
        act = ingest
    if mock_enabled():
        return mock_rows()
    now = time.time()
    cache = load_cache()
    if cache and 0 <= now - cache.get("ts", 0) < COOLDOWN:
        return cache["rows"]                                  # rapid re-refresh → reuse, don't hit the API
    rows = _collect_live(ingest, cache=cache)
    # Freshness is judged on Claude alone: only Claude hits the network, and a retained Codex
    # snapshot (never an error) must not make a sweep that read nothing look like it read something.
    claude = [r for r in rows if r.get("provider", "claude") == "claude"]
    codex  = [r for r in rows if r.get("provider") == "codex"]
    out = merge_last_known(claude, cache) + codex
    fresh_ok    = any(not r.get("error") for r in claude)
    all_latched = all(r.get("needs_login") for r in claude if r.get("error"))
    fetched     = not claude or fresh_ok or all_latched       # read something (or had nothing to read)
    # The rows are always kept, so the next sweep can still hand back last-known numbers and the
    # deadlines it has to wait out. The timestamp is not: it dates the last real reading, and a
    # sweep that read nothing must not advance it into a claim of freshness it didn't earn.
    # `is None`, not `or`: a cache stamped at epoch 0 is a timestamp, and the oldest one there is.
    prev_ts = (cache or {}).get("ts")
    save_cache(out, now if fetched else (now - COOLDOWN if prev_ts is None else prev_ts))
    if fetched:
        append_history(out, now)                              # skips stale and errored rows itself
        if act:
            out = maybe_auto_switch(out)
    return out

def rate_limit_holds(cache):
    """uuid → when the usage API said its rate limit clears, from the last sweep's rows.

    A 429 answers with its own deadline, and a call made before that passes cannot succeed.
    Sitting the window out costs a stale reading; spending the calls anyway costs the same
    reading and, against a limiter that re-arms on each attempt, a wait that never ends.
    """
    now = time.time()
    return {r["uuid"]: r["retry_after"] for r in (cache or {}).get("rows") or []
            if r.get("uuid") and isinstance(r.get("retry_after"), (int, float))
            and r["retry_after"] > now}

def _collect_live(ingest=True, cache=None):
    holds = rate_limit_holds(cache)
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
            # token_for_live takes every shape a live blob arrives in, down to the partial one
            # (refresh token only, from a mid-write file) that match_live_uuid still identifies
            token, err = token_for_live(uuid, live)
        else:
            token, err = token_for_parked(uuid)
        row = {"provider": "claude", "uuid": uuid, "email": e["email"], "label": e["label"],
               "tier": e.get("tier"), "org_type": e.get("org_type"), "is_team": is_team_entry(e),
               "active": uuid == active_uuid, "error": err}
        hold = holds.get(uuid)
        if token and not err and hold:
            row["error"], row["retry_after"] = rate_limited_msg(hold), hold
        elif token and not err:
            u, uerr, until = fetch_usage(uuid, token, row["active"], live)
            if uerr:
                row["error"] = uerr
            if until:
                row["retry_after"] = until
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
        if row.get("error") == NEEDS_LOGIN:
            # a state, not a failure: doctor and --json consumers key on this to render a
            # sign-in prompt instead of a retrying refresh error
            row["needs_login"] = True
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

def codex_latest_usage(not_before=0):
    """The newest rate_limits event that actually carries a window, as (epoch_ts, rate_limits).

    not_before discards readings written before the signed-in account took the auth.json slot: a
    rollout doesn't name its account, so anything older than that boundary may belong to another one.

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
                    if ts.timestamp() < not_before: continue  # predates this account's sign-in
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

# Codex credentials live in one 0600 JSON file, so a switch is a file swap: the whole auth.json is
# stashed per account in the Keychain (never on disk) and written back on the way in. Unlike the
# Claude side there is no refresh here — the tokens are handed over as captured and the codex CLI
# refreshes them itself on its next run.
def codex_key(aid):
    return f"codex:{aid}"

CODEX_PREV_KEY = codex_key("__previous__")

def codex_read_auth_raw():
    try:
        with open(CODEX_AUTH) as f: return f.read()
    except Exception:
        return None

def codex_identity(raw):
    """(account_id, email, name, plan) from an auth.json blob, or Nones. The id_token is a JWT we
    decode without verifying — it's a local file we already trust and we only read it."""
    try:
        auth = json.loads(raw or "")
        tokens = auth.get("tokens") if isinstance(auth, dict) else None
    except Exception:
        tokens = None
    if not isinstance(tokens, dict): return None, "", "", None
    claims = _jwt_claims(tokens.get("id_token"))
    oauth  = claims.get("https://api.openai.com/auth")
    oauth  = oauth if isinstance(oauth, dict) else {}
    aid = tokens.get("account_id") or oauth.get("chatgpt_account_id")
    return aid, claims.get("email") or "", claims.get("name") or "", oauth.get("chatgpt_plan_type")

def codex_store_auth(aid, raw):
    """Stash an account's auth.json. Written only when the contents changed: every write puts the
    secret in argv, and an unchanged re-capture happens on every refresh tick.

    Stored compact. Codex writes the file pretty-printed, and a secret carrying a newline comes back
    from `security` hex-encoded — which the comparison below would never match, so the credential
    would be rewritten on every tick."""
    try:
        raw = json.dumps(json.loads(raw), separators=(",", ":"))
    except Exception:
        return False                  # not JSON: storing it would only produce an unusable blob
    if keychain_read(STORE_SVC, codex_key(aid)) == raw:
        return True
    return keychain_write(STORE_SVC, codex_key(aid), raw)

CODEX_AUTH_KEYS = ("tokens", "auth_mode", "last_refresh", "OPENAI_API_KEY")

def codex_write_auth(raw):
    """Install a stashed auth.json as the signed-in one. Every field that identifies or authenticates
    an account comes from the incoming blob — including OPENAI_API_KEY, which is that account's key
    and must not survive from the one being displaced. Anything else in the file is left as found."""
    try:
        new = json.loads(raw)
        if not isinstance(new, dict) or not isinstance(new.get("tokens"), dict): return False
    except Exception:
        return False
    try:
        cur = json.loads(codex_read_auth_raw() or "{}")
        if not isinstance(cur, dict): cur = {}
    except Exception:
        cur = {}
    for k in CODEX_AUTH_KEYS:
        if k in new: cur[k] = new[k]
        else: cur.pop(k, None)      # absent upstream means unset, not "keep the old account's"
    try:
        os.makedirs(CODEX_HOME, exist_ok=True)
        tmp = CODEX_AUTH + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)   # the file holds live tokens
        with os.fdopen(fd, "w") as f: json.dump(cur, f, indent=2)
        os.replace(tmp, CODEX_AUTH)      # atomic: codex must never read a half-written credential
        return True
    except Exception:
        return False

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

def codex_boundary(idx, aid):
    """The earliest moment a reading could belong to `aid`, when this run is the first to find it in
    the auth.json slot — else None, leaving any recorded boundary alone.

    Session rollouts don't name their account, so usage is attributed to whoever is signed in. That
    only holds if readings written under the previous account are excluded, and the changeover is
    visible here: the account holding the most recent last_seen_live is the one we saw last, so a
    different account in the slot now means a sign-in happened between the two runs.

    The floor is that last sighting rather than the present moment, so an account signed into after
    a stretch of use elsewhere keeps the history it really earned. It is a bound, not a timestamp:
    the sign-in fell somewhere between the sighting and now, so usage from the tail of that gap can
    still land on the wrong account — at most one refresh interval of it. `switch` doesn't rely on
    this, recording the exact instant it moves the credential.

    A registry with no other sighting to bound against — one account, or a registry that predates
    this field — yields no boundary: with nothing to have signed in from, every reading found is
    that account's own.
    """
    seen = [e["last_seen_live"] for a, e in idx.items()
            if a != aid and isinstance(e, dict) and isinstance(e.get("last_seen_live"), (int, float))]
    mine = (idx.get(aid) or {}).get("last_seen_live") if isinstance(idx.get(aid), dict) else None
    if not seen: return None
    if isinstance(mine, (int, float)) and mine >= max(seen): return None   # we saw it here last run
    return max(seen)

def collect_codex(persist=True):
    """Codex rows for every known account. The signed-in account (auth.json) is refreshed from the
    latest session; accounts seen before but not currently signed in render from their last snapshot
    (marked stale by age). Registry is keyed by account_id, so rotating the auth.json slot accretes
    accounts the same way the Claude side does."""
    idx = load_codex_index()
    raw = codex_read_auth_raw()
    aid, email, name, plan = codex_identity(raw)
    live_aid = aid
    if aid:
        prev = idx.get(aid) if isinstance(idx.get(aid), dict) else {}
        prev_as_of = prev.get("as_of") if isinstance(prev.get("as_of"), (int, float)) else 0
        # identity always refreshes from auth.json; usage only when the scan turns up a reading at
        # least as new as the stored one — a window-less stretch must not erase the last real figure.
        entry = {"account_id": aid, "email": email, "name": name, "plan": plan or prev.get("plan")}
        since = codex_boundary(idx, aid)
        if since is not None:
            entry["signed_in_since"] = since
        best = codex_latest_usage(entry.get("signed_in_since") or prev.get("signed_in_since") or 0)
        if best and best[0] >= prev_as_of:
            ts, rl = best
            entry["windows"] = codex_windows(rl)
            entry["as_of"]   = ts
            entry["plan"]    = rl.get("plan_type") or entry["plan"]
            cr = rl.get("credits") or {}
            entry["credits"] = {"has": bool(cr.get("has_credits")),
                                "unlimited": bool(cr.get("unlimited")), "balance": cr.get("balance")}
        entry["last_seen_live"] = time.time()
        idx[aid] = {**prev, **entry}
        if persist:
            save_codex_index(idx)
            codex_store_auth(aid, raw)      # so this account can be switched back to later
    rows = []
    for aid, e in idx.items():
        if not isinstance(e, dict): continue         # skip a tampered/foreign registry entry
        wins = e.get("windows") or []
        rows.append({
            "provider": "codex", "uuid": aid, "account_id": aid,
            "email": e.get("email") or aid, "label": codex_label(e.get("email"), e.get("name"), aid),
            "plan": e.get("plan"), "windows": wins, "as_of": e.get("as_of"),
            "credits": e.get("credits"), "active": aid == live_aid,
            # one `security` read per parked account: the Keychain has no bulk listing that returns
            # every match, so existence is asked per account
            "switchable": aid != live_aid and bool(keychain_read(STORE_SVC, codex_key(aid))),
            "error": None if (wins or e.get("as_of")) else "no usage recorded yet — run codex once",
        })
    return rows

# ---- mock mode --------------------------------------------------------------
# A synthetic machine, for working on the display without waiting for real windows to move.
# It substitutes the three inputs the renderers read — usage rows, history, and the insights
# scan — and nothing downstream knows the difference, so what appears is the real formatting,
# the real severity thresholds and the real chart code, not an approximation of them.
# Deliberately loud: `doctor` reports it and the table prints a banner, because numbers that
# look plausible and are invented are worse than obviously broken ones.

MOCK_FLAG = os.path.join(STATE_DIR, "mock-mode")

def mock_enabled():
    return os.path.exists(MOCK_FLAG)

def cmd_mock(arg):
    if arg == "on":
        os.makedirs(STATE_DIR, exist_ok=True)
        open(MOCK_FLAG, "w").close()
        clear_cache()
        print("mock mode ON — every surface now shows invented data.\n"
              "turn it off with:  claude-usage mock off")
    elif arg == "off":
        try: os.remove(MOCK_FLAG)
        except OSError: pass
        clear_cache()
        print("mock mode off")
    else:
        print("mock mode is " + ("ON" if mock_enabled() else "off"))

# One fictional week per account, staggered so the burn chart shows windows that opened on
# different days, and spanning idle to nearly spent so every severity colour appears.
MOCK_ACCOUNTS = [
    # label, email, tier, 5-hour %, weekly %, scoped %, days until weekly reset, burn shape
    ("work",     "work@example.com",     "default_claude_max_20x", 34, 22, 17, 4.8, "even"),
    ("personal", "personal@example.com", "default_claude_max_20x", 68, 78, 91, 0.4, "late"),
    ("side",     "side@example.com",     "default_claude_max_5x",   9,  9,  3, 6.2, "sparse"),
    ("archive",  "archive@example.com",  "default_claude_max_20x", 47, 63, 55, 3.7, "early"),
]

_MOCK_CODEX = []

def mock_codex_window():
    """A weekly window timed like the machine's real Codex one, so the band sits on a plausible
    schedule instead of an invented offset that can collide with a Claude account's. Only the
    timing is borrowed — the name, address and percentage are invented, because mock output gets
    screenshotted and pasted into places real addresses should not go."""
    if _MOCK_CODEX:
        return _MOCK_CODEX[0]
    for aid, e in load_codex_index().items():
        if not isinstance(e, dict):
            continue
        for w in e.get("windows") or []:
            if (w.get("minutes") or 0) >= 10080 and w.get("resets_at"):
                dt = parse_dt(w["resets_at"])
                if not dt:
                    continue
                _MOCK_CODEX.append(
                    {"label": "codex", "email": "codex@example.com", "plan": "pro",
                     "resets_at": w["resets_at"], "minutes": w.get("minutes") or 10080,
                     "days": (dt.timestamp() - time.time()) / 86400})
                return _MOCK_CODEX[0]
    _MOCK_CODEX.append(
        {"label": "codex", "email": "codex@example.com", "plan": "pro",
         "resets_at": datetime.fromtimestamp(time.time() + 1.6 * 86400, timezone.utc).isoformat(),
         "minutes": 10080, "days": 1.6})
    return _MOCK_CODEX[0]

def _mock_uuid(i):
    return f"00000000-0000-4000-8000-{i:012d}"

def mock_rows():
    now = time.time()
    rows = []
    for i, (label, email, tier, fh, wk, sc, days, _shape) in enumerate(MOCK_ACCOUNTS):
        reset = now + days * 86400
        rows.append({
            "provider": "claude", "uuid": _mock_uuid(i), "email": email, "label": label,
            "tier": tier, "org_type": "claude_max", "is_team": False,
            "active": label == "archive", "error": None,
            "five_hour": {"pct": fh, "resets_at": datetime.fromtimestamp(
                now + (5 - fh / 25) * 3600, timezone.utc).isoformat()},
            "seven_day": {"pct": wk, "resets_at": datetime.fromtimestamp(reset, timezone.utc).isoformat()},
            "scoped": [{"model": "Fable", "pct": sc,
                        "resets_at": datetime.fromtimestamp(reset, timezone.utc).isoformat()}],
            "spend": {"enabled": False},
        })
    cw = mock_codex_window()
    rows.append({
        "provider": "codex", "uuid": "mock-codex", "account_id": "mock-codex",
        "email": cw["email"], "label": cw["label"], "plan": cw["plan"], "active": True,
        "switchable": False, "error": None, "as_of": now - 900,
        "windows": [{"label": "weekly", "minutes": cw["minutes"], "pct": 41.0,
                     "resets_at": cw["resets_at"]}],
        "credits": {"has": False},
    })
    return rows

def mock_history(max_age_s=None):
    """Two weeks of readings: every account's current window in full, and the one before it.

    Usage arrives in shifts rather than a smooth ramp, because only one account is being spent at
    a time — whoever holds the session climbs for a few hours while the rest sit flat, and a reset
    drops that account back to zero. Deterministic, so the chart looks the same on every refresh.
    """
    now = time.time()
    step = 1800
    horizon = 14 * 86400
    t_start = now - horizon
    n_steps = int(horizon / step) + 1
    # the Codex account draws a band as well, so it takes part in the rotation
    specs = [(_mock_uuid(i), wk, days) for i, (_l, _e, _t, _fh, wk, _sc, days, _s)
             in enumerate(MOCK_ACCOUNTS)] + [("mock-codex", 41, mock_codex_window()["days"])]
    ids = [sp[0] for sp in specs]

    # Which account holds the session in each ~3h block. A fixed shuffle per block keeps the
    # rotation uneven — real switching is not round-robin — while staying reproducible.
    def holder(block, n):
        return (block * 7 + (block // 5) * 3) % n

    # raw work units per account per step, then normalised per window to the target percentage
    raw = {i: [0.0] * n_steps for i in range(len(specs))}
    for k in range(n_steps):
        ts = t_start + k * step
        lt = time.localtime(ts)
        hour = lt.tm_hour + lt.tm_min / 60      # local, so the flat nights line up with the axis
        if not (8 <= hour <= 23.5):
            continue                      # asleep: nothing moves on any account
        block = int(ts // (3 * 3600))
        who = holder(block, len(specs))
        # within a block the pace still varies, and short gaps break the line into steps
        pace = 1.0 if (k % 7) else 0.15
        raw[who][k] = pace

    out = [{"ts": t_start + k * step, "a": {}} for k in range(n_steps)]
    for i, (uid, wk, days) in enumerate(specs):
        reset = now + days * 86400
        win = 7 * 86400
        # walk each window separately so the reading drops to zero at the boundary
        for w_end in (reset - win, reset):
            w_start = w_end - win
            idx = [k for k in range(n_steps)
                   if w_start <= t_start + k * step < min(w_end, now)]
            if not idx:
                continue
            total = sum(raw[i][k] for k in idx) or 1.0
            # the window still running lands on its live percentage; a finished one ran hotter
            target = wk if w_end == reset else min(96, wk + 21)
            cum = 0.0
            for k in idx:
                cum += raw[i][k]
                out[k]["a"][uid] = {"wk": round(cum / total * target, 1)}
    return [s for s in out if s["a"]]

def mock_insights():
    now = time.time()
    def eff(*pairs):
        return [{"effort": e, "rank": r, "cost": c, "msgs": m} for e, r, c, m in pairs]
    return {
        "as_of": now, "ttl_s": 900, "window_days": 7,
        "total_cost": 4654.0, "today_cost": 1192.0,
        "models": [
            {"name": "Fable 5", "family": "Fable", "msgs": 412, "cost": 2607.0,
             "output": 386412, "cache_read": 14662301,
             "efforts": eff(("max", 0, 1355.0, 190), ("high", 1, 808.0, 142), ("medium", 2, 444.0, 80))},
            {"name": "Opus 5", "family": "Opus", "msgs": 268, "cost": 1894.0,
             "output": 221004, "cache_read": 9120455,
             "efforts": eff(("xhigh", 0, 1250.0, 160), ("medium", 1, 644.0, 108))},
            {"name": "GPT-5.6 sol", "family": "GPT", "msgs": 41, "cost": 62.0,
             "output": 30122, "cache_read": 411203, "efforts": eff(("high", 0, 44.0, 28), ("low", 1, 18.0, 13))},
            {"name": "Sonnet 5", "family": "Sonnet", "msgs": 96, "cost": 52.0,
             "output": 51221, "cache_read": 880110, "efforts": eff(("high", 0, 26.0, 40), ("low", 1, 26.0, 56))},
            {"name": "Sonnet 4.6", "family": "Sonnet", "msgs": 54, "cost": 28.0,
             "output": 24110, "cache_read": 402118, "efforts": eff((None, 0, 28.0, 54))},
            {"name": "Opus 4.8", "family": "Opus", "msgs": 12, "cost": 7.21,
             "output": 6120, "cache_read": 90210, "efforts": eff((None, 0, 7.21, 12))},
            {"name": "Haiku 4.5", "family": "Haiku", "msgs": 320, "cost": 4.50,
             "output": 88120, "cache_read": 1200310, "efforts": eff((None, 0, 4.50, 320))},
        ],
    }

# ---- rendering --------------------------------------------------------------

def rel(dt):
    if not dt: return "?"
    secs = (dt - datetime.now(timezone.utc)).total_seconds()
    if secs <= 0: return "now"
    d, rem = divmod(int(secs), 86400); h, rem = divmod(rem, 3600); m = rem // 60
    if d: return f"{d}d {h}h"
    if h: return f"{h}h {m}m"
    return f"{m}m"

def clock_short(dt):
    """Compact local clock time: '5pm', '6:59am' (drops :00 on the hour, lowercase am/pm)."""
    d = dt.astimezone()
    mins = f":{d.strftime('%M')}" if d.minute else ""
    return f"{d.strftime('%-I')}{mins}{d.strftime('%p').lower()}"

def local_short(dt):
    """Compact local time: 'Tue 5pm', 'Mon 6:59am'."""
    return f"{dt.astimezone().strftime('%a')} {clock_short(dt)}"

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

ACTIVE_MARK = "▶"     # the account the CLI is on — same glyph on both providers

def active_mark(r):
    """The margin marker for a table row: ▶ on the account you're on, a blank of equal width on
    the rest, so the names below it stay aligned."""
    return f"{C['cyan']}{ACTIVE_MARK}{C['x']}" if r.get("active") else " "

def _table_claude_row(r, w):
    lw, ew = w
    # Name at col 2 under the provider header, matching the Codex section; the ▶ marking the account
    # you're on hangs in the col-0 margin, so every name still shares one left edge.
    print(f"{active_mark(r)} {C['b']}{r['label']:<{lw}}{C['x']} {C['dim']}{r['email']:<{ew}}{C['x']}"
          f"  {C['cyan']}{plan_of(r)}{C['x']}")
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

def _table_codex_row(r, w):
    lw, ew = w
    print(f"{active_mark(r)} {C['b']}{r['label']:<{lw}}{C['x']} {C['dim']}{r['email']:<{ew}}{C['x']}"
          f"  {C['cyan']}{plan_of(r)}{C['x']}")
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
    if mock_enabled():
        print(f"\n{C['y']}⚠ MOCK MODE — every figure below is invented. "
              f"turn it off with `claude-usage mock off`{C['x']}")
    if not rows:
        print(f"\n{C['b']}Usage{C['x']}\n")
        print(f"{C['y']}No accounts found.{C['x']} Log in with the `claude` CLI "
              f"(`claude` → /login), then run this again.")
        print(f"{C['dim']}It reads the account the CLI is signed into; log into each of your "
              f"accounts once to add them all.{C['x']}\n")
        return
    groups = by_provider(rows)
    print(f"\n{C['b']}Usage{C['x']}  {C['dim']}· {datetime.now().astimezone().strftime('%-I:%M %p')}{C['x']}\n")
    multi = len(groups) > 1   # only label the sections when there's more than one provider to tell apart
    w = _col_widths(rows)     # shared across sections so the plan column lines up throughout
    for key, name, grp in groups:
        if multi:
            print(f"{C['dim']}── {name} " + "─" * (56 - len(name)) + f"{C['x']}")
        if key == "codex":
            for r in grp: _table_codex_row(r, w)
        else:
            for r in grp: _table_claude_row(r, w)
    stale = [r for r in rows if is_claude(r) and r.get("stale")]
    if stale:
        # the reason belongs with the rows it explains, and one shared reason is the common case
        # (a sweep that failed the same way for everything it touched)
        reasons = sorted({r.get("stale_reason") or "the last refresh failed" for r in stale})
        who = ", ".join(r["email"] for r in stale)
        print(f"{C['y']}⚠ Showing last known values for {who} — {'; '.join(reasons)}.{C['x']}\n")
    latched = [r for r in rows if is_claude(r) and r.get("needs_login")]
    if latched:
        # the row states the condition; here is the only place in this surface that can carry
        # what ends it, and without it a signed-out row reads as something to wait out
        who = latched[0]["email"] if len(latched) == 1 else "<account>"
        subject = (f"{latched[0]['email']} is" if len(latched) == 1
                   else f"{len(latched)} accounts are")
        print(f"{C['y']}⚠ {subject} signed out and no longer refreshing. "
              f"Sign back in with `claude-usage relogin {who}`.{C['x']}\n")
    # suppressed under mock: a real switch record under invented data would read as fiction
    line = None if mock_enabled() else last_auto_line()
    if line:
        print(f"{C['dim']}{line}{C['x']}\n")
    n = len([r for r in rows if is_claude(r) and not r.get("is_team")])
    if n <= 1:
        lead = "No personal accounts tracked yet" if n == 0 else "Only one account tracked so far"
        print(f"{C['dim']}{lead} — log into your other accounts with the `claude` CLI "
              f"(`claude` → /login) to add them.{C['x']}\n")

def title_specs(rows):
    """(ring pct, pie pct) per active provider — the menu-bar gauge pair the app draws."""
    claude_rows = [r for r in rows if r.get("provider", "claude") == "claude"]
    head = next((r for r in claude_rows
                 if r.get("active") and not r.get("error")
                 and (r.get("five_hour") or {}).get("pct") is not None), None)
    specs = []
    if head:
        specs.append((head["seven_day"]["pct"] or 0, head["five_hour"]["pct"] or 0))
    cs = codex_ring_spec(rows)
    if cs is not None:
        specs.append(cs)
    return specs

def can_switch(r):
    """Whether clicking this account's row can switch to it — a Codex row knows (its credential must
    have been captured); a Claude row only needs to be parked and readable."""
    if r.get("provider") == "codex":
        return bool(r.get("switchable"))
    return not r.get("active") and not r.get("error")

def _row(label, pct, meta, resets_at=None):
    """One dropdown line as data. A line that counts down also carries the split — meta_prefix plus
    resets_at — so the native app can keep "Xh Ym left" ticking by recomposing prefix + countdown
    instead of parsing the finished text."""
    row = {"label": label, "pct": pct, "meta": meta, "resets_at": resets_at}
    dt = parse_dt(resets_at) if resets_at else None
    if dt:
        tail = f"{rel(dt)} left"
        if meta.endswith(tail):
            row["meta_prefix"] = meta[:-len(tail)]     # "" | "~" | "Tue 5pm · " | "~Tue 5pm · "
    return row

def _display_rows(r):
    """The account's dropdown lines as data: {label, pct, meta, resets_at, meta_prefix?}. pct None is
    a text-only line. Every renderer draws exactly this list, so their content can't drift."""
    rows = []
    if r.get("provider") == "codex":
        for w in codex_display_windows(r):
            if w.get("expired"):
                rows.append(_row((w.get("label") or "")[:6], None, "window reset — run codex to refresh"))
            else:
                rows.append(_row((w.get("label") or "")[:6], w.get("pct") or 0,
                                 resets_phrase(w.get("resets_at"), "week"), w.get("resets_at")))
        cr = r.get("credits") or {}
        if cr.get("has") and cr.get("balance") not in (None, "0"):
            rows.append(_row("credits", None, str(cr["balance"])))
        return rows
    fh, wk = r.get("five_hour") or {}, r.get("seven_day") or {}
    scoped = [s for s in r.get("scoped", []) if s.get("pct") is not None]
    rows.append(_row("5-hour", fh.get("pct") or 0, resets_phrase(fh.get("resets_at"), "short"),
                     fh.get("resets_at")))
    rows.append(_row("weekly", wk.get("pct") or 0,
                     wk_phrase(wk, "short") if scoped else wk_phrase(wk, "week"), wk.get("resets_at")))
    for i, s in enumerate(scoped):
        rows.append(_row((s.get("model") or "scoped")[:6], s["pct"], week_abs_label(wk) if i == 0 else ""))
    sp = r.get("spend") or {}
    if sp.get("enabled") and sp.get("limit"):
        rows.append(_row("extra", None, f"{usd(sp.get('used') or 0)} / {usd(sp['limit'])} used"))
    return rows

def _resample(pts, n=168):
    """Cap a (ts, value) series at n points, last-per-bucket — the JSON trend series the burn
    chart draws; interpolation happens at render time, so density beyond this is wasted bytes."""
    if len(pts) <= n:
        return pts
    t0, t1 = pts[0][0], pts[-1][0]
    span = max(1e-9, t1 - t0)
    buckets = {}
    for t, v in pts:
        buckets[min(n - 1, int((t - t0) / span * n))] = (t, v)
    return [buckets[k] for k in sorted(buckets)]

def trend_view(hist, r):
    """The account's trend samples, or None until an hour of them exists — minutes of history draw
    a speck, not a shape."""
    series = weekly_series(hist, r["uuid"])
    if len(series) < 2 or series[-1][0] - series[0][0] < 3600:
        return None
    return series

def attach_display(rows):
    """Give every row a `display` view-model: plan text, switchability, formatted lines, and the
    trend (series + numeric pace + the window's reset epoch and length, for charts). All phrasing
    and strategy stays here, in one place — a consumer that renders `display` verbatim is always
    current."""
    hist = load_history(8 * 86400)     # the full week a chart can frame, plus a day of slack
    for r in rows:
        d = {"plan": plan_of(r), "can_switch": can_switch(r)}
        d["rows"] = _display_rows(r) if not r.get("error") else []
        series = trend_view(hist, r)
        if series and not r.get("error"):
            t = {"series": [[round(a, 1), b] for a, b in _resample(series)]}
            # The reset only anchors a chart if it belongs to a genuinely weekly-scale window —
            # a Codex log carrying just its 5-hour window must not masquerade as a week.
            if r.get("provider") == "codex":
                wins = [w for w in (r.get("windows") or []) if (w.get("minutes") or 0) > 1440]
                src = wins[-1] if wins else {}
                win_s = (src.get("minutes") or 0) * 60
            else:
                src, win_s = r.get("seven_day") or {}, 7 * 86400
            reset = parse_dt(src.get("resets_at"))
            if reset:
                t["reset_ts"] = round(reset.timestamp())
                t["window_s"] = win_s
            d["trend"] = t
        r["display"] = d
    return rows

def render_json(rows):
    rows = attach_display(sort_rows(rows))
    aw = load_autoswitch()
    print(json.dumps({"accounts": rows,
                      "gauges": [list(s) for s in title_specs(rows)],
                      # the record of an identity change belongs in every surface. `line` is the
                      # sentence, preformatted here so the bar and the table can never disagree
                      # on wording or recency; `enabled` marks the mode as armed.
                      # suppressed under mock like the table's lines: a real switch record
                      # under invented data would read as part of the fiction
                      "auto_switch": ({"enabled": bool(aw.get("enabled")),
                                       "last": aw.get("last_auto"),
                                       "line": last_auto_line(aw)}
                                      if not mock_enabled()
                                      and (aw.get("enabled") or aw.get("last_auto"))
                                      else None),
                      # the bar has no other way to know, and unmarked invented numbers are
                      # worse than no numbers — they get screenshotted and believed
                      "mock": True if mock_enabled() else None,
                      "updated_ts": data_ts(),
                      "generated_at": datetime.now(timezone.utc).isoformat()}, indent=2))

# ---- account switching ------------------------------------------------------

# the pre-switch credential lives in the Keychain like every other secret — never in a file
PREV_KEY = "__previous__"
# A switch moves the credential and the profile together. When only the credential lands, the CLI
# spends one account under another's name — a partial result that must never read as a clean
# success (see _report_switch).
MISMATCH_NOTE = " — but ~/.claude.json still names the other account, so the CLI will show that name"
# `switch --undo` reverses whichever provider was switched last, so the switch records which it was.
LAST_SWITCH = os.path.join(STATE_DIR, "last-switch.json")

def record_last_switch(provider, auto=False):
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(LAST_SWITCH, "w") as f:
            json.dump({"provider": provider, "ts": time.time(), "auto": auto}, f)
    except Exception:
        pass

def last_switch_info():
    """The last switch as recorded: {provider, ts?, auto?}. ts is absent in records written
    by versions that predate auto-switch — treat those as 'long ago'."""
    try:
        with open(LAST_SWITCH) as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}

def last_switch_provider():
    return last_switch_info().get("provider") or "claude"

def _fail(msg):
    print(msg, file=sys.stderr); sys.exit(1)

def _report_switch(msg, note):
    """Announce a switch. A note means it went through only in part — the credential moved but the
    profile didn't — and a partial switch must reach the user wherever the switch came from: the
    app surfaces stderr from a nonzero exit, and shells read the status."""
    if note:
        print(msg + note, file=sys.stderr)
        sys.exit(3)                    # partial: the credential DID move — not a clean failure
    print(msg)

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

def codex_resolve(key):
    """A registered Codex account by account_id, email or label."""
    k = (key or "").lower()
    if not k: return None
    for aid, e in load_codex_index().items():
        if not isinstance(e, dict): continue
        if k in (aid.lower(), (e.get("email") or "").lower(),
                 codex_label(e.get("email"), e.get("name"), aid).lower()):
            return {**e, "account_id": aid}
    return None

CODEX_PREFIX = "codex:"

def resolve_target(key):
    """(provider, entry, note) for a name typed at the CLI, or (None, None, note).

    One email commonly names an account on both providers, so `codex:` in front picks the Codex one;
    a bare name that matches both takes Claude and carries a note saying how to reach the other. Every
    command that accepts an account name resolves through here, so the prefix and the collision
    behave the same wherever a name is typed.
    """
    k = (key or "").strip()
    if k.lower().startswith(CODEX_PREFIX):
        bare = k[len(CODEX_PREFIX):]
        e = codex_resolve(bare)
        return ("codex", e, None) if e else (None, None, f"unknown Codex account: {bare}")
    claude, codex = resolve_account(k), codex_resolve(k)
    if claude and codex:
        return "claude", claude, (f"note: Codex also has {codex.get('email') or k} — "
                                  f"use `{CODEX_PREFIX}{k}` for that one")
    if claude: return "claude", claude, None
    if codex:  return "codex", codex, None
    return None, None, f"unknown account: {k}"

def codex_mark_signed_in(aid):
    """Record the sign-in boundary the moment the credential lands, so a render between the switch
    and the next capture doesn't credit this account with the previous one's usage."""
    idx = load_codex_index()
    e = idx.get(aid) if isinstance(idx.get(aid), dict) else {}
    now = time.time()
    idx[aid] = {**e, "signed_in_since": now, "last_seen_live": now}
    save_codex_index(idx)

def cmd_codex_switch(e):
    aid = e["account_id"]
    who = e.get("email") or aid
    stored = keychain_read(STORE_SVC, codex_key(aid))
    if not stored:
        _fail(f"{who} isn't captured — sign into it with `codex login` once, then switching works")
    cur = codex_read_auth_raw()
    cur_aid = codex_identity(cur)[0]
    if cur and cur_aid:
        if not codex_store_auth(cur_aid, cur):     # keep the outgoing account switchable-back-to
            _fail("couldn't stash the current Codex credential — unlock the Keychain and retry")
        # compact, matching the stash: `security` hands back a newline-bearing secret hex-encoded,
        # and one encoding for one kind of blob keeps a backup comparable to a stash
        if not keychain_write(STORE_SVC, CODEX_PREV_KEY, json.dumps(json.loads(cur), separators=(",", ":"))):
            _fail("couldn't back up the current Codex credential — switch did not happen")
    else:
        # Nothing identifiable is being displaced (signed out, or a file we can't read). Drop any
        # older backup rather than leave --undo pointing at an account this switch didn't replace.
        keychain_delete(STORE_SVC, CODEX_PREV_KEY)
    if not codex_write_auth(stored):
        _fail(f"couldn't write ~/.codex/auth.json — switch to {who} did not happen")
    codex_mark_signed_in(aid)
    record_last_switch("codex")
    clear_cache()
    # The tokens go over as captured; codex refreshes them itself on its next run. If OpenAI rotated
    # the refresh token after this snapshot was taken, that run asks for a login instead.
    _report_switch(f"switched Codex to {who}", "")

def cmd_codex_undo():
    prev = keychain_read(STORE_SVC, CODEX_PREV_KEY)
    if not prev:
        _fail("nothing to undo")
    if not codex_write_auth(prev):
        _fail("couldn't restore the previous Codex account")
    aid = codex_identity(prev)[0]
    if aid: codex_mark_signed_in(aid)
    keychain_delete(STORE_SVC, CODEX_PREV_KEY)
    record_last_switch("codex")     # an undo is a manual choice — the holdoff must protect it
    clear_cache()
    _report_switch("restored the previous Codex account", "")

def cmd_switch(target):
    # Undo reverses the last switch, so it follows the marker — but only while that provider still
    # holds a backup. Once a Codex undo has consumed its own, an earlier Claude switch is still
    # undoable, and falling through is what keeps it reachable.
    if target == "--undo":
        if last_switch_provider() == "codex" and keychain_read(STORE_SVC, CODEX_PREV_KEY):
            cmd_codex_undo(); return
    elif target:
        provider, e, note = resolve_target(target)
        if note and not e:
            _fail(note)
        if note:
            print(note, file=sys.stderr)
        if provider == "codex":
            cmd_codex_switch(e); return
    if target == "--undo":
        prev = keychain_read(STORE_SVC, PREV_KEY)
        if not prev:
            print("nothing to undo", file=sys.stderr); sys.exit(1)
        try:
            blob = json.loads(prev).get("claudeAiOauth")
        except Exception:
            blob = None
        if not blob or not write_live(blob):
            _fail("couldn't restore the previous account — no writable credential store")
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
        record_last_switch("claude")   # an undo is a manual choice — the holdoff must protect it
        clear_cache()
        _report_switch(f"restored the previous account", note); return

    e = resolve_account(target)
    if not e:
        _fail(f"unknown account: {target}")
    ok, msg, note = _switch_claude(e)
    if not ok:
        _fail(msg)
    record_last_switch("claude")
    clear_cache()                                     # so the post-switch refresh shows the new active account
    _report_switch(msg, note)

def _switch_claude(e):
    """Move the live credential + profile to account entry `e`. Returns (ok, message, note):
    ok False means nothing moved; a non-empty note means the credential moved but the profile
    didn't. No exits — the auto-switch path calls this where dying would kill a render."""
    sec = load_secret(e["uuid"])
    gap = _secret_gap(sec)
    if gap:
        return False, f"{e['email']} {gap}", ""

    # a full-life token, not merely an unexpired one: this is written into Claude Code's item and
    # left there, and a token with minutes left would strand the account as soon as they ran out
    token, err = token_for_parked(e["uuid"], min_life_ms=SWITCH_MIN_LIFE_MS)
    if err:
        return False, f"can't switch to {e['email']}: {err}", ""
    sec = load_secret(e["uuid"]) or sec               # re-read: the refresh may have rotated it;
                                                      # a failed re-read falls back, never crashes

    blob = {"accessToken": sec.get("accessToken") or token,
            "refreshToken": sec.get("refreshToken"),
            "expiresAt": sec.get("expiresAt")}
    blob.update({k: sec[k] for k in BLOB_META if sec.get(k) is not None})   # never write nulls

    if live_store() is None:
        return False, "no Claude Code credential store found — sign in with the claude CLI first", ""
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
        return False, f"couldn't write the credential — switch to {e['email']} did not happen", ""

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

    return True, f"switched to {e['email']}", note

# ---- signing an account back in ----------------------------------------------
# An account the server signed out can only come back through a real OAuth sign-in, and that
# has to happen in the claude CLI. Everything around that one interactive step is mechanical:
# spot the credential it writes, capture it, and put the CLI back on the account it was on.
# Doing all of it here is what keeps recovery a single action from any surface.

LOGIN_WAIT_S = 300      # a browser round trip; past this the sign-in was abandoned
LOGIN_POLL_S = 3
LOGIN_SCRIPT = os.path.join(STATE_DIR, "relogin.command")
# The sign-in's exit status, written by the script itself. A wait that only ever ended on a
# new credential or a five-minute timeout would hold the app's controls for the whole window
# after a sign-in the user had already given up on.
LOGIN_STATUS = os.path.join(STATE_DIR, "relogin.status")

# The desktop app injects its own host auth into every process it launches, and a `claude`
# that authenticates that way never writes the Keychain item this tool reads — the sign-in
# would report success and leave the account just as signed out. Launch it without them.
HOST_AUTH_ENV = ("CLAUDE_CODE_SDK_HAS_HOST_AUTH_REFRESH", "ANTHROPIC_BASE_URL")

def _launch_login(email):
    """Open a Terminal window running the CLI's sign-in for `email`. Returns (ok, message).

    A window of the user's own is the only place this can go: the flow is interactive, it
    prints a URL, and it may ask for a browser. Passing --email means the account being
    recovered is the one already filled in on the login page.
    """
    unset = " ".join(f"-u {v}" for v in HOST_AUTH_ENV)
    # Every interpolation is quoted: an address reaches here from the accounts API, and this
    # file is executed as a shell script.
    script = f"""#!/bin/sh
# Written by claude-usage for a one-off sign-in. Safe to delete.
echo {shlex.quote(f"Signing {email} back in for claude-usage.")}
echo
env {unset} claude auth login --email {shlex.quote(email)}
status=$?
echo "$status" > {shlex.quote(LOGIN_STATUS)}
echo
if [ "$status" -eq 0 ]; then
  echo "Signed in. The menu bar takes it from here; you can close this window."
else
  echo "Sign-in did not finish. Close this window and start it again from the menu bar."
fi
"""
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        # an earlier run's status would read as this one finishing the moment it starts
        if os.path.exists(LOGIN_STATUS):
            os.remove(LOGIN_STATUS)
        with open(LOGIN_SCRIPT, "w") as f:
            f.write(script)
        os.chmod(LOGIN_SCRIPT, 0o700)
    except Exception as ex:
        return False, f"couldn't write the sign-in script ({ex})"
    try:
        subprocess.run(["open", "-a", "Terminal", LOGIN_SCRIPT],
                       check=True, capture_output=True, timeout=20)
    except Exception as ex:
        return False, f"couldn't open a Terminal window to sign in ({ex})"
    return True, ""

def _login_gave_up():
    """Whether the sign-in has already finished without succeeding. A zero status is not an
    answer: the credential is written before the CLI exits, but identifying it can need a
    retry or two, so a clean exit keeps the wait running."""
    try:
        with open(LOGIN_STATUS) as f:
            return int(f.read().strip()) != 0
    except Exception:
        return False        # not written yet, or unreadable: still in progress as far as we know

def await_new_live(before, deadline=None):
    """Wait for the CLI to write a credential that isn't `before`, and say whose it is.

    Identity comes from /profile rather than a stored-token match: a fresh sign-in mints a
    token this tool has never seen, which is the whole point of waiting for it. None means
    nothing landed — either the sign-in failed or it never finished.
    """
    if deadline is None:
        deadline = time.time() + LOGIN_WAIT_S
    while time.time() < deadline:
        time.sleep(LOGIN_POLL_S)
        live = read_live()
        if not live or not live.get("accessToken") or live.get("refreshToken") == before:
            if _login_gave_up():
                return None
            continue
        try:
            uuid = api_get(PROFILE_URL, live["accessToken"]).get("account", {}).get("uuid")
        except Exception:
            continue        # a half-written blob, or a token the API hasn't caught up with
        if uuid:
            return uuid
    return None

def _hand_cli_back(prev, now_uuid, now_email):
    """Return the CLI to the account it was on before the sign-in displaced it. Returns a
    sentence to append to the outcome: a recovery that leaves the CLI somewhere other than
    where it started is a second surprise, and one discovered later."""
    if not prev:
        # nothing identified what this displaced, so there is nowhere to hand it back to
        return f" The CLI is now on {now_email}."
    if prev["uuid"] == now_uuid:
        return ""
    ok, msg, note = _switch_claude(prev)
    if not ok:
        return f" The CLI is now on the account you signed in; couldn't move it back ({msg})."
    record_last_switch("claude")            # a deliberate move: the auto-switch holdoff covers it
    clear_cache()                           # the credential moved last, so the clear comes last
    return f" CLI is back on {prev['email']}." + (note if note else "")

def cmd_relogin(target):
    provider, e, note = resolve_target(target)
    if not e:
        _fail(note)
    if provider == "codex":
        _fail("relogin is for Claude accounts; sign Codex in with `codex login`")
    if note:
        print(note, file=sys.stderr)
    prev_uuid = active_uuid_only()
    prev = next((x for x in load_index() if x["uuid"] == prev_uuid), None)
    before = (read_live() or {}).get("refreshToken")

    ok, msg = _launch_login(e["email"])
    if not ok:
        _fail(msg)
    print(f"signing {e['email']} in — finish it in the Terminal window that just opened",
          flush=True)

    uuid = await_new_live(before)
    if not uuid:
        why = ("the sign-in didn't complete" if _login_gave_up()
               else f"no sign-in landed within {LOGIN_WAIT_S // 60} minutes")
        _fail(f"{why}, so {e['email']} is still signed out")
    ingest_live(load_index())               # store the new token; that is what drops the latch
    clear_cache()
    if uuid != e["uuid"]:
        who = next((x["email"] for x in load_index() if x["uuid"] == uuid), uuid)
        _fail(f"that signed in {who}, not {e['email']}, so {e['email']} is still signed out."
              + _hand_cli_back(prev, uuid, who))
    if (load_secret(e["uuid"]) or {}).get("needsLogin"):
        # ingest_live declined the capture (a team context over a personal entry) or the
        # Keychain write failed — either way the account is still parked on a dead grant
        _fail(f"{e['email']} signed in, but its credentials couldn't be captured, "
              f"so it still reads as signed out. Run `claude-usage doctor` for the state."
              + _hand_cli_back(prev, uuid, e["email"]))
    print(f"{e['email']} is signed in again." + _hand_cli_back(prev, uuid, e["email"]))

# ---- auto-switch on limit hit ------------------------------------------------
# Opt-in: when the ACTIVE account's 5-hour or weekly window is spent, move the CLI credential to
# the best parked account so new work continues. The check rides the normal refresh (every surface
# funnels through collect()), so reaction time is bounded by the refresh interval — that latency is
# accepted; there is no event to hook when a limit trips. It only ever moves the CLI
# credential; the desktop app signs in separately and is left alone.

AUTOSWITCH = os.path.join(STATE_DIR, "autoswitch.json")
AUTO_COOLDOWN_S   = 15 * 60   # after an auto-switch: absorb API lag on the new window, no flapping
MANUAL_HOLDOFF_S  = 10 * 60   # after a manual switch: the user chose an account — don't fight them
STRANDED_RENOTIFY_S = 60 * 60 # while every account is spent, remind at most hourly
WEEKLY_REFUGE_MAX = 95        # a candidate this close to its weekly cap isn't a refuge
DRAIN_MIN_HEADROOM = 40       # 5-hour room a drain target must have — landing on a nearly-spent
                              # window would just trigger the next swap within the hour

def load_autoswitch():
    try:
        with open(AUTOSWITCH) as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}

def save_autoswitch(st):
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        _replace_file(AUTOSWITCH, json.dumps(st, indent=2))
    except Exception:
        pass

def update_autoswitch(updates):
    """Merge runtime keys into autoswitch.json against a FRESH read, under an advisory lock.
    The refresh holds its loaded copy across network calls, and the user can flip
    `enabled`/`scoped` in that window — writing the held copy back whole would silently revert
    their toggle. The lock closes the same hole between two concurrent writers (menu-bar tick
    and a CLI run), whose read-modify-write cycles would otherwise drop each other's keys."""
    import fcntl
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(AUTOSWITCH + ".lock", "w") as lk:
            try:
                fcntl.flock(lk, fcntl.LOCK_EX)
            except Exception:
                pass
            st = load_autoswitch()
            st.update(updates)
            save_autoswitch(st)
        return st
    except Exception:
        st = load_autoswitch()
        st.update(updates)
        save_autoswitch(st)
        return st

def _recent_stamp(record):
    """The record's local_short timestamp, or None when it isn't recent (older than a day)
    or carries an unusable ts."""
    ts = (record or {}).get("ts")
    if not isinstance(ts, (int, float)) or time.time() - ts >= 86400:
        return None
    return local_short(datetime.fromtimestamp(ts, timezone.utc))

def last_auto_line(st=None):
    """The one sentence describing the last auto-switch, shared by every text surface."""
    last = (st if st is not None else load_autoswitch()).get("last_auto") or {}
    when = _recent_stamp(last)
    if not when:
        return None
    tail = " — profile name mismatch, see notification" if last.get("partial") else ""
    return (f"auto-switched {last.get('from')} → {last.get('to')} "
            f"({last.get('reason')}, {when}){tail}")

def _win_pct(r, key):
    return (r.get(key) or {}).get("pct")

def _limit_windows(r, scoped=False):
    """Every window that can bind this account, as (name, pct, resets_at, refuge_max). A window
    triggers exhaustion at 100; it disqualifies a refuge at its refuge_max — the weekly-flavored
    ones disqualify early (WEEKLY_REFUGE_MAX) because they won't reset within hours. Exhaustion,
    refuge-fitness, relief time, and the reason string all read this one list, so a new window
    kind added here binds everywhere at once."""
    wins = [("5-hour", _win_pct(r, "five_hour"), (r.get("five_hour") or {}).get("resets_at"), 100),
            ("weekly", _win_pct(r, "seven_day"), (r.get("seven_day") or {}).get("resets_at"),
             WEEKLY_REFUGE_MAX)]
    if scoped:
        wins += [(f"{s.get('model') or 'scoped'} weekly", s.get("pct"), s.get("resets_at"),
                  WEEKLY_REFUGE_MAX) for s in r.get("scoped") or []]
    return wins

def exhaustion_reason(r, scoped=False):
    """The window at its cap, as notification text — or None, which is also the exhaustion test.
    Hard exhaustion only, never predictive: switching early strands paid budget on the account
    being left. With scoped on, a model-scoped weekly cap (e.g. Fable) counts too: for
    model-heavy work it is the binding limit long before the overall weekly is."""
    for name, pct, _ra, _cap in _limit_windows(r, scoped):
        if pct is not None and pct >= 100:
            return f"{name} limit"
    return None

def account_exhausted(r, scoped=False):
    return exhaustion_reason(r, scoped) is not None

TIER_RANK = {"Max 20x": 4, "Max 5x": 3, "Max": 2, "Pro": 1}   # ranks plan_name's spellings

def _tier_rank(r):
    return TIER_RANK.get(plan_name(r), 0)

def _weekly_reset_ts(r):
    dt = parse_dt((r.get("seven_day") or {}).get("resets_at"))
    return dt.timestamp() if dt else float("inf")

def auto_pick(rows, has_full_creds, scoped=False):
    """Accounts worth switching to, best first. Drain strategy: among accounts with at least
    DRAIN_MIN_HEADROOM of 5-hour room, spend the weekly budget that expires soonest — unspent
    weekly capacity evaporates at reset, so the account resetting first is the one to burn
    (ties: higher plan tier, then more 5-hour room). Thin-headroom accounts come after those,
    best-room first — a last resort, not a drain target. Rows missing either window number are
    excluded — a refuge must be verifiably usable, not just not-known-bad."""
    roomy, thin = [], []
    for r in rows:
        if not is_claude(r): continue
        # stale as well as errored: a row holding last-known numbers is not-known-bad, and the
        # bar here is verifiably usable — switching onto cached headroom is how you land on an
        # account that filled up while it was unreadable
        if r.get("active") or r.get("error") or r.get("stale") or r.get("is_team"): continue
        fh, wk = _win_pct(r, "five_hour"), _win_pct(r, "seven_day")
        if fh is None or wk is None: continue
        if any(pct is not None and pct >= cap for _n, pct, _ra, cap in _limit_windows(r, scoped)):
            continue
        if not has_full_creds(r.get("uuid")): continue
        (roomy if 100 - fh >= DRAIN_MIN_HEADROOM else thin).append(r)
    return (sorted(roomy, key=lambda r: (_weekly_reset_ts(r), -_tier_rank(r),
                                         _win_pct(r, "five_hour")))
            + sorted(thin, key=lambda r: (_win_pct(r, "five_hour"), -_tier_rank(r),
                                          _weekly_reset_ts(r))))

def _usable_at(r, scoped=False):
    """When this account next has room: the latest reset among its binding windows. None if any
    binding window's reset can't be determined — an unknown blocker makes the whole answer
    unknown, not optimistic."""
    blockers = []
    for _name, pct, resets_at, cap in _limit_windows(r, scoped):
        if pct is not None and pct >= cap:
            dt = parse_dt(resets_at)
            if dt is None: return None
            blockers.append(dt.timestamp())
    return max(blockers) if blockers else None

def earliest_relief(rows, scoped=False):
    """The soonest moment any personal account frees up, or None."""
    times = []
    for r in rows:
        if not is_claude(r) or r.get("error") or r.get("stale") or r.get("is_team"): continue
        t = _usable_at(r, scoped)
        if t is not None: times.append(t)
    return min(times) if times else None

def auto_decision(rows, last_switch, now, has_full_creds, scoped=False):
    """The pure core: ('idle'|'cooldown'|'stranded'|'switch', payload). payload is the ordered
    candidate list for 'switch', the earliest-relief epoch (or None) for 'stranded'."""
    active = next((r for r in rows if is_claude(r) and r.get("active")), None)
    if not active or active.get("error") or active.get("stale") \
            or not account_exhausted(active, scoped):
        return "idle", None
    ts = (last_switch or {}).get("ts")
    if ts:
        # any provider's manual switch holds: the record is single-slot, so filtering by provider
        # would let a Codex switch erase a Claude switch's holdoff
        hold = AUTO_COOLDOWN_S if last_switch.get("auto") else MANUAL_HOLDOFF_S
        if 0 <= now - ts < hold:
            return "cooldown", None
    cands = auto_pick(rows, has_full_creds, scoped)
    if not cands:
        return "stranded", earliest_relief(rows, scoped)
    return "switch", cands

def _secret_gap(sec):
    """Why this stored secret can't be switched to, or None if it can. The one predicate both
    the candidate filter and the switch itself apply — they must never disagree."""
    if not sec or not sec.get("refreshToken"):
        return "isn't captured — log into it once with the claude CLI"
    if not sec.get("scopes"):
        # captured before we stored the full blob; writing a partial one could break the CLI login
        return "needs one login with the claude CLI to store its full credentials"
    return None

def _has_full_creds(uuid):
    return _secret_gap(load_secret(uuid)) is None

def _notify(title, body):
    """Post a macOS notification. Auto-switching changes which account gets billed, so it must
    never be silent — but a notification failure must not take the refresh down. ensure_ascii
    stays off: AppleScript has no \\uXXXX escape, so an escaped name would render garbled."""
    try:
        subprocess.run(["osascript", "-e",
                        f"display notification {json.dumps(body, ensure_ascii=False)} "
                        f"with title {json.dumps(title, ensure_ascii=False)}"],
                       capture_output=True, timeout=10)
    except Exception:
        pass

def _notify_hourly(state_key, title, body):
    """A notification that repeats at most hourly while its condition persists."""
    now = time.time()
    if now - (load_autoswitch().get(state_key) or 0) >= STRANDED_RENOTIFY_S:
        _notify(title, body)
        update_autoswitch({state_key: now})

AUTOSWITCH_LOCK = os.path.join(STATE_DIR, "autoswitch.lock")

def _switch_lock():
    """One switcher at a time: concurrent refreshes (menu-bar tick + a CLI run) that both see an
    exhausted account must not both switch — the second would back up the first's freshly written
    credential, destroying the undo path. O_EXCL is the arbiter; a stale lock (holder crashed)
    expires after 2 minutes. Returns an fd to close+unlink, or None if another switcher holds it."""
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        return os.open(AUTOSWITCH_LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        try:
            if time.time() - os.path.getmtime(AUTOSWITCH_LOCK) > 120:
                os.remove(AUTOSWITCH_LOCK)
                return os.open(AUTOSWITCH_LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except Exception:
            pass
        return None
    except Exception:
        return None

def _release_switch_lock(fd):
    try:
        os.close(fd)
        os.remove(AUTOSWITCH_LOCK)
    except Exception:
        pass

def maybe_auto_switch(rows):
    """Called on every fresh refresh; returns the rows to render (active flags moved
    if a switch fired)."""
    st = load_autoswitch()
    if mock_enabled():
        return rows
    if st.get("enabled"):
        rows = _auto_tick(rows, st, time.time())
    return rows

def _auto_tick(rows, st, now):
    scoped = bool(st.get("scoped"))
    kind, payload = auto_decision(rows, last_switch_info(), now, _has_full_creds, scoped)
    if kind == "stranded":
        when = (f"Earliest reset {local_short(datetime.fromtimestamp(payload, timezone.utc))}."
                if payload else "Reset times unknown.")  # unknown must still notify — silence
                                                         # here reads as "everything is fine"
        latched = [r for r in rows if is_claude(r) and r.get("needs_login")]
        if latched:
            # waiting for a reset can't relieve a signed-out account; the notification has
            # to name the actual remedy or it points the user at the wrong wait
            n = len(latched)
            who = "1 parked account needs" if n == 1 else f"{n} parked accounts need"
            body = (f"No account has room. {who} a fresh sign-in. "
                    f"Use Sign in on its card in the menu bar. {when}")
        else:
            body = f"Every account is at its limit. {when}"
        _notify_hourly("last_stranded_notify", "claude-usage", body)
        return rows
    if kind != "switch":
        return rows
    lock = _switch_lock()
    if lock is None:
        return rows                                    # another refresh is mid-switch; its tick wins
    try:
        if auto_decision(rows, last_switch_info(), time.time(), _has_full_creds, scoped)[0] \
                != "switch":
            return rows                                # the lock wait outdated the decision
        active = next(r for r in rows if is_claude(r) and r.get("active"))
        idx = {e["uuid"]: e for e in load_index()}
        for cand in payload:
            e = idx.get(cand.get("uuid"))
            if not e: continue
            try:
                ok, msg, note = _switch_claude(e)
            except Exception:                          # a crash here must not take down the render
                ok, note = False, ""
            if not ok: continue                        # dead refresh token etc. — try the next one
            reason = exhaustion_reason(active, scoped)
            update_autoswitch({"last_auto": {"ts": now, "from": active.get("email"),
                                             "to": e["email"], "reason": reason,
                                             "partial": bool(note)},
                               "last_stranded_notify": None, "last_failed_notify": None})
            record_last_switch("claude", auto=True)
            # notification copy stays dash-free: no em dashes in anything osascript posts
            body = f"{active.get('email')} hit its {reason}. CLI now on {e['email']}."
            if note:
                body += " Note: ~/.claude.json still shows the old account's name."
            _notify("claude-usage auto-switch", body)
            for r in rows:                             # this tick renders the switch it just made —
                if is_claude(r):                       # usage numbers can't have changed, only who
                    r["active"] = r is cand            # is active, so no second network sweep
            clear_cache()
            save_cache(rows, time.time())
            return rows
        # every candidate refused to switch — that's as wrong as stranded and must not be silent
        _notify_hourly("last_failed_notify", "claude-usage",
                       f"{active.get('email')} is at its limit but no account could be switched "
                       f"to. Check `claude-usage doctor`.")
        return rows
    finally:
        _release_switch_lock(lock)

def cmd_autoswitch(args):
    arg = args[0] if args else ""
    if arg not in ("", "on", "off", "scoped"):
        # an unknown word must not fall through to the status view: `autoswitch onn` read as
        # "show status" leaves the user believing they armed a feature that is still off
        print("usage: claude-usage autoswitch [on|off|scoped on|off]",
              file=sys.stderr)
        sys.exit(2)
    if arg == "on":
        update_autoswitch({"enabled": True})
        print("auto-switch ON — when the active account's 5-hour or weekly window hits 100%,\n"
              "the CLI moves to the parked account whose weekly window resets soonest\n"
              "(among those with real 5-hour room).\n"
              "turn it off with:  claude-usage autoswitch off\n"
              "also switch when a model-scoped weekly cap (e.g. Fable) fills:\n"
              "  claude-usage autoswitch scoped on")
    elif arg == "off":
        update_autoswitch({"enabled": False})
        print("auto-switch off")
    elif arg == "scoped":
        sub = args[1] if len(args) > 1 else ""
        if sub not in ("on", "off"):
            print("usage: claude-usage autoswitch scoped on|off", file=sys.stderr); sys.exit(2)
        st = update_autoswitch({"scoped": sub == "on"})
        print("scoped trigger " + ("ON — a model-scoped weekly cap at 100% now switches too"
                                   if st["scoped"] else "off"))
        if st["scoped"] and not st.get("enabled"):
            print("note: auto-switch itself is off — arm it with `claude-usage autoswitch on`")
    else:
        st = load_autoswitch()
        print("auto-switch is " + ("ON" if st.get("enabled") else "off")
              + (" (scoped trigger on)" if st.get("scoped") else ""))
        line = last_auto_line(st)
        if line:
            print(line)

# ---- native app presence (for doctor) ---------------------------------------

def _app_bundle_path():
    for base in ("/Applications", os.path.expanduser("~/Applications")):
        p = os.path.join(base, APP_BUNDLE)
        if os.path.isdir(p):
            return p
    return None

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
    if mock_enabled():
        print(f"\n  {C['y']}⚠{C['x']} mock mode is ON — the app and table are showing invented data")
        print(f"      {C['dim']}turn it off with: claude-usage mock off{C['x']}")

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
        if r.get("needs_login"):
            # the row's error is the one canonical wording; doctor only adds the remedy
            say("bad", f"{r['email']}: {r['error']}",
                "its refresh token was revoked server-side and nothing retries until it signs "
                f"in again — `claude-usage relogin {r['email']}`, or Sign in on its card in "
                "the menu bar, does the whole recovery.")
        elif r.get("error"):
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
            "the menu-bar app may not be running.")

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
            if r.get("active"): continue
            if r.get("switchable"):
                # the prefix unconditionally: an email registered on both providers resolves to
                # Claude without it, so the command we print would switch the other provider
                say("ok", f"{r['email']}: parked — switch to it with "
                          f"`claude-usage switch {CODEX_PREFIX}{r['email']}`")
            else:
                say("warn", f"{r['email']}: shown from a past snapshot, and switching to it won't work",
                    "sign into it with `codex login` once to capture its credential.")

    section("Menu bar")
    bundle = _app_bundle_path()
    if not bundle:
        say("warn", "the menu-bar app isn't built", "run `claude-usage app` to build and launch it.")
    else:
        if subprocess.run(["pgrep", "-x", APP_EXECUTABLE], capture_output=True).returncode != 0:
            say("warn", f"{APP_BUNDLE} is built but not running", "open it, or run `claude-usage app`.")
        else:
            say("ok", f"{APP_BUNDLE} is running")
        # the app runs the script at the absolute path baked in at build time — a moved or
        # deleted checkout strands it with no other symptom than a failing refresh
        try:
            import plistlib
            with open(os.path.join(bundle, "Contents", "Info.plist"), "rb") as f:
                backend = plistlib.load(f).get("CUBackend")
            if backend and not os.path.exists(backend):
                say("bad", f"the app's backend script is missing: {backend}",
                    "run `claude-usage app` from your current checkout to rebuild.")
            elif backend and os.path.realpath(backend) != os.path.realpath(__file__):
                say("warn", "the app was built from a different checkout than this command",
                    f"it runs {backend}; rebuild with `claude-usage app` if that's stale.")
        except Exception:
            pass
    stale = glob.glob(os.path.expanduser(
        "~/Library/Application Support/xbar/plugins/claude-usage.*.sh"))
    if stale:
        say("warn", "an old xbar plugin link is still present",
            "delete " + ", ".join(stale) + " — see the README's upgrade note.")
    leftovers = sorted(os.path.basename(p) for p in glob.glob(os.path.join(STATE_DIR, "desktop-*")))
    if leftovers:
        armed = " (its auto-switch setting no longer does anything)" if \
            load_autoswitch().get("desktop") else ""
        say("warn", "desktop app switching was removed, but its saved state is still on disk" + armed,
            "the stashes hold saved sign-ins; when you no longer want them: "
            "rm -r ~/.claude-usage/desktop-*")

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
    print("  • optionally build the menu-bar app (compiled locally; needs the Xcode Command Line Tools)")
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

    # optional: the menu bar (the primary experience)
    if not shutil.which("swiftc"):
        print("Menu bar: needs the Xcode Command Line Tools (xcode-select --install);")
        print("run `claude-usage app` once they're installed.")
    elif _ask("Build and launch the menu-bar app?", True):
        if not build_app():
            print("  the build didn't finish — fix the error above and run `claude-usage app`.")

    print("\nRegistering the account you're signed into…")
    rows = collect(act=False)
    render_table(rows)
    if rows:
        print("From now on, log into another Claude account with the `claude` CLI once and it appears")
        print("on the next run — and in the menu bar on its next refresh. No need to run setup again.\n")

# ---- commands ---------------------------------------------------------------

# ---- insights (local transcript scan) ---------------------------------------
# The usage endpoint says how much window is left; the local Claude Code transcripts say where it
# went. Each assistant message there records its model and token counters, so the trailing week can
# be priced in API list terms — "what this usage would have cost through the API", a common unit
# for comparing models and days, never a bill (subscriptions are what's actually paid).

CLAUDE_PROJECTS = os.path.expanduser(os.environ.get("CU_PROJECTS") or "~/.claude/projects")
INSIGHTS_CACHE = os.path.join(STATE_DIR, "insights.json")   # non-secret: aggregates only
INSIGHTS_SCHEMA = 3                   # bump when the payload shape or semantics change
INSIGHTS_TTL = 1800                   # a scan is seconds; a menu open should never pay it twice
INSIGHTS_WINDOW_D = 7
# USD per MTok: (input, output, cache write, cache read) — API list prices as of Aug 2026.
# The gpt row prices Codex turns: cache write is unused there (Codex reports cached input reads).
PRICING = {"opus": (5.0, 25.0, 6.25, 0.50), "fable": (10.0, 50.0, 12.50, 1.00),
           "sonnet": (3.0, 15.0, 3.75, 0.30), "haiku": (1.0, 5.0, 1.25, 0.10),
           "gpt": (1.25, 10.0, None, 0.125)}
FAMILY_NAMES = {"opus": "Opus", "fable": "Fable", "sonnet": "Sonnet", "haiku": "Haiku", "gpt": "GPT"}

def _model_family(model):
    m = (model or "").lower()
    for k in PRICING:
        if k in m:
            return k
    return None

EFFORT_ORDER = {"max": 0, "xhigh": 1, "high": 2, "medium": 3, "low": 4, "minimal": 5, "": 6}

def _model_display(model):
    """Row label for the mix: family + version — "Opus 4.8" and "Opus 5" are different spends.
    Effort splits again inside each row (transcripts record it per message). Pricing stays per
    family; versions within a family share list rates."""
    fam = _model_family(model)
    if not fam:
        return None, None
    m = (model or "").lower()
    if fam == "gpt":
        # conventional GPT rendering joins the version and spaces the variant: "GPT-5.6 sol"
        t = re.match(r".*?gpt[-_](\d+(?:\.\d+)*)(?:[-_](.+))?$", m)
        if t:
            return fam, "GPT-" + t.group(1) + (f" {t.group(2)}" if t.group(2) else "")
        return fam, "GPT"
    # version parts are 1-2 digits; an 8-digit date stamp is not a version. Modern ids put the
    # version after the family ("opus-4-5-20260805"), legacy ids before it ("3-5-sonnet-20241022").
    ver = (re.search(rf"{fam}[-_](\d{{1,2}}(?:[-.]\d{{1,2}})?)(?!\d)", m)
           or re.search(rf"(\d{{1,2}}(?:[-_.]\d{{1,2}})?)[-_]{fam}", m))
    return fam, FAMILY_NAMES[fam] + (f" {ver.group(1).replace('-', '.').replace('_', '.')}" if ver else "")

def _msg_cost(fam, u):
    pi, po, pw, pr = PRICING[fam]
    return ((u.get("input_tokens", 0) or 0) * pi + (u.get("output_tokens", 0) or 0) * po
            + (u.get("cache_creation_input_tokens", 0) or 0) * pw
            + (u.get("cache_read_input_tokens", 0) or 0) * pr) / 1e6

APP_BUNDLE = "Claude Usage.app"
APP_EXECUTABLE = "ClaudeUsage"

def codex_session_files():
    """Every Codex rollout file. Recursive: independent of the YYYY/MM/DD layout Codex uses today."""
    return glob.glob(os.path.join(CODEX_SESSIONS, "**", "*.jsonl"), recursive=True)

def _tally(agg, fam, name, effort, cost, tokens):
    """One accounting entry for both scan loops — a single schema for a model row, so a field added
    for one provider exists for the other."""
    s = agg.setdefault(name, {"family": FAMILY_NAMES[fam], "msgs": 0, "input": 0, "output": 0,
                              "cache_write": 0, "cache_read": 0, "cost": 0.0, "efforts": {}})
    s["msgs"] += 1
    for k, v in tokens.items():
        s[k] += v
    s["cost"] += cost
    ef = s["efforts"].setdefault(effort or "", {"cost": 0.0, "msgs": 0})
    ef["cost"] += cost
    ef["msgs"] += 1

def compute_insights(now=None):
    """Scan the trailing week's transcripts into per-model totals. A full rescan of the window
    rather than an incremental cache: the window keeps the file set small (mtime pre-filter), and
    seeing the whole window at once is what makes dedupe by message id correct — resumed and forked
    sessions duplicate messages across files."""
    now = now or time.time()
    cut = now - INSIGHTS_WINDOW_D * 86400
    # timestamps are UTC ISO strings, so boundaries expressed in the same format compare
    # lexicographically — to the second for the window cut, and at the local midnight (in UTC
    # terms) for the "today" figure
    cut_iso = datetime.fromtimestamp(cut, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    today_utc = (datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
                 .astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"))
    seen, agg = set(), {}
    today_cost = 0.0
    for fp in glob.glob(os.path.join(CLAUDE_PROJECTS, "**", "*.jsonl"), recursive=True):
        try:
            if os.path.getmtime(fp) < cut:
                continue
            with open(fp, errors="replace") as f:
                for line in f:
                    if '"usage"' not in line:
                        continue
                    try:
                        rec = json.loads(line)
                    except Exception:
                        continue                       # a half-written tail line skips cleanly
                    msg = rec.get("message") or {}
                    u = msg.get("usage")
                    if not isinstance(u, dict):
                        continue
                    ts = rec.get("timestamp") or ""
                    if ts[:19] < cut_iso:
                        continue
                    mid = msg.get("id")     # dedupe before parsing: duplicates shouldn't pay it
                    if mid:
                        if mid in seen:
                            continue
                        seen.add(mid)
                    fam, name = _model_display(msg.get("model"))
                    if not fam:
                        continue
                    c = _msg_cost(fam, u)
                    _tally(agg, fam, name, rec.get("effort"), c,
                           {"input": u.get("input_tokens", 0) or 0,
                            "output": u.get("output_tokens", 0) or 0,
                            "cache_write": u.get("cache_creation_input_tokens", 0) or 0,
                            "cache_read": u.get("cache_read_input_tokens", 0) or 0})
                    if ts >= today_utc:
                        today_cost += c
        except Exception:
            continue                                   # one unreadable file must not sink the scan
    def effort_slices(e):
        return [{"effort": k or None, "rank": EFFORT_ORDER.get(k, 9),
                 "cost": round(v["cost"], 2), "msgs": v["msgs"]}
                for k, v in sorted(e.items(), key=lambda kv: EFFORT_ORDER.get(kv[0], 9))]
    today_cost += _scan_codex(agg, cut, cut_iso, today_utc)
    models = [{"name": n, "family": s["family"], "msgs": s["msgs"], "input": s["input"],
               "output": s["output"], "cache_write": s["cache_write"],
               "cache_read": s["cache_read"], "cost": round(s["cost"], 2),
               "efforts": effort_slices(s["efforts"])}
              for n, s in sorted(agg.items(), key=lambda kv: -kv[1]["cost"])]
    return {"schema": INSIGHTS_SCHEMA, "as_of": round(now, 1), "ttl_s": INSIGHTS_TTL,
            "day": datetime.now().astimezone().strftime("%Y-%m-%d"),
            "window_days": INSIGHTS_WINDOW_D,
            "total_cost": round(sum(s["cost"] for s in agg.values()), 2),
            "today_cost": round(today_cost, 2), "models": models,
            "note": "API list-price equivalents, not billing"}

def _replace_file(path, text):
    """Write-then-rename, pid-suffixed: concurrent pollers may write at once, and a reader must
    never see a half-written file."""
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w") as f:
        f.write(text)
    os.replace(tmp, path)

def _scan_codex(agg, cut, cut_iso, today_utc):
    """Fold Codex rollouts into the mix. Each turn_context names the model and effort for the
    token_count events that follow it; cost is (input − cached)·in + cached·cached-read + output·out.
    Returns the window's Codex today-cost. Resumed rollouts replay their history, so events dedupe
    on (timestamp, running total)."""
    seen, today = set(), 0.0
    for fp in codex_session_files():
        try:
            if os.path.getmtime(fp) < cut:
                continue
            model = effort = None
            with open(fp, errors="replace") as f:
                for line in f:
                    if '"turn_context"' not in line and 'token_usage' not in line:
                        continue
                    try:
                        rec = json.loads(line)
                    except Exception:
                        continue
                    p = rec.get("payload") or {}
                    if rec.get("type") == "turn_context":
                        model = p.get("model") or model
                        effort = p.get("effort") or p.get("reasoning_effort") or effort
                        continue
                    info = p.get("info") or {}
                    u = info.get("last_token_usage") or {}
                    ts = rec.get("timestamp") or ""
                    if not u or ts[:19] < cut_iso:
                        continue
                    fam, name = _model_display(model)
                    if not fam:
                        continue
                    key = (ts, (info.get("total_token_usage") or {}).get("total_tokens"))
                    if key in seen:
                        continue
                    seen.add(key)
                    pi, po, _, pr = PRICING[fam]
                    i_all = u.get("input_tokens", 0) or 0
                    cached = u.get("cached_input_tokens", 0) or 0
                    out = u.get("output_tokens", 0) or 0
                    c = (max(0, i_all - cached) * pi + cached * pr + out * po) / 1e6
                    _tally(agg, fam, name, effort, c,
                           {"input": max(0, i_all - cached), "output": out, "cache_read": cached})
                    if ts >= today_utc:
                        today += c
        except Exception:
            continue                                   # one unreadable rollout must not sink the scan
    return today

def cmd_insights(as_json):
    if mock_enabled():
        data = mock_insights()
        if as_json:
            print(json.dumps(data, indent=2)); return
        print(f"\nmock insights — ${data['total_cost']:,.0f} this week\n"); return

    """Print the trailing-week transcript aggregate, computing at most once per INSIGHTS_TTL —
    callers poll freely and the scan runs only when it's due. A cache from another schema or
    another local day recomputes: the shape must match this printer, and the "today" figure must
    not survive midnight."""
    data = None
    try:
        with open(INSIGHTS_CACHE) as f:
            cached = json.load(f)
        if (cached.get("schema") == INSIGHTS_SCHEMA and time.time() - cached.get("as_of", 0) < INSIGHTS_TTL
                and cached.get("day") == datetime.now().astimezone().strftime("%Y-%m-%d")):
            data = cached
    except Exception:
        pass
    if data is None:
        data = compute_insights()
        try:
            os.makedirs(STATE_DIR, exist_ok=True)
            _replace_file(INSIGHTS_CACHE, json.dumps(data))
        except Exception:
            pass
    if as_json:
        print(json.dumps(data, indent=2))
        return
    print(f"past {data['window_days']} days · API list-price equivalent "
          f"(not billing) · today ${data['today_cost']:,.0f}")
    w = max([5] + [len(m["name"]) for m in data["models"]])
    def line(*cells):
        spec = [(w, "<"), (8, ">"), (9, ">"), (9, ">"), (10, ">"), (10, ">"), (9, ">")]
        print(" ".join(f"{c:{a}{wd}}" for c, (wd, a) in zip(cells, spec)))
    line("model", "msgs", "input", "output", "cache wr", "cache rd", "cost")
    for m in data["models"]:
        line(m["name"], f"{m['msgs']:,}", f"{m['input']:,}", f"{m['output']:,}",
             f"{m['cache_write']:,}", f"{m['cache_read']:,}", "$%.0f" % m["cost"])
    line("total", "", "", "", "", "", "$%.0f" % data["total_cost"])

def build_app(dev=False):
    """Compile native/ClaudeUsageBar.swift into "Claude Usage.app" and (re)launch it — /Applications
    when writable, else ~/Applications.

    Built locally with the Command Line Tools' swiftc, so there is nothing to sign or notarize —
    Gatekeeper doesn't quarantine binaries built on the machine itself. The bundle embeds this
    script's absolute path (CUBackend), which is how the app finds its backend; rebuilding from a
    different checkout repoints it."""
    # A dev build is a separate bundle and a separate executable name, so it installs beside the
    # everyday app and both can run: testing a branch must not cost the user their working bar.
    # Its icon is identical, so the two are told apart by which one you quit.
    bundle = "Claude Usage (dev).app" if dev else APP_BUNDLE
    executable = APP_EXECUTABLE + ("Dev" if dev else "")
    bundle_id = "com.allenmervia.claude-usage" + (".dev" if dev else "")
    src_dir = os.path.dirname(os.path.realpath(__file__))
    src = os.path.join(src_dir, "native", "ClaudeUsageBar.swift")
    if not os.path.exists(src):
        print("native/ClaudeUsageBar.swift not found next to this script", file=sys.stderr)
        return False
    swiftc = shutil.which("swiftc")
    if not swiftc:
        print("swiftc not found — install the Xcode Command Line Tools first:", file=sys.stderr)
        print("  xcode-select --install", file=sys.stderr)
        return False
    # /Applications when writable (login items and app registries treat it as home), else ~/Applications
    apps_dir = "/Applications" if os.access("/Applications", os.W_OK) else os.path.expanduser("~/Applications")
    app = os.path.join(apps_dir, bundle)
    macos_dir = os.path.join(app, "Contents", "MacOS")
    os.makedirs(macos_dir, exist_ok=True)
    binary = os.path.join(macos_dir, executable)
    print("compiling the menu-bar app (takes a moment the first time)…")
    r = subprocess.run([swiftc, "-O", "-swift-version", "5", "-parse-as-library", src, "-o", binary],
                      capture_output=True, text=True)
    if r.returncode != 0:
        print(r.stderr, file=sys.stderr)
        print("build failed", file=sys.stderr)
        return False
    import plistlib
    with open(os.path.join(app, "Contents", "Info.plist"), "wb") as f:
        plistlib.dump({
            "CFBundleIdentifier": bundle_id,
            "CFBundleName": "Claude Usage" + (" (dev)" if dev else ""),
            "CFBundleDisplayName": "Claude Usage" + (" (dev)" if dev else ""),
            "CFBundleExecutable": executable,
            "CFBundlePackageType": "APPL",
            "CFBundleShortVersionString": "1.0",
            "CFBundleVersion": "1",
            "LSMinimumSystemVersion": "13.0",
            "LSUIElement": True,          # menu-bar only: no Dock icon, no app switcher entry
            "NSHighResolutionCapable": True,
            "CUBackend": os.path.realpath(__file__),
        }, f)
    subprocess.run(["codesign", "--force", "--sign", "-", app], capture_output=True)
    subprocess.run(["pkill", "-x", executable], capture_output=True)   # replace a running copy
    time.sleep(0.3)
    subprocess.run(["open", app])
    print(f"launched {app}")
    return True

def cmd_app(dev=False):
    sys.exit(0 if build_app(dev) else 1)

def main():
    arg = sys.argv[1] if len(sys.argv) > 1 else ""
    if arg == "setup":
        cmd_setup(); return
    if arg == "app":
        cmd_app(dev="--dev" in sys.argv[2:]); return
    if arg == "desktop-switch":
        print("desktop app switching was removed — this tool covers the CLI account only",
              file=sys.stderr)
        sys.exit(2)
    if arg in ("install", "interval", "--xbar"):
        print(f"`{arg}` went with the xbar plugin — the menu bar is `claude-usage app` now"
              + ("; `--json` is the machine format" if arg == "--xbar" else ""), file=sys.stderr)
        sys.exit(2)
    if arg == "insights":
        cmd_insights("--json" in sys.argv[2:]); return
    if arg == "doctor":
        cmd_doctor(); return
    if arg == "switch":
        cmd_switch(sys.argv[2] if len(sys.argv) > 2 else ""); return
    if arg == "relogin":
        cmd_relogin(sys.argv[2] if len(sys.argv) > 2 else ""); return
    if arg == "mock":
        cmd_mock(sys.argv[2] if len(sys.argv) > 2 else ""); return
    if arg == "autoswitch":
        cmd_autoswitch(sys.argv[2:]); return
    if arg == "creds-debug":
        # Non-secret metadata about each stored credential, for debugging refresh failures.
        # One formatter for every row so the live and stored credentials stay grep-comparable,
        # and _secret_gap for completeness so this can never disagree with what switch accepts.
        def show(label, sec, live_ref):
            ts = sec.get("expiresAt")
            gap = _secret_gap(sec)
            print(f"{label:<22} refreshToken={'yes' if sec.get('refreshToken') else 'no'}"
                  f" needsLogin={bool(sec.get('needsLogin'))}"
                  f" same-as-live={bool(sec.get('refreshToken') and sec.get('refreshToken') == live_ref)}"
                  f" tokenHost={sec.get('tokenHost')}"
                  f" accessExpiresAt={datetime.fromtimestamp(ts / 1000, timezone.utc).isoformat() if ts else None}"
                  + (f"  [{gap}]" if gap else ""))
        live = read_live() or {}
        live_ref = live.get("refreshToken")
        if live:
            show("live", live, live_ref)
        for e in load_index():
            show(e["email"], load_secret(e["uuid"]) or {}, live_ref)
        return
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
        for aid, e in load_codex_index().items():
            if isinstance(e, dict):
                print(f"{e.get('email') or aid:<28} codex  {aid}")
        return
    if arg == "forget":
        key = sys.argv[2] if len(sys.argv) > 2 else ""
        provider, e, note = resolve_target(key)
        if not e:
            print(note, file=sys.stderr); sys.exit(1)
        if note: print(note, file=sys.stderr)            # names both providers; `codex:` picks the other
        # forget acts on the resolved account, so every name that switches also forgets — matching
        # the key again here would drop a label the resolver accepts
        who = e.get("email") or e.get("account_id") or e.get("uuid")
        if provider == "codex":
            aid = e["account_id"]
            idx = load_codex_index(); idx.pop(aid, None); save_codex_index(idx)
            item = codex_key(aid)
        else:
            save_index([x for x in load_index() if x["uuid"] != e["uuid"]])
            item = e["uuid"]
        if not keychain_delete(STORE_SVC, item):
            # the index entry is gone; say so rather than leave an orphan credential unmentioned
            print(f"  warning: couldn't delete {who}'s Keychain item — remove it by hand",
                  file=sys.stderr)
        print(f"forgot {who}")
        return
    rows = collect()
    if arg == "--json": render_json(rows)
    else: render_table(rows)

if __name__ == "__main__":
    main()
