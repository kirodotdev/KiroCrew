"""Shared pytest configuration and fixtures."""

from __future__ import annotations

import asyncio
import os
import shutil
import sys

import pytest
from hypothesis import HealthCheck, settings

from kiro_crew import sel as _sel
from kiro_crew.safety_override import reset_singleton as _reset_safety_override
from kiro_crew.slack.client import SlackClientOps
from kiro_crew.slack.handler import _PHASE_EMOJIS, _build_phase_emojis

# ── Hypothesis profiles ─────────────────────────────────────────────────
# Default (CI): fast iteration.  Run ``HYPOTHESIS_PROFILE=thorough brazil-build test``
# for deeper coverage.
settings.register_profile("default", max_examples=20, suppress_health_check=[HealthCheck.too_slow], deadline=None)
settings.register_profile("thorough", max_examples=100)
settings.load_profile(os.getenv("HYPOTHESIS_PROFILE", "default"))

# Ensure .hypothesis/tmp exists (build environment may not have it)
os.makedirs(os.path.join(os.path.dirname(__file__), "..", ".hypothesis", "tmp"), exist_ok=True)

_HAS_GIT = shutil.which("git") is not None

requires_git = pytest.mark.skipif(not _HAS_GIT, reason="git not available")


@pytest.fixture(autouse=True)
def _isolate_aim_skills_dir(tmp_path, monkeypatch):
    """Prevent SkillsLoader from discovering ~/.aim/skills during tests.

    Without this, hosts with many AIM capability packages (400+ skill files,
    ~35MB) inflate session context beyond _MAX_CONTEXT_CHARS, causing silent
    truncation and non-deterministic test failures under xdist.
    """
    monkeypatch.setattr("kiro_crew.skills.aim_skills_dir", lambda: tmp_path / "no_aim")


def pytest_configure(config: pytest.Config) -> None:
    """Pre-import ``tracemalloc`` so pytest's unraisable hook can't crash on it.

    pytest's ``_pytest/unraisableexception`` plugin replaces ``sys.unraisablehook``
    and, when a leaked object (an un-awaited coroutine, an orphaned
    ``SessionManager._cleanup_loop`` task, etc.) is garbage-collected, calls
    ``tracemalloc_message()`` which runs ``import tracemalloc`` *from inside the
    GC callback*. If ``tracemalloc`` has not been imported yet, that first import
    lands in a partially-initialized state (a CPython circular-import artifact
    observed on 3.12) and raises ``AttributeError: partially initialized module
    'tracemalloc' has no attribute 'get_object_traceback'``. pytest then re-raises
    it as ``RuntimeError: Failed to process unraisable exception`` and reports it
    as an ERROR at the *next* test's setup — turning a benign "object was never
    awaited" warning into a hard build failure that lands on an innocent test.

    Importing the module eagerly here (once per xdist worker, before any test
    runs or any GC fires) makes the hook's ``import tracemalloc`` a no-op
    ``sys.modules`` hit against a fully-built module, so leaks degrade back to
    warnings instead of failing the suite. Touch ``get_object_traceback`` to
    force full initialization and to keep the import from reading as unused.
    """
    import tracemalloc

    assert hasattr(tracemalloc, "get_object_traceback")


def pytest_xdist_auto_num_workers(config: pytest.Config) -> int:
    """Cap the worker count used by ``-n auto`` (and ``-n logical``).

    The optimal worker count for this suite plateaus around 24-32 and then
    *regresses*: every extra xdist worker pays a fixed cost -- it re-imports the
    full app (aiohttp/boto3/numpy/pdfplumber/transcribe) and writes its own
    ``.coverage.*`` data file that must be combined at the end. Past ~32 that
    fixed cost outweighs the added parallelism. Measured on a 64-core host:
    156s @ 64 workers vs 92s @ 32 workers (-41%).

    ``min(cpu, CAP)`` stays optimal on every machine and is architecture-
    agnostic -- the cap addresses fixed per-worker serialization cost, not
    per-core speed, so it holds across Intel / ARM / Apple silicon. An 8-core
    laptop gets 8 workers (the cap never binds, so no oversubscription), a
    16-core build host gets 16 (unchanged from today), while 64/128-core
    desktops are held at 32 instead of stampeding. Override the ceiling with
    ``KIROCREW_MAX_TEST_WORKERS`` for a host that profiles differently. An
    explicit ``-n <N>`` on the command line always wins; this hook only fires
    for ``auto`` / ``logical``.
    """
    cpu = os.cpu_count() or 1
    try:
        cap = int(os.environ.get("KIROCREW_MAX_TEST_WORKERS", "32"))
    except ValueError:
        cap = 32
    return min(cpu, max(1, cap))


@pytest.fixture(autouse=True)
def _reset_safety_override_between_tests():
    """Reset the SafetyOverride singleton between tests to prevent state leaking."""
    _reset_safety_override()
    yield
    _reset_safety_override()


@pytest.fixture(autouse=True)
def _reset_reasoning_effort_globals():
    """Snapshot + restore the process-global reasoning-effort allowlist around
    each test. The allowlist is union-only/monotonic by design (persistence
    safety), and several AcpSessionHandle tests drive synthetic effort levels
    through ``_sync_effort_levels`` -> ``update_reasoning_effort_values``;
    without this, a level like ``"extreme"`` leaks into the global and poisons
    validation tests sharing the xdist worker (e.g. test_chat_slot_reasoning_effort)."""
    import kiro_crew.dashboard.chat_persistence as _cp

    saved_values = set(_cp._reasoning_effort_values)
    saved_ordered = list(_cp._reasoning_effort_ordered)
    try:
        yield
    finally:
        _cp._reasoning_effort_values = saved_values
        _cp._reasoning_effort_ordered = saved_ordered


@pytest.fixture(autouse=True)
def _isolate_kirocrew_home(tmp_path_factory, monkeypatch):
    """Pin ``KIROCREW_HOME`` to a per-test tmp dir as a safety net.

    ``config_dir()`` reads ``KIROCREW_HOME`` on every call and falls back to the
    operator's real ``~/.kirocrew`` when it is unset. Any test that reaches a
    code path resolving ``apps_dir()`` / ``config_dir()`` (e.g. a lifecycle
    dispatch that calls ``app_dir(name)/"data".mkdir()``) without setting
    ``KIROCREW_HOME`` itself would otherwise create real dirs/files under the
    developer's home — and under Hypothesis that means one orphan per generated
    example, accumulating into thousands of stray ``~/.kirocrew/apps/<name>/``
    dirs over a dev's test history.

    This runs before the test body, so a test that sets its own
    ``KIROCREW_HOME`` via ``monkeypatch.setenv`` still wins (its value is applied
    later and reverted independently). The guard only changes behavior for tests
    that did NOT isolate the home themselves — exactly the leak we want to close.
    """
    home = tmp_path_factory.mktemp("kirocrew-home")
    monkeypatch.setenv("KIROCREW_HOME", str(home))


@pytest.fixture(autouse=True)
def _isolate_agent_state_sidecar(tmp_path_factory, monkeypatch):
    """Pin the agent_state sidecar to a tmp dir for the whole suite.

    ``kiro_crew.agent_state`` stores per-agent bookkeeping (model_managed,
    cc_model) in ``~/.kirocrew/agent_model_state.json`` via ``config_dir()``.
    Tests that exercise the install / refresh / migration / PATCH paths would
    otherwise read and write the operator's real sidecar. Redirect
    ``config_dir`` — referenced as a module attribute at call time — to a fresh
    tmp dir so every test starts from empty state.
    """
    sidecar_root = tmp_path_factory.mktemp("agent-state-isolation")
    monkeypatch.setattr("kiro_crew.agent_state.config_dir", lambda: sidecar_root)


@pytest.fixture(autouse=True)
def _ensure_event_loop():
    """Ensure a USABLE (open) event loop exists for tests that call
    ``asyncio.get_event_loop().run_until_complete(...)`` (e.g. test_knowledge).

    Two failure modes this guards, both seen on the loaded CI farm under xdist:
      * no current loop set (``RuntimeError``) — Python 3.9 Semaphore default_factory; and
      * a current loop that is set but CLOSED — left behind by a prior test in the same
        worker that ran ``asyncio.run(...)`` (which on 3.12 closes its loop at teardown).
        ``get_event_loop()`` returns that closed loop WITHOUT raising, so the next
        ``run_until_complete`` blows up with ``RuntimeError: Event loop is closed``. We
        detect a closed/absent loop and install a fresh open one so each test starts clean.
    """
    try:
        loop = asyncio.get_event_loop()
        if loop.is_closed():
            asyncio.set_event_loop(asyncio.new_event_loop())
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())


@pytest.fixture(autouse=True)
def _restore_default_child_watcher():
    """Restore the always-active ThreadedChildWatcher after every test (Python 3.10).

    Some tests install a real, non-default asyncio child watcher via the
    gateway's ``_install_child_watcher()`` -- notably
    ``test_cli.py::test_real_subprocess_works_after_install_on_linux``, which on
    Linux installs a ``PidfdChildWatcher`` and runs ``asyncio.run``. On exit,
    ``asyncio.run`` detaches the watcher's loop, leaving a loop-less watcher in
    the global policy. On Python 3.10 that watcher's ``is_active()`` is then
    False, so the NEXT subprocess-spawning test scheduled on the same
    pytest-xdist worker (``test_taskrunner::TestGitCoord``, the code_reviewer git
    routes) fails with "asyncio.get_child_watcher() is not activated, subprocess
    support is not available". Whether the leak bites depends purely on xdist
    sharding, so adding or removing unrelated tests can flip a green run red with
    no production-code change. Resetting to ``ThreadedChildWatcher`` (whose
    ``is_active()`` is always True) after each test removes the cross-test
    coupling. No-op on 3.12+, where child watchers were removed and the event
    loop reaps children directly.
    """
    yield
    if sys.version_info >= (3, 12):
        return
    threaded = getattr(asyncio, "ThreadedChildWatcher", None)
    if threaded is None:  # non-Unix (no child watchers) -- nothing to restore
        return
    try:
        if not isinstance(asyncio.get_child_watcher(), threaded):
            asyncio.set_child_watcher(threaded())
    except Exception:  # noqa: BLE001 -- isolation cleanup must never fail a test
        # Test-isolation cleanup must never fail a test; worst case is the
        # pre-existing leak, which the next test's loop setup also tolerates.
        pass


@pytest.fixture(autouse=True)
def _git_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make git tests hermetic: pin identity AND neutralize host global/system config.

    Two independent host-environment bleeds must be closed for git-backed tests
    (git_coord scenarios) to be deterministic across machines:

    1. Identity — a host without a global ``user.name``/``user.email`` makes
       ``git commit`` fail. Pin it via the ``GIT_AUTHOR_*``/``GIT_COMMITTER_*``
       env vars.
    2. Global/system config — the host's ``~/.gitconfig`` (via
       ``core.excludesFile`` → e.g. ``~/.gitignore_global`` containing ``*.png``)
       silently makes ``git add -A`` skip files, so a "commit a binary file"
       test sees an empty tree and gets an empty sha. Point
       ``GIT_CONFIG_GLOBAL``/``GIT_CONFIG_SYSTEM`` at ``/dev/null`` so no
       host-level config (excludes, aliases, hooks, signing) leaks into tests.
    """
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Test")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "test@example.com")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "Test")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "test@example.com")
    # Isolate from the host's global/system git config (Git >= 2.32). An empty
    # file (/dev/null) means git reads no global or system settings.
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", os.devnull)


@pytest.fixture(autouse=True)
def _no_load_check(monkeypatch: pytest.MonkeyPatch) -> None:
    """Skip system load checks in tests — avoids real asyncio.sleep delays."""
    from unittest.mock import AsyncMock

    try:
        monkeypatch.setattr("kiro_crew.task_executor._wait_for_load", AsyncMock())
    except AttributeError:
        pass  # load guard not present in this branch


@pytest.fixture(autouse=True)
def _enterprise_bypass(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set a default validated team_id so _route_message doesn't reject messages."""
    monkeypatch.setattr("kiro_crew.slack.enterprise._validated_team_id", "TTEST")
    monkeypatch.setattr("kiro_crew.slack.enterprise._validated_enterprise_id", "ETEST")
    monkeypatch.setattr("kiro_crew.slack.enterprise._allowed_team_ids", {"TTEST"})


@pytest.fixture(autouse=True)
def _clean_emojis():
    """Reset _PHASE_EMOJIS to defaults before each test (suppresses local config)."""
    original = dict(_PHASE_EMOJIS)
    _PHASE_EMOJIS.clear()
    _PHASE_EMOJIS.update(_build_phase_emojis({})[0])
    yield
    _PHASE_EMOJIS.clear()
    _PHASE_EMOJIS.update(original)


@pytest.fixture(autouse=True)
def _clean_slack_thread_state():
    """Reset the ``handler`` module-global thread-state maps between tests.

    ``handler`` keeps process-global maps for per-thread privacy and routing
    state: ``_thread_temporary`` / ``_thread_incognito`` (drive
    ``_is_slack_restricted`` — the memory-write gate consulted by ``!title``,
    consolidation, etc.), ``_titled_threads`` (auto-title claim), and
    ``_thread_agents`` (per-thread agent override). Nothing clears these
    globally, so a test that marks a thread restricted — including one that
    drives the real ``handle_message_transport`` drain path against a
    ``MagicMock`` session map, whose ``_hydrate_conv_flags`` reads truthy mock
    flags and calls ``_mark_incognito`` / ``_mark_temporary`` — leaves e.g.
    ``"thread1"`` in ``_thread_incognito`` forever. Under ``pytest -n auto``
    (``--dist load`` interleaves tests across files on each worker) a later
    ``test_title_updates_conversation_log`` then sees
    ``_is_slack_restricted("thread1") is True`` and skips ``set_title``,
    failing with no production-code change — a classic order-dependent flake.
    Clearing before and after every test makes each hermetic regardless of
    scheduling. Idempotent with per-file fixtures that already clear a subset.
    """
    from kiro_crew.slack import handler as _h

    for _m in (_h._thread_temporary, _h._thread_incognito, _h._titled_threads, _h._thread_agents):
        _m.clear()
    yield
    for _m in (_h._thread_temporary, _h._thread_incognito, _h._titled_threads, _h._thread_agents):
        _m.clear()


@pytest.fixture(autouse=True, scope="session")
def _isolate_sel_default_dir(tmp_path_factory):
    """Redirect the Security Event Log default dir to a session-local tmp dir.

    SEL's default singleton writes to the real ``~/.kirocrew/security_events.jsonl``
    (``_DEFAULT_DIR = Path.home()/".kirocrew"``, non-atomic append). Tests that
    emit events via the default ``sel()`` would otherwise pollute that real file
    and, under ``pytest -n auto``, share it across worker processes. Redirect the
    module-level default to a per-session tmp dir. Session-scoped so we don't
    churn SEL's background writer thread per test; tests that manage their own
    ``SecurityEventLog`` (test_sel.py resets ``_instance`` + passes ``base_dir``)
    are unaffected.
    """
    orig_dir = _sel._DEFAULT_DIR
    orig_inst = _sel.SecurityEventLog._instance
    _sel._DEFAULT_DIR = tmp_path_factory.mktemp("sel")
    _sel.SecurityEventLog._instance = None
    yield
    _sel._DEFAULT_DIR = orig_dir
    _sel.SecurityEventLog._instance = orig_inst


class MockSlackClient(SlackClientOps):
    """In-memory mock for testing."""

    def __init__(self):
        self.actions: list[tuple[str, dict]] = []
        self._next_ts = 1000000
        self._fetch_message_result: str | None = None
        self._fetch_thread_replies_result: list[dict] = []

    async def post_message(self, channel, text, thread_ts=None, unfurl_links=None, unfurl_media=None):
        ts = f"{self._next_ts}.000000"
        self._next_ts += 1
        self.actions.append(
            ("post", {"channel": channel, "text": text, "thread_ts": thread_ts, "ts": ts,
                      "unfurl_links": unfurl_links, "unfurl_media": unfurl_media})
        )
        return ts

    async def post_blocks(self, channel, blocks, text, thread_ts=None, unfurl_links=None, unfurl_media=None):
        ts = f"{self._next_ts}.000000"
        self._next_ts += 1
        self.actions.append(
            (
                "blocks",
                {
                    "channel": channel,
                    "blocks": blocks,
                    "text": text,
                    "thread_ts": thread_ts,
                    "ts": ts,
                    "unfurl_links": unfurl_links,
                    "unfurl_media": unfurl_media,
                },
            )
        )
        return ts

    async def update_message(self, channel, ts, text):
        self.actions.append(("update", {"channel": channel, "ts": ts, "text": text}))

    async def delete_message(self, channel, ts):
        self.actions.append(("delete", {"channel": channel, "ts": ts}))

    async def add_reaction(self, channel, ts, emoji, raise_on_error=False):
        self.actions.append(("react", {"channel": channel, "ts": ts, "emoji": emoji}))

    async def remove_reaction(self, channel, ts, emoji, raise_on_error=False):
        self.actions.append(("unreact", {"channel": channel, "ts": ts, "emoji": emoji}))

    async def open_dm(self, user_id):
        self.actions.append(("open_dm", {"user_id": user_id}))
        return f"D{user_id}"

    async def post_ephemeral(self, channel, user_id, text, blocks=None, thread_ts=None):
        self.actions.append(("ephemeral", {"channel": channel, "user_id": user_id, "text": text, "blocks": blocks, "thread_ts": thread_ts}))

    async def views_publish(self, user_id, view):
        self.actions.append(("views_publish", {"user_id": user_id, "view": view}))

    async def views_open(self, trigger_id, view):
        self.actions.append(("views_open", {"trigger_id": trigger_id, "view": view}))

    async def views_update(self, view_id, view):
        self.actions.append(("views_update", {"view_id": view_id, "view": view}))

    async def upload_file(self, channel, thread_ts, file, filename, title):
        self.actions.append(
            (
                "upload_file",
                {
                    "channel": channel,
                    "thread_ts": thread_ts,
                    "file": file,
                    "filename": filename,
                    "title": title,
                },
            )
        )

    async def start_stream(self, channel, thread_ts, initial_text=None, team_id=None, user_id=None):
        if not getattr(self, "_stream_enabled", False) or getattr(self, "_start_stream_fails", False):
            return None
        ts = f"{self._next_ts}.000000"
        self._next_ts += 1
        self.actions.append(
            (
                "start_stream",
                {
                    "channel": channel,
                    "thread_ts": thread_ts,
                    "text": initial_text,
                    "ts": ts,
                },
            )
        )
        return ts

    async def append_stream(self, channel, ts, text):
        self.actions.append(("append_stream", {"channel": channel, "ts": ts, "text": text}))
        return True

    async def append_task(self, channel, ts, task_id, title, status, details="", output=""):
        self.actions.append(
            (
                "append_task",
                {
                    "channel": channel,
                    "ts": ts,
                    "task_id": task_id,
                    "title": title,
                    "status": status,
                    "details": details,
                },
            )
        )
        return True

    async def stop_stream(self, channel, ts, final_text=None):
        self.actions.append(("stop_stream", {"channel": channel, "ts": ts, "text": final_text}))
        return True

    async def set_thread_title(self, channel, thread_ts, title):
        self.actions.append(
            ("set_thread_title", {"channel": channel, "thread_ts": thread_ts, "title": title})
        )

    async def set_thread_status(self, channel, thread_ts, status):
        self.actions.append(
            ("set_thread_status", {"channel": channel, "thread_ts": thread_ts, "status": status})
        )

    async def fetch_message(self, channel: str, ts: str) -> str | None:
        self.actions.append(("fetch_message", {"channel": channel, "ts": ts}))
        return self._fetch_message_result

    async def fetch_thread_replies(self, channel: str, thread_ts: str, limit: int = 200, warn_on_pagination: bool = True) -> list[dict]:
        self.actions.append(("fetch_thread_replies", {"channel": channel, "thread_ts": thread_ts, "limit": limit, "warn_on_pagination": warn_on_pagination}))
        return self._fetch_thread_replies_result


@pytest.fixture(autouse=True)
def _reset_platform_context(monkeypatch):
    """Clear the process-global PlatformContext between tests.

    A test that composes a non-default context (e.g. an Amazon-overlay probe)
    must not leak it into the next test.  ``current_context()`` lazily rebuilds
    the standalone default on next access.

    Also pins ``KIROCREW_PROFILE=standalone`` by default so a dev box that has a
    real ``~/.midway`` directory does not make ``boot_platform`` resolve the
    ``amazon`` profile and fail closed (no companion installed) for the many
    pre-existing tests that drive ``run_gateway`` / boot.  A test that wants the
    amazon profile overrides this env via its own ``monkeypatch.setenv`` (it
    runs after this autouse fixture), or composes the context directly via
    ``set_context`` without booting.
    """
    from kiro_crew.platform.bootstrap import _reset_boot_state
    from kiro_crew.platform.context import reset_context

    monkeypatch.setenv("KIROCREW_PROFILE", "standalone")
    reset_context()
    _reset_boot_state()
    yield
    reset_context()
    _reset_boot_state()
