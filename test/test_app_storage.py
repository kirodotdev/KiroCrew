"""Property tests for AppStorage key isolation and validation.

Feature: app-sdk-gateway-hooks
Properties 18, 19: Storage key isolation and key validation.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

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
