"""MCP config files saved with a UTF-8 byte-order mark still load.

Windows editors save UTF-8 "with BOM" by default, and ``json.loads`` refuses a
leading U+FEFF ("Unexpected UTF-8 BOM"). These tests write each user-owned MCP
config with that mark and drive the readers that parse it.
"""

from __future__ import annotations

import codecs
import json
import logging
from pathlib import Path
from typing import Any

import pytest

from kiro_crew.user_json import (
    load_mcp_servers,
    load_user_json_object,
    loads_mcp_config,
    loads_user_json,
    strip_utf8_bom,
)

_BOM = codecs.BOM_UTF8


def _write_bom_json(path: Path, doc: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_BOM + json.dumps(doc).encode("utf-8"))


class TestLoadsUserJson:
    def test_text_with_bom_parses(self) -> None:
        assert loads_user_json('\ufeff{"a": 1}') == {"a": 1}

    def test_plain_text_is_unchanged(self) -> None:
        assert loads_user_json('{"a": "\ufeff"}') == {"a": "\ufeff"}

    def test_only_one_mark_is_dropped(self) -> None:
        assert strip_utf8_bom("\ufeff\ufeffx") == "\ufeffx"


class TestLoadsMcpConfig:
    def test_bom_object_parses(self) -> None:
        assert loads_mcp_config('\ufeff{"mcpServers": {"a": {}}}') == {"mcpServers": {"a": {}}}

    @pytest.mark.parametrize(
        "text",
        ["\ufeff[]", '"x"', '{"mcpServers": []}', '\ufeff{"mcpServers": "x"}'],
        ids=["bom_list_root", "scalar_root", "list_servers", "bom_scalar_servers"],
    )
    def test_a_shape_readers_cannot_index_reads_as_unparseable(self, text: str) -> None:
        # The readers' existing "cannot parse" branch then handles it, rather
        # than a ``.get`` on a list raising further down.
        with pytest.raises(json.JSONDecodeError):
            loads_mcp_config(text)


class TestLoadUserJsonObject:
    def test_missing_file_is_empty(self, tmp_path: Path) -> None:
        assert load_user_json_object(tmp_path / "absent.json") == {}

    def test_malformed_file_is_empty(self, tmp_path: Path) -> None:
        path = tmp_path / "mcp.json"
        path.write_bytes(_BOM + b"{not json")
        assert load_user_json_object(path) == {}

    def test_non_object_root_is_empty(self, tmp_path: Path) -> None:
        path = tmp_path / "mcp.json"
        _write_bom_json(path, ["a"])
        assert load_user_json_object(path) == {}


class TestLoadMcpServers:
    def test_bom_file_yields_its_servers(self, tmp_path: Path) -> None:
        path = tmp_path / "mcp.json"
        _write_bom_json(path, {"mcpServers": {"a": {"command": "a"}}})
        assert load_mcp_servers(path) == {"a": {"command": "a"}}

    def test_non_object_servers_contribute_nothing(self, tmp_path: Path) -> None:
        # Materialization iterates the map; a list here must not abort it.
        path = tmp_path / "mcp.json"
        _write_bom_json(path, {"mcpServers": [{"command": "a"}]})
        assert load_mcp_servers(path) == {}


class TestWritersPreserveAWrongShape:
    def test_the_cc_sidecar_writer_never_replaces_a_file_it_cannot_index(
        self, tmp_path: Path
    ) -> None:
        # A parseable file with a list ``mcpServers`` must not be read as empty
        # and overwritten with only the new entry.
        from kiro_crew import mcp_discovery

        path = tmp_path / ".mcp.json"
        raw = _BOM + json.dumps({"mcpServers": [{"command": "x"}], "keep": 1}).encode("utf-8")
        path.write_bytes(raw)
        server = mcp_discovery.McpServerInfo(name="n", command="c", args=[])
        # Refused with a warning, not crashed on: the writer skips the sidecar.
        assert mcp_discovery.register_servers_for_cc([server], mcp_json_path=path) is False
        assert path.read_bytes() == raw


@pytest.fixture()
def bridges_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    from kiro_crew.apps import bridges

    agents = tmp_path / ".kiro" / "agents"
    settings = tmp_path / ".kiro" / "settings"
    agents.mkdir(parents=True)
    settings.mkdir(parents=True)
    monkeypatch.setattr(bridges, "_mcp_json_path", lambda: agents / "kirocrew.json")
    monkeypatch.setattr(bridges, "_LEGACY_SHARED_MCP_PATH", settings / "mcp.json")
    return tmp_path


class TestBridgesSetup:
    def test_legacy_scrub_reads_a_bom_file_and_writes_it_back_plain(
        self, bridges_home: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        from kiro_crew.apps import bridges

        legacy = bridges._LEGACY_SHARED_MCP_PATH
        _write_bom_json(legacy, {"mcpServers": {"tunnel:srv": {"command": "x"}, "keep": {}}})

        with caplog.at_level(logging.WARNING, logger="kiro_crew.apps.bridges"):
            assert bridges._scrub_legacy_shared_mcp("tunnel") == 1

        assert "Failed to scrub" not in caplog.text
        raw = legacy.read_bytes()
        assert not raw.startswith(_BOM)
        assert json.loads(raw) == {"mcpServers": {"keep": {}}}

    def test_registration_reads_a_bom_agent_config(self, bridges_home: Path) -> None:
        from kiro_crew.apps import bridges

        _write_bom_json(bridges._mcp_json_path(), {"mcpServers": {"user": {"command": "u"}}})

        # strict is the read-modify-write mode registration uses: an unreadable
        # file aborts the write, so a BOM here would stop app setup.
        cfg = bridges._read_mcp_json_unlocked(strict=True)
        assert cfg["mcpServers"] == {"user": {"command": "u"}}

    def test_a_bom_list_root_agent_config_is_refused_not_crashed_on(
        self, bridges_home: Path
    ) -> None:
        from kiro_crew.apps import bridges

        _write_bom_json(bridges._mcp_json_path(), [])
        assert bridges._read_mcp_json_unlocked() == {}
        with pytest.raises(json.JSONDecodeError):
            bridges._read_mcp_json_unlocked(strict=True)

    def test_an_installed_app_agent_spec_with_bom_keeps_its_user_edits(
        self, tmp_path: Path
    ) -> None:
        from kiro_crew.apps import bridges

        path = tmp_path / "app--agent.json"
        _write_bom_json(path, {"name": "app--agent", "model": "user-pick"})
        assert bridges._read_agent_config(path) == {"name": "app--agent", "model": "user-pick"}

    def test_global_policy_specs_read_a_bom_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew.apps import bridges

        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        _write_bom_json(
            tmp_path / ".kiro" / "settings" / "mcp.json",
            {"mcpServers": {"g": {"command": "g"}}},
        )
        assert bridges._global_mcp_specs() == {"g": {"command": "g"}}


class TestChatStartLoading:
    def test_agent_materialization_loader_reads_a_bom_mcp_json(self, tmp_path: Path) -> None:
        from kiro_crew.user_json import load_user_json_object

        path = tmp_path / "mcp.json"
        _write_bom_json(path, {"mcpServers": {"s": {"command": "s"}}})
        assert load_user_json_object(path) == {"mcpServers": {"s": {"command": "s"}}}

    def test_agent_loader_keeps_judging_crew_config_like_the_config_loader(
        self, tmp_path: Path
    ) -> None:
        # ``KiroCrewConfig.load`` accepts a BOM'd ``config.json``, so the agent's
        # own loader must too: if the two judged one file differently, the spec
        # builder would treat a config the gateway loaded as absent.
        from kiro_crew import agent

        path = tmp_path / "config.json"
        _write_bom_json(path, {"model": "m"})
        assert agent._load_json(path) == {"model": "m"}

    def test_doctor_reads_a_bom_agent_config(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from kiro_crew import cli_doctor

        class _Stop(Exception):
            pass

        def _stop(**_kw: object) -> None:
            raise _Stop

        # Stop right after the parse verdict: the rest of doctor probes servers.
        monkeypatch.setattr(cli_doctor._agent, "_decline_shared_agent_home", _stop)
        path = tmp_path / "kirocrew.json"
        # Valid JSON that is not an object: doctor names it only when the parse
        # got past the byte-order mark.
        _write_bom_json(path, ["not", "an", "object"])
        with pytest.raises(_Stop):
            cli_doctor._doctor_mcp_tools(path, [])
        assert "agent spec is not a JSON object" in capsys.readouterr().out

    def test_json_agent_spec_with_bom_parses(self) -> None:
        from kiro_crew.agent_spec_format import parse_agent_spec_bytes

        spec = parse_agent_spec_bytes(_BOM + b'{"name": "a"}', "a.json")
        assert spec == {"name": "a"}

    def test_discovery_reads_a_bom_mcp_json(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew import mcp_discovery

        path = tmp_path / "crew" / "mcp.json"
        _write_bom_json(path, {"mcpServers": {"d": {"command": "d"}}})
        monkeypatch.setattr(mcp_discovery, "_MCP_JSON_PATHS", (path,))
        monkeypatch.setattr(mcp_discovery, "_extra_scope_sources", lambda: [])

        merged = mcp_discovery._load_mcp_json()
        assert merged == {"d": {"command": "d"}}


# ── Keystone deny rules and script hooks (``denied_commands.json``, ``hooks.json``) ──
#
# Windows PowerShell 5.1 ``Set-Content -Encoding UTF8`` writes the mark and a CRLF.

_PS51_DENIED = _BOM + b'{"user_added":[{"id":"u1","pattern":"git push --force"}]}\r\n'


@pytest.fixture
def denied_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    from kiro_crew.config import loader
    from kiro_crew.dashboard.handlers import security as security_handlers

    path = tmp_path / "denied_commands.json"
    monkeypatch.setattr(loader, "denied_commands_path", lambda: path)
    monkeypatch.setattr(security_handlers, "denied_commands_path", lambda: path)
    return path


class TestDeniedCommandsKeystone:
    @pytest.mark.parametrize(
        "raw",
        [_PS51_DENIED, _PS51_DENIED[len(_BOM) :]],
        ids=["utf8_bom_crlf", "plain_utf8_control"],
    )
    def test_the_user_added_rule_reaches_the_gate(self, denied_file: Path, raw: bytes) -> None:
        from kiro_crew.hooks import hooks_config_from_config_dict

        denied_file.write_bytes(raw)
        config = hooks_config_from_config_dict({})
        assert [p.id for p in config.denied_commands_user_added] == ["u1"]

    @pytest.mark.parametrize(
        "raw",
        [b"{not json", "{}".encode("utf-16"), b"[1, 2]"],
        ids=["corrupt_json", "utf16", "non_object_root"],
    )
    def test_an_unusable_file_keeps_built_ins_and_says_so(
        self, denied_file: Path, raw: bytes, caplog: pytest.LogCaptureFixture
    ) -> None:
        from kiro_crew.hooks import load_denied_commands_state

        denied_file.write_bytes(raw)
        with caplog.at_level(logging.WARNING):
            assert load_denied_commands_state() == {}
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert any("denied_commands.json" in r.getMessage() for r in warnings)

    def test_a_missing_file_is_no_opt_out_without_a_warning(
        self, denied_file: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        from kiro_crew.hooks import load_denied_commands_state

        with caplog.at_level(logging.WARNING):
            assert load_denied_commands_state() == {}
        assert not [r for r in caplog.records if "denied_commands.json" in r.getMessage()]

    def test_the_settings_page_reads_and_edits_a_bom_file(self, denied_file: Path) -> None:
        from kiro_crew.dashboard.handlers import security as security_handlers

        denied_file.write_bytes(_PS51_DENIED)
        expected = {"user_added": [{"id": "u1", "pattern": "git push --force"}]}
        assert security_handlers._read_denied_data() == expected
        assert security_handlers._read_denied_strict() == expected

    def test_a_corrupt_file_still_refuses_an_edit(self, denied_file: Path) -> None:
        from kiro_crew.dashboard.handlers import security as security_handlers
        from kiro_crew.dashboard.handlers.security_audit import ConfigCorruptError

        denied_file.write_bytes(_BOM + b"{not json")
        with pytest.raises(ConfigCorruptError):
            security_handlers._read_denied_strict()


@pytest.fixture
def saved_hooks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A ``hooks.json`` holding one PreToolUse hook, re-saved with a leading mark."""
    import kiro_crew.hooks as hooks_mod
    from kiro_crew.config import loader
    from kiro_crew.hooks import HOOK_EVENT_PRE_TOOL_USE, ScriptHook, ScriptHookStore

    monkeypatch.setattr(loader, "config_dir", lambda: tmp_path)
    monkeypatch.setattr(hooks_mod, "_global_script_hook_store", None)
    monkeypatch.setattr(hooks_mod, "get_global_hook_store", lambda: None)
    store = ScriptHookStore(config_dir=tmp_path)
    store._hooks = {
        "h1": ScriptHook(id="h1", event=HOOK_EVENT_PRE_TOOL_USE, matcher="web_fetch", command="s")
    }
    store._save()
    path = tmp_path / "hooks.json"
    path.write_bytes(_BOM + path.read_bytes())
    return path


class TestScriptHooksFile:
    def test_the_gateway_store_loads_a_bom_file(self, saved_hooks: Path) -> None:
        from kiro_crew.hooks import ScriptHookStore

        store = ScriptHookStore(config_dir=saved_hooks.parent)
        assert [h.id for h in store.list_all()] == ["h1"]

    def test_a_hook_edit_saves_over_a_bom_file(self, saved_hooks: Path) -> None:
        from kiro_crew.hooks import ScriptHookStore

        store = ScriptHookStore(config_dir=saved_hooks.parent)
        assert store.toggle("h1") is not None
        reread = ScriptHookStore(config_dir=saved_hooks.parent)
        assert [h.enabled for h in reread.list_all()] == [False]
        assert not saved_hooks.read_bytes().startswith(_BOM)

    def test_the_standalone_gate_store_reads_a_bom_file(self, saved_hooks: Path) -> None:
        import kiro_crew.hooks as hooks_mod

        store = hooks_mod.persisted_hook_store()
        assert [h.id for h in store.list_all()] == ["h1"]

    def test_the_webhook_registration_count_reads_a_bom_file(self, tmp_path: Path) -> None:
        from kiro_crew import webhooks

        path = tmp_path / "hooks.json"
        path.write_bytes(_BOM + b'{"r1": {"context_summary": "a"}, "hooks": []}')
        assert webhooks.registered_context_count(path) == (1, True)
        path.write_bytes(_BOM + b"{not json")
        assert webhooks.registered_context_count(path) == (0, False)

    def test_the_webhook_intake_reads_a_bom_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew.dashboard.handlers import hooks as hooks_handlers

        path = tmp_path / "hooks.json"
        path.write_bytes(_BOM + b'{"r1": {"context_summary": "a"}, "hooks": []}')
        monkeypatch.setattr(hooks_handlers, "_hook_store_path", lambda: path)
        assert list(hooks_handlers._read_hook_registrations()) == ["r1"]
        path.write_bytes(_BOM + b"{not json")
        assert hooks_handlers._read_json_file(path) is None

    def test_an_imported_bom_file_has_its_command_hooks_switched_off(
        self, saved_hooks: Path
    ) -> None:
        # The import sanitizer must read the file the way the store does: a BOM
        # file the store now loads must not slip past it with its hooks still on.
        from kiro_crew.hooks import ScriptHookStore
        from kiro_crew.portability_vetting import _disable_imported_script_hooks

        assert saved_hooks.read_bytes().startswith(_BOM)
        assert len(_disable_imported_script_hooks(saved_hooks)) == 1
        store = ScriptHookStore(config_dir=saved_hooks.parent)
        assert [h.enabled for h in store.list_all()] == [False]

    def test_a_corrupt_bom_file_still_fails_the_standalone_gate_closed(
        self, saved_hooks: Path
    ) -> None:
        import kiro_crew.hooks as hooks_mod

        saved_hooks.write_bytes(_BOM + b"{not json")
        with pytest.raises(ValueError):
            hooks_mod.persisted_hook_store()


# ── Governance policy files (``security_policy.json``) ──


class TestGovernancePolicyReader:
    _DOC = b'{"version": 1, "boot": {"allow_terminal": false}}'

    @pytest.mark.parametrize("raw", [_BOM + _DOC, _DOC], ids=["utf8_bom", "plain_utf8_control"])
    def test_the_policy_parses(self, tmp_path: Path, raw: bytes) -> None:
        from kiro_crew.platform.governance import _read_json_file

        path = tmp_path / "security_policy.json"
        path.write_bytes(raw)
        assert _read_json_file(path) == {"version": 1, "boot": {"allow_terminal": False}}

    @pytest.mark.parametrize(
        "raw",
        [_DOC.decode().encode("utf-16"), _BOM + b"{not json", _BOM + _BOM + _DOC],
        ids=["utf16", "bom_corrupt_json", "two_marks"],
    )
    def test_anything_else_still_raises(self, tmp_path: Path, raw: bytes) -> None:
        from kiro_crew.platform.governance import _read_json_file

        path = tmp_path / "security_policy.json"
        path.write_bytes(raw)
        with pytest.raises(ValueError):
            _read_json_file(path)


# ── App admission policy (``app_admission.json``) ──


class TestAppAdmissionPolicyReader:
    _DOC = b'{"mode": "enforce", "banned": ["bad-app"], "approved": ["good-app"], "require_signature": false}'

    @pytest.mark.parametrize("raw", [_BOM + _DOC, _DOC], ids=["utf8_bom", "plain_utf8_control"])
    def test_the_policy_loads_as_authored(self, tmp_path: Path, monkeypatch, raw: bytes) -> None:
        from kiro_crew.apps import admission

        monkeypatch.setattr(admission, "config_dir", lambda: tmp_path)
        (tmp_path / "app_admission.json").write_bytes(raw)
        assert admission.load_app_admission_policy() == admission.AppAdmissionPolicy(
            mode="enforce", banned=["bad-app"], approved=["good-app"], require_signature=False
        )
        assert admission.app_admission_denied("good-app", check_version=False) is None
        assert admission.app_admission_denied("other-app", check_version=False) is not None

    @pytest.mark.parametrize(
        "raw",
        [_DOC.decode().encode("utf-16"), _BOM + b"{not json", _BOM + _BOM + _DOC],
        ids=["utf16", "bom_corrupt_json", "two_marks"],
    )
    def test_anything_else_still_fails_closed(
        self, tmp_path: Path, monkeypatch, raw: bytes
    ) -> None:
        from kiro_crew.apps import admission

        monkeypatch.setattr(admission, "config_dir", lambda: tmp_path)
        (tmp_path / "app_admission.json").write_bytes(raw)
        assert admission.load_app_admission_policy() == admission._fail_closed_policy()


# ── Fleet admission policy and governance profiles ──


class TestAdmissionPolicyReader:
    _DOC = b'{"mode": "enforce", "approved": ["good-plugin"]}'

    @pytest.mark.parametrize("raw", [_BOM + _DOC, _DOC], ids=["utf8_bom", "plain_utf8_control"])
    def test_the_policy_loads_as_authored(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, raw: bytes
    ) -> None:
        from kiro_crew.platform.admission import AdmissionPolicy, load_admission_policy

        path = tmp_path / "admission_policy.json"
        path.write_bytes(raw)
        monkeypatch.setenv("KIROCREW_ADMISSION_POLICY", str(path))
        assert load_admission_policy() == AdmissionPolicy.from_dict(json.loads(self._DOC))

    @pytest.mark.parametrize(
        "raw",
        [_DOC.decode().encode("utf-16"), _BOM + b"{not json", _BOM + _BOM + _DOC],
        ids=["utf16", "bom_corrupt_json", "two_marks"],
    )
    def test_anything_else_still_fails_closed(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, raw: bytes
    ) -> None:
        from kiro_crew.platform.admission import MODE_ENFORCE, load_admission_policy

        path = tmp_path / "admission_policy.json"
        path.write_bytes(raw)
        monkeypatch.setenv("KIROCREW_ADMISSION_POLICY", str(path))
        policy = load_admission_policy()
        assert (policy.mode, policy.approved, policy.require_signature) == (MODE_ENFORCE, [], True)


class TestGovernanceProfileReader:
    _DOC = b'{"name": "harness", "tools": {"mode": "allow", "allow": ["read"]}}'

    @staticmethod
    def _store(monkeypatch: pytest.MonkeyPatch, directory: Path, raw: bytes) -> Any:
        from kiro_crew.platform import governance_profiles

        (directory / "harness.json").write_bytes(raw)
        monkeypatch.setattr(governance_profiles, "_PROFILES_DIR", directory)
        return governance_profiles.ProfileStore()

    @pytest.mark.parametrize("raw", [_BOM + _DOC, _DOC], ids=["utf8_bom", "plain_utf8_control"])
    def test_the_profile_parses(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, raw: bytes
    ) -> None:
        from kiro_crew.platform.governance import parse_profile

        store = self._store(monkeypatch, tmp_path, raw)
        assert store.get("harness") == parse_profile(json.loads(self._DOC))
        assert store.unrecoverable_files() == ()
        assert store.snapshot().fallback_profiles == frozenset()

    @pytest.mark.parametrize(
        "raw",
        [_DOC.decode().encode("utf-16"), _BOM + b"{not json", _BOM + _BOM + _DOC],
        ids=["utf16", "bom_corrupt_json", "two_marks"],
    )
    def test_anything_else_still_falls_back_to_deny_all(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, raw: bytes
    ) -> None:
        store = self._store(monkeypatch, tmp_path, raw)
        assert store.resolved()
        assert store.snapshot().fallback_profiles == frozenset({"harness"})
