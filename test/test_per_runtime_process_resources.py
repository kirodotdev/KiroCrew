"""Process-level resources are per RUNTIME, and the scope says which runtime.

A transient scope carries no identity of its own: systemd auto-names it
``run-u<N>.scope``, so a reader holding a scope -- the slice OOM report, an
operator reading ``systemctl --user list-units`` -- can name the victim directory
and nothing else. One scope holds one spawn and one spawn is one agent runtime,
which may serve several sessions, so that missing name is exactly the step
between "a scope died" and "these sessions died". These tests pin the naming that
supplies it, and pin the two properties that must survive it: every existing
caller's argv is unchanged, and a naming defect can never cost a spawn its
ceiling.

They also pin the unit of state of the resources the ownership audit found being
treated as per-session, so the docstrings describing them cannot drift back: the
cli.json overlay is keyed by work directory, and the kiro-cli chat log by process.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from unittest.mock import patch

import pytest

import kiro_crew.sandbox as sb


def _wrap(argv=None):
    """``cgroup_scope_argv`` with the cgroup backend forced available.

    Mirrors ``test_sandbox_agent_slice_identity``'s fixture: the probe, the
    trusted-binary resolution and the off-thread slice reconciliation are all
    stubbed so the assertions do not depend on the test host having cgroup v2
    delegation or systemd-run in a trusted directory.
    """
    sb._CGROUP_SCOPE_PROBE = None
    sb._CGROUP_WARNED = False
    try:
        with (
            patch("kiro_crew.sandbox._probe_cgroup_scope", return_value=(True, "ok")),
            patch(
                "kiro_crew.platform_compat.trusted_system_bin",
                return_value="/usr/bin/systemd-run",
            ),
            patch("kiro_crew.sandbox._reconcile_slice_memory_high_off_thread"),
            patch(
                "kiro_crew.sandbox._cgroup_limits_from_config",
                return_value=(8192, 8192, 50, 0),
            ),
            patch("kiro_crew.sandbox._cpu_controller_delegated", return_value=False),
        ):
            return sb.cgroup_scope_argv(list(argv or ["kiro-cli", "chat"]))
    finally:
        sb._CGROUP_SCOPE_PROBE = None
        sb._CGROUP_WARNED = False


def _unit_arg(argv: list[str]) -> str | None:
    """The value following ``--unit`` in a systemd-run argv, or None."""
    for i, a in enumerate(argv):
        if a == "--unit":
            return argv[i + 1]
    return None


class TestScopeUnitName:
    def test_name_carries_the_token_so_a_scope_resolves_to_its_runtime(self):
        """The whole point: the token is recoverable FROM the scope name.

        The same token travels in the child's environment as the spawn
        incarnation, so a name that did not contain it verbatim would leave the
        two unjoinable and the naming would buy nothing.
        """
        name = sb.scope_unit_name("a1b2c3d4e5f6a7b8")
        assert name == "kirocrew-rt-a1b2c3d4e5f6a7b8.scope"
        assert "a1b2c3d4e5f6a7b8" in name

    def test_distinct_spawns_get_distinct_names(self):
        assert sb.scope_unit_name("aaaa1111") != sb.scope_unit_name("bbbb2222")

    @pytest.mark.parametrize(
        "token",
        [
            "",
            "has space",
            "has/slash",
            "has.dot",
            "has:colon",
            "unicodé",
            "x" * 65,
        ],
    )
    def test_a_token_that_cannot_form_a_legal_unit_is_refused(self, token):
        """Refused rather than escaped or mangled.

        A mangled name would still be emitted and would still fail to match a
        reverse lookup, so it would cost the spawn its ceiling for no gain --
        every caller mints its own opaque token, so a token needing escapes is a
        caller bug rather than a user-supplied value to accommodate.
        """
        assert sb.scope_unit_name(token) is None


class TestScopeIsNamed:
    @pytest.mark.parametrize(
        "token",
        ["deadbeef", "a1b2c3d4e5f6a7b8", "A" * 64, "with-dash", "with_underscore", "0"],
    )
    def test_a_valid_token_reaches_the_argv_before_the_separator(self, token):
        """A scope option, so it must precede ``--``; after it, it is an argument.

        Parametrized over the whole accepted token shape rather than one sample:
        a single case cannot tell "naming works" from "naming works for the one
        token I happened to pick", and the naming is guarded by a regex where
        exactly that distinction is where a defect would sit.
        """
        argv = sb.name_scope_unit(_wrap(), token)
        assert _unit_arg(argv) == f"kirocrew-rt-{token}.scope"
        assert argv.index("--unit") < argv.index("--")

    def test_the_wrapped_command_is_untouched(self):
        argv = sb.name_scope_unit(_wrap(["kiro-cli", "chat"]), "deadbeef")
        assert argv[argv.index("--") + 1 :] == ["kiro-cli", "chat"]

    def test_the_unnamed_wrap_is_the_argv_it_always_was(self):
        """The load-bearing compatibility property.

        ``cgroup_scope_argv`` has dozens of callers that wrap a one-off tool or
        app subprocess and have no runtime to name. Naming is a separate step
        precisely so none of them changes, and this is the assertion that keeps
        the wrap itself untouched.
        """
        assert "--unit" not in _wrap()

    @pytest.mark.parametrize(
        "token", ["", "has space", "has/slash", "has.dot", "has:colon", "unicodé", "x" * 65]
    )
    def test_a_token_that_cannot_be_spelled_leaves_the_ceiling_intact(self, token):
        """Degrade to an anonymous scope, never to an unbounded or failed spawn.

        ``systemd-run`` exits non-zero on a name it cannot parse, so emitting an
        unvalidated name would turn a naming defect into a failed spawn -- losing
        the process as well as the ceiling. The scope's job is the DoS ceiling;
        the name is a diagnostic, and the diagnostic must lose.
        """
        wrapped = _wrap()
        argv = sb.name_scope_unit(wrapped, token)
        assert argv == wrapped
        assert "--unit" not in argv
        assert "MemoryMax=8192M" in argv
        assert "TasksMax=8192" in argv

    def test_a_spawn_that_was_never_wrapped_is_left_alone(self):
        """On a host with no cgroup delegation there is no scope to name.

        ``cgroup_scope_argv`` hands the bare command back there, so naming has to
        recognise that and do nothing -- inserting ``--unit`` into a plain command
        line would pass it to the command as an argument.
        """
        bare = ["kiro-cli", "chat"]
        assert sb.name_scope_unit(bare, "deadbeef") == bare

    def test_naming_a_scope_does_not_move_the_slice_or_the_ceilings(self):
        """Identity is added; confinement is not renegotiated."""
        plain = _wrap()
        named = sb.name_scope_unit(_wrap(), "deadbeef")
        assert [a for a in plain if a.startswith("--slice=")] == [
            a for a in named if a.startswith("--slice=")
        ]
        for prop in ("TasksMax=8192", "MemoryMax=8192M", "MemorySwapMax=0"):
            assert prop in plain and prop in named


class TestRuntimeNamesItsScope:
    def test_the_runtime_spawn_names_its_scope_after_its_spawn_instance(self):
        """The scope name and the child's env marker are ONE token.

        Read from the source rather than by driving a spawn: the wiring under
        test is that the token is minted before the cgroup wrap and used by both,
        and a spawn test would pass just as well with two independent tokens,
        which is the defect this is against.
        """
        src = Path(sb.__file__).with_name("acp") / "runtime.py"
        body = src.read_text(encoding="utf-8")
        mint = body.index("spawn_instance = uuid.uuid4().hex[:16]")
        wrap = body.index("name_scope_unit(argv, spawn_instance)")
        env = body.index("env[KIROCREW_SPAWN_INSTANCE_ENV] = spawn_instance")
        assert mint < wrap, "the token must exist before the scope is named"
        assert wrap < env, "the same token must then travel in the child's env"
        assert (
            body.count("spawn_instance = uuid.uuid4().hex[:16]") == 1
        ), "two mints would give the scope and the process different identities"

    def test_the_spawn_records_the_unit_only_when_the_wrap_happened(self):
        """The recorded unit is conditional, because the wrap is.

        ``cgroup_scope_argv`` hands back the bare command on macOS, on Windows, on
        Linux without cgroup-v2 ``--user`` delegation, and for ``systemd-run``
        outside a trusted directory -- and the token is spellable on all of them.
        So the only thing that distinguishes "this runtime has a scope" from "this
        runtime has none" is whether the naming step changed the argv, and the
        recording has to be gated on exactly that.
        """
        src = Path(sb.__file__).with_name("acp") / "runtime.py"
        body = src.read_text(encoding="utf-8")
        record = [ln for ln in body.splitlines() if ln.strip().startswith("scope_unit = ")]
        assert len(record) == 1, "exactly one place decides the recorded unit"
        line = record[0]
        assert "named_argv is not argv" in line, (
            "the recorded unit must be gated on the argv actually changing, not on "
            f"the token alone -- got: {line.strip()}"
        )
        assert body.count("self._scope_unit = scope_unit") == 1, (
            "the decided value is what reaches the runtime, so the log cannot "
            "re-derive a name the wrap never applied"
        )

    def test_the_runtime_logs_its_scope_name_beside_its_pid(self):
        """The scope name reaches a durable record, not only the live process.

        The case the naming exists for is a scope the kernel already killed, where
        the child's environment and the in-memory token are both gone with it. The
        initialization log line is what survives that, so it is what makes the
        scope resolvable at all; read it out of the AST rather than by matching
        text, so reformatting the call cannot break the pin and the pin cannot
        match its own assertion.
        """
        src = Path(sb.__file__).with_name("acp") / "runtime.py"
        tree = ast.parse(src.read_text(encoding="utf-8"))
        calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
            and node.args[0].value.startswith("AcpRuntime initialized")
        ]
        assert len(calls) == 1, "exactly one line announces an initialized runtime"
        call = calls[0]
        recorded = any(
            isinstance(node, ast.Attribute) and node.attr == "_scope_unit"
            for node in ast.walk(call)
        )
        assert recorded, "the initialized line must carry the RECORDED scope unit name"
        derived = any(
            isinstance(node, ast.Call) and getattr(node.func, "id", "") == "scope_unit_name"
            for node in ast.walk(call)
        )
        assert not derived, (
            "the name must not be re-derived from the token here: the token is always "
            "spellable, so a derived name announces a scope on every host that has none"
        )
        assert call.args[0].value.count("%") == len(call.args) - 1, (
            "every placeholder needs its argument, or the log call raises "
            "at the one moment it is the only surviving record"
        )

    def test_a_no_op_naming_is_the_same_object_so_the_spawn_can_detect_it(self):
        """The spawn tells "named" from "not named" by object identity.

        ``AcpRuntime._spawn`` records the unit only when ``name_scope_unit`` really
        changed the argv, and it asks that question with ``is not``. That is only a
        correct question while the no-op path returns the caller's own list rather
        than a copy, so pin the identity itself: an equal-but-distinct list would
        make every non-delegated host record a scope it does not have.
        """
        bare = ["kiro-cli", "chat"]
        assert sb.name_scope_unit(bare, "deadbeef") is bare
        unspellable = _wrap()
        assert sb.name_scope_unit(unspellable, "not a token") is unspellable
        wrapped = _wrap()
        assert sb.name_scope_unit(wrapped, "deadbeef") is not wrapped

    def test_the_recorded_scope_unit_is_cleared_wherever_the_token_is(self):
        """A dead runtime must not keep answering with a scope name.

        The token and the unit name are two halves of one join, so a site that
        clears one and leaves the other publishes a scope name for a process that
        has ended -- which is the same false claim as naming a scope that never
        existed, just later.
        """
        src = Path(sb.__file__).with_name("acp") / "runtime.py"
        body = src.read_text(encoding="utf-8")
        token_clears = body.count('self._process_instance = ""')
        unit_clears = body.count('self._scope_unit = ""')
        assert token_clears >= 2, "control: the token is cleared on the teardown paths"
        assert (
            unit_clears == token_clears
        ), f"{token_clears} sites clear the token but {unit_clears} clear the unit name"


class TestOverlayIsKeyedByWorkDir:
    def test_the_overlay_path_is_derived_from_the_work_dir_alone(self, tmp_path):
        """No session appears in the path, so sessions sharing a work dir share it.

        The overlay's unit of state is the work directory, not the writing slot's
        session: two sessions on one runtime share the work directory and
        therefore this file, and kiro-cli reads it at spawn.
        """
        from kiro_crew.providers.acp import _read_cli_overlay, _write_cli_overlay

        _write_cli_overlay(tmp_path, "claude-sonnet-4", "high")
        written = tmp_path / ".kiro" / "settings" / "cli.json"
        assert written.is_file()
        assert json.loads(written.read_text(encoding="utf-8"))["chat.modelDefaults"]
        assert _read_cli_overlay(tmp_path) == {"claude-sonnet-4": "high"}

    def test_a_second_write_for_one_model_replaces_the_first(self, tmp_path):
        """One entry per model per work dir: the last writer is what a respawn reads."""
        from kiro_crew.providers.acp import _read_cli_overlay, _write_cli_overlay

        _write_cli_overlay(tmp_path, "claude-sonnet-4", "high")
        _write_cli_overlay(tmp_path, "claude-sonnet-4", "low")
        assert _read_cli_overlay(tmp_path) == {"claude-sonnet-4": "low"}


class TestChatLogIsPerProcess:
    def test_the_log_path_lives_under_the_process_own_scratch(self, tmp_path):
        """Per process, and per process means per runtime rather than per session.

        ``shared`` names the tree a joining process mounts, and the log
        deliberately does NOT follow it: two live processes appending to one
        kiro-cli log is the failure the pin exists to prevent. What it cannot fix
        is several sessions on ONE process, which share the file by construction.
        """
        from kiro_crew import agent_scratch

        own, shared = tmp_path / "own", tmp_path / "shared"
        env = agent_scratch.scratch_env(own, shared=shared)
        assert env["KIROCREW_SCRATCH"] == str(shared)
        assert env["TMPDIR"] == str(own)
        if agent_scratch._CAN_CAP_LOGS:
            assert env[agent_scratch.KIRO_CHAT_LOG_FILE_ENV].startswith(str(own))
            assert str(shared) not in env[agent_scratch.KIRO_CHAT_LOG_FILE_ENV]
