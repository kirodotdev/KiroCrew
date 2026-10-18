"""The isolation floor's between-test restores, each proven on the real fixture code.

Production sets some process-global state once and never resets it, so a test that
changes it would hand the change to the next test; the rootdir conftest puts it back.
Covers the dynamic ``JIRA_TOKEN_<HEX>`` credential keys, the inherited shell preloads
``name_grant`` refuses, the logging record factory, logger levels, the CLI log queue
listener, the environment CLI startup mutates and the hooks dispatcher globals. The
other floor guards are in ``test_host_isolation_floor.py`` and the worker budget in
``test_host_isolation_floor_worker_budget.py``; the conftest loader comes from
``host_isolation_floor_helpers.py``.
"""

from __future__ import annotations

import io
import logging
import os
import queue
import sys
from logging.handlers import QueueListener

import pytest
from host_isolation_floor_helpers import (
    _root,
)

from kiro_crew import cli
from kiro_crew.log_redaction import install_log_redaction

# ── driving the floor's own between-test restores ─────────────────────────


def _autouse_floor_generator(name: str):
    """The plain generator function behind one of the rootdir conftest's autouse fixtures.

    The floor tests below drive one setup -> teardown cycle of the REAL fixture code,
    which is what lets a SINGLE test observe the restore the floor performs between
    tests. An injector/observer pair cannot do that reliably: pytest-split partitions
    the collected suite into shard groups BEFORE xdist runs, so it can place the two
    halves in different CI shards regardless of any ``xdist_group`` mark, and an
    observer whose meaning depends on which test ran before it is unprovable in a
    sharded run.

    Asserts the attribute still IS an autouse fixture on the way through: the floor's
    guarantee is that it fires around every test, and a fixture demoted to a plain
    helper (or one whose ``autouse`` was dropped) would still pass a direct drive.
    """
    definition = getattr(_root, name)
    marker = getattr(definition, "_fixture_function_marker", None) or getattr(
        definition, "_pytestfixturefunction", None
    )
    assert (
        marker is not None and marker.autouse
    ), f"conftest.{name} is not an autouse fixture, so the floor it implements is unarmed"
    return getattr(definition, "__wrapped__", definition)


def _listener_thread_alive(listener: QueueListener) -> bool:
    """Whether the listener's drain thread is still running.

    ``QueueListener.stop`` joins the thread and then drops the handle, so a stopped
    listener reports ``None`` here. Checked rather than trusting the cleared slot: the
    leak this floor absorbs is the live thread and the descriptor it holds, and dropping
    the reference alone would satisfy every assertion about the slot while leaking both.
    """
    thread = getattr(listener, "_thread", None)
    return thread is not None and thread.is_alive()


class TestDynamicCredentialEnvironmentIsRestored:
    """A dynamic per-host Jira token must not leak to the next test.

    ``load_credentials`` propagates ``JIRA_TOKEN_<HEX>`` keys even though they are
    not members of the fixed ``CREDENTIAL_KEYS`` tuple, so the floor's restore has
    to recognise the dynamic shape too — a snapshot bounded to the fixed keys would
    pass over it. The restore under test is ``conftest._no_credential_env_residue``'s
    own teardown, driven directly (see ``_autouse_floor_generator``).
    """

    def test_this_test_starts_without_the_dynamic_token(self) -> None:
        """Which is only true if no earlier test on this worker leaked one."""
        assert "JIRA_TOKEN_AABBCC" not in os.environ

    def test_an_injected_dynamic_token_is_removed_by_the_floors_own_teardown(self) -> None:
        """One full cycle of the real fixture: snapshot while absent, inject, restore.

        A failure part-way cannot leak the token past this test: the live autouse
        instance of the same fixture wraps this test too, and its snapshot predates
        the injection.
        """
        cycle = _autouse_floor_generator("_no_credential_env_residue")()
        next(cycle)  # the floor's setup: snapshot, taken while the token is absent

        os.environ["JIRA_TOKEN_AABBCC"] = "token-from-this-test"

        with pytest.raises(StopIteration):
            next(cycle)  # the floor's teardown: the restore under test
        assert "JIRA_TOKEN_AABBCC" not in os.environ


class TestInheritedShellEnvironmentIsScrubbed:
    """The entries ``name_grant`` refuses as inherited preloads are hidden per test.

    On a RHEL-family host ``which2.sh`` puts ``BASH_FUNC_which%%`` in every login
    shell's environment, and ``name_grant``'s AMBIGUOUS_ENV refusal -- checked before
    every narrower code -- then rewrote what 79 unrelated assertions observed
    The scrub under test is ``conftest._scrub_inherited_preload_env``,
    driven directly (see ``_autouse_floor_generator``); the domain-level regression
    lives in ``test_name_grant.py::TestInheritedHostEnvironment``, but only this
    direct drive survives ``autouse=True`` being dropped or the restore half being
    lost, because it asserts the marker and both halves of one real cycle.
    """

    def test_this_test_starts_without_the_injected_entries(self) -> None:
        """True on every host: the live scrub hides even a genuinely inherited entry."""
        assert "BASH_FUNC_kcfloor%%" not in os.environ
        assert "BASH_ENV" not in os.environ

    def test_an_inherited_entry_is_scrubbed_then_restored_by_one_cycle(self) -> None:
        """One full cycle of the real fixture: inject first, so it reads as inherited.

        The fixture records its removals on the monkeypatch instance it is handed,
        so the restore under test is that instance's ``undo`` -- driven explicitly
        here, exactly as pytest drives the shared per-test instance after every
        fixture teardown. A failure part-way cannot leak the entries past this
        test: the live autouse instance of the same fixture wraps this test too,
        and its teardown sweep removes matching entries its own snapshot never saw.
        """
        mp = pytest.MonkeyPatch()
        os.environ["BASH_FUNC_kcfloor%%"] = "() { :; }"
        os.environ["BASH_ENV"] = "/etc/kc-floor-rc"
        try:
            cycle = _autouse_floor_generator("_scrub_inherited_preload_env")(mp)
            next(cycle)  # the floor's setup: the scrub under test
            assert "BASH_FUNC_kcfloor%%" not in os.environ
            assert "BASH_ENV" not in os.environ
            with pytest.raises(StopIteration):
                next(cycle)  # the floor's teardown sweep: inherited keys stay absent
            assert "BASH_FUNC_kcfloor%%" not in os.environ
            mp.undo()  # the restore under test: rides the monkeypatch undo stack
            assert os.environ.get("BASH_FUNC_kcfloor%%") == "() { :; }"
            assert os.environ.get("BASH_ENV") == "/etc/kc-floor-rc"
        finally:
            mp.undo()
            os.environ.pop("BASH_FUNC_kcfloor%%", None)
            os.environ.pop("BASH_ENV", None)

    def test_an_entry_leaked_during_a_test_is_swept_by_the_floors_own_teardown(self) -> None:
        """The sweep half: a raw write that appeared mid-test does not leak on."""
        mp = pytest.MonkeyPatch()
        try:
            cycle = _autouse_floor_generator("_scrub_inherited_preload_env")(mp)
            next(cycle)  # the floor's setup: nothing inherited, nothing recorded

            os.environ["BASH_FUNC_kcfloor%%"] = "() { leaked; }"

            with pytest.raises(StopIteration):
                next(cycle)  # the floor's teardown: the sweep under test
            assert "BASH_FUNC_kcfloor%%" not in os.environ
            mp.undo()  # no record for a raw write, so the sweep's removal stands
            assert "BASH_FUNC_kcfloor%%" not in os.environ
        finally:
            mp.undo()
            os.environ.pop("BASH_FUNC_kcfloor%%", None)


# ── the logging record factory ─────────────────────────────────────────────


class TestTheLogRecordFactoryIsRestored:
    """``logging.setLogRecordFactory`` is ONE process-global slot, per worker.

    ``log_redaction``'s wrapper clears ``args`` and ``exc_info`` on every record created
    after it, so a test that leaves it installed reds whatever unrelated test later
    asserts on either field -- and because PR CI shards, the victim usually lands in a
    different process and the pollution is invisible until an unsharded release run.
    ``conftest._restore_log_record_factory`` is what removes the class; without a test,
    an edit to it reverts silently and the failures reappear in files that have nothing
    to do with the cause. Its teardown is driven directly (see
    ``_autouse_floor_generator``), so the proof needs no adjacent observer test.
    """

    def test_this_test_starts_with_the_stdlib_factory(self) -> None:
        """Which is only true if no earlier test on this worker left a wrapper installed."""
        assert logging.getLogRecordFactory() is logging.LogRecord

    def test_a_leaked_wrapper_is_removed_by_the_floors_own_teardown(self) -> None:
        """One full cycle of the real fixture: snapshot, install and leave installed, restore.

        The install is exactly the leak the floor absorbs in the wild — a test driving
        the real ``cli.main()`` reaches ``_setup_cli_logging``, which installs this
        wrapper and never removes it. A failure part-way cannot leak the wrapper past
        this test: the live autouse instance of the same fixture wraps this test too,
        and its snapshot predates the install.
        """
        cycle = _autouse_floor_generator("_restore_log_record_factory")()
        next(cycle)  # the floor's setup: snapshot, taken while the stdlib factory holds

        install_log_redaction([])
        assert logging.getLogRecordFactory() is not logging.LogRecord

        with pytest.raises(StopIteration):
            next(cycle)  # the floor's teardown: the restore under test
        assert logging.getLogRecordFactory() is logging.LogRecord, (
            f"the record factory is still {logging.getLogRecordFactory()!r} -- a wrapper "
            "left installed by a test would rewrite every later record on its worker"
        )

    def test_the_restore_target_is_what_the_test_inherited_not_the_stdlib(self) -> None:
        """The floor restores the INHERITED factory, so a higher-scoped installer survives.

        Pinned because it is the documented reason the floor is a fixture rather than a
        ``pytest_runtest_setup`` hookimpl: a class- or module-scoped fixture that installs
        a factory for its whole scope must not have it torn out after the first test.
        """
        sentinel_calls: list[str] = []

        def sentinel(*args: object, **kwargs: object) -> logging.LogRecord:
            sentinel_calls.append("made")
            return logging.LogRecord(*args, **kwargs)  # type: ignore[arg-type]

        before = logging.getLogRecordFactory()
        logging.setLogRecordFactory(sentinel)
        try:
            cycle = _autouse_floor_generator("_restore_log_record_factory")()
            next(cycle)  # snapshot taken while the sentinel is installed
            install_log_redaction([])
            with pytest.raises(StopIteration):
                next(cycle)
            assert logging.getLogRecordFactory() is sentinel, (
                "the floor restored past the factory this cycle inherited, so a "
                "higher-scoped installer would be torn out after its first test"
            )
        finally:
            logging.setLogRecordFactory(before)


# ── logger levels ──────────────────────────────────────────────────────────


class TestLoggerLevelsAreRestored:
    """A logger's level is PROCESS-GLOBAL, per worker, and HIERARCHICAL.

    Together those are what make this leak class so hard to attribute. ``Logger.debug``
    gates on the EFFECTIVE level, so an explicit level left on ``kiro_crew`` decides what
    every ``kiro_crew.*`` logger in the worker may emit, and it outranks the root level
    ``caplog.at_level()`` sets -- the victim gets ``caplog.text == ""``, nothing at all
    rather than the wrong text, from a test that passes alone. The suite reaches this
    through ``cli._setup_cli_logging``, which pins ``kiro_crew`` at WARNING, and which
    test modules across the suite run for real by driving ``cli.main()`` in process.

    ``conftest._restore_logger_levels`` is what removes the class; without a test, an edit
    to it reverts silently and the failures reappear in files that have nothing to do with
    the cause. Its teardown is driven directly (see ``_autouse_floor_generator``), so the
    proof needs no adjacent observer test.
    """

    def test_this_test_starts_with_an_unconfigured_kiro_crew_logger(self) -> None:
        """Which is only true if no earlier test on this worker left a level on it."""
        assert logging.getLogger("kiro_crew").level == logging.NOTSET

    def test_a_leaked_level_is_removed_by_the_floors_own_teardown(self) -> None:
        """One full cycle of the real fixture: snapshot, pin a level and leave it, restore.

        The pin is exactly the leak the floor absorbs in the wild — ``_setup_cli_logging``
        sets ``kiro_crew`` to WARNING once per process and never undoes it. A failure
        part-way cannot leak the level past this test: the live autouse instance of the
        same fixture wraps this test too, and its snapshot predates the pin.
        """
        cycle = _autouse_floor_generator("_restore_logger_levels")()
        next(cycle)  # the floor's setup: snapshot, taken while the level is NOTSET

        logging.getLogger("kiro_crew").setLevel(logging.WARNING)
        assert logging.getLogger("kiro_crew").level == logging.WARNING

        with pytest.raises(StopIteration):
            next(cycle)  # the floor's teardown: the restore under test
        level = logging.getLogger("kiro_crew").level
        assert level == logging.NOTSET, (
            f"kiro_crew is still pinned at {logging.getLevelName(level)} -- every "
            "kiro_crew.* record below that level would be dropped before it reaches "
            "the root handler caplog captures through"
        )

    def test_a_logger_created_during_the_test_is_restored_to_pristine(self) -> None:
        """The gap a plain snapshot cannot see: a logger that did not exist at setup.

        The floor restores a name missing from its "before" snapshot to the pristine
        ``(NOTSET, enabled)`` state rather than passing over it, and this is the only
        place that behaviour is exercised with a genuinely fresh name.
        """
        name = "kiro_crew._floor_probe_6351"
        assert name not in logging.Logger.manager.loggerDict

        cycle = _autouse_floor_generator("_restore_logger_levels")()
        next(cycle)  # snapshot taken while the logger does not exist

        probe = logging.getLogger(name)
        probe.setLevel(logging.CRITICAL)
        probe.disabled = True

        with pytest.raises(StopIteration):
            next(cycle)
        assert probe.level == logging.NOTSET and probe.disabled is False, (
            "a logger created mid-test kept its configuration -- the floor's "
            "missing-from-snapshot branch no longer restores to pristine"
        )

    def test_a_debug_record_from_a_kiro_crew_logger_still_reaches_caplog(self, caplog) -> None:
        """The capability the level restore exists to protect, asserted directly.

        The level assertion above pins the mechanism; this pins the OUTCOME, in the exact
        shape the victim test uses -- ``at_level`` on the root logger, a ``debug`` call on
        a ``kiro_crew.*`` child -- so a future floor that restores something subtly
        different still has to keep this working.
        """
        with caplog.at_level("DEBUG"):
            logging.getLogger("kiro_crew.slack.gateway").debug("floor canary")
        assert "floor canary" in caplog.text


# ── the CLI log queue listener ─────────────────────────────────────────────


class TestTheLogQueueListenerIsRestored:
    """``cli._LOG_QUEUE_LISTENER`` is ONE process-global slot, per worker.

    ``_setup_cli_logging`` starts a ``QueueListener`` for a LONG-LIVED command and never
    stops it -- correct in production, which does it once per process -- so every test
    that drives the real ``cli.main()`` for ``serve`` / ``gateway`` / ``chat`` leaves one
    running. The SHORT-LIVED branch then reads it: it takes the ``else`` path and does not
    touch the global, so a test asserting a short-lived verb starts no listener sees the
    PREVIOUS test's and fails on its own first line, in a file that cleans up after itself
    correctly. Two tests in ``test_cli_logging.py`` assert exactly that, and under
    ``-n auto --dist loadgroup`` whether a leaker precedes them on the worker varies run
    to run, so it surfaces as an intermittent failure rather than an ordering bug.

    ``conftest._restore_log_queue_listener`` is what removes the class; without a test, an
    edit to it reverts silently and the failures reappear as a flake. Its teardown is
    driven directly (see ``_autouse_floor_generator``), so the proof needs no adjacent
    observer test.
    """

    @pytest.fixture(autouse=True)
    # nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- no test in this module undoes the shared monkeypatch
    def _isolated_cli_logging(self, monkeypatch):
        """Foreground mode, and remove the handlers these tests' real setup calls attach.

        Driving the real ``_setup_cli_logging`` is the point -- a test that assigned the
        global instead would pass even if the production install site moved -- but it also
        attaches a handler holding an open descriptor, and the floor under test
        deliberately does not restore handlers. Removing only what this test ADDED is what
        ``_pristine_logging`` does; the snapshotted list is never written back, because
        ``caplog`` swaps a handler on the root logger at every phase boundary and writing
        a setup-phase list back during teardown would drop the one it is capturing
        through.
        """
        monkeypatch.setattr("kiro_crew.cli._fd_targets_file", lambda fd, path: False)
        loggers = (logging.getLogger(), logging.getLogger("kiro_crew"))
        saved = [(lgr, lgr.handlers[:]) for lgr in loggers]
        yield
        for lgr, handlers in saved:
            for handler in lgr.handlers[:]:
                if handler not in handlers:
                    lgr.removeHandler(handler)
                    handler.close()

    def test_this_test_starts_with_no_listener(self) -> None:
        """Which is only true if no earlier test on this worker left one running."""
        assert cli._LOG_QUEUE_LISTENER is None

    def test_a_leaked_listener_is_removed_by_the_floors_own_teardown(self) -> None:
        """One full cycle of the real fixture: snapshot, leak a listener, restore.

        The leak is produced by the real ``_setup_cli_logging`` on the real long-lived
        branch, which is exactly how it happens in the wild, so the test still fails if
        the production install site moves. A failure part-way cannot leak the listener
        past this test: the live autouse instance of the same fixture wraps this test too,
        and its snapshot predates the install.
        """
        cycle = _autouse_floor_generator("_restore_log_queue_listener")()
        next(cycle)  # the floor's setup: snapshot, taken while the slot is empty

        cli._setup_cli_logging("gateway", 1)
        leaked = cli._LOG_QUEUE_LISTENER
        assert leaked is not None, "the long-lived branch no longer starts a listener"

        with pytest.raises(StopIteration):
            next(cycle)  # the floor's teardown: the restore under test
        assert cli._LOG_QUEUE_LISTENER is None, (
            f"the slot still holds {cli._LOG_QUEUE_LISTENER!r} -- the next test to run "
            "_setup_cli_logging for a SHORT-LIVED command would read this listener and "
            "fail asserting it started none"
        )
        assert not _listener_thread_alive(leaked), (
            "the listener object was dropped but its thread is still running -- it holds "
            "the file handler's descriptor open on a gateway.log under a tmp_path the "
            "next test deletes"
        )

    def test_the_restore_target_is_what_the_test_inherited(self, monkeypatch) -> None:
        """The floor restores the INHERITED listener, so a higher-scoped installer survives.

        Same reason the record-factory floor restores to its snapshot: a class- or
        module-scoped fixture that starts a listener for its whole scope must not have it
        torn out after the first test.
        """
        sentinel = QueueListener(queue.SimpleQueue())
        monkeypatch.setattr(cli, "_LOG_QUEUE_LISTENER", sentinel)

        cycle = _autouse_floor_generator("_restore_log_queue_listener")()
        next(cycle)  # snapshot taken while the sentinel holds the slot

        cli._setup_cli_logging("gateway", 1)
        assert cli._LOG_QUEUE_LISTENER is not sentinel

        with pytest.raises(StopIteration):
            next(cycle)
        assert cli._LOG_QUEUE_LISTENER is sentinel, (
            "the floor restored past the listener this cycle inherited, so a "
            "higher-scoped installer would be torn out after its first test"
        )

    def test_a_short_lived_command_leaves_the_slot_alone(self, monkeypatch) -> None:
        """The production behaviour the floor exists to accommodate, pinned at source.

        ``_setup_cli_logging`` deliberately does not clear the global on the short-lived
        branch -- a listener a long-lived command started genuinely still exists -- which
        is why the leak is absorbed in the test seam rather than by clearing it there. If
        this ever changes, the floor becomes redundant rather than wrong, and this test is
        what says so.
        """
        sentinel = QueueListener(queue.SimpleQueue())
        monkeypatch.setattr(cli, "_LOG_QUEUE_LISTENER", sentinel)

        cli._setup_cli_logging("status", 1)

        assert cli._LOG_QUEUE_LISTENER is sentinel


class TestCliProcessEnvironmentIsRestored:
    """CLI startup may mutate this call, not the next test's environment."""

    @pytest.mark.parametrize(
        "initial",
        [
            pytest.param(None, id="absent"),
            pytest.param(
                ("outer-active", "outer-tier", "0", "latin-1:strict"),
                id="non-default",
            ),
            pytest.param(("", "", "", ""), id="empty"),
        ],
    )
    def test_real_cli_mutations_are_restored_exactly(self, initial, monkeypatch, tmp_path):
        names = (
            "KIROCREW_SANDBOX_ACTIVE",
            "KIROCREW_SANDBOX_LEVEL",
            "PYTHONUTF8",
            "PYTHONIOENCODING",
        )
        for index, name in enumerate(names):
            # Record an undo even for absent keys, so a failed assertion cannot
            # leak the deliberately mutated process environment.
            monkeypatch.setenv(name, "test-sentinel")
            if initial is None:
                monkeypatch.delenv(name)
            else:
                monkeypatch.setenv(name, initial[index])
        before = {name: os.environ.get(name) for name in names}

        # On Windows the real initializer also reconfigures stdout/stderr. Give it
        # test-owned streams so that side effect is observed without changing
        # pytest's capture streams for later tests.
        stdout = io.TextIOWrapper(io.BytesIO(), encoding="utf-8", errors="strict")
        stderr = io.TextIOWrapper(io.BytesIO(), encoding="utf-8", errors="strict")
        monkeypatch.setattr(sys, "stdout", stdout)
        monkeypatch.setattr(sys, "stderr", stderr)
        monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(tmp_path))
        monkeypatch.setattr(sys, "argv", ["kirocrew", "--help"])

        definition = _root._floor_monkeypatch
        cycle = getattr(definition, "__wrapped__", definition)()
        next(cycle)
        try:
            with pytest.raises(SystemExit) as exit_info:
                cli.main()
            assert exit_info.value.code == 0
            assert "KIROCREW_SANDBOX_ACTIVE" not in os.environ
            assert "KIROCREW_SANDBOX_LEVEL" not in os.environ
            assert os.environ["PYTHONUTF8"] == "1"
            assert os.environ["PYTHONIOENCODING"] == "utf-8:backslashreplace"
            expected_errors = "backslashreplace" if cli.platform_compat.IS_WINDOWS else "strict"
            assert stdout.errors == expected_errors
            assert stderr.errors == expected_errors
        finally:
            cycle.close()

        assert {name: os.environ.get(name) for name in names} == before


class TestTheHooksDispatcherIsRestored:
    """``hooks_integration._lifecycle_dispatcher`` goes back after every test.

    ``init_hooks_system`` -- reached by every test that builds the real dashboard app --
    assigns the process-wide dispatcher (and the route registry beside it) and nothing
    in production clears them: a gateway sets them once. In a worker the LAST such test's
    dispatcher, with whatever it passed as ``cron_service`` (routinely a ``MagicMock``),
    therefore reaches every later test; the trust-revoke teardown awaits that mock's
    cron store and answers 409 ``teardown_incomplete``. Seven to nine of
    ``test_trusted_apps_api.py``'s revoke tests were red in every round of a five-run
    hygiene sweep with exactly that body, all green alone.

    ``conftest._restore_hooks_integration_globals`` is what removes the class; without a
    test, an edit to it reverts silently. Its teardown is driven directly (see
    ``_autouse_floor_generator``), so the proof needs no adjacent observer test.
    """

    @staticmethod
    def _hi():
        from kiro_crew.apps import hooks_integration

        return hooks_integration

    def test_this_test_starts_with_no_dispatcher(self) -> None:
        """Which is only true if no earlier test on this worker left one behind."""
        assert self._hi()._lifecycle_dispatcher is None

    def test_a_leaked_dispatcher_is_removed_by_the_floors_own_teardown(self) -> None:
        """One full cycle of the real fixture: snapshot, leak through the real installer, restore.

        The leak is produced by the real ``init_hooks_system`` on a bare aiohttp app with a
        ``MagicMock`` cron service -- exactly how ~170 dashboard tests produce it -- so
        this still fails if the production install site moves. A failure part-way cannot
        leak past this test: the live autouse instance of the same fixture wraps it too.
        """
        from unittest.mock import MagicMock

        from aiohttp import web

        hi = self._hi()
        cycle = _autouse_floor_generator("_restore_hooks_integration_globals")()
        next(cycle)  # snapshot, taken while both slots are empty

        hi.init_hooks_system(web.Application(), cron_service=MagicMock())
        leaked = hi._lifecycle_dispatcher
        assert leaked is not None, "init_hooks_system no longer installs the dispatcher"
        assert hi._route_registry is not None

        with pytest.raises(StopIteration):
            next(cycle)  # the restore under test
        assert hi._lifecycle_dispatcher is None, (
            f"the slot still holds {hi._lifecycle_dispatcher!r} -- the next revoke test on "
            "this worker would await its MagicMock cron store and answer 409"
        )
        assert hi._route_registry is None

    def test_the_restore_target_is_what_the_test_inherited(self, monkeypatch) -> None:
        """The floor restores the INHERITED dispatcher, so a higher-scoped installer survives."""
        from unittest.mock import MagicMock

        from aiohttp import web

        hi = self._hi()
        sentinel = object()
        monkeypatch.setattr(hi, "_lifecycle_dispatcher", sentinel)
        monkeypatch.setattr(hi, "_route_registry", None)

        cycle = _autouse_floor_generator("_restore_hooks_integration_globals")()
        next(cycle)  # snapshot taken while the sentinel holds the slot

        hi.init_hooks_system(web.Application(), cron_service=MagicMock())
        assert hi._lifecycle_dispatcher is not sentinel

        with pytest.raises(StopIteration):
            next(cycle)
        assert hi._lifecycle_dispatcher is sentinel, (
            "the floor restored past the dispatcher this cycle inherited, so a "
            "higher-scoped installer would be torn out after its first test"
        )
