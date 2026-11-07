---
name: contributor
description: Develop alongside an integrator session that lands everyone's work in this repo. Work only in your own git worktree and branch, claim the paths you'll change, commit and check small units, submit them to the integrator's queue, sync when main moves under you, and answer the integrator's messages. Use whenever a session (or a SessionStart reminder) says an integrator is running for this repo, when the user says "work with the integrator", "submit to the resolver", "you're a contributor", or launched you with `integrator launch contributor`.
---

# Working as a contributor

Another Claude Code session, the **integrator**, owns `main` and lands everyone's work. You build in your own
worktree, tell it what you'll touch, and hand it finished, checked units. It merges them, resolves collisions,
and tells you when to sync. The `integrator` CLI (this repo's `bin/integrator`, put on PATH by `install.sh`)
keeps the shared ledger.
`integrator status` shows the board.

## 1. Find the integrator and join (once)

1. `integrator status`: the integrator line names it ("'integrator-myapp' live"). That name is its SendMessage
   address. No live integrator? Tell the user. The protocol still works (your submissions wait in the queue), but
   nothing lands until one runs.
2. `ListAgents`: its first line names you ("This session is <name>"). Then
   `integrator join <topic> --task "<one line>" --session "<name>"`. Make the topic short, lowercase and dashed
   (`csv-fix`). Run from the main checkout, it makes your worktree (`.claude/worktrees/<topic>`, branch
   `wt/<topic>`, from main). Run inside an existing worktree, it adopts that one. If you were launched by
   `integrator launch`, you're already registered and in it: join again anyway, to record your session.
3. If you aren't in the worktree yet, move there: **EnterWorktree with `path` = the worktree it printed**. From now
   on, edit only inside it. A hook refuses edits to the main checkout and to other contributors' worktrees.
4. Isolate your environment: run builds, servers and tests from your worktree. Don't share build output, ports or
   simulators with other sessions. Follow the repo's own rules for those (its CLAUDE.md or AGENTS.md: locks, slots,
   ports).

## 2. Say your scope, then claim

Once you know your plan (after reading the code or the logs, before your first edit), put it on the board in one
line: `integrator scope "<the files you'll change; any interface you add or change that others use; shared
resources you need (a port, a slot, a simulator, a device)>"`. The integrator wakes on it and looks for neighbours,
and the output lists everyone else working now, with their scopes and session names. Run it again if your scope
changes much.

`integrator claim <paths>` names the files or directories (`src/parser/`) you'll change. Globs work. Claims are
exclusive unless `--shared` or the repo lists the path as shared (docs, changelogs). Claim narrowly: a file
beats a folder.

- **REFUSED** means someone else is changing it. Don't edit around it. SendMessage the integrator: what you need and
  why. It can split the work, order it, or grant you the path. Do other parts meanwhile.
- Files you edit without claiming are claimed for you (softly) by the hook. If another contributor is in the same
  file, the hook tells you both, and the integrator. Keep such edits small, and submit them soon.

## 3. Work with your neighbours

Your work meets another contributor's when you'd change the same file, one of you defines an interface the other
uses, you build one feature from two sides, or you'd share a loop, a port or a simulator. Then:

- Message that session directly (its name is on the board) and agree the seam: who changes which lines or files,
  the interface's exact shape, what neither may break for the other, and who lands first. Keep it to the seam.
- Tell the integrator what you agreed, in one line. It grants the shared edits and lands in that order.
- Can't agree, or the seam would change the other's design? Ask the integrator to decide. Don't edit around them.
- Otherwise leave each other alone: no status broadcasts, and no reviewing each other's work unasked.

## 4. Work in small, checked units

1. Commit each logical change. Keep units small: a unit that lands in an hour beats a perfect branch in a day,
   because main moves under a long branch.
2. `integrator check` runs the repo's checks in your worktree and records the result for that exact commit.
   The integrator trusts it, so it doesn't rerun them. Fix failures before submitting.
3. `integrator submit --summary "<what and why, one line>"`. Use `--no-check "<why>"` only for docs-only changes.
   It prints the predicted landing ("clean", or the files that conflict with main: the integrator resolves those,
   so stay reachable).
4. Tell the integrator: SendMessage to its name, "submitted #N: <summary>" (the submit output gives you the line).
   Then go straight on to your next unit. Don't wait for the landing.

## 5. Listen and sync

You hear from the integrator in two ways: `<cross-session-message from="...">` messages (reply by copying the
`from` into SendMessage `to`), and notices that a hook shows beside your next edit ("integrator: ...").
`integrator status --me` shows both, plus anything you missed.

- **"landed"**: nothing to do. If it says "rebased", run `integrator sync` before your next submit.
- **"main moved under you" / SYNC NEEDED**: at your next stopping point, commit, then `integrator sync`. It rebases
  your branch onto main, replaying only your unlanded commits. On a conflict it stops: resolve in your worktree,
  keeping both intents (main's side is someone's landed work: read its commit), `git add`, then
  `integrator sync` again. Re-run `integrator check` afterwards: your tree changed.
- **"bounced: ..."**: fix what it says, check, submit again.
- **A question**: answer it promptly and plainly. The integrator may be holding a landing for your answer.
- **Need help or a decision?** SendMessage the integrator. For something it must not miss even if messages are
  held, also run `integrator ask "<text>"` (its watch wakes it).
- `integrator sync` refuses while one of your submissions is queued, because it would rewrite what the submission
  points at. Let it land first, or `sync --withdraw` and resubmit.

## 6. Leave

When everything you made has landed, stop everything you started (servers, simulators, locks, background
checks), then `integrator leave` (it refuses while work is unlanded or uncommitted), then ExitWorktree with action
"keep". The integrator's `prune` removes the worktree.

`leave` prints one line: `🏁 SAFE TO CLOSE: <topic>, all landed (<#ids → shas>)`. End your last message to the user
with it, on its own line. It is how the user knows this session can be closed, so send it only then: never while
anything is unlanded, uncommitted, still running, or waiting on the user. Working on a second topic in the same
session? Its leave prints the flag only when the last topic leaves, because closing the session ends them all.
Leaving unfinished work for later? Tell
the integrator and the user what is left and where, stay registered, and don't send the 🏁 line.

## Never

- Edit the main checkout, merge into `main`, or push. Only the integrator lands.
- Edit another contributor's worktree, or rebase their branch.
- Edit around a refused claim, or submit with a failing check.
- Leave uncommitted work at the end of your session. Commit it on your branch (WIP is fine; it doesn't land until
  you submit) and say so in your last message.
