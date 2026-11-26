"""An update adds the memory-index extra once, best effort, and never fails on it."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
from wheel_engine_helpers import _stub_children

from kiro_crew.platform import wheel_engine


def _extra(wheel: Path) -> str:
    return f"{wheel}[memory-index]"


def test_an_update_adds_the_memory_index_extra_from_wheels_only(monkeypatch, tmp_path):
    monkeypatch.delenv("KIROCREW_ALLOW_SOURCE_BUILDS", raising=False)
    seen = _stub_children(monkeypatch)
    wheel = tmp_path / "w.whl"

    wheel_engine.build_shadow_venv(wheel, tmp_path / "crew-venv-1.0.0")

    added = [argv for argv, _kwargs in seen if _extra(wheel) in argv]
    assert len(added) == 1
    assert added[0][-2:] == ["--only-binary=:all:", _extra(wheel)]


def test_a_host_without_the_wheel_still_updates_and_warns(monkeypatch, tmp_path, caplog):
    wheel = tmp_path / "w.whl"
    no_wheel = b"ERROR: No matching distribution found for usearch<3,>=2.26.6"
    _stub_children(
        monkeypatch, lambda argv: (1, b"", no_wheel) if _extra(wheel) in argv else (0, b"", b"")
    )

    with caplog.at_level(logging.WARNING, logger="kiro_crew.platform.wheel_engine"):
        wheel_engine.build_shadow_venv(wheel, tmp_path / "crew-venv-1.0.0")

    assert "memory-index extra did not install" in caplog.text


def test_a_tree_that_carries_the_extra_gets_no_second_install(monkeypatch, tmp_path):
    seen = _stub_children(monkeypatch)
    wheel = tmp_path / "w.whl"

    wheel_engine.build_shadow_venv(wheel, tmp_path / "crew-venv-1.0.0", extras=("memory-index",))

    # The carried extra rides on the one wheel install; nothing installs it again.
    assert len([argv for argv, _kwargs in seen if _extra(wheel) in argv]) == 1


def test_a_cancel_during_the_extra_still_cancels_the_update(monkeypatch, tmp_path):
    wheel = tmp_path / "w.whl"
    _stub_children(monkeypatch)
    real_run = wheel_engine._run

    def run(argv, timeout, step, **kwargs):
        if _extra(wheel) in argv:
            raise wheel_engine.WheelUpdateCancelled("cancelled")
        return real_run(argv, timeout, step, **kwargs)

    monkeypatch.setattr(wheel_engine, "_run", run)
    with pytest.raises(wheel_engine.WheelUpdateCancelled):
        wheel_engine.build_shadow_venv(wheel, tmp_path / "crew-venv-1.0.0")
