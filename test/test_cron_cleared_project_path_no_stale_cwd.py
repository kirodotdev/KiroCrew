"""A cleared cron ``project_path`` cannot inherit the directory it was bound to.

``session_allocation``'s resume path restores a key's STORED cwd when the caller
passes none::

    effective_cwd = cwd
    if not effective_cwd and resume_sid:
        stored_cwd = owner._session_map.get_cwd(key)
        if stored_cwd and await asyncio.to_thread(Path(stored_cwd).is_dir):
            effective_cwd = stored_cwd

Clearing a cron job's ``project_path`` is exactly that caller: the fire passes
``cwd=job.project_path or cwd`` and so passes nothing. Were the restore to fire,
the job would keep running in the repository the operator just unbound it from,
and the in-memory ``_cron_session_binding`` re-root could not save it -- that map
is process state, empty after a gateway restart.

It cannot fire, and the reason is a single membership: ``"cron:"`` is in
``session._STATELESS_PREFIXES``, so ``is_stateless`` is true for every cron key,
so ``resume_sid`` is never read, so the guard above is never entered. The
conversation continuity a persistent cron job does have is carried by
``build_cron_session_context`` prepending ``job.last_result`` to the prompt --
NOT by a native resume -- which is why the prefix is stateless in the first place
and why making it resumable would double that context as well as resurrect the
stale directory.

These tests pin that membership by its CONSEQUENCE rather than by asserting the
tuple's contents, so dropping ``"cron:"`` from the tuple, or marking a cron key
continuable (which defeats the same branch via ``_is_continuable_key``), reds the
outcome the membership exists to produce.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.config import KiroCrewConfig
from kiro_crew.session import SessionManager

#: A stable persistent-job key (``build_cron_session_context`` for
#: ``persistent_session=True``) -- the only cron shape that could resume at all.
CRON_KEY = "cron:abc12345"
#: The sequence-path shape, which rides the same prefix.
CRON_SEQUENCE_KEY = "cron:abc12345:repo-bot"
#: A non-stateless key, used only as the control.
DASHBOARD_KEY = "dashboard:chat-9"


@pytest.fixture
def cfg():
    c = KiroCrewConfig()
    c.session.timeout_secs = 2
    return c


async def _empty_provider_stream(_command: str):
    """An empty async iterator for provider methods consumed by ``async for``."""
    if False:  # pragma: no cover - establishes the async-generator protocol
        yield None


def _capturing_factory(seen: dict):
    """A provider factory that records the ``cwd`` it was handed."""

    def factory(session_key=None, agent=None, channel_id=None, **kwargs):
        seen["cwd"] = kwargs.get("cwd")
        m = AsyncMock()
        m.start = AsyncMock()
        m.memory_mode = kwargs.get("memory_mode", "persistent")
        m.shutdown = AsyncMock()
        # Explicit, not AsyncMock-generated: the post-semaphore re-validate calls
        # this synchronously, and an auto-generated coroutine would read as
        # "alive" only by truthiness while leaking an un-awaited coroutine.
        m.is_process_alive = lambda: True
        m.context_usage_pct = lambda: 0.0
        m.context_window_tokens = lambda: 0
        m.has_active_turn = lambda: False
        m.runtime_info = lambda: (None, None)
        m.stream_command = MagicMock(side_effect=_empty_provider_stream)
        return m

    return factory


async def _acquire_with_restore_armed(cfg, key: str, stored_dir) -> dict:
    """ARM the stored-cwd restore fully, acquire *key* with no cwd, report what happened.

    Both readers are stubbed rather than seeded through ``SessionMap.set``:
    ``SessionMap.get`` gates on the transcript file existing and prunes the entry
    as a side effect, so a seeded sid reads back as ``None`` and the branch under
    test is never armed -- the experiment would then "pass" for every key,
    including the control, and measure nothing. Stubbing is symmetric across both
    arms, and the control below is what proves the arming works.
    """
    seen: dict = {}
    mgr = SessionManager(cfg, provider_factory=_capturing_factory(seen))
    mgr._session_map.get = MagicMock(return_value="sid-old")  # type: ignore[method-assign]
    mgr._session_map.get_cwd = MagicMock(return_value=str(stored_dir))  # type: ignore[method-assign]
    try:
        await mgr.get_or_create(key)
    finally:
        seen["resume_lookup_called"] = mgr._session_map.get.called
        await mgr.close_all()
    return seen


class TestClearedCronProjectPathKeepsNoStaleDirectory:
    @pytest.mark.asyncio
    async def test_a_cleared_cron_project_path_does_not_inherit_the_old_directory(
        self, cfg, tmp_path
    ):
        """The OUTCOME: a cron fire passing no cwd is not handed the stored one."""
        unbound_from = tmp_path / "old_repo"
        unbound_from.mkdir()
        seen = await _acquire_with_restore_armed(cfg, CRON_KEY, unbound_from)
        assert seen["cwd"] != str(unbound_from), (
            "a cron key resurrected the directory its project_path was cleared from; "
            'has "cron:" been dropped from session._STATELESS_PREFIXES, or has a cron '
            "key been marked continuable?"
        )
        assert seen["cwd"] is None

    @pytest.mark.asyncio
    async def test_a_cron_sequence_key_does_not_inherit_the_old_directory_either(
        self, cfg, tmp_path
    ):
        """The sequence path mints ``cron:<job id>:<agent>``, which rides the same prefix."""
        unbound_from = tmp_path / "old_repo_seq"
        unbound_from.mkdir()
        seen = await _acquire_with_restore_armed(cfg, CRON_SEQUENCE_KEY, unbound_from)
        assert seen["cwd"] != str(unbound_from)
        assert seen["cwd"] is None

    @pytest.mark.asyncio
    async def test_a_cron_key_never_resolves_a_persisted_resume_sid(self, cfg, tmp_path):
        """The MECHANISM: the restore is guarded by ``resume_sid``, which is never read."""
        stored = tmp_path / "old_repo_mech"
        stored.mkdir()
        seen = await _acquire_with_restore_armed(cfg, CRON_KEY, stored)
        assert seen["resume_lookup_called"] is False

    @pytest.mark.asyncio
    async def test_control_a_dashboard_key_does_restore_its_stored_directory(self, cfg, tmp_path):
        """Control: the same arming DOES restore on a non-stateless key.

        Without this, all three assertions above would also hold if the arming
        were broken, if ``get_or_create`` never reached the branch, or if the
        factory simply never received a ``cwd`` -- so this is what makes them
        evidence about the cron prefix rather than about the harness.
        """
        stored = tmp_path / "old_repo_ctl"
        stored.mkdir()
        seen = await _acquire_with_restore_armed(cfg, DASHBOARD_KEY, stored)
        assert seen["resume_lookup_called"] is True
        assert seen["cwd"] == str(stored)
