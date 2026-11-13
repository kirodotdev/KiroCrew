"""A stuck agent-spec lock is waited out once per rebuild, not once per install site.

The boot rebuild takes ``agents_spec_lock`` at several install sites and each one
catches the bounded-acquire refusal as best-effort work. Each site waiting out the
whole bound kept boot from ever reaching its port bind while a holder was stuck.
"""

from __future__ import annotations

import contextlib
import errno

import pytest

import kiro_crew.agent as agent_mod
from kiro_crew import platform_compat


@pytest.fixture
def stuck_lock(monkeypatch):
    """``file_lock`` refuses like a bounded acquire on a held lock, counting calls."""
    calls: list[int] = []

    @contextlib.contextmanager
    def refuse(fd, *, exclusive, wait):
        calls.append(fd)
        raise OSError(errno.ETIMEDOUT, "still held after waiting 300.00s (limit 300s)")
        yield  # pragma: no cover - never reached

    monkeypatch.setattr(platform_compat, "file_lock", refuse)
    return calls


def _scope():
    # A missing scope reads as no scope, so a revert fails on the call count.
    return getattr(agent_mod, "one_spec_lock_wait", contextlib.nullcontext)()


def _acquire(agents_dir) -> None:
    with agent_mod.agents_spec_lock(agents_dir):
        pass  # pragma: no cover - the acquire refuses


def test_inside_a_rebuild_scope_only_the_first_site_waits(tmp_path, stuck_lock):
    with _scope():
        for _ in range(3):
            with pytest.raises(OSError):
                _acquire(tmp_path)

    assert len(stuck_lock) == 1


def test_outside_a_scope_every_acquire_still_waits(tmp_path, stuck_lock):
    for _ in range(2):
        with pytest.raises(OSError):
            _acquire(tmp_path)

    assert len(stuck_lock) == 2


def test_a_new_scope_waits_again(tmp_path, stuck_lock):
    for _ in range(2):
        with _scope():
            with pytest.raises(OSError):
                _acquire(tmp_path)

    assert len(stuck_lock) == 2
