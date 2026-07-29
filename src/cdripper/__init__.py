#!/usr/bin/env python3
"""
cdripper - Rip audio CDs to FLAC with MusicBrainz metadata.

Polls for disc insertion, rips tracks with cdparanoia, encodes to FLAC,
tags with MusicBrainz metadata, and organizes into Artist/Album/ directories.
"""

import argparse
import contextlib
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from glob import glob
from importlib.metadata import version as pkg_version
from pathlib import Path

import discid
import musicbrainzngs
from mutagen import MutagenError
from mutagen.flac import FLAC

try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11
    try:
        import tomli as tomllib
    except ModuleNotFoundError:
        tomllib = None

try:
    VERSION = pkg_version("cdripper")
except Exception:
    VERSION = "dev"

musicbrainzngs.set_useragent("cdripper", VERSION, "https://github.com/aaronsb/cdreader")

# Set by signal handler to request clean shutdown
_shutdown = False

# Thread-safe log writing
_log_lock = threading.Lock()

# Desktop notification support (GNOME, KDE, etc. via freedesktop)
_has_notify = shutil.which("notify-send") is not None

# Sleep inhibition support (systemd-based Linux)
_has_inhibit = shutil.which("systemd-inhibit") is not None

# Track retry config
MAX_TRACK_RETRIES = 3

# MusicBrainz retry config
MB_RETRIES = 3
MB_RETRY_DELAY = 5

# CD audio 1x read speed in bytes/sec (75 sectors * 2352 bytes)
CD_1X_BPS = 176400

# Per-drive log buffer depth (circular)
LOG_BUFFER_LINES = 50

# Minimum width per log column before switching to vertical stack
MIN_LOG_COL_WIDTH = 40

# Width of the "HH:MM:SS" gutter in the TUI log panels
LOG_STAMP_WIDTH = 8

# Hand-editable metadata sidecar, written into every album directory
METADATA_FILE = "metadata.toml"

# Album-level sidecar fields beyond what MusicBrainz gives us, mapped to the
# Vorbis comment they are written as. Aimed at recordings MusicBrainz does not
# carry -- soundboards, school concerts, self-published discs -- where there is
# a performer and an engineer but no "artist" in the release sense.
# PERFORMER, LOCATION, DESCRIPTION and GENRE are standard Vorbis comments;
# ENGINEER and RECORDINGDATE follow MusicBrainz Picard's usage.
EXTRA_ALBUM_FIELDS = {
    "genre": "GENRE",
    "performer": "PERFORMER",
    "engineer": "ENGINEER",
    "recorded": "RECORDINGDATE",
    "venue": "LOCATION",
    "notes": "DESCRIPTION",
}


# --- Drive state for TUI ---

@dataclass
class DriveState:
    """Mutable state for one drive, read by the display thread."""
    device: str
    label: str = ""
    status: str = "Waiting"
    album: str = ""
    track_num: int = 0
    track_total: int = 0
    track_title: str = ""
    track_progress: float = 0.0
    speed: float = 0.0
    log_lines: deque = field(default_factory=lambda: deque(maxlen=LOG_BUFFER_LINES), repr=False)
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def __post_init__(self):
        self.label = os.path.basename(self.device)

    def update(self, **kwargs):
        with self.lock:
            for k, v in kwargs.items():
                setattr(self, k, v)

    def add_log(self, stamp, msg):
        """Buffer one log entry as (HH:MM:SS, message) for the display thread."""
        with self.lock:
            self.log_lines.append((stamp, msg))

    def get_logs(self):
        with self.lock:
            return list(self.log_lines)

    def snapshot(self):
        with self.lock:
            return {
                "label": self.label,
                "status": self.status,
                "album": self.album,
                "track_num": self.track_num,
                "track_total": self.track_total,
                "track_title": self.track_title,
                "track_progress": self.track_progress,
                "speed": self.speed,
            }


# Global registry of drive states, keyed by device path
_drive_states: dict[str, DriveState] = {}
_display_live = None  # Set to a rich Live object when TUI is active


def _init_display(devices):
    """Start the rich live display if we're in a TTY."""
    global _display_live
    if not sys.stdout.isatty():
        return

    try:
        from rich.console import Console
        from rich.layout import Layout
        from rich.live import Live
        from rich.panel import Panel
        from rich.table import Table
        from rich.text import Text

        console = Console()

        def make_table():
            t = Table(title=f"cdripper {VERSION}", title_style="bold", show_edge=False,
                      pad_edge=False, expand=True)
            t.add_column("Drive", style="cyan", width=8)
            t.add_column("Status", width=12)
            t.add_column("Album", ratio=1, no_wrap=True, overflow="ellipsis")
            t.add_column("Track", width=24)
            t.add_column("Speed", width=8, justify="right")

            for device in sorted(_drive_states):
                s = _drive_states[device].snapshot()

                # Status styling
                status_style = {
                    "Waiting": "dim",
                    "Ripping": "green",
                    "Encoding": "yellow",
                    "Looking up": "blue",
                    "Reading TOC": "blue",
                    "Ejecting": "dim",
                    "Error": "red bold",
                }.get(s["status"], "")
                status = Text(s["status"], style=status_style)

                # Track progress: bar shows intra-track, text shows album position
                if s["track_total"] > 0:
                    tp = s["track_progress"]
                    filled = int(tp * 12)
                    bar = "\u2588" * filled + "\u2591" * (12 - filled)
                    pct = f"{int(tp * 100):>3d}%"
                    track = f"{bar} {pct} {s['track_num']}/{s['track_total']}"
                else:
                    track = ""

                # Speed
                speed = f"{s['speed']:.1f}x" if s["speed"] > 0 else ""

                album = s["album"] or ""

                t.add_row(s["label"], status, album, track, speed)

            return t

        def make_display():
            """Build full TUI: status table on top, per-drive log panels below."""
            table = make_table()
            drive_count = len(_drive_states)
            if drive_count == 0:
                return table

            # Calculate available height for log panels
            term_height = console.height or 25
            # Table: 1 title + 2 header/separator + N rows + 1 bottom = N + 4
            table_height = drive_count + 4
            # Panel border takes 2 lines; leave at least 5 inner lines
            log_inner_height = max(term_height - table_height - 4, 5)

            sorted_devices = sorted(_drive_states)

            def log_body(ds, inner_width, inner_height, placeholder):
                """Render one drive's buffer: dim gutter, hanging-indented wraps.

                Entries are wrapped *before* the height cut, so the panel shows
                the last N rendered lines rather than the last N entries -- the
                latter overflows whenever anything wraps.
                """
                entries = ds.get_logs()
                if not entries:
                    return Text(placeholder, style="dim")

                rows = _wrap_log_entries(entries, inner_width)[-inner_height:]
                text = Text()
                for i, (stamp, body) in enumerate(rows):
                    if i:
                        text.append("\n")
                    if stamp:
                        text.append(f"{stamp} ", style="dim")
                    else:
                        text.append(" " * (LOG_STAMP_WIDTH + 1))
                    text.append(body, style=_log_style(body))
                return text

            # Panel chrome: 2 border columns + 2 padding columns
            panel_chrome = 4
            term_width = console.width or 80

            if drive_count == 1:
                ds = _drive_states[sorted_devices[0]]
                log_panel = Panel(
                    log_body(ds, term_width - panel_chrome, log_inner_height,
                             "Waiting for activity..."),
                    title=f"Log [{ds.label}]", border_style="dim",
                )
            else:
                # Horizontal columns unless too narrow, then stack vertically.
                # Decide first: it determines how wide each panel actually is,
                # and so how the text inside it must be wrapped.
                col_width = term_width // drive_count
                side_by_side = col_width >= MIN_LOG_COL_WIDTH
                inner = max((col_width if side_by_side else term_width)
                            - panel_chrome, 12)
                # Side by side each panel gets the full height; stacked, they
                # split it, and each one's own two border lines come out of
                # its share.
                inner_h = (log_inner_height if side_by_side
                           else max(log_inner_height // drive_count - 2, 3))

                panels = []
                for device in sorted_devices:
                    ds = _drive_states[device]
                    panels.append(Layout(
                        Panel(log_body(ds, inner, inner_h, "Waiting..."),
                              title=ds.label, border_style="cyan"),
                        name=ds.label,
                    ))

                log_area = Layout(name="logs")
                if side_by_side:
                    log_area.split_row(*panels)
                else:
                    log_area.split_column(*panels)

                log_panel = log_area

            layout = Layout(name="root")
            layout.split_column(
                Layout(table, name="status", size=table_height),
                Layout(log_panel, name="log_area"),
            )
            return layout

        live = Live(make_display(), console=console, screen=True,
                    refresh_per_second=2)
        live.start()
        _display_live = live

        # Background refresh
        def refresh_loop():
            while not _shutdown and _display_live:
                try:
                    _display_live.update(make_display())
                except Exception:
                    break
                time.sleep(0.5)

        t = threading.Thread(target=refresh_loop, daemon=True)
        t.start()

    except ImportError:
        pass  # rich not available, fall back to plain output


def _stop_display():
    """Stop the live display."""
    global _display_live
    if _display_live:
        try:
            _display_live.stop()
        except Exception:
            pass
        _display_live = None


def notify(summary, body="", urgency="normal"):
    """Send a desktop notification if notify-send is available."""
    if not _has_notify:
        return
    cmd = ["notify-send", "--app-name=cdripper", f"--urgency={urgency}"]
    icon = {"normal": "media-optical", "critical": "dialog-error"}.get(urgency, "media-optical")
    cmd.extend([f"--icon={icon}", summary])
    if body:
        cmd.append(body)
    subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


@contextlib.contextmanager
def inhibit_sleep():
    """Prevent system sleep while ripping using systemd-inhibit."""
    if not _has_inhibit:
        yield
        return
    proc = subprocess.Popen(
        ["systemd-inhibit", "--what=sleep:idle",
         "--who=cdripper", "--why=Ripping audio CD",
         "--mode=block", "sleep", "infinity"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        yield
    finally:
        proc.terminate()
        proc.wait(timeout=5)


def _handle_signal(signum, frame):
    global _shutdown
    _shutdown = True
    _stop_display()
    print("\nShutting down after current operation...")


def sanitize_filename(name, max_length=200):
    """Replace anything not [-a-zA-Z0-9_ .] with underscore, and truncate.

    Names made only of dots are neutralised too. Slashes already become
    underscores, but "." and ".." would otherwise survive as real path
    components -- and these values become directory names, so a metadata.toml
    saying artist = ".." would place an album outside the library.
    """
    sanitized = re.sub(r"[^-\w .]", "_", name)
    if sanitized and set(sanitized) == {"."}:
        sanitized = "_" * len(sanitized)
    if len(sanitized.encode("utf-8")) > max_length:
        truncated = sanitized.encode("utf-8")[:max_length].decode("utf-8", errors="ignore")
        sanitized = truncated.rstrip(" _-")
    return sanitized


def _device_label(device):
    """Short label for a device, e.g. /dev/sr0 -> sr0."""
    return os.path.basename(device)


# Anchored to the shapes log() is actually called with. Deliberately not
# substring matches: track titles are user data and contain words like "Done"
# and "Error", which would otherwise style a starting rip as a finished one.
_LOG_BAD_PREFIXES = (
    "error", "failed", "drive error", "rip failed",
    "musicbrainz lookup failed", "no musicbrainz match",
)
_LOG_GOOD_PREFIXES = ("found:", "rip complete", "album written")
_LOG_DONE_SUFFIX = re.compile(r"\bdone \(\d+m\d{2}s\)$")


def _log_style(msg):
    """Pick a rich style from the shape of a message this program emits."""
    text = msg.strip()
    low = text.lower()
    if low.startswith(_LOG_BAD_PREFIXES):
        return "red"
    if low.startswith(_LOG_GOOD_PREFIXES) or _LOG_DONE_SUFFIX.search(text):
        return "green"
    return ""


def _wrap_log_entries(entries, width, stamp_width=LOG_STAMP_WIDTH):
    """Flatten (stamp, message) entries into display rows wrapped to `width`.

    Returns a list of (stamp, text) rows. Continuation rows carry an empty
    stamp so the renderer indents them under the message, which is what makes
    a wrapped title read as one entry rather than two.
    """
    avail = max(width - stamp_width - 1, 12)
    rows = []
    for stamp, msg in entries:
        wrapped = textwrap.wrap(msg, width=avail) or [""]
        rows.append((stamp, wrapped[0]))
        rows.extend(("", cont) for cont in wrapped[1:])
    return rows


def log(msg, logfile=None, device=None):
    """Print timestamped message and optionally append to logfile.

    The on-disk log and plain stdout keep the full date and the [srN] prefix,
    since both are read outside any per-drive context. The TUI drops both: its
    panels are already titled with the drive, and a full ISO timestamp on every
    line costs more than half the usable width of a narrow column.
    """
    now = time.localtime()
    prefix = f"[{_device_label(device)}] " if device else ""
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S', now)}] {prefix}{msg}"
    with _log_lock:
        if _display_live:
            stamp = time.strftime("%H:%M:%S", now)
            # Route to per-drive log buffer (rendered by the TUI refresh loop)
            if device and device in _drive_states:
                _drive_states[device].add_log(stamp, msg)
            elif _drive_states:
                # No owning drive: broadcast, keeping the prefix to disambiguate
                for ds in _drive_states.values():
                    ds.add_log(stamp, f"{prefix}{msg}")
        else:
            print(line, flush=True)
        if logfile:
            with open(logfile, "a") as f:
                f.write(line + "\n")


def check_dependencies():
    """Verify required system binaries are available."""
    missing = []
    for cmd in ("cdparanoia", "flac", "eject"):
        if shutil.which(cmd) is None:
            missing.append(cmd)
    if missing:
        print(f"Missing required commands: {', '.join(missing)}", file=sys.stderr)
        print("Install them with your package manager.", file=sys.stderr)
        sys.exit(1)


def detect_drives():
    """Auto-detect all optical drives."""
    drives = sorted(glob("/dev/sr*"))
    if not drives:
        if os.path.exists("/dev/cdrom"):
            drives = ["/dev/cdrom"]
    return drives


def read_disc(device):
    """Try to read the disc TOC. Returns discid.Disc or None."""
    try:
        return discid.read(device)
    except (discid.DiscError, OSError):
        return None


def lookup_metadata(disc, logfile=None, device=None):
    """Query MusicBrainz for disc metadata with retries. Returns dict or None."""
    for attempt in range(1, MB_RETRIES + 1):
        try:
            result = musicbrainzngs.get_releases_by_discid(
                disc.id, includes=["artists", "recordings", "artist-credits"]
            )
            break
        except musicbrainzngs.WebServiceError as e:
            if attempt < MB_RETRIES:
                log(f"MusicBrainz lookup failed (attempt {attempt}/{MB_RETRIES}), "
                    f"retrying in {MB_RETRY_DELAY}s: {e}", logfile, device)
                time.sleep(MB_RETRY_DELAY)
            else:
                log(f"MusicBrainz lookup failed after {MB_RETRIES} attempts: {e}",
                    logfile, device)
                return None
    else:
        return None

    if "disc" not in result:
        return None

    release_list = result["disc"].get("release-list", [])
    if not release_list:
        return None

    release = release_list[0]
    album_artist = release.get("artist-credit-phrase", "Unknown Artist")
    album = release.get("title", "Unknown Album")
    date = release.get("date", "")

    media = release.get("medium-list", [])
    matched, exact = _match_medium(media, disc.id)
    if matched is None:
        return None

    tracks = _extract_tracks(matched, album_artist)
    if not tracks:
        # A medium that matched but carries no track list tells us nothing
        # useful. Better to rip under the disc ID than to label this disc with
        # some other disc's titles.
        log(f"Release '{album}' has no track list for this disc.", logfile, device)
        return None

    if not exact and len(media) > 1:
        log(f"Disc ID not listed on any medium of '{album}' ({len(media)} discs). "
            f"Assuming disc 1 -- check DISCNUMBER before filing these.",
            logfile, device)

    is_va = album_artist.lower() in ("various artists", "various")

    return {
        "artist": album_artist,
        "album": album,
        "date": date,
        "tracks": sorted(tracks, key=lambda t: t["number"]),
        "is_va": is_va,
        "disc_id": disc.id,
        "disc_number": _medium_position(matched, media),
        "disc_total": len(media),
        "disc_subtitle": matched.get("title", ""),
    }


def _match_medium(media, disc_id):
    """Find the medium carrying this disc ID.

    Returns (medium, exact). `exact` is False when nothing matched and the
    first medium was substituted, because on a multi-disc release that means
    any disc number we report is a guess.
    """
    for medium in media:
        for disc_entry in medium.get("disc-list", []):
            if disc_entry.get("id") == disc_id:
                return medium, True
    return (media[0], False) if media else (None, False)


def _medium_position(medium, media):
    """1-based position of a medium within its release.

    Prefers MusicBrainz's own `position`, falling back to list order when it
    is missing or unparseable.
    """
    try:
        position = int(medium.get("position", 0))
    except (TypeError, ValueError):
        position = 0
    if position > 0:
        return position
    try:
        return media.index(medium) + 1
    except ValueError:
        return 1


def _extract_tracks(medium, album_artist):
    """Extract track list from a MusicBrainz medium, including per-track artists."""
    tracks = []
    for track in medium.get("track-list", []):
        recording = track.get("recording", {})

        track_artist = album_artist
        artist_credit = recording.get("artist-credit", [])
        if artist_credit:
            parts = []
            for credit in artist_credit:
                if isinstance(credit, dict) and "artist" in credit:
                    parts.append(credit["artist"].get("name", ""))
                elif isinstance(credit, str):
                    parts.append(credit)
            joined = "".join(parts).strip()
            if joined:
                track_artist = joined

        tracks.append({
            "number": int(track.get("number", 0)),
            "title": recording.get("title", f"Track {track.get('number', '?')}"),
            "artist": track_artist,
        })
    return tracks


def rip_and_encode(device, track_num, output_path, logfile=None, track_label="",
                   drive_state=None, expected_wav_size=0):
    """Rip a single track with cdparanoia and encode to FLAC.

    Monitors the temp WAV file size to calculate read speed and track progress.
    """
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        wav_path = tmp.name

    start_time = time.time()
    speed_stop = threading.Event()

    def speed_monitor():
        """Periodically check WAV file size to calculate read speed and progress."""
        last_size = 0
        last_time = start_time
        while not speed_stop.wait(2):
            try:
                current_size = os.path.getsize(wav_path)
            except OSError:
                continue
            now = time.time()
            dt = now - last_time
            if dt > 0:
                bps = (current_size - last_size) / dt
                speed = bps / CD_1X_BPS
                progress = (min(current_size / expected_wav_size, 1.0)
                            if expected_wav_size > 0 else 0.0)
                if drive_state:
                    drive_state.update(speed=speed, track_progress=progress)
            last_size = current_size
            last_time = now

    mon = threading.Thread(target=speed_monitor, daemon=True)
    mon.start()

    try:
        proc = subprocess.Popen(
            ["cdparanoia", "-d", device, str(track_num), wav_path],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        proc.wait()
        if proc.returncode != 0:
            raise subprocess.CalledProcessError(proc.returncode, "cdparanoia")

        if drive_state:
            drive_state.update(status="Encoding", speed=0.0, track_progress=1.0)

        # Remove existing FLAC to avoid encoder refusal on re-rip
        out = Path(output_path)
        if out.exists():
            out.unlink()

        subprocess.run(
            ["flac", "-s", "-8", "--force", "-o", str(output_path), wav_path],
            check=True,
            capture_output=True,
        )
    finally:
        speed_stop.set()
        mon.join(timeout=1)
        if os.path.exists(wav_path):
            os.unlink(wav_path)

    elapsed = time.time() - start_time
    mins, secs = divmod(int(elapsed), 60)
    log(f"  {track_label} done ({mins}m{secs:02d}s)", logfile, device)


def tag_flac(path, metadata, track):
    """Write Vorbis tags to a FLAC file."""
    audio = FLAC(str(path))
    audio["ARTIST"] = track["artist"]
    audio["ALBUM"] = metadata["album"]
    audio["TITLE"] = track["title"]
    audio["TRACKNUMBER"] = str(track["number"])
    audio["TRACKTOTAL"] = str(len(metadata["tracks"]))
    audio["ALBUMARTIST"] = metadata["artist"]
    audio["DISCID"] = metadata["disc_id"]
    # Media servers group multi-disc releases by these, not by directory layout
    audio["DISCNUMBER"] = str(metadata.get("disc_number", 1))
    audio["DISCTOTAL"] = str(metadata.get("disc_total", 1))
    if metadata.get("disc_subtitle"):
        audio["DISCSUBTITLE"] = metadata["disc_subtitle"]
    if metadata.get("date"):
        audio["DATE"] = metadata["date"]
    # Hand-entered fields from metadata.toml. Blank means "leave it alone", so
    # an empty box in the sidecar clears the tag rather than writing "".
    for key, tag in EXTRA_ALBUM_FIELDS.items():
        value = str(metadata.get(key, "") or "").strip()
        if value:
            audio[tag] = value
        elif tag in audio:
            del audio[tag]
    audio.save()


def _album_info_name(metadata):
    """Filename for this disc's info file.

    Every disc of a release shares one directory, and DISCID, TRACKS and
    FAILED_TRACKS are all per-disc facts. Rather than merge them into one
    ambiguous file, each disc of a multi-disc release gets its own.
    """
    if metadata.get("disc_total", 1) > 1:
        return f"album_info_disc{metadata.get('disc_number', 1)}.txt"
    return "album_info.txt"


def write_album_info(album_dir, metadata, failed_tracks=None):
    """Write album_info.txt with tag data and any failures."""
    info_path = album_dir / _album_info_name(metadata)
    with open(info_path, "w") as f:
        f.write(f"ARTIST={metadata['artist']}\n")
        f.write(f"ALBUM={metadata['album']}\n")
        if metadata.get("date"):
            f.write(f"DATE={metadata['date']}\n")
        f.write(f"DISCID={metadata['disc_id']}\n")
        if metadata.get("disc_total", 1) > 1:
            f.write(f"DISCNUMBER={metadata.get('disc_number', 1)}\n")
            f.write(f"DISCTOTAL={metadata['disc_total']}\n")
            if metadata.get("disc_subtitle"):
                f.write(f"DISCSUBTITLE={metadata['disc_subtitle']}\n")
        f.write(f"TRACKS={len(metadata['tracks'])}\n")

        if failed_tracks:
            f.write(f"FAILED_TRACKS={','.join(str(t) for t in sorted(failed_tracks))}\n")

        for track in metadata["tracks"]:
            num = track["number"]
            if failed_tracks and num in failed_tracks:
                f.write(f"TRACK{num:02d}=FAILED: rip error after {MAX_TRACK_RETRIES} attempts\n")
            elif metadata["is_va"]:
                f.write(f"TRACK{num:02d}={track['artist']} - {track['title']}\n")
            else:
                f.write(f"TRACK{num:02d}={track['title']}\n")


def _playlist_sort_key(name):
    """Order playlist entries by disc then track, not by string.

    Lexical order would put "10-01" before "2-01" on a box set.
    """
    match = re.match(r"^(?:(\d+)-)?(\d+) - ", name)
    if match:
        return (int(match.group(1) or 0), int(match.group(2)), name)
    return (0, 0, name)


def write_playlist(album_dir, metadata, failed_tracks=None):
    """Write .m3u playlist file (skipping failed tracks).

    Every disc of a release writes into the same directory, so for a multi-disc
    album this merges with what an earlier disc already wrote. Replacing it
    would leave a playlist holding only whichever disc was ripped last.
    """
    artist_safe = sanitize_filename(metadata["artist"])
    album_safe = sanitize_filename(metadata["album"])
    m3u_path = album_dir / f"{artist_safe} - {album_safe}.m3u"

    entries = []
    if metadata.get("disc_total", 1) > 1 and m3u_path.exists():
        # Keep other discs' entries, but drop this disc's: the rip we are
        # finishing is authoritative for it. Merging blindly would strand a
        # line pointing at a track that just failed and had its FLAC deleted.
        own = f"{metadata.get('disc_number', 1)}-"
        entries = [ln for ln in m3u_path.read_text().splitlines()
                   if ln.strip() and not ln.startswith(own)]

    for track in metadata["tracks"]:
        if failed_tracks and track["number"] in failed_tracks:
            continue
        name = _track_filename(track, metadata)
        if name not in entries:
            entries.append(name)

    with open(m3u_path, "w") as f:
        for name in sorted(entries, key=_playlist_sort_key):
            f.write(name + "\n")


def _track_filename(track, metadata):
    """Build the FLAC filename for a track.

    Multi-disc releases get a `disc-` prefix on the track number, so both discs
    can share one album directory without colliding. This matches MusicBrainz
    Picard's default naming and keeps the release as a single album in Plex,
    Jellyfin and Navidrome. Single-disc rips are unaffected.
    """
    num = f"{track['number']:02d}"
    if metadata.get("disc_total", 1) > 1:
        num = f"{metadata.get('disc_number', 1)}-{num}"
    title = sanitize_filename(track["title"])
    if metadata["is_va"]:
        return f"{num} - {sanitize_filename(track['artist'])} - {title}.flac"
    return f"{num} - {title}.flac"


# --- metadata.toml sidecar ---

def _toml_str(value):
    """Render a value as a TOML basic string."""
    text = "" if value is None else str(value)
    for old, new in (("\\", "\\\\"), ('"', '\\"'), ("\n", "\\n"),
                     ("\r", "\\r"), ("\t", "\\t")):
        text = text.replace(old, new)
    return f'"{text}"'


def _metadata_file_name(metadata):
    """Sidecar filename for this disc.

    Every disc of a release shares a directory, so each needs its own sidecar --
    otherwise the second disc's rip overwrites the first's and applying it
    would treat disc 1's tracks as missing.
    """
    if metadata.get("disc_total", 1) > 1:
        return f"metadata_disc{metadata.get('disc_number', 1)}.toml"
    return METADATA_FILE


def _sidecar_paths(album_dir):
    """Every metadata sidecar in a directory, ordered by disc."""
    album_dir = Path(album_dir)
    paths = []
    plain = album_dir / METADATA_FILE
    if plain.exists():
        paths.append(plain)
    numbered = []
    for candidate in album_dir.glob("metadata_disc*.toml"):
        match = re.fullmatch(r"metadata_disc(\d+)\.toml", candidate.name)
        if match:
            numbered.append((int(match.group(1)), candidate))
    paths.extend(path for _, path in sorted(numbered))
    return paths


def write_metadata_toml(album_dir, metadata):
    """Write the hand-editable metadata sidecar for a rip.

    Written for every rip, not just unmatched discs, so an album with good
    MusicBrainz data can still be corrected or annotated. Known values are
    pre-filled; the extra fields start blank.
    """
    path = Path(album_dir) / _metadata_file_name(metadata)
    lines = [
        "# cdripper metadata. Edit this file, then run:",
        "#",
        f"#     cdripper apply {shlex.quote(str(album_dir))}",
        "#",
        "# Applying re-tags the FLACs, renames them to match changed titles,",
        "# and moves the album directory if artist or album changed.",
        "# Blank values are skipped rather than written as empty tags.",
        "",
        "[album]",
        f"artist      = {_toml_str(metadata.get('artist', ''))}",
        f"album       = {_toml_str(metadata.get('album', ''))}",
        f"date        = {_toml_str(metadata.get('date', ''))}"
        "        # release date, YYYY-MM-DD",
        f"compilation = {str(bool(metadata.get('is_va'))).lower()}"
        "        # true puts the artist in each filename",
        "",
        "# For recordings MusicBrainz does not have: live sets, soundboards,",
        "# school concerts, self-published discs.",
    ]
    for key in EXTRA_ALBUM_FIELDS:
        comment = {
            "recorded": "        # when it was recorded, YYYY-MM-DD",
            "venue": "        # where it was recorded",
            "engineer": "        # audio engineer",
        }.get(key, "")
        lines.append(f"{key:<11} = {_toml_str(metadata.get(key, ''))}{comment}")

    lines += [
        "",
        "[disc]",
        f"number   = {metadata.get('disc_number', 1)}",
        f"total    = {metadata.get('disc_total', 1)}",
        f"subtitle = {_toml_str(metadata.get('disc_subtitle', ''))}",
        f"id       = {_toml_str(metadata.get('disc_id', ''))}"
        "        # MusicBrainz disc ID -- do not edit",
        "",
    ]

    for track in metadata.get("tracks", []):
        lines += [
            "[[track]]",
            f"number = {track['number']}",
            f"title  = {_toml_str(track.get('title', ''))}",
            f"artist = {_toml_str(track.get('artist', ''))}",
            "",
        ]

    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return path


def read_metadata_toml(path):
    """Parse a metadata sidecar into the metadata dict the writers expect.

    Raises ValueError on anything that would produce a broken rip.
    """
    if tomllib is None:
        raise ValueError(
            "Reading metadata.toml needs Python 3.11+, or the 'tomli' package "
            "on older versions. Reinstall with: pipx install --force cdripper")

    path = Path(path)
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        raise ValueError(f"{path.name} is not valid TOML: {e}") from e

    album = data.get("album") or {}
    disc = data.get("disc") or {}
    raw_tracks = data.get("track") or []

    artist = str(album.get("artist", "")).strip()
    title = str(album.get("album", "")).strip()
    if not artist:
        raise ValueError("album.artist is empty -- it becomes a directory name")
    if not title:
        raise ValueError("album.album is empty -- it becomes a directory name")
    if not raw_tracks:
        raise ValueError("no [[track]] entries found")

    tracks, seen = [], set()
    for entry in raw_tracks:
        try:
            number = int(entry.get("number", 0))
        except (TypeError, ValueError):
            raise ValueError(f"track number {entry.get('number')!r} is not a number") from None
        if number < 1:
            raise ValueError(f"track number {number} must be 1 or greater")
        if number in seen:
            raise ValueError(f"track number {number} appears more than once")
        seen.add(number)
        tracks.append({
            "number": number,
            "title": str(entry.get("title", "")).strip() or f"Track {number:02d}",
            "artist": str(entry.get("artist", "")).strip() or artist,
        })

    def _positive_int(value, default):
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return default
        return parsed if parsed > 0 else default

    metadata = {
        "artist": artist,
        "album": title,
        "date": str(album.get("date", "")).strip(),
        "is_va": bool(album.get("compilation", False)),
        "disc_id": str(disc.get("id", "")).strip(),
        "disc_number": _positive_int(disc.get("number"), 1),
        "disc_total": _positive_int(disc.get("total"), 1),
        "disc_subtitle": str(disc.get("subtitle", "")).strip(),
        "tracks": sorted(tracks, key=lambda t: t["number"]),
    }
    for key in EXTRA_ALBUM_FIELDS:
        metadata[key] = str(album.get(key, "")).strip()
    return metadata


def _parse_ripped_filename(name):
    """Extract (disc, track) from a ripped FLAC's name, or None if unrecognised."""
    match = re.match(r"^(?:(\d+)-)?(\d+) - ", name)
    if not match:
        return None
    return (int(match.group(1) or 0), int(match.group(2)))


def _track_key(metadata, number):
    """The (disc, track) key a track's filename should carry."""
    disc = metadata.get("disc_number", 1) if metadata.get("disc_total", 1) > 1 else 0
    return (disc, number)


def metadata_from_flacs(album_dir):
    """Rebuild a metadata dict from the FLACs already sitting in a directory.

    Lets rips made before the sidecar existed be brought into the round trip.
    """
    album_dir = Path(album_dir)
    flacs = sorted(album_dir.glob("*.flac"))
    if not flacs:
        raise ValueError(f"no FLAC files in {album_dir}")

    def first(tags, key, default=""):
        value = tags.get(key)
        return value[0] if value else default

    tracks = []
    album_tags = None
    for flac_path in flacs:
        try:
            tags = FLAC(str(flac_path))
        except MutagenError as e:
            raise ValueError(f"{flac_path.name} is not readable as FLAC: {e}") from e
        if album_tags is None:
            album_tags = tags
        parsed = _parse_ripped_filename(flac_path.name)
        try:
            number = int(first(tags, "TRACKNUMBER", "0"))
        except ValueError:
            number = 0
        if number < 1:
            number = parsed[1] if parsed else len(tracks) + 1
        tracks.append({
            "number": number,
            "title": first(tags, "TITLE", flac_path.stem),
            "artist": first(tags, "ARTIST"),
        })

    album_artist = first(album_tags, "ALBUMARTIST") or first(album_tags, "ARTIST")
    metadata = {
        "artist": album_artist,
        "album": first(album_tags, "ALBUM", album_dir.name),
        "date": first(album_tags, "DATE"),
        "is_va": len({t["artist"] for t in tracks}) > 1,
        "disc_id": first(album_tags, "DISCID"),
        "disc_number": int(first(album_tags, "DISCNUMBER", "1") or 1),
        "disc_total": int(first(album_tags, "DISCTOTAL", "1") or 1),
        "disc_subtitle": first(album_tags, "DISCSUBTITLE"),
        "tracks": sorted(tracks, key=lambda t: t["number"]),
    }
    for key, tag in EXTRA_ALBUM_FIELDS.items():
        metadata[key] = first(album_tags, tag)
    return metadata


def _rename_tracks(album_dir, metadata, logfile=None):
    """Rename existing FLACs to match the sidecar's titles.

    Renames go via temporary names so a set of changes that permutes existing
    filenames cannot clobber a file that has not moved yet.
    """
    existing = {}
    for flac_path in album_dir.glob("*.flac"):
        key = _parse_ripped_filename(flac_path.name)
        if key is not None:
            existing[key] = flac_path

    pending = []
    for track in metadata["tracks"]:
        source = existing.get(_track_key(metadata, track["number"]))
        if source is None:
            # The sidecar's disc numbering may have been edited, in which case
            # the file on disk still carries the old prefix. Fall back to the
            # track number alone, but only when it is unambiguous.
            candidates = [path for (_, number), path in existing.items()
                          if number == track["number"]]
            if len(candidates) != 1:
                continue
            source = candidates[0]
        wanted = _track_filename(track, metadata)
        if source.name != wanted:
            pending.append((source, wanted))

    if not pending:
        return 0

    staged = []
    for index, (source, wanted) in enumerate(pending):
        temp = album_dir / f".cdripper-rename-{index}"
        source.rename(temp)
        staged.append((temp, wanted))
    for temp, wanted in staged:
        temp.rename(album_dir / wanted)

    log(f"Renamed {len(pending)} file(s)", logfile)
    return len(pending)


def _remove_stale_sidecars(album_dir, metadata, logfile=None, keep_info=None):
    """Delete our own playlist/info files left orphaned by a rename.

    The playlist is named after the artist and album, so correcting either in
    the sidecar leaves the old one behind pointing at files that have moved.
    Only files this program clearly wrote are removed: a playlist has to
    consist entirely of ripped-track filenames, none of which still resolve.
    """
    removed = 0
    keep_playlist = (f"{sanitize_filename(metadata['artist'])} - "
                     f"{sanitize_filename(metadata['album'])}.m3u")
    for m3u in album_dir.glob("*.m3u"):
        if m3u.name == keep_playlist:
            continue
        entries = [ln.strip() for ln in m3u.read_text().splitlines() if ln.strip()]
        if not entries or not all(_parse_ripped_filename(e) for e in entries):
            continue  # not ours -- leave it alone
        if any((album_dir / e).exists() for e in entries):
            continue  # still points at real files
        m3u.unlink()
        removed += 1
        log(f"Removed stale playlist {m3u.name}", logfile)

    # Switching between the single- and multi-disc naming leaves the other
    # convention's info file behind. Sibling discs' files must survive.
    keep_info = keep_info or {_album_info_name(metadata)}
    multi = metadata.get("disc_total", 1) > 1
    for info in album_dir.glob("album_info*.txt"):
        if info.name in keep_info:
            continue
        per_disc = re.fullmatch(r"album_info_disc\d+\.txt", info.name)
        if multi and per_disc:
            continue  # belongs to another disc of this release
        info.unlink()
        removed += 1
        log(f"Removed stale {info.name}", logfile)
    return removed


def _relocate_album(album_dir, metadata, logfile=None):
    """Move the album directory if artist or album changed. Returns the new path."""
    library_root = album_dir.parent.parent
    target = (library_root
              / sanitize_filename(metadata["artist"])
              / sanitize_filename(metadata["album"]))
    if target.resolve() == album_dir.resolve():
        return album_dir

    # Defence in depth: the sidecar is editable, and these values become
    # directory names. Never write outside the library the album came from.
    root = library_root.resolve()
    if root not in target.resolve().parents:
        log(f"Refusing to move outside {root}: artist/album resolve to {target}",
            logfile)
        return album_dir
    if target.exists():
        log(f"Not moving: {target} already exists. Files updated in place.", logfile)
        return album_dir

    target.parent.mkdir(parents=True, exist_ok=True)
    album_dir.rename(target)
    log(f"Moved album to {target}", logfile)

    # Tidy up an artist directory left empty by the move
    old_artist_dir = album_dir.parent
    with contextlib.suppress(OSError):
        old_artist_dir.rmdir()
    return target


def apply_metadata(album_dir, logfile=None, move=True):
    """Apply a directory's metadata sidecars back onto the rip they describe.

    Re-tags every FLAC, renames files whose titles changed, rewrites
    album_info.txt and the playlist, and moves the album directory when the
    artist or album name changed. A multi-disc album has one sidecar per disc
    and all of them are applied. Returns the directory the album now lives in.
    """
    album_dir = Path(album_dir)
    sidecars = _sidecar_paths(album_dir)
    if not sidecars:
        raise ValueError(f"no {METADATA_FILE} in {album_dir}")

    applied, tagged = [], 0
    for sidecar in sidecars:
        metadata = read_metadata_toml(sidecar)
        _rename_tracks(album_dir, metadata, logfile)

        missing = []
        for track in metadata["tracks"]:
            flac_path = album_dir / _track_filename(track, metadata)
            if not flac_path.exists():
                missing.append(track["number"])
                continue
            tag_flac(flac_path, metadata, track)
            tagged += 1

        failed = set(missing) or None
        write_album_info(album_dir, metadata, failed)
        write_playlist(album_dir, metadata, failed)
        if missing:
            log(f"No FLAC for track(s) {', '.join(str(n) for n in sorted(missing))}"
                f" of disc {metadata.get('disc_number', 1)} -- recorded as failed",
                logfile)
        applied.append((sidecar, metadata))

    # All discs of a release share an artist and album, so the first sidecar
    # decides where the directory belongs.
    primary = applied[0][1]
    _remove_stale_sidecars(album_dir, primary, logfile,
                           keep_info={_album_info_name(m) for _, m in applied})

    if move:
        album_dir = _relocate_album(album_dir, primary, logfile)

    # Refresh each sidecar in its final location, dropping any whose name
    # changed because the disc numbering was edited.
    for sidecar, metadata in applied:
        written = write_metadata_toml(album_dir, metadata)
        stale = album_dir / sidecar.name
        if stale != written and stale.exists():
            stale.unlink()

    log(f"Applied metadata to {tagged} file(s) in {album_dir}", logfile)
    return album_dir


def eject_disc(device):
    """Eject the disc."""
    subprocess.run(["eject", device], capture_output=True)


def rip_disc(disc, device, output_dir, logfile, drive_state=None):
    """Full rip pipeline for one disc."""
    total = disc.last_track_num
    log(f"Disc ID: {disc.id}, {total} tracks", logfile, device)

    if drive_state:
        drive_state.update(status="Looking up", track_num=0, track_total=total)

    log("Looking up metadata on MusicBrainz...", logfile, device)
    metadata = lookup_metadata(disc, logfile, device)

    if metadata is None:
        log("No MusicBrainz match. Using disc ID for folder name.", logfile, device)
        notify("Unknown disc", f"No MusicBrainz match\nDisc ID: {disc.id}")
        metadata = {
            "artist": "Unknown Artist",
            "album": disc.id,
            "date": "",
            "tracks": [
                {"number": i, "title": f"Track {i:02d}", "artist": "Unknown Artist"}
                for i in range(1, total + 1)
            ],
            "is_va": False,
            "disc_id": disc.id,
            "disc_number": 1,
            "disc_total": 1,
            "disc_subtitle": "",
        }
    else:
        log(f"Found: {metadata['artist']} - {metadata['album']}", logfile, device)
        notify("Ripping CD",
               f"{metadata['artist']} \u2014 {metadata['album']}\n{len(metadata['tracks'])} tracks")

    album_display = f"{metadata['artist']} \u2014 {metadata['album']}"
    if drive_state:
        drive_state.update(album=album_display, track_total=len(metadata["tracks"]))

    # Create output directory
    artist_dir = sanitize_filename(metadata["artist"])
    album_dir_name = sanitize_filename(metadata["album"])
    album_dir = Path(output_dir) / artist_dir / album_dir_name
    album_dir.mkdir(parents=True, exist_ok=True)

    # Build expected WAV sizes from disc TOC (sectors * 2352 bytes + 44 byte header)
    track_wav_sizes = {}
    for dt in disc.tracks:
        track_wav_sizes[dt.number] = dt.sectors * 2352 + 44

    total = len(metadata["tracks"])
    failed_tracks = set()

    for track in metadata["tracks"]:
        if _shutdown:
            log("Shutdown requested, stopping after current track.", logfile, device)
            return False

        num = track["number"]
        track_label = f"Track {num:02d}/{total:02d}: {track['title']}"
        fname = _track_filename(track, metadata)
        flac_path = album_dir / fname
        if flac_path.exists():
            log(f"  {track_label}: overwriting existing file", logfile, device)
        log(f"  Ripping {track_label}", logfile, device)

        expected_wav = track_wav_sizes.get(num, 0)

        if drive_state:
            drive_state.update(status="Ripping", track_num=num,
                               track_title=track["title"], speed=0.0,
                               track_progress=0.0)

        success = False
        for attempt in range(1, MAX_TRACK_RETRIES + 1):
            try:
                rip_and_encode(device, num, flac_path, logfile, track_label,
                               drive_state, expected_wav_size=expected_wav)
                tag_flac(flac_path, metadata, track)
                success = True
                break
            except (subprocess.CalledProcessError, OSError) as e:
                if attempt < MAX_TRACK_RETRIES:
                    log(f"  ERROR on {track_label} (attempt {attempt}/{MAX_TRACK_RETRIES}): "
                        f"{e}, retrying...", logfile, device)
                else:
                    log(f"  FAILED {track_label} after {MAX_TRACK_RETRIES} attempts: {e}",
                        logfile, device)

        if not success:
            failed_tracks.add(num)
            if flac_path.exists():
                flac_path.unlink()

    ripped = total - len(failed_tracks)

    write_album_info(album_dir, metadata, failed_tracks or None)
    write_playlist(album_dir, metadata, failed_tracks or None)
    write_metadata_toml(album_dir, metadata)
    log(f"Album written to {album_dir}", logfile, device)
    log(f"Edit {METADATA_FILE} there and run 'cdripper apply' to correct it",
        logfile, device)

    if drive_state:
        drive_state.update(status="Done", speed=0.0, track_num=total, track_progress=1.0)

    if not failed_tracks:
        notify("Rip complete",
               f"{metadata['artist']} \u2014 {metadata['album']}\n{ripped} tracks")
    elif ripped > 0:
        failed_list = ", ".join(str(t) for t in sorted(failed_tracks))
        notify("Rip finished with errors",
               f"{metadata['artist']} \u2014 {metadata['album']}\n"
               f"{ripped}/{total} tracks, failed: {failed_list}", "critical")
    else:
        notify("Rip failed",
               f"{metadata['artist']} \u2014 {metadata['album']}", "critical")

    return ripped > 0


def poll_and_rip(device, output_dir, poll_interval=2):
    """Main loop: poll for disc, rip, eject, repeat."""
    log_dir = Path(output_dir) / "_logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    logfile = str(log_dir / "cdripper.log")

    drive_state = _drive_states.get(device)

    log(f"cdripper {VERSION} started", logfile, device)
    log(f"Watching {device}, output to {output_dir}", logfile, device)
    log("Insert a disc to begin.", logfile, device)

    try:
        while not _shutdown:
            disc = read_disc(device)
            if disc is not None:
                log("Disc detected", logfile, device)
                if drive_state:
                    drive_state.update(status="Reading TOC")

                success = rip_disc(disc, device, output_dir, logfile, drive_state)

                if drive_state:
                    drive_state.update(status="Ejecting", speed=0.0)

                if success:
                    log("Rip complete. Ejecting.", logfile, device)
                else:
                    log("Rip failed or interrupted. Ejecting.", logfile, device)
                eject_disc(device)

                if drive_state:
                    drive_state.update(status="Waiting", album="", track_num=0,
                                       track_total=0, track_title="", speed=0.0,
                                       track_progress=0.0)
                log("Ready for next disc.", logfile, device)
                time.sleep(5)
            else:
                time.sleep(poll_interval)
    except Exception as e:
        log(f"Drive error: {e}", logfile, device)
        if drive_state:
            drive_state.update(status="Error")

    log("Stopped.", logfile, device)


def _run_apply(paths, move=True):
    """`cdripper apply` -- returns a process exit code."""
    failures = 0
    for raw in paths:
        album_dir = Path(raw).expanduser()
        try:
            apply_metadata(album_dir, move=move)
        except (ValueError, OSError, MutagenError) as e:
            print(f"{album_dir}: {e}", file=sys.stderr)
            failures += 1
    return 1 if failures else 0


def _run_toml(paths, force=False):
    """`cdripper toml` -- returns a process exit code."""
    failures = 0
    for raw in paths:
        album_dir = Path(raw).expanduser()
        sidecar = album_dir / METADATA_FILE
        if sidecar.exists() and not force:
            print(f"{sidecar} exists; pass --force to overwrite", file=sys.stderr)
            failures += 1
            continue
        try:
            metadata = metadata_from_flacs(album_dir)
        except (ValueError, OSError, MutagenError) as e:
            print(f"{album_dir}: {e}", file=sys.stderr)
            failures += 1
            continue
        print(f"Wrote {write_metadata_toml(album_dir, metadata)}")
    return 1 if failures else 0


def main():
    parser = argparse.ArgumentParser(
        description="Rip audio CDs to FLAC with MusicBrainz metadata."
    )
    parser.add_argument(
        "-d", "--device",
        nargs="*",
        default=None,
        help="CD-ROM device(s) (default: auto-detect all drives)",
    )
    parser.add_argument(
        "-o", "--output",
        default=os.path.expanduser("~/Music"),
        help="Output directory (default: ~/Music)",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Rip one disc and exit (no polling loop)",
    )
    parser.add_argument(
        "-v", "--version",
        action="version",
        version=f"cdripper {VERSION}",
    )

    # Subcommands are optional: bare `cdripper` still polls and rips.
    sub = parser.add_subparsers(dest="command")

    apply_cmd = sub.add_parser(
        "apply",
        help=f"apply an edited {METADATA_FILE} back onto its rip",
        description=f"Re-tag, rename and relocate a rip from its {METADATA_FILE}.",
    )
    apply_cmd.add_argument("paths", nargs="+", help="album directories")
    apply_cmd.add_argument(
        "--in-place", action="store_true",
        help="re-tag without moving the directory when artist/album changed",
    )

    toml_cmd = sub.add_parser(
        "toml",
        help=f"(re)generate {METADATA_FILE} from the FLACs already in a directory",
        description="Bring an existing rip into the edit/apply round trip.",
    )
    toml_cmd.add_argument("paths", nargs="+", help="album directories")
    toml_cmd.add_argument(
        "-f", "--force", action="store_true",
        help=f"overwrite an existing {METADATA_FILE}",
    )

    args = parser.parse_args()

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    if args.command == "apply":
        sys.exit(_run_apply(args.paths, move=not args.in_place))
    if args.command == "toml":
        sys.exit(_run_toml(args.paths, force=args.force))

    check_dependencies()

    # Resolve device list (auto-detect by default)
    if args.device is None or (len(args.device) == 1 and args.device[0] == "all"):
        devices = detect_drives()
        if not devices:
            print("No optical drives detected.", file=sys.stderr)
            sys.exit(1)
        print(f"Detected {len(devices)} drive(s): {', '.join(devices)}")
    else:
        devices = args.device

    with inhibit_sleep():
        if args.once:
            # --once: no TUI, just rip and exit
            log_dir = Path(args.output) / "_logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            logfile = str(log_dir / "cdripper.log")

            for device in devices:
                disc = read_disc(device)
                if disc is not None:
                    success = rip_disc(disc, device, args.output, logfile)
                    eject_disc(device)
                    sys.exit(0 if success else 1)

            log("No disc found in any drive.", logfile)
            sys.exit(1)

        # Initialize drive states and display
        for device in devices:
            _drive_states[device] = DriveState(device=device)

        _init_display(devices)

        try:
            if len(devices) == 1:
                poll_and_rip(devices[0], args.output)
            else:
                threads = []
                for device in devices:
                    t = threading.Thread(
                        target=poll_and_rip,
                        args=(device, args.output),
                        name=_device_label(device),
                        daemon=True,
                    )
                    t.start()
                    threads.append(t)

                while not _shutdown and any(t.is_alive() for t in threads):
                    time.sleep(1)
        finally:
            _stop_display()


if __name__ == "__main__":
    main()
