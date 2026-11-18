"""AcpSessionProvider.new_conversation — unconfirmed-teardown registration.

Covers the gap where terminate_session swallows a timeout, so destroy() returns
normally while the session_id remains in _unconfirmed_teardowns.  The post-destroy
check detects this and calls note_unconfirmed_close so the drain loop can retry.
"""

from __future__ import annotations

import unittest.mock
from unittest.mock import AsyncMock

import pytest

# Re-use the helpers from the main provider test module so the runtime mock
# matches what new_conversation() expects (acp_backend in ACP_BACKENDS_SESSION_EVICTION).
from test_acp_session_provider import _make_handle, _make_runtime

from kiro_crew.acp.client import AcpError
from kiro_crew.acp.session_provider import AcpSessionProvider


def _runtime_with_new_session(alive: bool = True):
    runtime = _make_runtime(alive=alive)
    runtime._work_dir = "/tmp/ws"
    runtime._agent = "kirocrew"
    runtime._unconfirmed_teardowns = set()
    new_handle = _make_handle(session_id="fresh-session-unconf")
    runtime.create_session = AsyncMock(return_value=new_handle)
    return runtime, new_handle


@pytest.mark.asyncio
async def test_old_session_destroy_silent_timeout_registers_unconfirmed_close():
    """When old.destroy() returns normally but the session is still unconfirmed
    (terminate_session swallowed a timeout), note_unconfirmed_close must be called.

    terminate_session catches internal timeouts, so destroy() returns without
    raising while the session_id remains in _unconfirmed_teardowns.  The
    post-destroy check detects this and registers the session for retry.

    Remove the post-destroy _unconfirmed_teardowns check and ``recorded``
    stays empty even though the session is still unconfirmed.
    """
    old = _make_handle(session_id="old-silent-timeout-sid")
    # destroy() returns normally (terminate_session swallowed the timeout).
    old.destroy = AsyncMock(return_value=None)
    runtime, new_handle = _runtime_with_new_session()
    # Simulate the runtime still holding the old session as unconfirmed.
    runtime._unconfirmed_teardowns = {old.session_id}
    provider = AcpSessionProvider(old, runtime)

    recorded: list[tuple] = []

    def _record(session_key, rt, sid):
        recorded.append((session_key, rt, sid))

    with unittest.mock.patch(
        "kiro_crew.bot_chat_app_tools.note_unconfirmed_close", side_effect=_record
    ):
        await provider.new_conversation()

    assert any(sid == "old-silent-timeout-sid" for _, _, sid in recorded), (
        "old session must be registered via note_unconfirmed_close when destroy() "
        "returns normally but the session_id remains in _unconfirmed_teardowns; "
        "remove the post-destroy check and recorded stays empty"
    )


@pytest.mark.asyncio
async def test_an_old_handle_without_a_session_id_is_kept_unconfirmed():
    """A replaced handle that has no ``session_id`` must not crash the swap, and
    its close is not confirmed. Read ``old.session_id`` directly and this raises
    AttributeError after the new conversation is already in place."""
    old = _make_handle(session_id="unused")
    del old.session_id
    old.destroy = AsyncMock(return_value=None)
    runtime, new_handle = _runtime_with_new_session()
    provider = AcpSessionProvider(old, runtime)

    recorded: list[tuple] = []

    def _record(session_key, rt, sid):
        recorded.append((session_key, rt, sid))

    with unittest.mock.patch(
        "kiro_crew.bot_chat_app_tools.note_unconfirmed_close", side_effect=_record
    ):
        await provider.new_conversation()

    assert provider._handle is new_handle
    assert [sid for _, _, sid in recorded] == [None]


@pytest.mark.asyncio
async def test_rejected_replacement_destroy_silent_timeout_registers_unconfirmed_close():
    """When new_handle.destroy() returns normally but the rejected session is
    still unconfirmed, note_unconfirmed_close must be called.

    terminate_session catches internal timeouts, so destroy() returns without
    raising while the session_id remains in _unconfirmed_teardowns.  The
    post-destroy check detects this and registers the session for retry.

    Remove the post-destroy _unconfirmed_teardowns check and ``recorded``
    stays empty even though the rejected session is still unconfirmed.
    """
    old = _make_handle(session_id="old-session-rejected")
    old.model = "claude-opus-4-8"
    runtime, new_handle = _runtime_with_new_session()
    new_handle.set_model = AsyncMock(side_effect=RuntimeError("set_model boom"))
    # destroy() returns normally (terminate_session swallowed the timeout).
    new_handle.destroy = AsyncMock(return_value=None)
    # Simulate the runtime still holding the rejected session as unconfirmed.
    runtime._unconfirmed_teardowns = {new_handle.session_id}
    provider = AcpSessionProvider(old, runtime)

    recorded: list[tuple] = []

    def _record(session_key, rt, sid):
        recorded.append((session_key, rt, sid))

    with unittest.mock.patch(
        "kiro_crew.bot_chat_app_tools.note_unconfirmed_close", side_effect=_record
    ):
        with pytest.raises(AcpError):
            await provider.new_conversation()

    assert any(sid == new_handle.session_id for _, _, sid in recorded), (
        "rejected replacement must be registered via note_unconfirmed_close when "
        "destroy() returns normally but session_id remains in _unconfirmed_teardowns; "
        "remove the post-destroy check and recorded stays empty"
    )


# ---------------------------------------------------------------------------
# F1: AcpSessionProvider.shutdown — process tree confirmation gate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_shutdown_confirms_unconfirmed_when_process_tree_fully_dead(monkeypatch):
    """shutdown() must call confirm_last_lease_killed when kill() completes AND
    process_tree_confirmed_dead is True.

    Remove the ``last.process_tree_confirmed_dead`` check (revert to confirming
    unconditionally after kill()) and confirm_called still fires here, but the
    companion test below (tree not confirmed) would then also fire -- suppressing
    the stop-failure warning for a detached descendant.
    """
    import contextlib
    import unittest.mock
    from types import SimpleNamespace

    old = _make_handle(session_id="shared-confirmed-sid")
    old.cancel = AsyncMock()
    old.destroy = AsyncMock()
    old.memory_mode = "persistent"
    old.kas_registered_agents = []
    old.kas_launch_snapshots = []
    runtime, _new = _runtime_with_new_session()
    runtime.acp_backend = "kas"
    runtime.keep_record_on_teardown = contextlib.contextmanager(lambda sid: (yield))

    provider = AcpSessionProvider(old, runtime)
    provider._shared_runtime = True
    provider._kas_shutdown_unconfirmed = True
    provider._kas_unconfirmed_session_id = "shared-confirmed-sid"

    last_runtime = SimpleNamespace(
        kill=AsyncMock(),
        process_tree_confirmed_dead=True,
    )

    async def _release(lease):
        return last_runtime

    confirm_called: list = []
    with unittest.mock.patch("kiro_crew.acp.session_provider_shared.RUNTIME_OWNERSHIP") as mock_ro:
        mock_ro.release = _release
        provider._runtime_lease = "fake-lease"
        with unittest.mock.patch(
            "kiro_crew.bot_chat_app_tools.confirm_last_lease_killed",
            side_effect=lambda sid: confirm_called.append(sid),
        ):
            await provider.shutdown()

    assert "shared-confirmed-sid" in confirm_called, (
        "confirm_last_lease_killed must run when process_tree_confirmed_dead is True; "
        "remove the tree-confirmation check and the call still happens, but the NEXT "
        "test (tree not confirmed) would also fire, suppressing stop-failure reporting"
    )
    assert provider._kas_shutdown_unconfirmed is False


@pytest.mark.asyncio
async def test_shutdown_preserves_unconfirmed_when_process_tree_not_fully_dead(monkeypatch):
    """shutdown() must NOT call confirm_last_lease_killed when kill() completes but
    process_tree_confirmed_dead is False (a detached app-server descendant survived).

    Remove the ``last.process_tree_confirmed_dead`` check (revert to confirming
    unconditionally after kill()) and confirm_called gets the sid even with
    process_tree_confirmed_dead=False, suppressing the stop-failure warning.
    """
    import contextlib
    import unittest.mock
    from types import SimpleNamespace

    old = _make_handle(session_id="shared-unconfirmed-sid")
    old.cancel = AsyncMock()
    old.destroy = AsyncMock()
    old.memory_mode = "persistent"
    old.kas_registered_agents = []
    old.kas_launch_snapshots = []
    runtime, _new = _runtime_with_new_session()
    runtime.acp_backend = "kas"
    runtime.keep_record_on_teardown = contextlib.contextmanager(lambda sid: (yield))

    provider = AcpSessionProvider(old, runtime)
    provider._shared_runtime = True
    provider._kas_shutdown_unconfirmed = True
    provider._kas_unconfirmed_session_id = "shared-unconfirmed-sid"

    last_runtime = SimpleNamespace(
        kill=AsyncMock(),
        process_tree_confirmed_dead=False,
    )

    async def _release(lease):
        return last_runtime

    confirm_called: list = []
    with unittest.mock.patch("kiro_crew.acp.session_provider_shared.RUNTIME_OWNERSHIP") as mock_ro:
        mock_ro.release = _release
        provider._runtime_lease = "fake-lease"
        with unittest.mock.patch(
            "kiro_crew.bot_chat_app_tools.confirm_last_lease_killed",
            side_effect=lambda sid: confirm_called.append(sid),
        ):
            await provider.shutdown()

    assert confirm_called == [], (
        "confirm_last_lease_killed must NOT run when process_tree_confirmed_dead is False; "
        "a detached descendant may still be running. Remove the tree-confirmation check "
        "and confirm_called gets the sid, suppressing the stop-failure warning."
    )
    assert (
        provider._kas_shutdown_unconfirmed is True
    ), "_kas_shutdown_unconfirmed must remain True when tree not confirmed"


# ---------------------------------------------------------------------------
# F2: Owned-runtime kill failure sets _kas_shutdown_unconfirmed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_owned_runtime_kill_failure_sets_unconfirmed_flag():
    """AcpSessionProvider.shutdown must set _kas_shutdown_unconfirmed when the
    owned-runtime kill raises, so discard_conversation returns False on
    confirm_teardown=True callers (trust withdrawal, disable, uninstall).

    Remove the ``self._kas_shutdown_unconfirmed = True`` line in the
    except-Exception block and _kas_shutdown_unconfirmed stays False even
    though the process tree was not confirmed dead.
    """
    old = _make_handle(session_id="owned-kill-fail-sid")
    runtime, _ = _runtime_with_new_session()
    runtime._unconfirmed_teardowns = set()

    provider = AcpSessionProvider(old, runtime)
    provider._owns_runtime = True
    provider._shared_runtime = False
    # make kill() raise so the exception path runs
    runtime.kill = AsyncMock(side_effect=RuntimeError("WindowsTreeDrainPending"))
    # release_runtime_lease must succeed
    provider.release_runtime_lease = AsyncMock()

    await provider.shutdown()

    assert getattr(provider, "_kas_shutdown_unconfirmed", False) is True, (
        "_kas_shutdown_unconfirmed must be True when owned-runtime kill fails; "
        "remove the flag assignment and discard_conversation (teardown.py) "
        "returns True (confirmed) even though the process tree was not dead."
    )


# ---------------------------------------------------------------------------
# B-3: note_unconfirmed_close only called when key is in _MOUNTED_SESSION_KEYS
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_discard_conversation_skips_note_unconfirmed_close_when_key_not_mounted():
    """discard_conversation must NOT call note_unconfirmed_close when the session key
    is not in _MOUNTED_SESSION_KEYS (session never mounted any app server).

    Remove the ``if key in _MOUNTED_SESSION_KEYS.values():`` guard (revert to calling
    note_unconfirmed_close unconditionally) and note_called becomes non-empty for
    sessions that never had an app server -- registering spurious close-retry timers.
    """
    import unittest.mock
    from types import SimpleNamespace

    import kiro_crew.bot_chat_app_tools as tools

    old = _make_handle(session_id="not-mounted-sid")
    old.cancel = AsyncMock()
    old.destroy = AsyncMock()
    old.memory_mode = "persistent"
    old.kas_registered_agents = []
    old.kas_launch_snapshots = []
    runtime, _ = _runtime_with_new_session()
    provider = AcpSessionProvider(old, runtime)
    # Simulate an unconfirmed close (shared KAS path)
    provider._kas_shutdown_unconfirmed = True
    provider._kas_unconfirmed_session_id = "not-mounted-sid"
    provider._kas_unconfirmed_runtime = SimpleNamespace()

    note_called: list[str] = []

    # _MOUNTED_SESSION_KEYS has NO entry for KEY "not-mounted-key" -- not mounted
    with unittest.mock.patch.dict(tools._MOUNTED_SESSION_KEYS, {}, clear=True):
        with unittest.mock.patch(
            "kiro_crew.bot_chat_app_tools.note_unconfirmed_close",
            side_effect=lambda key, rt, sid: note_called.append(key),
        ):
            # Drive discard_conversation via provider.shutdown (simplest path)
            provider._shared_runtime = True
            provider._runtime_lease = None
            await provider.shutdown()

    assert note_called == [], (
        "note_unconfirmed_close must NOT be called when the session key is not in "
        "_MOUNTED_SESSION_KEYS; without the guard, spurious retry timers fire for "
        "every discarded session even without app servers (First Principles item 10)."
    )


# ---------------------------------------------------------------------------
# Rule 2: owned-runtime kill failure retains unconfirmed handle for retry
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_owned_runtime_kill_failure_retains_unconfirmed_runtime_and_session_id():
    """When owned-runtime kill raises, session_provider must set both
    _kas_unconfirmed_runtime and _kas_unconfirmed_session_id so that
    discard_conversation can call note_unconfirmed_close and register
    the handle in _PENDING_CLOSE for retry.

    Remove the two new assignments (``self._kas_unconfirmed_runtime = ...`` and
    ``self._kas_unconfirmed_session_id = ...``) and both fields stay None;
    teardown.py:975 skips note_unconfirmed_close because rt is None,
    so the owned-kill failure is silently lost and no retry is registered.
    """
    old = _make_handle(session_id="owned-kill-sid-rule2")
    runtime, _ = _runtime_with_new_session()
    runtime._unconfirmed_teardowns = set()

    provider = AcpSessionProvider(old, runtime)
    provider._owns_runtime = True
    provider._shared_runtime = False
    runtime.kill = AsyncMock(side_effect=RuntimeError("WindowsTreeDrainPending"))
    provider.release_runtime_lease = AsyncMock()

    await provider.shutdown()

    assert (
        getattr(provider, "_kas_shutdown_unconfirmed", False) is True
    ), "_kas_shutdown_unconfirmed must be True when owned kill fails"
    assert getattr(provider, "_kas_unconfirmed_runtime", None) is not None, (
        "_kas_unconfirmed_runtime must be set when kill fails; "
        "without it teardown.py:975 skips note_unconfirmed_close and no retry is registered"
    )
    assert getattr(provider, "_kas_unconfirmed_session_id", None) is not None, (
        "_kas_unconfirmed_session_id must be set when kill fails; "
        "without it teardown.py:975 skips note_unconfirmed_close and no retry is registered"
    )


# ---------------------------------------------------------------------------
# Rule 2: only a whole tree confirmed dead settles an owned-runtime kill
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("tree_dead", [False, True])
async def test_owned_runtime_kill_that_returns_is_confirmed_only_by_tree_death(tree_dead):
    """``kill()`` returning normally with ``process_tree_confirmed_dead=False`` (a
    descendant left the process group) is an unconfirmed close: the flag, the runtime
    and the session id are kept and the handle is registered for retry. With the tree
    confirmed dead nothing is kept.

    Flag the owned path only when ``kill()`` raises and the ``False`` case keeps
    ``_kas_shutdown_unconfirmed`` False, so this fails.
    """
    old = _make_handle(session_id="owned-kill-returns-sid")
    runtime, _ = _runtime_with_new_session()
    runtime.kill = AsyncMock(return_value=None)
    runtime.process_tree_confirmed_dead = tree_dead
    provider = AcpSessionProvider(old, runtime)
    provider._owns_runtime = True
    provider._shared_runtime = False
    provider.release_runtime_lease = AsyncMock()

    recorded: list[tuple] = []
    with unittest.mock.patch(
        "kiro_crew.bot_chat_app_tools.note_unconfirmed_close",
        side_effect=lambda key, rt, sid: recorded.append((rt, sid)),
    ):
        await provider.shutdown()

    if tree_dead:
        assert provider._kas_shutdown_unconfirmed is False
        assert recorded == []
    else:
        assert provider._kas_shutdown_unconfirmed is True
        assert provider._kas_unconfirmed_runtime is runtime
        assert provider._kas_unconfirmed_session_id == "owned-kill-returns-sid"
        assert recorded == [(runtime, "owned-kill-returns-sid")]


@pytest.mark.asyncio
async def test_a_kill_with_no_process_keeps_the_tree_verdict_the_reaping_kill_wrote(tmp_path):
    """A second ``kill()`` on a runtime whose process is already reaped keeps the
    verdict of the kill that reaped it, so a confirmed-dead tree is not re-read as
    unconfirmed (which would leave a Bot's close pending forever) and an unconfirmed
    one is not read as dead.

    Reset ``_process_tree_confirmed_dead`` unconditionally at the top of ``kill()``
    and the ``True`` verdict reads ``False`` after the second kill, so this fails.
    """
    from kiro_crew.acp.runtime import AcpRuntime

    for verdict in (True, False):
        rt = AcpRuntime(work_dir=str(tmp_path))
        rt._process = None
        rt._process_tree_confirmed_dead = verdict
        await rt.kill(expected=True, reason="second kill")
        assert rt.process_tree_confirmed_dead is verdict


@pytest.mark.parametrize(
    ("killed", "tree_dead", "unconfirmed", "expected"),
    [
        (False, False, set(), True),  # close acknowledged or host answered absent
        (False, False, {"s"}, False),  # close timed out or was refused
        (True, True, {"s"}, True),  # whole tree confirmed dead
        (True, False, set(), False),  # kill returned, a descendant survived
    ],
)
def test_teardown_confirmed_accepts_only_the_three_answers(
    killed, tree_dead, unconfirmed, expected
):
    """The one judge shared and owned runtimes use. Make it trust a kill that
    returned, or ignore ``_unconfirmed_teardowns``, and a row fails."""
    from types import SimpleNamespace

    from kiro_crew.acp.session_provider_shared import teardown_confirmed

    rt = SimpleNamespace(process_tree_confirmed_dead=tree_dead, _unconfirmed_teardowns=unconfirmed)
    assert teardown_confirmed(rt, "s", killed=killed) is expected


@pytest.mark.parametrize("sid", [None, ""])
def test_a_close_with_no_session_id_is_never_confirmed(sid):
    """A handle with no id has no host answer to read. Drop the empty-id check in
    ``teardown_confirmed`` and ``None not in set()`` reads as a confirmed close."""
    from types import SimpleNamespace

    from kiro_crew.acp.session_provider_shared import teardown_confirmed

    rt = SimpleNamespace(process_tree_confirmed_dead=False, _unconfirmed_teardowns=set())
    assert teardown_confirmed(rt, sid, killed=False) is False


def test_an_unconfirmed_close_with_no_session_id_is_kept_under_a_token(monkeypatch):
    """Drop the token fallback in ``note_unconfirmed_close`` and the retry is
    queued under ``None``, which no host can be asked to close."""
    from types import SimpleNamespace

    import kiro_crew.bot_chat_app_tools as tools

    monkeypatch.setattr(tools, "_schedule_pending_close_drain", lambda *_a: None)
    tools._SESSION_APP_SNAPSHOT_PENDING["chat-no-sid"] = ("slug", frozenset(["app"]))
    rt = SimpleNamespace()

    tools.note_unconfirmed_close("chat-no-sid", rt, None)

    assert tools._PENDING_CLOSE[("slug", "app")] == [(rt, f"unconfirmed-teardown:{id(rt)}")]


# ---------------------------------------------------------------------------
# Rule 2: every destroy branch is judged by teardown_confirmed
# ---------------------------------------------------------------------------


def _destroy_provider(backend: str, *, shared: bool, outcome: str):
    """A provider on *backend* whose destroy left the close *outcome*:
    ``confirmed``, ``timed_out`` (terminate recorded the id as unconfirmed) or
    ``raised``."""
    sid = f"{backend}-{'shared' if shared else 'sub'}-sid"
    handle = _make_handle(session_id=sid)
    handle.memory_mode = "persistent"
    handle.kas_registered_agents = []
    handle.kas_launch_snapshots = []
    runtime, _ = _runtime_with_new_session()
    runtime.acp_backend = backend
    runtime._unconfirmed_teardowns = set()

    async def _destroy() -> None:
        if outcome == "timed_out":
            runtime._unconfirmed_teardowns.add(sid)
        elif outcome == "raised":
            raise RuntimeError("destroy failed")

    handle.destroy = AsyncMock(side_effect=_destroy)
    handle.is_turn_active = False
    provider = AcpSessionProvider(handle, runtime)
    provider._owns_runtime = False
    provider._shared_runtime = shared
    provider._runtime_lease = None  # another lease keeps the runtime alive
    return provider, runtime, sid


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["kas", "claude", "codex"])
@pytest.mark.parametrize("shared", [True, False])
@pytest.mark.parametrize("outcome", ["confirmed", "timed_out", "raised"])
async def test_every_destroy_branch_keeps_an_unconfirmed_close(backend, shared, outcome):
    """Shared runtime (KAS record-keeping branch and the non-KAS branch) and the
    unshared co-tenant branch: a close the host did not confirm, or a destroy
    that raised, keeps the unconfirmed flag and registers the handle; a confirmed
    close keeps nothing. Check only the KAS branch (or ignore a raised destroy)
    and the claude/codex or ``raised`` rows fail."""
    provider, runtime, sid = _destroy_provider(backend, shared=shared, outcome=outcome)
    recorded: list[tuple] = []
    with unittest.mock.patch(
        "kiro_crew.bot_chat_app_tools.note_unconfirmed_close",
        side_effect=lambda key, rt, s: recorded.append((rt, s)),
    ):
        await provider.shutdown()

    if outcome == "confirmed":
        assert getattr(provider, "_kas_shutdown_unconfirmed", False) is False
        assert recorded == []
    else:
        assert provider._kas_shutdown_unconfirmed is True
        assert provider._kas_unconfirmed_runtime is runtime
        assert recorded == [(runtime, sid)]


@pytest.mark.asyncio
@pytest.mark.parametrize("tree_dead", [True, False])
async def test_terminate_on_a_dead_runtime_is_confirmed_only_by_tree_death(tmp_path, tree_dead):
    """``terminate_session`` on a dead runtime sends nothing; the close counts only
    when the process exited and its whole tree is confirmed dead. Skip the
    dead-runtime check and the unconfirmed row reads confirmed."""
    from kiro_crew.acp.runtime import AcpRuntime

    rt = AcpRuntime(work_dir=str(tmp_path))
    rt._dead = True
    rt._process = None
    rt._process_tree_confirmed_dead = tree_dead
    await rt.terminate_session("sid-dead")
    assert ("sid-dead" in rt._unconfirmed_teardowns) is (not tree_dead)


@pytest.mark.asyncio
@pytest.mark.parametrize("verb", ["terminate_session", "evict_session"])
async def test_a_dead_runtime_whose_process_cannot_say_is_unconfirmed(tmp_path, verb):
    """A dead runtime holding a process object with no ``returncode`` is not known to
    have exited: both teardown verbs answer unconfirmed instead of raising. Read
    ``returncode`` off the object directly and this raises AttributeError."""
    from kiro_crew.acp.runtime import AcpRuntime

    rt = AcpRuntime(work_dir=str(tmp_path))
    rt._dead = True
    rt._process = object()  # type: ignore[assignment]
    rt._process_tree_confirmed_dead = True
    result = await getattr(rt, verb)("sid-unknown")
    if verb == "evict_session":
        assert result is False
    assert "sid-unknown" in rt._unconfirmed_teardowns


@pytest.mark.asyncio
async def test_old_session_destroy_failure_registers_unconfirmed_close():
    """When old.destroy() fails during new_conversation, the replaced session's
    ID must be registered via note_unconfirmed_close so that a later trust
    revocation can retry closing it.

    Remove the note_unconfirmed_close call in new_conversation's except block
    and ``recorded`` stays empty.
    """
    old = _make_handle(session_id="old-replaced-sid")
    old.destroy = AsyncMock(side_effect=RuntimeError("old close timed out"))
    runtime, new_handle = _runtime_with_new_session()
    provider = AcpSessionProvider(old, runtime)

    recorded: list[tuple] = []

    def _record(session_key, rt, sid):
        recorded.append((session_key, rt, sid))

    with unittest.mock.patch(
        "kiro_crew.bot_chat_app_tools.note_unconfirmed_close", side_effect=_record
    ):
        await provider.new_conversation()  # must NOT raise

    assert provider._handle is new_handle, "new handle must be committed after destroy failure"
    assert any(sid == "old-replaced-sid" for _, _, sid in recorded), (
        "old session must be registered via note_unconfirmed_close after destroy failure; "
        "remove the call and recorded stays empty"
    )


@pytest.mark.asyncio
async def test_rejected_replacement_destroy_failure_registers_unconfirmed_close():
    """When new_handle.destroy() fails after a set_model failure, the rejected
    replacement session's ID must be registered via note_unconfirmed_close.

    Remove the note_unconfirmed_close call in new_conversation's inner except
    block and ``recorded`` stays empty.
    """
    old = _make_handle(session_id="old-session-1")
    old.model = "claude-opus-4-8"
    runtime, new_handle = _runtime_with_new_session()
    new_handle.set_model = AsyncMock(side_effect=RuntimeError("set_model boom"))
    new_handle.destroy = AsyncMock(side_effect=RuntimeError("destroy also boom"))
    provider = AcpSessionProvider(old, runtime)

    recorded: list[tuple] = []

    def _record(session_key, rt, sid):
        recorded.append((session_key, rt, sid))

    with unittest.mock.patch(
        "kiro_crew.bot_chat_app_tools.note_unconfirmed_close", side_effect=_record
    ):
        with pytest.raises(AcpError):
            await provider.new_conversation()

    assert any(sid == new_handle.session_id for _, _, sid in recorded), (
        "rejected replacement must be registered via note_unconfirmed_close; "
        "remove the call and recorded stays empty"
    )
