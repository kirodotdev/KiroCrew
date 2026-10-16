"""Kiro CLI prerequisite tests: the required agent specs.

A missing or rejected spec narrows readiness. The repair is a POST the status GET never
reaches; it writes the missing specs, never over a present one, and reports a failed or
silent write.
"""

from __future__ import annotations

import asyncio
import json
import threading
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from aiohttp import web
from kiro_prerequisite_helpers import _agents_dir_never_the_real_home  # noqa: F401
from kiro_prerequisite_helpers import _isolate_agent_specs  # noqa: F401
from kiro_prerequisite_helpers import _make_executable, _no_audit

from kiro_crew.agent_files import AGENT_FILENAME
from kiro_crew.kiro_prerequisite import KiroPrerequisiteService, PrerequisiteStatus, ProcessResult
from kiro_crew.testing.wait import async_wait_until, await_until


def _parked_on(lock: asyncio.Lock) -> bool:
    """Whether a task is parked waiting for ``lock`` (a woken waiter does not count)."""
    return any(not waiter.done() for waiter in (getattr(lock, "_waiters", None) or ()))


class TestAgentSpecsNarrowReadiness:
    """A viable binary + a good ``whoami`` are NOT sufficient for readiness.

    Kiro Crew's own agent specs (``~/.kiro/agents/kirocrew*.json``) were not an
    input to ``ready`` at all, so an install whose spec write failed reported
    ready while kiro-cli answered every ``session/set_mode`` with
    ``Mode '<name>' not found`` — the gate affirmatively told the user setup was
    complete on an install that could not run one turn. That is the customer
    report these tests pin.

    The status is set directly rather than probed: the probe's two subprocesses
    are irrelevant here (both of its inputs are already true in the scenario),
    and ``snapshot()`` without ``force`` reads the latch, so the overlay is what
    is under test.
    """

    @staticmethod
    def _service(tmp_path: Path) -> KiroPrerequisiteService:
        service = KiroPrerequisiteService(
            platform_name="linux",
            environ={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"},
            home=tmp_path,
            audit_writer=_no_audit,
        )
        service._status = PrerequisiteStatus(
            platform="Linux",
            installed=True,
            authenticated=True,
            ready=True,
            initial_setup_complete=True,
        )
        service._has_probed = True
        return service

    @staticmethod
    def _agents_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, specs: bool) -> Path:
        """Point the agents dir at a tmp path, optionally fully populated."""
        from kiro_crew import agent as agent_module
        from kiro_crew.agent_files import REQUIRED_KIRO_AGENT_FILES

        agents = tmp_path / "agents"
        agents.mkdir(parents=True, exist_ok=True)
        if specs:
            for name in REQUIRED_KIRO_AGENT_FILES:
                (agents / name).write_text('{"name": "x"}', encoding="utf-8")
        monkeypatch.setattr(agent_module, "KIRO_AGENTS_DIR", agents)
        return agents

    @pytest.mark.asyncio
    async def test_missing_specs_make_a_ready_install_not_ready(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from kiro_crew.agent_files import REQUIRED_KIRO_AGENT_FILES

        self._agents_dir(tmp_path, monkeypatch, specs=False)

        status = await self._service(tmp_path).snapshot()

        assert status["missing_agent_specs"] == list(REQUIRED_KIRO_AGENT_FILES)
        assert status["ready"] is False
        assert status["repair_required"] is True
        # Untouched: the binary IS installed and the user IS signed in. Reporting
        # either as false would send them to re-install or re-login for a
        # condition neither one causes.
        assert status["installed"] is True
        assert status["authenticated"] is True

    @pytest.mark.asyncio
    async def test_present_specs_leave_readiness_alone(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        self._agents_dir(tmp_path, monkeypatch, specs=True)

        status = await self._service(tmp_path).snapshot()

        assert status["missing_agent_specs"] == []
        assert status["ready"] is True
        assert status["repair_required"] is False

    @pytest.mark.asyncio
    async def test_overlay_only_narrows_never_grants(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        self._agents_dir(tmp_path, monkeypatch, specs=True)
        service = self._service(tmp_path)
        service._status = replace(service._status, authenticated=False, ready=False)

        status = await service.snapshot()

        # Specs on disk say nothing about auth — present specs must not promote a
        # signed-out install to ready. The second assertion is what makes this
        # revert-verified: only the overlay populates that key, so a reverted
        # feature fails here instead of passing on the constructor's own False.
        assert status["ready"] is False
        assert status["missing_agent_specs"] == []

    @pytest.mark.asyncio
    async def test_unreadable_agents_dir_does_not_block_a_working_install(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A failed CHECK is not evidence of a missing spec."""
        from kiro_crew import agent as agent_module

        def _boom() -> list[str]:
            raise OSError("permission denied")

        monkeypatch.setattr(agent_module, "missing_required_agent_specs", _boom)

        status = await self._service(tmp_path).snapshot()

        assert status["ready"] is True
        assert status["missing_agent_specs"] == []

    @pytest.mark.asyncio
    async def test_an_instance_that_may_not_write_the_specs_reports_none(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A pod or worktree gateway must never be gated on specs it cannot write.

        ``_decline_shared_agent_home`` refuses the write for an ephemeral instance
        by design, and the overlay reads the AMBIENT machine-wide agents dir. On a
        host whose ``~/.kiro/agents`` is empty that combination would report
        ``ready=False`` permanently, put the whole dashboard behind the
        missing-spec screen, and offer a repair that declines every time -- turning
        "chat is broken" into "the product is unreachable" for exactly the
        instances an operator uses to diagnose.
        """
        from kiro_crew import agent as agent_module

        self._agents_dir(tmp_path, monkeypatch, specs=False)
        monkeypatch.setattr(
            agent_module,
            "_decline_shared_agent_home",
            lambda *, audit=True: tmp_path / "agents" / AGENT_FILENAME,
        )

        status = await self._service(tmp_path).snapshot()

        assert status["missing_agent_specs"] == []
        assert status["ready"] is True
        assert status["repair_required"] is False

    def test_the_decline_check_is_audit_free_when_read(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The status read must not write SEL records.

        ``_decline_shared_agent_home`` audits both its grant and its refusal
        because they are permission decisions about a WRITE. A polled status read
        is not one, and emitting a record every 30 seconds per client would bury
        the real decisions.
        """
        from kiro_crew import agent as agent_module

        events: list[str] = []

        class _Recorder:
            def log_api_access(self, **kwargs: Any) -> None:
                events.append(str(kwargs.get("outcome")))

        monkeypatch.setattr(agent_module, "sel", lambda: _Recorder())
        agent_module._decline_shared_agent_home(audit=False)

        assert events == []

    @pytest.mark.asyncio
    async def test_assume_ready_bypasses_the_overlay(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``assume_ready`` is the deliberate host-reality bypass.

        Test-mode gateways, fixtures and the offline E2E suite run on homes with
        no managed specs; applying the overlay there would put every one of them
        behind a repair gate.
        """
        self._agents_dir(tmp_path, monkeypatch, specs=False)
        service = KiroPrerequisiteService(
            platform_name="linux",
            environ={"HOME": str(tmp_path), "PATH": ""},
            home=tmp_path,
            audit_writer=_no_audit,
            assume_ready=True,
        )

        status = await service.snapshot()

        assert status["ready"] is True
        assert status["missing_agent_specs"] == []

    @pytest.mark.asyncio
    async def test_session_ready_is_not_narrowed(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The overlay is dashboard-facing only, by design.

        ``session_ready()`` gates poll-driven spawn sites, and turn-starting paths
        deliberately do not block on it — they let the turn carry the failure,
        which now arrives as an actionable missing-spec message. Narrowing it here
        would re-introduce the stale-latch lockout that design removed.
        """
        self._agents_dir(tmp_path, monkeypatch, specs=False)
        service = self._service(tmp_path)

        assert (await service.snapshot())["ready"] is False
        assert await service.session_ready() is True


class TestRejectedAgentSpecsNarrowReadiness:
    """A spec that is PRESENT can still be one kiro-cli refuses to load.

    ``missing_agent_specs`` answers presence by statting the file, which cannot
    see this: kiro-cli drops a spec it rejects from its agent table, so
    ``--agent kirocrew`` resolves to the default agent with none of Kiro Crew's
    MCP servers and only a line on stderr. That is the shape of the customer
    report — "my migrated agents stopped working" with a
    perfectly present file on disk.

    The oracle is the binary, not a schema copied into this repo, so a future
    kiro-cli that changes the spec format is REPORTED rather than guessed at.

    Two properties are load-bearing and are pinned in both directions:

    * ``kiro-cli agent validate`` exits 0 whether or not it accepted the file, so
      the verdict lives only in its output. A test that asserted on the exit code
      would pass while detecting nothing.
    * output that is NOT a schema rejection (a sandbox denial, a vanished file)
      must NOT be reported as a rejected spec, or the gate sends the user to
      rewrite a spec that was fine.
    """

    @pytest.fixture(autouse=True)
    # nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- no test in the test_kiro_prerequisite*.py modules calls monkeypatch.undo()
    def _isolate_agents_dir(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Keep the ownership guard in ``agent.py`` from declining under a worktree.

        These tests supply their specs through ``spec_lister`` (see ``_service``),
        so all this needs to do is keep the env from pointing at the real home.
        """
        monkeypatch.setenv("KIRO_HOME", str(tmp_path / ".kiro"))

    @staticmethod
    def _spec_dir(tmp_path: Path) -> Path:
        agents = tmp_path / ".kiro" / "agents"
        agents.mkdir(parents=True, exist_ok=True)
        return agents

    @staticmethod
    def _service(
        tmp_path: Path,
        runner: Any,
    ) -> KiroPrerequisiteService:
        # Staged inside tmp_path so binary discovery and sign-in eligibility do
        # not depend on the host: without these, a machine with a real kiro-cli
        # reaches the probe while CI finds nothing viable and never gets far
        # enough to ask about specs.
        _make_executable(tmp_path / ".local" / "bin" / "kiro-cli")
        token = tmp_path / ".aws" / "sso" / "cache" / "kiro-auth-token-cli.json"
        token.parent.mkdir(parents=True, exist_ok=True)
        token.write_text('{"accessToken":"secret"}', encoding="utf-8")
        agents = tmp_path / ".kiro" / "agents"

        def _lister() -> list[tuple[str, Path]]:
            from kiro_crew.agent_files import REQUIRED_KIRO_AGENT_FILES

            # Mirrors present_required_agent_specs (required-and-present only) over
            # this test's dir, so the probe's spawn count is stated here rather than
            # inherited from the host's real agents dir.
            return [
                (name, agents / name)
                for name in REQUIRED_KIRO_AGENT_FILES
                if (agents / name).is_file()
            ]

        return KiroPrerequisiteService(
            platform_name="linux",
            environ={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"},
            home=tmp_path,
            process_runner=runner,
            audit_writer=_no_audit,
            spec_lister=_lister,
        )

    @pytest.mark.asyncio
    async def test_a_rejected_required_spec_blocks_readiness_and_names_the_reason(
        self,
        tmp_path: Path,
    ) -> None:
        agents = self._spec_dir(tmp_path)
        (agents / "kirocrew.json").write_text("{}", encoding="utf-8")
        reason = (
            f"Error: Json supplied at {agents / 'kirocrew.json'} is invalid: "
            "data did not match any variant of untagged enum Repr"
        )

        async def run(_command: str, args: list[str], **_kwargs: Any) -> ProcessResult:
            if args[:2] == ["agent", "validate"]:
                # Exit 0 WITH an error message is exactly what kiro-cli does.
                return ProcessResult(ok=True, output=reason, returncode=0)
            return ProcessResult(ok=True)

        status = await self._service(tmp_path, run).snapshot(force=True)

        assert status["rejected_agent_specs"] == ["kirocrew.json"]
        assert status["ready"] is False
        assert status["repair_required"] is True
        assert "is invalid" in status["agent_spec_rejection_detail"]

    @pytest.mark.asyncio
    async def test_an_accepted_spec_leaves_readiness_alone(
        self,
        tmp_path: Path,
    ) -> None:
        agents = self._spec_dir(tmp_path)
        (agents / "kirocrew.json").write_text("{}", encoding="utf-8")

        async def run(_command: str, args: list[str], **_kwargs: Any) -> ProcessResult:
            if args[:2] == ["agent", "validate"]:
                # Acceptance is SILENT — no output at all.
                return ProcessResult(ok=True, output="", returncode=0)
            return ProcessResult(ok=True)

        status = await self._service(tmp_path, run).snapshot(force=True)

        assert status["rejected_agent_specs"] == []
        assert status["agent_spec_rejection_detail"] == ""
        assert status["ready"] is True
        assert status["repair_required"] is False

    @pytest.mark.asyncio
    async def test_output_that_is_not_a_schema_rejection_is_not_reported(
        self,
        tmp_path: Path,
    ) -> None:
        """A probe that could not READ the spec must not indict the spec.

        Without this, a sandbox denial or a spec deleted between the stat and the
        spawn would put the install behind a repair card whose only remedy is to
        rewrite a file that was never malformed.
        """
        agents = self._spec_dir(tmp_path)
        (agents / "kirocrew.json").write_text("{}", encoding="utf-8")

        async def run(_command: str, args: list[str], **_kwargs: Any) -> ProcessResult:
            if args[:2] == ["agent", "validate"]:
                return ProcessResult(
                    ok=False,
                    output="Error: Permission denied (os error 13)",
                    returncode=1,
                )
            return ProcessResult(ok=True)

        status = await self._service(tmp_path, run).snapshot(force=True)

        assert status["rejected_agent_specs"] == []
        assert status["ready"] is True

    @pytest.mark.asyncio
    async def test_an_absent_spec_costs_no_spawn(
        self,
        tmp_path: Path,
    ) -> None:
        """Absence is ``missing_agent_specs``' job, so it is not probed here.

        Also bounds the added cost: the probe budget grows only by the specs that
        actually exist, and a home with none pays nothing.
        """
        calls: list[list[str]] = []

        async def run(_command: str, args: list[str], **_kwargs: Any) -> ProcessResult:
            calls.append(args)
            return ProcessResult(ok=True)

        status = await self._service(tmp_path, run).snapshot(force=True)

        assert not any(a[:2] == ["agent", "validate"] for a in calls)
        assert status["rejected_agent_specs"] == []

    @pytest.mark.asyncio
    async def test_a_signed_out_cli_is_not_asked_about_specs(
        self,
        tmp_path: Path,
    ) -> None:
        """A signed-out CLI is never asked to validate: it refuses to run.

        ``kiro-cli agent validate`` answers "You are not logged in" while signed
        out, so spawning it spends a spawn per spec on no answer. A spec that
        loads (``{}``) is not reported either: the local KAS check that runs in
        its place accepts it. Pins the gate so a later refactor cannot quietly
        reintroduce those spawns.
        """
        spec = self._spec_dir(tmp_path) / "kirocrew.json"
        spec.write_text("{}", encoding="utf-8")
        calls: list[list[str]] = []

        async def run(_command: str, args: list[str], **_kwargs: Any) -> ProcessResult:
            calls.append(args)
            # Version succeeds so a binary is found; whoami fails -> signed out.
            if args[:1] == ["whoami"] or "whoami" in args:
                return ProcessResult(ok=False, output="not logged in")
            return ProcessResult(ok=True, output="1.0.0")

        status = await self._service(tmp_path, run).snapshot(force=True)

        assert not status["authenticated"]
        assert not any(a[:2] == ["agent", "validate"] for a in calls)
        assert status["rejected_agent_specs"] == []

    @staticmethod
    async def _signed_out_run(_command: str, args: list[str], **_kwargs: Any) -> ProcessResult:
        if args == ["whoami"]:
            return ProcessResult(ok=False, output="Not logged in")
        if args[:2] == ["agent", "validate"]:
            # What kiro-cli 2.21.1 answers signed out, for any spec.
            return ProcessResult(
                ok=False,
                output="error: You are not logged in, please log in with kiro-cli login",
                returncode=1,
            )
        return ProcessResult(ok=True, output="1.0.0")

    @staticmethod
    def _allow_kas_setup(monkeypatch: pytest.MonkeyPatch) -> None:
        from kiro_crew import acp_backends, sandbox
        from kiro_crew.agent_sdk import backend_install

        monkeypatch.setattr(sandbox, "detect_backend", lambda: "bwrap")
        monkeypatch.setattr(sandbox, "configured_sandbox_mode", lambda: "standard")
        monkeypatch.setattr(sandbox, "credential_mask_applies", lambda _mode: True)
        monkeypatch.setattr(acp_backends, "selectable_backend_values", lambda: {"kas"})
        monkeypatch.setattr(
            backend_install,
            "probe_backend",
            lambda backend: backend_install.BackendInstallState(
                backend, backend, backend_install.INSTALLED
            ),
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "body,reason",
        [
            ('{"name":', "is not a valid spec"),
            ("[]", "is not an object"),
            ('{"prompt": 7}', "prompt must be a string"),
        ],
        ids=["unparsable", "not-an-object", "non-string-prompt"],
    )
    async def test_a_spec_kas_cannot_load_is_rejected_while_signed_out(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        body: str,
        reason: str,
    ) -> None:
        """Signed out is KAS's normal state, so KAS's own loader judges the spec.

        The CLI cannot validate while signed out; without the local check a spec
        KAS session creation refuses to load was reported as fine, and KAS
        finished first-run setup onto a session that could not start.
        """
        (self._spec_dir(tmp_path) / "kirocrew.json").write_text(body, encoding="utf-8")
        self._allow_kas_setup(monkeypatch)
        service = self._service(tmp_path, self._signed_out_run)

        status = await service.snapshot(force=True)

        assert status["authenticated"] is False
        assert status["rejected_agent_specs"] == ["kirocrew.json"]
        assert status["repair_required"] is True
        assert reason in status["agent_spec_rejection_detail"]
        assert await service.record_independent_backend_setup("kas") is False
        assert service.initial_setup_complete is False
        assert not service._setup_marker.exists()

    @pytest.mark.asyncio
    async def test_a_spec_kas_loads_is_accepted_while_signed_out(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        (self._spec_dir(tmp_path) / "kirocrew.json").write_text(
            json.dumps({"name": "kirocrew", "prompt": "You are Kiro."}), encoding="utf-8"
        )
        self._allow_kas_setup(monkeypatch)
        service = self._service(tmp_path, self._signed_out_run)

        status = await service.snapshot(force=True)

        assert status["rejected_agent_specs"] == []
        assert status["repair_required"] is False
        assert await service.record_independent_backend_setup("kas") is True
        assert service._setup_marker.exists()

    @pytest.mark.asyncio
    async def test_kas_setup_rechecks_a_spec_broken_after_the_probe(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        spec = self._spec_dir(tmp_path) / "kirocrew.json"
        spec.write_text(json.dumps({"name": "kirocrew"}), encoding="utf-8")
        self._allow_kas_setup(monkeypatch)
        service = self._service(tmp_path, self._signed_out_run)
        assert (await service.snapshot(force=True))["rejected_agent_specs"] == []

        spec.write_text('{"name":', encoding="utf-8")

        assert await service.record_independent_backend_setup("kas") is False
        assert not service._setup_marker.exists()

    @pytest.mark.asyncio
    async def test_a_signed_in_cli_keeps_cli_validation_as_the_only_judge(
        self,
        tmp_path: Path,
    ) -> None:
        """Signed in, the CLI's verdict stands and the local KAS check is not run."""
        (self._spec_dir(tmp_path) / "kirocrew.json").write_text(
            json.dumps({"prompt": 7}), encoding="utf-8"
        )
        calls: list[list[str]] = []

        async def run(_command: str, args: list[str], **_kwargs: Any) -> ProcessResult:
            calls.append(args)
            return ProcessResult(ok=True, output="", returncode=0)

        status = await self._service(tmp_path, run).snapshot(force=True)

        assert status["authenticated"] is True
        assert any(a[:2] == ["agent", "validate"] for a in calls)
        assert status["rejected_agent_specs"] == []
        assert status["repair_required"] is False

    @pytest.mark.asyncio
    async def test_an_mcp_server_with_no_command_is_reported_without_a_spawn(
        self,
        tmp_path: Path,
    ) -> None:
        """``agent validate`` accepts this; the loader cannot start the server.

        Probed against the shipped binary: a spec whose ``mcpServers`` entry omits
        ``command`` produces no output and exit 0 from ``agent validate``, so the
        binary alone would call it accepted while the session runs without the
        tools the spec declares. Structural, so it must cost no subprocess.
        """
        agents = self._spec_dir(tmp_path)
        (agents / "kirocrew.json").write_text(
            json.dumps({"name": "kirocrew", "mcpServers": {"broken": {"args": ["x"]}}}),
            encoding="utf-8",
        )
        calls: list[list[str]] = []

        async def run(_command: str, args: list[str], **_kwargs: Any) -> ProcessResult:
            calls.append(args)
            # Mimics the real binary on this input: silent acceptance.
            return ProcessResult(ok=True, output="", returncode=0)

        status = await self._service(tmp_path, run).snapshot(force=True)

        assert status["rejected_agent_specs"] == ["kirocrew.json"]
        assert "broken" in status["agent_spec_rejection_detail"]
        assert "neither a command" in status["agent_spec_rejection_detail"]
        assert not any(a[:2] == ["agent", "validate"] for a in calls)

    @pytest.mark.asyncio
    async def test_a_spec_the_bounded_reader_rejects_fails_open(
        self,
        tmp_path: Path,
    ) -> None:
        """An unreadable spec is a different fault with its own reporting.

        The structural read goes through the module's bounded helper so a
        pathologically large spec cannot OOM the gateway through the readiness
        probe. That helper also refuses a symlink, and when it refuses, the
        structural check must decline to judge rather than invent a rejection —
        the binary still gets its say.
        """
        agents = self._spec_dir(tmp_path)
        real = tmp_path / "elsewhere.json"
        real.write_text(json.dumps({"mcpServers": {"broken": {"args": []}}}), encoding="utf-8")
        (agents / "kirocrew.json").symlink_to(real)
        calls: list[list[str]] = []

        async def run(_command: str, args: list[str], **_kwargs: Any) -> ProcessResult:
            calls.append(args)
            return ProcessResult(ok=True, output="", returncode=0)

        status = await self._service(tmp_path, run).snapshot(force=True)

        # No structural verdict, and the binary was still consulted.
        assert status["rejected_agent_specs"] == []
        assert any(a[:2] == ["agent", "validate"] for a in calls)

    @pytest.mark.asyncio
    async def test_a_structural_rejection_detail_is_redacted(
        self,
        tmp_path: Path,
    ) -> None:
        """The detail names the MCP server, and a server name is user-supplied.

        Every dashboard-facing string in this module goes through
        ``_sanitize_detail``; the structural finding must not be the one exception,
        or a name carrying a credential reaches the readiness payload verbatim.
        """
        agents = self._spec_dir(tmp_path)
        (agents / "kirocrew.json").write_text(
            json.dumps(
                {
                    "name": "kirocrew",
                    "mcpServers": {
                        "svc?token=AKIAIOSFODNN7EXAMPLE": {"args": []},
                    },
                }
            ),
            encoding="utf-8",
        )

        async def run(_command: str, args: list[str], **_kwargs: Any) -> ProcessResult:
            del args
            return ProcessResult(ok=True, output="", returncode=0)

        status = await self._service(tmp_path, run).snapshot(force=True)

        assert status["rejected_agent_specs"] == ["kirocrew.json"]
        assert "AKIAIOSFODNN7EXAMPLE" not in status["agent_spec_rejection_detail"]

    @pytest.mark.asyncio
    async def test_a_remote_url_mcp_server_is_launchable_and_not_reported(
        self,
        tmp_path: Path,
    ) -> None:
        """A remote MCP server has no ``command`` and is perfectly valid.

        The custom-MCP handler's own contract is "spec needs 'command' (stdio) or
        'url' (remote)", and ``rebuild_agent_config`` writes a URL-only entry into
        the required spec. Treating that as unlaunchable would force a healthy
        install into a readiness gate whose only escape discards the user's MCP tool
        selections — worse than the silent gap this check closes.
        """
        agents = self._spec_dir(tmp_path)
        (agents / "kirocrew.json").write_text(
            json.dumps(
                {
                    "name": "kirocrew",
                    "mcpServers": {"remote": {"url": "https://mcp.example.com/sse"}},
                }
            ),
            encoding="utf-8",
        )

        async def run(_command: str, args: list[str], **_kwargs: Any) -> ProcessResult:
            del args
            return ProcessResult(ok=True, output="", returncode=0)

        status = await self._service(tmp_path, run).snapshot(force=True)

        assert status["rejected_agent_specs"] == []
        assert status["ready"]

    @pytest.mark.asyncio
    async def test_a_registry_pointer_is_launchable_and_not_reported(
        self,
        tmp_path: Path,
    ) -> None:
        """A bare ``"type": "registry"`` pointer is resolved by kiro-cli.

        The spec rebuild carries kiro-cli's own catalog pointers into the required
        spec on purpose. kiro-cli supplies the transport from the admin's catalog,
        so reporting the pointer would put every governed host behind this gate.
        An entry with no transport and no marker is still reported (see the
        no-command test above).
        """
        agents = self._spec_dir(tmp_path)
        (agents / "kirocrew.json").write_text(
            json.dumps({"name": "kirocrew", "mcpServers": {"aws-iac-mcp": {"type": "registry"}}}),
            encoding="utf-8",
        )

        async def run(_command: str, args: list[str], **_kwargs: Any) -> ProcessResult:
            del args
            return ProcessResult(ok=True, output="", returncode=0)

        status = await self._service(tmp_path, run).snapshot(force=True)

        assert status["rejected_agent_specs"] == []
        assert status["ready"]

    @pytest.mark.asyncio
    async def test_a_launchable_mcp_server_still_reaches_the_binary(
        self,
        tmp_path: Path,
    ) -> None:
        """The structural check must not become the only oracle.

        It answers one narrow question. Everything else — including whether this
        build of kiro-cli likes the schema at all — stays the binary's call, so a
        spec that passes the structural read must still be validated.
        """
        agents = self._spec_dir(tmp_path)
        (agents / "kirocrew.json").write_text(
            json.dumps({"name": "kirocrew", "mcpServers": {"ok": {"command": "uvx", "args": []}}}),
            encoding="utf-8",
        )
        calls: list[list[str]] = []

        async def run(_command: str, args: list[str], **_kwargs: Any) -> ProcessResult:
            calls.append(args)
            return ProcessResult(ok=True, output="", returncode=0)

        status = await self._service(tmp_path, run).snapshot(force=True)

        assert status["rejected_agent_specs"] == []
        assert any(a[:2] == ["agent", "validate"] for a in calls)

    @pytest.mark.asyncio
    async def test_a_rejected_spec_is_re_probed_but_never_rewritten(
        self,
        tmp_path: Path,
    ) -> None:
        """The rejection card's button re-asks the binary; it must not rewrite.

        ``_repair_agent_specs`` documents that it only rebuilds when the main spec
        is ABSENT, which is what keeps it out of a lost-update class: the rebuild
        regenerates the whole file and re-merges ``mcpServers``, but not the
        ``tools``/``allowedTools`` half ``api_mcp_toggle`` writes separately. A
        rejected spec is present, so rebuilding it can silently drop a concurrent
        toggle's grant. Pins both halves: no write, and a forced re-probe so the
        button still does something only the binary can answer.
        """
        agents = self._spec_dir(tmp_path)
        (agents / "kirocrew.json").write_text("{}", encoding="utf-8")
        # The lite spec is PRESENT so this stays a pure rejection state; the
        # mixed rejected-main/missing-lite state has its own repair test in
        # TestAgentSpecRepair.
        (agents / "kirocrew-lite.json").write_text("{}", encoding="utf-8")
        rebuilt: list[str] = []
        calls: list[list[str]] = []

        async def run(_command: str, args: list[str], **_kwargs: Any) -> ProcessResult:
            calls.append(args)
            if args[:2] == ["agent", "validate"]:
                # Reject ONLY the main spec: the lite spec is staged present and
                # accepted, keeping this a pure single-spec rejection state.
                if Path(args[-1]).name == "kirocrew.json":
                    return ProcessResult(ok=True, output="x is invalid: bad", returncode=0)
            return ProcessResult(ok=True)

        service = self._service(tmp_path, run)
        await service.snapshot(force=True)
        assert service._status.rejected_agent_specs == ["kirocrew.json"]

        async def _fake_repair() -> str:
            rebuilt.append("called")
            return ""

        # Patch the write itself: this asserts the guard's decision, not that
        # rebuild_agent_config works (which has its own coverage).
        service._repair_agent_specs = _fake_repair  # type: ignore[method-assign]

        await service.repair_agent_specs(caller="test")

        # A rejected spec exists on disk, so regenerating it would drop a
        # concurrent api_mcp_toggle edit to the tools/allowedTools half that
        # rebuild_agent_config does not re-merge. Losing a user's tool grants to
        # clear a rejection is worse than the rejection, so the write must NOT
        # happen -- the button re-asks the binary instead.
        assert rebuilt == []
        # The button is not inert: a forced re-probe means a spec kiro-cli now
        # accepts clears the card, which a stat-only overlay could never decide.
        assert sum(1 for a in calls if a[:2] == ["agent", "validate"]) >= 2

    @pytest.mark.asyncio
    async def test_repair_declines_when_nothing_is_wrong(
        self,
        tmp_path: Path,
    ) -> None:
        """The widened guard must not turn every Check again into a write."""
        agents = self._spec_dir(tmp_path)
        (agents / "kirocrew.json").write_text("{}", encoding="utf-8")
        (agents / "kirocrew-lite.json").write_text("{}", encoding="utf-8")
        rebuilt: list[str] = []

        async def run(_command: str, args: list[str], **_kwargs: Any) -> ProcessResult:
            return ProcessResult(ok=True)

        service = self._service(tmp_path, run)
        await service.snapshot(force=True)

        async def _fake_repair() -> str:
            rebuilt.append("called")
            return ""

        service._repair_agent_specs = _fake_repair  # type: ignore[method-assign]

        await service.repair_agent_specs(caller="test")

        assert rebuilt == []


class TestAgentSpecRepairIsAPostNotAGet:
    """The repair must never be reachable from the status GET.

    ``/api/kiro-prerequisite`` is an ``add_get``, and BOTH dashboard barriers are
    method-scoped: ``csrf_middleware`` skips ``check_origin`` for
    ``{GET, HEAD, OPTIONS}`` and ``sel_audit_middleware`` logs only
    ``{POST, PUT, DELETE, PATCH}``. A spec rewrite hung off that GET would be
    cross-site triggerable (a ``SameSite=Lax`` cookie rides a top-level cross-site
    GET) and would leave no SEL record, so it lives on its own POST route.
    """

    @staticmethod
    def _service(tmp_path: Path) -> KiroPrerequisiteService:
        service = KiroPrerequisiteService(
            platform_name="linux",
            environ={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"},
            home=tmp_path,
            audit_writer=_no_audit,
        )
        service._status = PrerequisiteStatus(
            platform="Linux",
            installed=True,
            authenticated=True,
            ready=True,
            initial_setup_complete=True,
        )
        service._has_probed = True

        async def _no_probe(*, force: bool = False) -> PrerequisiteStatus:
            del force
            return service._status

        service._probe = _no_probe  # type: ignore[method-assign]
        return service

    @staticmethod
    def _agents_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        from kiro_crew import agent as agent_module

        agents = tmp_path / "agents"
        agents.mkdir(exist_ok=True)
        monkeypatch.setattr(agent_module, "KIRO_AGENTS_DIR", agents)
        return agents

    @pytest.mark.asyncio
    async def test_forced_status_read_never_writes(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Even ``?refresh=1`` is a pure read — this is the regression guard."""
        from kiro_crew import agent as agent_module

        self._agents_dir(tmp_path, monkeypatch)
        calls: list[int] = []
        monkeypatch.setattr(agent_module, "rebuild_agent_config", lambda: calls.append(1))

        status = await self._service(tmp_path).snapshot(force=True)

        assert calls == []
        # Still fully REPORTED — the read half of the feature is unaffected.
        assert status["missing_agent_specs"] != []
        assert status["ready"] is False

    def test_repair_route_accepts_only_POST(self) -> None:
        """Asserted over the ROUTE TABLE, not the module text.

        A source-text match proves nothing about what was registered and breaks on
        any reflow. What matters is the method the router will accept: GET is the
        one method csrf_middleware and sel_audit_middleware both skip.
        """
        from kiro_crew.dashboard.handlers.kiro_prerequisite import (
            api_kiro_prerequisite_repair_specs,
        )

        app = web.Application()
        app.router.add_post(
            "/api/kiro-prerequisite/repair-specs",
            api_kiro_prerequisite_repair_specs,
        )
        methods = {
            route.method
            for route in app.router.routes()
            if getattr(route.resource, "canonical", "") == "/api/kiro-prerequisite/repair-specs"
        }

        assert methods == {"POST"}, methods


class TestAgentSpecRepair:
    """``repair_agent_specs`` — the POST handler's service call."""

    _service = staticmethod(TestAgentSpecRepairIsAPostNotAGet._service)
    _agents_dir = staticmethod(TestAgentSpecRepairIsAPostNotAGet._agents_dir)

    @pytest.mark.asyncio
    async def test_writes_the_specs_and_returns_ready(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from kiro_crew import agent as agent_module
        from kiro_crew.agent_files import REQUIRED_KIRO_AGENT_FILES

        agents = self._agents_dir(tmp_path, monkeypatch)
        calls: list[int] = []

        def _rebuild() -> Path:
            calls.append(1)
            for name in REQUIRED_KIRO_AGENT_FILES:
                (agents / name).write_text('{"name": "x"}', encoding="utf-8")
            return agents / REQUIRED_KIRO_AGENT_FILES[0]

        monkeypatch.setattr(agent_module, "rebuild_agent_config", _rebuild)

        status = await self._service(tmp_path).repair_agent_specs("owner")

        assert calls == [1]
        # The response IS the post-repair snapshot, so one request fixes and
        # reports rather than making the user press the button twice.
        assert status["missing_agent_specs"] == []
        assert status["ready"] is True
        assert status["agent_spec_repair_error"] == ""

    @pytest.mark.asyncio
    async def test_failed_repair_reports_a_sanitized_exception(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The swallowed boot exception is what made the original undiagnosable.

        Sanitized like every other dashboard-facing string here: the rebuild's call
        graph merges ``mcpServers[*].env``, so raw exception text can carry a
        credential.
        """
        from kiro_crew import agent as agent_module

        self._agents_dir(tmp_path, monkeypatch)

        def _rebuild() -> Path:
            raise FileNotFoundError("no shipped defaults.json")

        monkeypatch.setattr(agent_module, "rebuild_agent_config", _rebuild)

        status = await self._service(tmp_path).repair_agent_specs("owner")

        assert "FileNotFoundError: no shipped defaults.json" in (status["agent_spec_repair_error"])
        assert status["ready"] is False

    @pytest.mark.asyncio
    async def test_credential_shaped_exception_text_is_redacted(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from kiro_crew import agent as agent_module

        self._agents_dir(tmp_path, monkeypatch)
        # Assembled at runtime so the literal never sits in the file for the scan.
        secret = "ghp_" + "A" * 36

        def _rebuild() -> Path:
            raise RuntimeError(f"bad env GITHUB_TOKEN={secret}")

        monkeypatch.setattr(agent_module, "rebuild_agent_config", _rebuild)

        status = await self._service(tmp_path).repair_agent_specs("owner")

        assert secret not in status["agent_spec_repair_error"]

    @pytest.mark.asyncio
    async def test_silent_no_op_rebuild_is_reported_as_a_failure(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``rebuild_agent_config`` can decline WITHOUT raising.

        ``_decline_shared_agent_home`` returns early when an ephemeral instance
        would rewrite a shared agent home. Reporting that as success would leave
        the gate showing no error and a button that changes nothing on every press.
        """
        from kiro_crew import agent as agent_module

        self._agents_dir(tmp_path, monkeypatch)
        monkeypatch.setattr(agent_module, "rebuild_agent_config", lambda: None)

        status = await self._service(tmp_path).repair_agent_specs("owner")

        assert "still missing" in status["agent_spec_repair_error"]
        assert "kirocrew setup --agent-only --clean" in status["agent_spec_repair_error"]
        assert status["ready"] is False

    @pytest.mark.asyncio
    async def test_does_not_rebuild_over_a_present_main_spec(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A present kirocrew.json is never regenerated by this route.

        ``rebuild_agent_config`` rewrites the whole file. It re-merges
        ``mcpServers`` under ``bridges._mcp_lock``, but NOT the
        ``tools``/``allowedTools`` half ``api_mcp_toggle`` writes in a separate
        step — so rebuilding over an existing spec can drop a concurrent toggle's
        edit and resurrect a server the user just disabled. Gating on the MAIN
        spec's ABSENCE removes that window: with no file there is no edit to lose.

        The missing LITE spec is still repaired — via its own writer, never via
        the whole-file rebuild. Remove the auxiliary arm from
        ``repair_agent_specs`` and this test fails: the repair would report
        success while the overlay keeps listing the lite spec.
        """
        from kiro_crew import agent as agent_module
        from kiro_crew.agent_files import LITE_AGENT_FILENAME

        agents = self._agents_dir(tmp_path, monkeypatch)
        (agents / AGENT_FILENAME).write_text('{"name": "kirocrew"}', encoding="utf-8")
        calls: list[int] = []
        monkeypatch.setattr(agent_module, "rebuild_agent_config", lambda: calls.append(1))

        def _write_lite() -> None:
            (agents / LITE_AGENT_FILENAME).write_text('{"name": "kirocrew-lite"}', encoding="utf-8")

        monkeypatch.setattr(agent_module, "_install_lite_agent_fallback", _write_lite)

        status = await self._service(tmp_path).repair_agent_specs("owner")

        assert calls == []
        assert (agents / LITE_AGENT_FILENAME).is_file()
        assert status["missing_agent_specs"] == []
        assert status["agent_spec_repair_error"] == ""
        assert status["ready"] is True

    @pytest.mark.asyncio
    async def test_a_rejected_main_spec_does_not_block_the_lite_repair(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Rejection and a missing lite spec can coexist; both get their remedy.

        Acceptance is only evaluated for PRESENT specs, so kirocrew.json can be
        latched REJECTED while kirocrew-lite.json is missing. A rejection branch
        that returns before the auxiliary arm leaves that mixed state with no
        error, no write, and a permanently blocked gate. Writing a MISSING file
        rewrites nothing, so the lost-update reasoning behind the rejection
        guard does not apply to it.
        """
        from kiro_crew import agent as agent_module
        from kiro_crew.agent_files import LITE_AGENT_FILENAME

        agents = self._agents_dir(tmp_path, monkeypatch)
        (agents / AGENT_FILENAME).write_text('{"name": "kirocrew"}', encoding="utf-8")
        service = self._service(tmp_path)
        service._status.rejected_agent_specs = [AGENT_FILENAME]
        calls: list[int] = []
        monkeypatch.setattr(agent_module, "rebuild_agent_config", lambda: calls.append(1))

        def _write_lite() -> None:
            (agents / LITE_AGENT_FILENAME).write_text('{"name": "kirocrew-lite"}', encoding="utf-8")

        monkeypatch.setattr(agent_module, "_install_lite_agent_fallback", _write_lite)

        status = await service.repair_agent_specs("owner")

        assert calls == []  # the rejected main spec is never regenerated
        assert (agents / LITE_AGENT_FILENAME).is_file()
        assert status["missing_agent_specs"] == []
        assert status["agent_spec_repair_error"] == ""

    @pytest.mark.asyncio
    async def test_failed_lite_spec_write_reports_a_sanitized_exception(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The auxiliary arm reports its failure the way the main rebuild does.

        Sanitized for the same reason as the main path: the error string is
        dashboard-facing.
        """
        from kiro_crew import agent as agent_module

        agents = self._agents_dir(tmp_path, monkeypatch)
        (agents / AGENT_FILENAME).write_text('{"name": "kirocrew"}', encoding="utf-8")
        monkeypatch.setattr(agent_module, "rebuild_agent_config", lambda: None)

        def _boom() -> None:
            raise PermissionError("agents dir is read-only")

        monkeypatch.setattr(agent_module, "_install_lite_agent_fallback", _boom)

        status = await self._service(tmp_path).repair_agent_specs("owner")

        assert "PermissionError: agents dir is read-only" in (status["agent_spec_repair_error"])
        assert status["ready"] is False

    @pytest.mark.asyncio
    async def test_silent_no_op_lite_write_is_reported_as_a_failure(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A lite write that declines without raising must not read as success.

        Mirrors ``test_silent_no_op_rebuild_is_reported_as_a_failure`` for the
        auxiliary arm: the post-repair overlay is what turns a silent no-op into
        a visible error instead of a success report, honoring the docstring's
        no-op-is-failure rule.
        """
        from kiro_crew import agent as agent_module

        agents = self._agents_dir(tmp_path, monkeypatch)
        (agents / AGENT_FILENAME).write_text('{"name": "kirocrew"}', encoding="utf-8")
        monkeypatch.setattr(agent_module, "rebuild_agent_config", lambda: None)
        monkeypatch.setattr(agent_module, "_install_lite_agent_fallback", lambda: None)

        status = await self._service(tmp_path).repair_agent_specs("owner")

        assert "still missing" in status["agent_spec_repair_error"]
        assert status["ready"] is False

    @pytest.mark.asyncio
    async def test_concurrent_repairs_rebuild_exactly_once(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Two owner POSTs landing together must produce ONE rebuild.

        ``operation_running`` does not cover repair -- it tracks ``_task``, which
        only install/login set -- so both callers pass that guard. Without the
        repair lock both observe the spec missing and both rebuild, and the second
        regenerates the file the first just wrote: the rebuild-over-an-existing-spec
        case the main-spec gate exists to avoid, which can drop a concurrent
        ``api_mcp_toggle`` write to ``tools``/``allowedTools``.

        The interleaving is FORCED rather than hoped for. An earlier version of
        this test simply gathered two calls and passed even with the lock removed,
        because the event loop happened to run them end to end. Here the stub parks
        inside the rebuild until the second caller has had its chance to reach its
        own check, which is the only ordering that distinguishes the two versions.
        """
        from kiro_crew import agent as agent_module
        from kiro_crew.agent_files import REQUIRED_KIRO_AGENT_FILES

        agents = self._agents_dir(tmp_path, monkeypatch)
        calls: list[int] = []
        entered = threading.Event()
        release = threading.Event()

        def _rebuild() -> None:
            calls.append(1)
            entered.set()
            # Runs on a worker thread (asyncio.to_thread), so parking here does not
            # block the loop -- the second caller keeps running.
            release.wait(timeout=10)
            for name in REQUIRED_KIRO_AGENT_FILES:
                (agents / name).write_text('{"name": "x"}', encoding="utf-8")

        monkeypatch.setattr(agent_module, "rebuild_agent_config", _rebuild)
        service = self._service(tmp_path)

        first = asyncio.ensure_future(service.repair_agent_specs("owner"))
        # Wait until the first caller is INSIDE the rebuild, off-loop so the await
        # does not starve it.
        await async_wait_until(entered.is_set, describe=lambda: f"first.done()={first.done()}")
        second = asyncio.ensure_future(service.repair_agent_specs("owner"))
        try:
            # Wait for the second caller to settle one way or the other: parked on
            # the repair lock, or (unlocked) through its own overlay + check and
            # into a second rebuild.
            await async_wait_until(
                lambda: len(calls) >= 2 or _parked_on(service._repair_lock),
                describe=lambda: f"calls={calls} second.done()={second.done()}",
            )
        finally:
            release.set()
        first_result, second_result = await await_until(
            asyncio.gather(first, second), what="both repairs"
        )

        assert calls == [1], "the second repair must be a no-op, not a second rebuild"
        # Both callers still get a truthful post-repair answer.
        for payload in (first_result, second_result):
            assert payload["missing_agent_specs"] == []
            assert payload["agent_spec_repair_error"] == ""
