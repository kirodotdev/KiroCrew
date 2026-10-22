"""Auto-improvement regressions: credentials stay out of the agent's reach and out of what
the app serves.

The fallback agent and the watchers run confined, inherit no credentials and are
audited tool by tool; a failed registration or a missing confinement refuses the run;
and the activity feed, terminal errors, MCP errors, watcher snapshots, commit messages
and commit errors are redacted, failing closed, before they reach a browser, a log or
git history.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from kiro_crew.apps.builtins.auto_improvement.tests.dogfood_learnings_helpers import (  # noqa: F401
    _cwd_is_the_scratch_dir,
)


class TestSubprocessFallbackIsAudited:
    """The `claude -p` fallback passes `--dangerously-skip-permissions`, so the whole
    subprocess is one unattended blanket approval covering every tool it will use.

    Raised by review of this branch, which read the asymmetry as a sandbox bypass: the
    SESSION path refused an unauditable tool while this path ran a permissionless agent
    with no trail at all. The spawn's containment was already argued in test_spawn_audit
    (fixed argv[0], throwaway worktree of a push-disabled clone, test execution routed
    through the sandbox separately) — what was missing was the audit.
    """

    def test_launch_is_logged_before_the_spawn(self, monkeypatch) -> None:
        import kiro_crew.sel as sel_mod
        from kiro_crew.apps.builtins.auto_improvement.spine import agent_runner as ar

        logged: list[dict] = []

        class _Sel:
            def log_tool_invocation(self, **kw):
                logged.append(kw)

        monkeypatch.setattr(sel_mod, "sel", lambda: _Sel())
        assert ar._audit_unattended_agent(cwd="/w", model="m", max_turns=3) is True
        assert logged and logged[0]["critical"] is True
        assert logged[0]["metadata"]["skip_permissions"] is True

    def test_audit_failure_refuses_to_launch(self, monkeypatch) -> None:
        """No trail, no permissionless agent."""
        import kiro_crew.sel as sel_mod
        from kiro_crew.apps.builtins.auto_improvement.spine import agent_runner as ar

        class _Sel:
            def log_tool_invocation(self, **kw):
                raise OSError("audit sink is unwritable")

        monkeypatch.setattr(sel_mod, "sel", lambda: _Sel())
        assert ar._audit_unattended_agent(cwd="/w", model="m", max_turns=3) is False

    def test_run_returns_an_error_instead_of_spawning_when_unauditable(
        self, monkeypatch, tmp_path
    ) -> None:
        """End-to-end: `run` must not reach Popen when the audit fails."""
        from kiro_crew.apps.builtins.auto_improvement.spine import agent_runner as ar

        monkeypatch.setattr(ar, "_audit_unattended_agent", lambda **kw: False)

        def _no_spawn(*a, **kw):  # pragma: no cover - reaching this IS the failure
            raise AssertionError("spawned a permissionless agent without an audit trail")

        monkeypatch.setattr(ar, "popen_limited", _no_spawn)
        result = ar.AgentRunner().run("do a thing", cwd=str(tmp_path))
        assert result.ok is False
        assert "audited" in result.error


class TestActivityFeedIsRedacted:
    """The live activity feed is an egress path the drift guard could not see: it walks
    redactor call sites, and `runner.py` had none.

    Raised by review of this branch. Every entry is served verbatim by
    `RunSupervisor.status()` -> `GET /run` -> `activityLine` in the browser, and the
    strings inside are RAW model output — assistant text and the `command` of a bash tool
    call. A credential the discovery agent reads out of the target clone and quotes in its
    turn crossed to the dashboard unscanned, while three sibling paths in the same PR
    already scanned exactly this class of text.
    """

    def test_a_credential_in_agent_text_is_redacted(self) -> None:
        from kiro_crew.apps.builtins.auto_improvement.backend.runner import _redact_activity

        event = {"kind": "tool", "detail": "aws_access_" "key_id=glpat-IOSFODNN7EXAMPLE"}
        out = _redact_activity(event)
        assert "glpat-IOSFODNN7EXAMPLE" not in str(out)

    def test_nested_agent_events_are_reached(self) -> None:
        """The agent event is nested under an "agent" key — redacting only the top level
        would miss the field that actually carries the text."""
        from kiro_crew.apps.builtins.auto_improvement.backend.runner import _redact_activity

        nested = {"agent": {"detail": "aws_access_" "key_id=glpat-IOSFODNN7EXAMPLE"}}
        assert "glpat-IOSFODNN7EXAMPLE" not in str(_redact_activity(nested))
        listed = {"agent": {"lines": ["aws_access_" "key_id=glpat-IOSFODNN7EXAMPLE"]}}
        assert "glpat-IOSFODNN7EXAMPLE" not in str(_redact_activity(listed))

    def test_ordinary_text_and_non_strings_survive(self) -> None:
        """Fail-open feed: it must stay readable, and numbers must stay numbers."""
        from kiro_crew.apps.builtins.auto_improvement.backend.runner import _redact_activity

        event = {"kind": "stage", "cycle": 3, "detail": "running the gate", "ok": True}
        assert _redact_activity(event) == event

    def test_appended_activity_is_scanned_end_to_end(self) -> None:
        """Through the real append path, not just the helper."""
        from kiro_crew.apps.builtins.auto_improvement.backend.runner import RunSupervisor

        sup = RunSupervisor()
        sup._on_agent_activity({"detail": "aws_access_" "key_id=glpat-IOSFODNN7EXAMPLE"})
        assert "glpat-IOSFODNN7EXAMPLE" not in str(list(sup._state.activity))


class TestFallbackAuditsEveryToolItUses:
    """The subprocess fallback logged ONE blanket launch event, not the tools it then ran.

    The launch is audited ``critical=True`` before spawn, but that records only "an
    unattended agent started" — a forensic query could not answer "did this run touch a
    shell?". The session path gets per-tool events from its approval hook; the fallback
    parsed `tool_use` blocks already (that is what drives the UI activity feed) and simply
    never persisted them, so the information was present and thrown away.

    Raised repeatedly by the GPT review of this branch — its "remove the fallback" remedy
    was declined (it is the only path that authors fixes with no in-process provider), but
    the audit gap it named was real and is closed here.
    """

    def test_each_tool_use_is_logged_to_sel(self, monkeypatch) -> None:
        from kiro_crew.apps.builtins.auto_improvement.spine import agent_runner as ar

        logged: list[dict] = []

        class _Sel:
            def log_tool_invocation(self, **kw):
                logged.append(kw)

        import kiro_crew.sel as sel_mod

        monkeypatch.setattr(sel_mod, "sel", lambda: _Sel())
        ar._audit_fallback_tool(tool="Bash", detail="pytest -q tests/", cwd="/w")

        assert len(logged) == 1, "the tool was not audited"
        ev = logged[0]
        assert ev["tool_name"] == "Bash" and ev["outcome"] == "invoked"
        assert ev["metadata"]["unattended"] is True
        assert ev["metadata"]["target"] == "pytest -q tests/"
        # NOT critical: the tool has already run, so raising cannot prevent anything and
        # would only turn an audit-sink problem into a failed run.
        assert not ev.get("critical")

    def test_the_target_hint_is_redacted(self, monkeypatch) -> None:
        """`detail` is agent-influenced text landing in a log signed as-written.

        Uses an AWS access-key id, which `kiro_crew.security.redact` matches by SHAPE.
        Note the scanner is not exhaustive — measured while writing this: the lowercase
        `aws_secret_access_key=<v>` form is caught but the UPPERCASE
        `AWS_SECRET_ACCESS_KEY=<v>` env-assignment form is not. Widening the shared
        redactor is out of scope for this PR; this test pins that the hint goes THROUGH
        the redactor, which is the part this module controls.
        """
        from kiro_crew.apps.builtins.auto_improvement.spine import agent_runner as ar

        logged: list[dict] = []

        class _Sel:
            def log_tool_invocation(self, **kw):
                logged.append(kw)

        import kiro_crew.sel as sel_mod

        monkeypatch.setattr(sel_mod, "sel", lambda: _Sel())
        ar._audit_fallback_tool(tool="Bash", detail="aws s3 cp glpat-IOSFODNN7EXAMPLE", cwd=None)
        assert "glpat-IOSFODNN7EXAMPLE" not in logged[0]["metadata"]["target"]
        assert "REDACTED" in logged[0]["metadata"]["target"]

    def test_a_broken_redactor_never_emits_raw_agent_text(self, monkeypatch) -> None:
        from kiro_crew.apps.builtins.auto_improvement.spine import agent_runner as ar

        logged: list[dict] = []

        class _Sel:
            def log_tool_invocation(self, **kw):
                logged.append(kw)

        import kiro_crew.security as security_mod
        import kiro_crew.sel as sel_mod

        def _boom(_t):
            raise RuntimeError("scanner down")

        monkeypatch.setattr(sel_mod, "sel", lambda: _Sel())
        monkeypatch.setattr(security_mod, "redact", _boom)
        ar._audit_fallback_tool(tool="Bash", detail="secret-ish text", cwd=None)
        assert logged[0]["metadata"]["target"] == "[redaction unavailable]"

    def test_a_broken_audit_sink_does_not_fail_the_run(self, monkeypatch) -> None:
        """The tool already ran — a log failure must not raise into the stream loop."""
        import kiro_crew.sel as sel_mod
        from kiro_crew.apps.builtins.auto_improvement.spine import agent_runner as ar

        def _boom():
            raise OSError("sink down")

        monkeypatch.setattr(sel_mod, "sel", _boom)
        ar._audit_fallback_tool(tool="Bash", detail="x", cwd=None)  # must not raise

    def test_the_stream_loop_audits_tool_events(self) -> None:
        """Structural: the parsed `tool` event must reach the auditor, not just the UI."""
        import inspect

        from kiro_crew.apps.builtins.auto_improvement.spine.agent_runner import AgentRunner

        src = inspect.getsource(AgentRunner)
        assert "_audit_fallback_tool(" in src, "tool_use blocks are still unaudited"


class TestBrowserServedFeedsFailClosed:
    """Two redactors failed OPEN on the reasoning that nothing there leaves the host.

    Both are served over HTTP with no second scan: `status()` puts the activity list into the
    `GET /run` JSON, and `GET /watchers/{fp}/log` returns watcher lines verbatim. So each is
    the only thing standing between agent/CI output and the operator's browser — the same
    boundary `routes._redact_for_display` already fails CLOSED on. Measured with a raising
    redactor: `aws_secret_access_key=…` reached the `/run` payload unchanged.

    Failing closed does not blank either feed — the unscannable STRING becomes a placeholder
    while structure, timestamps and other fields survive, which was the real concern behind
    the original fail-open choice. Raised by the GPT review of this branch.
    """

    def test_the_activity_feed_withholds_unscannable_text(self, monkeypatch) -> None:
        import kiro_crew.security as security_mod
        from kiro_crew.apps.builtins.auto_improvement.backend import runner as R

        def _boom(_t):
            raise RuntimeError("scanner down")

        monkeypatch.setattr(security_mod, "redact", _boom)
        secret = "aws_secret_" "access_key=wJalrXUtnFEMI0K7EXAMPLE"
        out = R._redact_activity(
            {"t": 1.5, "agent": {"kind": "tool", "detail": secret}, "ok": True}
        )

        assert secret not in str(out), "raw agent text reached the browser-served payload"
        # The feed must stay USABLE: structure and non-string fields survive.
        assert out["t"] == 1.5 and out["ok"] is True
        assert "withheld" in out["agent"]["detail"]

    def test_the_watcher_log_withholds_unscannable_text(self, monkeypatch) -> None:
        import kiro_crew.security as security_mod
        from kiro_crew.apps.builtins.auto_improvement.backend import pr_watchers

        def _boom(_t):
            raise RuntimeError("scanner down")

        monkeypatch.setattr(security_mod, "redact", _boom)
        out = pr_watchers._redact("aws_secret_" "access_key=wJalrXUtnFEMI0K7EXAMPLE")
        assert "wJalrXUtnFEMI0K7EXAMPLE" not in out
        assert "withheld" in out

    def test_the_normal_path_still_redacts_and_keeps_text(self) -> None:
        """The fix must not turn a WORKING redactor into a wall of placeholders."""
        from kiro_crew.apps.builtins.auto_improvement.backend import pr_watchers
        from kiro_crew.apps.builtins.auto_improvement.backend import runner as R

        assert "REDACTED" in pr_watchers._redact("aws_secret_" "access_key=glpat-IOSFODNN7EXAMPLE")
        assert pr_watchers._redact("gate: build GREEN") == "gate: build GREEN"
        assert R._redact_activity({"d": "propose: 3 candidates"}) == {"d": "propose: 3 candidates"}


class TestAgentTestsCannotWriteKiroCrewConfig:
    """`mode="strict"` hides credential READS; it does not make the filesystem read-only.

    Measured on this host before the fix: a strict-mode child ran
    `open('~/.kiro/crew/config.json','a')` and exited 0 — it MODIFIED Kiro Crew's own
    write-protected config. Those paths are `security.write_protected_home_paths()`, enforced
    by the platform HOOK layer, which a sandboxed subprocess never passes through, so the
    protection was inert for exactly the code that most needs it: the target repository's
    pytest, including a candidate's agent-authored conftest.

    `_run` now bind-mounts an empty dir over the PARENT of each write-protected path.
    Raised by the GPT review of this branch.
    """

    def test_the_masked_targets_are_directories_that_exist(self) -> None:
        """A FILE path silently no-ops: the launcher's `sensitive_dirs` loop is guarded by
        `os.path.isdir(target)`, and files are masked through a separate list that
        `sandboxed_spawn_argv` does not expose. Passing files is why the first attempt at
        this fix changed nothing."""
        from kiro_crew.apps.builtins.auto_improvement.profiles.github_repo import profile as P

        targets = P._write_protected_targets()
        for t in targets:
            assert Path(t).is_dir(), f"{t} is not a directory — the mask would be a no-op"

    def test_it_covers_every_write_protected_path(self) -> None:
        """Derived from the platform list, so a newly-protected path cannot drift uncovered."""
        from kiro_crew.agent_sdk.native_allow_stores import home_leaves
        from kiro_crew.apps.builtins.auto_improvement.profiles.github_repo import profile as P
        from kiro_crew.security import write_protected_home_paths

        masked = {Path(t) for t in P._write_protected_targets()}
        home = Path.home()
        native = set(home_leaves())
        for rel in write_protected_home_paths():
            parent = (home / rel).parent
            if parent.is_dir() and rel not in native:
                assert parent in masked, f"{rel} is not covered by any masked directory"

    def test_a_harness_config_dir_is_not_hidden(self, tmp_path, monkeypatch) -> None:
        """Each harness's own permission files are on the write tier for the file-edit
        tools only; masking their parents would hide ``~/.claude`` and its siblings from
        the target repo's pytest, reads included."""
        from kiro_crew.agent_sdk.native_allow_stores import home_leaves
        from kiro_crew.apps.builtins.auto_improvement.profiles.github_repo import profile as P

        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        native_parents = {tmp_path / Path(rel).parent for rel in home_leaves()}
        for parent in native_parents:
            parent.mkdir(parents=True, exist_ok=True)
        crew_dir = tmp_path / ".kiro" / "crew"
        crew_dir.mkdir(parents=True, exist_ok=True)

        masked = {Path(t) for t in P._write_protected_targets()}
        assert crew_dir in masked, "Kiro Crew's own config dir must still be hidden"
        assert not native_parents & masked, sorted(str(p) for p in native_parents & masked)

    @pytest.mark.skipif(os.name == "nt", reason="POSIX home anchoring, as test_native_allow_stores")
    def test_the_file_edit_guard_still_refuses_harness_files(self, tmp_path, monkeypatch) -> None:
        """Leaving the harness dirs unmasked here does not loosen the write tier."""
        from kiro_crew.agent_sdk.native_allow_stores import home_leaves, override_env_vars
        from kiro_crew.security import is_sensitive_write_path

        monkeypatch.setenv("HOME", str(tmp_path))
        for env in override_env_vars():
            monkeypatch.delenv(env, raising=False)
        for rel in home_leaves():
            assert is_sensitive_write_path(str(tmp_path.joinpath(*Path(rel).parts))), rel

    def test_the_run_helper_passes_the_mask_to_the_sandbox(self) -> None:
        """Structural: resolving the targets is useless unless `_run` actually passes them."""
        import inspect

        from kiro_crew.apps.builtins.auto_improvement.profiles.github_repo import profile as P

        src = inspect.getsource(P._run)
        assert (
            "extra_hidden_dirs=_write_protected_targets()" in src
        ), "the sandbox spawn does not mask Kiro Crew's write-protected paths"

    def test_a_broken_helper_still_runs_the_suite(self, monkeypatch) -> None:
        """Fail-soft: masking is defense-in-depth. Refusing to run any test would be worse."""
        import kiro_crew.security as security_mod
        from kiro_crew.apps.builtins.auto_improvement.profiles.github_repo import profile as P

        def _boom():
            raise RuntimeError("unavailable")

        monkeypatch.setattr(security_mod, "write_protected_home_paths", _boom)
        assert P._write_protected_targets() == ()


class TestFallbackAgentCannotSeeCredentials:
    """The `claude -p` fallback runs with `--dangerously-skip-permissions`, so its Bash
    tool is unattended. It must therefore run in the sandbox's STRICT mode, not the
    default "standard" — which deliberately leaves ``~/.aws`` readable so a test suite can
    use the AWS CLI. A fix-AUTHORING agent has no such need.

    Measured on the dev host while fixing this: standard exposed all 7 ``~/.aws`` entries
    to the child, strict exposed 0. This test pins the MODE rather than re-measuring the
    filesystem, so it is meaningful on a host where namespaces are unavailable too.
    """

    def test_the_spawn_requests_strict_mode(self, monkeypatch, tmp_path) -> None:
        from kiro_crew.apps.builtins.auto_improvement.spine import agent_runner as ar

        seen: dict = {}

        def _fake_spawn(argv, mode="standard", **kw):
            seen["mode"] = mode
            seen["kw"] = kw
            return list(argv), {}, None

        # Patch the name in THIS module's namespace: `sandboxed_spawn_argv` is imported at
        # module scope, so it is bound here at import time and patching
        # `kiro_crew.sandbox` would not affect the already-bound reference.
        monkeypatch.setattr(ar, "sandboxed_spawn_argv", _fake_spawn)
        monkeypatch.setattr(ar, "popen_limited", lambda *a, **k: object())
        ar.AgentRunner()._spawn_sandboxed_agent(["/bin/true"], str(tmp_path))

        assert seen["mode"] == "strict", "the unattended agent must not see credential dirs"
        # The worktree must stay VISIBLE or there is nothing for the agent to edit.
        assert str(tmp_path.resolve()) in seen["kw"]["extra_visible_dirs"]
        # And the agent's tooling must not inherit Kiro Crew's interpreter paths.
        assert seen["kw"]["strip_python_env"] is True


class TestTheFallbackNeverBypassesAConfiguredProvider:
    """A configured provider whose agent registration fails must go OFFLINE, not subprocess.

    `_build_runner` must not fall through when `SessionAgentRunner.available()` is True but
    `ensure_agent_registered()` is False: landing on `AgentRunner`, i.e. `claude -p`, bypasses
    the provider's own permission gate even though a provider EXISTS. "The fallback only runs
    when no provider is configured" holds for the `available()` branch and NOT for the
    registration-failure branch, which is the hole this closes — the fallback is only
    defensible when there is genuinely no provider to route through.
    """

    @staticmethod
    def _choose(*, provider_available: bool, registered: bool, claude_present: bool):
        from unittest.mock import patch

        from kiro_crew.apps.builtins.auto_improvement.backend import crew
        from kiro_crew.apps.builtins.auto_improvement.backend import runner as R
        from kiro_crew.apps.builtins.auto_improvement.spine import agent_runner as ar
        from kiro_crew.apps.builtins.auto_improvement.spine.crew_runner import CrewRunner

        with (
            patch.object(
                ar.SessionAgentRunner, "available", staticmethod(lambda: provider_available)
            ),
            patch.object(crew, "build_runner", lambda **kwargs: CrewRunner(None, {})),
            patch.object(CrewRunner, "ensure_agent_registered", lambda self: registered),
            patch.object(ar.AgentRunner, "available", staticmethod(lambda: claude_present)),
            # This class is about the SELECTION (provider vs subprocess vs offline), not the
            # sandbox, so the credential-confinement precondition is satisfied here; the gate
            # has its own class (TestTheLoopRunnerRefusesWithoutCredentialConfinement).
            patch.object(R, "_credentials_are_unconfined", lambda: ""),
        ):
            return R.RunSupervisor()._build_runner(stop_check=lambda: False)

    def test_a_registration_failure_goes_offline_not_subprocess(self) -> None:
        chosen = self._choose(provider_available=True, registered=False, claude_present=True)
        assert chosen is None, (
            f"chose {type(chosen).__name__} — a configured provider's permission gate was "
            "bypassed by falling back to the subprocess agent"
        )

    def test_the_watcher_path_also_refuses_rather_than_falling_through(self) -> None:
        """`pr_watchers._make_runner` is the TWIN of `runner._build_runner`.

        Fixing one and not the other left the same hole open: inside
        `if SessionAgentRunner.available():` a registration failure fell through to
        `claude -p`, bypassing a CONFIGURED provider's permission gate. GPT's finding moved
        straight from `runner.py` to `pr_watchers.py:781` once the first was fixed, which is
        how the second site surfaced. It RAISES here (this function's contract) and
        `_run_watcher` turns that into `STATUS_ERROR` — a failed pass, not a dead gateway.
        """
        import threading
        from unittest.mock import patch

        from kiro_crew.apps.builtins.auto_improvement.backend import pr_watchers as W
        from kiro_crew.apps.builtins.auto_improvement.spine import agent_runner as ar

        st = W.WatcherState(fp="fp1", pr="https://github.com/o/r/pull/1")
        registry = W.PRWatcherRegistry()
        with (
            # The egress gate fires FIRST and is a separate refusal; accept it so this test
            # reaches the subprocess-fallback refusal it is actually about.
            patch.object(W, "_watcher_egress_accepted", lambda: True),
            patch.object(ar.SessionAgentRunner, "available", staticmethod(lambda: True)),
            patch.object(ar.SessionAgentRunner, "ensure_agent_registered", lambda self: False),
            patch.object(ar.AgentRunner, "available", staticmethod(lambda: True)),
        ):
            with pytest.raises(RuntimeError, match="refusing the subprocess fallback"):
                registry._make_runner(st, threading.Event())

    def test_the_watcher_never_selects_the_subprocess_fallback(self) -> None:
        """Even with `claude` on PATH, an unavailable provider must NOT shell out.

        REPLACES a test that asserted the opposite. `create_provider_factory` has two
        returns (`AcpProvider(...)` and `_acp`) and never returns None, so
        `SessionAgentRunner.available()` is False only when the config load or the factory
        RAISES — a broken install, not an unconfigured one. Nudging a PR through
        `claude -p --dangerously-skip-permissions` in that state runs an unattended agent
        outside the provider's permission gate exactly when the platform is unhealthy.
        """
        import threading
        from unittest.mock import patch

        from kiro_crew.apps.builtins.auto_improvement.backend import pr_watchers as W
        from kiro_crew.apps.builtins.auto_improvement.spine import agent_runner as ar

        st = W.WatcherState(fp="fp1", pr="https://github.com/o/r/pull/1")
        registry = W.PRWatcherRegistry()
        with (
            patch.object(W, "_watcher_egress_accepted", lambda: True),
            patch.object(ar.SessionAgentRunner, "available", staticmethod(lambda: False)),
            patch.object(ar.AgentRunner, "available", staticmethod(lambda: True)),
        ):
            with pytest.raises(RuntimeError, match="refusing the subprocess fallback"):
                registry._make_runner(st, threading.Event())

    def test_the_run_path_never_selects_the_subprocess_fallback(self) -> None:
        """REPLACES a test that asserted the fallback was kept for the no-provider case.

        That case cannot occur: `create_provider_factory` never returns None (two returns,
        `AcpProvider(...)` and `_acp` — verified by inspecting its source), so
        `SessionAgentRunner.available()` is False only when loading RAISES. The review asked
        for this removal on every head and was right on the facts; running OFFLINE (no
        fabricated fixes) is the honest outcome when the platform cannot provide a governed
        agent. `AgentRunner` the class remains for a future caller that can route it properly.
        """
        chosen = self._choose(provider_available=False, registered=True, claude_present=True)
        assert chosen is None, f"chose {type(chosen).__name__} — the fallback was selected"

    def test_a_registered_provider_is_preferred(self) -> None:
        chosen = self._choose(provider_available=True, registered=True, claude_present=True)
        assert type(chosen).__name__ == "CrewRunner"

    def test_nothing_available_is_offline(self) -> None:
        assert self._choose(provider_available=False, registered=True, claude_present=False) is None


class TestTheWatcherRefusesToRunWithoutEgressAcknowledgement:
    """A watcher agent is UNATTENDED, its prompt embeds outsider-writable PR-comment text, and
    it needs `gh` (host auth + network) to read PR state — so it cannot run under a strict
    credential+network sandbox without deleting the feature (D-84), and the provider-runner
    path's sandbox hides credential DIRECTORIES but does not isolate the network (D-105). That
    residual exfil risk is not the runner's to silently accept.

    `_make_runner` therefore FAILS CLOSED: it refuses to build ANY watcher runner unless the
    operator has explicitly set `watcherAcceptEgressRisk` (default OFF), the same one-time
    consent shape as `watcherAutoStart` (D-107). This is the operator-decision resolution of
    the GPT review's `pr_watchers.py:782` finding — chosen over forcing strict (which breaks
    `gh`) and over silently accepting the risk.
    """

    def _registry(self):
        from kiro_crew.apps.builtins.auto_improvement.backend import pr_watchers as W

        return W, W.PRWatcherRegistry()

    def test_absent_flag_refuses(self, monkeypatch, tmp_path) -> None:
        import threading

        W, registry = self._registry()
        # An empty config on disk → flag absent → OFF.
        monkeypatch.setattr(W.store, "read_json", lambda *_a, **_k: {})
        st = W.WatcherState(fp="fp1", pr="https://github.com/o/r/pull/1")
        with pytest.raises(RuntimeError, match="watcher runner refused"):
            registry._make_runner(st, threading.Event())

    def test_flag_false_refuses(self, monkeypatch) -> None:
        import threading

        W, registry = self._registry()
        monkeypatch.setattr(
            W.store, "read_json", lambda *_a, **_k: {"watcherAcceptEgressRisk": False}
        )
        st = W.WatcherState(fp="fp2", pr="https://github.com/o/r/pull/2")
        with pytest.raises(RuntimeError, match="watcher runner refused"):
            registry._make_runner(st, threading.Event())

    def test_a_truthy_non_true_value_still_refuses(self, monkeypatch) -> None:
        """Only the explicit boolean opts in — a stray ``1`` or ``"yes"`` must not, so a
        malformed config cannot accidentally grant egress."""
        import threading

        W, registry = self._registry()
        for sneaky in (1, "true", "yes"):
            monkeypatch.setattr(
                W.store, "read_json", lambda *_a, _v=sneaky, **_k: {"watcherAcceptEgressRisk": _v}
            )
            st = W.WatcherState(fp="fp3", pr="https://github.com/o/r/pull/3")
            with pytest.raises(RuntimeError, match="watcher runner refused"):
                registry._make_runner(st, threading.Event())

    def test_the_gate_reads_config_fresh_each_call(self) -> None:
        """Structural: the flag is read via a helper on every build, not cached, so turning it
        off immediately stops new passes."""
        import inspect

        from kiro_crew.apps.builtins.auto_improvement.backend import pr_watchers as W

        src = inspect.getsource(W.PRWatcherRegistry._make_runner)
        assert "_watcher_egress_accepted()" in src, (
            "the egress gate is not consulted in `_make_runner`, so an unattended watcher can "
            "run without the operator accepting the network-egress risk"
        )
        # And the gate must precede the runner construction, not sit after it.
        assert src.index("_watcher_egress_accepted()") < src.index(
            "SessionAgentRunner"
        ), "the egress gate runs AFTER the runner is chosen — it must refuse first"

    def test_an_injected_factory_bypasses_the_gate_for_tests(self, monkeypatch) -> None:
        """The `_runner_factory` test seam is checked BEFORE the gate, so unit tests that inject
        a fake runner are not forced to also flip a global config flag. The gate guards the real
        provider/subprocess path, which is where the egress risk lives."""
        import threading

        W, registry = self._registry()
        sentinel = object()
        registry._runner_factory = lambda: sentinel
        # Even with the flag OFF, an injected factory is returned untouched.
        monkeypatch.setattr(W.store, "read_json", lambda *_a, **_k: {})
        assert (
            registry._make_runner(
                W.WatcherState(fp="fp4", pr="https://github.com/o/r/pull/4"), threading.Event()
            )
            is sentinel
        )

    def test_the_config_key_is_writable(self) -> None:
        from kiro_crew.apps.builtins.auto_improvement.backend.routes import _CONFIG_WRITABLE

        assert (
            "watcherAcceptEgressRisk" in _CONFIG_WRITABLE
        ), "the operator cannot set the egress acknowledgement, so the watcher can never run"


class TestTheLoopRunnerRefusesWithoutCredentialConfinement:
    """The loop requires effective strict isolation or explicit risk acknowledgement.

    The provider inherits `agent.sandbox`, clamped by the governance floor. The
    `cc` profile leaves SSH and GitHub CLI credentials visible; `auto` and
    `standard` also expose AWS credentials. These modes require the operator's
    explicit `acceptUnsandboxedAgentRisk` decision before unattended execution.
    """

    @pytest.mark.parametrize(
        "mode,floor,confined",
        [
            ("standard", None, False),
            ("auto", None, False),
            ("auto", "cc", False),
            ("auto", "strict", True),
        ],
    )
    @pytest.mark.parametrize("accept_risk", [False, True])
    def test_requires_effective_strict_or_explicit_risk(
        self, monkeypatch, mode, floor, confined, accept_risk
    ) -> None:
        from kiro_crew import sandbox
        from kiro_crew.apps.builtins.auto_improvement.backend import runner as R
        from kiro_crew.config import KiroCrewConfig

        config = KiroCrewConfig()
        config.agent.sandbox = mode
        monkeypatch.setattr(KiroCrewConfig, "load", classmethod(lambda cls: config))
        monkeypatch.setattr(sandbox, "_governance_sandbox_floor", lambda: floor)
        monkeypatch.setattr(
            R.store, "read_json", lambda *_a, **_k: {"acceptUnsandboxedAgentRisk": accept_risk}
        )
        reason = R._credentials_are_unconfined()
        if confined or accept_risk:
            assert reason == ""
        else:
            assert repr(floor or mode) in reason
            assert "sandbox.min_level governance floor of 'strict'" in reason
            assert "acceptUnsandboxedAgentRisk" in reason

    def test_only_the_explicit_boolean_opts_in(self, monkeypatch) -> None:
        """A stray ``1``/``"yes"`` must not grant it — same `is True` contract as the watcher
        gate, so a malformed config cannot accidentally expose credentials."""
        from kiro_crew.apps.builtins.auto_improvement.backend import runner as R

        for sneaky in (1, "true", "yes", None):
            monkeypatch.setattr(
                R.store,
                "read_json",
                lambda *_a, _v=sneaky, **_k: {"acceptUnsandboxedAgentRisk": _v},
            )
            assert R._unsandboxed_agent_accepted() is False, f"{sneaky!r} opted in"

    def test_an_unreadable_config_fails_closed(self, monkeypatch) -> None:
        """A sandbox state we cannot VERIFY is treated as unconfined — the alternative is
        running the agent with credentials visible on the strength of a failed read."""
        from kiro_crew.apps.builtins.auto_improvement.backend import runner as R

        monkeypatch.setattr(R, "_unsandboxed_agent_accepted", lambda: False)

        class _Boom:
            @staticmethod
            def load():
                raise OSError("config is unreadable")

        import kiro_crew.config as cfgmod

        monkeypatch.setattr(cfgmod, "KiroCrewConfig", _Boom)
        assert R._credentials_are_unconfined(), "an unreadable sandbox setting must fail closed"

    def test_the_gate_precedes_the_runner_construction(self) -> None:
        """Structural: the check must refuse BEFORE a runner is built, and `_build_runner` must
        consult it at all — so a later edit cannot reintroduce the unconfined path."""
        import inspect

        from kiro_crew.apps.builtins.auto_improvement.backend import runner as R

        src = inspect.getsource(R.RunSupervisor._build_runner)
        assert "_credentials_are_unconfined()" in src, (
            "the loop's runner does not check credential confinement, so a provider-driven "
            "agent can run with the operator's credential stores visible"
        )
        assert src.index("_credentials_are_unconfined()") < src.index(
            "runner = build_runner("
        ), "the confinement check runs AFTER the runner is constructed — it must refuse first"

    def test_the_config_key_is_writable(self) -> None:
        from kiro_crew.apps.builtins.auto_improvement.backend.routes import _CONFIG_WRITABLE

        assert "acceptUnsandboxedAgentRisk" in _CONFIG_WRITABLE, (
            "the operator cannot acknowledge the risk, so a default install can never run "
            "the provider-backed agent"
        )


class TestAgentRegistrationFailsClosed:
    """A failed agent registration must NOT fall through to the default agent.

    kiro-cli does not error on an unknown agent name — it silently activates the DEFAULT
    agent, which carries the full kirocrew-core toolset including ``spawn_sub_agents``.
    ``ensure_agent_registered`` returns a bool for exactly this reason, and both call
    sites ignored it: ``runner._build_runner`` discarded it, and the watcher builder never
    registered at all. An unwritable ``~/.kiro/agents`` therefore widened an unattended
    agent's tool scope to everything — the opposite of what registering it is for.

    Raised by review of this branch. Tool scoping is what stopped the subagent-orphan
    hang, so a silent fallback also reintroduces that failure mode.
    """

    def test_runner_refuses_the_session_runner_when_registration_fails(self, monkeypatch) -> None:
        from kiro_crew.apps.builtins.auto_improvement.backend import crew
        from kiro_crew.apps.builtins.auto_improvement.backend import runner as R
        from kiro_crew.apps.builtins.auto_improvement.backend.runner import RunSupervisor
        from kiro_crew.apps.builtins.auto_improvement.spine import agent_runner as ar
        from kiro_crew.apps.builtins.auto_improvement.spine.crew_runner import CrewRunner

        monkeypatch.setattr(ar.SessionAgentRunner, "available", staticmethod(lambda: True))
        monkeypatch.setattr(crew, "build_runner", lambda **kwargs: CrewRunner(None, {}))
        monkeypatch.setattr(CrewRunner, "ensure_agent_registered", lambda self: False)
        monkeypatch.setattr(R, "_credentials_are_unconfined", lambda: "")
        # No subprocess fallback either, so the result must be "offline", never a runner
        # with an unscoped agent.
        monkeypatch.setattr(ar.AgentRunner, "available", staticmethod(lambda: False))

        got = RunSupervisor()._build_runner(stop_check=lambda: False)
        assert got is None, "an unregistered agent must not be used for unattended work"

    def test_runner_returns_the_session_runner_when_registration_succeeds(
        self, monkeypatch
    ) -> None:
        """The happy path must be untouched — this is a guard, not a new refusal."""
        from kiro_crew.apps.builtins.auto_improvement.backend import crew
        from kiro_crew.apps.builtins.auto_improvement.backend import runner as R
        from kiro_crew.apps.builtins.auto_improvement.backend.runner import RunSupervisor
        from kiro_crew.apps.builtins.auto_improvement.spine import agent_runner as ar
        from kiro_crew.apps.builtins.auto_improvement.spine.crew_runner import CrewRunner

        monkeypatch.setattr(ar.SessionAgentRunner, "available", staticmethod(lambda: True))
        monkeypatch.setattr(crew, "build_runner", lambda **kwargs: CrewRunner(None, {}))
        monkeypatch.setattr(CrewRunner, "ensure_agent_registered", lambda self: True)
        # This test is about REGISTRATION, not the sandbox: satisfy the credential-confinement
        # precondition so it exercises the path it names (see
        # TestTheLoopRunnerRefusesWithoutCredentialConfinement for the gate itself).
        monkeypatch.setattr(R, "_credentials_are_unconfined", lambda: "")
        got = RunSupervisor()._build_runner(stop_check=lambda: False)
        assert isinstance(got, CrewRunner)

    def test_the_watcher_builder_registers_before_returning(self) -> None:
        """Structural: the watcher path must call registration and honor its result."""
        from pathlib import Path

        src = (Path(__file__).resolve().parent.parent / "backend" / "pr_watchers.py").read_text(
            encoding="utf-8"
        )
        assert "ensure_agent_registered()" in src, "watcher never registers the agent"
        assert "if session_runner.ensure_agent_registered():" in src, "result not honored"

    def test_registration_never_clobbers_a_users_existing_agent_file(
        self, tmp_path, monkeypatch
    ) -> None:
        """`~/.kiro/agents/<name>.json` is the USER's directory. Unconditionally copying the
        app's agent JSON there destroys a file the user wrote if it happens to share this
        app's agent name. Registration must write only when the file is absent or already
        byte-identical, and REFUSE (leaving the user's file intact) on a real conflict — the
        run then proceeds with the default agent, as it does on any registration failure.
        Raised by the GPT review of this branch."""
        from kiro_crew.apps.builtins.auto_improvement.spine import agent_runner as ar

        # Point kiro-cli's home at a tmp dir so we never touch the real ~/.kiro.
        monkeypatch.setenv("KIRO_HOME", str(tmp_path / "kiro"))
        from kiro_crew.config.paths import kiro_agents_dir

        agents_dir = kiro_agents_dir()
        agents_dir.mkdir(parents=True, exist_ok=True)
        dest = agents_dir / "auto-improvement-discovery.json"

        # A pre-existing USER file under the same name, with THEIR content.
        users_content = '{"name": "auto-improvement-discovery", "tools": ["everything"]}'
        dest.write_text(users_content, encoding="utf-8")

        runner = ar.SessionAgentRunner(agent_name="auto-improvement-discovery")
        ok = runner.ensure_agent_registered()

        assert ok is False, "registration reported success while clobbering the user's file"
        assert dest.read_text(encoding="utf-8") == users_content, "the user's agent was overwritten"

    def test_registration_is_idempotent_when_the_file_is_already_ours(
        self, tmp_path, monkeypatch
    ) -> None:
        """The guard must not break re-registration: an identical existing file is a no-op
        success, not a refusal."""
        from kiro_crew.apps.builtins.auto_improvement.spine import agent_runner as ar

        monkeypatch.setenv("KIRO_HOME", str(tmp_path / "kiro"))

        runner = ar.SessionAgentRunner(agent_name="auto-improvement-discovery")
        # First registration writes the app's file; the second must see it as ours.
        assert runner.ensure_agent_registered() is True
        assert runner.ensure_agent_registered() is True, "identical re-register was refused"

    def test_registration_writes_when_no_file_exists(self, tmp_path, monkeypatch) -> None:
        from kiro_crew.apps.builtins.auto_improvement.spine import agent_runner as ar

        monkeypatch.setenv("KIRO_HOME", str(tmp_path / "kiro"))
        from kiro_crew.config.paths import kiro_agents_dir

        runner = ar.SessionAgentRunner(agent_name="auto-improvement-discovery")
        assert runner.ensure_agent_registered() is True
        assert (kiro_agents_dir() / "auto-improvement-discovery.json").is_file()


class TestFallbackAgentInheritsNoCredentials:
    """Review raised "exported GITHUB_TOKEN reaches the fallback agent" repeatedly, and it
    was TRUE even after the spawn was sandboxed: `kiro_crew.sandbox.scrub_env` covers
    AWS_SECRET/SLACK_*/TELEGRAM_* but not GITHUB_*.

    Measured before fixing: the child printed the real token value. This agent runs with
    `--dangerously-skip-permissions`, so a repository instruction could have used it. The
    gate already stripped by shape; the agent spawn now does too — the two places untrusted
    content executes.
    """

    def test_credential_shaped_names_are_dropped(self) -> None:
        from kiro_crew.apps.builtins.auto_improvement.spine.agent_runner import (
            strip_credential_env,
        )

        out = strip_credential_env(
            {
                "PATH": "/usr/bin",
                "HOME": "/h",
                "GITHUB_TOKEN": "ghp_x",
                "MY_API_KEY": "k",
                "DB_PASSWORD": "p",
                "AWS_SECRET_ACCESS_KEY": "s",
                "NPM_CREDENTIAL": "c",
            }
        )
        assert sorted(out) == ["HOME", "PATH"], out

    def test_the_spawn_applies_the_strip(self) -> None:
        """Structural: the sandboxed spawn must run the strip, not just define it."""
        import inspect

        from kiro_crew.apps.builtins.auto_improvement.spine.agent_runner import AgentRunner

        src = inspect.getsource(AgentRunner._spawn_sandboxed_agent)
        assert "strip_credential_env(scrubbed_env)" in src
        # And it must happen AFTER the sandbox builds the env, or it would be undone.
        assert src.index("sandboxed_spawn_argv(") < src.index("strip_credential_env(")

    def test_functional_variables_survive(self) -> None:
        """An agent that cannot find python is not a fix."""
        from kiro_crew.apps.builtins.auto_improvement.spine.agent_runner import (
            strip_credential_env,
        )

        out = strip_credential_env({"PATH": "/usr/bin", "LANG": "C", "TMPDIR": "/tmp"})
        assert sorted(out) == ["LANG", "PATH", "TMPDIR"]


class TestCommitMessageRedactionFailsClosed:
    """A commit message becomes UNWIPEABLE git history the moment it is pushed, so an
    agent-authored subject that CANNOT be credential-scanned must not be committed
    verbatim — both the one-click path (`commit._commit_message`) and the driver's
    direct-commit path (`Driver._redact_commit_message`) fall back to a fixed, prose-free
    subject when the redactor errors. Raised by the GPT review of this branch (both failed
    open before, returning the raw agent text).
    """

    def test_one_click_commit_message_falls_back_on_a_broken_redactor(
        self, tmp_path, monkeypatch
    ) -> None:
        from kiro_crew.apps.builtins.auto_improvement.backend import commit as commit_mod

        def _boom(_t):
            raise RuntimeError("scanner down")

        # Patch the name in the CONSUMING module: `redact` is imported at module scope
        # there, so it is bound at import time and patching `kiro_crew.security` would
        # leave the already-bound reference — and the real redactor — in place, silently
        # not exercising the broken-redactor path this test exists for.
        monkeypatch.setattr(commit_mod, "redact", _boom)
        body = tmp_path / "fp.pr.md"
        body.write_text("# aws_secret_" "access_key=glpat-IOSFODNN7EXAMPLE\n", encoding="utf-8")
        subject = commit_mod._commit_message(body, "deadbeefcafe")
        assert "glpat-IOSFODNN7EXAMPLE" not in subject, "an unscanned credential reached the commit"
        assert subject == "auto-improvement: apply verified change deadbeefcafe"

    def test_driver_commit_message_falls_back_on_a_broken_redactor(self, monkeypatch) -> None:
        import kiro_crew.security as security_mod
        from kiro_crew.apps.builtins.auto_improvement.spine.driver import Driver

        def _boom(_t):
            raise RuntimeError("scanner down")

        monkeypatch.setattr(security_mod, "redact", _boom)
        out = Driver._redact_commit_message("leak aws_secret_" "access_key=glpat-IOSFODNN7EXAMPLE")
        assert "glpat-IOSFODNN7EXAMPLE" not in out
        assert out == "auto-improvement: apply verified change"


class TestTerminalErrorsAreRedacted:
    """`_state.error` reached the dashboard unscanned while `_state.activity` beside it was
    redacted — the SAME response object, one field guarded and one not.

    `status()` serializes both (`"error": st.error`) and `SetupPanel` renders it verbatim
    (`run?.error || t('runError')`). The string is `f"{type(exc).__name__}: {exc}"`, and an
    exception message routinely quotes the thing that failed: a git url, a subprocess argv,
    a path. When a run dies on an agent-influenced value that happens to contain a
    credential, that credential crosses to the browser — the exact egress boundary
    `_redact_activity` was made FAIL-CLOSED for. Raised by the GPT review.
    """

    def test_a_credential_in_a_terminal_error_does_not_reach_the_response(self) -> None:
        from kiro_crew.apps.builtins.auto_improvement.backend import runner as runner_mod

        secret = "glpat-IOSFODNN7EXAMPLE"
        sup = runner_mod.RunSupervisor()
        sup._fail(RuntimeError(f"clone failed: aws_secret_access_key={secret}"))
        status = sup.status()
        assert secret not in str(status), "a credential reached the run-status response"
        assert secret not in status["error"], "a credential reached the error field"
        # Still ACTIONABLE: the operator must be able to tell what kind of failure it was.
        assert "RuntimeError" in status["error"], "the error lost its type"

    def test_the_activity_copy_is_redacted_too(self) -> None:
        """The error is appended to the feed as well; redacting one copy and not the other
        leaves the same string on the same response."""
        from kiro_crew.apps.builtins.auto_improvement.backend import runner as runner_mod

        secret = "glpat-IOSFODNN7EXAMPLE"
        sup = runner_mod.RunSupervisor()
        sup._fail(RuntimeError(f"boom {secret}"))
        assert secret not in str(sup.status()["activity"]), "the feed copy is unredacted"

    def test_every_terminal_error_site_goes_through_the_helper(self) -> None:
        """There are three `_state.error` assignments (run loop, calibrate loop, and the
        canary's reported non-clear). A fix applied to one is how the next one drifts."""
        import inspect

        from kiro_crew.apps.builtins.auto_improvement.backend import runner as runner_mod

        # Scan every method EXCEPT `_fail` itself, which is the redaction site: its own
        # assignment is of an already-scanned value, so including it would make the guard
        # unsatisfiable. Excluding by source range rather than by the variable's name, so
        # renaming `message` cannot silently create a hole.
        fail_src = inspect.getsource(runner_mod.RunSupervisor._fail)
        src = inspect.getsource(runner_mod.RunSupervisor).replace(fail_src, "")
        raw = [
            ln.strip()
            for ln in src.splitlines()
            if "_state.error = " in ln
            # An explicitly reviewed site may opt out, but it must SAY so on the line, so
            # the exemption is visible in review rather than inferred from absence.
            and "redaction-exempt" not in ln
        ]
        assert raw == [], (
            f"terminal-error assignment(s) bypassing `_fail`: {raw} — these reach "
            "`GET /run` and are rendered by SetupPanel"
        )


class TestANestedProcessCannotAuthenticateToGitHub:
    """Review asked to remove ``Bash`` from the watcher's allowed tools, because the shell
    denylist inspects only the REQUESTED command: `gh pr comment` is refused, but
    `python helper.py` is allowed and the helper could call `gh api` itself. The premise is
    correct — measured, `python helper.py`, `make test` and `./run.sh` are all ALLOWED.

    The proposed fix is declined, because it deletes the feature: the watcher's own prompt
    requires running the repo's build/test/lint commands, `gh pr view --comments`,
    `gh pr checks`, `gh run view --log-failed`, a rebase, and a local commit. Without Bash
    there is no watcher.

    The mutation is instead blocked one layer down, where it is actually enforceable rather
    than pattern-matched: a nested process inherits no GitHub credential.
      * ``strip_credential_env`` removes GH_TOKEN / GITHUB_TOKEN / GITHUB_ENTERPRISE_TOKEN
        from the spawn env, so the env route is closed.
      * ``sandboxed_spawn_argv(mode="strict")`` hides ``~/.config/gh``, so the STORED OAuth
        credential is closed too — which is the route a token-strip alone would miss.

    Measured on the author's host: `~/.config/gh` contains `hosts.yml`, yet a nested
    `subprocess` inside the sandbox lists that directory as EMPTY, and a nested
    `gh auth status` reports "You are not logged into any GitHub hosts" (rc=1). So the
    escalation this finding describes ends at an unauthenticated `gh`. The denylist remains
    valuable as defence in depth for the DIRECT command; it was never the only control.
    """

    def test_the_env_route_is_closed(self) -> None:
        from kiro_crew.apps.builtins.auto_improvement.spine.push_policy import (
            strip_credential_env,
        )

        out = strip_credential_env(
            {
                "GITHUB_TOKEN": "ghp_x",
                "GH_TOKEN": "ghp_y",
                "GITHUB_ENTERPRISE_TOKEN": "z",
                "PATH": "/usr/bin",
            }
        )
        assert "PATH" in out, "stripping must not break the spawn"
        for k in ("GITHUB_TOKEN", "GH_TOKEN", "GITHUB_ENTERPRISE_TOKEN"):
            assert k not in out, f"{k} reached a nested process"

    def test_the_stored_credential_route_is_closed(self) -> None:
        """The half a token-strip cannot cover: `gh` reads `~/.config/gh/hosts.yml`."""
        from kiro_crew.sandbox import _STRICT_DIRS

        assert ".config/gh" in _STRICT_DIRS, (
            "strict mode no longer hides gh's credential store, so a nested process can "
            "authenticate and mutate GitHub state"
        )

    def test_the_watcher_spawns_in_strict_mode(self) -> None:
        """Both controls above only apply if the agent actually goes through them."""
        import inspect

        from kiro_crew.apps.builtins.auto_improvement.spine import agent_runner as ar

        src = inspect.getsource(ar)
        assert 'mode="strict"' in src, "the agent spawn left strict mode"
        assert "strip_credential_env(" in src, "the spawn no longer strips credentials"


class TestMcpErrorsAreRedacted:
    """MCP tool RESULTS were redacted; the ERROR paths beside them were not.

    `_dispatch` interpolates `str(exc)` straight into both the SEL audit record and the
    JSON-RPC error message, and tool argument values reach exception text by design —
    `_tool_get_finding` raises `f"no finding with fingerprint {fp}"` with the caller's raw
    `fp`. So a credential-shaped argument came back verbatim to the model. Reproduced end to
    end: `get_finding` with `fp="aws_secret_access_key=glpat-…"` returned
    `{"error": {"message": "no finding with fingerprint aws_secret_access_key=glpat-…"}}`.

    This is D-81's defect on a second surface — the same "results scanned, errors not"
    asymmetry — which is why the fix routes every error string through the SAME fail-closed
    redactor the results use rather than adding a second ad-hoc one. Raised by the GPT review.
    """

    def _call(self, tmp_path, monkeypatch, fp: str):
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        from kiro_crew.apps.builtins.auto_improvement.backend import mcp_server as m

        return m.handle(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "get_finding", "arguments": {"fp": fp}},
            }
        )

    def test_a_credential_in_an_argument_does_not_come_back(self, tmp_path, monkeypatch) -> None:
        import json

        secret = "glpat-IOSFODNN7EXAMPLE"
        resp = self._call(tmp_path, monkeypatch, f"aws_secret_access_key={secret}")
        assert secret not in json.dumps(
            resp
        ), f"a credential-shaped argument was echoed back to the model: {json.dumps(resp)[:200]}"

    def test_the_error_is_still_useful(self, tmp_path, monkeypatch) -> None:
        """Redaction must not turn every error into an opaque blob — a caller still has to
        learn that the fingerprint was not found."""
        resp = self._call(tmp_path, monkeypatch, "deadbeef")
        msg = str(resp.get("error", {}).get("message", ""))
        assert "fingerprint" in msg or "finding" in msg, f"error lost its meaning: {msg!r}"

    def test_a_credential_shaped_tool_NAME_does_not_come_back(self, tmp_path, monkeypatch) -> None:
        """The tool NAME is also caller-supplied and reaches the same two readers as the
        argument: the SEL record (`_audit(name, …)`) and the JSON-RPC error (`unknown tool:
        {name}`). An unknown-tool `tools/call` with a credential-shaped name must not echo it
        back or persist it raw. Raised by the GPT review."""
        import json

        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        from kiro_crew.apps.builtins.auto_improvement.backend import mcp_server as m

        secret = "glpat-IOSFODNN7EXAMPLE"
        resp = m.handle(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": f"aws_access_key_id={secret}", "arguments": {}},
            }
        )
        assert secret not in json.dumps(
            resp
        ), f"a credential-shaped tool NAME was echoed back to the model: {json.dumps(resp)[:200]}"
        # Still a useful, correctly-coded rejection. `handle` is typed `dict | None`; a
        # `tools/call` always returns a response, so narrow it before reading the code.
        assert resp is not None
        assert resp.get("error", {}).get("code") == m._METHOD_NOT_FOUND

    def test_the_unknown_tool_branch_never_passes_the_raw_name(self) -> None:
        """Structural: inside the `tools/call` branch, every `_audit(...)` and error/log
        mention must use the redacted `safe_name`, never the raw `name` (the raw value is used
        only for the `TOOLS` allowlist dict-lookup, which is never emitted)."""
        import inspect
        import re

        from kiro_crew.apps.builtins.auto_improvement.backend import mcp_server as m

        src = inspect.getsource(m.handle)
        branch = src[src.index('if method == "tools/call":') :]
        assert "safe_name = _redact_error(name)" in branch, "the tool name is not redacted once"
        # No audit/error/log site may reference the raw `name`.
        assert not re.search(r"_audit\(name\b", branch), "an _audit call still uses the raw name"
        assert "{name}" not in branch, "an f-string still interpolates the raw name"

    def test_every_error_path_uses_the_shared_redactor(self) -> None:
        """Structural: three sites build an error string from `str(exc)`; fixing one is how
        the next one drifts."""
        import ast
        import inspect

        from kiro_crew.apps.builtins.auto_improvement.backend import mcp_server as m

        # Walk STATEMENTS, not lines: the operator's `print(..., file=sys.stderr)` spans
        # several lines, so a line filter cannot tell it apart from a model-facing return.
        tree = ast.parse(inspect.getsource(m.handle).lstrip())
        offenders: list[str] = []
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Return, ast.Call)):
                continue
            seg = ast.unparse(node)
            if "exc" not in seg or "_redact" in seg:
                continue
            # `print(..., file=sys.stderr)` is the OPERATOR's local diagnostic, not a model
            # or SEL reader: scrubbing it would hide the detail needed to fix an unavailable
            # audit log, and it crosses no boundary the other sites do.
            if "sys.stderr" in seg:
                continue
            # Only the two egress readers matter: the JSON-RPC response and the SEL record.
            if seg.startswith("_error(") or seg.startswith("_audit("):
                offenders.append(seg[:120])
        assert offenders == [], f"unredacted exception text reaches the model: {offenders}"


class TestWatcherSnapshotsAreRedacted:
    """The watcher LOG ring is redacted on write (`_log` calls `_redact` before appending), but
    the `as_dict()` SNAPSHOT beside it was served raw — a third instance of the
    "one field on a response is scanned, its neighbour is not" asymmetry behind D-81 and D-88.

    `as_dict()` carries `target`, `title`, `lastNote`, `verdict`/`verdictReason` and `fixing`,
    all derived from model output or from the pull request's own text (which the watcher
    ingests as untrusted input by design). Measured: a `WatcherState` whose `target` is
    `src/m.py::aws_secret_access_key=glpat-…` serialized that credential verbatim, and both the
    reconcile listing and the start response hand it to the browser.

    `_redact_tree` already exists in this module and is used for the findings response, so the
    fix is to use it here too rather than add a fourth mechanism. `get_log` deliberately stays
    as-is: it is scanned at the point of write, which is stronger. Raised by the GPT review.
    """

    def test_the_snapshot_carries_agent_authored_text(self) -> None:
        """The premise: if `as_dict` ever stops exposing model-derived fields, this whole
        class is moot and should be re-derived rather than trusted."""
        from kiro_crew.apps.builtins.auto_improvement.backend import pr_watchers as pw

        st = pw.WatcherState(fp="fp1", pr="https://github.com/o/r/pull/1")
        keys = set(st.as_dict())
        for field in ("target", "title", "lastNote", "verdictReason", "fixing"):
            assert field in keys, f"as_dict no longer exposes {field!r}"

    def test_both_watcher_responses_are_redacted(self) -> None:
        import inspect

        from kiro_crew.apps.builtins.auto_improvement.backend import routes as routes_mod

        for handler in (routes_mod._handle_watchers, routes_mod._handle_watcher_start):
            src = inspect.getsource(handler)
            responses = [ln for ln in src.splitlines() if "json_response(" in ln]
            assert responses, f"{handler.__name__} has no response line"
            assert any(
                "_redact_tree" in ln for ln in responses
            ), f"{handler.__name__} serves an unredacted watcher snapshot: {responses}"

    def test_a_credential_in_a_snapshot_field_is_scrubbed(self) -> None:
        import json

        from kiro_crew.apps.builtins.auto_improvement.backend import pr_watchers as pw
        from kiro_crew.apps.builtins.auto_improvement.backend import routes as routes_mod

        secret = "glpat-IOSFODNN7EXAMPLE"
        st = pw.WatcherState(fp="fp1", pr="https://github.com/o/r/pull/1")
        st.target = f"src/m.py::aws_secret_access_key={secret}"
        st.last_note = f"agent said {secret}"
        scrubbed = routes_mod._redact_tree(st.as_dict())
        assert secret not in json.dumps(
            scrubbed
        ), f"a credential survived the snapshot redaction: {json.dumps(scrubbed)[:200]}"
        # Structure must survive — the UI reads these keys by name.
        assert set(scrubbed) == set(st.as_dict()), "redaction changed the response shape"


class TestCommitErrorsReachTheBrowserRedacted:
    """The commit route returned `str(result.get("error"))` verbatim, and `commit.py` built
    that value from a raw fixed-bound slice of git stderr — which quotes the ref, the
    path, and whatever a repository's own hooks printed. (`commit.py` now scrubs its
    stderr at the source with `redact_via_context`; this route-level pass stays as the
    output-boundary backstop.)

    This was latent while nothing rendered it. D-97 (surfacing a refused commit at the finding
    row, so the operator learns WHY) turned it into a live egress path: a failing pre-commit
    hook that echoes a credential now puts it on the page. A fix that makes an error visible
    has to make it safe at the same time — otherwise improving the UX is the thing that opens
    the leak.

    Review named one site; the same shape appears at five `result.get("error")` responses plus
    the PR-status one and the two draft-path bodies, all carrying subprocess output to the same
    reader, so all were closed. Raised by the GPT review.
    """

    def test_no_error_response_serves_raw_subprocess_output(self) -> None:
        import inspect
        import re

        from kiro_crew.apps.builtins.auto_improvement.backend import routes as routes_mod

        src = inspect.getsource(routes_mod)
        # Any `"error": str(<something>.get("error")…)` that is not wrapped is a leak. The
        # pattern is deliberately narrow: `str(exc)` sites are validation messages built from
        # the request, not from a subprocess.
        # Keyed on `.get("error"...)` specifically, not on the dict name: the draft route also
        # does `str(result.get("detail")…)`, and `detail` is one of two FIXED app-authored
        # strings ("draft pull request opened" / "still queued locally…") with no subprocess
        # output in it. My first version of this scan flagged it — a false positive that would
        # have had me redact a constant.
        offenders = [
            m.group(0)
            for m in re.finditer(
                r'"error":\s*str\((?:result|status|staged|committed)\.get\(\s*"error"', src
            )
        ]
        assert offenders == [], f"unredacted subprocess output reaches the dashboard: {offenders}"

    def test_a_credential_in_git_stderr_is_scrubbed(self) -> None:
        from kiro_crew.apps.builtins.auto_improvement.backend import routes as routes_mod

        secret = "glpat-IOSFODNN7EXAMPLE"
        stderr = f"fatal: could not read Username for 'https://x:{secret}@github.com'"
        assert secret not in routes_mod._redact_for_display(
            stderr
        ), "the display redactor does not scrub a credential embedded in git stderr"

    def test_the_redactor_keeps_the_actionable_part(self) -> None:
        """Scrubbing must not turn every refusal into an opaque blob — D-97 exists so the
        operator learns why the commit failed."""
        from kiro_crew.apps.builtins.auto_improvement.backend import routes as routes_mod

        msg = "could not check out main: branch is protected by push policy"
        out = routes_mod._redact_for_display(msg)
        assert "protected by push policy" in out, f"the reason was lost: {out!r}"


class TestWatcherSandboxConfinesCredentialsButNotEgress:
    """The watcher's residual risk, pinned and stated rather than papered over.

    Review asked twice to remove `Bash` from the PR watcher (D-84, then again). It stays,
    because the watcher's four documented jobs — run the repo's build/test/lint, rebase,
    `gh pr view --comments`, commit locally — are all shell, so removing it deletes the
    feature rather than hardening it. But the second report added a claim D-84 never tested,
    and re-measuring showed it is CORRECT:

      * CREDENTIALS are confined. A nested process under `mode="strict"` sees `~/.aws`,
        `~/.config/gh` and `~/.docker` as EMPTY on a host where they are populated, and
        `~/.ssh` exposes only `known_hosts` (deliberate — host-key verification needs it)
        while `id_rsa`/`*.key` stay hidden. `gh auth status` reports "not logged into any
        GitHub hosts".
      * NETWORK EGRESS is NOT confined. The sandbox never enters a network namespace
        (`CLONE_NEWNET` appears nowhere in `sandbox.py`, and its own docstring explains that
        agentic commands need reachable networking). `curl`/`wget`/`nc` are on
        `_FORBIDDEN_BINARIES`, but `python helper.py` is ALLOWED and can open a socket, so a
        prompt-injection in a PR comment can reach the network. The denylist cannot close
        this — it gates the requested command, not what that command then does.

    So the honest posture is: the watcher may be pointed only at repositories whose PR
    comments you would be willing to execute. That is documented at the disclosure sink and
    in the module spec; these tests pin the half that IS enforced, so a regression in the
    credential confinement fails loudly instead of quietly widening the gap.
    """

    def test_the_strict_dir_list_still_covers_every_credential_store(self) -> None:
        from kiro_crew.sandbox import _STRICT_DIRS

        for required in (".aws", ".config/gh", ".config/gcloud", ".docker", ".kube", ".gnupg"):
            assert (
                required in _STRICT_DIRS
            ), f"{required} left the strict hide-list — a nested watcher process can now read it"

    def test_ssh_keys_are_hidden_while_known_hosts_is_exposed(self) -> None:
        """The one deliberate exception, asserted so a future edit cannot widen it to the
        whole directory (which would expose `id_rsa`)."""
        from kiro_crew.sandbox_plan import (
            BACKEND_SEATBELT,
            PlanHost,
            SandboxRequest,
            plan_confinement,
        )
        from kiro_crew.sandbox_seatbelt import render_seatbelt_profile

        home = os.path.abspath("/srv/u")
        ssh = os.path.join(home, ".ssh")
        known_hosts = os.path.join(ssh, "known_hosts")
        linux = plan_confinement(SandboxRequest(tier="strict"), PlanHost(home=home))
        assert linux.hide_ssh, "the strict tier no longer hides ~/.ssh"
        assert (linux.ssh_dir, linux.ssh_known_hosts) == (ssh, known_hosts)
        macos = plan_confinement(
            SandboxRequest(tier="strict", backend=BACKEND_SEATBELT), PlanHost(home=home)
        )
        rule = (
            f'(deny file-read* (require-all (subpath "{ssh}")'
            f' (require-not (literal "{known_hosts}"))))'
        )
        assert rule in render_seatbelt_profile(macos), "the narrow known_hosts exposure changed"

    def test_the_network_binaries_are_still_denied(self) -> None:
        """Not sufficient (a nested interpreter bypasses it) but still the first barrier, so a
        regression here would remove the only egress control that exists."""
        from kiro_crew.apps.builtins.auto_improvement.spine.agent_runner import (
            shell_command_refusal,
        )

        for cmd in ("curl http://x/", "wget http://x/", "nc x 80", "ssh h", "scp a b"):
            assert shell_command_refusal(cmd), f"{cmd!r} is no longer denied"

    def test_the_egress_limit_is_disclosed(self) -> None:
        """The residual risk must be WRITTEN DOWN, because it is the operator's decision to
        accept. A silent limitation is the actual defect here."""
        from kiro_crew.security_posture import build_posture_snapshot

        blob = str(build_posture_snapshot()).lower()
        assert (
            "egress" in blob or "network" in blob
        ), "the watcher's un-confined network egress is not disclosed anywhere"
