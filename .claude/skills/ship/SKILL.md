---
name: ship
description: Take a change from issue to merged main, following this project's branch > verify > PR > review > merge discipline. Use when starting work on an issue, when asked to fix or implement something, or when a change is ready to land. Also use when a PR needs review findings addressed or is ready to merge.
---

# Shipping a change to cdreader

The rule this exists to enforce: **nothing lands on `main` without a branch, a
passing CI run, and a review.** No direct commits to `main`, ever — not even
one-line typo fixes.

Work through the stages in order. Don't batch them; each one gates the next.

---

## 1. Know what you're fixing

If an issue number wasn't given, find it:

```bash
gh issue list --state open
gh issue view <N>          # read the full body before writing any code
```

If there's no issue for the work and it's more than a trivial fix, open one
first. The issue is what the PR will close, and it's where the "why" lives.

Screenshots in an issue are evidence — download and actually look at them:

```bash
curl -sL -o /tmp/issue.png "<attachment-url>"
```

Then Read the file. Don't guess at a visual complaint you can inspect.

## 2. Start from a current main

Skipping this is how you get a PR full of conflicts.

```bash
git checkout main
git pull --ff-only origin main
```

`--ff-only` refuses to create a surprise merge commit. If it errors, your local
`main` has diverged — stop and sort that out before branching.

## 3. Branch

```bash
git checkout -b <type>/<issue-number>-<short-slug>
```

| Type | Use for |
|---|---|
| `fix/` | bug fixes |
| `feat/` | new capability |
| `chore/` | tooling, CI, deps |
| `docs/` | documentation only |

Examples: `fix/11-log-readability`, `feat/12-multi-disc`.

## 4. Do the work

Read `CLAUDE.md` first — it documents the threading model, the shared
`DriveState` locking rules, and the non-TTY code path that's easy to break.

Two constraints worth repeating:
- New logic should be **pure and testable** where possible. State goes in as an
  argument, not read from a module global.
- The **non-TTY path must keep working**. The systemd service runs without a
  terminal, so anything touching the TUI needs to be a no-op when
  `sys.stdout.isatty()` is false.

## 5. Verify — the stage people skip

```bash
make lint     # must pass; CI enforces it
make test     # must pass
```

Then ask what those *don't* cover. The suite is offline and hardware-free by
design, so it cannot tell you whether a real rip still works. If the change
touches ripping, the TUI, ejecting, or drive polling:

```bash
make once     # rip one real disc, end to end
```

If you can't test against hardware, **say so explicitly in the PR** rather than
implying it was verified. An honest "not tested against a real drive" is worth
more than silence.

Add tests for whatever you changed. A bug fix without a regression test invites
the bug back.

## 6. Commit

```bash
git status --short      # look before you add
git diff                # read what you're about to commit
git add <explicit paths>
```

Prefer explicit paths over `git add -A`. It stops stray scratch files and
unrelated work from riding along.

Message format — subject line in imperative mood, under ~72 chars:

```
fix: shorten log timestamps and drop redundant device prefix

Per-drive panels showed a full ISO timestamp and an [sr0] tag on every
line, inside a panel already titled sr0. That consumed ~27 of ~57 usable
columns, forcing wraps mid-title.

Timestamps are now HH:MM:SS, the device prefix is suppressed in the TUI
(kept in the file log where it disambiguates), and wrapped lines are
indented so continuations read as continuations.

Closes #11

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
```

Explain **why**, not what — the diff already says what. `Closes #11` makes
GitHub close the issue automatically when the PR merges.

## 7. Push and open the PR

```bash
git push -u origin <branch>      # -u only needed the first time
gh pr create --base main --title "..." --body "..."
```

The body should carry:
- **Why** — the problem, linked to the issue
- **What** — the approach, and any design choice a reviewer might question
- **Verification** — what you ran, and honestly what you couldn't run
- **Screenshots** — for anything visual, before and after

To update a PR, just commit and `git push` again. The PR follows the branch;
there's no re-submit step.

## 8. Wait for CI

```bash
gh pr checks <N>
```

Green before review. Asking someone to review a red PR wastes their time.
If CI fails, read the failing job's log — don't guess:

```bash
gh run view <run-id> --log-failed
```

## 9. Review

Run the review, and treat it as real:

```
/review <PR-number>
```

Then triage each finding: fix it, or reply saying why not. Silently ignoring
findings defeats the point. Verify claims before acting on them — a reviewer
(human or agent) can be wrong, and confirming a bug reproduces takes a minute.

Push fixes as additional commits on the same branch.

## 10. Second pair of eyes (optional)

Aaron isn't always around, so this is a judgement call rather than a hard gate.
Request his review when the change is:
- a design decision that's hard to reverse (on-disk layout, tag schema, CLI)
- a change to how discs are read or files are written — data loss territory
- anything you're genuinely unsure about

```bash
gh pr edit <N> --add-reviewer aaronsb
```

Routine fixes with green CI and a clean review don't need to wait for him.

## 11. Merge

```bash
gh pr merge <N> --merge --delete-branch
```

Use `--merge`, not `--squash`. This repo's history is merge commits
(`Merge pull request #10 from ...`) with the individual commits preserved —
match it rather than mixing styles. Keep the commits on a branch meaningful,
since they survive the merge.

Then get local back in sync:

```bash
git checkout main
git pull --ff-only origin main
```

Confirm the issue actually closed. If the `Closes #N` line was missing or
malformed, close it by hand with a comment pointing at the PR.

---

## When things go sideways

**Committed to `main` by accident, not yet pushed**

```bash
git branch fix/my-work        # save the work on a new branch
git reset --hard origin/main  # rewind main -- destroys uncommitted changes
git checkout fix/my-work
```

**`main` moved while you were working**

```bash
git fetch origin
git rebase origin/main        # replay your commits on top
```

Only rebase a branch nobody else has pulled. If someone else is on it, merge
instead: `git merge origin/main`.

**Wrong files committed**

```bash
git reset --soft HEAD~1       # undo the commit, keep the changes staged
```

**Need to see what a PR actually changed**

```bash
gh pr diff <N>
```
