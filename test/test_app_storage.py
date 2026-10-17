"""Property tests for AppStorage key isolation and validation.

Feature: app-sdk-gateway-hooks
Properties 18, 19: Storage key isolation and key validation.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

import kiro_crew.apps.app_storage as app_storage_mod
from kiro_crew.apps.app_storage import AppStorage

# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------


def _valid_key() -> st.SearchStrategy[str]:
    """Generate valid storage keys (no traversal chars)."""
    return st.from_regex(r"[a-z][a-z0-9_-]{1,30}", fullmatch=True)


def _json_value() -> st.SearchStrategy[dict]:
    """Generate JSON-serializable dict values."""
    return st.dictionaries(
        st.from_regex(r"[a-z][a-z0-9_]{0,10}", fullmatch=True),
        st.one_of(
            st.text(min_size=0, max_size=50, alphabet=st.characters(whitelist_categories=("L", "N"))),
            st.integers(min_value=-1000, max_value=1000),
            st.booleans(),
        ),
        max_size=5,
    )


# ---------------------------------------------------------------------------
# Property 18: AppStorage key isolation
# ---------------------------------------------------------------------------


class TestAppStorageKeyIsolation:
    """Property 18: AppStorage key isolation.

    **Validates: Requirements 5.1**
    """

    @settings(max_examples=100, suppress_health_check=[HealthCheck.function_scoped_fixture])
    @given(key=_valid_key(), value=_json_value())
    def test_set_then_get_returns_equivalent(self, key: str, value: dict, tmp_path: Path) -> None:
        """set(K, V) then get(K) returns equivalent value."""
        storage = AppStorage("test-app", tmp_path)
        storage.set(key, value)
        result = storage.get(key)
        assert result == value

    @settings(max_examples=100, suppress_health_check=[HealthCheck.function_scoped_fixture])
    @given(key=_valid_key(), value=_json_value())
    def test_delete_then_get_returns_none(self, key: str, value: dict, tmp_path: Path) -> None:
        """delete(K) then get(K) returns None."""
        storage = AppStorage("test-app", tmp_path)
        storage.set(key, value)
        assert storage.get(key) is not None
        deleted = storage.delete(key)
        assert deleted is True
        assert storage.get(key) is None

    @settings(max_examples=50, suppress_health_check=[HealthCheck.function_scoped_fixture])
    @given(key=_valid_key())
    def test_get_nonexistent_returns_none(self, key: str, tmp_path: Path) -> None:
        """get(K) for non-existent key returns None."""
        storage = AppStorage("test-app", tmp_path)
        assert storage.get(key) is None

    @settings(max_examples=50, suppress_health_check=[HealthCheck.function_scoped_fixture])
    @given(key=_valid_key())
    def test_delete_nonexistent_returns_false(self, key: str, tmp_path: Path) -> None:
        """delete(K) for non-existent key returns False."""
        storage = AppStorage("test-app", tmp_path)
        assert storage.delete(key) is False

    def test_delete_returns_false_when_key_vanishes_after_probe(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A concurrent delete is the same observable state as an absent key."""
        storage = AppStorage("test-app", tmp_path)
        storage.set("shared", {"value": 1})
        key_path = storage._key_path("shared")
        real_is_file = Path.is_file

        def _remove_after_probe(path: Path) -> bool:
            present = real_is_file(path)
            if path == key_path and present:
                path.unlink()
            return present

        monkeypatch.setattr(Path, "is_file", _remove_after_probe)

        assert storage.delete("shared") is False

    @settings(max_examples=50, suppress_health_check=[HealthCheck.function_scoped_fixture])
    @given(keys=st.lists(_valid_key(), min_size=1, max_size=10, unique=True))
    def test_list_keys_returns_all_set_keys(self, keys: list[str], tmp_path: Path) -> None:
        """list_keys() returns all keys that have been set."""
        # Use unique subdir to avoid hypothesis tmp_path reuse
        import uuid
        work_dir = tmp_path / uuid.uuid4().hex
        work_dir.mkdir()
        storage = AppStorage("test-app", work_dir)
        for k in keys:
            storage.set(k, {"key": k})
        listed = storage.list_keys()
        assert set(listed) == set(keys)

    def test_string_value_round_trip(self, tmp_path: Path) -> None:
        """String values round-trip correctly."""
        storage = AppStorage("test-app", tmp_path)
        storage.set("my-key", "hello world")
        # String values are stored as-is (not JSON-wrapped)
        result = storage.get("my-key")
        assert result == "hello world"


# ---------------------------------------------------------------------------
# Property 19: AppStorage key validation rejects traversal
# ---------------------------------------------------------------------------


class TestAppStorageKeyValidation:
    """Property 19: AppStorage key validation rejects traversal.

    **Validates: Security (path traversal prevention)**
    """

    @pytest.mark.parametrize("bad_key", [
        "../etc/passwd",
        "..secret",
        "path/to/file",
        "back\\slash",
        "",
        ".hidden",
        "~home",
    ])
    def test_invalid_keys_raise_valueerror(self, bad_key: str, tmp_path: Path) -> None:
        """Keys with traversal characters raise ValueError."""
        storage = AppStorage("test-app", tmp_path)
        with pytest.raises(ValueError):
            storage.set(bad_key, {"data": True})

    @pytest.mark.parametrize("bad_key", [
        "../escape",
        "sub/dir",
        "back\\slash",
        "",
    ])
    def test_get_with_invalid_key_raises(self, bad_key: str, tmp_path: Path) -> None:
        """get() with invalid key raises ValueError."""
        storage = AppStorage("test-app", tmp_path)
        with pytest.raises(ValueError):
            storage.get(bad_key)

    @pytest.mark.parametrize("bad_key", [
        "../escape",
        "sub/dir",
    ])
    def test_delete_with_invalid_key_raises(self, bad_key: str, tmp_path: Path) -> None:
        """delete() with invalid key raises ValueError."""
        storage = AppStorage("test-app", tmp_path)
        with pytest.raises(ValueError):
            storage.delete(bad_key)

    @settings(max_examples=100, suppress_health_check=[HealthCheck.function_scoped_fixture])
    @given(key=_valid_key())
    def test_valid_keys_do_not_raise(self, key: str, tmp_path: Path) -> None:
        """Valid keys (no traversal chars) do not raise."""
        storage = AppStorage("test-app", tmp_path)
        # Should not raise
        storage.set(key, {"ok": True})
        storage.get(key)
        storage.delete(key)


# ---------------------------------------------------------------------------
# Keys that are not usable Windows filenames
# ---------------------------------------------------------------------------

#: One representative per sub-class of key that Win32 cannot store verbatim.
#: The device names are reachable from the ``[a-z][a-z0-9_-]{1,30}`` charset the
#: property tests above generate, so they are not a theoretical set.
_UNSTORABLE_KEYS = [
    "aux",
    "AUX",
    "Aux",
    "con",
    "prn",
    "nul",
    "com1",
    "com9",
    "lpt1",
    "conin$",
    "aux.backup",  # a device name owns every extension after it
    "aux ",  # Win32 strips the trailing space before matching the device
    "a<b",
    "a>b",
    "a:b",  # on NTFS this names an alternate data stream, not a file
    'a"b',
    "a|b",
    "a?b",
    "a*b",
    "a\x01b",
]

#: Keys the encoder must leave completely alone, so a store written without the
#: encoding keeps every key it can already address.
_UNCHANGED_KEYS = [
    "simple",
    "my-key",
    "with_underscore",
    "a.b",
    "a.",  # the ".json" suffix follows, so no component ends with the dot
    "a ",
    "100%",
    "na\u00efve",
    "auxx",
    "aux1",
    "com0",
    "com10",
    "conn",
]


class TestAppStorageWindowsFilenames:
    """A key must round-trip whatever Win32 makes of the filename it implies.

    ``_key_path`` and ``list_keys`` are one mapping and its inverse, so both
    halves are asserted together. These assertions are on the MAPPING, not on
    filesystem behaviour, so they hold identically on every platform.
    """

    @pytest.mark.parametrize("key", _UNSTORABLE_KEYS)
    def test_unstorable_keys_map_to_a_usable_filename(self, key: str, tmp_path: Path) -> None:
        """The filename carries no forbidden character and is not a device."""
        storage = AppStorage("test-app", tmp_path)
        name = storage._key_path(key).name
        assert not (set(name) & app_storage_mod._UNSAFE_KEY_CHARS), name
        head = name.split(".", 1)[0].rstrip(" ").upper()
        assert head not in app_storage_mod._RESERVED_DEVICE_NAMES, name
        assert name.endswith(".json"), name

    @pytest.mark.parametrize("key", _UNSTORABLE_KEYS + _UNCHANGED_KEYS)
    def test_the_stem_decodes_back_to_the_key(self, key: str, tmp_path: Path) -> None:
        """``list_keys``' decode is the exact inverse of the path mapping."""
        storage = AppStorage("test-app", tmp_path)
        stem = storage._key_path(key).stem
        assert app_storage_mod._decode_stem(stem) == key

    @pytest.mark.parametrize("key", _UNCHANGED_KEYS)
    def test_storable_keys_keep_their_filename(self, key: str, tmp_path: Path) -> None:
        """A key Win32 can store is its own stem, byte for byte."""
        storage = AppStorage("test-app", tmp_path)
        assert storage._key_path(key).name == f"{key}.json"

    def test_distinct_keys_never_share_a_filename(self, tmp_path: Path) -> None:
        storage = AppStorage("test-app", tmp_path)
        names = [storage._key_path(k).name for k in _UNSTORABLE_KEYS + _UNCHANGED_KEYS]
        assert len(set(names)) == len(names), "two keys collide on one file"

    @pytest.mark.parametrize("key", _UNSTORABLE_KEYS + _UNCHANGED_KEYS)
    def test_set_get_list_delete_round_trip(self, key: str, tmp_path: Path) -> None:
        """The whole store works for every sub-class, on this platform."""
        storage = AppStorage("test-app", tmp_path)
        storage.set(key, {"key": key})
        assert storage.get(key) == {"key": key}
        assert storage.list_keys() == [key]
        assert storage.delete(key) is True
        assert storage.get(key) is None
        assert storage.list_keys() == []

    def test_every_key_is_listed_once_when_all_are_stored(self, tmp_path: Path) -> None:
        storage = AppStorage("test-app", tmp_path)
        # Case variants are excluded: whether two keys differing only in case
        # share one file is the VOLUME's property, asserted separately below.
        keys = [k for k in _UNSTORABLE_KEYS + _UNCHANGED_KEYS if k not in {"AUX", "Aux"}]
        for key in keys:
            storage.set(key, {"key": key})
        assert sorted(storage.list_keys()) == sorted(keys)

    @pytest.mark.parametrize("pair", [("aux", "AUX"), ("aux", "Aux"), ("simple", "Simple")])
    def test_case_is_carried_into_the_filename_unchanged(
        self, pair: tuple[str, str], tmp_path: Path
    ) -> None:
        """The mapping preserves case and never folds two keys together.

        Whether the VOLUME then distinguishes those two names is the platform's
        property and is the same for every key, encoded or not: on a
        case-insensitive volume ``Simple`` and ``simple`` already shared one
        file. The mapping must not make that worse by folding case itself.
        """
        storage = AppStorage("test-app", tmp_path)
        lower, other = pair
        assert storage._key_path(lower).name != storage._key_path(other).name

    def test_an_undecodable_marked_file_is_skipped(self, tmp_path: Path) -> None:
        """A marked stem that is not valid UTF-8 belongs to something else."""
        storage = AppStorage("test-app", tmp_path)
        storage.set("real", {"ok": True})
        bogus = f"{app_storage_mod._ENCODED_STEM_PREFIX}%FF%FE.json"
        (tmp_path / "kv" / bogus).write_text("{}", encoding="utf-8")
        assert storage.list_keys() == ["real"]


class TestAppStorageUnencodedFiles:
    """A store holding a key under the key's own name keeps working.

    A key that needs encoding now was storable verbatim on a platform whose
    filenames it was always legal in, so such a file can already be on disk.
    ``list_keys`` reports it and ``get`` must read the same one, or the store
    contradicts itself about which keys exist.
    """

    #: Written directly, the way a store without the encoding holds them. Each is
    #: a legal POSIX filename, so this is the state a real store can be in.
    LEGACY = ["a:b", "a*b", "a?b", "aux"]

    def _plant(self, tmp_path: Path, key: str, value: dict) -> Path:
        kv = tmp_path / "kv"
        kv.mkdir(parents=True, exist_ok=True)
        path = kv / f"{key}.json"
        if path.parent != kv:
            pytest.skip(f"{key!r} does not name a file inside the store on this platform")
        try:
            path.write_text(json.dumps(value), encoding="utf-8")
        except OSError:
            pytest.skip(f"this platform cannot create {path.name!r}")
        return path

    @pytest.mark.parametrize("key", LEGACY)
    def test_get_reads_the_unencoded_file(self, key: str, tmp_path: Path) -> None:
        self._plant(tmp_path, key, {"from": "unencoded"})
        storage = AppStorage("test-app", tmp_path)
        assert storage.get(key) == {"from": "unencoded"}

    @pytest.mark.parametrize("key", LEGACY)
    def test_list_and_get_agree(self, key: str, tmp_path: Path) -> None:
        """The divergence this guards: listed but unreadable."""
        self._plant(tmp_path, key, {"from": "unencoded"})
        storage = AppStorage("test-app", tmp_path)
        for listed in storage.list_keys():
            assert storage.get(listed) is not None, f"{listed!r} is listed but unreadable"

    @pytest.mark.parametrize("key", LEGACY)
    def test_set_wins_over_the_unencoded_file(self, key: str, tmp_path: Path) -> None:
        self._plant(tmp_path, key, {"from": "unencoded"})
        storage = AppStorage("test-app", tmp_path)
        storage.set(key, {"from": "encoded"})
        assert storage.get(key) == {"from": "encoded"}
        assert storage.list_keys() == [key], "the key is reported twice"

    @pytest.mark.parametrize("key", LEGACY)
    def test_set_removes_the_superseded_copy(self, key: str, tmp_path: Path) -> None:
        """Only one file holds the current value.

        A copy left under the key's own name is what a build without the encoding
        reads, so leaving it would have that build serve a stale value as current.
        """
        planted = self._plant(tmp_path, key, {"from": "unencoded"})
        storage = AppStorage("test-app", tmp_path)
        storage.set(key, {"from": "encoded"})
        assert not planted.exists(), "the superseded copy is still on disk"
        assert storage._key_path(key).is_file()

    @pytest.mark.parametrize("key", LEGACY)
    def test_delete_removes_the_unencoded_file_too(self, key: str, tmp_path: Path) -> None:
        """A deleted key must not come back from the other location."""
        self._plant(tmp_path, key, {"from": "unencoded"})
        storage = AppStorage("test-app", tmp_path)
        storage.set(key, {"from": "encoded"})
        assert storage.delete(key) is True
        assert storage.get(key) is None
        assert storage.list_keys() == []

    def test_the_unencoded_path_never_leaves_the_store(self, tmp_path: Path) -> None:
        """On Windows ``a:b`` parses as a DRIVE, so the join must be refused.

        ``kv_dir / "a:b.json"`` yields the drive-relative ``a:b.json`` there,
        which is not inside the store at all.
        """
        storage = AppStorage("test-app", tmp_path)
        kv = tmp_path / "kv"
        for key in ["a:b", "c:x", "simple"]:
            resolved = storage._unencoded_key_path(key)
            assert resolved is None or resolved.parent == kv, (key, resolved)

    def test_a_safe_key_has_no_second_location(self, tmp_path: Path) -> None:
        """For a key that needs no encoding the two paths are the same file."""
        storage = AppStorage("test-app", tmp_path)
        assert storage._unencoded_key_path("plain") == storage._key_path("plain")

    def test_the_second_location_is_never_another_key(self, tmp_path: Path) -> None:
        """A drive-relative key must not resolve onto an unrelated key's file.

        On Windows ``C:foo`` joined onto a key directory on drive ``C:`` yields
        ``kv/foo.json`` â€” inside the directory, but the file of the unrelated key
        ``foo``.
        """
        storage = AppStorage("test-app", tmp_path)
        drive = os.path.splitdrive(str(tmp_path))[0]
        candidates = ["C:foo", "c:foo", "a:foo", f"{drive}foo"] if drive else ["C:foo", "a:foo"]
        for key in candidates:
            resolved = storage._unencoded_key_path(key)
            assert resolved is None or resolved.name == f"{key}.json", (key, resolved)

    def test_deleting_a_drive_relative_key_keeps_the_other_key(self, tmp_path: Path) -> None:
        """The consequence the guard prevents: losing an unrelated key's data."""
        storage = AppStorage("test-app", tmp_path)
        storage.set("foo", {"victim": True})
        drive = os.path.splitdrive(str(tmp_path))[0] or "C:"
        storage.delete(f"{drive}foo")
        assert storage.get("foo") == {"victim": True}
        assert "foo" in storage.list_keys()


class TestAppStorageLongKeys:
    """A key whose escaped name would not fit still round-trips.

    Percent-encoding costs up to three characters per character, so the escaped
    name is not bounded; the digest name is, and the key is recorded in the file
    so ``list_keys`` can still report it.
    """

    #: 82 is the first length at which 82 unsafe characters overflow the escaped
    #: form: 5 + 246 + 5 = 256 bytes against a 255-byte filename limit.
    OVERLONG = "*" * 82

    def test_the_filename_stays_within_the_limit(self, tmp_path: Path) -> None:
        storage = AppStorage("test-app", tmp_path)
        for key in (self.OVERLONG, "*" * 200, "?" * 500):
            name = storage._key_path(key).name
            assert len(name.encode()) <= app_storage_mod._MAX_FILENAME_BYTES, (key[:8], len(name))

    def test_an_overlong_key_round_trips(self, tmp_path: Path) -> None:
        storage = AppStorage("test-app", tmp_path)
        storage.set(self.OVERLONG, {"v": 1})
        assert storage.get(self.OVERLONG) == {"v": 1}
        assert storage.list_keys() == [self.OVERLONG]
        assert storage.delete(self.OVERLONG) is True
        assert storage.list_keys() == []

    def test_a_string_value_round_trips_through_the_envelope(self, tmp_path: Path) -> None:
        """The envelope must not swallow a non-dict value."""
        storage = AppStorage("test-app", tmp_path)
        storage.set(self.OVERLONG, "hello")
        assert storage.get(self.OVERLONG) == "hello"

    def test_two_overlong_keys_stay_separate(self, tmp_path: Path) -> None:
        storage = AppStorage("test-app", tmp_path)
        other = "*" * 81 + "?"
        storage.set(self.OVERLONG, {"which": "first"})
        storage.set(other, {"which": "second"})
        assert storage.get(self.OVERLONG) == {"which": "first"}
        assert storage.get(other) == {"which": "second"}
        assert sorted(storage.list_keys()) == sorted([self.OVERLONG, other])

    def test_an_overlong_key_reads_its_unencoded_file(self, tmp_path: Path) -> None:
        """The pre-encoding store held this key at 87 bytes; it stays readable."""
        kv = tmp_path / "kv"
        kv.mkdir(parents=True, exist_ok=True)
        legacy = kv / f"{self.OVERLONG}.json"
        try:
            legacy.write_text(json.dumps({"from": "unencoded"}), encoding="utf-8")
        except OSError:
            pytest.skip("this platform cannot create the unencoded name")
        storage = AppStorage("test-app", tmp_path)
        assert storage.get(self.OVERLONG) == {"from": "unencoded"}

    def test_a_digest_file_without_a_key_is_skipped(self, tmp_path: Path) -> None:
        storage = AppStorage("test-app", tmp_path)
        storage.set("real", {"ok": True})
        kv = tmp_path / "kv"
        (kv / f"{app_storage_mod._DIGEST_STEM_PREFIX}00ff.json").write_text(
            json.dumps({"value": {"orphan": True}}), encoding="utf-8"
        )
        assert storage.list_keys() == ["real"]

    def test_a_non_utf8_digest_file_does_not_break_listing(self, tmp_path: Path) -> None:
        """One foreign file must not break every ``list_keys`` call.

        ``UnicodeDecodeError`` is a ``ValueError``, so it is caught by neither
        ``OSError`` nor ``JSONDecodeError``.
        """
        storage = AppStorage("test-app", tmp_path)
        storage.set("real", {"ok": True})
        kv = tmp_path / "kv"
        (kv / f"{app_storage_mod._DIGEST_STEM_PREFIX}dead.json").write_bytes(b"\xff\xfe\x00bad")
        assert storage.list_keys() == ["real"]

    def test_a_non_utf8_value_file_reads_as_absent(self, tmp_path: Path) -> None:
        storage = AppStorage("test-app", tmp_path)
        storage.set(self.OVERLONG, {"v": 1})
        storage._key_path(self.OVERLONG).write_bytes(b"\xff\xfe\x00bad")
        assert storage.get(self.OVERLONG) is None


class TestAppStorageDeviceSetIsShared:
    """The reserved-name set must not restate the one shared definition."""

    def test_every_shared_stem_is_covered(self) -> None:
        from kiro_crew.constants import WINDOWS_DEVICE_STEMS

        assert {s.upper() for s in WINDOWS_DEVICE_STEMS} <= app_storage_mod._RESERVED_DEVICE_NAMES

    def test_a_shared_stem_is_not_filename_safe(self) -> None:
        from kiro_crew.constants import WINDOWS_DEVICE_STEMS

        for stem in sorted(WINDOWS_DEVICE_STEMS):
            assert not app_storage_mod._key_is_filename_safe(stem), stem
