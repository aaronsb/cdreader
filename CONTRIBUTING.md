# Contributing to cdripper

## Setup

```bash
git clone git@github.com:aaronsb/cdreader.git
cd cdreader
make deps     # system packages -- needs sudo, asked once
make dev      # virtualenv + editable install + dev tools
make check    # confirm it all landed
```

`make check` prints what's present and what's missing. Everything should say
`ok` except the drive list, which is empty on a machine with no optical drive.

One thing to know up front: `cdripper` imports `discid`, which is a binding to
the **libdiscid** system library. Without it the module can't be imported at
all — not even by the tests. That's what `make deps` installs.

## The workflow

Every change goes through the same path. No direct commits to `main`.

```
pull main  ->  branch  ->  work  ->  verify  ->  PR  ->  CI  ->  review  ->  merge
```

### 1. Start from a current main

```bash
git checkout main
git pull --ff-only origin main
```

`--ff-only` means "fast-forward or fail". If it fails, your local `main` has
picked up commits that aren't on the remote — sort that out before branching,
or you'll end up with a tangled PR.

### 2. Branch

```bash
git checkout -b fix/11-log-readability
```

Name it `<type>/<issue-number>-<slug>`, where type is `fix`, `feat`, `chore`,
or `docs`.

### 3. Work

Read `CLAUDE.md` — it covers the threading model, the locking rules around
`DriveState`, and the non-TTY path. The last one bites people: the systemd
service runs with no terminal, so the TUI must stay a no-op there.

### 4. Verify

```bash
make lint
make test
```

Both must pass; CI enforces them.

Then think about what the tests *can't* see. They're deliberately offline and
hardware-free, so they say nothing about whether a real rip still works. If you
touched ripping, ejecting, drive polling, or the TUI, put a disc in a drive:

```bash
make once
```

If you don't have hardware to test on, say so in the PR. Nobody minds a change
that wasn't hardware-tested; people mind finding out later that it wasn't.

### 5. Commit

```bash
git status --short
git diff
git add <paths>
git commit
```

Read your own diff before committing — it catches debug prints and stray files.
Prefer naming paths explicitly over `git add -A`.

Write the subject line as an instruction ("fix: shorten log timestamps"), and
use the body to explain **why**. The diff already shows what changed. Put
`Closes #11` in the body and GitHub closes the issue when the PR merges.

### 6. Push and open a PR

```bash
git push -u origin fix/11-log-readability
gh pr create --base main
```

`-u` links the branch to the remote, so later pushes are just `git push`.

Your PR body should say why the change exists, what approach you took, and how
you verified it. Screenshots for anything visual — before and after.

Pushing more commits to the same branch updates the PR automatically. There's
no resubmit step.

### 7. CI

```bash
gh pr checks <N>
```

Wait for green before asking for review. If something fails:

```bash
gh run view <run-id> --log-failed
```

Read the actual failure rather than guessing at it.

### 8. Review

Every PR gets reviewed. For each finding, either fix it or say why you
disagree — quietly ignoring review comments defeats the point of having them.
Push fixes as further commits on the branch.

Add Aaron (`gh pr edit <N> --add-reviewer aaronsb`) when the change is hard to
reverse — on-disk layout, tag schema, CLI surface — or when you're unsure.
Routine fixes with green CI don't need to wait on him.

### 9. Merge

```bash
gh pr merge <N> --merge --delete-branch
git checkout main && git pull --ff-only origin main
```

This repo uses merge commits, not squash — match the existing history.

## Tests

`tests/test_cdripper.py` covers the pure logic and file output: name
sanitizing, filename construction, MusicBrainz parsing (mocked, including the
retry paths), `album_info.txt` and `.m3u` contents, drive detection, `log()`
routing, and `tag_flac` against a FLAC encoded on the fly.

It does **not** cover `read_disc`, `rip_and_encode`, `eject_disc`,
`poll_and_rip`, or the `rich` rendering — those need real hardware.

The suite must stay offline and hardware-free, so mock `musicbrainzngs` rather
than calling it. When you add a function, passing state in as an argument
instead of reading a module global is what makes it testable.

Fixing a bug? Add a test that fails without your fix.

## Releases

Versions come from git tags via setuptools-scm — there's no version string in
the source.

```bash
make tag V=0.4.0
```

The target refuses to run unless the tree is clean, you're on `main`, `main`
matches the remote, and lint and tests pass. See `.claude/skills/release/` for
how to choose the number; the short version is that changing where files land
or how they're named is user-visible, so it isn't a patch release.

## Known rough edges

- `sanitize_filename` only strips trailing spaces when a name is long enough to
  be truncated, so a short album name ending in a space keeps it. Fine on ext4,
  awkward on SMB/NTFS shares.
- Track numbers are parsed with a bare `int()`, which raises `ValueError` on
  vinyl-style numbering (`A1`, `B2`). The disc-ID match normally lands on a CD
  medium with numeric numbers, but `lookup_metadata`'s fallback takes the
  *first* medium of any format — so on a mixed CD+vinyl release this is
  reachable, and it kills that drive's thread rather than degrading.
