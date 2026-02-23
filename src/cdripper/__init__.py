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
import subprocess
import sys
import tempfile
import time
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

# Desktop notification support (GNOME, KDE, etc. via freedesktop)
_has_notify = shutil.which("notify-send") is not None


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


def _handle_signal(signum, frame):
    global _shutdown
    _shutdown = True
    print("\nShutting down after current operation...")


def sanitize_filename(name):
    """Replace anything not [-a-zA-Z0-9_ .] with underscore."""
    return re.sub(r"[^-\w .]", "_", name)


def log(msg, logfile=None):
    """Print timestamped message and optionally append to logfile."""
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
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


def read_disc(device):
    """Try to read the disc TOC. Returns discid.Disc or None."""
    try:
        return discid.read(device)
    except discid.DiscError:
        return None


def lookup_metadata(disc):
    """Query MusicBrainz for disc metadata. Returns dict or None."""
    try:
        result = musicbrainzngs.get_releases_by_discid(
            disc.id, includes=["artists", "recordings", "artist-credits"]
        )
    except musicbrainzngs.WebServiceError:
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

    # Find the medium that matches our disc
    tracks = []
    for medium in release.get("medium-list", []):
        for disc_entry in medium.get("disc-list", []):
            if disc_entry.get("id") == disc.id:
                tracks = _extract_tracks(medium, album_artist)
                break
        if tracks:
            break

    # Fallback: if we didn't match a medium, use the first one
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

        # Per-track artist: check recording's artist-credit first
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


def rip_and_encode(device, track_num, output_path):
    """Rip a single track with cdparanoia and encode to FLAC."""
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        wav_path = tmp.name

    try:
        subprocess.run(
            ["cdparanoia", "-d", device, str(track_num), wav_path],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["flac", "-s", "-8", "-o", str(output_path), wav_path],
            check=True,
            capture_output=True,
        )
    finally:
        if os.path.exists(wav_path):
            os.unlink(wav_path)


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


def write_album_info(album_dir, metadata):
    """Write album_info.txt with tag data."""
    info_path = album_dir / "album_info.txt"
    with open(info_path, "w") as f:
        f.write(f"ARTIST={metadata['artist']}\n")
        f.write(f"ALBUM={metadata['album']}\n")
        if metadata.get("date"):
            f.write(f"DATE={metadata['date']}\n")
        f.write(f"DISCID={metadata['disc_id']}\n")
        f.write(f"TRACKS={len(metadata['tracks'])}\n")
        for track in metadata["tracks"]:
            if metadata["is_va"]:
                f.write(f"TRACK{track['number']:02d}={track['artist']} - {track['title']}\n")
            else:
                f.write(f"TRACK{track['number']:02d}={track['title']}\n")


def write_playlist(album_dir, metadata):
    """Write .m3u playlist file."""
    artist_safe = sanitize_filename(metadata["artist"])
    album_safe = sanitize_filename(metadata["album"])
    m3u_path = album_dir / f"{artist_safe} - {album_safe}.m3u"
    with open(m3u_path, "w") as f:
        for track in metadata["tracks"]:
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


def rip_disc(disc, device, output_dir, logfile):
    """Full rip pipeline for one disc."""
    log(f"Disc ID: {disc.id}, {disc.last_track_num} tracks", logfile)

    log("Looking up metadata on MusicBrainz...", logfile)
    metadata = lookup_metadata(disc)

    if metadata is None:
        log("No MusicBrainz match. Using disc ID for folder name.", logfile)
        notify("Unknown disc", f"No MusicBrainz match\nDisc ID: {disc.id}")
        metadata = {
            "artist": "Unknown Artist",
            "album": disc.id,
            "date": "",
            "tracks": [
                {"number": i, "title": f"Track {i:02d}", "artist": "Unknown Artist"}
                for i in range(1, disc.last_track_num + 1)
            ],
            "is_va": False,
            "disc_id": disc.id,
        }
    else:
        log(f"Found: {metadata['artist']} - {metadata['album']}", logfile)
        notify("Ripping CD", f"{metadata['artist']} \u2014 {metadata['album']}\n{len(metadata['tracks'])} tracks")

    # Create output directory
    artist_dir = sanitize_filename(metadata["artist"])
    album_dir_name = sanitize_filename(metadata["album"])
    album_dir = Path(output_dir) / artist_dir / album_dir_name
    album_dir.mkdir(parents=True, exist_ok=True)

    # Rip each track
    for track in metadata["tracks"]:
        if _shutdown:
            log("Shutdown requested, stopping after current track.", logfile)
            return False

        fname = _track_filename(track, metadata)
        flac_path = album_dir / fname
        log(f"  Ripping track {track['number']:02d}: {track['title']}", logfile)

        try:
            rip_and_encode(device, track["number"], flac_path)
            tag_flac(flac_path, metadata, track)
        except subprocess.CalledProcessError as e:
            log(f"  ERROR ripping track {track['number']:02d}: {e}", logfile)
            continue

    # Count how many tracks actually ripped successfully
    ripped = len(list(album_dir.glob("*.flac")))
    total = len(metadata["tracks"])

    write_album_info(album_dir, metadata)
    write_playlist(album_dir, metadata)
    log(f"Album written to {album_dir}", logfile)

    if ripped == total:
        notify("Rip complete", f"{metadata['artist']} \u2014 {metadata['album']}\n{ripped} tracks")
    elif ripped > 0:
        notify("Rip finished with errors",
               f"{metadata['artist']} \u2014 {metadata['album']}\n{ripped}/{total} tracks", "critical")
    else:
        notify("Rip failed", f"{metadata['artist']} \u2014 {metadata['album']}", "critical")

    return ripped > 0


def poll_and_rip(device, output_dir, poll_interval=2):
    """Main loop: poll for disc, rip, eject, repeat."""
    log_dir = Path(output_dir) / "_logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    logfile = str(log_dir / "cdripper.log")

    log(f"cdripper {VERSION} started", logfile)
    log(f"Watching {device}, output to {output_dir}", logfile)
    log("Insert a disc to begin.", logfile)

    while not _shutdown:
        disc = read_disc(device)
        if disc is not None:
            log(f"Disc detected on {device}", logfile)
            success = rip_disc(disc, device, output_dir, logfile)
            if success:
                log("Rip complete. Ejecting.", logfile)
            else:
                log("Rip failed or interrupted. Ejecting.", logfile)
            eject_disc(device)
            log("Ready for next disc.", logfile)
            # Brief pause after eject before polling again
            time.sleep(5)
        else:
            time.sleep(poll_interval)

    log("Stopped.", logfile)


def main():
    parser = argparse.ArgumentParser(
        description="Rip audio CDs to FLAC with MusicBrainz metadata."
    )
    parser.add_argument(
        "-d", "--device",
        default="/dev/cdrom",
        help="CD-ROM device (default: /dev/cdrom)",
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

    if args.once:
        log_dir = Path(args.output) / "_logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        logfile = str(log_dir / "cdripper.log")
        disc = read_disc(args.device)
        if disc is None:
            log("No disc found.", logfile)
            sys.exit(1)
        success = rip_disc(disc, args.device, args.output, logfile)
        eject_disc(args.device)
        sys.exit(0 if success else 1)
    else:
        poll_and_rip(args.device, args.output)


if __name__ == "__main__":
    main()
