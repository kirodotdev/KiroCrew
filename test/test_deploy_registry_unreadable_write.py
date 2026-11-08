"""A locked registry write never saves an empty registry over a present ``profiles.json``.

``load_registry`` degrades a present-but-unreadable file to an empty registry so no
reader 500s. ``locked_registry`` saves what it loaded, so a write that started from
that answer replaced every registered profile with the one edit. A UTF-8 byte-order
mark reads normally. Any other unreadable file is moved to ``<name>.corrupt-<utc>``
before the save, and when even that move fails the write raises with the file left
as it is.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from pathlib import Path

import pytest
from stray_line_helpers import cyclic_gc_quiesced  # noqa: F401 - fixture
from stray_line_helpers import too_deep_line

from kiro_crew.deploy import profiles as profiles_mod

BOM = b"\xef\xbb\xbf"


@pytest.fixture()
def registry(tmp_path: Path, monkeypatch) -> Path:
    path = tmp_path / "profiles.json"
    monkeypatch.setattr(profiles_mod, "_data_dir", lambda: tmp_path)
    monkeypatch.setattr(profiles_mod, "_registry_path", lambda: path)
    monkeypatch.setattr(profiles_mod, "_legacy_registry_path", lambda: tmp_path / "nope.json")
    monkeypatch.setattr(profiles_mod, "_legacy_config_path", lambda: tmp_path / "nope2.json")
    return path


def _two_profiles() -> bytes:
    return json.dumps(
        {
            "version": 2,
            "default": "alpha",
            "profiles": [
                profiles_mod.make_entry("alpha", "us-east-1"),
                profiles_mod.make_entry("beta", "us-west-2"),
            ],
        }
    ).encode("utf-8")


def _add_gamma() -> None:
    """One profile add, the shape of the POST route's locked read-modify-write."""
    with profiles_mod.locked_registry() as reg:
        reg["profiles"].append(profiles_mod.make_entry("gamma", "eu-west-1"))


def _names(path: Path) -> list[str]:
    return [p["name"] for p in json.loads(path.read_text(encoding="utf-8"))["profiles"]]


def test_a_bom_registry_keeps_its_profiles_through_a_write(registry: Path) -> None:
    registry.write_bytes(BOM + _two_profiles())
    _add_gamma()
    assert _names(registry) == ["alpha", "beta", "gamma"]
    assert json.loads(registry.read_text(encoding="utf-8"))["default"] == "alpha"
    assert _asides(registry) == []


def test_readers_see_a_bom_registrys_profiles(registry: Path) -> None:
    registry.write_bytes(BOM + _two_profiles())
    assert [p["name"] for p in profiles_mod.load_registry()["profiles"]] == ["alpha", "beta"]


def _asides(path: Path) -> list[Path]:
    return sorted(path.parent.glob(f"{path.name}.corrupt-*"))


def _fail_reads_of(path: Path, monkeypatch) -> None:
    """A Windows share-mode-0 handle (a scanner) held past the retry budget."""
    real = profiles_mod.read_bytes_with_retry

    def held(p, *args, **kwargs):
        if Path(p) == path:
            raise PermissionError(13, "being used by another process")
        return real(p, *args, **kwargs)

    monkeypatch.setattr(profiles_mod, "read_bytes_with_retry", held)


@pytest.mark.usefixtures("cyclic_gc_quiesced")
@pytest.mark.parametrize(
    "content",
    [
        '{"profiles": [{"name": "café", "region": "us-east-1"}]}'.encode("latin-1"),
        b'{"profiles": [',
        b"[]",
        b'{"profiles": 5}',
        b'{"profiles": {"alpha": {"name": "alpha", "region": "us-east-1"}}}',
        b'{"profiles": "alpha"}',
        too_deep_line,  # json.loads raises RecursionError, which is not a ValueError
    ],
    ids=[
        "undecodable",
        "truncated",
        "list-root",
        "non-iterable-profiles",
        "profiles-mapping",
        "profiles-string",
        "nested-past-the-decoder",
    ],
)
def test_an_unreadable_registry_is_moved_aside_before_the_write(
    registry: Path, content: bytes | Callable[[], bytes], caplog
) -> None:
    if callable(content):
        content = content()
    registry.write_bytes(content)
    with caplog.at_level(logging.WARNING, logger=profiles_mod.__name__):
        _add_gamma()
    assert _names(registry) == ["gamma"]
    [aside] = _asides(registry)
    assert aside.read_bytes() == content
    assert aside.name in caplog.text


def test_a_failed_read_refuses_the_write_and_keeps_the_file(registry: Path, monkeypatch) -> None:
    original = _two_profiles()
    registry.write_bytes(original)
    _fail_reads_of(registry, monkeypatch)
    with pytest.raises(profiles_mod.RegistryUnreadableError, match="profiles.json"):
        _add_gamma()
    assert registry.read_bytes() == original
    assert _asides(registry) == []


def test_a_registry_that_cannot_be_moved_aside_refuses_the_write(
    registry: Path, monkeypatch
) -> None:
    content = b'{"profiles": ['
    registry.write_bytes(content)
    real_replace = profiles_mod.replace_with_retry

    def refuse(src, dst):
        if ".corrupt-" in str(dst):
            raise PermissionError(13, "being used by another process")
        return real_replace(src, dst)

    monkeypatch.setattr(profiles_mod, "replace_with_retry", refuse)
    with pytest.raises(profiles_mod.RegistryUnreadableError, match="profiles.json"):
        _add_gamma()
    assert registry.read_bytes() == content
    assert _asides(registry) == []


def test_a_missing_registry_still_starts_empty_and_the_first_add_writes_it(
    registry: Path,
) -> None:
    _add_gamma()
    assert _names(registry) == ["gamma"]
    assert _asides(registry) == []


def test_a_missing_registry_still_migrates_the_legacy_one(
    registry: Path, tmp_path: Path, monkeypatch
) -> None:
    legacy = tmp_path / "legacy.json"
    legacy.write_bytes(BOM + _two_profiles())
    monkeypatch.setattr(profiles_mod, "_legacy_registry_path", lambda: legacy)
    _add_gamma()
    assert _names(registry) == ["alpha", "beta", "gamma"]
