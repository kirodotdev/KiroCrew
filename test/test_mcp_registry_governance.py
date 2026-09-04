"""Enterprise MCP registry governance: spec markers, detection, doctor section.

An enterprise Kiro profile with an MCP Registry URL puts kiro-cli in `registry`
access mode, where it connects ONLY to `mcpServers` entries carrying
``"type": "registry"`` that resolve to a catalog entry of the same name. Outside
that mode the filter inverts and marked entries are the dropped ones, so the
marker has to appear exactly when the operator declares the account governed and
disappear when they do not.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from kiro_crew import agent as agent_mod
from kiro_crew import cli_doctor
from kiro_crew import kiro_cli as kiro_cli_mod
from kiro_crew.config import KiroCrewConfig
from kiro_crew.kiro_cli import (
    kiro_cli_state_dbs,
    mcp_governance_may_apply,
    signed_in_via_idc,
)

MANAGED = ("kirocrew-core", "kirocrew-cron", "kirocrew-computer")

# ``kirocrew-computer`` carries a ``spec_gate`` and is therefore absent from an
# emitted spec on any non-macOS host (and on macOS with the keystone off) -- see
# ``agent._computer_use_spec_gate``. These tests are about the REGISTRY MARKER, not
# about that gate, so they pin it OPEN with an empty snapshot: the marker rule must
# hold for every managed server the spec actually carries, on every CI platform.
NO_GATES: frozenset[str] = frozenset()


def _write_config(home: Path, *, registry_mode: bool | None) -> Path:
    path = home / "config.json"
    agent: dict[str, object] = {}
    if registry_mode is not None:
        agent["mcp_registry_mode"] = registry_mode
    path.write_text(json.dumps({"agent": agent}), encoding="utf-8")
    return path


def _point_config_at(tmp_path: Path, monkeypatch) -> None:
    """Make both config readers resolve to tmp_path.

    ``_mcp_registry_mode`` reads the EFFECTIVE config (base + local overlay) via
    the loader, which resolves from ``KIROCREW_HOME``; the fallback path reads the
    base file directly. Point both, and drop the load cache so a per-test config
    is actually seen.
    """
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    monkeypatch.setattr(agent_mod, "_mc_config_path", lambda: tmp_path / "config.json")
    if hasattr(KiroCrewConfig.load, "cache_clear"):
        KiroCrewConfig.load.cache_clear()


class TestRegistryModeDeclaration:
    def test_absent_key_is_not_registry_mode(self, tmp_path, monkeypatch):
        """A personal install must emit today's spec byte-for-byte."""
        _write_config(tmp_path, registry_mode=None)
        _point_config_at(tmp_path, monkeypatch)
        assert agent_mod._mcp_registry_mode() is False

    def test_declared_true_is_registry_mode(self, tmp_path, monkeypatch):
        _write_config(tmp_path, registry_mode=True)
        _point_config_at(tmp_path, monkeypatch)
        assert agent_mod._mcp_registry_mode() is True

    @pytest.mark.parametrize("junk", ["true", 1, [], {"a": 1}, None])
    def test_only_a_real_bool_counts(self, tmp_path, monkeypatch, junk):
        """A truthy string from a hand-edited config must not silently arm the
        marker: arming it wrongly breaks an ungoverned install outright. The
        loader's own coercion is strict the same way, so both paths agree."""
        (tmp_path / "config.json").write_text(
            json.dumps({"agent": {"mcp_registry_mode": junk}}), encoding="utf-8"
        )
        _point_config_at(tmp_path, monkeypatch)
        assert agent_mod._mcp_registry_mode() is False

    def test_missing_config_file_is_not_registry_mode(self, tmp_path, monkeypatch):
        _point_config_at(tmp_path, monkeypatch)
        monkeypatch.setattr(agent_mod, "_mc_config_path", lambda: tmp_path / "nope.json")
        assert agent_mod._mcp_registry_mode() is False

    def test_config_loader_round_trips_the_field(self, tmp_path, monkeypatch):
        _write_config(tmp_path, registry_mode=True)
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        KiroCrewConfig.load.cache_clear() if hasattr(KiroCrewConfig.load, "cache_clear") else None
        cfg = KiroCrewConfig.load()
        assert cfg.agent.mcp_registry_mode is True

    def test_local_overlay_declaration_is_honoured(self, tmp_path, monkeypatch):
        """`kirocrew config set --local` writes config.local.json, which deep-merges
        OVER config.json. Reading only the base file would ignore a declaration made
        the way the CLI advertises as upgrade-durable, emit no marker, and reproduce
        the silent drop this change exists to prevent."""
        _write_config(tmp_path, registry_mode=None)
        (tmp_path / "config.local.json").write_text(
            json.dumps({"agent": {"mcp_registry_mode": True}}), encoding="utf-8"
        )
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        monkeypatch.setattr(agent_mod, "_mc_config_path", lambda: tmp_path / "config.json")
        if hasattr(KiroCrewConfig.load, "cache_clear"):
            KiroCrewConfig.load.cache_clear()
        assert agent_mod._mcp_registry_mode() is True

    def test_local_overlay_can_turn_the_declaration_off(self, tmp_path, monkeypatch):
        """Precedence runs both ways: an overlay that disables it must win over a
        base file that enables it, or a host cannot switch the marker off."""
        _write_config(tmp_path, registry_mode=True)
        (tmp_path / "config.local.json").write_text(
            json.dumps({"agent": {"mcp_registry_mode": False}}), encoding="utf-8"
        )
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        monkeypatch.setattr(agent_mod, "_mc_config_path", lambda: tmp_path / "config.json")
        if hasattr(KiroCrewConfig.load, "cache_clear"):
            KiroCrewConfig.load.cache_clear()
        assert agent_mod._mcp_registry_mode() is False


class TestIdentityProbeIsAudited:
    """The store holds live credential material, so the probe owes an SEL event.

    Registered in ``hooks._AUDIT_ONLY_READ_IDS`` and FAIL-CLOSED: no audit means
    no read, which costs a diagnostic hint rather than an unaudited credential
    access.
    """

    def test_read_id_is_registered(self):
        from kiro_crew import hooks

        assert kiro_cli_mod._IDC_PROBE_READ_ID in hooks._AUDIT_ONLY_READ_IDS

    def test_probe_emits_an_audit_event(self, tmp_path, monkeypatch):
        db = kiro_cli_state_dbs("linux", tmp_path, {})[0]
        TestIdcDetection._make_db(db, ("auth.idc.start-url",))
        seen: list[tuple[str, str]] = []

        def _spy(read_id: str, outcome: str) -> bool:
            seen.append((read_id, outcome))
            return True

        monkeypatch.setattr("kiro_crew.hooks.emit_internal_read_audit", _spy)
        assert signed_in_via_idc("linux", tmp_path, {}) is True
        assert seen and seen[0][0] == kiro_cli_mod._IDC_PROBE_READ_ID

    def test_probe_fails_closed_when_the_audit_is_unavailable(self, tmp_path, monkeypatch):
        """An unregistered or unavailable audit surface must stop the read, not
        merely log — otherwise the audit requirement is advisory."""
        db = kiro_cli_state_dbs("linux", tmp_path, {})[0]
        TestIdcDetection._make_db(db, ("auth.idc.start-url",))
        monkeypatch.setattr("kiro_crew.hooks.emit_internal_read_audit", lambda *_: False)
        assert signed_in_via_idc("linux", tmp_path, {}) is False


class TestFreshInstallMarker:
    def test_no_marker_outside_registry_mode(self, monkeypatch):
        monkeypatch.setattr(agent_mod, "_mcp_registry_mode", lambda: False)
        config = agent_mod.build_agent_config(gated_off=NO_GATES)
        for name in MANAGED:
            assert "type" not in config["mcpServers"][name], name

    def test_marker_on_every_managed_server_in_registry_mode(self, monkeypatch):
        monkeypatch.setattr(agent_mod, "_mcp_registry_mode", lambda: True)
        config = agent_mod.build_agent_config(gated_off=NO_GATES)
        for name in MANAGED:
            assert config["mcpServers"][name]["type"] == "registry", name

    def test_marker_does_not_replace_the_command(self, monkeypatch):
        """command/args stay so doctor's handshake probe, the CC sidecar sync and
        a later ungoverned refresh still describe a runnable server."""
        monkeypatch.setattr(agent_mod, "_mcp_registry_mode", lambda: True)
        entry = agent_mod.build_agent_config(gated_off=NO_GATES)["mcpServers"]["kirocrew-core"]
        assert entry["command"]
        assert entry["args"] == ["mcp-core"] or "mcp-core" in entry["args"]


class TestRefreshMarker:
    def test_refresh_adds_the_marker(self, monkeypatch):
        monkeypatch.setattr(agent_mod, "_mcp_registry_mode", lambda: True)
        config = {"mcpServers": {name: {"command": "x", "args": []} for name in MANAGED}}
        agent_mod._refresh_dynamic_fields(config, gated_off=NO_GATES)
        for name in MANAGED:
            assert config["mcpServers"][name]["type"] == "registry", name

    def test_refresh_removes_a_stale_marker(self, monkeypatch):
        """A host that leaves an enterprise account must stop shipping a marker
        that the inverse filter would now use to drop these servers."""
        monkeypatch.setattr(agent_mod, "_mcp_registry_mode", lambda: False)
        config = {
            "mcpServers": {
                name: {"command": "x", "args": [], "type": "registry"} for name in MANAGED
            }
        }
        agent_mod._refresh_dynamic_fields(config, gated_off=NO_GATES)
        for name in MANAGED:
            assert "type" not in config["mcpServers"][name], name

    def test_refresh_preserves_a_user_transport_hint(self, monkeypatch):
        """Only the registry marker is ours. A transport hint the user wrote is
        theirs, and kiro-cli tolerates it."""
        monkeypatch.setattr(agent_mod, "_mcp_registry_mode", lambda: False)
        config = {"mcpServers": {"kirocrew-core": {"command": "x", "args": [], "type": "stdio"}}}
        agent_mod._refresh_dynamic_fields(config, gated_off=NO_GATES)
        assert config["mcpServers"]["kirocrew-core"]["type"] == "stdio"


class TestIdcDetection:
    @staticmethod
    def _make_db(path: Path, keys: tuple[str, ...]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(path)
        con.execute("CREATE TABLE state (key TEXT PRIMARY KEY, value TEXT)")
        con.executemany("INSERT INTO state VALUES (?, ?)", [(k, "v") for k in keys])
        con.commit()
        con.close()

    def test_idc_rows_are_detected(self, tmp_path):
        db = kiro_cli_state_dbs("linux", tmp_path, {})[0]
        self._make_db(db, ("auth.idc.start-url", "auth.idc.region"))
        assert signed_in_via_idc("linux", tmp_path, {}) is True

    def test_builder_id_store_is_not_idc(self, tmp_path):
        db = kiro_cli_state_dbs("linux", tmp_path, {})[0]
        self._make_db(db, ("telemetryClientId", "migration.kiro.completed"))
        assert signed_in_via_idc("linux", tmp_path, {}) is False

    def test_absent_store_is_not_idc(self, tmp_path):
        assert signed_in_via_idc("linux", tmp_path, {}) is False

    def test_corrupt_store_is_not_idc(self, tmp_path):
        """"Cannot tell" must never render as an enterprise diagnosis."""
        db = kiro_cli_state_dbs("linux", tmp_path, {})[0]
        db.parent.mkdir(parents=True, exist_ok=True)
        db.write_bytes(b"not a database")
        assert signed_in_via_idc("linux", tmp_path, {}) is False

    def test_xdg_data_home_redirection_is_followed(self, tmp_path):
        moved = tmp_path / "elsewhere"
        db = kiro_cli_state_dbs("linux", tmp_path, {"XDG_DATA_HOME": str(moved)})[0]
        assert moved in db.parents
        self._make_db(db, ("auth.idc.region",))
        assert signed_in_via_idc("linux", tmp_path, {"XDG_DATA_HOME": str(moved)}) is True

    def test_rows_still_in_the_wal_are_seen(self, tmp_path):
        """A fresh Identity Center sign-in commits into `data.sqlite3-wal`. Opening
        with `immutable=1` would ignore the WAL and read the rows as absent, so a
        just-signed-in enterprise host would look personal and the whole governance
        diagnosis would go silent — the same silent-failure class this fixes."""
        db = kiro_cli_state_dbs("linux", tmp_path, {})[0]
        db.parent.mkdir(parents=True, exist_ok=True)
        writer = sqlite3.connect(db)
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("CREATE TABLE state (key TEXT PRIMARY KEY, value TEXT)")
        writer.execute("INSERT INTO state VALUES ('auth.idc.start-url', 'x')")
        writer.commit()
        # Keep the writer open and DO NOT checkpoint, so the row lives in the -wal
        # sidecar rather than the main database file.
        try:
            assert (db.parent / "data.sqlite3-wal").exists()
            assert signed_in_via_idc("linux", tmp_path, {}) is True
        finally:
            writer.close()

    def test_symlinked_store_is_refused(self, tmp_path):
        """Path defenses match the readiness probe's on the same file.

        Two redundant gates close this: the symlink check and the regular-file
        check (``lstat`` reports the link itself, not its target). Removing
        either alone leaves the other holding, so this asserts the behaviour
        rather than one gate — it fails only when both are gone.
        """
        real = tmp_path / "real.sqlite3"
        self._make_db(real, ("auth.idc.region",))
        db = kiro_cli_state_dbs("linux", tmp_path, {})[0]
        db.parent.mkdir(parents=True, exist_ok=True)
        db.symlink_to(real)
        assert signed_in_via_idc("linux", tmp_path, {}) is False


class TestGovernanceCapableIdentity:
    """Governance covers Identity Center AND API-key sign-ins.

    Answering "ungoverned" for the signal a caller does not check is what turns
    the diagnostic into advice that breaks a correctly configured host.
    """

    def test_api_key_alone_is_governance_capable(self, tmp_path):
        assert mcp_governance_may_apply("linux", tmp_path, {"KIRO_API_KEY": "abc"}) is True

    def test_blank_api_key_is_not_a_credential(self, tmp_path):
        assert mcp_governance_may_apply("linux", tmp_path, {"KIRO_API_KEY": "   "}) is False

    def test_no_signal_is_not_governance_capable(self, tmp_path):
        assert mcp_governance_may_apply("linux", tmp_path, {}) is False

    def test_idc_alone_is_governance_capable(self, tmp_path):
        db = kiro_cli_state_dbs("linux", tmp_path, {})[0]
        TestIdcDetection._make_db(db, ("auth.idc.start-url",))
        assert mcp_governance_may_apply("linux", tmp_path, {}) is True


class TestDoctorGovernanceSection:
    def _spec(self, tmp_path: Path, *, marked: bool) -> Path:
        entry: dict[str, object] = {"command": "kirocrew", "args": ["mcp-core"]}
        if marked:
            entry["type"] = "registry"
        path = tmp_path / "kirocrew.json"
        path.write_text(
            json.dumps({"mcpServers": {name: dict(entry) for name in MANAGED}}), encoding="utf-8"
        )
        return path

    def test_silent_on_a_personal_account(self, tmp_path, monkeypatch, capsys):
        """A governance warning in front of every personal install is noise."""
        monkeypatch.setattr(cli_doctor, "mcp_governance_may_apply", lambda: False)
        monkeypatch.setattr(
            cli_doctor.KiroCrewConfig, "load", staticmethod(lambda: _cfg(registry_mode=False))
        )
        issues: list[str] = []
        cli_doctor._doctor_mcp_governance(self._spec(tmp_path, marked=False), issues)
        assert capsys.readouterr().out == ""
        assert issues == []

    def test_declared_on_a_non_idc_account_is_flagged(self, tmp_path, monkeypatch, capsys):
        """The inverse filter. Outside registry mode a MARKED entry is the one the
        client drops, so this state breaks the same servers just as silently — and
        it is reachable by copying the guide onto a personal account."""
        monkeypatch.setattr(cli_doctor, "mcp_governance_may_apply", lambda: False)
        monkeypatch.setattr(
            cli_doctor.KiroCrewConfig, "load", staticmethod(lambda: _cfg(registry_mode=True))
        )
        issues: list[str] = []
        cli_doctor._doctor_mcp_governance(self._spec(tmp_path, marked=True), issues)
        out = capsys.readouterr().out
        assert issues == ["MCP registry mode on non-IDC account"]
        assert "mcp_registry_mode false" in out
        for name in MANAGED:
            assert name in out

    def test_stale_markers_without_the_declaration_are_flagged(self, tmp_path, monkeypatch, capsys):
        """Markers can outlive the declaration (a spec written under an enterprise
        account, refreshed by an older build). Same silent drop, so same warning."""
        monkeypatch.setattr(cli_doctor, "mcp_governance_may_apply", lambda: False)
        monkeypatch.setattr(
            cli_doctor.KiroCrewConfig, "load", staticmethod(lambda: _cfg(registry_mode=False))
        )
        issues: list[str] = []
        cli_doctor._doctor_mcp_governance(self._spec(tmp_path, marked=True), issues)
        assert issues == ["MCP registry mode on non-IDC account"]
        assert "markers are present" in capsys.readouterr().out

    def test_idc_without_declaration_explains_the_silent_failure(
        self, tmp_path, monkeypatch, capsys
    ):
        monkeypatch.setattr(cli_doctor, "mcp_governance_may_apply", lambda: True)
        monkeypatch.setattr(
            cli_doctor.KiroCrewConfig, "load", staticmethod(lambda: _cfg(registry_mode=False))
        )
        cli_doctor._doctor_mcp_governance(self._spec(tmp_path, marked=False), [])
        out = capsys.readouterr().out
        assert "registry mode: off" in out
        assert "agent.mcp_registry_mode true" in out
        for name in MANAGED:
            assert name in out

    def test_declared_but_unmarked_spec_is_an_issue(self, tmp_path, monkeypatch, capsys):
        """Declaring the mode and shipping an unmarked spec is the one state that
        silently drops every managed server, so it must not read as healthy."""
        monkeypatch.setattr(cli_doctor, "mcp_governance_may_apply", lambda: True)
        monkeypatch.setattr(
            cli_doctor.KiroCrewConfig, "load", staticmethod(lambda: _cfg(registry_mode=True))
        )
        issues: list[str] = []
        cli_doctor._doctor_mcp_governance(self._spec(tmp_path, marked=False), issues)
        assert issues == ["MCP registry markers"]
        assert "markers missing" in capsys.readouterr().out

    def test_declared_and_marked_reports_the_names_to_allow_list(
        self, tmp_path, monkeypatch, capsys
    ):
        monkeypatch.setattr(cli_doctor, "mcp_governance_may_apply", lambda: True)
        monkeypatch.setattr(
            cli_doctor.KiroCrewConfig, "load", staticmethod(lambda: _cfg(registry_mode=True))
        )
        issues: list[str] = []
        cli_doctor._doctor_mcp_governance(self._spec(tmp_path, marked=True), issues)
        out = capsys.readouterr().out
        assert issues == []
        assert "allow-listed" in out
        for name in MANAGED:
            assert name in out
        # Whether the admin actually allow-listed the names is not knowable
        # locally, so this branch must not render as a verified success.
        assert "✅" not in out
        assert "cannot verify" in out
        # And it must name the way out of a stale declaration.
        assert "mcp_registry_mode false" in out


class TestDoctorSurvivesAMalformedSpec:
    """Doctor must not crash on the spec shapes it exists to diagnose.

    `spec.get("mcpServers") or {}` only replaces a FALSY value, so a string or
    list survives and the membership walk raises AttributeError, aborting the
    whole doctor run.
    """

    @pytest.mark.parametrize(
        "servers", ["not-a-dict", ["kirocrew-core"], 7, True, "", [], {}]
    )
    def test_non_dict_mcp_servers_does_not_crash(self, tmp_path, monkeypatch, capsys, servers):
        path = tmp_path / "kirocrew.json"
        path.write_text(json.dumps({"mcpServers": servers}), encoding="utf-8")
        monkeypatch.setattr(cli_doctor, "mcp_governance_may_apply", lambda: True)
        monkeypatch.setattr(
            cli_doctor.KiroCrewConfig, "load", staticmethod(lambda: _cfg(registry_mode=True))
        )
        issues: list[str] = []
        cli_doctor._doctor_mcp_governance(path, issues)
        out = capsys.readouterr().out
        # No marker can be read from a malformed map, so it reports the missing
        # markers rather than raising.
        assert "markers missing" in out
        assert issues == ["MCP registry markers"]

    @pytest.mark.parametrize("body", ["[]", "null", '"a string"', "not json at all", ""])
    def test_malformed_spec_file_does_not_crash(self, tmp_path, monkeypatch, capsys, body):
        path = tmp_path / "kirocrew.json"
        path.write_text(body, encoding="utf-8")
        monkeypatch.setattr(cli_doctor, "mcp_governance_may_apply", lambda: True)
        monkeypatch.setattr(
            cli_doctor.KiroCrewConfig, "load", staticmethod(lambda: _cfg(registry_mode=False))
        )
        issues: list[str] = []
        cli_doctor._doctor_mcp_governance(path, issues)
        assert "registry mode: off" in capsys.readouterr().out

    def test_missing_spec_file_does_not_crash(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(cli_doctor, "mcp_governance_may_apply", lambda: True)
        monkeypatch.setattr(
            cli_doctor.KiroCrewConfig, "load", staticmethod(lambda: _cfg(registry_mode=False))
        )
        cli_doctor._doctor_mcp_governance(tmp_path / "absent.json", [])
        assert "registry mode: off" in capsys.readouterr().out


def _cfg(*, registry_mode: bool):
    """Minimal stand-in for the loaded config: only the one field is read."""

    class _Agent:
        mcp_registry_mode = registry_mode

    class _Cfg:
        agent = _Agent()

    return _Cfg()
