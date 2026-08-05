---
name: desktop-accounts
description: Switch, add, inspect, or repair which account the Claude desktop app is signed into, without logging out. Use when the user asks to switch the desktop app account, add another desktop account, fix a mislabeled account stash, check which desktop account is active, or recover from a failed desktop account switch. Also use when they hit a usage limit in the desktop app and want to move to another account, and when someone setting up or onboarding to the claude-usage menu bar app asks how to get desktop account switching working, what the setup costs, or why their desktop account is not listed. NOT for the `claude` CLI's account (that is `claude-usage switch`), and not for usage percentages.
---

# Desktop app account switching

Changes which account the Claude **desktop app** is signed into by swapping its session files,
with no logout and no login. Backed by `tools/desktop-switch.py` in this repo.

Run it as `./tools/desktop-switch.py <command>` from the repo root. If the working directory is
elsewhere, resolve the repo root first (`git rev-parse --show-toplevel`) rather than guessing a
path. macOS only — it drives Claude.app and the macOS Keychain.

## The one rule that matters

**Never log out in the app.** A logout revokes that account's session server-side, which kills
its stash permanently — restoring the files afterwards only ever reaches a login screen. To
reach a login screen safely, `signout-local` clears the session files while leaving the session
valid. If a switch lands on a login screen, the first question is whether that account was
logged out since it was captured; that is the usual cause, not a broken stash.

## Where you can run what

Two commands quit and reopen the desktop app: `switch` and `add`. Everything else only reads or
renames local state and is safe to run anywhere.

Check `CLAUDE_CODE_ENTRYPOINT` — it is `claude-desktop` when this session is hosted inside the
desktop app.

- **Hosted in the desktop app**: run the read/fix commands directly. Do NOT run `switch` or
  `add` — quitting the app would kill this session mid-operation, and the tool refuses anyway.
  Give the user the exact command to paste into a terminal.
- **A terminal session**: run everything directly, including `switch`. Warn first that the app
  will quit and any Claude Code sessions inside it will be killed, so they can park them.

`add` is interactive — it prompts for names and waits while the user logs in — so it cannot be
driven from a tool call at all. Always hand it to the user to run.

## Onboarding someone new

Someone arriving from the menu bar app usually wants desktop switching to work and does not yet
know what it costs. Set that expectation before they start, because the cost is front-loaded and
paid once.

Say the shape of it plainly:

- Each account must be **captured once**, and capturing needs that account signed into the app.
  So the first setup spends one login per account beyond the one already signed in. After that,
  switching never logs in again.
- Capturing and switching **quit and reopen the app**, killing any Claude Code sessions running
  inside it. Sessions resume from the app's list, but a turn in flight is lost. Park them first.
- Their **device stays trusted** — device registration is deliberately left in place — so
  switching should not trigger verification emails.

Then the whole setup is `./tools/desktop-switch.py add`, run in a terminal, once per account
they want to add. Run `status` first: if it says nothing is captured, that is the starting point;
if stashes already exist, they may only need to add the missing account.

Two things worth saying up front rather than after they hit them. Their desktop account will not
appear in the menu bar's usage list unless that same account has also been logged into with the
`claude` CLI at least once, because usage needs a refresh token this tool can read — switching
and usage are separate capabilities with separate one-time costs. And they must never use the
app's log-out button, which silently destroys a stash.

## Commands

| Task | Command |
|---|---|
| What is installed, plus health | `status` |
| Stashes with age and app version | `list` |
| Switch to a captured account | `switch <label>` |
| Preview a switch, change nothing | `switch <label> -n` |
| Add another account (interactive) | `add` |
| Undo the last switch | `undo` |
| Finish/roll back an interrupted run | `repair` |
| Fix a wrong label | `rename <old> <new>` |
| Drop a stash | `forget <label>` |

## Reading `status`

- **`account:` with "from this tool's record; the files have drifted"** — normal. These stores
  are rewritten constantly while the app runs, so an exact match only holds right after a
  switch. Drift is not evidence that anything is wrong; do not offer to "fix" it.
- **An open journal warning** — an operation was interrupted. Run `repair` before anything
  else; `switch` refuses until it is clear.
- **`account: unknown — not captured yet`** — nothing captured on this machine yet; the user
  needs `add`.

## Handling common requests

**"Switch my desktop app to X."** Check `list` for the label. If this session is hosted in the
desktop app, hand over the command. Otherwise warn about the restart, run it, and tell the user
to check the app — if it shows a login screen, `undo` restores what was there.

**"I mislabeled an account."** Use `rename`. The label is bookkeeping; the files inside decide
which account the app opens as. Never suggest re-capturing to fix a name — that spends a login
for nothing.

**"Add another account."** Hand them `./tools/desktop-switch.py add` to run in a terminal. It
asks for both names up front, clears the session locally rather than logging out, waits for
them to log in, then proves the switch works.

**"The switch landed on a login screen."** Ask whether that account was logged out in the app
since capture. If yes, its session is revoked and the stash cannot be recovered — re-add it
with `add`. If no, the stash may be missing a file; the candidates are `buddy-tokens.json` and
`config.json`, noted in the tool's IDENTITY comment. Either way `undo` restores the previous
account first.

**"Which account am I on?"** Report `status`, and be honest that it is the tool's record of what
it last installed, not a reading from the app — a login done by hand is not visible to it.

## What this does not do

Desktop and CLI accounts are independent. `claude-usage switch` moves the **CLI**; this moves
the **desktop app**. Switching one says nothing about the other.

Usage percentages come from the CLI's stored refresh tokens, so an account only shows usage if
it has been logged into with the `claude` CLI at least once. A desktop-only account is
switchable but has no usage numbers.

## Unproven

Whether a stash still works after days rather than minutes is not yet known — it depends on how
long the session cookies stay valid. If an old stash stops working, re-adding it with `add` is
the fix. Do not promise durability.
