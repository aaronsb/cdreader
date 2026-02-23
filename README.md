# cdripper

Rip audio CDs to FLAC with MusicBrainz metadata. One Python script, no complex toolchain.

Polls your CD drive, looks up album/artist/track info from MusicBrainz, rips with cdparanoia (EAC-quality error correction), encodes to FLAC, tags, and organizes into `Artist/Album/` directories. Ejects when done, waits for the next disc.

## Install

```bash
curl -fsSL https://raw.githubusercontent.com/aaronsb/cdreader/main/setup.sh | bash
```

The installer:
- Installs system packages via your package manager (sudo, asked once, then dropped)
- Installs cdripper in an isolated virtualenv via pipx (no sudo)
- Sets up a systemd user service (disabled by default)

Works on Arch, Ubuntu/Kubuntu, Debian, Fedora, and derivatives.

### Manual install

```bash
sudo pacman -S cdparanoia flac libdiscid eject pipx   # arch
sudo apt install cdparanoia flac libdiscid0 eject pipx # ubuntu/debian
pipx install git+https://github.com/aaronsb/cdreader.git
```

## Usage

```bash
cdripper                        # poll for discs, rip to ~/Music
cdripper -d /dev/sr0            # specific CD device
cdripper -o /mnt/nas/music      # custom output directory
cdripper --once                 # rip one disc and exit
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

- FLAC files at max compression (`flac -8`) with full Vorbis tags
- `album_info.txt` has all metadata in `KEY=value` format
- `.m3u` playlist for each album
- Filenames sanitized: special characters become `_`

## Dependencies

Installed automatically by `setup.sh`:

| System packages | Python packages (via pipx) |
|-----------------|---------------------------|
| cdparanoia      | discid                    |
| flac            | musicbrainzngs            |
| libdiscid       | mutagen                   |
| eject           |                           |

## How It Works

1. Poll the CD drive by attempting to read the disc TOC
2. On disc detection, compute disc ID and query MusicBrainz
3. Rip each track with cdparanoia (paranoia mode error correction)
4. Encode to FLAC at max compression
5. Tag each file with Vorbis comments (artist, album, title, track number)
6. Write `album_info.txt` and `.m3u` playlist
7. Eject, wait for next disc

If MusicBrainz doesn't recognize the disc, tracks are ripped with the disc ID as the album name and generic track numbers.

## License

MIT
