"""Shared builders for the ``GatewayOrchestrator`` tests split from ``test_slack_gateway.py``.

``test_slack_gateway.py`` and its ``test_slack_gateway_<topic>.py`` files import
them. ``_make_orchestrator`` builds the orchestrator over stub credentials, and
``_mock_sessions``, ``_mock_context_builder`` and ``_mock_dashboard_state`` stand in
for its collaborators. ``_fake_async_proc`` stands in for a spawned child and
``_git_exec_fake`` answers the git commands an update runs by subcommand.
``_install_effect`` fixes how an update sees the install, and
``_ImportlibWithoutReload`` keeps a completed update from re-importing ``kiro_crew``
in the worker. ``_within_lost_run`` bounds a wait at the lost-run ceiling defined
here, and ``_until_set_while_progressing`` waits for an event, naming the progress
made so far if it never arrives. ``_restore_update_check_cache`` is an
autouse fixture: a test module that imports it hands each next test the
update-check cache the previous one inherited.
"""

from __future__ import annotations

import asyncio
import importlib
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.dashboard.handlers import updates as dashboard_updates
from kiro_crew.slack.gateway import GatewayOrchestrator
from kiro_crew.testing.wait import async_wait_until

#: Lost-run ceiling for a wait the test itself must end (a stub it parks, a
#: writer it holds, a signal production raises on the way). Timed wait by wait,
#: every one of them took at most 0.04 s at ``-n 4`` on a loaded 32-CPU host and
#: at most 0.01 s with every executor hop delayed by 1.2 s (a starved-runner
#: model). The one exception spans a whole update and waits on progress instead
#: (see ``_PROGRESS_WINDOW_SECS``). 60 s is half the module's ``--timeout=120``.
_LOST_RUN_SECS = 60.0

#: The lost-run ceiling of a wait that spans many executor hops: how long it may go
#: without a hop being submitted or finishing. Over ten times one hop's 1.2 s under
#: the starved-runner model. The update ahead of the queued reinstall makes about
#: nine such hops (the whole test took 10.9 s under that model, 0.04 s at ``-n 4``),
#: so a slow pool keeps the wait alive while a wedged one fails within one window.
#: Such a wait is also capped at ``_LOST_RUN_SECS`` in all: a livelock backstop held
#: at the half-timeout limit, not a race bound.
_PROGRESS_WINDOW_SECS = 15.0


async def _until_set_while_progressing(event, progress, what: str) -> None:
    """Wait for *event* while ``progress()`` keeps changing."""
    await async_wait_until(
        event.is_set,
        describe=lambda: f"{what}: {progress()} step(s) so far",
    )


async def _within_lost_run(awaitable, what: str, *, timeout: float = _LOST_RUN_SECS):
    """Await *awaitable* under a lost-run ceiling; fail naming *what* on expiry.

    A bare ``wait_for`` raises an empty ``TimeoutError``. This one says what never
    finished, the ceiling and the elapsed time. A ``TimeoutError`` the awaited code
    raises on its own, before the ceiling, propagates unchanged.
    """
    # flake-ok: pre-existing monotonic lost-run ceiling, moved verbatim; tracked for a de-flake pass
    started = time.monotonic()
    try:
        return await asyncio.wait_for(awaitable, timeout)
    except asyncio.TimeoutError:
        # flake-ok: pre-existing monotonic lost-run ceiling, moved verbatim; tracked for a de-flake pass
        elapsed = time.monotonic() - started
        # A loop timer can fire up to one clock tick early (15.6 ms on Windows 3.12).
        # flake-ok: pre-existing lost-run ceiling check, moved verbatim; tracked for a de-flake pass
        if elapsed < timeout - 1.0:
            raise
        pytest.fail(
            f"{what} did not finish within the {timeout:.0f}s lost-run ceiling "
            f"({elapsed:.1f}s elapsed)"
        )


class _ImportlibWithoutReload:
    """``importlib`` as the gateway sees it, except that ``reload`` returns its argument.

    A completed update re-imports ``kiro_crew`` to log the rebuilt version. In a
    test that re-runs ``kiro_crew/__init__.py`` inside the worker's one package
    and leaves a NEW ``kiro_crew.shutdown_event`` there, while every module that
    imported the name keeps the old object. A later test on the same worker that
    sets ``kiro_crew.shutdown_event`` then never wakes ``gateway.run()``:
    ``TestRunMethod::test_run_raises_on_shutdown`` hung to ``--timeout`` behind
    any update test that completes an update (15 tests in the venv and reset
    classes). Only the gateway's binding is replaced.
    """

    def reload(self, module):
        return module

    def __getattr__(self, name):
        return getattr(importlib, name)


@pytest.fixture(autouse=True)
def _restore_update_check_cache():
    """Hand the next test the update-check cache this one inherited.

    ``dashboard.handlers.updates._update_info`` is one process-wide dict, and the
    gateway imported it by identity, so it is restored in place, never rebound. Tests
    here clear it and seed their own verdicts, and the provider path writes
    ``managed_by="command"`` into it; left behind, that key makes a later legacy check
    on the same worker read "a policy update command owns this install" and only
    notify, so an apply the test expects never runs.
    """
    inherited = dict(dashboard_updates._update_info)
    yield
    dashboard_updates._update_info.clear()
    dashboard_updates._update_info.update(inherited)


def _install_effect(effect: str = "install", route: str | None = "git"):
    """State the install's shape the way the update loop reads it.

    The loop branches on ``auto_update_effect``, not on the check's cached
    ``can_apply``; a test standing in for a git checkout says so here instead of
    hoping the real derivation agrees with a fake tree.
    """
    from kiro_crew.platform.update_capability import AutoUpdateEffect

    return patch(
        "kiro_crew.slack.gateway.auto_update_effect",
        return_value=AutoUpdateEffect(effect, route),
    )


def _make_orchestrator(
    *,
    slack_enabled: bool = False,
    owner_id: str = "U_OWNER",
    no_dashboard: bool = False,
    no_crons: bool = False,
    no_open: bool = False,
    test_mode: bool = False,
) -> GatewayOrchestrator:
    """Build a GatewayOrchestrator with mocked credentials."""
    cfg = KiroCrewConfig()
    creds: dict[str, str] = {}
    if slack_enabled:
        creds = {
            "SLACK_APP_TOKEN": "xapp-test",
            "SLACK_BOT_TOKEN": "xoxb-test",
            "KIROCREW_OWNER_ID": owner_id,
        }
    else:
        if owner_id:
            creds["KIROCREW_OWNER_ID"] = owner_id
    with patch.object(cfg, "load_credentials", return_value=creds):
        orch = GatewayOrchestrator(
            cfg,
            no_dashboard=no_dashboard,
            no_crons=no_crons,
            no_open=no_open,
            test_mode=test_mode,
        )
    return orch


def _fake_async_proc(returncode: int = 0, stdout: bytes = b"", stderr: bytes = b""):
    """A stand-in for an ``asyncio.create_subprocess_exec`` child process."""
    proc = MagicMock()
    proc.returncode = returncode
    proc.communicate = AsyncMock(return_value=(stdout, stderr))
    proc.kill = MagicMock()
    return proc


def _mock_sessions():
    """Return a mock SessionManager with common methods."""
    s = MagicMock()
    s.get_or_create = AsyncMock(return_value=(MagicMock(), True, False))
    s.release = MagicMock()
    s.reset = AsyncMock()
    s.cancel_current = AsyncMock()
    s.get_channel = MagicMock(return_value=None)
    s.get_thread = MagicMock(return_value=None)
    s.set_thread = AsyncMock()
    s.set_channel = AsyncMock()
    s.start_pool = AsyncMock()
    s.close_all = AsyncMock()
    s.recycle_background = AsyncMock()
    # ``_drain_update_callback_work`` polls ``int(sessions.inbound_callback_count)``
    # until it reaches 0 or its 30 s deadline. A bare MagicMock attribute answers
    # ``int(...) == 1`` FOREVER, so every auto-apply-update test that reached the
    # drain sat out the full 30 s (12 tests, 6 min per run) and then silently
    # exercised the "restart deferred" branch instead of the restart it named.
    s.inbound_callback_count = 0
    return s


def _mock_context_builder():
    """Return a context builder with readable, unbound session metadata."""
    builder = MagicMock()
    builder.conversation_log.get_metadata_status.return_value = ({}, True)
    return builder


def _mock_dashboard_state():
    """Return a mock DashboardState."""
    ds = MagicMock()
    ds._slots = {}
    ds._yolo = False
    ds.notify = MagicMock()
    ds.push_slots_update = MagicMock()
    ds.push_refresh = MagicMock()
    ds.broadcast_ws = MagicMock()
    ds.broadcast_ws_subagent_subscribers = MagicMock()
    ds.request_approval = AsyncMock(return_value=True)
    ds.resolve_approval = MagicMock()
    ds.resolve_slot = MagicMock(return_value=None)
    ds.get_slot = MagicMock(return_value=None)
    ds.get_or_create_slot = MagicMock()
    ds.close_all_ws = AsyncMock()
    ds.file_indexes = MagicMock()
    ds.file_indexes.stop_all = MagicMock()
    ds._background_tasks = set()
    ds.clear_update_progress = MagicMock()
    ds.push_update_progress = MagicMock()
    return ds


def _git_exec_fake(
    *,
    branch: bytes = b"mainline\n",
    fetch_rc: int = 0,
    diff_rc: int = 1,
    status_out: bytes = b"",
    status_rc: int = 0,
    reset_rc: int = 0,
    other_rc: int = 0,
    target: bytes = b"0" * 40 + b"\n",
    added_out: bytes = b"",
    added_rc: int = 0,
    record: list | None = None,
):
    """A `create_subprocess_exec` fake that dispatches on the GIT SUBCOMMAND.

    Deliberately NOT a call counter. `_auto_apply_update` has gained git calls
    three times, and each time every counter-keyed fake silently re-mapped its
    own cases -- the stub written for `fetch` started answering the `diff`, and
    the tests failed for a reason that had nothing to do with what they assert.
    Dispatching on argv keeps each stub bound to the command it describes, so
    inserting a call is not a test edit.

    Defaults describe the interesting path: a branch is detected, the fetch
    succeeds, the diff reports changes (rc=1), the tree is clean, the target adds
    no colliding paths, the reset succeeds, and non-git spawns succeed.
    `other_rc` fails the latter while leaving every git step green.

    Note that `git diff` is now TWO distinct calls with opposite rc conventions,
    so this dispatches on their flags. Subcommand alone is not always enough --
    when a new call reuses a subcommand, the discriminator has to get narrower.
    """

    async def _fake(*args, **kwargs):
        if record is not None:
            record.append(args)
        argv = [a for a in args if isinstance(a, str)]
        proc = AsyncMock()
        proc.kill = MagicMock()
        out: bytes = b""
        rc = 0
        if "rev-parse" in argv and "--abbrev-ref" in argv:
            out = branch
        elif "rev-parse" in argv:
            out = target
        elif "rev-list" in argv:
            out = b"0\n"
        elif "fetch" in argv:
            rc = fetch_rc
        elif "diff" in argv and any(a.startswith("--diff-filter") for a in argv):
            # The added-paths listing before the reset. Distinguished by its
            # flags, not by call order: the `--quiet` check below uses rc as a
            # BOOLEAN ("there are differences"), while this one uses rc for
            # success and returns paths on stdout -- so answering both from one
            # `"diff" in argv` branch made every happy-path test refuse.
            out = added_out
            rc = added_rc
        elif "diff" in argv:
            rc = diff_rc
        elif "status" in argv:
            out = status_out
            rc = status_rc
        elif "reset" in argv:
            rc = reset_rc
        else:
            # Not a git step: the dependency install, the core-dep repair, the
            # optional kiro-cli update.
            rc = other_rc
        proc.returncode = rc
        proc.communicate = AsyncMock(return_value=(out, b""))
        proc.wait = AsyncMock(return_value=rc)
        return proc

    return _fake
