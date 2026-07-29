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
            "position": "1",
            "disc-list": [{"id": disc_id}],
            "track-list": [
                {"number": "2", "recording": {"title": "Lazing"}},
                {"number": "1", "recording": {"title": "Death on Two Legs"}},
            ],
        }],
    }]}}


def _mb_multidisc(matching_disc_id):
    """Two-CD release modelled on the one in issue #12 (Ani DiFranco, Rome Italy).

    Both discs have a track 1, which is what collided in a shared directory.
    """
    return {"disc": {"release-list": [{
        "artist-credit-phrase": "Ani DiFranco",
        "title": "Rome, Italy 11.15.04",
        "date": "2004-11-15",
        "medium-list": [
            {
                "position": "1",
                "disc-list": [{"id": "DISC-ONE"}],
                "track-list": [
                    {"number": "1", "recording": {"title": "Swan Dive"}},
                    {"number": "2", "recording": {"title": "Educated Guess"}},
                ],
            },
            {
                "position": "2",
                "disc-list": [{"id": "DISC-TWO"}],
                "track-list": [
                    {"number": "1", "recording": {"title": "Nicotine"}},
                    {"number": "2", "recording": {"title": "Bubble"}},
                ],
            },
        ],
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


# --- multi-disc handling (issue #12) ---

class TestMultiDiscLookup:
    @pytest.mark.parametrize("disc_id,position,first_title", [
        ("DISC-ONE", 1, "Swan Dive"),
        ("DISC-TWO", 2, "Nicotine"),
    ])
    def test_identifies_which_medium_the_inserted_disc_is(
            self, monkeypatch, disc_id, position, first_title):
        monkeypatch.setattr(cdripper.musicbrainzngs, "get_releases_by_discid",
                            lambda *a, **k: _mb_multidisc(disc_id))

        meta = cdripper.lookup_metadata(SimpleNamespace(id=disc_id))

        assert meta["disc_number"] == position
        assert meta["disc_total"] == 2
        assert meta["tracks"][0]["title"] == first_title

    def test_single_disc_release_reports_one_of_one(self, monkeypatch):
        monkeypatch.setattr(cdripper.musicbrainzngs, "get_releases_by_discid",
                            lambda *a, **k: _mb_response())

        meta = cdripper.lookup_metadata(SimpleNamespace(id="DISCID123"))
        assert (meta["disc_number"], meta["disc_total"]) == (1, 1)

    def test_disc_subtitle_is_captured_when_the_medium_has_one(self, monkeypatch):
        payload = _mb_multidisc("DISC-ONE")
        payload["disc"]["release-list"][0]["medium-list"][0]["title"] = "The Acoustic Set"
        monkeypatch.setattr(cdripper.musicbrainzngs, "get_releases_by_discid",
                            lambda *a, **k: payload)

        meta = cdripper.lookup_metadata(SimpleNamespace(id="DISC-ONE"))
        assert meta["disc_subtitle"] == "The Acoustic Set"

    def test_missing_position_falls_back_to_list_order(self, monkeypatch):
        payload = _mb_multidisc("DISC-TWO")
        for medium in payload["disc"]["release-list"][0]["medium-list"]:
            medium.pop("position")
        monkeypatch.setattr(cdripper.musicbrainzngs, "get_releases_by_discid",
                            lambda *a, **k: payload)

        assert cdripper.lookup_metadata(SimpleNamespace(id="DISC-TWO"))["disc_number"] == 2

    def test_unparseable_position_falls_back_to_list_order(self, monkeypatch):
        payload = _mb_multidisc("DISC-TWO")
        for medium in payload["disc"]["release-list"][0]["medium-list"]:
            medium["position"] = "side B"
        monkeypatch.setattr(cdripper.musicbrainzngs, "get_releases_by_discid",
                            lambda *a, **k: payload)

        assert cdripper.lookup_metadata(SimpleNamespace(id="DISC-TWO"))["disc_number"] == 2

    def test_unknown_disc_id_falls_back_to_the_first_medium(self, monkeypatch):
        monkeypatch.setattr(cdripper.musicbrainzngs, "get_releases_by_discid",
                            lambda *a, **k: _mb_multidisc("DISC-ONE"))

        meta = cdripper.lookup_metadata(SimpleNamespace(id="NOT-IN-RELEASE"))
        assert meta["disc_number"] == 1

    def test_the_guess_is_reported_rather_than_asserted_silently(
            self, monkeypatch, capsys):
        # Falling back means the disc number is a guess; on a multi-disc
        # release that would otherwise be written into tags as if it were fact.
        monkeypatch.setattr(cdripper.musicbrainzngs, "get_releases_by_discid",
                            lambda *a, **k: _mb_multidisc("DISC-ONE"))

        cdripper.lookup_metadata(SimpleNamespace(id="NOT-IN-RELEASE"))

        out = capsys.readouterr().out
        assert "Disc ID not listed" in out and "2 discs" in out

    def test_no_warning_when_the_disc_id_matched(self, monkeypatch, capsys):
        monkeypatch.setattr(cdripper.musicbrainzngs, "get_releases_by_discid",
                            lambda *a, **k: _mb_multidisc("DISC-TWO"))

        cdripper.lookup_metadata(SimpleNamespace(id="DISC-TWO"))
        assert "Disc ID not listed" not in capsys.readouterr().out

    def test_no_warning_on_an_ordinary_single_disc_release(self, monkeypatch, capsys):
        monkeypatch.setattr(cdripper.musicbrainzngs, "get_releases_by_discid",
                            lambda *a, **k: _mb_response())

        cdripper.lookup_metadata(SimpleNamespace(id="SOMETHING-ELSE"))
        assert "Disc ID not listed" not in capsys.readouterr().out

    def test_matched_medium_with_no_track_list_is_treated_as_no_match(
            self, monkeypatch):
        # Rather than fall through to another disc's titles, which would label
        # this disc with the wrong track names.
        payload = _mb_multidisc("DISC-ONE")
        payload["disc"]["release-list"][0]["medium-list"][0]["track-list"] = []
        monkeypatch.setattr(cdripper.musicbrainzngs, "get_releases_by_discid",
                            lambda *a, **k: payload)

        assert cdripper.lookup_metadata(SimpleNamespace(id="DISC-ONE")) is None


class TestMultiDiscFilenames:
    def test_track_numbers_are_disc_prefixed_on_multi_disc_releases(self):
        meta = {"is_va": False, "disc_number": 2, "disc_total": 2}
        track = {"number": 1, "title": "Nicotine", "artist": "Ani DiFranco"}
        assert cdripper._track_filename(track, meta) == "2-01 - Nicotine.flac"

    def test_single_disc_filenames_are_unchanged(self):
        meta = {"is_va": False, "disc_number": 1, "disc_total": 1}
        track = {"number": 1, "title": "Swan Dive", "artist": "Ani DiFranco"}
        assert cdripper._track_filename(track, meta) == "01 - Swan Dive.flac"

    def test_metadata_without_disc_keys_behaves_as_single_disc(self):
        # older callers, and the no-MusicBrainz-match fallback
        assert cdripper._track_filename(
            {"number": 3, "title": "X", "artist": "Y"}, {"is_va": False}) == "03 - X.flac"

    def test_the_two_colliding_tracks_from_issue_12_no_longer_collide(self):
        disc1 = {"is_va": False, "disc_number": 1, "disc_total": 2}
        disc2 = {"is_va": False, "disc_number": 2, "disc_total": 2}

        a = cdripper._track_filename({"number": 1, "title": "Swan Dive", "artist": "A"}, disc1)
        b = cdripper._track_filename({"number": 1, "title": "Nicotine", "artist": "A"}, disc2)

        assert a != b
        assert a.startswith("1-01") and b.startswith("2-01")

    def test_various_artists_multi_disc_keeps_both_prefixes(self):
        meta = {"is_va": True, "disc_number": 2, "disc_total": 2}
        track = {"number": 5, "title": "Song", "artist": "Some Band"}
        assert cdripper._track_filename(track, meta) == "2-05 - Some Band - Song.flac"


class TestPlaylistSortKey:
    def test_orders_by_disc_then_track(self):
        names = ["2-01 - B.flac", "1-02 - A.flac", "1-01 - C.flac"]
        assert sorted(names, key=cdripper._playlist_sort_key) == [
            "1-01 - C.flac", "1-02 - A.flac", "2-01 - B.flac"]

    def test_disc_10_sorts_after_disc_2_not_lexically(self):
        names = ["10-01 - X.flac", "2-01 - Y.flac"]
        assert sorted(names, key=cdripper._playlist_sort_key) == [
            "2-01 - Y.flac", "10-01 - X.flac"]

    def test_single_disc_names_still_order_by_track(self):
        names = ["10 - J.flac", "02 - B.flac", "01 - A.flac"]
        assert sorted(names, key=cdripper._playlist_sort_key) == [
            "01 - A.flac", "02 - B.flac", "10 - J.flac"]


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


# --- multi-disc rips sharing one directory (issue #12) ---

def _disc_metadata(number, total, tracks):
    return {
        "artist": "Ani DiFranco",
        "album": "Rome, Italy 11.15.04",
        "date": "2004-11-15",
        "disc_id": f"DISC-{number}",
        "is_va": False,
        "disc_number": number,
        "disc_total": total,
        "disc_subtitle": "",
        "tracks": [{"number": n, "title": t, "artist": "Ani DiFranco"}
                   for n, t in tracks],
    }


DISC1 = [(1, "Swan Dive"), (2, "Educated Guess")]
DISC2 = [(1, "Nicotine"), (2, "Bubble")]

# The comma in "Rome, Italy" is not in the allowed charset, so it sanitizes
# to an underscore -- this is the name write_playlist actually produces.
M3U_NAME = "Ani DiFranco - Rome_ Italy 11.15.04.m3u"


class TestMultiDiscSharedDirectory:
    def test_each_disc_gets_its_own_info_file(self, tmp_path):
        cdripper.write_album_info(tmp_path, _disc_metadata(1, 2, DISC1))
        cdripper.write_album_info(tmp_path, _disc_metadata(2, 2, DISC2))

        assert (tmp_path / "album_info_disc1.txt").exists()
        assert (tmp_path / "album_info_disc2.txt").exists()
        assert not (tmp_path / "album_info.txt").exists()

    def test_disc_one_info_survives_ripping_disc_two(self, tmp_path):
        cdripper.write_album_info(tmp_path, _disc_metadata(1, 2, DISC1))
        cdripper.write_album_info(tmp_path, _disc_metadata(2, 2, DISC2))

        info1 = _parse_info(tmp_path / "album_info_disc1.txt")
        assert info1["TRACK01"] == "Swan Dive"
        assert info1["DISCID"] == "DISC-1"
        assert info1["DISCNUMBER"] == "1"
        assert info1["DISCTOTAL"] == "2"

    def test_single_disc_release_keeps_the_plain_filename(self, tmp_path):
        cdripper.write_album_info(tmp_path, _disc_metadata(1, 1, DISC1))

        assert (tmp_path / "album_info.txt").exists()
        info = _parse_info(tmp_path / "album_info.txt")
        assert "DISCNUMBER" not in info  # not noise on ordinary albums

    def test_disc_subtitle_is_recorded_when_present(self, tmp_path):
        meta = _disc_metadata(1, 2, DISC1)
        meta["disc_subtitle"] = "The Acoustic Set"
        cdripper.write_album_info(tmp_path, meta)

        assert _parse_info(tmp_path / "album_info_disc1.txt")["DISCSUBTITLE"] \
            == "The Acoustic Set"

    def test_playlist_accumulates_both_discs_rather_than_being_replaced(self, tmp_path):
        cdripper.write_playlist(tmp_path, _disc_metadata(1, 2, DISC1))
        cdripper.write_playlist(tmp_path, _disc_metadata(2, 2, DISC2))

        lines = (tmp_path / M3U_NAME).read_text().splitlines()
        assert lines == [
            "1-01 - Swan Dive.flac",
            "1-02 - Educated Guess.flac",
            "2-01 - Nicotine.flac",
            "2-02 - Bubble.flac",
        ]

    def test_playlist_merge_is_order_independent(self, tmp_path):
        # discs can finish in either order across parallel drives
        cdripper.write_playlist(tmp_path, _disc_metadata(2, 2, DISC2))
        cdripper.write_playlist(tmp_path, _disc_metadata(1, 2, DISC1))

        lines = (tmp_path / M3U_NAME).read_text().splitlines()
        assert lines[0] == "1-01 - Swan Dive.flac"
        assert lines[-1] == "2-02 - Bubble.flac"

    def test_re_ripping_the_same_disc_does_not_duplicate_entries(self, tmp_path):
        cdripper.write_playlist(tmp_path, _disc_metadata(1, 2, DISC1))
        cdripper.write_playlist(tmp_path, _disc_metadata(1, 2, DISC1))

        lines = (tmp_path / M3U_NAME).read_text().splitlines()
        assert lines == ["1-01 - Swan Dive.flac", "1-02 - Educated Guess.flac"]

    def test_failed_tracks_are_still_excluded_when_merging(self, tmp_path):
        cdripper.write_playlist(tmp_path, _disc_metadata(1, 2, DISC1))
        cdripper.write_playlist(tmp_path, _disc_metadata(2, 2, DISC2), failed_tracks={1})

        content = (tmp_path / M3U_NAME).read_text()
        assert "Nicotine" not in content
        assert "2-02 - Bubble.flac" in content
        assert "1-01 - Swan Dive.flac" in content  # disc 1 untouched

    def test_re_rip_drops_an_entry_whose_track_now_fails(self, tmp_path):
        # rip_disc deletes the FLAC of a track that fails, so leaving its line
        # in the playlist would point at a file that is no longer there.
        cdripper.write_playlist(tmp_path, _disc_metadata(1, 2, DISC1))
        cdripper.write_playlist(tmp_path, _disc_metadata(2, 2, DISC2))
        cdripper.write_playlist(tmp_path, _disc_metadata(1, 2, DISC1), failed_tracks={1})

        lines = (tmp_path / M3U_NAME).read_text().splitlines()
        assert "1-01 - Swan Dive.flac" not in lines   # failed, file deleted
        assert "1-02 - Educated Guess.flac" in lines  # still fine
        assert "2-01 - Nicotine.flac" in lines        # other disc untouched
        assert "2-02 - Bubble.flac" in lines

    def test_re_rip_of_one_disc_leaves_the_other_disc_alone(self, tmp_path):
        cdripper.write_playlist(tmp_path, _disc_metadata(1, 2, DISC1))
        cdripper.write_playlist(tmp_path, _disc_metadata(2, 2, DISC2))
        before = (tmp_path / M3U_NAME).read_text().splitlines()

        cdripper.write_playlist(tmp_path, _disc_metadata(1, 2, DISC1))

        assert (tmp_path / M3U_NAME).read_text().splitlines() == before

    def test_single_disc_playlist_is_replaced_not_merged(self, tmp_path, metadata):
        # a re-rip of a normal album should not accumulate stale entries
        cdripper.write_playlist(tmp_path, metadata)
        metadata["tracks"] = metadata["tracks"][:1]
        cdripper.write_playlist(tmp_path, metadata)

        lines = (tmp_path / "Queen - A Night at the Opera.m3u").read_text().splitlines()
        assert lines == ["01 - Death on Two Legs.flac"]


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

    def test_writes_disc_tags_so_players_group_multi_disc_releases(self, flac_file):
        meta = _disc_metadata(2, 2, DISC2)
        cdripper.tag_flac(flac_file, meta, meta["tracks"][0])

        from mutagen.flac import FLAC
        tags = FLAC(str(flac_file))
        assert tags["DISCNUMBER"] == ["2"]
        assert tags["DISCTOTAL"] == ["2"]

    def test_single_disc_albums_are_tagged_one_of_one(self, flac_file, metadata):
        cdripper.tag_flac(flac_file, metadata, metadata["tracks"][0])

        from mutagen.flac import FLAC
        tags = FLAC(str(flac_file))
        assert tags["DISCNUMBER"] == ["1"]
        assert tags["DISCTOTAL"] == ["1"]

    def test_disc_subtitle_is_tagged_only_when_present(self, flac_file):
        from mutagen.flac import FLAC

        meta = _disc_metadata(1, 2, DISC1)
        cdripper.tag_flac(flac_file, meta, meta["tracks"][0])
        assert "DISCSUBTITLE" not in FLAC(str(flac_file))

        meta["disc_subtitle"] = "The Acoustic Set"
        cdripper.tag_flac(flac_file, meta, meta["tracks"][0])
        assert FLAC(str(flac_file))["DISCSUBTITLE"] == ["The Acoustic Set"]


# --- metadata.toml round trip (issue #13) ---

def _sidecar_metadata(**overrides):
    meta = {
        "artist": "The Weather Station",
        "album": "Live at Massey Hall",
        "date": "2024-06-01",
        "is_va": False,
        "disc_id": "xK9fL2mQpR7-",
        "disc_number": 1,
        "disc_total": 1,
        "disc_subtitle": "",
        "tracks": [
            {"number": 1, "title": "Robber", "artist": "The Weather Station"},
            {"number": 2, "title": "Atlantic", "artist": "The Weather Station"},
        ],
    }
    meta.update(overrides)
    return meta


class TestTomlString:
    def test_quotes_are_escaped(self):
        assert cdripper._toml_str('say "hi"') == '"say \\"hi\\""'

    def test_backslashes_are_escaped(self):
        assert cdripper._toml_str("a\\b") == '"a\\\\b"'

    def test_newlines_and_tabs_are_escaped(self):
        assert cdripper._toml_str("a\nb\tc") == '"a\\nb\\tc"'

    def test_none_becomes_an_empty_string(self):
        assert cdripper._toml_str(None) == '""'


class TestWriteMetadataToml:
    def test_writes_the_sidecar_and_returns_its_path(self, tmp_path):
        path = cdripper.write_metadata_toml(tmp_path, _sidecar_metadata())

        assert path == tmp_path / cdripper.METADATA_FILE
        assert path.exists()

    def test_output_is_parseable_toml(self, tmp_path):
        cdripper.write_metadata_toml(tmp_path, _sidecar_metadata())
        # must not raise
        cdripper.read_metadata_toml(tmp_path / cdripper.METADATA_FILE)

    def test_known_values_are_prefilled(self, tmp_path):
        cdripper.write_metadata_toml(tmp_path, _sidecar_metadata())
        parsed = cdripper.read_metadata_toml(tmp_path / cdripper.METADATA_FILE)

        assert parsed["artist"] == "The Weather Station"
        assert parsed["album"] == "Live at Massey Hall"
        assert parsed["date"] == "2024-06-01"
        assert [t["title"] for t in parsed["tracks"]] == ["Robber", "Atlantic"]

    def test_extra_fields_are_present_and_blank_by_default(self, tmp_path):
        cdripper.write_metadata_toml(tmp_path, _sidecar_metadata())
        parsed = cdripper.read_metadata_toml(tmp_path / cdripper.METADATA_FILE)

        for key in cdripper.EXTRA_ALBUM_FIELDS:
            assert parsed[key] == ""

    def test_extra_fields_survive_a_round_trip(self, tmp_path):
        meta = _sidecar_metadata(performer="Tamara Lindeman", engineer="J. Rivera",
                                 recorded="2024-03-14", venue="Massey Hall")
        cdripper.write_metadata_toml(tmp_path, meta)
        parsed = cdripper.read_metadata_toml(tmp_path / cdripper.METADATA_FILE)

        assert parsed["performer"] == "Tamara Lindeman"
        assert parsed["engineer"] == "J. Rivera"
        assert parsed["recorded"] == "2024-03-14"
        assert parsed["venue"] == "Massey Hall"

    def test_titles_containing_quotes_round_trip(self, tmp_path):
        meta = _sidecar_metadata(tracks=[
            {"number": 1, "title": 'The "Real" Thing', "artist": "X"}])
        cdripper.write_metadata_toml(tmp_path, meta)
        parsed = cdripper.read_metadata_toml(tmp_path / cdripper.METADATA_FILE)

        assert parsed["tracks"][0]["title"] == 'The "Real" Thing'

    def test_multi_disc_values_round_trip(self, tmp_path):
        meta = _sidecar_metadata(disc_number=2, disc_total=2, disc_subtitle="Encore")
        path = cdripper.write_metadata_toml(tmp_path, meta)
        parsed = cdripper.read_metadata_toml(path)

        assert (parsed["disc_number"], parsed["disc_total"]) == (2, 2)
        assert parsed["disc_subtitle"] == "Encore"

    def test_multi_disc_sidecars_are_named_per_disc(self, tmp_path):
        path = cdripper.write_metadata_toml(
            tmp_path, _sidecar_metadata(disc_number=2, disc_total=2))
        assert path.name == "metadata_disc2.toml"

    def test_single_disc_sidecar_keeps_the_plain_name(self, tmp_path):
        path = cdripper.write_metadata_toml(tmp_path, _sidecar_metadata())
        assert path.name == cdripper.METADATA_FILE

    def test_compilation_flag_round_trips(self, tmp_path):
        cdripper.write_metadata_toml(tmp_path, _sidecar_metadata(is_va=True))
        assert cdripper.read_metadata_toml(tmp_path / cdripper.METADATA_FILE)["is_va"] is True


class TestReadMetadataToml:
    def _write(self, tmp_path, body):
        path = tmp_path / cdripper.METADATA_FILE
        path.write_text(body, encoding="utf-8")
        return path

    def test_rejects_invalid_toml(self, tmp_path):
        path = self._write(tmp_path, "[album\nartist = ")
        with pytest.raises(ValueError, match="not valid TOML"):
            cdripper.read_metadata_toml(path)

    def test_rejects_empty_artist(self, tmp_path):
        path = self._write(tmp_path, '[album]\nartist=""\nalbum="A"\n[[track]]\nnumber=1\n')
        with pytest.raises(ValueError, match="album.artist is empty"):
            cdripper.read_metadata_toml(path)

    def test_rejects_empty_album(self, tmp_path):
        path = self._write(tmp_path, '[album]\nartist="A"\nalbum=""\n[[track]]\nnumber=1\n')
        with pytest.raises(ValueError, match="album.album is empty"):
            cdripper.read_metadata_toml(path)

    def test_rejects_a_file_with_no_tracks(self, tmp_path):
        path = self._write(tmp_path, '[album]\nartist="A"\nalbum="B"\n')
        with pytest.raises(ValueError, match="no .*track.* entries"):
            cdripper.read_metadata_toml(path)

    def test_rejects_duplicate_track_numbers(self, tmp_path):
        path = self._write(tmp_path, '[album]\nartist="A"\nalbum="B"\n'
                                     '[[track]]\nnumber=1\n[[track]]\nnumber=1\n')
        with pytest.raises(ValueError, match="appears more than once"):
            cdripper.read_metadata_toml(path)

    def test_rejects_a_zero_or_negative_track_number(self, tmp_path):
        path = self._write(tmp_path, '[album]\nartist="A"\nalbum="B"\n[[track]]\nnumber=0\n')
        with pytest.raises(ValueError, match="must be 1 or greater"):
            cdripper.read_metadata_toml(path)

    def test_rejects_a_non_numeric_track_number(self, tmp_path):
        path = self._write(tmp_path, '[album]\nartist="A"\nalbum="B"\n[[track]]\nnumber="one"\n')
        with pytest.raises(ValueError, match="is not a number"):
            cdripper.read_metadata_toml(path)

    def test_blank_track_title_falls_back_to_a_placeholder(self, tmp_path):
        path = self._write(tmp_path, '[album]\nartist="A"\nalbum="B"\n'
                                     '[[track]]\nnumber=4\ntitle=""\n')
        assert cdripper.read_metadata_toml(path)["tracks"][0]["title"] == "Track 04"

    def test_blank_track_artist_inherits_the_album_artist(self, tmp_path):
        path = self._write(tmp_path, '[album]\nartist="A"\nalbum="B"\n'
                                     '[[track]]\nnumber=1\nartist=""\n')
        assert cdripper.read_metadata_toml(path)["tracks"][0]["artist"] == "A"

    def test_tracks_are_sorted_by_number(self, tmp_path):
        path = self._write(tmp_path, '[album]\nartist="A"\nalbum="B"\n'
                                     '[[track]]\nnumber=3\n[[track]]\nnumber=1\n')
        assert [t["number"] for t in cdripper.read_metadata_toml(path)["tracks"]] == [1, 3]

    def test_nonsense_disc_numbers_fall_back_to_one(self, tmp_path):
        path = self._write(tmp_path, '[album]\nartist="A"\nalbum="B"\n'
                                     '[disc]\nnumber="x"\ntotal=0\n[[track]]\nnumber=1\n')
        meta = cdripper.read_metadata_toml(path)
        assert (meta["disc_number"], meta["disc_total"]) == (1, 1)

    def test_surrounding_whitespace_is_stripped(self, tmp_path):
        path = self._write(tmp_path, '[album]\nartist="  A  "\nalbum="  B  "\n'
                                     '[[track]]\nnumber=1\ntitle="  T  "\n')
        meta = cdripper.read_metadata_toml(path)
        assert (meta["artist"], meta["album"], meta["tracks"][0]["title"]) == ("A", "B", "T")


class TestParseRippedFilename:
    @pytest.mark.parametrize("name,expected", [
        ("01 - Robber.flac", (0, 1)),
        ("2-05 - Nicotine.flac", (2, 5)),
        ("10-01 - X.flac", (10, 1)),
        ("03 - Artist - Song.flac", (0, 3)),
    ])
    def test_recognises_ripped_names(self, name, expected):
        assert cdripper._parse_ripped_filename(name) == expected

    @pytest.mark.parametrize("name", [
        "cover.jpg", "notes.txt", "Robber.flac", "album_info.txt",
    ])
    def test_returns_none_for_anything_else(self, name):
        assert cdripper._parse_ripped_filename(name) is None


@pytest.fixture
def ripped_album(tmp_path):
    """A realistic rip: real FLACs, tags, album_info, playlist and sidecar.

    Shaped like an unmatched disc, which is the case issue #13 is about.
    """
    if not cdripper.shutil.which("flac"):
        pytest.skip("flac binary not installed")

    import struct
    frames = int(44100 * 0.05)
    data = b"\x00" * (frames * 4)
    header = (b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVEfmt " +
              struct.pack("<IHHIIHH", 16, 1, 2, 44100, 44100 * 4, 4, 16) +
              b"data" + struct.pack("<I", len(data)))
    wav = tmp_path / "silence.wav"
    wav.write_bytes(header + data)

    album_dir = tmp_path / "Music" / "Unknown Artist" / "xK9fL2mQpR7-"
    album_dir.mkdir(parents=True)

    meta = {
        "artist": "Unknown Artist", "album": "xK9fL2mQpR7-", "date": "",
        "is_va": False, "disc_id": "xK9fL2mQpR7-",
        "disc_number": 1, "disc_total": 1, "disc_subtitle": "",
        "tracks": [{"number": n, "title": f"Track {n:02d}", "artist": "Unknown Artist"}
                   for n in (1, 2, 3)],
    }
    for track in meta["tracks"]:
        out = album_dir / cdripper._track_filename(track, meta)
        subprocess.run(["flac", "-s", "-o", str(out), str(wav)], check=True)
        cdripper.tag_flac(out, meta, track)
    cdripper.write_album_info(album_dir, meta)
    cdripper.write_playlist(album_dir, meta)
    cdripper.write_metadata_toml(album_dir, meta)
    return album_dir


def _edit_sidecar(album_dir, replacements):
    path = album_dir / cdripper.METADATA_FILE
    text = path.read_text()
    for old, new in replacements:
        assert old in text, f"pattern not found in sidecar: {old}"
        text = text.replace(old, new)
    path.write_text(text)


RENAME_ALBUM = [
    ('artist      = "Unknown Artist"', 'artist      = "The Weather Station"'),
    ('album       = "xK9fL2mQpR7-"', 'album       = "Live at Massey Hall"'),
    ('artist = "Unknown Artist"', 'artist = "The Weather Station"'),
    ('title  = "Track 01"', 'title  = "Robber"'),
    ('title  = "Track 02"', 'title  = "Atlantic"'),
    ('title  = "Track 03"', 'title  = "Tried to Tell You"'),
]


class TestApplyMetadata:
    def test_missing_sidecar_is_an_error(self, tmp_path):
        with pytest.raises(ValueError, match="no metadata.toml"):
            cdripper.apply_metadata(tmp_path)

    def test_files_are_renamed_to_the_new_titles(self, ripped_album):
        _edit_sidecar(ripped_album, RENAME_ALBUM)
        new_dir = cdripper.apply_metadata(ripped_album)

        names = sorted(p.name for p in new_dir.glob("*.flac"))
        assert names == ["01 - Robber.flac", "02 - Atlantic.flac",
                         "03 - Tried to Tell You.flac"]

    def test_album_directory_moves_to_the_new_artist_and_album(self, ripped_album):
        _edit_sidecar(ripped_album, RENAME_ALBUM)
        new_dir = cdripper.apply_metadata(ripped_album)

        assert new_dir.name == "Live at Massey Hall"
        assert new_dir.parent.name == "The Weather Station"
        assert not ripped_album.exists()

    def test_an_emptied_artist_directory_is_cleaned_up(self, ripped_album):
        old_artist_dir = ripped_album.parent
        _edit_sidecar(ripped_album, RENAME_ALBUM)
        cdripper.apply_metadata(ripped_album)

        assert not old_artist_dir.exists()

    def test_tags_are_rewritten_from_the_sidecar(self, ripped_album):
        _edit_sidecar(ripped_album, RENAME_ALBUM)
        new_dir = cdripper.apply_metadata(ripped_album)

        from mutagen.flac import FLAC
        tags = FLAC(str(new_dir / "01 - Robber.flac"))
        assert tags["TITLE"] == ["Robber"]
        assert tags["ALBUM"] == ["Live at Massey Hall"]
        assert tags["ALBUMARTIST"] == ["The Weather Station"]

    def test_hand_entered_fields_reach_the_flac_tags(self, ripped_album):
        _edit_sidecar(ripped_album, RENAME_ALBUM + [
            ('performer   = ""', 'performer   = "Tamara Lindeman"'),
            ('engineer    = ""', 'engineer    = "J. Rivera"'),
            ('recorded    = ""', 'recorded    = "2024-03-14"'),
            ('venue       = ""', 'venue       = "Massey Hall, Toronto"'),
        ])
        new_dir = cdripper.apply_metadata(ripped_album)

        from mutagen.flac import FLAC
        tags = FLAC(str(new_dir / "01 - Robber.flac"))
        assert tags["PERFORMER"] == ["Tamara Lindeman"]
        assert tags["ENGINEER"] == ["J. Rivera"]
        assert tags["RECORDINGDATE"] == ["2024-03-14"]
        assert tags["LOCATION"] == ["Massey Hall, Toronto"]

    def test_clearing_a_field_removes_the_tag(self, ripped_album):
        _edit_sidecar(ripped_album, [('performer   = ""', 'performer   = "Someone"')])
        cdripper.apply_metadata(ripped_album, move=False)

        _edit_sidecar(ripped_album, [('performer   = "Someone"', 'performer   = ""')])
        cdripper.apply_metadata(ripped_album, move=False)

        from mutagen.flac import FLAC
        assert "PERFORMER" not in FLAC(str(ripped_album / "01 - Track 01.flac"))

    def test_stale_playlist_from_the_old_name_is_removed(self, ripped_album):
        _edit_sidecar(ripped_album, RENAME_ALBUM)
        new_dir = cdripper.apply_metadata(ripped_album)

        playlists = sorted(p.name for p in new_dir.glob("*.m3u"))
        assert playlists == ["The Weather Station - Live at Massey Hall.m3u"]

    def test_a_playlist_we_did_not_write_is_left_alone(self, ripped_album):
        mine = ripped_album / "my favourites.m3u"
        mine.write_text("/elsewhere/some other song.flac\n")

        _edit_sidecar(ripped_album, RENAME_ALBUM)
        new_dir = cdripper.apply_metadata(ripped_album)

        assert (new_dir / "my favourites.m3u").exists()

    def test_playlist_is_rewritten_with_the_new_filenames(self, ripped_album):
        _edit_sidecar(ripped_album, RENAME_ALBUM)
        new_dir = cdripper.apply_metadata(ripped_album)

        lines = (new_dir / "The Weather Station - Live at Massey Hall.m3u") \
            .read_text().splitlines()
        assert lines == ["01 - Robber.flac", "02 - Atlantic.flac",
                         "03 - Tried to Tell You.flac"]

    def test_album_info_is_rewritten(self, ripped_album):
        _edit_sidecar(ripped_album, RENAME_ALBUM)
        new_dir = cdripper.apply_metadata(ripped_album)

        info = _parse_info(new_dir / "album_info.txt")
        assert info["ARTIST"] == "The Weather Station"
        assert info["TRACK01"] == "Robber"

    def test_in_place_leaves_the_directory_where_it_is(self, ripped_album):
        _edit_sidecar(ripped_album, RENAME_ALBUM)
        result = cdripper.apply_metadata(ripped_album, move=False)

        assert result == ripped_album
        assert (ripped_album / "01 - Robber.flac").exists()

    def test_refuses_to_move_onto_an_existing_directory(self, ripped_album):
        blocker = ripped_album.parent.parent / "The Weather Station" / "Live at Massey Hall"
        blocker.mkdir(parents=True)
        (blocker / "keep me.flac").touch()

        _edit_sidecar(ripped_album, RENAME_ALBUM)
        result = cdripper.apply_metadata(ripped_album)

        assert result == ripped_album           # stayed put
        assert (blocker / "keep me.flac").exists()  # untouched

    def test_a_track_with_no_flac_is_recorded_as_failed(self, ripped_album):
        (ripped_album / "02 - Track 02.flac").unlink()
        cdripper.apply_metadata(ripped_album, move=False)

        assert _parse_info(ripped_album / "album_info.txt")["FAILED_TRACKS"] == "2"

    def test_the_sidecar_is_refreshed_in_the_new_location(self, ripped_album):
        _edit_sidecar(ripped_album, RENAME_ALBUM)
        new_dir = cdripper.apply_metadata(ripped_album)

        refreshed = cdripper.read_metadata_toml(new_dir / cdripper.METADATA_FILE)
        assert refreshed["artist"] == "The Weather Station"

    def test_applying_twice_is_a_no_op(self, ripped_album):
        _edit_sidecar(ripped_album, RENAME_ALBUM)
        first = cdripper.apply_metadata(ripped_album)
        before = sorted(p.name for p in first.iterdir())

        second = cdripper.apply_metadata(first)

        assert second == first
        assert sorted(p.name for p in second.iterdir()) == before

    def test_giving_two_tracks_the_same_title_keeps_both_files(self, ripped_album):
        # the track number stays in the filename, so identical titles are fine
        _edit_sidecar(ripped_album, [
            ('title  = "Track 01"', 'title  = "Reprise"'),
            ('title  = "Track 02"', 'title  = "Reprise"'),
        ])
        cdripper.apply_metadata(ripped_album, move=False)

        names = sorted(p.name for p in ripped_album.glob("*.flac"))
        assert names == ["01 - Reprise.flac", "02 - Reprise.flac", "03 - Track 03.flac"]
        assert not list(ripped_album.glob(".cdripper-rename-*"))

    def test_no_temporary_rename_files_are_left_behind(self, ripped_album):
        _edit_sidecar(ripped_album, RENAME_ALBUM)
        new_dir = cdripper.apply_metadata(ripped_album)

        assert not list(new_dir.glob(".cdripper-rename-*"))

    def test_promoting_a_rip_to_disc_two_renames_and_tags_it(self, ripped_album):
        # editing the disc numbering must not orphan the existing files
        _edit_sidecar(ripped_album, [("number   = 1", "number   = 2"),
                                     ("total    = 1", "total    = 2")])
        cdripper.apply_metadata(ripped_album, move=False)

        names = sorted(p.name for p in ripped_album.glob("*.flac"))
        assert names == ["2-01 - Track 01.flac", "2-02 - Track 02.flac",
                         "2-03 - Track 03.flac"]

        from mutagen.flac import FLAC
        assert FLAC(str(ripped_album / "2-01 - Track 01.flac"))["DISCNUMBER"] == ["2"]

    def test_renumbering_moves_the_sidecar_to_its_per_disc_name(self, ripped_album):
        _edit_sidecar(ripped_album, [("number   = 1", "number   = 2"),
                                     ("total    = 1", "total    = 2")])
        cdripper.apply_metadata(ripped_album, move=False)

        assert (ripped_album / "metadata_disc2.toml").exists()
        assert not (ripped_album / "metadata.toml").exists()


class TestMetadataFromFlacs:
    def test_errors_when_there_are_no_flacs(self, tmp_path):
        with pytest.raises(ValueError, match="no FLAC files"):
            cdripper.metadata_from_flacs(tmp_path)

    def test_rebuilds_album_fields_from_tags(self, ripped_album):
        meta = cdripper.metadata_from_flacs(ripped_album)

        assert meta["album"] == "xK9fL2mQpR7-"
        assert meta["artist"] == "Unknown Artist"
        assert meta["disc_id"] == "xK9fL2mQpR7-"

    def test_rebuilds_the_track_list_in_order(self, ripped_album):
        meta = cdripper.metadata_from_flacs(ripped_album)

        assert [t["number"] for t in meta["tracks"]] == [1, 2, 3]
        assert [t["title"] for t in meta["tracks"]] == ["Track 01", "Track 02", "Track 03"]

    def test_round_trips_through_a_regenerated_sidecar(self, ripped_album):
        (ripped_album / cdripper.METADATA_FILE).unlink()

        meta = cdripper.metadata_from_flacs(ripped_album)
        cdripper.write_metadata_toml(ripped_album, meta)
        parsed = cdripper.read_metadata_toml(ripped_album / cdripper.METADATA_FILE)

        assert parsed["artist"] == "Unknown Artist"
        assert len(parsed["tracks"]) == 3

    def test_extra_tags_are_recovered(self, ripped_album):
        _edit_sidecar(ripped_album, [('engineer    = ""', 'engineer    = "J. Rivera"')])
        cdripper.apply_metadata(ripped_album, move=False)

        assert cdripper.metadata_from_flacs(ripped_album)["engineer"] == "J. Rivera"


class TestCommandRunners:
    def test_apply_reports_failure_for_a_directory_with_no_sidecar(self, tmp_path, capsys):
        assert cdripper._run_apply([str(tmp_path)]) == 1
        assert "metadata.toml" in capsys.readouterr().err

    def test_apply_succeeds_on_a_real_rip(self, ripped_album):
        assert cdripper._run_apply([str(ripped_album)], move=False) == 0

    def test_toml_refuses_to_overwrite_without_force(self, ripped_album, capsys):
        assert cdripper._run_toml([str(ripped_album)]) == 1
        assert "--force" in capsys.readouterr().err

    def test_toml_overwrites_with_force(self, ripped_album):
        assert cdripper._run_toml([str(ripped_album)], force=True) == 0

    def test_toml_reports_failure_for_an_empty_directory(self, tmp_path, capsys):
        assert cdripper._run_toml([str(tmp_path)]) == 1
        assert "no FLAC files" in capsys.readouterr().err


@pytest.fixture
def multi_disc_album(tmp_path):
    """A two-disc rip sharing one directory, as issue #12's layout produces."""
    if not cdripper.shutil.which("flac"):
        pytest.skip("flac binary not installed")

    import struct
    data = b"\x00" * (int(44100 * 0.05) * 4)
    header = (b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVEfmt " +
              struct.pack("<IHHIIHH", 16, 1, 2, 44100, 44100 * 4, 4, 16) +
              b"data" + struct.pack("<I", len(data)))
    wav = tmp_path / "silence.wav"
    wav.write_bytes(header + data)

    album_dir = tmp_path / "Music" / "Ani DiFranco" / "Rome_ Italy"
    album_dir.mkdir(parents=True)

    for disc, titles in ((1, ["Swan Dive", "Educated Guess"]),
                         (2, ["Nicotine", "Bubble"])):
        meta = {
            "artist": "Ani DiFranco", "album": "Rome, Italy", "date": "2004-11-15",
            "is_va": False, "disc_id": f"DISC-{disc}",
            "disc_number": disc, "disc_total": 2, "disc_subtitle": "",
            "tracks": [{"number": n, "title": t, "artist": "Ani DiFranco"}
                       for n, t in enumerate(titles, 1)],
        }
        for track in meta["tracks"]:
            out = album_dir / cdripper._track_filename(track, meta)
            subprocess.run(["flac", "-s", "-o", str(out), str(wav)], check=True)
            cdripper.tag_flac(out, meta, track)
        cdripper.write_album_info(album_dir, meta)
        cdripper.write_playlist(album_dir, meta)
        cdripper.write_metadata_toml(album_dir, meta)
    return album_dir


class TestMultiDiscSidecars:
    def test_each_disc_gets_its_own_sidecar(self, multi_disc_album):
        assert (multi_disc_album / "metadata_disc1.toml").exists()
        assert (multi_disc_album / "metadata_disc2.toml").exists()
        assert not (multi_disc_album / "metadata.toml").exists()

    def test_disc_one_sidecar_is_not_overwritten_by_disc_two(self, multi_disc_album):
        meta = cdripper.read_metadata_toml(multi_disc_album / "metadata_disc1.toml")
        assert [t["title"] for t in meta["tracks"]] == ["Swan Dive", "Educated Guess"]

    def test_sidecar_paths_are_ordered_by_disc(self, multi_disc_album):
        names = [p.name for p in cdripper._sidecar_paths(multi_disc_album)]
        assert names == ["metadata_disc1.toml", "metadata_disc2.toml"]

    def test_disc_10_orders_after_disc_2(self, tmp_path):
        for n in (2, 10):
            (tmp_path / f"metadata_disc{n}.toml").touch()
        names = [p.name for p in cdripper._sidecar_paths(tmp_path)]
        assert names == ["metadata_disc2.toml", "metadata_disc10.toml"]

    def test_apply_covers_every_disc(self, multi_disc_album):
        _edit_sidecar_named(multi_disc_album, "metadata_disc1.toml",
                            [('title  = "Swan Dive"', 'title  = "Swandive"')])
        _edit_sidecar_named(multi_disc_album, "metadata_disc2.toml",
                            [('title  = "Nicotine"', 'title  = "Nicotine (Live)"')])

        cdripper.apply_metadata(multi_disc_album, move=False)

        names = sorted(p.name for p in multi_disc_album.glob("*.flac"))
        assert names == [
            "1-01 - Swandive.flac",
            "1-02 - Educated Guess.flac",
            "2-01 - Nicotine _Live_.flac",
            "2-02 - Bubble.flac",
        ]

    def test_apply_keeps_both_discs_info_files(self, multi_disc_album):
        cdripper.apply_metadata(multi_disc_album, move=False)

        assert (multi_disc_album / "album_info_disc1.txt").exists()
        assert (multi_disc_album / "album_info_disc2.txt").exists()

    def test_apply_leaves_a_playlist_spanning_both_discs(self, multi_disc_album):
        cdripper.apply_metadata(multi_disc_album, move=False)

        lines = (multi_disc_album / "Ani DiFranco - Rome_ Italy.m3u").read_text().splitlines()
        assert len(lines) == 4
        assert lines[0].startswith("1-01") and lines[-1].startswith("2-02")

    def test_renaming_the_album_moves_it_once_and_keeps_both_discs(self, multi_disc_album):
        for name in ("metadata_disc1.toml", "metadata_disc2.toml"):
            _edit_sidecar_named(multi_disc_album, name,
                                [('album       = "Rome, Italy"',
                                  'album       = "Rome Italy 2004"')])

        new_dir = cdripper.apply_metadata(multi_disc_album)

        assert new_dir.name == "Rome Italy 2004"
        assert len(list(new_dir.glob("*.flac"))) == 4
        assert (new_dir / "metadata_disc1.toml").exists()
        assert (new_dir / "metadata_disc2.toml").exists()


def _edit_sidecar_named(album_dir, name, replacements):
    path = album_dir / name
    text = path.read_text()
    for old, new in replacements:
        assert old in text, f"pattern not found in {name}: {old}"
        text = text.replace(old, new)
    path.write_text(text)
