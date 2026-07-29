# cdripper

Rips audio CDs to FLAC with MusicBrainz metadata. Polls optical drives, looks up
the disc, rips with cdparanoia, encodes, tags, files into `Artist/Album/`,
ejects, repeats. Supports several drives in parallel.

## Layout

The whole program is one module: `src/cdripper/__init__.py` (~830 lines).
Tests live in `tests/test_cdripper.py`. There is no other source file.

Keep it that way unless a change genuinely warrants splitting — the single-file
design is deliberate ("one Python script, no complex toolchain"). If you do
split it, `cdripper:main` must stay importable as the console-script entry point.

## Commands

```bash
make dev      # create .venv, install editable with dev extras
make test     # pytest
make lint     # ruff  (must pass -- CI enforces it)
make fmt      # ruff --fix
make check    # verify system binaries, imports, detected drives
make once     # rip a single disc and exit -- the real end-to-end check
make help     # everything else
```

Run `make lint && make test` before every commit. CI runs both on Python
3.9, 3.11 and 3.13.

## Architecture

**Threading.** One thread per drive runs `poll_and_rip`. A separate daemon
thread redraws the `rich` TUI twice a second. Inside a rip, `rip_and_encode`
spawns another short-lived thread that watches the temp WAV's size to derive
read speed and intra-track progress.

**Shared state.** `_drive_states: dict[str, DriveState]` is the only structure
crossing threads. Every read or write goes through `DriveState`'s own lock —
use `.update()`, `.add_log()`, `.get_logs()`, `.snapshot()` and never touch the
fields directly from another thread. `snapshot()` exists so the render thread
gets a consistent, detached view.

**Logging.** `log(msg, logfile, device)` is the single entry point. Behaviour
depends on whether the TUI is active:
- TUI active → append to that drive's ring buffer (`LOG_BUFFER_LINES` deep)
- no TUI → print to stdout
- `logfile` set → always append to disk as well

`_log_lock` serialises this so parallel drives don't interleave mid-line.

**TTY detection.** `_init_display` returns early when `sys.stdout.isatty()` is
false, so the TUI never activates under systemd or in a pipe. `--once` skips it
entirely. Any TUI change must keep the non-TTY path working — that's how the
systemd user service runs.

## Gotchas

- **`import discid` fails without libdiscid.** It's a ctypes binding to a system
  library, so the module is unimportable — including by tests — unless
  `libdiscid` is installed. `make deps` handles it; CI installs `libdiscid0`.
- **Version comes from git tags** via setuptools-scm. A shallow clone with no
  tags reports `dev`. CI checks out with `fetch-depth: 0` for this reason.
- **`cdparanoia` output is discarded** (`stdout`/`stderr` to DEVNULL), so
  progress is inferred from the growing WAV rather than parsed. Don't assume
  cdparanoia messages are available.
- **Failures are recorded, not fatal.** A track that fails all
  `MAX_TRACK_RETRIES` attempts is added to `failed_tracks`, its partial FLAC is
  deleted, and it's noted in `album_info.txt` as `FAILED_TRACKS=`. The rip
  continues. Preserve that — a scratched disc should still yield the tracks
  that are readable.
- **`_shutdown` is a module global** set by SIGINT/SIGTERM. Long loops check it
  between tracks so a Ctrl-C finishes the current track rather than corrupting
  it.

## Testing

Unit tests cover the pure logic and file output: name sanitizing, filename
construction, MusicBrainz response parsing (mocked, including retry paths),
`album_info.txt` and `.m3u` contents, drive detection, and `tag_flac` against a
FLAC encoded on the fly by the real `flac` binary.

Not unit tested, because they need real hardware: `read_disc`, `rip_and_encode`,
`eject_disc`, `poll_and_rip`, and the `rich` rendering. Verify those with
`make once` and an actual disc.

When adding a function, prefer making it pure and passing state in — that's what
made the existing helpers testable. Mock `musicbrainzngs` rather than hitting
the network; the test suite must stay offline and hardware-free.

## Conventions

- Line length 100, ruff-enforced. Config lives in `pyproject.toml`.
- Docstrings on every function, one line where one line does.
- Constants are module-level and SCREAMING_CASE (`MAX_TRACK_RETRIES`,
  `MB_RETRIES`, `LOG_BUFFER_LINES`).
- Internal helpers take a leading underscore.
- User-facing strings go through `log()` or `notify()`, never bare `print()`
  — except in `main()` before the display starts, and on the `sys.exit` error
  paths.

## Workflow

Contributions follow the branch → verify → PR → review → merge flow described
in `CONTRIBUTING.md`. The `/ship` skill in `.claude/skills/` drives it.
Never commit straight to `main`.
