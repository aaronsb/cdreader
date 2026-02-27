#!/usr/bin/env python3
"""
cdripper - Rip audio CDs to FLAC with MusicBrainz metadata.

Polls for disc insertion, rips tracks with cdparanoia, encodes to FLAC,
tags with MusicBrainz metadata, and organizes into Artist/Album/ directories.
"""

import argparse
import os
import re
import shutil
import signal
import contextlib
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from glob import glob
from importlib.metadata import version as pkg_version
from pathlib import Path

import discid
import musicbrainzngs
from mutagen.flac import FLAC

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
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def __post_init__(self):
        self.label = os.path.basename(self.device)

    def update(self, **kwargs):
        with self.lock:
            for k, v in kwargs.items():
                setattr(self, k, v)

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
        from rich.live import Live
        from rich.table import Table
        from rich.text import Text
        from rich.console import Console

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

        live = Live(make_table(), console=console, refresh_per_second=2)
        live.start()
        _display_live = live

        # Background refresh
        def refresh_loop():
            while not _shutdown and _display_live:
                try:
                    _display_live.update(make_table())
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
    """Replace anything not [-a-zA-Z0-9_ .] with underscore, and truncate."""
    sanitized = re.sub(r"[^-\w .]", "_", name)
    if len(sanitized.encode("utf-8")) > max_length:
        truncated = sanitized.encode("utf-8")[:max_length].decode("utf-8", errors="ignore")
        sanitized = truncated.rstrip(" _-")
    return sanitized


def _device_label(device):
    """Short label for a device, e.g. /dev/sr0 -> sr0."""
    return os.path.basename(device)


def log(msg, logfile=None, device=None):
    """Print timestamped message and optionally append to logfile."""
    prefix = f"[{_device_label(device)}] " if device else ""
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {prefix}{msg}"
    with _log_lock:
        if _display_live:
            # Print below the live table
            _display_live.console.print(line, highlight=False)
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
    except discid.DiscError:
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

    tracks = []
    for medium in release.get("medium-list", []):
        for disc_entry in medium.get("disc-list", []):
            if disc_entry.get("id") == disc.id:
                tracks = _extract_tracks(medium, album_artist)
                break
        if tracks:
            break

    if not tracks:
        for medium in release.get("medium-list", []):
            tracks = _extract_tracks(medium, album_artist)
            break

    if not tracks:
        return None

    is_va = album_artist.lower() in ("various artists", "various")

    return {
        "artist": album_artist,
        "album": album,
        "date": date,
        "tracks": sorted(tracks, key=lambda t: t["number"]),
        "is_va": is_va,
        "disc_id": disc.id,
    }


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
                progress = min(current_size / expected_wav_size, 1.0) if expected_wav_size > 0 else 0.0
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
    if metadata.get("date"):
        audio["DATE"] = metadata["date"]
    audio.save()


def write_album_info(album_dir, metadata, failed_tracks=None):
    """Write album_info.txt with tag data and any failures."""
    info_path = album_dir / "album_info.txt"
    with open(info_path, "w") as f:
        f.write(f"ARTIST={metadata['artist']}\n")
        f.write(f"ALBUM={metadata['album']}\n")
        if metadata.get("date"):
            f.write(f"DATE={metadata['date']}\n")
        f.write(f"DISCID={metadata['disc_id']}\n")
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


def write_playlist(album_dir, metadata, failed_tracks=None):
    """Write .m3u playlist file (skipping failed tracks)."""
    artist_safe = sanitize_filename(metadata["artist"])
    album_safe = sanitize_filename(metadata["album"])
    m3u_path = album_dir / f"{artist_safe} - {album_safe}.m3u"
    with open(m3u_path, "w") as f:
        for track in metadata["tracks"]:
            if failed_tracks and track["number"] in failed_tracks:
                continue
            f.write(_track_filename(track, metadata) + "\n")


def _track_filename(track, metadata):
    """Build the FLAC filename for a track."""
    num = f"{track['number']:02d}"
    if metadata["is_va"]:
        return f"{num} - {sanitize_filename(track['artist'])} - {sanitize_filename(track['title'])}.flac"
    return f"{num} - {sanitize_filename(track['title'])}.flac"


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
    log(f"Album written to {album_dir}", logfile, device)

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

    log("Stopped.", logfile, device)


def main():
    parser = argparse.ArgumentParser(
        description="Rip audio CDs to FLAC with MusicBrainz metadata."
    )
    parser.add_argument(
        "-d", "--device",
        nargs="*",
        default=None,
        help="CD-ROM device(s), or 'all' to auto-detect (default: /dev/cdrom)",
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
    args = parser.parse_args()

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    check_dependencies()

    # Resolve device list
    if args.device is None:
        devices = ["/dev/cdrom"]
    elif len(args.device) == 1 and args.device[0] == "all":
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
