# cdripper

Rip audio CDs to FLAC with MusicBrainz metadata. One Python script, no complex toolchain.

Polls your CD drive, looks up album/artist/track info from MusicBrainz, rips with cdparanoia (EAC-quality error correction), encodes to FLAC, tags, and organizes into `Artist/Album/` directories. Ejects when done, waits for the next disc. Supports multiple drives in parallel.

## Install

```bash
curl -fsSL https://raw.githubusercontent.com/aaronsb/cdreader/main/setup.sh | bash
```

The installer:
- Installs system packages via your package manager (sudo, asked once, then dropped)
- Installs cdripper in an isolated virtualenv via pipx (no sudo)
- Sets up a systemd user service (disabled by default)
- Re-running the installer upgrades an existing install

Works on Arch, Ubuntu/Kubuntu, Debian, Fedora, and derivatives.

### Manual install

```bash
sudo pacman -S cdparanoia flac libdiscid eject pipx   # arch
sudo apt install cdparanoia flac libdiscid0 eject pipx # ubuntu/debian
pipx install git+https://github.com/aaronsb/cdreader.git
```

## Usage

```bash
cdripper                            # poll /dev/cdrom, rip to ~/Music
cdripper -d /dev/sr0                # specific device
cdripper -d /dev/sr0 /dev/sr1       # multiple drives in parallel
cdripper -d all                     # auto-detect all optical drives
cdripper -o /mnt/nas/music          # custom output directory
cdripper --once                     # rip one disc and exit
```

To auto-start on login:

```bash
systemctl --user enable --now cdripper
```

## Output Structure

```
~/Music/
├── Artist Name/
│   └── Album Name/
│       ├── 01 - Track Name.flac
│       ├── 02 - Track Name.flac
│       ├── Artist Name - Album Name.m3u
│       └── album_info.txt
├── Various Artists/
│   └── Compilation/
│       ├── 01 - Artist - Track.flac
│       └── ...
└── _logs/
    └── cdripper.log
```

Multi-disc releases put every disc in the **one album directory**, with the
disc number prefixed onto the track number:

```
~/Music/
└── Ani DiFranco/
    └── Rome_ Italy 11.15.04/
        ├── 1-01 - Swan Dive.flac
        ├── 1-02 - Educated Guess.flac
        ├── 2-01 - Nicotine.flac
        ├── 2-02 - Bubble.flac
        ├── Ani DiFranco - Rome_ Italy 11.15.04.m3u   # spans both discs
        ├── album_info_disc1.txt
        └── album_info_disc2.txt
```

This matches MusicBrainz Picard's default naming, and keeps the release as a
single album in Plex, Jellyfin and Navidrome — separate `Disc 1`/`Disc 2`
album folders make players show one release as two. `DISCNUMBER`, `DISCTOTAL`
and (where MusicBrainz has one) `DISCSUBTITLE` are written to every file, which
is what media servers actually group on. Single-disc albums are unaffected.

- FLAC files at max compression (`flac -8`) with full Vorbis tags
- `album_info.txt` has all metadata in `KEY=value` format, including `FAILED_TRACKS` for any tracks that couldn't be ripped. Multi-disc releases get one `album_info_discN.txt` per disc, since the disc ID and track count differ per disc
- `.m3u` playlist for each album
- Filenames sanitized: special characters become `_`
- Desktop notifications on GNOME/KDE for rip progress and errors

## Error Handling

- **MusicBrainz down?** Retries 3 times with backoff, then rips with disc ID as album name
- **Scratched disc?** Retries each failed track up to 3 times. Failed tracks are logged in `album_info.txt` with `FAILED_TRACKS=` so you can go back and re-attempt later
- **Drive looks stalled?** Heartbeat prints elapsed time every 30s while cdparanoia works through bad sectors

## Dependencies

Installed automatically by `setup.sh`:

| System packages | Python packages (via pipx) |
|-----------------|---------------------------|
| cdparanoia      | discid                    |
| flac            | musicbrainzngs            |
| libdiscid       | mutagen                   |
| eject           |                           |

## How It Works

1. Poll CD drive(s) by attempting to read the disc TOC
2. On disc detection, compute disc ID and query MusicBrainz (3 retries)
3. Rip each track with cdparanoia (paranoia mode, retries on failure)
4. Encode to FLAC at max compression
5. Tag each file with Vorbis comments (artist, album, title, track number)
6. Write `album_info.txt` and `.m3u` playlist
7. Eject, wait for next disc

Multiple drives rip in parallel, each in its own thread with prefixed log output.

## Development

```bash
make deps      # system packages (cdparanoia, flac, libdiscid, eject) -- needs sudo
make dev       # create .venv and install cdripper + dev tools, editable
make test      # run the unit tests
make lint      # ruff
make check     # verify binaries, imports, and detected drives
make help      # list every target
```

`make check` is the fastest way to confirm a machine is set up correctly — it
reports missing system binaries, unimportable Python modules, and which optical
drives were detected.

Tests cover the pure logic: filename sanitizing, MusicBrainz response parsing,
`album_info.txt`/`.m3u` output, drive detection, and FLAC tagging. Paths that
need a real disc in a real drive aren't unit tested — verify those with
`make once` against an actual CD.

See [CONTRIBUTING.md](CONTRIBUTING.md) for the branch/PR/review workflow, and
[CLAUDE.md](CLAUDE.md) for architecture notes and the threading model.

## License

MIT
