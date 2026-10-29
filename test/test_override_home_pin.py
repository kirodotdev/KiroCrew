"""``KIROCREW_HOME`` is resolved once per process, keyed on the raw env string.

``data_home()`` calls ``_valid_override_home()`` on every call with the override
set. Without a pin each of those calls would ``realpath`` the override (tens of
microseconds), so a listing route that asks for the data home per row would pay
one ``Path.resolve`` per row. The pin makes a repeat call with the same string
free, while a CHANGED string (a test or pod repointing the home) is still
resolved afresh.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kiro_crew.config import paths


def _count_resolves(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    calls: list[Path] = []
    real = Path.resolve

    def spy(self: Path, *args: object, **kwargs: object) -> Path:
        calls.append(self)
        return real(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "resolve", spy)
    return calls


def test_data_home_resolves_the_override_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = tmp_path / "override-home"
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    calls = _count_resolves(monkeypatch)
    for _ in range(200):
        assert paths.data_home() == home.resolve()
    # ``home.resolve()`` in the assert above is the test's own call; drop it.
    own = [c for c in calls if c is not home]
    assert len(own) <= 1, f"Path.resolve ran {len(own)} times for 200 data_home() calls"


def test_a_changed_override_string_is_resolved_again(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    monkeypatch.setenv("KIROCREW_HOME", str(first))
    assert paths.data_home() == first.resolve()
    monkeypatch.setenv("KIROCREW_HOME", str(second))
    assert paths.data_home() == second.resolve()
    assert paths.config_dir() == second.resolve()


def test_a_refused_override_stays_refused_under_the_pin(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The filesystem/drive root is refused on every platform; the pin must cache
    # the refusal, not turn a repeat call into an accept.
    monkeypatch.setenv("KIROCREW_HOME", tmp_path.anchor)
    assert paths._valid_override_home() is None
    assert paths._valid_override_home() is None
