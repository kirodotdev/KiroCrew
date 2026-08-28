"""Codex adapter resolution and authentication.

The load-bearing test here is ``test_a_bare_codex_cli_does_not_resolve``: the
reference implementation had a third resolver rung that fell back to
``["codex", "acp"]``, and its own live probe against codex-cli 0.147.0 found that
the CLI treats ``acp`` as a PROMPT rather than starting an ACP server. That rung
would spawn an ordinary chat turn against the operator's subscription and fail as
a protocol timeout, so it is deliberately absent and this test keeps it absent.
"""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest

from kiro_crew.acp import codex, spec_servers

_POSIX_EXEC_PATHS_ONLY = pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX executable-resolution semantics only"
)


@pytest.fixture()
def codex_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "codexhome"
    home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(home))
    return home


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Isolate resolution from whatever this host happens to have installed.

    Setting ``PATH`` alone does NOT achieve this, which is why these tests only
    passed while the adapter happened to be absent: ``augmented_path`` appends
    the version-manager directories (``{mise_data}/shims``,
    ``{mise_data}/installs/node/*/bin``, ``{home}/.nvm/...``) to whatever ``PATH``
    it is handed, so a real global install is still on the search path. Redirect
    the roots those patterns expand from as well.
    """
    monkeypatch.delenv("CODEX_ACP_BIN", raising=False)
    empty = tmp_path / "empty-path"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    # The roots augmented_path interpolates. Without these, this fixture's own
    # promise is not kept and the test reads the operator's machine.
    monkeypatch.setenv("MISE_DATA_DIR", str(tmp_path / "no-mise"))
    monkeypatch.setenv("HOME", str(tmp_path / "no-home"))
    codex._argv_cache = codex._UNRESOLVED


def _make_executable(path: Path) -> Path:
    if sys.platform == "win32" and not path.suffix:
        path = path.with_suffix(".cmd")
        path.write_text("@exit /b 0\n")
    else:
        path.write_text("#!/bin/sh\nexit 0\n")
        path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


class TestResolutionLadder:
    def test_nothing_installed_resolves_to_none(self) -> None:
        assert codex.resolve_argv() is None

    def test_executable_override_runs_directly(self, tmp_path: Path) -> None:
        exe = _make_executable(tmp_path / "codex-acp")
        os.environ["CODEX_ACP_BIN"] = str(exe)
        assert codex.resolve_argv() == [str(exe)]

    @_POSIX_EXEC_PATHS_ONLY
    def test_non_executable_script_override_is_wrapped_with_node(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A bare .js entry has no x-bit and no usable shebang in a daemon."""
        node = _make_executable(tmp_path / "node")
        bin_dir = tmp_path / "nodebin"
        bin_dir.mkdir()
        (bin_dir / "node").symlink_to(node)
        monkeypatch.setenv("PATH", str(bin_dir))

        script = tmp_path / "adapter.js"
        script.write_text("console.log(1)")
        os.environ["CODEX_ACP_BIN"] = str(script)

        argv = codex.resolve_argv()
        assert argv is not None
        assert len(argv) == 2
        assert argv[1] == str(script.resolve())

    def test_a_nonexistent_override_is_ignored(self, tmp_path: Path) -> None:
        """A stale override must not mask the rest of the ladder or crash."""
        os.environ["CODEX_ACP_BIN"] = str(tmp_path / "gone")
        assert codex.resolve_argv() is None

    def test_adapter_on_path_resolves(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        executable = _make_executable(bin_dir / codex.CODEX_ACP_BIN)
        monkeypatch.setenv("PATH", str(bin_dir))
        argv = codex.resolve_argv()
        assert argv is not None
        assert argv[0] == str(executable)

    def test_a_bare_codex_cli_does_not_resolve(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The `codex` CLI is NOT an ACP server; it must never be spawned as one.

        With only `codex` on PATH the ladder must come up empty, so the operator
        gets "no adapter found" rather than a session that burns a subscription
        turn and then times out on a handshake that was never going to arrive.
        """
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        _make_executable(bin_dir / "codex")
        monkeypatch.setenv("PATH", str(bin_dir))
        assert codex.resolve_argv() is None

    def test_missing_adapter_message_warns_about_the_cli(self) -> None:
        message = codex.missing_adapter_message()
        assert "codex-acp" in message
        assert "does not serve ACP" in message
        assert "CODEX_ACP_BIN" in message

    @_POSIX_EXEC_PATHS_ONLY
    def test_an_eol_node_tree_is_skipped_for_a_supported_one(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Prefer a supported Node install over an EOL one holding the same package.

        MEASURED on a host with both: codex-acp 1.4.0 under Node 16 dies with
        ``TypeError: Writable.toWeb is not a function`` (that API is Node 17+) and
        answers no ACP request, while the same version under Node 24 starts
        cleanly. The adapters declare no ``engines``, so npm installs under an EOL
        Node without complaint and only this ordering keeps the session off it.

        The EOL copy is injected through ``mise which`` ON PURPOSE. That rung is
        tried FIRST and honours mise's ACTIVE node, so it is the one path where an
        EOL candidate outranks a supported one — a version that merely sits later
        on ``PATH`` is already beaten by ordering, so asserting against that would
        pass with the floor removed and prove nothing.
        """
        mise = tmp_path / "mise"
        monkeypatch.setenv("MISE_DATA_DIR", str(mise))
        installs = mise / "installs" / "node"
        for version in ("16.20.2", "24.18.0"):
            bin_dir = installs / version / "bin"
            bin_dir.mkdir(parents=True)
            _make_executable(bin_dir / "node")
            _make_executable(bin_dir / codex.CODEX_ACP_BIN)

        eol = installs / "16.20.2" / "bin" / codex.CODEX_ACP_BIN
        monkeypatch.setattr("kiro_crew.acp.client._mise_which", lambda _name: str(eol))

        argv = codex.resolve_argv()
        assert argv, "a supported install exists and must be found"
        assert not any("16.20.2" in part for part in argv), argv
        assert any("24.18.0" in part for part in argv), argv

    @_POSIX_EXEC_PATHS_ONLY
    def test_a_shim_loses_to_a_concrete_supported_install(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A shim resolves to mise's ACTIVE node, which may be the EOL one.

        Rejecting only the EOL pairing is not enough — measured, the ladder then
        fell through to the shim and re-acquired the same broken runtime.
        """
        mise = tmp_path / "mise"
        monkeypatch.setenv("MISE_DATA_DIR", str(mise))
        shims = mise / "shims"
        shims.mkdir(parents=True)
        _make_executable(shims / codex.CODEX_ACP_BIN)
        bin_dir = mise / "installs" / "node" / "24.18.0" / "bin"
        bin_dir.mkdir(parents=True)
        _make_executable(bin_dir / "node")
        _make_executable(bin_dir / codex.CODEX_ACP_BIN)

        argv = codex.resolve_argv()
        assert argv
        assert not any("shims" in part for part in argv), argv


class TestResolutionCaching:
    def test_success_is_memoised(self, tmp_path: Path) -> None:
        exe = _make_executable(tmp_path / "codex-acp")
        os.environ["CODEX_ACP_BIN"] = str(exe)
        first = codex.resolve_argv_cached()
        del os.environ["CODEX_ACP_BIN"]
        assert codex.resolve_argv_cached() == first, "success must be cached"

    def test_failure_is_not_memoised(self, tmp_path: Path) -> None:
        """Installing the adapter must not require a gateway restart."""
        assert codex.resolve_argv_cached() is None
        exe = _make_executable(tmp_path / "codex-acp")
        os.environ["CODEX_ACP_BIN"] = str(exe)
        assert codex.resolve_argv_cached() == [str(exe)]


class TestManagedMcpInjection:
    """codex-acp reads no Kiro Crew agent config, so the servers must be injected.

    Without them a Codex session has no cron, no memory and no core tools — the
    crew is present but inert.
    """

    def test_always_on_managed_servers_are_present(self) -> None:
        """cron and core are the always-on pair; the other two are conditional.

        This asserted ``kirocrew-computer`` was present too, which pinned a BUG:
        the entry carries a ``spec_gate`` that keeps it out of a kiro spec unless
        the host is macOS AND the keystone enable is on, and the shaper ignored it.
        Delivering it regardless spawns the desktop-automation shim for a feature
        that is off or unsupported. Gated membership is asserted separately below.
        """
        names = [entry["name"] for entry in codex.reshape_managed_servers()]
        assert "kirocrew-core" in names
        assert "kirocrew-cron" in names

    def test_a_gated_off_server_is_withheld(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """``spec_gate`` decides delivery, exactly as it decides kiro spec emission."""
        monkeypatch.setattr(
            "kiro_crew.agent._gated_off_servers",
            lambda: frozenset({"kirocrew-computer"}),
            raising=True,
        )
        names = [entry["name"] for entry in codex.reshape_managed_servers()]
        assert "kirocrew-computer" not in names
        assert "kirocrew-core" in names

    def test_an_opt_in_server_is_never_delivered(self) -> None:
        """``kirocrew-dashboard`` is an ASSIGNABLE set, not an always-on capability.

        It writes the operator's session layout. The loops that emit kiro specs
        skip it unless an agent was granted it, so delivering it to every spec
        adapter session would be a privilege grant nobody made.
        """
        names = [entry["name"] for entry in codex.reshape_managed_servers()]
        assert "kirocrew-dashboard" not in names

    def test_computer_carries_no_auto_approve(self) -> None:
        """An autoApprove key would be a complete gate bypass.

        kiro-cli approves an autoApproved MCP tool locally and emits no permission
        request, so hooks.on_tool_call never runs for it. For a tool that can click
        in an already-authenticated application that is the whole gate.
        """
        for entry in codex.reshape_managed_servers():
            assert "autoApprove" not in entry

    def test_entries_carry_only_spec_accepted_keys(self) -> None:
        """A strict deserializer rejects the WHOLE session/new, not one entry."""
        for entry in codex.reshape_managed_servers():
            assert set(entry) <= codex.SPEC_STDIO_SERVER_KEYS

    def test_no_type_key(self) -> None:
        """``type`` tags the http/sse variants; a stdio entry must omit it."""
        for entry in codex.reshape_managed_servers():
            assert "type" not in entry

    def test_env_is_present_and_empty_on_a_default_install(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``McpServerStdio`` REQUIRES env, so it is emitted even when empty.

        This asserted the opposite — that the key was omitted, "matching kiro's own
        pruning". That reasoning does not transfer: kiro reads a spec file it also
        wrote, while a spec adapter deserializes a strict schema where ``env`` is
        required. Omitting it fails the whole ``session/new`` on a DEFAULT install,
        which is the common case, and nothing caught it because the shaper had no
        callers.
        """
        monkeypatch.setattr("kiro_crew.agent._managed_mcp_env", lambda: {}, raising=True)
        for entry in codex.reshape_managed_servers():
            assert entry["env"] == []
            assert spec_servers.entry_is_spec_legal(entry)

    def test_override_home_is_pinned_as_env_pairs(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A child does not inherit KIROCREW_HOME; the spec env is the only channel.

        Without the pin the gateway and its own shims read different data homes,
        which is silent and self-contradictory rather than merely wrong.
        """
        monkeypatch.setattr(
            "kiro_crew.agent._managed_mcp_env",
            lambda: {"KIROCREW_HOME": "/custom/home"},
            raising=True,
        )
        for entry in codex.reshape_managed_servers():
            assert {"name": "KIROCREW_HOME", "value": "/custom/home"} in entry["env"]

    def test_a_failing_invocation_skips_only_that_server(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """One broken resolution must not take the whole session down."""

        def _boom() -> tuple[str, list[str]]:
            raise RuntimeError("nope")

        from kiro_crew import agent as agent_module

        patched = dict(agent_module._MANAGED_MCP_SERVERS)
        patched["kirocrew-core"] = {"invocation_fn": _boom}
        monkeypatch.setattr(agent_module, "_MANAGED_MCP_SERVERS", patched)

        names = [entry["name"] for entry in codex.reshape_managed_servers()]
        assert "kirocrew-core" not in names
        assert "kirocrew-cron" in names


class TestServerNameSafety:
    def test_a_legal_name_is_unchanged(self) -> None:
        assert codex.safe_server_name("my-server_1", set()) == "my-server_1"

    def test_managed_names_are_reserved_so_impersonation_fails(self) -> None:
        """A configured 'kirocrew core' must not become 'kirocrew-core'.

        Sanitising onto a managed name would let a user-configured server inherit
        whatever trust the declared name carries.
        """
        taken = set(codex.reserved_managed_names())
        result = codex.safe_server_name("kirocrew core", taken)
        assert result != "kirocrew-core"
        assert result not in taken

    def test_reservation_reads_the_real_managed_table(self) -> None:
        """Fails closed: an empty reserved set is the dangerous direction."""
        reserved = codex.reserved_managed_names()
        assert {"kirocrew-core", "kirocrew-cron", "kirocrew-computer"} <= reserved

    def test_a_name_with_nothing_salvageable_gets_a_digest(self) -> None:
        result = codex.safe_server_name("!!!", set())
        assert codex._SAFE_NAME_RE.fullmatch(result)

    def test_distinct_names_that_sanitise_alike_stay_distinct(self) -> None:
        first = codex.safe_server_name("a b", set())
        second = codex.safe_server_name("a!b", {first})
        assert first != second


class TestSessionServerMerge:
    def test_broker_stub_wins_on_a_name_collision(self) -> None:
        """The stub is the addressing layer MCP Apps callbacks route through.

        Preferring the direct entry would silently unroute those callbacks, and
        two elements sharing a name is undefined in the ACP schema.
        """
        managed = codex.reshape_managed_servers()
        stub = {"name": "kirocrew-core", "command": "gatewayd", "args": []}
        merged = codex.merge_session_servers(managed, [stub])
        core = [e for e in merged if e["name"] == "kirocrew-core"]
        assert len(core) == 1
        assert core[0]["command"] == "gatewayd"

    def test_stub_passthrough_keys_are_stripped(self) -> None:
        stub = {
            "name": "extra",
            "command": "x",
            "args": [],
            "autoApprove": ["a"],
            "timeout": 5,
        }
        merged = codex.merge_session_servers([], [stub])
        assert set(merged[0]) <= codex.SPEC_STDIO_SERVER_KEYS

    def test_entries_without_a_usable_name_are_dropped(self) -> None:
        assert codex.merge_session_servers([], [{"command": "x"}]) == []
        assert codex.merge_session_servers([], [{"name": "", "command": "x"}]) == []


class TestAuth:
    def test_absent_auth_json_reads_unknown_not_negative(self, codex_home: Path) -> None:
        """Absence must never be reported as "not signed in".

        A real turn has been driven through AcpClient on a host where auth.json
        did not exist and no key-shaped environment override was set, so the
        adapter reached a credential by some other channel. A boolean here made
        the doctor tell that operator to run `codex login` and appended a
        spurious issue.
        """
        assert codex.signin_state() == codex.SIGNIN_UNKNOWN

    def test_present_auth_json_reads_present(self, codex_home: Path) -> None:
        (codex_home / "auth.json").write_text("{}")
        assert codex.signin_state() == codex.SIGNIN_PRESENT

    def test_signin_state_is_never_a_negative_verdict(self, codex_home: Path) -> None:
        """Only two readings exist, so no caller can branch on "definitely not"."""
        assert codex.signin_state() in {codex.SIGNIN_PRESENT, codex.SIGNIN_UNKNOWN}
        (codex_home / "auth.json").write_text("{}")
        assert codex.signin_state() in {codex.SIGNIN_PRESENT, codex.SIGNIN_UNKNOWN}

    def test_auth_path_follows_codex_home(self, codex_home: Path) -> None:
        assert codex.auth_json_path() == codex_home / "auth.json"

    def test_message_names_codex_login_not_kiro(self) -> None:
        """A Codex-only host must never be told to run kiro-cli login."""
        message = codex.not_signed_in_message()
        assert "codex login" in message
        assert "kiro-cli" not in message

    def test_message_covers_the_headless_case(self) -> None:
        message = codex.not_signed_in_message()
        assert "headless" in message.lower()

    def test_no_api_key_path_exists(self) -> None:
        """Subscription OAuth only: there must be no key-shaped surface.

        The whole point of this backend is reusing an existing ChatGPT
        subscription, so an API-key path would be a different feature with a
        different threat model.
        """
        source = Path(codex.__file__).read_text()
        assert "API_KEY" not in source
        assert "api_key" not in source
