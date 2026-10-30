"""Tests that nothing on the HTTP surface blocks the event loop.

The autonomy gate, ``rotation.describe`` and the ``/state`` poll can each reach a blocking
call (a ``gh api user`` spawn, an entry-point scan), and every store or ledger read parses a
file whose size grows with use. Each must run off the loop. The behavioural tests count
heartbeat ticks while a deliberately slow stub runs; the structural ones scan the handlers'
source for a blocking call made directly on the loop.
"""

import shutil
import tempfile
import unittest
from unittest import mock

from aiohttp import web

from kiro_crew.apps.builtins.ops_mission_control.backend import routes
from kiro_crew.apps.builtins.ops_mission_control.tests.routes_helpers import (
    _handler_source,
    _http_surface_source,
)


class TestTheAuthorizationGateDoesNotBlockTheLoop(unittest.IsolatedAsyncioTestCase):
    """``authorize_action`` must never run inline on the asyncio loop.

    It is synchronous by design, and its off-shift check reads the committed schedule and
    -- with no ``schedule-file.github_login`` configured, which is the documented default
    -- resolves this instance's identity by spawning ``gh api user``: a blocking HTTPS
    round trip with a 10s timeout, on the FIRST call of a fresh gateway process.

    ``/incident/action`` and ``/incident/proposal/decide`` are the two handlers that reach
    the gate without awaiting ``registry.resolve_shift()`` first, so nothing has warmed the
    login cache off-loop by the time they get there. Run inline, the spawn freezes every
    other task on the loop -- the user's chat turn and the liveness heartbeat -- for the
    whole window.

    Asserted behaviourally rather than by grepping for ``to_thread``: a heartbeat coroutine
    ticks while the request is in flight, so the test fails if the loop ever stalls,
    whatever mechanism a future refactor uses to stay off it.
    """

    def setUp(self):
        import os

        self.tmp = tempfile.mkdtemp()
        self._prev = os.environ.get("KIROCREW_HOME")
        os.environ["KIROCREW_HOME"] = self.tmp

    def tearDown(self):
        import os

        if self._prev is None:
            os.environ.pop("KIROCREW_HOME", None)
        else:
            os.environ["KIROCREW_HOME"] = self._prev
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _incident(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend import models, store

        inc = store.claim(
            models.Signal.create(source="webhook", native_id="probe-1", title="latency"),
            operating_mode=models.MODE_ACT,
        )
        assert inc is not None
        return inc

    async def test_a_slow_identity_lookup_does_not_stall_other_tasks(self):
        import asyncio
        import time

        from aiohttp.test_utils import TestClient, TestServer

        incident = self._incident()
        app = web.Application()
        routes.register_routes(app)

        blocking_secs = 0.3

        def _slow_gate(_signal, _action):
            # Stands in for the `gh api user` spawn: synchronous, and long enough that a
            # stalled loop is unambiguous rather than a timing coincidence.
            # flake-ok: pre-existing blocking stub, moved verbatim; tracked for de-flaking
            time.sleep(blocking_secs)
            return (True, "granted by rule")

        ticks = 0

        async def _heartbeat():
            nonlocal ticks
            while True:
                # flake-ok: pre-existing heartbeat tick, moved verbatim; tracked for de-flaking
                await asyncio.sleep(blocking_secs / 10)
                ticks += 1

        with mock.patch.object(routes, "is_app_enabled", return_value=True):
            with mock.patch.object(routes.rotation, "authorize_action", _slow_gate):
                async with TestClient(TestServer(app)) as client:
                    beat = asyncio.create_task(_heartbeat())
                    try:
                        resp = await client.post(
                            "/api/apps/ops-mission-control/incident/action",
                            json={
                                "id": incident.incident_id,
                                "action": "comment",
                                "sink": "webhook",
                            },
                        )
                        self.assertEqual(resp.status, 200)
                    finally:
                        beat.cancel()

        # Inline, the loop gets zero slices during the sleep. Off-loop it gets ~10; assert
        # a floor well under that so the test is not itself timing-flaky.
        # flake-ok: pre-existing tick floor, moved verbatim; tracked for de-flaking
        self.assertGreaterEqual(
            ticks,
            3,
            "the event loop stalled during authorize_action — it must run off-loop "
            f"(only {ticks} heartbeat tick(s) in {blocking_secs}s)",
        )


class TestRotationDescribeDoesNotBlockTheLoop(unittest.IsolatedAsyncioTestCase):
    """``rotation.describe`` reaches the same ``gh api user`` spawn the gate does.

    ``describe()`` -> ``is_primary()`` -> ``_schedule_me()`` -> ``resolve_login()``, which
    spawns ``gh api user`` synchronously with a 10s timeout on a cold login cache.

    An earlier revision of these handlers argued the inline call was safe because the awaited
    ``registry.resolve_shift()`` above always warms that cache first. That was WRONG and
    review caught it: ``resolve_shift`` wraps each source in ``asyncio.wait_for(...,
    DEFAULT_POLL_TIMEOUT_SECS)``, and a timeout cancels the AWAITING coroutine while the
    ``to_thread`` worker keeps running -- so the poll can give up with the cache still unset,
    and ``describe()`` then pays the full spawn on the loop. "Something upstream probably
    warmed it" is not a guarantee.

    One of the three sites was subtler than the other two:
    ``to_thread(handover.build, providers, rotation.describe(shift))`` moved *build* off the
    loop while still evaluating ``describe()`` on it, because arguments are computed before
    the call.
    """

    def test_no_async_function_evaluates_tier_states_on_the_loop(self):
        """`tier_states` reaches the same `gh api user` spawn as `describe`, and two ASYNC
        callers evaluated it inline: `dispatch.run_cycle` (the 120s heartbeat) and
        `rotation.apply_tiers` (awaited from the default-enabled 300s rotation-check cron via
        `POST /rotation/arm`). Same 10s frozen-loop exposure `describe` had; found in review.

        `rotation.describe` is deliberately NOT flagged: it is SYNC and its callers already
        offload it (pinned by the sibling test above). This walks only `async def`s, so a sync
        helper computing `tier_states` for a caller to offload does not trip it.
        """
        import ast
        import inspect

        from kiro_crew.apps.builtins.ops_mission_control.backend import dispatch, rotation

        offenders = []
        for mod in (dispatch, rotation):
            tree = ast.parse(inspect.getsource(mod))
            for fn in ast.walk(tree):
                if not isinstance(fn, ast.AsyncFunctionDef):
                    continue
                nested = {
                    id(n)
                    for inner in ast.walk(fn)
                    if inner is not fn
                    and isinstance(inner, (ast.FunctionDef, ast.AsyncFunctionDef))
                    for n in ast.walk(inner)
                }
                for node in ast.walk(fn):
                    if id(node) in nested or not isinstance(node, ast.Call):
                        continue
                    fname = (
                        node.func.attr
                        if isinstance(node.func, ast.Attribute)
                        else (node.func.id if isinstance(node.func, ast.Name) else "")
                    )
                    if fname != "tier_states":
                        continue
                    # An `await asyncio.to_thread(tier_states, ...)` passes tier_states as an
                    # ARG, so the offending shape is tier_states being CALLED (it has args that
                    # are the shift, i.e. it is `tier_states(shift)`), not named.
                    if node.args or node.keywords:
                        offenders.append(f"{mod.__name__}:{node.lineno}")
        self.assertEqual(
            offenders,
            [],
            "tier_states() can spawn `gh api user`; every async caller must "
            f"`await asyncio.to_thread(tier_states, shift)`. Inline at: {offenders}",
        )

    def test_no_handler_evaluates_describe_on_the_loop(self):
        import re

        source = _http_surface_source()
        # Code only: the comments explaining the fix necessarily quote the broken shape.
        code = "\n".join(line for line in source.splitlines() if not line.lstrip().startswith("#"))
        # A floor on what the scan sees, so it cannot pass by finding nothing: `/state`,
        # `/handover` and `/rotation` each resolve the rotation.
        self.assertEqual(
            len(re.findall(r"to_thread\(\s*rotation\.describe", code)),
            3,
            "expected the three rotation.describe sites (/state, /handover, /rotation)",
        )
        inline = [
            line.strip()
            for line in code.splitlines()
            if "rotation.describe" in line
            and not re.search(r"to_thread\(\s*rotation\.describe", line)
        ]
        self.assertEqual(
            inline,
            [],
            "every rotation.describe() call must go through asyncio.to_thread -- it can "
            f"spawn `gh api user` on a cold cache. Inline: {inline}",
        )

    async def test_a_slow_identity_lookup_does_not_stall_the_state_handler(self):
        """Behavioural: the loop must keep running while /state resolves the rotation."""
        import asyncio
        import time

        from aiohttp.test_utils import TestClient, TestServer

        blocking_secs = 0.3

        def _slow_describe(_shift):
            # flake-ok: pre-existing blocking stub, moved verbatim; tracked for de-flaking
            time.sleep(blocking_secs)
            return {"mode": "observe", "primary": False}

        ticks = 0

        async def _heartbeat():
            nonlocal ticks
            while True:
                # flake-ok: pre-existing heartbeat tick, moved verbatim; tracked for de-flaking
                await asyncio.sleep(blocking_secs / 10)
                ticks += 1

        app = web.Application()
        routes.register_routes(app)
        with mock.patch.object(routes, "is_app_enabled", return_value=True):
            with mock.patch.object(routes.rotation, "describe", _slow_describe):
                async with TestClient(TestServer(app)) as client:
                    beat = asyncio.create_task(_heartbeat())
                    try:
                        resp = await client.get("/api/apps/ops-mission-control/rotation")
                        self.assertEqual(resp.status, 200)
                    finally:
                        beat.cancel()

        # flake-ok: pre-existing tick floor, moved verbatim; tracked for de-flaking
        self.assertGreaterEqual(
            ticks,
            3,
            "the event loop stalled while resolving the rotation -- describe() must run "
            f"off-loop (only {ticks} tick(s) in {blocking_secs}s)",
        )


class TestStatePollDoesNotBlockOnEntryPointDiscovery(unittest.IsolatedAsyncioTestCase):
    """`/state` is POLLED, so anything slow in it stalls the loop on every poll.

    `companion.companion_summary()` walks `importlib.metadata.entry_points()`, which
    enumerates every installed distribution's metadata from disk. Measured in this
    environment at ~9.3ms against ~0.03-0.05ms for the sibling reads in the same handler
    (`store.counts_by_status`, `ledger.stats`) -- roughly 200x, and that is with a warm page
    cache and a small site-packages. Found in review.

    The sibling reads are deliberately left inline: they touch one small per-instance JSON
    file each, and a thread hop would cost more than it saves. The measurement is what
    decided that, not a guess.
    """

    def test_companion_discovery_is_off_the_loop(self):
        import re

        source = _handler_source("_handle_state")
        code = "\n".join(line for line in source.splitlines() if not line.lstrip().startswith("#"))
        inline = [
            line.strip()
            for line in code.splitlines()
            if "companion_summary" in line
            and not re.search(r"to_thread\(\s*companion\.companion_summary", line)
        ]
        self.assertEqual(
            inline,
            [],
            f"companion_summary() scans installed distributions; keep it off-loop: {inline}",
        )
        self.assertIn("companion_summary", code, "the scan must see the /state call it guards")

    async def test_a_slow_entry_point_scan_does_not_stall_the_loop(self):
        import asyncio
        import time

        from aiohttp.test_utils import TestClient, TestServer

        blocking_secs = 0.3

        def _slow_summary():
            # flake-ok: pre-existing blocking stub, moved verbatim; tracked for de-flaking
            time.sleep(blocking_secs)
            return []

        ticks = 0

        async def _heartbeat():
            nonlocal ticks
            while True:
                # flake-ok: pre-existing heartbeat tick, moved verbatim; tracked for de-flaking
                await asyncio.sleep(blocking_secs / 10)
                ticks += 1

        app = web.Application()
        routes.register_routes(app)
        with mock.patch.object(routes, "is_app_enabled", return_value=True):
            with mock.patch.object(routes.companion, "companion_summary", _slow_summary):
                async with TestClient(TestServer(app)) as client:
                    beat = asyncio.create_task(_heartbeat())
                    try:
                        resp = await client.get("/api/apps/ops-mission-control/state")
                        self.assertEqual(resp.status, 200)
                    finally:
                        beat.cancel()

        # flake-ok: pre-existing tick floor, moved verbatim; tracked for de-flaking
        self.assertGreaterEqual(
            ticks,
            3,
            "the event loop stalled during /state -- companion discovery must run off-loop "
            f"(only {ticks} tick(s) in {blocking_secs}s)",
        )


class TestNoHandlerParsesAStoredFileOnTheLoop(unittest.IsolatedAsyncioTestCase):
    """Every store/ledger read on the HTTP surface must go through `asyncio.to_thread`.

    These helpers parse a JSON/JSONL file whose size grows with use, and they sit on POLLED
    endpoints. Measured in this environment:

      ledger.stats()             0.1ms @0    1.8ms @100    13ms @1k    93ms @5k   275ms @20k
      store.counts_by_status()   0.1ms @0    4.3ms @100    42ms @1k   188ms @5k
      store.open_incidents()     0.2ms @0    2.3ms @100    70ms @1k   251ms @5k

    A previous round measured `ledger.stats()` and `store.counts_by_status()` at ~0.03ms and
    left them inline as negligible. That measurement was taken against an EMPTY store and the
    conclusion generalised from it — the one input where the cost is zero by construction.
    Review caught `ledger.stats`; auditing for the class then found `counts_by_status`,
    `open_incidents` (called TWICE inline in `/state`), and inline index parses in
    `/handover`, `/incidents` and `/signals`.

    Asserted as a class rather than per call site, because the per-site version of this
    lesson has now been learned twice.
    """

    #: Helpers that read and parse a file whose size grows with use, split for the AST walk.
    #:
    #: `get_incident`/`find_by_signal` look like cheap single-record lookups and are not:
    #: both are `read_index().get(...)`, so each pays the FULL index parse. They were missing
    #: from the first version of this list, and review then found six inline calls across the
    #: incident, transition, action and proposal handlers. `read_log` reads a per-incident
    #: postmortem file. If a helper touches disk, it belongs here — "it only fetches one row"
    #: is about the return value, not about the work.
    FILE_PARSING_MODULES = frozenset({"store", "ledger"})
    FILE_PARSING_ATTRS = frozenset(
        {
            "open_incidents",
            "counts_by_status",
            "read_index",
            "recent_incidents",
            "get_incident",
            "find_by_signal",
            "read_log",
            "stats",
            "read_entries",
            # WRITES, not just reads. The first version of this list held only readers, and
            # review then found `store.update_fields` called inline in `slack_out.publish` —
            # a full read-modify-write of the incident index, on a coroutine `run_cycle`
            # awaits. A write parses the same file a read does and then rewrites it, so it is
            # strictly worse; "reads a growing file" was the wrong frame for the list.
            "update_fields",
            "transition",
            "claim",
            "decide_proposal",
        }
    )
    #: Deliberately NOT listed: `cron_service.list_jobs()`. It looks like exactly the shape
    #: this guard hunts and is not — `CronService.list_jobs` is documented cache-only (no
    #: lock open, no read, no hash) precisely so the dashboard WebSocket push and the Slack
    #: handlers can call it on the loop. Adding it here would flag correct code across the
    #: repo. `rotation.apply_tiers` still uses `list_jobs_async`, for FRESHNESS rather than
    #: for blocking: the snapshot only refreshes once per timer poll, so a pause made from
    #: the CLI could otherwise read as active and get "resumed".
    FILE_PARSING_ANY_RECEIVER: frozenset[str] = frozenset()
    #: Module-LOCAL helpers that read/scan a whole file. Called bare (`_helper()`), not as
    #: `module.attr()`, so the attribute walk above cannot see them — which is exactly how
    #: `ledger_sync._credential_bearing_lines` (a full read + regex scan of `ledger.jsonl`,
    #: called inline from `async def push`) reached review unnoticed after two rounds of
    #: widening this guard. Bare names are matched separately.
    #:
    #: The conflict probes joined after review found all four of them called bare inside
    #: `async def pull`/`push`/`_resolve_schedule_conflict`. They read like predicates, which is
    #: what hid them: `has_conflict()` reads the WHOLE ledger and scans every line for markers,
    #: and `resolve_conflict()` re-parses it and REWRITES it — on the one file in this app that
    #: grows without bound, from coroutines the heartbeat awaits. Same lesson as
    #: `_credential_bearing_lines` one round earlier: a bare name is invisible to the attribute
    #: walk, so every module-local disk toucher has to be listed by hand.
    FILE_PARSING_LOCALS = frozenset(
        {
            "_credential_bearing_lines",
            "has_conflict",
            "resolve_conflict",
            "schedule_has_conflict",
        }
    )
    #: Dotted form, for the `to_thread(f, g())`-shape check which is line-based by nature.
    FILE_PARSING = (
        "store.open_incidents",
        "store.counts_by_status",
        "store.read_index",
        "ledger.stats",
        "ledger.read_entries",
    )

    def test_no_async_function_parses_a_stored_file_inline(self):
        """AST over the WHOLE backend package, not a regex over one module.

        The first version of this guard was a line-based scan of `routes.py` only. Review then
        found `store.read_index()` inline in `dispatch.run_cycle` — a coroutine one module
        over, in the exact class this test was written to close. Two lessons, both applied
        here: match the LANGUAGE (an `ast` walk knows what is inside an `async def` and what
        is merely a call passed to `to_thread`, where a regex is guessing), and scope to the
        package rather than to the file where the bug happened to be found.
        """
        import ast
        import pathlib

        backend = pathlib.Path(routes.__file__).parent
        offenders = []
        for path in sorted(backend.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for fn in (n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef)):
                for node in ast.walk(fn):
                    if not isinstance(node, ast.Call):
                        continue
                    func = node.func
                    if (
                        isinstance(func, ast.Attribute)
                        and func.attr in self.FILE_PARSING_ATTRS
                        and isinstance(func.value, ast.Name)
                        and func.value.id in self.FILE_PARSING_MODULES
                    ):
                        offenders.append(
                            f"{path.name}:{node.lineno} "
                            f"{func.value.id}.{func.attr}() inside async {fn.name}()"
                        )
                    # Any receiver: `cron_service.list_jobs()` parses the cron store but the
                    # receiver is a parameter, so the module-scoped branch above is blind to it.
                    elif (
                        isinstance(func, ast.Attribute)
                        and func.attr in self.FILE_PARSING_ANY_RECEIVER
                    ):
                        offenders.append(
                            f"{path.name}:{node.lineno} .{func.attr}() inside async {fn.name}()"
                        )
                    # Bare local calls: `_credential_bearing_lines()` is a whole-file read plus
                    # a regex sweep, and no `module.attr` shape makes it visible above.
                    elif isinstance(func, ast.Name) and func.id in self.FILE_PARSING_LOCALS:
                        offenders.append(
                            f"{path.name}:{node.lineno} {func.id}() inside async {fn.name}()"
                        )
                # Synchronous `Path.read_text`/`write_text` DIRECTLY in the coroutine body —
                # `ledger_sync._ensure_repo` did this to a `.gitignore` on a path awaited
                # straight off the loop (`sync_safely`), the same class the module/local
                # checks above cover for the store parses. Skipped inside a nested `def`,
                # because that is precisely the fix (wrap the I/O and hand the callable to
                # `to_thread`) — flagging it there would forbid the remedy.
                nested_ids = {
                    id(n)
                    for inner in ast.walk(fn)
                    if inner is not fn
                    and isinstance(inner, (ast.FunctionDef, ast.AsyncFunctionDef))
                    for n in ast.walk(inner)
                }
                for node in ast.walk(fn):
                    if id(node) in nested_ids:
                        continue
                    if (
                        isinstance(node, ast.Call)
                        and isinstance(node.func, ast.Attribute)
                        and node.func.attr in {"read_text", "write_text"}
                    ):
                        offenders.append(
                            f"{path.name}:{node.lineno} .{node.func.attr}() "
                            f"inside async {fn.name}()"
                        )
        self.assertEqual(
            offenders,
            [],
            "these parse a growing file ON the event loop — pass them to asyncio.to_thread "
            "instead of calling them:\n  " + "\n  ".join(offenders),
        )

    def test_no_slow_call_is_evaluated_as_a_to_thread_argument(self):
        """`to_thread(f, g())` moves `f` off-loop and runs `g` ON it.

        This shape shipped once — `to_thread(handover.build, providers,
        rotation.describe(shift))` — and reads as fixed at a glance, which is why it needs
        its own check.
        """
        import re

        source = _http_surface_source()
        # A floor, so the scan cannot pass by finding nothing: the three `rotation.describe`
        # hops are `to_thread` lines this guard reads on every run.
        self.assertGreaterEqual(
            len(re.findall(r"to_thread\(\s*rotation\.describe", source)),
            3,
            "the scan must see the surface's to_thread calls",
        )
        offenders = []
        for lineno, line in enumerate(source.splitlines(), 1):
            if line.strip().startswith("#") or "to_thread(" not in line:
                continue
            args = line.split("to_thread(", 1)[1]
            # A `(` after the first argument name means a call is being evaluated inline.
            for helper in (*self.FILE_PARSING, "rotation.describe", "companion.companion_summary"):
                if re.search(rf"{re.escape(helper)}\s*\(", args):
                    offenders.append(f"{lineno}: {line.strip()}")
        self.assertEqual(
            offenders,
            [],
            "pass the callable to to_thread, do not call it:\n  " + "\n  ".join(offenders),
        )

    #: Helpers that reach LOOP-OWNED objects on `DashboardState` — the slot dicts, the
    #: Slack reverse index, or the notification bus (whose sink ends in `_broadcast`:
    #: `asyncio.Queue.put_nowait` per SSE client plus `asyncio.Event.set()`).
    LOOP_OWNED_STATE_HELPERS = (
        "slack_out.link_thread_to_investigation",
        "notify_out.notify_needs_human",
        "notify_out.notify_source_unhealthy",
        "notify_out.notify_work_released",
    )

    def test_no_loop_owned_state_helper_is_pushed_off_the_loop(self):
        """The INVERSE of the guard above, and the reason both are needed.

        `to_thread` is the right answer for file I/O and the wrong answer for loop-owned
        state. These helpers mutate `DashboardState` and broadcast to SSE clients through
        `asyncio` primitives that are only safe on the loop's own thread — `Event.set()`
        resolves waiter futures via `loop.call_soon`, whose contract is loop-thread-only
        (`call_soon_threadsafe` is the cross-thread door).

        The failure mode is what makes a test necessary rather than a comment: off the loop
        it *appears* to work, because the waiter future is marked done synchronously and the
        loop notices on its next poll. Nothing fails, so only a structural check keeps a
        future "wrap the blocking call" sweep from quietly reintroducing it — the same sweep
        that correctly wrapped every store parse on this path is what put it here. Found in
        review (GPT 5.6).

        Fails against the pre-fix source, which had both helpers inside `to_thread(...)`.
        """
        import re

        source = _http_surface_source()
        # The transition handler calls both of these on the loop; a scan that cannot find
        # them is not checking them.
        for helper in self.LOOP_OWNED_STATE_HELPERS[:2]:
            self.assertRegex(source, rf"\b{re.escape(helper)}\(", f"{helper} is not on the surface")
        offenders = []
        for lineno, line in enumerate(source.splitlines(), 1):
            if line.strip().startswith("#") or "to_thread(" not in line:
                continue
            for helper in self.LOOP_OWNED_STATE_HELPERS:
                if re.search(rf"\b{re.escape(helper)}\b", line):
                    offenders.append(f"{lineno}: {line.strip()}")
        # The call may also sit on a continuation line, so scan the whole statement.
        for helper in self.LOOP_OWNED_STATE_HELPERS:
            for match in re.finditer(
                rf"to_thread\(\s*\n?\s*{re.escape(helper)}\b", source, re.MULTILINE
            ):
                offenders.append(f"{source[: match.start()].count(chr(10)) + 1}: {helper}")
        self.assertEqual(
            sorted(set(offenders)),
            [],
            "these mutate loop-owned DashboardState and broadcast through asyncio "
            "primitives — call them directly on the loop, not via to_thread:\n  "
            + "\n  ".join(sorted(set(offenders))),
        )


class TestIncidentLogPathIsResolvedOffTheLoop(unittest.IsolatedAsyncioTestCase):
    """``/incident`` must resolve and stat the postmortem path in a worker thread.

    ``store.incident_log_path`` creates the incidents directory and the handler then stats
    the file, so both touch the disk on every poll of the board's incident pane. Asserted
    by thread identity rather than a timed heartbeat, so the test cannot be timing-flaky.
    """

    def setUp(self):
        import os

        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        env = mock.patch.dict(os.environ, {"KIROCREW_HOME": tmp})
        env.start()
        self.addCleanup(env.stop)

    @staticmethod
    def _closed_incident():
        from kiro_crew.apps.builtins.ops_mission_control.backend import models, store

        inc = store.claim(
            models.Signal.create(source="cloudwatch", native_id="alarm/off-loop", title="probe"),
            operating_mode=models.MODE_OBSERVE,
        )
        assert inc is not None
        store.transition(inc.incident_id, models.STATUS_INVESTIGATING)
        store.transition(inc.incident_id, models.STATUS_RESOLVED, resolution="done")
        return inc

    @staticmethod
    async def _get(incident_id: str):
        import json

        request = mock.MagicMock(spec=web.Request)
        request.query = {"id": incident_id}
        response = await routes._handle_incident(request)
        return response.status, json.loads(getattr(response, "text", "{}") or "{}")

    async def test_the_path_lookup_and_the_stat_run_off_the_loop_thread(self):
        import threading

        from kiro_crew.apps.builtins.ops_mission_control.backend import store

        inc = self._closed_incident()
        loop_thread = threading.get_ident()
        seen: list[tuple[str, int]] = []
        real_lookup = store.incident_log_path

        class _RecordingPath:
            def __init__(self, real):
                self._real = real

            def is_file(self):
                seen.append(("is_file", threading.get_ident()))
                return self._real.is_file()

            def __getattr__(self, name):
                return getattr(self._real, name)

            def __str__(self):
                return str(self._real)

        def _recording_lookup(incident_id):
            seen.append(("incident_log_path", threading.get_ident()))
            return _RecordingPath(real_lookup(incident_id))

        with mock.patch.object(store, "incident_log_path", _recording_lookup):
            status, payload = await self._get(inc.incident_id)

        self.assertEqual(status, 200)
        self.assertTrue(payload["log_path"].endswith(f"{inc.incident_id}.md"))
        # A floor, so the scan cannot pass by recording nothing.
        self.assertIn("is_file", [name for name, _ in seen])
        on_loop = [name for name, ident in seen if ident == loop_thread]
        self.assertEqual(on_loop, [], f"ran on the event loop thread: {on_loop}")

    async def test_a_lookup_failure_still_reports_no_path(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend import store

        inc = self._closed_incident()
        for exc in (ValueError("unsafe incident id"), OSError("disk gone")):
            with self.subTest(exc=type(exc).__name__):
                with mock.patch.object(store, "incident_log_path", side_effect=exc):
                    status, payload = await self._get(inc.incident_id)
                self.assertEqual(status, 200)
                self.assertEqual(payload["log_path"], "")


class _ThreadRecordingProvider:
    """A signal adapter that records which thread ran ``configured()``.

    The real adapters' ``configured()`` reads ``config.json`` and the secret store, so the
    catalog that asks every one of them must be built off the event loop.
    """

    id = "probe"
    display_name = "Probe"
    config_fields = ("region",)
    secret_fields = ("token",)

    def __init__(self):
        self.configured_threads: list[int] = []

    def configured(self) -> bool:
        import threading

        self.configured_threads.append(threading.get_ident())
        return True

    async def poll(self):
        return []


class TestProviderCatalogIsBuiltOffTheLoop(unittest.IsolatedAsyncioTestCase):
    """``/state``, ``/handover`` and ``/providers`` build the provider catalog in a thread.

    ``registry.catalog()`` calls every adapter's ``configured()``, and ``_provider_dict``
    then reads each provider's config and secret status: about ten adapters times one to
    three file reads. ``/state`` is the dashboard's hot poll, so inline those reads ran on
    the loop on every poll. Asserted by thread identity, so the test cannot be timing-flaky.
    """

    def setUp(self):
        import os

        from kiro_crew.apps.builtins.ops_mission_control.backend.http_routes import board
        from kiro_crew.apps.builtins.ops_mission_control.backend.registry import (
            OpsProviderRegistry,
        )

        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        self.enterContext(mock.patch.dict(os.environ, {"KIROCREW_HOME": tmp}))
        self.provider = _ThreadRecordingProvider()
        registry = OpsProviderRegistry()
        registry.register_signal_source(self.provider)
        self.enterContext(mock.patch.object(routes, "is_app_enabled", return_value=True))
        self.enterContext(mock.patch.object(routes, "get_registry", return_value=registry))
        self.config_threads: list[int] = []
        real_config = board.provider_config

        def _recording_config(provider_id):
            import threading

            self.config_threads.append(threading.get_ident())
            return real_config(provider_id)

        self.enterContext(mock.patch.object(board, "provider_config", _recording_config))

    async def _get(self, path: str) -> dict:
        import threading

        from aiohttp.test_utils import TestClient, TestServer

        app = web.Application()
        routes.register_routes(app)
        async with TestClient(TestServer(app)) as client:
            self.loop_thread = threading.get_ident()
            resp = await client.get(f"/api/apps/ops-mission-control/{path}")
            self.assertEqual(resp.status, 200)
            return await resp.json()

    def _assert_built_off_loop(self) -> None:
        # Floors, so the assertion cannot pass by recording nothing.
        self.assertTrue(self.provider.configured_threads, "configured() was never asked")
        self.assertTrue(self.config_threads, "provider_config() was never read")
        self.assertNotIn(self.loop_thread, self.provider.configured_threads)
        self.assertNotIn(self.loop_thread, self.config_threads)

    async def test_state_builds_the_catalog_off_the_loop(self):
        payload = await self._get("state")
        self.assertEqual([p["id"] for p in payload["providers"]], ["probe"])
        self.assertTrue(payload["providers"][0]["configured"])
        self._assert_built_off_loop()

    async def test_handover_builds_the_catalog_off_the_loop(self):
        payload = await self._get("handover")
        self.assertIn("text", payload)
        self._assert_built_off_loop()

    async def test_providers_builds_the_catalog_off_the_loop(self):
        payload = await self._get("providers")
        self.assertEqual([p["id"] for p in payload["providers"]], ["probe"])
        self.assertEqual(payload["providers"][0]["roles"], ["signal"])
        self._assert_built_off_loop()
