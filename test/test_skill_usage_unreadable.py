"""A ``skill-usage.json`` the load cannot fully read never turns the ledger off.

``SkillsLoader`` builds the ledger once and sets it to ``None`` when the
constructor raises, and nothing then rewrites the file, so one bad byte or one
bad row would end usage ranking on every later start. These tests pin that a bad
row is skipped, and that a non-UTF-8 file or nesting past the interpreter's limit
reads as a corrupt file: an empty ledger the next flush replaces.
"""

from __future__ import annotations

import json
import types

import pytest

from kiro_crew import skill_usage
from kiro_crew.skill_usage import SkillUsageLedger

#: The one clock these tests use, frozen at the module's own binding.
_NOW = 1_800_000_000.0


@pytest.fixture
def frozen(monkeypatch):
    """Freeze ``skill_usage``'s clock so last_seen values are inside the TTL."""
    monkeypatch.setattr(skill_usage, "time", types.SimpleNamespace(time=lambda: _NOW))


def _ledger(tmp_path) -> SkillUsageLedger:
    try:
        led = SkillUsageLedger(tmp_path / "skill-usage.json")
    except Exception as exc:  # the property under test: the load never raises
        pytest.fail(f"SkillUsageLedger raised on the file: {exc!r}")
    led._last_flush = _NOW + 3600  # inside the debounce: only explicit flushes write
    return led


def _saved_keys(tmp_path) -> dict:
    return json.loads((tmp_path / "skill-usage.json").read_text(encoding="utf-8"))["keys"]


def test_an_infinite_hit_count_skips_that_entry_only(tmp_path, frozen) -> None:
    (tmp_path / "skill-usage.json").write_text(
        '{"version": 1, "keys": {"bad": {"hits": 1e400, "last_seen": %r}, '
        '"good": {"hits": 3, "last_seen": %r}}}' % (_NOW, _NOW),
        encoding="utf-8",
    )

    led = _ledger(tmp_path)

    assert led.score("bad")[0] == 0.0
    assert led.score("good")[0] == 3.0
    led.record("good")
    assert led.flush() is True
    assert _saved_keys(tmp_path) == {"good": {"hits": 4, "last_seen": _NOW}}


@pytest.mark.parametrize(
    "content",
    [b'{"keys": {"caf\xe9": {"hits": 2}}}', b"[" * 100_000 + b"]" * 100_000],
    ids=["not-utf8", "nested-past-the-recursion-limit"],
)
def test_an_unparseable_file_starts_empty_and_the_next_flush_replaces_it(
    tmp_path, frozen, content
) -> None:
    (tmp_path / "skill-usage.json").write_bytes(content)

    led = _ledger(tmp_path)
    assert led.snapshot() == {}

    led.record("x")
    assert led.flush() is True
    assert set(_saved_keys(tmp_path)) == {"x"}
    assert _ledger(tmp_path).score("x")[0] == 1.0
