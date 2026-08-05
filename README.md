# claude-usage

See your 5-hour and weekly Claude usage across several accounts at a glance — and
your OpenAI Codex usage alongside — without logging in and out or opening incognito
windows to check each one.

If you run more than one Claude account (say a couple of Max subscriptions) and
switch between them as you hit limits, this shows all of them side by side — as a
macOS **menu-bar dropdown** that's always a glance away, or a terminal table. If you
also use the Codex CLI, its usage shows in its own group and switches the same way.
The menu bar is the main way to use it; the terminal command is the same data on
demand.

```
Usage  · 2:14 PM

── Claude ──────────────────────────────────────────────────
▶ allen        allen@example.com    Max 20x
    5-hour  ██░░░░░░░░   22%   3h 22m left
    weekly  ███████░░░   66%   5d left
    Fable   ███░░░░░░░   26%   weekly resets Sat 7am
  allen-1      allen-1@example.com  Max 20x
    5-hour  ░░░░░░░░░░    5%   3h 9m left
    weekly  ████░░░░░░   35%   2d 10h left
    Fable   █░░░░░░░░░    7%   weekly resets Wed 9pm
  allen-2      allen-2@example.com  Max 5x
    5-hour  ░░░░░░░░░░    0%   idle
    weekly  █████████░   94%   3d 20h left
    Fable   ██████░░░░   57%   weekly resets Tue 5pm

── Codex ───────────────────────────────────────────────────
▶ allen        allen@example.com    Pro Lite
    weekly  █████░░░░░   54%   Fri 8pm · 3d left
```

Accounts are listed alphabetically. (The weekly line shows the countdown; the exact
reset time rides on the Fable row, since the two share it.)

### The `~` on a weekly reset

A weekly window opens on first use, so between a reset and your next request the
endpoint reports no reset time — the row would otherwise go blank on exactly the
account you just got a fresh week on.

The reset time it reports once the window opens isn't seven days out; it's a fixed
weekly boundary the account keeps across resets. So the last one seen is remembered
and used to fill the gap:

```
    weekly  ░░░░░░░░░░    0%   ~6d 18h left
    Fable   ░░░░░░░░░░    0%   weekly resets ~Wed 9pm
```

The `~` means projected, not reported. It disappears the moment the endpoint has a
real answer, which then replaces the remembered boundary. A boundary older than eight
weeks isn't projected at all — by then the account has been idle long enough that it
may have moved unobserved, and `idle` is the honest answer.

The `── Claude ──` / `── Codex ──` headers appear only when you're tracking more than
one provider; with Claude alone the list is ungrouped, since there is nothing to tell
it apart from.

## Requirements

- macOS (reads Claude usage tokens from the macOS Keychain)
- Python 3.8+ (system `python3` is fine — no third-party packages)
- [Claude Code](https://claude.com/claude-code), signed in to at least one account
- Optional: the [Codex](https://openai.com/codex) CLI, signed in — its usage then
  appears alongside (nothing to set up; see [Codex](#codex))
- Optional: the Xcode Command Line Tools (`xcode-select --install`) — only the
  menu-bar app needs them; the terminal commands run on stock `python3`

## Install

```bash
git clone https://github.com/allenmervia/claude-usage.git
cd claude-usage
chmod +x claude-usage.py
./claude-usage.py setup
```

`setup` tells you what it will do, asks before doing it, and offers to build and
launch the menu-bar app (see [Menu bar](#menu-bar)). It also registers the account you're signed
into and can put `claude-usage` on your `PATH`. Everything it does is also available
as an individual command (see [Commands](#commands)) if you'd rather do it by hand.

The first run shows only the account you're currently signed into — that's expected.
See [Registering your accounts](#registering-your-accounts).

## Registering your accounts

There is no config file to edit. The tool learns an account the first time it sees
that account's token in the `claude` CLI's Keychain slot, then remembers it and
refreshes it from then on. So registering all your accounts is a one-time pass:

```
# for each account, once, in a terminal:
claude          # then /login  (pick the account)
claude-usage    # captures it
```

After that, every account shows on every run and in the menu bar — you never need
to repeat this. (There's no API that lists "every account you own," so the tool
can only learn an account after its token has passed through the CLI once.)

Two things to know:

- **Switch accounts with `/login`, not `/logout` + `/login`.** `/login` just swaps
  which account the CLI holds; the account you leave keeps its session, so the tool
  can still refresh it. **`/logout` revokes that account's token server-side** — the
  tool then can't refresh it and will show "sign into it again" until you re-login.
- **The desktop app can't register accounts.** The Claude **desktop app** keeps its
  tokens inside its own sandbox (a VM) and encrypted cookies, out of reach of any
  host-side tool. Switching or using accounts *in the desktop app* won't register
  them. To track a desktop-app account, log into it once with the `claude` CLI in a
  terminal (as above); after that it's tracked regardless of how you use it.

## Commands

```
claude-usage setup      guided first-time setup (register account + optional menu bar & PATH)
claude-usage            table of all known accounts (default)
claude-usage app        build + launch the menu-bar app (see below)
claude-usage doctor     check the setup and report what needs fixing
claude-usage insights   trailing-week tokens + API-equivalent cost by model (see below)
claude-usage --json     machine-readable JSON
claude-usage capture    register the active account now (same as any run)
claude-usage list       list registered accounts, Claude and Codex
claude-usage switch X   switch the CLI to that account (see below)
claude-usage switch --undo   put the previous account back
claude-usage forget X   drop an account by email, label or id (and delete its stored credential)
```

## Switching accounts

Click an account in the menu and the CLI switches to it — no browser, no
`/logout`+`/login`. Rows you can switch to are marked `⇄`; hold **⌥** and the row
spells out what the click will do. The bar then redraws with the ▶ moved to the new
account. If a switch can't go through, the menu says why at the bottom.

It works by minting a fresh access token from that account's stored refresh token and
writing it into Claude Code's Keychain item, so your next `claude` run *is* that
account. The account you leave keeps its session — it just becomes a parked account
you can switch back to.

Claude Code keeps two separate things: the credential, which decides whose quota a
request spends, and a cached copy of the account profile in `~/.claude.json`, which is
what it shows you and reads the plan from. A switch writes both together, so the account
`claude auth status` names is the account your usage lands on.

```bash
claude-usage switch allen-1@example.com   # or its label / uuid
claude-usage switch --undo                # restore the previous account
claude-usage switch codex:you@example.com # the Codex account of that name
```

The same email often names an account on both providers. A bare name switches the
Claude one and tells you the Codex account exists; `codex:` in front picks the Codex
one. Account ids are unique across providers, so those never need the prefix — which
is why clicking a row in the menu is always unambiguous.

Two things to know:

- **An account must be logged into once before you can switch to it**, so the tool has
  its full credentials stored — with the `claude` CLI for a Claude account, `codex
  login` for a Codex one. Until then the menu says so rather than writing a partial
  credential.
- **This switches the CLI account**, not the desktop app — the desktop app keeps its
  credentials in its own sandbox, out of reach (see [Registering your accounts](#registering-your-accounts)).

## Menu bar

```bash
claude-usage app
```

compiles a small macOS app from `native/` and installs it to `/Applications` — or
`~/Applications` when that isn't writable. It needs the Xcode Command Line Tools
(`xcode-select --install`) and macOS 13+; it's built locally, so there is nothing to
sign or notarize, and rebuilding after a `git pull` is the same command again.

The title icon is one ring gauge per active provider (Claude left, Codex right): the
ring is that account's **weekly** window, filled clockwise and tinted
green/amber/red; the **pie in its centre** is the **5-hour** window, same colors.
Slow budget and burst budget each read at a glance, with no numbers to parse.

The dropdown has two tabs:

- **Usage** — the account cards: meter bars whose countdowns tick while the window
  is open, plan tags, and click-anywhere-to-switch on parked accounts (the ⇄ appears
  on hover where the ▶ will land).
- **Insights** — the weekly burn (each account's current week as a band on one
  shared timeline, its recorded burn inside, one line marking now — hover for values,
  hover a name for its email and provider) and the model mix (see
  [Insights](#insights) — hover a row for its ledger).

Refresh cadence, launch at login, and quit live behind the gear. Refreshes are cheap —
one small `/usage` request per account, and these are **status calls that don't count
against your usage limits** — so short cadences are fine. The app is a shell over
this script: it runs `claude-usage --json` on its timer and `claude-usage switch`
when you click, so every number and phrase comes from the same place as the terminal
table — which is also the fallback if you'd rather skip the app entirely.

### Upgrading from the xbar plugin

Earlier versions rendered the bar through an xbar/SwiftBar plugin. If you ran one,
retire it by deleting the `claude-usage.*.sh` symlink from
`~/Library/Application Support/xbar/plugins/` — or from SwiftBar's plugin folder,
shown in its preferences — and `brew uninstall xbar` if nothing else uses it.
`claude-usage doctor` warns while an old link is still present. Nothing else to clean — the state in `~/.claude-usage/` and the
Keychain entries belong to the tool and carry over.

### When something looks wrong

```bash
claude-usage doctor
```

It checks each thing that has to line up — Keychain access, the signed-in account,
every registered account's usage read and whether it's switchable, whether the
menu-bar app is built and running, and the `PATH` symlink — and for anything that
fails, names the command that fixes it. This is the fastest way to tell a revoked
token (from `/logout`) apart from an app that simply isn't running.

## Insights

The Insights tab prices where the week's tokens went, from data already on disk:
every Claude Code transcript records each message's model, effort, and token counts,
and Codex rollouts record the same per turn. `claude-usage insights` scans the
trailing 7 days of both (a couple of seconds, cached for 30 minutes), dedupes
replayed messages, and totals tokens per model version with effort split out:

```
past 7 days · API list-price equivalent (not billing) · today $919
model           msgs     input    output   cache wr   cache rd      cost
Fable 5        4,697    13,121 4,469,532 39,722,852 1,681,900,198     $2402
Opus 5        15,129   230,268 5,730,719 90,425,713 2,951,623,044     $2185
GPT-5.6 sol    2,916 17,884,445 1,838,994          0 213,207,040       $67
```

Costs are **API list-price equivalents, not a bill** — you pay subscriptions; the
figure is what the same usage would have cost through the API, which makes models and
days comparable in one unit. The list rates are pinned in one dated table in the
script (`PRICING`); when prices move, that is the one place to update.

## Which account to use, and when

Two limits interact, and they are not the same kind of thing:

- The **5-hour limit** is a rolling burst cap. It always comes back five hours
  after the window's first message, so it is cheap — never something to hoard.
- The **weekly limit** is the scarce resource. It does not roll over: capacity you
  don't use before the weekly reset is gone. It is use-it-or-lose-it.

From that, a strategy:

1. **Use the account whose weekly limit resets soonest**, as long as it still has
   both 5-hour and weekly headroom. Its unused weekly capacity is about to expire
   anyway, so spend it first and keep the accounts that reset later in reserve.
2. **When that account hits its 5-hour cap, switch to the next** by the same rule.
   Don't bounce between accounts for a single message each — starting an account
   opens a fresh 5-hour window, so drain one before moving on.
3. **Staggered weekly resets are an asset.** If your accounts reset on different
   days, one is almost always fresh.

## Account types

Each account is tagged with its plan (Pro, Max 5x, Max 20x, Team), read from the
profile — the tool never assumes a plan. Team and enterprise seats use the same
5-hour + weekly percentage limits as personal plans, so they render the same way.
(A dollar line appears only if extra-usage credits are actually enabled on the
account — off by default, including on standard team seats.)

**One account, personal and team: personal wins.** If the same login has both a
personal plan and a team seat (same account, different orgs), the tool tracks the
**personal** one. Signing into the team context — say, to build something in the
org — is ignored: the personal account keeps showing (as a parked account) with its
usage intact, and no separate team section appears. A team account shows only when
it's the *only* context that login has.

## Codex

If you use [Codex](https://openai.com/codex) too, its usage appears in its own
section, under the Claude accounts, and its accounts switch the same way Claude's do —
click the row in the menu, or `claude-usage switch codex:<email>`. The account Codex is
currently on carries the ▶ marker, the same as on the Claude side.

Codex has no usage API, so the numbers come from Codex's own session logs. Identity
(email, plan, account id) is read from `~/.codex/auth.json`; the utilization figure
is the most recent rate limit Codex recorded in a session under `~/.codex/sessions/`.
Codex only reports the windows that apply to your plan — often just a weekly one — so
the row shows exactly those, no phantom "5-hour" line.

Two consequences follow from reading logs instead of an API:

- **The reading is only as fresh as your last Codex run.** It updates whenever you
  use `codex` (each turn logs a fresh figure); when a window has rolled over since,
  the row shows the reset rather than a stale percentage.
- **A session log doesn't name its account**, so the reading is attributed to
  whichever account is signed in now. Readings written before an account took over
  `~/.codex/auth.json` are left out, which is what keeps that attribution true once
  accounts rotate.

**Multiple Codex accounts.** Codex itself signs in one account at a time. Every
account is remembered as it passes through `~/.codex/auth.json`, keyed by account id —
the same way Claude accounts accrue — so signing into each one once with `codex login`
makes them all switchable afterwards. Accounts kept in separate `CODEX_HOME`
directories aren't auto-discovered; point the tool at one with
`CODEX_HOME=/path claude-usage`.

### How Codex switching differs from Claude's

Codex keeps its whole credential in one file, so a switch is a file swap: each
account's `~/.codex/auth.json` is stashed in the Keychain as it's seen and written
back on the way in. That stash is a copy of a live credential, so every run makes one
for the signed-in Codex account — the Claude side has always kept refresh tokens the
same way, and it is what lets you return to an account later. `claude-usage forget
codex:<email>` deletes an account's copy along with its entry.

Two differences from a Claude switch follow.

- **No refresh of our own.** Claude accounts get a fresh access token minted before
  the switch; Codex tokens are handed over as captured and the `codex` CLI refreshes
  them on its next run. If OpenAI rotated the refresh token after the snapshot was
  taken, that run asks you to sign in again — `codex login` re-captures it.
- **Don't switch while `codex` is running.** A running session holds its tokens in
  memory and rewrites `auth.json` when they refresh, which can overwrite the switch or
  be overwritten by it. Quit `codex` first. (The same is true of `claude` and a Claude
  switch.)

`claude-usage switch --undo` reverses the last switch, whichever provider it was. Each
provider keeps its own backup, so once one has been undone, a further `--undo` reaches
the other's — an earlier Claude switch stays reversible after a Codex one is put back.

## How it works, and why it can't desync your session

Claude Code stores the **currently signed-in** account's OAuth token in the macOS
Keychain item `Claude Code-credentials` (or, for a CLI configured without the
Keychain, in `~/.claude/.credentials.json`). The tool reads that token to identify
the active account, then keeps each account's refresh token in its own Keychain
item, `claude-usage/<uuid>`. With a stored refresh token it mints a short-lived
access token for a **parked** account and reads that account's usage — which is
what lets it show every account without a login swap.

Two properties keep the reporting side safe:

- **Reading never touches your session.** For the active account the tool only reads
  Claude Code's credential and never refreshes that token itself, so simply showing
  your usage cannot invalidate or desync the session you're logged into. Only *parked*
  accounts get refreshed, using their own stored tokens. Identifying which account is
  live falls back to matching the stored credential when the profile lookup can't be
  reached, so an expired token or a dropped network can't cause the live account to be
  mistaken for a parked one and refreshed.
- The tool is a **live mirror** of the usage endpoint: every number it *shows as
  current* comes from the endpoint on that refresh, so if Anthropic issues an
  out-of-band usage reset, it simply appears as lower usage on the next one. Two
  things are remembered alongside, and both only ever describe the past: each
  account's weekly boundary (see [The `~` on a weekly reset](#the--on-a-weekly-reset)),
  shown only when the endpoint reports none and always marked as the guess it is; and
  a history of the percentages already shown (`~/.claude-usage/history.jsonl`, trimmed
  back to the trailing two weeks as it grows), which feeds the Insights tab's weekly
  burn — never a current reading.

**Switching is the one exception, by design.** [Switching accounts](#switching-accounts)
deliberately *writes* Claude Code's credential — that's the whole mechanism. It replaces
only the `claudeAiOauth` value (anything else is preserved), writes back to whichever
store Claude Code actually reads (Keychain, or `~/.claude/.credentials.json` for a CLI
configured without it), and saves the previous credential first, so `claude-usage switch
--undo` puts it back. If the write fails it says so rather than reporting a switch that
didn't happen.

It also replaces the `oauthAccount` key in `~/.claude.json` with the switched-to
account's profile, built from the same `/profile` endpoint the rest of the tool reads.
The rest of that file is preserved, the previous profile is saved for `--undo`, and if
this write is the one that fails, the switch says so — a working switch that reads as
the wrong account is worse discovered silently than reported.

Nothing sensitive is written into the repo or into `~/.claude-usage/` — that
directory holds only non-secret state (account identity, cached and historical usage
percentages, the transcript-scan aggregates, the last failure's message, which
provider was switched last). **Every
credential, including the pre-switch backup, lives in the Keychain.**

## Caveats

- **macOS only.** It shells out to the macOS `security` tool for Keychain access.
- **Undocumented endpoints.** It uses the same private OAuth endpoints Claude Code
  uses for its own `/usage` view. Anthropic may change them; if a parked account
  stops refreshing, sign into it once and re-run.
- **Refresh-token rotation.** If a provider rotates refresh tokens on every use, a
  parked account's stored token can go stale between the last time it was active and
  now. Frequent menu-bar polling keeps the stored copy fresh; if a parked read
  fails, signing into that account once repairs it. `claude-usage doctor` names which
  accounts are affected.
- **Codex usage can lag.** It's read from Codex's session logs, not a live API, so it
  only updates when you run `codex` (found by scanning your recent sessions). The last
  reading is kept and shown until a newer one appears, so a stretch without Codex use
  leaves the figure unchanged rather than blank. See [Codex](#codex).

## License

MIT — see [LICENSE](LICENSE).
