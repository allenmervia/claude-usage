# claude-usage

See your 5-hour and weekly Claude usage across several accounts at a glance —
without logging in and out or opening incognito windows to check each one.

If you run more than one Claude account (say a couple of Max subscriptions) and
switch between them as you hit limits, this shows all of them side by side — as a
macOS **menu-bar dropdown** that's always a glance away, or a terminal table — and
tells you which account to use next. The menu bar is the main way to use it; the
terminal command is the same data on demand.

```
Claude usage  · 2:14 PM

▶ allen        allen@example.com    Max 20x  [active]
    5-hour  ██░░░░░░░░   22%   3h 22m left
    weekly  ███████░░░   66%   2d 10h left
    Fable   ███░░░░░░░   26%   weekly resets Mon 7am
  allen-1      allen-1@example.com  Max 20x
    5-hour  ░░░░░░░░░░    5%   3h 9m left
    weekly  ████░░░░░░   35%   5d left
    Fable   █░░░░░░░░░    7%   weekly resets Wed 9pm
  allen-2      allen-2@example.com  Max 5x
    5-hour  ░░░░░░░░░░    0%   idle
    weekly  █████████░   94%   3d 20h left
    Fable   ██████░░░░   57%   weekly resets Tue 5pm

▶ Use allen now — 34% weekly left.
```

Accounts are listed alphabetically, and the `▶` marks the account to use — not the
top row. Here it's `allen`: of the accounts with real headroom it resets soonest, so
use-it-or-lose-it says spend that capacity before it expires. `allen-2` is skipped —
at 94% weekly there's almost nothing left to use. (The weekly line shows the
countdown; the exact reset time rides on the Fable row, since the two share it.)

## Requirements

- macOS (reads Claude usage tokens from the macOS Keychain)
- Python 3.8+ (system `python3` is fine — no third-party packages)
- [Claude Code](https://claude.com/claude-code), signed in to at least one account

## Install

```bash
git clone https://github.com/allenmervia/claude-usage.git
cd claude-usage
chmod +x claude-usage.py claude-usage.5m.sh
./claude-usage.py setup
```

`setup` tells you what it will do, asks before doing it, and walks you through the
menu-bar view: if [xbar](https://xbarapp.com) (the app that renders the bar) isn't
installed, it offers to install it with Homebrew, links the plugin, and launches it,
so the bar appears when setup finishes. It also registers the account you're signed
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
claude-usage install    add the menu-bar view (see below)
claude-usage --json     machine-readable JSON
claude-usage --xbar     xbar / SwiftBar menu-bar format
claude-usage capture    register the active account now (same as any run)
claude-usage list       list registered accounts
claude-usage forget X   drop an account by email or uuid (and delete its stored token)
```

## Menu-bar view (xbar / SwiftBar)

The menu bar is rendered by [xbar](https://xbarapp.com) (or
[SwiftBar](https://github.com/swiftbar/SwiftBar)), a small app that runs plugins on a
schedule; `claude-usage` ships a plugin for it. `claude-usage setup` handles all of
this. To wire up the bar on its own later:

```bash
claude-usage install
```

`install` installs xbar with Homebrew if no host is present (it asks first), links
the plugin in, and launches the host. From then on the bar updates every 5 minutes;
click the menu-bar icon → Refresh to update immediately. If Homebrew isn't installed,
it points you to https://xbarapp.com instead.

The title shows the account you're currently on (the one the `claude` CLI is signed
into) and its `5-hour%/weekly%`, colored green/amber/red by how close you are to a
limit. The dropdown lists every account with its bars and resets, and marks the one
to switch to with `▶`.

### Refresh interval

The `.5m.` in the plugin filename (`claude-usage.5m.sh`) is how often the bar
refreshes — every **5 minutes** by default. Rename the file to change it:

| Filename | Interval | |
|---|---|---|
| `claude-usage.1m.sh`  | 1 minute   | account switches show up fastest |
| `claude-usage.5m.sh`  | 5 minutes  | default |
| `claude-usage.10m.sh` | 10 minutes | gentlest |

Each refresh is cheap — one small `/usage` request per account (a few KB, well under a
second) — and these are **status calls that don't count against your Claude usage
limits**. So a shorter interval is fine; it mainly changes how quickly a newly
signed-into account appears on its own. You can always click the menu-bar icon →
Refresh for an instant update. (If you rename the file, re-point the symlink:
`claude-usage install` again, or relink by hand.)

To remove the bar: delete the symlink from xbar's plugin folder (`claude-usage.5m.sh`).

## Which account to use, and when

Two limits interact, and they are not the same kind of thing:

- The **5-hour limit** is a rolling burst cap. It always comes back five hours
  after the window's first message, so it is cheap — never something to hoard.
- The **weekly limit** is the scarce resource. It does not roll over: capacity you
  don't use before the weekly reset is gone. It is use-it-or-lose-it.

From that, the strategy the tool recommends:

1. **Use the account whose weekly limit resets soonest**, as long as it still has
   both 5-hour and weekly headroom. Its unused weekly capacity is about to expire
   anyway, so spend it first and keep the accounts that reset later in reserve.
2. **When that account hits its 5-hour cap, switch to the next** by the same rule.
   Don't bounce between accounts for a single message each — starting an account
   opens a fresh 5-hour window, so drain one before moving on.
3. **Staggered weekly resets are an asset.** If your accounts reset on different
   days, one is almost always fresh. The tool sorts by soonest weekly reset so the
   rotation falls out naturally.

The `▶` marker and the closing line point at the account this rule selects right
now, so you don't have to work it out yourself.

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

## How it works, and why it can't desync your session

Claude Code stores the **currently signed-in** account's OAuth token in the macOS
Keychain item `Claude Code-credentials` (or, for a CLI configured without the
Keychain, in `~/.claude/.credentials.json`). The tool reads that token to identify
the active account, then keeps each account's refresh token in its own Keychain
item, `claude-usage/<uuid>`. With a stored refresh token it mints a short-lived
access token for a **parked** account and reads that account's usage — which is
what lets it show every account without a login swap.

Two properties keep this safe:

- The **active** account is always read live from Claude Code's own Keychain item
  and is never independently refreshed, so the tool cannot invalidate or desync the
  session you are actually logged into.
- The tool is a **live mirror** of the usage endpoint. It stores no usage numbers
  and no reset schedule — only refresh tokens and account identity. So if Anthropic
  issues an out-of-band usage reset, it simply appears as lower usage on the next
  refresh; there is nothing cached to fall out of sync.

Nothing sensitive is written into the repo or into `~/.claude-usage/` — that
directory holds only non-secret identity (uuid, email, label, plan tier). All
tokens live in the Keychain.

## Caveats

- **macOS only.** It shells out to the macOS `security` tool for Keychain access.
- **Undocumented endpoints.** It uses the same private OAuth endpoints Claude Code
  uses for its own `/usage` view. Anthropic may change them; if a parked account
  stops refreshing, sign into it once and re-run.
- **Refresh-token rotation.** If a provider rotates refresh tokens on every use, a
  parked account's stored token can go stale between the last time it was active and
  now. Frequent menu-bar polling keeps the stored copy fresh; if a parked read
  fails, signing into that account once repairs it.

## License

MIT — see [LICENSE](LICENSE).
