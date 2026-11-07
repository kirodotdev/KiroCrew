"""A dependency ledger that cannot be decoded, or is not an object, reads as empty.

``_read_ledger_unlocked`` already treats malformed JSON and an unreadable file
as an empty ledger. A file with non-UTF-8 bytes, or valid JSON of another shape
(``[]``, a string, a number, ``null``), must degrade the same way rather than
failing every app install, uninstall and uninstall preview that reads it. A
ledger that exists but cannot be read is never rewritten: the install records
nothing and the uninstall removes nothing, so every app's entries stay.
"""

from __future__ import annotations

import errno
import json
import logging
from pathlib import Path
from typing import Any, Callable

import pytest

from kiro_crew.apps.dependency_ledger import (
    _ledger_path,
    classify_and_clean_for_uninstall,
    classify_for_uninstall,
    get_entry,
    list_by_app,
    record_install,
    record_uninstall,
)

KEY = "capability/mcp/x"


@pytest.fixture(autouse=True)
def _ledger_home(tmp_path, _floor_monkeypatch):
    _floor_monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "kirocrew-home"))


def _write_raw(raw: bytes) -> None:
    path = _ledger_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)


def _call(fn: Callable[..., Any], *args: Any) -> Any:
    """Run *fn*, turning an escaped exception into an assertion failure."""
    try:
        return fn(*args)
    except Exception as exc:  # noqa: BLE001 - the escape IS the defect under test
        pytest.fail(f"{fn.__name__} raised on a bad ledger: {exc!r}")


BAD_LEDGERS = [
    pytest.param(b'{"capability/mcp/x": "\xe9"}', id="latin1-bytes"),
    pytest.param(b"[]", id="list"),
    pytest.param(b'"ledger"', id="string"),
    pytest.param(b"42", id="number"),
    pytest.param(b"null", id="null"),
    # Control: malformed JSON already degrades to empty on main.
    pytest.param(b"{bad", id="malformed-control"),
]


@pytest.mark.parametrize("raw", BAD_LEDGERS)
def test_readers_treat_bad_ledger_as_empty(raw: bytes, caplog) -> None:
    _write_raw(raw)
    with caplog.at_level(logging.WARNING, logger="kiro_crew.apps.dependency_ledger"):
        assert _call(get_entry, KEY) is None
    assert "Failed to read dependency ledger" in caplog.text
    assert _call(list_by_app, "app-a") == []
    preview = _call(classify_for_uninstall, "app-a", [KEY])
    assert preview["removable"] == [] and preview["shared"] == []
    assert [row["id"] for row in preview["userInstalled"]] == [KEY]


@pytest.mark.parametrize("raw", BAD_LEDGERS)
def test_uninstall_on_bad_ledger_finds_nothing_to_remove(raw: bytes) -> None:
    _write_raw(raw)
    _call(record_uninstall, KEY, "app-a")
    result = _call(classify_and_clean_for_uninstall, "app-a", [KEY])
    assert result["removable"] == [] and result["shared"] == []


@pytest.mark.parametrize("raw", BAD_LEDGERS)
def test_install_rewrites_bad_ledger_as_an_object(raw: bytes) -> None:
    _write_raw(raw)
    _call(record_install, KEY, "app-a", "capability.mcp")
    assert json.loads(_ledger_path().read_text(encoding="utf-8")) == {
        KEY: {
            "installedBy": ["app-a"],
            "installedAt": get_entry(KEY).installedAt,
            "type": "capability.mcp",
        }
    }
    _call(record_uninstall, KEY, "app-a")
    assert get_entry(KEY) is None


def test_an_unreadable_ledger_is_left_as_it_was_and_removes_nothing(monkeypatch) -> None:
    record_install("capability/mcp/other", "app-c", "capability.mcp")
    record_install(KEY, "app-a", "capability.mcp")
    path = _ledger_path()
    before = path.read_bytes()
    real_read_text = Path.read_text

    def read_text(self: Path, *args: Any, **kwargs: Any) -> str:
        if self == path:
            raise PermissionError(errno.EACCES, "Permission denied", str(self))
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_text)
    record_install(KEY, "app-b", "capability.mcp")
    assert classify_and_clean_for_uninstall("app-a", [KEY])["removable"] == []
    assert path.read_bytes() == before
