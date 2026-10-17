"""Ordering and durability of the queued member event-log appends.

One worker runs the appends in submission order, every writer reaches it
through the hooks module, the shutdown drain waits for a queued append within a
bound, and the Slack gateway publishes a patrol frame only after its append
has landed.
"""

from __future__ import annotations

import threading
import time

from members_eventlog_wiring_helpers import _fresh_eventlog  # noqa: F401

from kiro_crew import eventlog_hooks


class TestMemberEventLogAppendsAreOrdered:
    """Two appends handed to the DEFAULT thread pool complete in either order.

    The member log is the authoritative record other surfaces read, so two
    transitions close together could be written newest-first with no crash and no
    unusual load involved. The single-worker executor lives in ``eventlog_hooks``,
    beside the ``emit`` every writer already calls, so a writer cannot offload an
    append without seeing it.
    """

    def test_the_shared_executor_has_exactly_one_worker(self):
        from kiro_crew import eventlog_hooks as hooks

        ex = hooks.io_executor()
        assert ex is hooks.io_executor(), "a fresh pool per call orders nothing"
        assert ex._max_workers == 1, (
            f"the pool has {ex._max_workers} workers, so two appends can still "
            "execute out of submission order"
        )

    def test_it_runs_submissions_in_submission_order(self):
        from kiro_crew import eventlog_hooks as hooks

        ex = hooks.io_executor()
        seen: list[int] = []

        def _job(n: int) -> None:
            # A later submission that finishes faster must still land later.
            # flake-ok: pre-existing 20 ms hold on the first job so a pool with more than one worker would finish it last, moved verbatim; tracked for a de-flake pass
            time.sleep(0.02 if n == 0 else 0.0)
            seen.append(n)

        for f in [ex.submit(_job, n) for n in range(4)]:
            f.result(timeout=5)
        assert seen == [0, 1, 2, 3], f"appends completed out of order: {seen}"

    def test_no_offloaded_event_log_append_uses_the_default_pool(self):
        """Scans the WHOLE package, because the site this guard was first written
        for missed a third writer in another file entirely -- a per-module check
        cannot see that. Matched by what the offloaded function DOES (it calls
        ``eventlog_hooks.emit``) rather than by its name: a first attempt keyed on
        the name ``_emit`` and flagged session_map's SEL audit, which is a
        different subsystem and already retains and inspects its future.
        """
        import ast
        import pathlib

        import kiro_crew

        root = pathlib.Path(kiro_crew.__file__).parent
        offenders: list[str] = []
        for f in sorted(root.rglob("*.py")):
            src = f.read_text(encoding="utf-8", errors="replace")
            if "eventlog_hooks.emit" not in src:
                continue
            tree = ast.parse(src)
            appenders = {
                node.name
                for node in ast.walk(tree)
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and "eventlog_hooks.emit" in (ast.get_source_segment(src, node) or "")
            }
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "run_in_executor"
                    and len(node.args) >= 2
                    and isinstance(node.args[0], ast.Constant)
                    and node.args[0].value is None
                    and isinstance(node.args[1], ast.Name)
                    and node.args[1].id in appenders
                ):
                    offenders.append(f"{f.relative_to(root)}:{node.lineno}")
        assert not offenders, (
            "these offload an event-log append to the default multi-worker pool, "
            f"so their order is luck: {offenders}"
        )

    def test_every_writer_reaches_the_executor_through_the_hooks_module(self):
        import pathlib

        import kiro_crew

        root = pathlib.Path(kiro_crew.__file__).parent
        calls = sum(
            f.read_text(encoding="utf-8", errors="replace").count("eventlog_hooks.submit(")
            for f in root.rglob("*.py")
        )
        assert calls >= 3, f"expected the slot, message and patrol writers to share it, saw {calls}"


class TestQueuedAppendsSurviveShutdown:
    """A queued append is lost if the process exits before the worker runs, and
    the log is append-only with no replay, so the entry is simply gone. The
    shutdown window is ordinary operation rather than a crash.
    """

    def test_the_drain_waits_for_a_queued_append_to_finish(self):
        landed: list[str] = []

        def _slow_append() -> None:
            # flake-ok: pre-existing 50 ms sleep inside the stubbed slow append the drain waits for, moved verbatim; tracked for a de-flake pass
            # nosemgrep: kirocrew.test-sleep-as-barrier -- pre-existing sleep barrier, moved verbatim; tracked for a de-flake pass
            time.sleep(0.05)
            landed.append("written")

        eventlog_hooks.submit(_slow_append)
        assert eventlog_hooks.drain_for_shutdown(timeout=10) is True
        assert landed == ["written"], (
            "the drain returned before the queued append reached the log, so exit "
            "would report a complete record that is missing this entry"
        )

    def test_the_drain_reports_false_rather_than_hanging_on_a_wedged_append(self):
        """Bounded for the reason the crew-log drain documents: a wedged
        filesystem must delay exit, not hang it. False lets the caller report a
        short log instead of believing a complete one."""
        release = threading.Event()
        try:
            eventlog_hooks.submit(lambda: release.wait(30))
            # flake-ok: pre-existing 0.2 s drain budget the test lets run out on purpose against a wedged append: the timeout is the behaviour under test, moved verbatim
            assert eventlog_hooks.drain_for_shutdown(timeout=0.2) is False
        finally:
            release.set()
            eventlog_hooks.drain_for_shutdown(timeout=10)

    def test_submitting_registers_the_shutdown_drain(self):
        """The drain has to be ARMED by ordinary use: a hook that is only
        registered by an explicit setup call is a hook nobody calls."""
        eventlog_hooks.submit(lambda: None)
        eventlog_hooks.drain_for_shutdown(timeout=10)
        # The flag is the observable: atexit exposes no public list of callbacks.
        assert eventlog_hooks._drain_registered is True, (
            "submitting work did not arm the atexit drain, so a queued append "
            "would be lost at exit with nothing waiting for it"
        )

    def test_a_submit_failure_does_not_break_the_hooked_path(self, monkeypatch):
        """Writers call this from a best-effort hook, so a pool that cannot
        accept work must not propagate."""
        monkeypatch.setattr(
            eventlog_hooks, "io_executor", lambda: (_ for _ in ()).throw(RuntimeError("no pool"))
        )
        eventlog_hooks.submit(lambda: None)  # must not raise


def _gateway_source() -> str:
    """The Slack gateway facade's text followed by its composed owners' text."""
    import pathlib

    import kiro_crew

    facade = pathlib.Path(kiro_crew.__file__).parent / "slack" / "gateway.py"
    return "\n".join(
        path.read_text(encoding="utf-8")
        for path in (facade, *sorted((facade.parent / "gateway_runtime").glob("*.py")))
    )


class TestPatrolTransitionPersistsBeforeItPublishes:
    """The patrol frame must not be broadcast before its append has landed.

    The member log -- not the live loop -- is what the Crew Members drawer reads
    for a patrol's stop REASON across a restart:
    ``reconcile_members_at_startup`` closes a log that still reads ``armed`` with
    no live loop as ``reason='interrupted'``. So a transition published before its
    append landed lets a crash in that window replace the real reason
    (``runtime_budget``, say) permanently, with no later event able to correct it.

    Pinned at the CALL SITE rather than by driving the observer, because the
    observer is a closure built during gateway construction and reaching it needs
    the whole gateway stood up. The invariant that makes the ordering enforceable
    is that exactly ONE place performs the publish: once every caller goes through
    that one function, moving it relative to the append is a visible edit rather
    than an easy accident. Matched on the enclosing function of the call, not on a
    substring, so wrapping the call does not defeat it.
    """

    def _publish_call_owners(self) -> list[str]:
        """Names of the functions that broadcast an ``autonudge_state`` frame."""
        import ast

        src = _gateway_source()
        tree = ast.parse(src)
        owners: list[str] = []
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for inner in ast.walk(node):
                if not isinstance(inner, ast.Call) or not inner.args:
                    continue
                first = inner.args[0]
                if isinstance(first, ast.Constant) and first.value == "autonudge_state":
                    # Attribute the call to the INNERMOST enclosing function: a
                    # nested def is walked by its parent too, so without this
                    # every ancestor would be reported as an owner.
                    deepest = node.name
                    for cand in ast.walk(node):
                        if (
                            isinstance(cand, (ast.FunctionDef, ast.AsyncFunctionDef))
                            and cand is not node
                            and any(inner is c for c in ast.walk(cand))
                        ):
                            deepest = cand.name
                    owners.append(deepest)
        return owners

    def test_the_frame_is_published_from_exactly_one_function(self):
        owners = sorted(set(self._publish_call_owners()))
        assert owners == ["_publish"], (
            "an autonudge_state frame is broadcast from "
            f"{owners} rather than only from `_publish`; a second publish site is "
            "how the frame gets ahead of the append that makes it durable"
        )

    def test_the_observer_defers_the_publish_to_the_submitted_append(self):
        """The deferred path must exist AND be the one the append drives.

        Without this the test above passes on a `_publish` called straight from
        the observer, which is the pre-fix behaviour wearing the fixed shape.
        """
        import ast

        src = _gateway_source()
        tree = ast.parse(src)
        emit_fns = [
            n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "_emit_patrol"
        ]
        assert emit_fns, "`_emit_patrol` is gone; the append is no longer offloaded"
        publishes_inside = [
            inner
            for fn in emit_fns
            for inner in ast.walk(fn)
            if isinstance(inner, ast.Call)
            and (
                (isinstance(inner.func, ast.Name) and inner.func.id == "_publish")
                or (
                    isinstance(inner.func, ast.Attribute)
                    and inner.func.attr == "call_soon_threadsafe"
                )
            )
        ]
        assert publishes_inside, (
            "`_emit_patrol` appends but never publishes, so the frame is published "
            "somewhere else -- which is the publish-before-persist ordering again"
        )

    def test_the_observers_own_publish_is_only_the_nothing_was_queued_fallback(self):
        """Closes the gap the two tests above leave open.

        Both of those still pass if the observer ALSO calls ``_publish()``
        unconditionally before submitting the append -- the frame stays funnelled
        through one function and the job still publishes, while the pre-fix
        ordering is fully restored. What rules that out is that the observer's own
        publish is reachable only when nothing was queued, i.e. it sits under an
        ``if``.
        """
        import ast

        src = _gateway_source()
        tree = ast.parse(src)
        observers = [
            n
            for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
            and any(
                isinstance(c, ast.Call)
                and isinstance(c.func, ast.Attribute)
                and c.func.attr == "submit"
                and any(isinstance(a, ast.Name) and a.id == "_emit_patrol" for a in c.args)
                for c in ast.walk(n)
            )
        ]
        assert observers, "no function submits `_emit_patrol`; the append is not queued"

        unguarded: list[int] = []
        for obs in observers:
            # Statements of the observer itself, not of the nested `_emit_patrol`
            # (whose publish is the intended one) and not of `_publish` itself.
            nested = {
                id(x)
                for d in ast.walk(obs)
                if isinstance(d, ast.FunctionDef) and d.name in ("_emit_patrol", "_publish")
                for x in ast.walk(d)
            }
            for node in ast.walk(obs):
                if id(node) in nested or not isinstance(node, ast.Expr):
                    continue
                call = node.value
                if not (
                    isinstance(call, ast.Call)
                    and isinstance(call.func, ast.Name)
                    and call.func.id == "_publish"
                ):
                    continue
                # The nearest enclosing `if` is not enough: this whole block sits
                # inside the observer's own `if loop is not None:` guard, so "is
                # inside SOME If" is true by construction and tests nothing. Only
                # an `if` whose TEST reads `_publish_deferred` makes the call the
                # nothing-was-queued fallback.
                guards = [
                    anc
                    for anc in ast.walk(obs)
                    if isinstance(anc, ast.If)
                    and any(
                        isinstance(t, ast.Name) and t.id == "_publish_deferred"
                        for t in ast.walk(anc.test)
                    )
                ]
                inside_if = any(any(node is s for s in ast.walk(g)) for g in guards)
                if not inside_if:
                    unguarded.append(node.lineno)
        assert not unguarded, (
            "the observer publishes the patrol frame unconditionally at "
            f"gateway.py line(s) {sorted(set(unguarded))}, so the frame is broadcast "
            "whether or not the append was queued -- publish-before-persist again"
        )

    def test_the_publish_is_gated_on_the_appends_answer_not_merely_ordered_after_it(self):
        """Ordering alone is not persistence.

        The three tests above pin that the publish happens inside the submitted
        append and not before it. They all still pass when `_emit_patrol` DISCARDS
        what `emit` answered and publishes regardless -- which is exactly the
        report-success-on-a-failed-write shape `persist-before-you-publish`
        forbids, because the frame then asserts a transition the ledger does not
        hold and the drawer reads the ledger for the stop reason after a restart.

        So: the result must be ASSIGNED (a bare expression statement is the
        return-not-read shape), and a guard on that name must be able to leave the
        function before any publish is reached.
        """
        import ast

        src = _gateway_source()
        tree = ast.parse(src)
        emit_fns = [
            n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "_emit_patrol"
        ]
        assert emit_fns, "`_emit_patrol` is gone; the append is no longer offloaded"

        for fn in emit_fns:
            # The emit's answer is bound to a name rather than thrown away.
            bound: set[str] = set()
            for node in ast.walk(fn):
                if not isinstance(node, ast.Assign):
                    continue
                if any(
                    isinstance(c, ast.Call)
                    and isinstance(c.func, ast.Attribute)
                    and c.func.attr == "emit"
                    for c in ast.walk(node.value)
                ):
                    bound.update(t.id for t in ast.walk(node) if isinstance(t, ast.Name))
            assert bound, (
                "`_emit_patrol` calls emit without binding its answer, so the "
                "publish below cannot depend on whether the append landed"
            )
            # A guard on one of those names returns early, so a failed append
            # reaches no publish.
            guarded_returns = [
                g
                for g in ast.walk(fn)
                if isinstance(g, ast.If)
                and any(isinstance(t, ast.Name) and t.id in bound for t in ast.walk(g.test))
                and any(isinstance(r, ast.Return) for r in ast.walk(g))
            ]
            assert guarded_returns, (
                "nothing in `_emit_patrol` leaves the function on a refused "
                f"append (names bound from emit: {sorted(bound)}), so the frame is "
                "published whether or not the ledger holds the transition"
            )

    def test_the_fallback_publish_is_not_reachable_on_a_refused_queue(self):
        """`_publish_deferred` answers "will a worker publish", not "was anything owed".

        Both are false when the queue REFUSES the append, and the fallback that owns
        the frame in that case published it anyway -- asserting a patrol transition the
        ledger never received. Gating `_emit_patrol` (the test above) does not close
        this: on a refusal `_emit_patrol` never runs at all.

        So the fallback's test must ALSO depend on whether a durable transition applied,
        not on `_publish_deferred` alone.
        """
        import ast

        src = _gateway_source()
        tree = ast.parse(src)

        # The guard whose test reads `_publish_deferred` and which calls `_publish`.
        fallbacks = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.If):
                continue
            names = {t.id for t in ast.walk(node.test) if isinstance(t, ast.Name)}
            if "_publish_deferred" not in names:
                continue
            calls_publish = any(
                isinstance(c, ast.Call) and isinstance(c.func, ast.Name) and c.func.id == "_publish"
                for c in ast.walk(node)
            )
            if calls_publish:
                fallbacks.append((node, names))

        assert fallbacks, (
            "no guard on `_publish_deferred` publishes the frame any more; if the "
            "fallback moved, this pin must follow it rather than be deleted"
        )
        for node, names in fallbacks:
            assert len(names) > 1, (
                f"the fallback publish at line {node.lineno} is gated on "
                "`_publish_deferred` ALONE, so a refused queue -- which is also a "
                "false `_publish_deferred` -- publishes a transition the ledger "
                "never received"
            )
