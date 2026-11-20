"""The registry checkout probes stat the destination off the event loop.

``_git_fetch_ref`` and ``_git_clone_or_pull`` ask "is there a checkout here?"
before they touch a destination. Those are blocking ``stat`` calls, so they run
in ``asyncio.to_thread``. These tests pin that, and pin that ``_is_checkout``
answers exactly what ``dest.is_dir() and (dest / ".git").is_dir()`` answers.
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from kiro_crew.apps.registry_pipeline import checkout


def test_is_checkout_needs_a_git_directory(tmp_path):
    missing = tmp_path / "missing"
    plain = tmp_path / "plain"
    plain.mkdir()
    linked = tmp_path / "linked"
    linked.mkdir()
    (linked / ".git").write_text("gitdir: elsewhere\n")
    real = tmp_path / "real"
    (real / ".git").mkdir(parents=True)

    assert not checkout._is_checkout(missing)
    assert not checkout._is_checkout(plain)
    assert not checkout._is_checkout(linked)
    assert checkout._is_checkout(real)


@pytest.mark.asyncio
async def test_fetch_ref_refusal_probes_dest_off_loop(monkeypatch, tmp_path):
    dest = tmp_path / "demo"
    dest.mkdir()  # exists but is not a checkout: the refusal path
    on_loop: list[str] = []
    real_is_dir, real_exists = Path.is_dir, Path.exists

    def _is_dir(self, *args, **kwargs):
        if self == dest / ".git" and threading.current_thread() is threading.main_thread():
            on_loop.append("is_dir")
        return real_is_dir(self, *args, **kwargs)

    def _exists(self, *args, **kwargs):
        if self == dest and threading.current_thread() is threading.main_thread():
            on_loop.append("exists")
        return real_exists(self, *args, **kwargs)

    monkeypatch.setattr(Path, "is_dir", _is_dir)
    monkeypatch.setattr(Path, "exists", _exists)

    result = await checkout._git_fetch_ref(
        "https://github.com/o/demo.git",
        "main",
        dest,
        [],
        clone_env={},
        sandbox_mode="off",
    )

    assert result is not None and result["code"] == "destination_not_a_checkout"
    assert on_loop == []
    assert real_is_dir(dest)  # refused, never deleted
