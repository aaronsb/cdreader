"""Unit tests for cdripper.

These cover the pure logic and filesystem output: name sanitizing, track
filename construction, MusicBrainz response parsing, album_info/m3u writing,
drive detection, and the TUI drive-state container.

Anything that needs a real optical drive, cdparanoia, or the network is out of
scope here -- those paths are exercised by `make check` and by ripping a disc.
"""

import re
import subprocess
from types import SimpleNamespace

import pytest

import cdripper

# --- sanitize_filename ---

class TestSanitizeFilename:
    def test_replaces_path_and_shell_metacharacters(self):
        assert cdripper.sanitize_filename("AC/DC: Back?") == "AC_DC_ Back_"

    def test_preserves_allowed_punctuation(self):
        # hyphen, underscore, space and dot are all legal in the charset
        assert cdripper.sanitize_filename("Sgt. Pepper - Deluxe_2") == "Sgt. Pepper - Deluxe_2"

    def test_preserves_unicode_word_characters(self):
        # \w is unicode-aware, so accented latin and CJK survive intact
        assert cdripper.sanitize_filename("Björk") == "Björk"
        assert cdripper.sanitize_filename("坂本龍一") == "坂本龍一"

    def test_truncates_on_byte_length_not_character_count(self):
        # 100 3-byte characters = 300 bytes, over the 200 byte default
        name = "あ" * 100
        result = cdripper.sanitize_filename(name)
        assert len(result.encode("utf-8")) <= 200

    def test_truncation_does_not_split_a_multibyte_character(self):
        # 200 is not a multiple of 3, so a naive slice would leave a partial
        # character; errors="ignore" must drop it cleanly.
        result = cdripper.sanitize_filename("あ" * 100)
        result.encode("utf-8").decode("utf-8")  # must not raise
        assert "�" not in result

    def test_strips_trailing_separators_left_by_truncation(self):
        # 201 bytes, so truncation fires and the rstrip runs
        result = cdripper.sanitize_filename("x" * 195 + " " * 6)
        assert result == "x" * 195

    def test_trailing_spaces_survive_when_under_the_length_limit(self):
        # Documents current behaviour: the rstrip only runs on the truncation
        # path, so short names keep trailing whitespace. Harmless on ext4 but
        # awkward on SMB/NTFS shares -- see the note in CONTRIBUTING.
        assert cdripper.sanitize_filename("Album  ") == "Album  "

    def test_respects_custom_max_length(self):
        assert cdripper.sanitize_filename("abcdefghij", max_length=4) == "abcd"

    def test_empty_string_is_returned_unchanged(self):
        assert cdripper.sanitize_filename("") == ""


# --- _device_label ---

def test_device_label_strips_dev_prefix():
    assert cdripper._device_label("/dev/sr0") == "sr0"
    assert cdripper._device_label("/dev/cdrom") == "cdrom"


# --- _track_filename ---

class TestTrackFilename:
    def test_single_artist_album_omits_artist(self):
        meta = {"is_va": False}
        track = {"number": 3, "title": "Bohemian Rhapsody", "artist": "Queen"}
        assert cdripper._track_filename(track, meta) == "03 - Bohemian Rhapsody.flac"

    def test_various_artists_album_includes_track_artist(self):
        meta = {"is_va": True}
        track = {"number": 3, "title": "Song", "artist": "Some Band"}
        assert cdripper._track_filename(track, meta) == "03 - Some Band - Song.flac"

    def test_track_number_is_zero_padded_to_two_digits(self):
        meta = {"is_va": False}
        assert cdripper._track_filename({"number": 1, "title": "A", "artist": "X"}, meta).startswith("01 - ")

    def test_track_number_beyond_99_is_not_truncated(self):
        meta = {"is_va": False}
        assert cdripper._track_filename({"number": 100, "title": "A", "artist": "X"}, meta).startswith("100 - ")

    def test_title_is_sanitized(self):
        meta = {"is_va": False}
        track = {"number": 1, "title": "What?/Why", "artist": "X"}
        assert cdripper._track_filename(track, meta) == "01 - What__Why.flac"


# --- _extract_tracks ---

class TestExtractTracks:
    def test_uses_per_track_artist_credit_when_present(self):
        medium = {"track-list": [
            {"number": "1", "recording": {
                "title": "Song A",
                "artist-credit": [{"artist": {"name": "Real Artist"}}],
            }},
        ]}
        tracks = cdripper._extract_tracks(medium, "Various Artists")
        assert tracks[0]["artist"] == "Real Artist"

    def test_joins_multi_part_artist_credits_with_separators(self):
        # MusicBrainz interleaves dicts and joinphrase strings
        medium = {"track-list": [
            {"number": "1", "recording": {
                "title": "Duet",
                "artist-credit": [
                    {"artist": {"name": "Alice"}},
                    " & ",
                    {"artist": {"name": "Bob"}},
                ],
            }},
        ]}
        tracks = cdripper._extract_tracks(medium, "Album Artist")
        assert tracks[0]["artist"] == "Alice & Bob"

    def test_falls_back_to_album_artist_when_credit_absent(self):
        medium = {"track-list": [
            {"number": "1", "recording": {"title": "Song"}},
        ]}
        tracks = cdripper._extract_tracks(medium, "Album Artist")
        assert tracks[0]["artist"] == "Album Artist"

    def test_falls_back_to_album_artist_when_credit_is_empty_strings(self):
        medium = {"track-list": [
            {"number": "1", "recording": {"title": "Song", "artist-credit": ["", "  "]}},
        ]}
        tracks = cdripper._extract_tracks(medium, "Album Artist")
        assert tracks[0]["artist"] == "Album Artist"

    def test_missing_recording_title_falls_back_to_track_number(self):
        medium = {"track-list": [{"number": "7", "recording": {}}]}
        tracks = cdripper._extract_tracks(medium, "A")
        assert tracks[0]["title"] == "Track 7"

    def test_empty_track_list_yields_no_tracks(self):
        assert cdripper._extract_tracks({"track-list": []}, "A") == []


# --- lookup_metadata ---

def _mb_response(disc_id="DISCID123", artist="Queen", title="A Night at the Opera"):
    """Build a minimal but realistic get_releases_by_discid payload."""
    return {"disc": {"release-list": [{
        "artist-credit-phrase": artist,
        "title": title,
        "date": "1975-11-21",
        "medium-list": [{
            "disc-list": [{"id": disc_id}],
            "track-list": [
                {"number": "2", "recording": {"title": "Lazing"}},
                {"number": "1", "recording": {"title": "Death on Two Legs"}},
            ],
        }],
    }]}}


class TestLookupMetadata:
    def test_parses_release_into_metadata_dict(self, monkeypatch):
        disc = SimpleNamespace(id="DISCID123")
        monkeypatch.setattr(cdripper.musicbrainzngs, "get_releases_by_discid",
                            lambda *a, **k: _mb_response())

        meta = cdripper.lookup_metadata(disc)

        assert meta["artist"] == "Queen"
        assert meta["album"] == "A Night at the Opera"
        assert meta["date"] == "1975-11-21"
        assert meta["disc_id"] == "DISCID123"
        assert meta["is_va"] is False

    def test_tracks_are_sorted_by_number(self, monkeypatch):
        disc = SimpleNamespace(id="DISCID123")
        monkeypatch.setattr(cdripper.musicbrainzngs, "get_releases_by_discid",
                            lambda *a, **k: _mb_response())

        meta = cdripper.lookup_metadata(disc)
        assert [t["number"] for t in meta["tracks"]] == [1, 2]

    @pytest.mark.parametrize("artist", ["Various Artists", "various artists", "Various"])
    def test_various_artists_is_detected_case_insensitively(self, monkeypatch, artist):
        disc = SimpleNamespace(id="DISCID123")
        monkeypatch.setattr(cdripper.musicbrainzngs, "get_releases_by_discid",
                            lambda *a, **k: _mb_response(artist=artist))

        assert cdripper.lookup_metadata(disc)["is_va"] is True

    def test_returns_none_when_disc_key_absent(self, monkeypatch):
        monkeypatch.setattr(cdripper.musicbrainzngs, "get_releases_by_discid",
                            lambda *a, **k: {})
        assert cdripper.lookup_metadata(SimpleNamespace(id="X")) is None

    def test_returns_none_when_release_list_empty(self, monkeypatch):
        monkeypatch.setattr(cdripper.musicbrainzngs, "get_releases_by_discid",
                            lambda *a, **k: {"disc": {"release-list": []}})
        assert cdripper.lookup_metadata(SimpleNamespace(id="X")) is None

    def test_retries_then_succeeds_on_transient_web_service_error(self, monkeypatch):
        monkeypatch.setattr(cdripper, "MB_RETRY_DELAY", 0)
        calls = []

        def flaky(*a, **k):
            calls.append(1)
            if len(calls) < 3:
                raise cdripper.musicbrainzngs.WebServiceError("boom")
            return _mb_response()

        monkeypatch.setattr(cdripper.musicbrainzngs, "get_releases_by_discid", flaky)

        meta = cdripper.lookup_metadata(SimpleNamespace(id="DISCID123"))
        assert len(calls) == 3
        assert meta["artist"] == "Queen"

    def test_returns_none_after_exhausting_retries(self, monkeypatch):
        monkeypatch.setattr(cdripper, "MB_RETRY_DELAY", 0)
        calls = []

        def always_fail(*a, **k):
            calls.append(1)
            raise cdripper.musicbrainzngs.WebServiceError("down")

        monkeypatch.setattr(cdripper.musicbrainzngs, "get_releases_by_discid", always_fail)

        assert cdripper.lookup_metadata(SimpleNamespace(id="X")) is None
        assert len(calls) == cdripper.MB_RETRIES


# --- album_info.txt ---

@pytest.fixture
def metadata():
    return {
        "artist": "Queen",
        "album": "A Night at the Opera",
        "date": "1975-11-21",
        "disc_id": "DISCID123",
        "is_va": False,
        "tracks": [
            {"number": 1, "title": "Death on Two Legs", "artist": "Queen"},
            {"number": 2, "title": "Lazing", "artist": "Queen"},
            {"number": 3, "title": "Seaside Rendezvous", "artist": "Queen"},
        ],
    }


def _parse_info(path):
    """Read album_info.txt into a dict of KEY -> value."""
    out = {}
    for line in path.read_text().splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            out[k] = v
    return out


class TestWriteAlbumInfo:
    def test_writes_album_level_fields(self, tmp_path, metadata):
        cdripper.write_album_info(tmp_path, metadata)
        info = _parse_info(tmp_path / "album_info.txt")

        assert info["ARTIST"] == "Queen"
        assert info["ALBUM"] == "A Night at the Opera"
        assert info["DATE"] == "1975-11-21"
        assert info["DISCID"] == "DISCID123"
        assert info["TRACKS"] == "3"

    def test_writes_one_zero_padded_line_per_track(self, tmp_path, metadata):
        cdripper.write_album_info(tmp_path, metadata)
        info = _parse_info(tmp_path / "album_info.txt")

        assert info["TRACK01"] == "Death on Two Legs"
        assert info["TRACK03"] == "Seaside Rendezvous"

    def test_omits_date_line_when_metadata_has_no_date(self, tmp_path, metadata):
        metadata["date"] = ""
        cdripper.write_album_info(tmp_path, metadata)
        assert "DATE" not in _parse_info(tmp_path / "album_info.txt")

    def test_various_artists_track_lines_include_artist(self, tmp_path, metadata):
        metadata["is_va"] = True
        metadata["tracks"][0]["artist"] = "Other Band"
        cdripper.write_album_info(tmp_path, metadata)

        assert _parse_info(tmp_path / "album_info.txt")["TRACK01"] == "Other Band - Death on Two Legs"

    def test_failed_tracks_are_summarised_and_marked_inline(self, tmp_path, metadata):
        cdripper.write_album_info(tmp_path, metadata, failed_tracks={3, 1})
        info = _parse_info(tmp_path / "album_info.txt")

        assert info["FAILED_TRACKS"] == "1,3"  # sorted
        assert info["TRACK01"].startswith("FAILED:")
        assert info["TRACK02"] == "Lazing"  # untouched

    def test_no_failed_tracks_key_when_all_succeed(self, tmp_path, metadata):
        cdripper.write_album_info(tmp_path, metadata)
        assert "FAILED_TRACKS" not in _parse_info(tmp_path / "album_info.txt")


# --- .m3u playlist ---

class TestWritePlaylist:
    def test_playlist_is_named_for_artist_and_album(self, tmp_path, metadata):
        cdripper.write_playlist(tmp_path, metadata)
        assert (tmp_path / "Queen - A Night at the Opera.m3u").exists()

    def test_playlist_lists_every_track_in_order(self, tmp_path, metadata):
        cdripper.write_playlist(tmp_path, metadata)
        lines = (tmp_path / "Queen - A Night at the Opera.m3u").read_text().splitlines()
        assert lines == [
            "01 - Death on Two Legs.flac",
            "02 - Lazing.flac",
            "03 - Seaside Rendezvous.flac",
        ]

    def test_failed_tracks_are_excluded(self, tmp_path, metadata):
        cdripper.write_playlist(tmp_path, metadata, failed_tracks={2})
        content = (tmp_path / "Queen - A Night at the Opera.m3u").read_text()

        assert "01 - Death on Two Legs.flac" in content
        assert "Lazing" not in content
        assert "03 - Seaside Rendezvous.flac" in content

    def test_playlist_filename_is_sanitized(self, tmp_path, metadata):
        metadata["artist"] = "AC/DC"
        cdripper.write_playlist(tmp_path, metadata)
        assert (tmp_path / "AC_DC - A Night at the Opera.m3u").exists()


# --- detect_drives ---

class TestDetectDrives:
    def test_returns_sorted_sr_devices(self, monkeypatch):
        monkeypatch.setattr(cdripper, "glob", lambda p: ["/dev/sr2", "/dev/sr0", "/dev/sr1"])
        assert cdripper.detect_drives() == ["/dev/sr0", "/dev/sr1", "/dev/sr2"]

    def test_falls_back_to_dev_cdrom_when_no_sr_devices(self, monkeypatch):
        # Patch narrowly: only /dev/cdrom is faked, everything else defers to
        # the real os.path.exists so unrelated code is unaffected.
        real_exists = cdripper.os.path.exists
        monkeypatch.setattr(cdripper, "glob", lambda p: [])
        monkeypatch.setattr(cdripper.os.path, "exists",
                            lambda p: True if p == "/dev/cdrom" else real_exists(p))
        assert cdripper.detect_drives() == ["/dev/cdrom"]

    def test_returns_empty_when_no_drives_exist(self, monkeypatch):
        real_exists = cdripper.os.path.exists
        monkeypatch.setattr(cdripper, "glob", lambda p: [])
        monkeypatch.setattr(cdripper.os.path, "exists",
                            lambda p: False if p == "/dev/cdrom" else real_exists(p))
        assert cdripper.detect_drives() == []


# --- log routing ---

@pytest.fixture
def drive_states(monkeypatch):
    """Install a clean two-drive registry for the duration of a test."""
    states = {d: cdripper.DriveState(device=d) for d in ("/dev/sr0", "/dev/sr1")}
    monkeypatch.setattr(cdripper, "_drive_states", states)
    return states


class TestLog:
    def test_prints_to_stdout_when_no_display_is_active(self, capsys):
        cdripper.log("plain message")
        assert "plain message" in capsys.readouterr().out

    def test_line_is_timestamped(self, capsys):
        cdripper.log("hello")
        # leading [YYYY-MM-DD HH:MM:SS]
        assert re.match(r"^\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\] hello",
                        capsys.readouterr().out.strip())

    def test_device_is_rendered_as_a_short_prefix(self, capsys):
        cdripper.log("detected", device="/dev/sr1")
        assert "[sr1] detected" in capsys.readouterr().out

    def test_appends_each_call_to_the_logfile(self, tmp_path):
        path = tmp_path / "cdripper.log"
        cdripper.log("first", logfile=str(path))
        cdripper.log("second", logfile=str(path))

        lines = path.read_text().splitlines()
        assert len(lines) == 2
        assert lines[0].endswith("first")
        assert lines[1].endswith("second")

    def test_routes_to_the_owning_drive_buffer_when_display_is_active(
            self, monkeypatch, drive_states, capsys):
        monkeypatch.setattr(cdripper, "_display_live", object())

        cdripper.log("ripping track", device="/dev/sr0")

        (stamp, msg), = drive_states["/dev/sr0"].get_logs()
        assert msg == "ripping track"
        assert re.fullmatch(r"\d{2}:\d{2}:\d{2}", stamp)
        assert drive_states["/dev/sr1"].get_logs() == []
        assert capsys.readouterr().out == ""  # TUI owns the screen

    def test_tui_entry_omits_the_device_prefix(self, monkeypatch, drive_states):
        # The panel is already titled with the drive, so repeating [sr0] on
        # every line just burns column width.
        monkeypatch.setattr(cdripper, "_display_live", object())

        cdripper.log("Disc detected", device="/dev/sr0")

        (_, msg), = drive_states["/dev/sr0"].get_logs()
        assert msg == "Disc detected"
        assert "sr0" not in msg

    def test_tui_stamp_is_time_only_but_logfile_keeps_the_date(
            self, monkeypatch, drive_states, tmp_path):
        monkeypatch.setattr(cdripper, "_display_live", object())
        path = tmp_path / "cdripper.log"

        cdripper.log("Disc detected", logfile=str(path), device="/dev/sr0")

        (stamp, _), = drive_states["/dev/sr0"].get_logs()
        assert re.fullmatch(r"\d{2}:\d{2}:\d{2}", stamp)   # TUI: time only
        assert re.match(r"^\[\d{4}-\d{2}-\d{2} ", path.read_text())  # file: full date
        assert "[sr0]" in path.read_text()  # file keeps the prefix

    def test_broadcasts_to_every_drive_when_no_device_is_given(
            self, monkeypatch, drive_states):
        monkeypatch.setattr(cdripper, "_display_live", object())

        cdripper.log("global notice")

        for state in drive_states.values():
            assert [msg for _, msg in state.get_logs()] == ["global notice"]

    def test_still_writes_the_logfile_while_the_display_is_active(
            self, monkeypatch, drive_states, tmp_path):
        monkeypatch.setattr(cdripper, "_display_live", object())
        path = tmp_path / "cdripper.log"

        cdripper.log("recorded", logfile=str(path), device="/dev/sr0")

        assert "recorded" in path.read_text()


# --- log rendering (issue #11) ---

class TestWrapLogEntries:
    def test_short_entry_is_one_row_carrying_its_stamp(self):
        rows = cdripper._wrap_log_entries([("09:01:02", "Disc detected")], width=40)
        assert rows == [("09:01:02", "Disc detected")]

    def test_long_entry_wraps_with_continuation_rows_having_no_stamp(self):
        entry = [("09:01:52", "Ripping Track 01/10: I'm Gonna Laugh You Right Out of My Life")]
        rows = cdripper._wrap_log_entries(entry, width=40)

        assert len(rows) > 1
        assert rows[0][0] == "09:01:52"
        assert all(stamp == "" for stamp, _ in rows[1:])

    def test_wrapped_text_fits_the_column_once_the_gutter_is_accounted_for(self):
        entry = [("09:01:52", "Ripping Track 01/10: I'm Gonna Laugh You Right Out of My Life")]
        width = 40
        rows = cdripper._wrap_log_entries(entry, width=width)

        # every rendered row is "HH:MM:SS " + body, so body must fit what's left
        for _, body in rows:
            assert len(body) <= width - cdripper.LOG_STAMP_WIDTH - 1

    def test_no_text_is_lost_when_wrapping(self):
        msg = "Ripping Track 01/10: I'm Gonna Laugh You Right Out of My Life"
        rows = cdripper._wrap_log_entries([("09:01:52", msg)], width=40)

        assert " ".join(body for _, body in rows) == msg

    def test_a_token_longer_than_the_column_is_broken_rather_than_overflowing(self):
        # disc IDs are ~28 unbroken chars and used to blow out narrow columns
        disc_id = "6xBbWPviuVg90s7R07rUU6WfdMo-"
        rows = cdripper._wrap_log_entries([("09:01:51", disc_id)], width=24)

        for _, body in rows:
            assert len(body) <= 24 - cdripper.LOG_STAMP_WIDTH - 1

    def test_entries_are_flattened_in_order(self):
        rows = cdripper._wrap_log_entries(
            [("09:00:01", "first"), ("09:00:02", "second")], width=40)
        assert rows == [("09:00:01", "first"), ("09:00:02", "second")]

    def test_empty_message_still_produces_a_row(self):
        assert cdripper._wrap_log_entries([("09:00:01", "")], width=40) == [("09:00:01", "")]

    def test_absurdly_narrow_column_does_not_hang_or_crash(self):
        rows = cdripper._wrap_log_entries([("09:00:01", "some message here")], width=2)
        assert rows  # floor on available width keeps textwrap sane

    def test_no_entries_yields_no_rows(self):
        assert cdripper._wrap_log_entries([], width=40) == []


class TestLogStyle:
    @pytest.mark.parametrize("msg", [
        "  ERROR on Track 03/10: rip failed, retrying...",
        "  FAILED Track 03/10 after 3 attempts",
        "No MusicBrainz match. Using disc ID for folder name.",
    ])
    def test_problems_are_red(self, msg):
        assert cdripper._log_style(msg) == "red"

    @pytest.mark.parametrize("msg", [
        "  Track 01/10: Trust in Me done (0m39s)",
        "Rip complete. Ejecting.",
        "Found: Holly Cole Trio - Blame It on My Youth",
    ])
    def test_good_news_is_green(self, msg):
        assert cdripper._log_style(msg) == "green"

    @pytest.mark.parametrize("msg", [
        "  Ripping Track 01/10: Trust in Me",
        "Disc detected",
        "Looking up metadata on MusicBrainz...",
    ])
    def test_ordinary_progress_is_unstyled(self, msg):
        assert cdripper._log_style(msg) == ""

    @pytest.mark.parametrize("title", [
        "Done Deal",
        "What's Done Is Done",
        "Error of My Ways",
        "Complete Control",
        "Failed by Design",
    ])
    def test_status_words_inside_a_track_title_do_not_leak_into_the_style(self, title):
        # Track titles are user data. A rip that is only *starting* must not
        # render as finished (green) or failed (red) because of its name.
        assert cdripper._log_style(f"  Ripping Track 07/10: {title}") == ""

    def test_a_completion_is_still_green_when_the_title_contains_a_status_word(self):
        assert cdripper._log_style("  Track 07/10: Done Deal done (0m41s)") == "green"

    def test_a_failure_is_still_red_when_the_title_contains_a_status_word(self):
        assert cdripper._log_style(
            "  FAILED Track 07/10: Done Deal after 3 attempts: boom") == "red"


# --- check_dependencies ---

class TestCheckDependencies:
    def test_passes_when_all_binaries_present(self, monkeypatch):
        monkeypatch.setattr(cdripper.shutil, "which", lambda c: f"/usr/bin/{c}")
        cdripper.check_dependencies()  # must not raise

    def test_exits_nonzero_when_a_binary_is_missing(self, monkeypatch, capsys):
        monkeypatch.setattr(cdripper.shutil, "which",
                            lambda c: None if c == "flac" else f"/usr/bin/{c}")

        with pytest.raises(SystemExit) as exc:
            cdripper.check_dependencies()

        assert exc.value.code == 1
        assert "flac" in capsys.readouterr().err


# --- DriveState ---

class TestDriveState:
    def test_label_is_derived_from_device_path(self):
        assert cdripper.DriveState(device="/dev/sr1").label == "sr1"

    def test_update_sets_named_attributes(self):
        ds = cdripper.DriveState(device="/dev/sr0")
        ds.update(status="Ripping", track_num=4)

        assert ds.status == "Ripping"
        assert ds.track_num == 4

    def test_snapshot_returns_a_detached_copy(self):
        ds = cdripper.DriveState(device="/dev/sr0")
        ds.update(status="Ripping")
        snap = ds.snapshot()
        ds.update(status="Encoding")

        assert snap["status"] == "Ripping"  # snapshot not mutated by later update

    def test_log_buffer_is_bounded_and_keeps_newest(self):
        ds = cdripper.DriveState(device="/dev/sr0")
        for i in range(cdripper.LOG_BUFFER_LINES + 25):
            ds.add_log("09:01:02", f"line {i}")

        logs = ds.get_logs()
        assert len(logs) == cdripper.LOG_BUFFER_LINES
        assert logs[-1] == ("09:01:02", f"line {cdripper.LOG_BUFFER_LINES + 24}")

    def test_entries_are_stored_as_stamp_message_pairs(self):
        ds = cdripper.DriveState(device="/dev/sr0")
        ds.add_log("09:01:02", "Disc detected")

        assert ds.get_logs() == [("09:01:02", "Disc detected")]

    def test_get_logs_returns_a_copy_not_the_live_buffer(self):
        ds = cdripper.DriveState(device="/dev/sr0")
        ds.add_log("09:01:02", "first")
        logs = ds.get_logs()
        ds.add_log("09:01:03", "second")

        assert logs == [("09:01:02", "first")]

    def test_each_instance_has_its_own_log_buffer(self):
        a = cdripper.DriveState(device="/dev/sr0")
        b = cdripper.DriveState(device="/dev/sr1")
        a.add_log("09:01:02", "only in a")

        assert b.get_logs() == []


# --- tag_flac (uses the real flac encoder) ---

@pytest.fixture
def flac_file(tmp_path):
    """Encode a tiny silent FLAC so tagging can be tested for real."""
    if not cdripper.shutil.which("flac"):
        pytest.skip("flac binary not installed")

    wav = tmp_path / "silence.wav"
    # 0.1s of 44.1kHz 16-bit stereo silence, written as a raw WAV
    import struct
    frames = int(44100 * 0.1)
    data = b"\x00" * (frames * 4)
    header = (b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVEfmt " +
              struct.pack("<IHHIIHH", 16, 1, 2, 44100, 44100 * 4, 4, 16) +
              b"data" + struct.pack("<I", len(data)))
    wav.write_bytes(header + data)

    out = tmp_path / "track.flac"
    subprocess.run(["flac", "-s", "-o", str(out), str(wav)], check=True)
    return out


class TestTagFlac:
    def test_writes_expected_vorbis_comments(self, flac_file, metadata):
        cdripper.tag_flac(flac_file, metadata, metadata["tracks"][1])

        from mutagen.flac import FLAC
        tags = FLAC(str(flac_file))

        assert tags["TITLE"] == ["Lazing"]
        assert tags["ARTIST"] == ["Queen"]
        assert tags["ALBUM"] == ["A Night at the Opera"]
        assert tags["ALBUMARTIST"] == ["Queen"]
        assert tags["TRACKNUMBER"] == ["2"]
        assert tags["TRACKTOTAL"] == ["3"]
        assert tags["DISCID"] == ["DISCID123"]
        assert tags["DATE"] == ["1975-11-21"]

    def test_omits_date_tag_when_metadata_has_no_date(self, flac_file, metadata):
        metadata["date"] = ""
        cdripper.tag_flac(flac_file, metadata, metadata["tracks"][0])

        from mutagen.flac import FLAC
        assert "DATE" not in FLAC(str(flac_file))
