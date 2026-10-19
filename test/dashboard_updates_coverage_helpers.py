"""Shared fixture and stubs for the dashboard update handler tests.

The autouse ``_isolated_module_state`` fixture snapshots and restores the ``updates``
globals the tests mutate (the update-info cache, the log ring, the changelog cache, the
check clock and generation) and drops the check task after each test; it also clears the
project-directory and CDN environment variables and points the data home at ``tmp_path``.
Beside it sit the ``_request`` stub with its ``_as_owner`` claims, the ``_FakeProc``
subprocess stand-in, ``_sequence_procs``, which serves those to successive spawns, and
``_git_proj``, a directory the capability derivation accepts as this install's checkout.
``test_dashboard_updates_coverage.py`` and ``test_dashboard_updates_coverage_apply.py``
import all of them but ``_as_owner``; ``test_dashboard_updates_coverage_streams.py``
imports the fixture and ``_request``.
"""

from __future__ import annotations

import asyncio
from unittest.mock import DEFAULT, MagicMock

import pytest

from kiro_crew.dashboard.handlers import updates
from kiro_crew.platform import update_layout


@pytest.fixture(autouse=True)
# nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- pre-existing autouse fixture moved verbatim; no importing module calls monkeypatch.undo()
def _isolated_module_state(monkeypatch, tmp_path):
    """Snapshot every module global these tests mutate, then put it all back.

    ``_update_info``, the ring buffer and the changelog cache are process-wide,
    so a test that left one dirty would change what a LATER test measures --
    exactly the class of cross-test coupling this module's cache contract exists
    to prevent.
    """
    monkeypatch.delenv("KIROCREW_PROJECT_DIR", raising=False)
    monkeypatch.delenv("KIROCREW_CDN_BASE", raising=False)
    monkeypatch.setattr(update_layout, "data_home", lambda: tmp_path)
    saved_info = dict(updates._update_info)
    saved_ring = list(updates._log_ring)
    saved_cache = updates._changelog_cache
    saved_clock = updates._last_update_check
    saved_generation = updates._check_generation
    updates._changelog_cache = None
    yield
    updates._update_info.clear()
    updates._update_info.update(saved_info)
    updates._log_ring.clear()
    updates._log_ring.extend(saved_ring)
    updates._changelog_cache = saved_cache
    updates._last_update_check = saved_clock
    updates._check_generation = saved_generation
    updates._check_task = None
    updates._check_task_generation = None


def _request(body: object = None, *, query: dict[str, str] | None = None) -> MagicMock:
    """A request stub: only ``.json()``, ``.query`` and ``app["state"]`` are read."""
    req = MagicMock()

    async def _json() -> object:
        if isinstance(body, Exception):
            raise body
        return body

    req.json = _json
    req.query = dict(query or {})
    state = MagicMock()
    state._background_tasks = set()
    state.owner_id = ""  # no owner configured: the local bootstrap subject is the owner
    req.app = {"state": state}
    _as_owner(req)
    return req


def _as_owner(req: MagicMock) -> None:
    """Give a mock request the dashboard owner's claims, leaving every other key as-is.

    ``POST /api/update`` is owner-gated, so the dashboard-user request these tests
    model carries ``app == ""`` and the owner's subject: ``state.owner_id`` when
    one is configured, else the signed local bootstrap subject.
    """
    owner = str(getattr(req.app["state"], "owner_id", "") or "") or "local-app"
    claims = {"app": "", "user": owner}
    req.__contains__.side_effect = lambda key: key in claims
    req.__getitem__.side_effect = lambda key: claims[key] if key in claims else DEFAULT
    req.get.side_effect = lambda key, *default: claims[key] if key in claims else DEFAULT


class _FakeProc:
    """A stand-in for an ``asyncio`` subprocess that never spawns anything.

    ``time_out=True`` raises on the FIRST ``communicate()`` only: the handler's
    timeout branch kills the process and awaits ``communicate()`` a second time
    to reap it, so a fake that raised every time would mask that reap.
    """

    def __init__(
        self,
        *,
        out: bytes = b"",
        err: bytes = b"",
        returncode: int = 0,
        time_out: bool = False,
        kill_raises: bool = False,
    ) -> None:
        self._out = out
        self._err = err
        self.returncode = returncode
        self._time_out = time_out
        self._kill_raises = kill_raises
        self.communicate_calls = 0
        self.killed = False

    async def communicate(self) -> tuple[bytes, bytes]:
        self.communicate_calls += 1
        if self._time_out and self.communicate_calls == 1:
            raise asyncio.TimeoutError
        return self._out, self._err

    def kill(self) -> None:
        self.killed = True
        if self._kill_raises:
            raise ProcessLookupError("already gone")


def _sequence_procs(monkeypatch, procs: list[_FakeProc | OSError]) -> list[tuple[str, ...]]:
    """Serve *procs* in order to successive ``create_subprocess_exec`` calls.

    An ``OSError`` in the sequence is raised by that spawn instead.
    """
    argv_seen: list[tuple[str, ...]] = []
    pending = list(procs)

    async def _fake_exec(*args: str, **_kwargs: object) -> _FakeProc:
        argv_seen.append(tuple(str(a) for a in args))
        if not pending:
            raise AssertionError(f"unexpected extra subprocess: {args}")
        served = pending.pop(0)
        if isinstance(served, OSError):
            raise served
        return served

    monkeypatch.setattr("asyncio.create_subprocess_exec", _fake_exec)
    return argv_seen


def _git_proj(monkeypatch, tmp_path) -> str:
    """A directory the capability derivation accepts as this install's checkout.

    The ``.git`` directory is the minimal one git ITSELF recognises -- ``HEAD``
    plus empty ``objects/`` and ``refs/`` -- rather than a bare ``.git/HEAD``.
    The derivation asks git first (``rev-parse --show-toplevel``) and only falls
    back to on-disk markers when git cannot answer. A ``.git`` git rejects makes
    it keep walking UP, so the verdict then depends on what sits above
    ``tmp_path``: outside any repository git fails and the fallback accepts the
    markers, but a temp root under a checkout (a developer's ``TMPDIR=./tmp``)
    makes git report THAT checkout's top level, which is not this directory, and
    the fixture reads as "not a checkout". A repository git accepts answers for
    itself wherever it lives. The git lane also requires provenance (the running
    package loads from the tree), which a fabricated directory cannot satisfy,
    so it is declared here rather than derived.
    """
    proj = tmp_path / "checkout"
    proj.mkdir()
    git_dir = proj / ".git"
    (git_dir / "objects").mkdir(parents=True)
    (git_dir / "refs").mkdir()
    (git_dir / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    monkeypatch.setattr(
        "kiro_crew.platform.update_capability.running_from_checkout",
        lambda root, **kw: True,
    )
    return str(proj)
