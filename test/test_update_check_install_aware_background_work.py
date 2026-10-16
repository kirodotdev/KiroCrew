"""The recurring update check, and the background work the restart waits for.

The coordinator re-checks after its interval and retries soon after a busy
deferral, and the restart boundary's census of in-flight work is pinned against
every launcher that registers it. The shared autouse fixture lives in
``update_check_install_aware_helpers.py``.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from update_check_install_aware_helpers import (  # noqa: F401 -- the autouse fixture registers by import
    _wheel_install,
)

from kiro_crew.dashboard.handlers import updates


class TestRecurringAutoUpdateCoordinator:
    def test_rechecks_after_the_interval(self):
        from kiro_crew.slack import gateway

        orch = object.__new__(gateway.GatewayOrchestrator)
        orch._check_for_updates = AsyncMock(side_effect=[None, asyncio.CancelledError()])
        sleep = AsyncMock(return_value=None)

        async def _drive() -> None:
            # flake-ok: the stdlib asyncio.sleep is an AsyncMock for the worker only while this test's asyncio.run drives the coordinator, so no real time passes
            with patch.object(gateway.asyncio, "sleep", sleep):
                with pytest.raises(asyncio.CancelledError):
                    await orch._run_update_checks()

        asyncio.run(_drive())
        assert orch._check_for_updates.await_count == 2
        sleep.assert_awaited_once_with(updates._UPDATE_CHECK_INTERVAL)

    def test_busy_deferral_retries_soon(self):
        from kiro_crew.slack import gateway

        orch = object.__new__(gateway.GatewayOrchestrator)
        calls = 0

        async def _check() -> None:
            nonlocal calls
            calls += 1
            if calls == 1:
                orch._update_apply_deferred = True
                return
            raise asyncio.CancelledError

        orch._check_for_updates = AsyncMock(side_effect=_check)
        sleep = AsyncMock(return_value=None)

        async def _drive() -> None:
            # flake-ok: the stdlib asyncio.sleep is an AsyncMock for the worker only while this test's asyncio.run drives the coordinator, so no real time passes
            with patch.object(gateway.asyncio, "sleep", sleep):
                with pytest.raises(asyncio.CancelledError):
                    await orch._run_update_checks()

        asyncio.run(_drive())
        sleep.assert_awaited_once_with(gateway.GatewayOrchestrator._UPDATE_BUSY_RETRY_SECS)


class TestUpdateBackgroundWorkContract:
    """Make the distributed restart boundary fail CI when a launcher drifts."""

    @staticmethod
    def _registrations(kind: str, tree: ast.AST) -> list[ast.AST]:
        found: list[ast.AST] = []
        for node in ast.walk(tree):
            if kind == "subagents":
                queued = (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "append"
                    and isinstance(node.func.value, ast.Attribute)
                    and node.func.value.attr == "_queue"
                )
                running = (
                    isinstance(node, ast.AugAssign)
                    and isinstance(node.target, ast.Attribute)
                    and node.target.attr == "_running_count"
                    and isinstance(node.op, ast.Add)
                )
                if queued or running:
                    found.append(node)
            elif kind == "cron":
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "add"
                    and isinstance(node.func.value, ast.Attribute)
                    and node.func.value.attr == "_running_script_ids"
                ):
                    found.append(node)
            elif kind == "taskrunner":
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "add"
                    and isinstance(node.func.value, ast.Attribute)
                    and node.func.value.attr == "_start_ids_in_flight"
                ):
                    found.append(node)
            elif kind == "workflows":
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "run_background"
                ):
                    found.append(node)
        return found

    @staticmethod
    def _is_admission_guard(statement: ast.stmt) -> bool:
        if not isinstance(statement, ast.If):
            return False
        names_gate = any(
            isinstance(node, ast.Attribute)
            and node.attr in {"admission_closed", "_admission_closed"}
            or isinstance(node, ast.Constant)
            and node.value == "admission_closed"
            for node in ast.walk(statement.test)
        )
        exits_on_closed = any(
            isinstance(node, (ast.Raise, ast.Return)) for node in ast.walk(statement)
        )
        return names_gate and exits_on_closed

    def _has_dominating_admission_guard(
        self, node: ast.AST, parents: dict[ast.AST, ast.AST]
    ) -> bool:
        current = node
        while current in parents:
            parent = parents[current]
            for field in ("body", "orelse", "finalbody"):
                block = getattr(parent, field, None)
                if not isinstance(block, list) or current not in block:
                    continue
                index = block.index(current)
                if any(self._is_admission_guard(statement) for statement in block[:index]):
                    return True
                break
            current = parent
        return False

    def test_one_cron_branch_cannot_cover_another(self):
        tree = ast.parse("""
async def callback(job):
    if job.command:
        if getattr(sessions, "admission_closed", False):
            return
        self._running_script_ids.add(job.id)
    if job.script:
        self._running_script_ids.add(job.id)
""")
        registrations = self._registrations("cron", tree)
        parents = {
            child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)
        }
        guarded = [
            self._has_dominating_admission_guard(registration, parents)
            for registration in registrations
        ]

        assert guarded == [True, False]

    def test_every_background_registration_is_gated_and_counted(self):
        root = Path(__file__).resolve().parents[1] / "src" / "kiro_crew"
        from kiro_crew.slack import gateway

        census = inspect.getsource(gateway.GatewayOrchestrator._in_flight_work_counts)
        for token in (
            "sessions.inbound_callback_count",
            "inbound_spool.pending_refusal_write_count()",
            'getattr(slot, "task", None)',
            'getattr(owner, "_handler_tasks", None)',
            'getattr(self, "_channel_handles", {})',
        ):
            assert token in census, f"channel callback census lost {token}"

        from kiro_crew.subagent import SubagentManager

        subagent_census = inspect.getsource(SubagentManager.pending_work_count.fget)
        for token in (
            "self._tasks.values()",
            "self._report_tasks",
            "self._followup_watchers.values()",
            'getattr(self, "_reconcile_task", None)',
            "self._abandoned_state_writers",
            "len(self._queue)",
            "self._running_count",
        ):
            assert token in subagent_census, f"subagent lifecycle census lost {token}"
        contracts = {
            # Subagent admission is a PACKAGE: every module in it is read as one
            # unit, so a registration site anywhere inside counts here and a new
            # module cannot carry one in unseen.
            # Six sites: three in gate.py (the spawn queue append, the
            # ClaimPoint reserve-then-commit running-count increment, the
            # registered start's running-count increment), the window refill
            # append in taskq_bridge_window.py, the resume reservation in waits.py,
            # and the approval-released start's queue append in pump.py
            # (``_admit_released_start_impl``: a start whose spawn prompt
            # resolved re-enters the queue to be metered into startup).
            "subagents": (
                sorted((root / "subagent_manager" / "admission").glob("*.py")),
                6,
                ("subagents.pending_work_count",),
            ),
            "cron": (
                [
                    root / "slack" / "gateway.py",
                    *sorted((root / "slack" / "gateway_runtime").glob("*.py")),
                ],
                2,
                ("len(self._running_script_ids)",),
            ),
            "taskrunner": (
                [root / "taskrunner.py", *sorted((root / "taskrunner_parts").glob("*.py"))],
                3,
                ("runner.running",),
            ),
            "workflows": (
                [root / "workflows" / "service.py"],
                3,
                ("workflows.list_runs()", 'run.get("status") == "running"'),
            ),
        }

        for kind, (paths, expected_count, census_tokens) in contracts.items():
            registrations: list[tuple[Path, ast.AST, dict[ast.AST, ast.AST]]] = []
            for path in paths:
                tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
                parents = {
                    child: parent
                    for parent in ast.walk(tree)
                    for child in ast.iter_child_nodes(parent)
                }
                registrations.extend(
                    (path, node, parents) for node in self._registrations(kind, tree)
                )
            assert (
                len(registrations) == expected_count
            ), f"{kind} registration sites changed; update the admission/census contract"
            for path, registration, parents in registrations:
                assert self._has_dominating_admission_guard(registration, parents), (
                    f"{kind} registers at {path.name}:{registration.lineno} without a "
                    "dominating terminating admission guard"
                )
            for token in census_tokens:
                assert token in census, f"{kind} registers work but is absent from the census"

    def test_every_pre_turn_channel_reserves_before_commands(self):
        root = Path(__file__).resolve().parents[1] / "src" / "kiro_crew"
        owners = {
            path.parent.name
            for path in root.glob("*/client.py")
            if "_handler_tasks" in path.read_text(encoding="utf-8")
        }
        if "_handler_tasks" in (root / "imessage" / "rpc.py").read_text(encoding="utf-8"):
            owners.add("imessage")
        owners.add("slack")  # host-managed handler set lives on GatewayOrchestrator
        # These transports dispatch through inline receive paths, so they need a
        # dispatcher reservation even where WhatsApp/Weixin now also expose the
        # upstream receive segment in a client handler registry.
        inline_owners = {"feishu", "whatsapp", "weixin"}
        owners.update(inline_owners)
        assert owners == {
            "discord",
            "feishu",
            "imessage",
            "slack",
            "teams",
            "telegram",
            "webex",
            "wecom",
            "whatsapp",
            "weixin",
        }

        dispatcher_paths = {owner: root / owner / "transport_dispatch.py" for owner in owners}
        dispatcher_paths["slack-native"] = root / "slack" / "handler.py"
        for owner, path in dispatcher_paths.items():
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            handlers = [
                node
                for node in ast.walk(tree)
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name in {"handle_message", "handle_message_transport"}
            ]
            assert handlers, f"{owner} has no inbound handler in {path.name}"
            for handler in handlers:
                admit_lines = [
                    node.lineno
                    for node in ast.walk(handler)
                    if isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id in {"admit_inbound_callback", "hold_inbound_callback"}
                ]
                assert admit_lines, f"{owner} handler does not reserve inbound callback work"
                governance_lines = [
                    node.lineno
                    for node in ast.walk(handler)
                    if isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id == "inbound_permitted"
                ]
                if owner in inline_owners:
                    assert governance_lines and min(admit_lines) < min(
                        governance_lines
                    ), f"{owner} suspends for governance before callback reservation"
                effect_lines = [
                    node.lineno
                    for node in ast.walk(handler)
                    if isinstance(node, ast.Call)
                    and (
                        (isinstance(node.func, ast.Name) and node.func.id == "parse_command")
                        or (
                            isinstance(node.func, ast.Attribute)
                            and node.func.attr == "_handle_admitted"
                        )
                    )
                ]
                if effect_lines:
                    assert min(admit_lines) < min(
                        effect_lines
                    ), f"{owner} reserves only after command handling"

        for owner in ("telegram", "discord", "teams"):
            source = dispatcher_paths[owner].read_text(encoding="utf-8")
            assert "refused_resume_is_restricted" in source
            assert "_refused_turn_restricted" in source
        teams_source = dispatcher_paths["teams"].read_text(encoding="utf-8")
        assert "inbound_restricted=session_restricted" in teams_source

    def test_upstream_inline_receivers_register_before_dispatch(self):
        root = Path(__file__).resolve().parents[1] / "src" / "kiro_crew"
        contracts = (
            (root / "whatsapp" / "client.py", "_on_message", "on_message"),
            (root / "weixin" / "transport.py", "_poll_forever", "receive"),
        )
        for path, function_name, dispatch_name in contracts:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            function = next(
                node
                for node in ast.walk(tree)
                if isinstance(node, ast.AsyncFunctionDef) and node.name == function_name
            )
            registrations = [
                node.lineno
                for node in ast.walk(function)
                if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "add"
                and "_handler_tasks" in ast.unparse(node.func.value)
            ]
            dispatches = [
                node.lineno
                for node in ast.walk(function)
                if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == dispatch_name
            ]
            assert registrations and dispatches
            assert min(registrations) < min(
                dispatches
            ), f"{path.parent.name} receives a callback before making it census-visible"

        assert "_handler_tasks" in (root / "whatsapp" / "client.py").read_text(encoding="utf-8")
        assert "_handler_tasks" in (root / "weixin" / "client.py").read_text(encoding="utf-8")

    def test_slack_socket_reserves_before_ack(self):
        path = Path(__file__).resolve().parents[1] / "src" / "kiro_crew" / "slack" / "events.py"
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        listener = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "_on_event"
        )
        admit = [
            node.lineno
            for node in ast.walk(listener)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "admit_inbound_callback"
        ]
        ack = [
            node.lineno
            for node in ast.walk(listener)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "send_socket_mode_response"
        ]
        assert admit and ack and min(admit) < min(ack)

    def test_every_auto_apply_path_uses_the_final_restart_fence(self):
        path = Path(__file__).resolve().parents[1] / "src" / "kiro_crew" / "slack" / "gateway.py"
        sources = (path, *sorted((path.parent / "gateway_runtime").glob("*.py")))
        text = "\n".join(source.read_text(encoding="utf-8") for source in sources)
        tree = ast.parse(text, filename=str(path))
        methods = {
            node.name: node
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }

        def attrs_of(name: str) -> set[str]:
            return {
                node.func.attr
                for node in ast.walk(methods[name])
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            }

        # The managed-venv apply builds with admission open and reaches the fence
        # through the restart bracket, which pauses admission first.
        assert "_retry_pending_update_restart" in attrs_of("_auto_apply_wheel_update")
        for name in ("_auto_apply_update", "_retry_pending_update_restart"):
            assert "_restart_after_update" in attrs_of(name)
        for name in ("_auto_apply_update", "_auto_apply_wheel_update"):
            assert "reexec_python_module" not in attrs_of(name)

        prepare_attrs = {
            node.func.attr
            for node in ast.walk(methods["_prepare_auto_update_apply"])
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        }
        assert "drain_active_turns" not in prepare_attrs

        restart = methods["_restart_after_update_claimed"]
        calls: dict[str, list[int]] = {}
        for node in ast.walk(restart):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                calls.setdefault(node.func.attr, []).append(node.lineno)
        drains = sorted(calls["_drain_update_callback_work"])
        assert len(drains) == 2
        assert drains[0] < calls["fence_update_restart"][0]
        assert calls["fence_update_restart"][0] < calls["close_all"][0] < drains[1]
        assert drains[1] < calls["reexec_python_module"][0]
