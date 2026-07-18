#!/usr/bin/env python3
"""claude-usage — show 5-hour and weekly usage across several Claude accounts
without logging in and out.

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

Usage:
  claude-usage setup      guided first-time setup (register account, optional menu bar + PATH)
  claude-usage            table of all known accounts (default)
  claude-usage install    add the menu-bar view (installs xbar if needed, links + launches it)
  claude-usage --json     machine-readable JSON
  claude-usage --xbar     xbar/SwiftBar menu-bar format
  claude-usage capture    explicitly ingest the active account (same as a run)
  claude-usage list       list registered accounts
  claude-usage forget X   drop account by email or uuid
"""
import sys, os, json, time, subprocess, shutil, urllib.request, urllib.error
from datetime import datetime, timezone

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
FH_NEAR = 90   # at/above this 5-hour %, an account is treated as unusable *right now*
WK_NEAR = 90   # at/above this weekly %, an account has too little runway to recommend
COOLDOWN = 30  # s — rapid re-refreshes within this window reuse the last result, sparing the API

# ---- keychain helpers -------------------------------------------------------

def _sec(args, inp=None):
    return subprocess.run(["security", *args], capture_output=True, text=True, input=inp)

def keychain_read(service, account=None):
    args = ["find-generic-password", "-s", service, "-w"]
    if account: args = ["find-generic-password", "-s", service, "-a", account, "-w"]
    r = _sec(args)
    return r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else None

def keychain_write(service, account, secret):
    # Note: the secret is passed as an argv value, briefly visible to `ps`. `security` has no
    # non-interactive stdin path for this, and we stay stdlib-only, so this is a deliberate tradeoff.
    _sec(["add-generic-password", "-U", "-s", service, "-a", account, "-w", secret])

def keychain_delete(service, account):
    _sec(["delete-generic-password", "-s", service, "-a", account])

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

def store_secret(uuid, refresh, access=None, expires_at=None, host=None):
    keychain_write(STORE_SVC, uuid, json.dumps({
        "refreshToken": refresh, "accessToken": access,
        "expiresAt": expires_at, "tokenHost": host,
    }))

def load_secret(uuid):
    raw = keychain_read(STORE_SVC, uuid)
    return json.loads(raw) if raw else None

def token_for_parked(uuid, force=False):
    """Valid access token for a parked account, refreshing + rotating if needed.
    force=True skips the cached access token (used to recover from a 401 on a token that
    looked unexpired but was invalidated server-side)."""
    sec = load_secret(uuid)
    if not sec: return None, "not captured — sign into it once and re-run"
    now_ms = time.time() * 1000
    if not force and sec.get("accessToken") and sec.get("expiresAt", 0) > now_ms + 60_000:
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
    store_secret(uuid, newref, access, exp, host)   # persist rotated refresh token
    return access, None

def is_team_entry(e):
    return bool(e.get("seat_tier")) or e.get("org_type") in ("claude_team", "claude_enterprise")

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
    upsert(idx, {
        "uuid": uuid,
        "email": acct.get("email", uuid),
        "label": acct.get("display_name") or acct.get("email", uuid),
        "tier":  org.get("rate_limit_tier"),          # e.g. default_claude_max_20x / _5x / pro
        "org_type": org.get("organization_type"),     # claude_max / claude_team / claude_enterprise
        "seat_tier": org.get("seat_tier"),            # non-null => a team/enterprise seat
    })
    # keep this account's stored refresh token current from the live keychain
    prev = load_secret(uuid)
    store_secret(uuid, live.get("refreshToken"), live.get("accessToken"),
                 live.get("expiresAt"), prev.get("tokenHost") if prev else None)
    save_index(idx)
    return uuid

# ---- gathering usage --------------------------------------------------------

def parse_dt(s):
    if not s: return None
    try: return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception: return None

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

def data_ts():
    """Epoch seconds of the last real fetch (cache timestamp), or None."""
    c = load_cache()
    return c.get("ts") if c else None

def collect():
    """Cached wrapper: debounce rapid refreshes, and fall back to last-known values on a rate-limit."""
    now = time.time()
    cache = load_cache()
    if cache and 0 <= now - cache.get("ts", 0) < COOLDOWN:
        return cache["rows"]                                  # rapid re-refresh → reuse, don't hit the API
    rows = _collect_live()
    if any(not r.get("error") for r in rows):                 # got fresh data → this is the new truth
        save_cache(rows, now)
        return rows
    if cache and cache.get("rows"):                           # everything errored (e.g. 429 storm) → last known
        stale = cache["rows"]
        for r in stale: r["stale"] = True
        return stale
    return rows

def _collect_live():
    idx = load_index()
    active_uuid = ingest_live(idx)
    idx = load_index()
    live = read_live()
    rows = []
    for e in idx:
        uuid = e["uuid"]
        if uuid == active_uuid and live:
            token, err = live["accessToken"], None
        else:
            token, err = token_for_parked(uuid)
        row = {"uuid": uuid, "email": e["email"], "label": e["label"],
               "tier": e.get("tier"), "org_type": e.get("org_type"), "is_team": is_team_entry(e),
               "active": uuid == active_uuid, "error": err}
        if token and not err:
            u, uerr = fetch_usage(uuid, token, row["active"])
            if uerr:
                row["error"] = uerr
            if u:
                row["five_hour"] = {"pct": u.get("five_hour", {}).get("utilization"),
                                    "resets_at": u.get("five_hour", {}).get("resets_at")}
                row["seven_day"] = {"pct": u.get("seven_day", {}).get("utilization"),
                                    "resets_at": u.get("seven_day", {}).get("resets_at")}
                # dollar spend is a SEPARATE, opt-in thing (extra-usage credits / usage-based billing).
                # It is disabled on most plans incl. standard team seats, so only surface it when enabled.
                sp = u.get("spend") or {}
                row["spend"] = {"enabled": bool(sp.get("enabled")),
                                "used": (sp.get("used") or {}).get("amount_minor"),
                                "limit": (sp.get("limit") or {}).get("amount_minor"),
                                "percent": sp.get("percent")}
                # scoped weekly limits (e.g. Opus) if present
                scoped = []
                for lim in u.get("limits", []) or []:
                    if lim.get("kind") == "weekly_scoped" and lim.get("scope"):
                        m = (lim["scope"].get("model") or {}).get("display_name")
                        scoped.append({"model": m, "pct": lim.get("percent"),
                                       "resets_at": lim.get("resets_at")})
                row["scoped"] = scoped
        rows.append(row)
    return rows

def recommend(rows):
    def wk(r):   return parse_dt(r.get("seven_day", {}).get("resets_at")) or datetime.max.replace(tzinfo=timezone.utc)
    def fh(r):   return parse_dt(r.get("five_hour", {}).get("resets_at")) or datetime.max.replace(tzinfo=timezone.utc)
    def wpct(r): return (r.get("seven_day") or {}).get("pct")
    def fpct(r): return (r.get("five_hour") or {}).get("pct")
    ok = [r for r in rows if not r.get("error") and wpct(r) is not None]
    viable    = [r for r in ok if (wpct(r) or 0) < 100]                 # weekly headroom left
    usable    = [r for r in viable if (fpct(r) or 0) < FH_NEAR]         # ...and can work right now
    healthy   = [r for r in usable if (wpct(r) or 0) < WK_NEAR]         # ...with real weekly runway
    pool = healthy or usable                                           # prefer real runway; fall back if none
    if pool:
        # use-it-or-lose-it among accounts worth using: drain the one whose weekly resets soonest.
        best = sorted(pool, key=lambda r: (wk(r), fpct(r) or 0))[0]
        left = 100 - int(wpct(best) or 0)
        return best["uuid"], f"{left}% weekly left"
    if viable:
        nxt = sorted(viable, key=fh)[0]
        return None, f"every account with weekly headroom is near its 5-hour cap; {nxt['label']}'s resets first (in {rel(fh(nxt))})"
    if ok:
        nxt = sorted(ok, key=wk)[0]
        return None, f"all accounts are weekly-capped; {nxt['label']}'s weekly resets first (in {rel(wk(nxt))})"
    return None, "no usage data"

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

def week_abs_label(wk):
    """The absolute weekly reset, labeled — shown on the first scoped (e.g. Fable) row, which shares it."""
    dt = parse_dt((wk or {}).get("resets_at"))
    return f"weekly resets {local_short(dt)}" if dt else ""

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

def short_label(row):
    s = row.get("email") or row.get("label") or "?"
    if "@" in s: s = s.split("@")[0]
    return s if len(s) <= 10 else s[:9] + "…"

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
    # stable alphabetical order so the list doesn't reshuffle as reset times change;
    # the ▶ marker (not position) points at the recommended account.
    return sorted(rows, key=lambda r: (r.get("label") or r.get("email") or "").lower())

def render_table(rows):
    if not rows:
        print(f"\n{C['b']}Claude usage{C['x']}\n")
        print(f"{C['y']}No accounts found.{C['x']} Log in with the `claude` CLI "
              f"(`claude` → /login), then run this again.")
        print(f"{C['dim']}It reads the account the CLI is signed into; log into each of your "
              f"accounts once to add them all.{C['x']}\n")
        return
    rows = sort_rows(rows)
    rec_uuid, reason = recommend(rows)
    print(f"\n{C['b']}Claude usage{C['x']}  {C['dim']}· {datetime.now().astimezone().strftime('%-I:%M %p')}{C['x']}\n")
    for r in rows:
        mark = f"{C['g']}▶{C['x']}" if r["uuid"] == rec_uuid else " "
        tag  = plan_name(r)
        head = (f"{mark} {C['b']}{r['label']:<12}{C['x']} {C['dim']}{r['email']}{C['x']}"
                f"  {C['cyan']}{tag}{C['x']}")
        if r.get("active"): head += f"  {C['cyan']}[active]{C['x']}"
        print(head)
        if r.get("error"):
            print(f"    {C['r']}{r['error']}{C['x']}\n"); continue
        fh, wk = r["five_hour"], r["seven_day"]
        fp, wp = fh["pct"] or 0, wk["pct"] or 0
        scoped = [s for s in r.get("scoped", []) if s.get("pct")]
        wk_meta = resets_phrase(wk['resets_at'], 'short') if scoped else resets_phrase(wk['resets_at'], 'week')
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
    if rec_uuid:
        rec = next(r for r in rows if r["uuid"] == rec_uuid)
        print(f"{C['g']}▶ Use {rec['label']} now{C['x']} — {reason}.\n")
    else:
        print(f"{C['y']}⏳ {reason}.{C['x']}\n")
    if any(r.get("stale") for r in rows):
        print(f"{C['y']}⚠ Showing last known values — the usage API rate-limited this refresh.{C['x']}\n")
    n = len([r for r in rows if not r.get("is_team")])
    if n <= 1:
        lead = "No personal accounts tracked yet" if n == 0 else "Only one account tracked so far"
        print(f"{C['dim']}{lead} — log into your other accounts with the `claude` CLI "
              f"(`claude` → /login) to add them.{C['x']}\n")

def render_json(rows):
    rec_uuid, reason = recommend(rows)
    print(json.dumps({"accounts": sort_rows(rows),
                      "recommend": {"uuid": rec_uuid, "reason": reason},
                      "generated_at": datetime.now(timezone.utc).isoformat()}, indent=2))

def render_xbar(rows):
    if not rows:
        print("Claude · —")
        print("---")
        print("No accounts found — run: claude → /login | color=#d9a13b font=Menlo size=12")
        print("Refresh now | refresh=true")
        return
    rows = sort_rows(rows)
    rec_uuid, reason = recommend(rows)
    rec = next((r for r in rows if r["uuid"] == rec_uuid), None)
    # menu-bar title: the account you're on (CLI-active) with its 5h%/weekly%, colored by severity.
    # Falls back to the recommended account when there's no usable active one (e.g. desktop-only).
    def has_usage(r): return not r.get("error") and (r.get("five_hour") or {}).get("pct") is not None
    head = next((r for r in rows if r.get("active") and has_usage(r)), None) or (rec if rec and has_usage(rec) else None)
    if head:
        fp = head["five_hour"]["pct"] or 0; wp = head["seven_day"]["pct"] or 0
        dot = "🟢" if wp < 65 and fp < 65 else ("🟡" if wp < 90 else "🔴")
        stale = " ·" if any(r.get("stale") for r in rows) else ""
        print(f"{dot} {short_label(head)} {int(fp)}%/{int(wp)}%{stale}")
    else:
        have = any(has_usage(r) for r in rows)
        print("🔴 capped" if have else "Claude · ⏳")
    print("---")
    def hexcol(p):
        p = p or 0
        return "#e5534b" if p >= 90 else ("#d9a13b" if p >= 65 else "#3fb950")
    def barline(label, pct, meta=""):
        tail = f"  · {meta}" if meta else ""
        print(f"    {label:<6} {bar(pct)} {int(pct):>3}%{tail} | color={hexcol(pct)} font=Menlo size=12")
    for r in rows:
        star = "▶ " if r["uuid"] == rec_uuid else "  "
        act  = " ·active" if r.get("active") else ""
        print(f"{star}{r['label']}  {plan_name(r)}{act} | font=Menlo size=13")
        if r.get("error"):
            print(f"    {r['error']} | color=#e5534b font=Menlo size=12"); print("---"); continue
        fh, wk = r["five_hour"], r["seven_day"]
        fp, wp = fh["pct"] or 0, wk["pct"] or 0
        scoped = [s for s in r.get("scoped", []) if s.get("pct")]
        barline("5-hour", fp, resets_phrase(fh["resets_at"], "short"))
        barline("weekly", wp, resets_phrase(wk["resets_at"], "short") if scoped else resets_phrase(wk["resets_at"], "week"))
        for i, s in enumerate(scoped):
            barline((s["model"] or "scoped")[:6], s["pct"], week_abs_label(wk) if i == 0 else "")
        sp = r.get("spend") or {}
        if sp.get("enabled") and sp.get("limit"):
            print(f"    extra  {usd(sp['used'])} / {usd(sp['limit'])} used | color=#8b949e font=Menlo size=12")
        print("---")
    if rec:
        print(f"→ Use {rec['label']} · {reason} | color=#3fb950 font=Menlo size=12")
    else:
        print(f"{reason} | color=#d9a13b font=Menlo size=12")
    if any(r.get("stale") for r in rows):
        print("⚠ last known values — rate-limited; updates on the next refresh | color=#d9a13b font=Menlo size=12")
    if len([r for r in rows if not r.get("is_team")]) <= 1:
        print("＋ Log into another account with the claude CLI (claude → /login) to track it | color=#8b949e font=Menlo size=12")
    ts = data_ts()
    upd = datetime.fromtimestamp(ts).strftime("%-I:%M:%S %p") if ts else "—"
    print(f"Updated {upd} · auto-refreshes on a timer | color=#8b949e font=Menlo size=11")
    print("↻ Refresh now | refresh=true")

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

def install_plugin():
    """Symlink the plugin into the host's folder (host must already exist). Returns 'xbar'/'SwiftBar' or None."""
    here = os.path.dirname(os.path.realpath(__file__))     # realpath: works via a PATH symlink too
    plugin = os.path.join(here, "claude-usage.5m.sh")
    if not os.path.exists(plugin):
        print(f"  plugin not found next to the script: {plugin}"); return None
    os.chmod(plugin, 0o755)

    if _app_installed("xbar.app"):
        app = "xbar"
        target = (_read_default("com.xbarapp.app", "pluginsDirectory")
                  or os.path.expanduser("~/Library/Application Support/xbar/plugins"))
    elif _app_installed("SwiftBar.app"):
        app = "SwiftBar"
        target = _read_default("com.ameba.SwiftBar", "PluginDirectory")
        if not target:
            print("  SwiftBar is installed but no plugin folder is set.")
            print("  Open SwiftBar → Preferences, choose a plugin folder, then re-run.")
            return None
    else:
        print("  No menu-bar host found."); return None

    os.makedirs(target, exist_ok=True)
    link = os.path.join(target, "claude-usage.5m.sh")
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
    print("It never changes or signs out your Claude Code session.\n")
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
        gone = [e for e in idx if key and key in (e["uuid"], e["email"])]
        save_index([e for e in idx if e not in gone])
        for e in gone: keychain_delete(STORE_SVC, e["uuid"])   # drop the stored token too
        print(f"forgot {len(gone)} account(s)")
        return
    rows = collect()
    if arg == "--json": render_json(rows)
    elif arg == "--xbar": render_xbar(rows)
    else: render_table(rows)

if __name__ == "__main__":
    main()
