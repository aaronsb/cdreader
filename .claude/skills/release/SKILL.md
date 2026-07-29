---
name: release
description: Cut a tagged release of cdripper. Use when asked to tag a version, cut a release, bump the version, or publish. Covers choosing the version number, the guarded tag process, and release notes.
---

# Releasing cdripper

Versions come from **git tags** via setuptools-scm. There is no version string
in the source to edit — tagging *is* the release. An untagged working copy
reports its version as a `.devN` suffix on the last tag.

## 1. Check where you are

```bash
git describe --tags --abbrev=0     # latest release tag
git log $(git describe --tags --abbrev=0)..main --oneline
```

That second command is the changelog: everything merged since the last tag.

## 2. Pick the number

Semantic versioning, `MAJOR.MINOR.PATCH`. For a tool that writes files to
people's music libraries, read these in terms of user impact:

| Bump | When |
|---|---|
| **PATCH** (0.3.2 → 0.3.3) | bug fixes, TUI polish, docs. Nothing about the output changes. |
| **MINOR** (0.3.2 → 0.4.0) | new features, new CLI flags, new tags written, new metadata files. Existing libraries stay valid. |
| **MAJOR** (0.3.2 → 1.0.0) | on-disk layout changes, renamed/removed CLI flags — anything that makes an existing library or script wrong. |

Be honest about the middle row. Changing where files land or what gets written
is user-visible even when the code change is small. A directory layout change
that reorganises someone's library is not a patch release.

## 3. Tag

```bash
make tag V=0.4.0
```

No leading `v` — the target adds it. It refuses to proceed unless:
- `V` is well-formed and the tag doesn't already exist
- the working tree is clean
- you're on `main`
- local `main` matches `origin/main`
- `make lint` and `make test` both pass

Then it creates an annotated tag and pushes it.

If a guard fires, fix the cause. Don't tag from a feature branch or a dirty
tree — setuptools-scm bakes that state into the version.

## 4. Release notes

```bash
gh release create v0.4.0 --generate-notes
```

`--generate-notes` builds the list from merged PR titles, which is why PR
titles are worth writing carefully. Edit the result to lead with what a *user*
would notice, not what changed internally.

Call out explicitly:
- anything that changes where files are written or how they're named
- anything requiring a new system package
- known-broken things, so nobody rediscovers them

## 5. Verify the tag took

```bash
git fetch --tags
pipx install --force git+https://github.com/aaronsb/cdreader.git
cdripper --version
```

Should print the version you just tagged, with no `.dev` suffix. A `.dev`
suffix means the tag didn't reach the remote.

---

## Undoing a bad tag

Only if nobody has installed it yet:

```bash
git tag -d v0.4.0
git push origin :refs/tags/v0.4.0
```

Once it's public, don't re-point a tag — people have it pinned. Cut the next
patch version instead.
