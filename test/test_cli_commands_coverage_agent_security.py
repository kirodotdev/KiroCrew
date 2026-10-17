"""Coverage tests for :mod:`kiro_crew.cli_commands`: agent, workspace, security, policy.

The agent and workspace subcommands write locked deltas into ``config.json``; the
security and policy subcommands read the governance state back: the deny list, the
``audit`` scan, ``events`` with ``--since``/``--until``, the audit-chain ``verify``,
and policy ``show``, ``validate``, ``explain`` and ``profile``. Same conventions as
``test_cli_commands_coverage.py``: handlers are called directly with an
``argparse.Namespace``, and every write lands in ``tmp_path``.
"""

from __future__ import annotations

import dataclasses
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from cli_commands_coverage_helpers import _ns

from kiro_crew import cli_commands as cc
from kiro_crew import sel as sel_mod
from kiro_crew.config.loader import KiroCrewAgentConfig, KiroCrewConfig, WorkspaceConfig
from kiro_crew.security import BUILTIN_DENIED_RULES


def _cfg_with(
    *,
    agents: dict[str, KiroCrewAgentConfig] | None = None,
    workspaces: dict[str, WorkspaceConfig] | None = None,
    default_agent: str = "default",
    default_workspace: str = "default",
) -> KiroCrewConfig:
    """An in-memory config whose ``save()`` is a no-op recorder."""
    cfg = KiroCrewConfig()
    cfg.agents = agents if agents is not None else {}
    cfg.workspaces = workspaces if workspaces is not None else {}
    cfg.default_agent = default_agent
    cfg.default_workspace = default_workspace
    cfg.save = MagicMock()  # type: ignore[method-assign]
    return cfg


def _seed_doc_file(tmp_path: Path, cfg: KiroCrewConfig) -> Path:
    """Materialize *cfg* as a real config.json for the locked-delta writers.

    The CLI CRUD commands do not mutate the loaded snapshot and ``save()``
    it -- they write a delta on the document read inside the sidecar flock
    so tests that check persistence must seed and read the
    FILE, not the in-memory dataclass.
    """
    doc = {
        "workspaces": {n: dataclasses.asdict(w) for n, w in cfg.workspaces.items()},
        "agents": {n: dataclasses.asdict(a) for n, a in cfg.agents.items()},
        "memory_stores": {n: dataclasses.asdict(m) for n, m in cfg.memory_stores.items()},
        "agent": {"default_agent": cfg.default_agent},
    }
    p = tmp_path / "config.json"
    p.write_text(json.dumps(doc), encoding="utf-8")
    return p


def _read_doc(p: Path) -> dict:
    return json.loads(p.read_text(encoding="utf-8"))


# ── agent subcommands ──


class TestAgentCli:
    def test_list_marks_default(self, capsys: pytest.CaptureFixture[str]) -> None:
        cfg = _cfg_with(
            agents={
                "default": KiroCrewAgentConfig(kiro_agent="kirocrew"),
                "other": KiroCrewAgentConfig(kiro_agent="alt"),
            }
        )
        with patch.object(KiroCrewConfig, "load", return_value=cfg):
            cc._handle_agent(_ns(agent_action="list"))
        out = capsys.readouterr().out
        assert "default *" in out and "other" in out and "alt" in out

    def test_create_persists_new_agent(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        cfg = _cfg_with(agents={})
        cfg_path = _seed_doc_file(tmp_path, cfg)
        with (
            patch.object(KiroCrewConfig, "load", return_value=cfg),
            patch("kiro_crew.config.loader.config_path", return_value=cfg_path),
        ):
            cc._handle_agent(
                _ns(
                    agent_action="create",
                    name="new",
                    kiro_agent="ka",
                    workspace="ws",
                    memory_store="",
                )
            )
        doc = _read_doc(cfg_path)
        assert doc["agents"]["new"]["kiro_agent"] == "ka"
        assert doc["agents"]["new"]["workspace"] == "ws"
        store = doc["agents"]["new"]["memory_store"]
        assert store != "default"
        assert doc["memory_stores"][store]["owner_member"] == "new"
        assert doc["memory_stores"][store]["memory_version"] == 2
        assert "Created agent: new" in capsys.readouterr().out

    def test_create_duplicate_exits_1_without_saving(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        cfg = _cfg_with(agents={"dup": KiroCrewAgentConfig()})
        with (
            patch.object(KiroCrewConfig, "load", return_value=cfg),
            pytest.raises(SystemExit) as exc,
        ):
            cc._handle_agent(
                _ns(
                    agent_action="create",
                    name="dup",
                    kiro_agent="",
                    workspace="",
                    memory_store="",
                )
            )
        assert exc.value.code == 1
        cfg.save.assert_not_called()  # type: ignore[attr-defined]
        assert "already exists" in capsys.readouterr().err

    def test_update_applies_only_provided_fields(self, tmp_path: Path) -> None:
        cfg = _cfg_with(
            agents={"a": KiroCrewAgentConfig(kiro_agent="old", workspace="ws0", memory_store="m0")}
        )
        cfg_path = _seed_doc_file(tmp_path, cfg)
        with (
            patch.object(KiroCrewConfig, "load", return_value=cfg),
            patch("kiro_crew.config.loader.config_path", return_value=cfg_path),
        ):
            cc._handle_agent(
                _ns(
                    agent_action="update",
                    name="a",
                    kiro_agent="new",
                    workspace=None,
                    memory_store=None,
                )
            )
        agent = _read_doc(cfg_path)["agents"]["a"]
        assert agent["kiro_agent"] == "new"
        assert agent["workspace"] == "ws0"
        assert agent["memory_store"] == "m0"

    def test_update_template_and_workspace_preserves_private_memory(self, tmp_path: Path) -> None:
        from kiro_crew.memory_stores import provision_member_memory

        cfg = _cfg_with(agents={"a": KiroCrewAgentConfig()})
        store = provision_member_memory(cfg, "a")
        cfg_path = _seed_doc_file(tmp_path, cfg)
        with (
            patch.object(KiroCrewConfig, "load", return_value=cfg),
            patch("kiro_crew.config.loader.config_path", return_value=cfg_path),
        ):
            cc._handle_agent(
                _ns(
                    agent_action="update",
                    name="a",
                    kiro_agent="k",
                    workspace="w",
                    memory_store=None,
                )
            )
        agent = _read_doc(cfg_path)["agents"]["a"]
        assert (agent["kiro_agent"], agent["workspace"], agent["memory_store"]) == (
            "k",
            "w",
            store,
        )

    def test_update_missing_exits_1(self, capsys: pytest.CaptureFixture[str]) -> None:
        cfg = _cfg_with(agents={})
        with (
            patch.object(KiroCrewConfig, "load", return_value=cfg),
            pytest.raises(SystemExit) as exc,
        ):
            cc._handle_agent(
                _ns(
                    agent_action="update",
                    name="ghost",
                    kiro_agent=None,
                    workspace=None,
                    memory_store=None,
                )
            )
        assert exc.value.code == 1
        assert "not found" in capsys.readouterr().err

    def test_delete_removes_non_default(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        cfg = _cfg_with(
            agents={"default": KiroCrewAgentConfig(), "spare": KiroCrewAgentConfig()},
        )
        cfg_path = _seed_doc_file(tmp_path, cfg)
        with (
            patch.object(KiroCrewConfig, "load", return_value=cfg),
            patch("kiro_crew.config.loader.config_path", return_value=cfg_path),
        ):
            cc._handle_agent(_ns(agent_action="delete", name="spare"))
        assert "spare" not in _read_doc(cfg_path)["agents"]
        assert "Deleted agent: spare" in capsys.readouterr().out

    def test_delete_drops_the_crew_from_its_team_best_effort(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Same contract as the dashboard delete: the crew is dropped from its
        team, and a store that cannot even be locked never refuses the delete
        (the recreate-inherits harm is closed on the create path instead)."""
        from kiro_crew import crew_teams

        cfg = _cfg_with(
            agents={"default": KiroCrewAgentConfig(), "spare": KiroCrewAgentConfig()},
        )
        cfg_path = _seed_doc_file(tmp_path, cfg)
        dropped: list[str] = []
        with (
            patch.object(KiroCrewConfig, "load", return_value=cfg),
            patch("kiro_crew.config.loader.config_path", return_value=cfg_path),
            patch.object(crew_teams, "drop_member", lambda name: dropped.append(name)),
        ):
            cc._handle_agent(_ns(agent_action="delete", name="spare"))
        assert dropped == ["spare"]
        assert "spare" not in _read_doc(cfg_path)["agents"]
        assert "Deleted agent: spare" in capsys.readouterr().out

        class _Broken:
            def __enter__(self):
                raise OSError("crew-teams: lock file unwritable")

            def __exit__(self, *exc):
                return False

        cfg = _cfg_with(
            agents={"default": KiroCrewAgentConfig(), "other": KiroCrewAgentConfig()},
        )
        cfg_path = _seed_doc_file(tmp_path, cfg)
        with (
            patch.object(KiroCrewConfig, "load", return_value=cfg),
            patch("kiro_crew.config.loader.config_path", return_value=cfg_path),
            patch.object(crew_teams, "document_lock", lambda: _Broken()),
        ):
            cc._handle_agent(_ns(agent_action="delete", name="other"))
        assert "other" not in _read_doc(cfg_path)["agents"]
        assert "Deleted agent: other" in capsys.readouterr().out

    def test_create_releases_the_name_from_a_stale_team_first(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A name a deleted crew left on a team is purged BEFORE the new crew
        is registered, so it never inherits the old membership."""
        from kiro_crew import crew_teams

        cfg = _cfg_with(agents={})
        cfg_path = _seed_doc_file(tmp_path, cfg)
        released: list[str] = []
        with (
            patch.object(KiroCrewConfig, "load", return_value=cfg),
            patch("kiro_crew.config.loader.config_path", return_value=cfg_path),
            patch.object(crew_teams, "release_name", lambda name: released.append(name)),
        ):
            cc._handle_agent(
                _ns(
                    agent_action="create",
                    name="new",
                    kiro_agent="ka",
                    workspace="ws",
                    memory_store="",
                )
            )
        assert released == ["new"]
        assert "new" in _read_doc(cfg_path)["agents"]

    def test_create_is_refused_while_a_stale_membership_cannot_be_purged(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Registering the name while its old team entry cannot be removed is
        exactly what would expose that entry, so the create is refused with
        the registry untouched."""
        from kiro_crew import crew_teams

        cfg = _cfg_with(agents={})
        cfg_path = _seed_doc_file(tmp_path, cfg)

        def _boom(name: str) -> bool:
            raise OSError("crew-teams: disk full")

        with (
            patch.object(KiroCrewConfig, "load", return_value=cfg),
            patch("kiro_crew.config.loader.config_path", return_value=cfg_path),
            patch.object(crew_teams, "release_name", _boom),
            pytest.raises(SystemExit) as exc,
        ):
            cc._handle_agent(
                _ns(
                    agent_action="create",
                    name="new",
                    kiro_agent="ka",
                    workspace="ws",
                    memory_store="",
                )
            )
        assert exc.value.code == 1
        assert "new" not in _read_doc(cfg_path)["agents"]
        out = capsys.readouterr()
        assert "Created agent" not in out.out
        assert "cannot create agent 'new'" in out.err

    def test_delete_default_is_refused(self, capsys: pytest.CaptureFixture[str]) -> None:
        cfg = _cfg_with(agents={"default": KiroCrewAgentConfig()})
        with (
            patch.object(KiroCrewConfig, "load", return_value=cfg),
            pytest.raises(SystemExit) as exc,
        ):
            cc._handle_agent(_ns(agent_action="delete", name="default"))
        assert exc.value.code == 1
        assert "default" in cfg.agents
        assert "cannot delete default agent" in capsys.readouterr().err

    def test_delete_missing_exits_1(self) -> None:
        cfg = _cfg_with(agents={})
        with (
            patch.object(KiroCrewConfig, "load", return_value=cfg),
            pytest.raises(SystemExit),
        ):
            cc._handle_agent(_ns(agent_action="delete", name="ghost"))

    def test_unknown_action_prints_usage(self, capsys: pytest.CaptureFixture[str]) -> None:
        with patch.object(KiroCrewConfig, "load", return_value=_cfg_with()):
            cc._handle_agent(_ns(agent_action=None))
        assert "Usage: kirocrew agent" in capsys.readouterr().out


# ── workspace create --copy-from ──


class TestWorkspaceCopyFrom:
    def _base(self) -> KiroCrewConfig:
        return _cfg_with(
            workspaces={
                "default": WorkspaceConfig(dir="workspace"),
                "src": WorkspaceConfig(dir="workspace-src"),
            }
        )

    def test_copy_from_unknown_source_exits_1(self, tmp_path: Path) -> None:
        with (
            patch("kiro_crew.cli_commands.config_dir", return_value=tmp_path),
            patch.object(KiroCrewConfig, "load", return_value=self._base()),
            patch("kiro_crew.cli_commands.sel"),
            pytest.raises(SystemExit) as exc,
        ):
            cc._handle_workspace(
                _ns(workspace_action="create", name="new", dir=None, copy_from="ghost")
            )
        assert exc.value.code == 1

    def test_copy_from_copies_tree_and_registers(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        src = tmp_path / "workspace-src"
        (src / "memory").mkdir(parents=True)
        (src / "memory" / "notes.md").write_text("hi", encoding="utf-8")
        cfg = self._base()
        cfg_path = _seed_doc_file(tmp_path, cfg)
        with (
            patch("kiro_crew.cli_commands.config_dir", return_value=tmp_path),
            patch.object(KiroCrewConfig, "load", return_value=cfg),
            patch("kiro_crew.config.loader.config_path", return_value=cfg_path),
            patch("kiro_crew.cli_commands.sel"),
        ):
            cc._handle_workspace(
                _ns(workspace_action="create", name="copy1", dir=None, copy_from="src")
            )
        assert (tmp_path / "workspace-copy1" / "memory" / "notes.md").read_text() == "hi"
        assert _read_doc(cfg_path)["workspaces"]["copy1"]["dir"] == "workspace-copy1"
        assert "Created workspace: copy1" in capsys.readouterr().out

    def test_copy_from_skips_sensitive_entries(self, tmp_path: Path) -> None:
        src = tmp_path / "workspace-src"
        src.mkdir()
        (src / "ok.txt").write_text("keep", encoding="utf-8")
        (src / "secret.env").write_text("nope", encoding="utf-8")

        def _sensitive(p: str) -> bool:
            return p.endswith("secret.env")

        with (
            patch("kiro_crew.cli_commands.config_dir", return_value=tmp_path),
            patch.object(KiroCrewConfig, "load", return_value=self._base()),
            patch("kiro_crew.cli_commands.is_sensitive_path", side_effect=_sensitive),
            patch("kiro_crew.cli_commands.sel"),
        ):
            cc._handle_workspace(
                _ns(workspace_action="create", name="copy2", dir=None, copy_from="src")
            )
        dst = tmp_path / "workspace-copy2"
        assert (dst / "ok.txt").exists()
        assert not (dst / "secret.env").exists()

    def test_copy_from_escaping_dir_is_refused(self, tmp_path: Path) -> None:
        with (
            patch("kiro_crew.cli_commands.config_dir", return_value=tmp_path),
            patch.object(KiroCrewConfig, "load", return_value=self._base()),
            patch("kiro_crew.cli_commands.sel") as sel,
            pytest.raises(SystemExit),
        ):
            cc._handle_workspace(
                _ns(workspace_action="create", name="bad", dir="/etc", copy_from="src")
            )
        assert sel.return_value.log_api_access.call_args.kwargs["outcome"] == "denied"

    def test_copy_from_dir_collision_is_refused(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with (
            patch("kiro_crew.cli_commands.config_dir", return_value=tmp_path),
            patch.object(KiroCrewConfig, "load", return_value=self._base()),
            patch("kiro_crew.cli_commands.sel"),
            pytest.raises(SystemExit),
        ):
            cc._handle_workspace(
                _ns(
                    workspace_action="create",
                    name="clash",
                    dir="workspace-src",
                    copy_from="src",
                )
            )
        assert "already used by another workspace" in capsys.readouterr().err

    def test_copy_from_missing_source_dir_still_registers(self, tmp_path: Path) -> None:
        """A source workspace with no directory on disk registers a USABLE copy.

        With no source tree to publish, the create falls through to the plain
        branch, which materializes the destination. A registered ``dir`` that does
        not exist is precisely the entry that makes the V2 private-memory layout
        refuse every private member.
        """
        cfg = self._base()
        cfg_path = _seed_doc_file(tmp_path, cfg)
        with (
            patch("kiro_crew.cli_commands.config_dir", return_value=tmp_path),
            patch.object(KiroCrewConfig, "load", return_value=cfg),
            patch("kiro_crew.config.loader.config_path", return_value=cfg_path),
            patch("kiro_crew.cli_commands.sel"),
        ):
            cc._handle_workspace(
                _ns(workspace_action="create", name="copy3", dir=None, copy_from="src")
            )
        assert "copy3" in _read_doc(cfg_path)["workspaces"]
        assert (tmp_path / "workspace-copy3").is_dir()


# ── security subcommands ──


class TestSecurityCli:
    def test_deny_list_prints_builtins_and_user_patterns(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        (tmp_path / "config.json").write_text(
            json.dumps({"hooks": {"auto_deny_tools": ["my-custom-pattern"]}}), encoding="utf-8"
        )
        with patch("kiro_crew.cli_commands.config_dir", return_value=tmp_path):
            cc._security(_ns(sec_action="deny-list"))
        out = capsys.readouterr().out
        assert "Built-in deny patterns" in out
        assert "my-custom-pattern" in out

    def test_deny_list_without_config_file(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with patch("kiro_crew.cli_commands.config_dir", return_value=tmp_path):
            cc._security(_ns(sec_action="deny-list"))
        out = capsys.readouterr().out
        assert "Built-in deny patterns" in out
        assert "User-configured" not in out

    def test_audit_clean_reports_history_only(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Both stores clean: the history line is printed and the memory verdict is
        deliberately suppressed (the ``elif not findings: pass`` branch), so a fully
        clean audit says nothing about vector memory."""
        with (
            patch("kiro_crew.cli_commands.config_dir", return_value=tmp_path),
            patch("kiro_crew.cli_commands.scan_history", return_value=[]),
            patch("kiro_crew.cli_commands.scan_memory", return_value=[]),
        ):
            cc._security(_ns(sec_action="audit"))
        out = capsys.readouterr().out
        assert "No suspicious tool usage" in out
        assert "vector memory" not in out

    def test_audit_reports_history_and_memory_findings(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        history = [{"file": "s.jsonl", "warning": "exfil", "snippet": "curl evil"}]
        memory = [{"type": "semantic", "key": "k", "warning": "odd", "value": "v"}]
        with (
            patch("kiro_crew.cli_commands.config_dir", return_value=tmp_path),
            patch("kiro_crew.cli_commands.scan_history", return_value=history),
            patch("kiro_crew.cli_commands.scan_memory", return_value=memory),
        ):
            cc._security(_ns(sec_action="audit"))
        out = capsys.readouterr().out
        assert "1 suspicious entries found" in out and "s.jsonl" in out
        assert "1 suspicious memory entries" in out and "[semantic] k" in out

    def test_audit_history_findings_with_clean_memory(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        history = [{"file": "s.jsonl", "warning": "w", "snippet": "x"}]
        with (
            patch("kiro_crew.cli_commands.config_dir", return_value=tmp_path),
            patch("kiro_crew.cli_commands.scan_history", return_value=history),
            patch("kiro_crew.cli_commands.scan_memory", return_value=[]),
        ):
            cc._security(_ns(sec_action="audit"))
        assert "No suspicious content in vector memory." in capsys.readouterr().out

    def test_events_empty(self, capsys: pytest.CaptureFixture[str]) -> None:
        with patch("kiro_crew.cli_commands.sel") as sel:
            sel.return_value.recent.return_value = []
            cc._security(_ns(sec_action="events", limit=5))
        assert "No security events recorded." in capsys.readouterr().out

    def test_events_prints_the_resources_a_decision_was_about(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A refusal the owner cannot read the subject of tells them nothing.

        ``resources`` is where a scanner refusal names the file it held back, and
        this renderer is the read path an owner actually has: the endpoint is
        owner-only and there is no audit UI. Printing operation and outcome alone
        says a refusal happened and never says what was refused.
        """
        with patch("kiro_crew.cli_commands.sel") as sel:
            sel.return_value.recent.return_value = [
                {
                    "timestamp": "2026-09-24T12:00:00Z",
                    "event_type": "api_access",
                    "operation": "file_delivery_consent.refused",
                    "outcome": "refused",
                    "source": "file-delivery-consent",
                    "caller_identity": "gateway",
                    "resources": "owner_dashboard: download (flagged content): device.conf",
                }
            ]
            cc._security(_ns(sec_action="events", limit=5))
        out = capsys.readouterr().out
        assert "device.conf" in out
        assert "resources:" in out

    def test_events_omits_the_resources_line_when_there_is_none(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The negative: an empty field must print no line, not a bare label."""
        with patch("kiro_crew.cli_commands.sel") as sel:
            sel.return_value.recent.return_value = [
                {
                    "timestamp": "2026-09-24T12:00:00Z",
                    "event_type": "api_access",
                    "operation": "sel.events.read",
                    "outcome": "allowed",
                    "source": "dashboard",
                    "caller_identity": "owner",
                    "resources": "",
                }
            ]
            cc._security(_ns(sec_action="events", limit=5))
        assert "resources:" not in capsys.readouterr().out

    def test_events_strips_control_bytes_from_untrusted_audit_fields(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A refused file name is text the AGENT chose, and it lands on a terminal.

        A POSIX name may carry ESC/OSC bytes, and nothing upstream removes them --
        the consent module's redaction and SEL's write-path pass both police
        credentials and length, not control sequences. Printed raw here those bytes
        would execute in the owner's terminal, which is the one place this trail is
        read, so both untrusted fields go through the shared one-line policy.
        """
        with patch("kiro_crew.cli_commands.sel") as sel:
            sel.return_value.recent.return_value = [
                {
                    "timestamp": "2026-09-24T12:00:00Z",
                    "event_type": "api_access",
                    "operation": "file_delivery_consent.refused",
                    "outcome": "refused",
                    "source": "file-delivery-consent",
                    "caller_identity": "gateway",
                    "resources": "owner_dashboard: download: \x1b]0;pwned\x07evil.conf",
                    "error": "content_redacted: \x1b[2Jwiped.conf",
                }
            ]
            cc._security(_ns(sec_action="events", limit=5))
        out = capsys.readouterr().out
        assert "\x1b" not in out
        assert "\x07" not in out
        # The readable part survives -- sanitizing must not blank the subject.
        assert "evil.conf" in out
        assert "wiped.conf" in out

    def test_events_strips_control_bytes_from_the_summary_line_too(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """``operation`` and ``outcome`` are caller text, like the detail fields.

        SEL's own ``_REDACTED_TEXT_FIELDS`` names ``operation`` beside ``resources``
        and ``error``, and ``log_api_access`` documents ``outcome`` the same way,
        because an installed app reaches it through ``ctx.audit``. Sanitizing only
        the indented detail lines would leave the summary line above them a live
        path to the same terminal.
        """
        with patch("kiro_crew.cli_commands.sel") as sel:
            sel.return_value.recent.return_value = [
                {
                    "timestamp": "2026-09-24T12:00:00Z",
                    "event_type": "api_\x1b[31maccess",
                    "operation": "app:evil.\x1b]0;pwned\x07publish",
                    "outcome": "suc\x1b[2Jcess",
                    "source": "app\x1b[1m-kit",
                    "caller_identity": "app:e\x1bvil",
                }
            ]
            cc._security(_ns(sec_action="events", limit=5))
        out = capsys.readouterr().out
        assert "\x1b" not in out
        assert "\x07" not in out
        # The readable parts survive on the summary line.
        assert "publish" in out
        assert "cess" in out

    def test_events_survives_a_forged_row_whose_fields_are_not_strings(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A non-string field must not abort the owner's only read path.

        ``recent()`` validates that each line is a dict, not that any field is a
        string, and the log is sandbox read-write, so a forged line can carry an int
        or a list. Handing that straight to ``re.sub`` would raise ``TypeError`` and
        take the whole listing down -- one bad row would hide every good one.
        """
        with patch("kiro_crew.cli_commands.sel") as sel:
            sel.return_value.recent.return_value = [
                {
                    "timestamp": 1234567890,
                    "event_type": ["api_access"],
                    "operation": {"a": 1},
                    "outcome": None,
                    "source": 42,
                    "caller_identity": 7,
                    "resources": 99,
                    "error": ["boom"],
                    "downstream_service": 5,
                },
                {
                    "timestamp": "2026-09-24T12:00:00Z",
                    "event_type": "api_access",
                    "operation": "later.row",
                    "outcome": "success",
                    "source": "sel",
                    "caller_identity": "gateway",
                },
            ]
            cc._security(_ns(sec_action="events", limit=5))
        out = capsys.readouterr().out
        # The forged row renders instead of raising, and the good row still prints.
        assert "99" in out
        assert "later.row" in out

    def test_events_passes_the_time_window_through(self) -> None:
        """``-n`` alone cannot express "the last two hours"."""
        with patch("kiro_crew.cli_commands.sel") as sel:
            sel.return_value.recent.return_value = []
            cc._security(
                _ns(sec_action="events", limit=5, since="2026-08-21T00:00:00Z", until="2h")
            )
        kwargs = sel.return_value.recent.call_args.kwargs
        assert kwargs["since"] == datetime(2026, 8, 21, tzinfo=timezone.utc)
        assert kwargs["until"] is not None and kwargs["until"].tzinfo is not None

    def test_events_rejects_an_unreadable_time(self, capsys: pytest.CaptureFixture[str]) -> None:
        """A typo must not read as "no events in that window"."""
        with patch("kiro_crew.cli_commands.sel") as sel:
            with pytest.raises(SystemExit) as exc:
                cc._security(_ns(sec_action="events", limit=5, since="yesterdayish"))
        assert exc.value.code == 2
        assert "cannot read" in capsys.readouterr().out
        sel.return_value.recent.assert_not_called()

    def test_events_rejects_an_inverted_window(self, capsys: pytest.CaptureFixture[str]) -> None:
        with patch("kiro_crew.cli_commands.sel") as sel:
            with pytest.raises(SystemExit) as exc:
                cc._security(
                    _ns(
                        sec_action="events",
                        limit=5,
                        since="2026-08-21T10:00:00Z",
                        until="2026-08-21T09:00:00Z",
                    )
                )
        assert exc.value.code == 2
        assert "--since must be earlier than --until" in capsys.readouterr().out
        sel.return_value.recent.assert_not_called()

    def test_events_names_the_window_when_empty(self, capsys: pytest.CaptureFixture[str]) -> None:
        with patch("kiro_crew.cli_commands.sel") as sel:
            sel.return_value.recent.return_value = []
            cc._security(_ns(sec_action="events", limit=5, since="2026-08-21T00:00:00Z"))
        out = capsys.readouterr().out
        assert "No security events recorded in [2026-08-21T00:00:00+00:00, now)." in out

    def test_events_renders_error_and_downstream(self, capsys: pytest.CaptureFixture[str]) -> None:
        events = [
            {
                "timestamp": "2026-01-01T00:00:00Z",
                "event_type": "api_access",
                "operation": "cron.add",
                "outcome": "allowed",
                "source": "cli",
                "caller_identity": "me",
                "error": "boom",
                "downstream_service": "slack",
            }
        ]
        with patch("kiro_crew.cli_commands.sel") as sel:
            sel.return_value.recent.return_value = events
            cc._security(_ns(sec_action="events", limit=20))
        out = capsys.readouterr().out
        assert "cron.add → allowed" in out and "error: boom" in out and "slack" in out

    @pytest.mark.parametrize(
        ("total", "valid", "verifiable", "expected"),
        [
            (0, 0, True, "No security events to verify."),
            (3, 3, True, "HMAC chain intact"),
            (3, 1, True, "HMAC chain COMPROMISED"),
            (
                5,
                5,
                False,
                "Audit history UNVERIFIABLE: segment directory refused to pin",
            ),
            (0, 0, False, "Audit history UNVERIFIABLE"),
            (
                5,
                3,
                False,
                "the live log shows tampered entries: 3/5 entries valid",
            ),
        ],
    )
    def test_verify_reports_chain_state(
        self,
        total: int,
        valid: int,
        verifiable: bool,
        expected: str,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        outcome = sel_mod.SelVerification(
            total=total,
            valid=valid,
            history_verifiable=verifiable,
            reason="" if verifiable else "segment directory refused to pin (planted link?)",
        )
        with patch("kiro_crew.cli_commands.sel") as sel:
            sel.return_value.verify_integrity.return_value = outcome
            cc._security(_ns(sec_action="verify"))
        assert expected in capsys.readouterr().out

    def test_unknown_action_prints_usage(self, capsys: pytest.CaptureFixture[str]) -> None:
        cc._security(_ns(sec_action="nope"))
        assert "Usage: kirocrew security" in capsys.readouterr().out


# ── policy subcommands ──


def _fake_ceiling() -> Any:
    return SimpleNamespace(
        version=2,
        signature_summary=lambda: "unsigned",
        boot=SimpleNamespace(require_sandbox=True, allow_terminal=False, fail_closed=True),
        controls={"capabilities.telemetry": "off"},
    )


class TestParseTimeSelector:
    """``--since``/``--until`` input handling for ``security events``."""

    def test_empty_means_no_bound(self) -> None:
        assert cc.parse_time_selector("") is None
        assert cc.parse_time_selector("   ") is None

    @pytest.mark.parametrize(
        ("text", "secs"),
        [("45s", 45), ("30m", 1800), ("2h", 7200), ("7d", 604800), ("1w", 604800 * 1)],
    )
    def test_relative_age_counts_back_from_now(self, text: str, secs: int) -> None:
        now = datetime(2026, 8, 21, 12, 0, tzinfo=timezone.utc)
        assert cc.parse_time_selector(text, now=now) == now - timedelta(seconds=secs)

    def test_relative_age_is_case_insensitive(self) -> None:
        now = datetime(2026, 8, 21, 12, 0, tzinfo=timezone.utc)
        assert cc.parse_time_selector("2H", now=now) == cc.parse_time_selector("2h", now=now)

    def test_iso_instant_with_z_suffix(self) -> None:
        # fromisoformat only accepts "Z" from 3.11; the package supports 3.10.
        assert cc.parse_time_selector("2026-08-21T04:00:00Z") == datetime(
            2026, 8, 21, 4, tzinfo=timezone.utc
        )

    def test_bare_date_is_read_as_utc(self) -> None:
        """The audit log is written in UTC.

        Reading a bare date as local time would silently shift the window by the
        host's offset, so a window that looks right returns the wrong records.
        """
        assert cc.parse_time_selector("2026-08-21") == datetime(2026, 8, 21, tzinfo=timezone.utc)

    def test_offset_is_normalized_to_utc(self) -> None:
        assert cc.parse_time_selector("2026-08-21T10:00:00+02:00") == datetime(
            2026, 8, 21, 8, tzinfo=timezone.utc
        )

    def test_unreadable_input_raises_with_the_accepted_forms(self) -> None:
        with pytest.raises(ValueError, match="relative age"):
            cc.parse_time_selector("last tuesday")

    def test_an_absurd_but_well_formed_age_raises_instead_of_overflowing(self) -> None:
        """``timedelta`` overflows before the subtraction.

        Uncaught, that is an OverflowError traceback and exit 1 from a bad flag
        value -- the CLI must still refuse it as input (exit 2) with guidance.
        """
        with pytest.raises(ValueError, match="too far in the past"):
            cc.parse_time_selector("999999999999999999w")


class TestPolicyCli:
    def test_show_without_policy(self, capsys: pytest.CaptureFixture[str]) -> None:
        with patch(
            "kiro_crew.platform.context.current_context",
            return_value=SimpleNamespace(governance=None),
        ):
            cc._policy(_ns(policy_action="show"))
        assert "No enterprise security policy is active" in capsys.readouterr().out

    def test_show_prints_provenance_boot_and_scopes(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with patch(
            "kiro_crew.platform.context.current_context",
            return_value=SimpleNamespace(governance=_fake_ceiling()),
        ):
            cc._policy(_ns(policy_action="show"))
        out = capsys.readouterr().out
        assert "Security policy v2" in out
        assert "provenance: unsigned" in out
        assert "require_sandbox=True" in out
        assert "capabilities.telemetry: off" in out

    def test_show_without_policy_includes_denied_command_summary(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """An agent's only other discovery mechanism for
        the built-in denied-command rules was to attempt one and be refused.
        `policy show` must surface them even on a standalone (non-enterprise)
        install, which is the common case the early-return branch serves.

        The totals are DERIVED from the catalog rather than spelled out: the
        printer itself derives them, so a literal here would only duplicate
        the explicit count assertion in ``test_denied_commands_security.py``
        and rot on every rule that gets added (it did -- the docstring said
        139 while the catalog held 140)."""
        by_category: dict[str, int] = {}
        for rule in BUILTIN_DENIED_RULES:
            by_category[rule.category] = by_category.get(rule.category, 0) + 1
        biggest, biggest_n = max(by_category.items(), key=lambda kv: kv[1])
        with patch(
            "kiro_crew.platform.context.current_context",
            return_value=SimpleNamespace(governance=None),
        ):
            cc._policy(_ns(policy_action="show"))
        out = capsys.readouterr().out
        assert (
            f"commands.denied: {len(BUILTIN_DENIED_RULES)} rules "
            f"in {len(by_category)} categories"
        ) in out
        assert f"{biggest}({biggest_n})" in out
        # Counts only by default -- rule ids are the --ids opt-in.
        assert "aws-destructive-ec2-terminate-instances" not in out

    def test_show_with_policy_also_includes_denied_command_summary(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with patch(
            "kiro_crew.platform.context.current_context",
            return_value=SimpleNamespace(governance=_fake_ceiling()),
        ):
            cc._policy(_ns(policy_action="show"))
        assert "commands.denied:" in capsys.readouterr().out

    def test_show_ids_lists_rule_ids_per_category(self, capsys: pytest.CaptureFixture[str]) -> None:
        # Named --ids, not --verbose: the top-level parser already defines
        # --verbose/-v as an int `count` (log level); a same-named store_true
        # on this subparser would collide via argparse's parent/subparser
        # default-override gotcha.
        with patch(
            "kiro_crew.platform.context.current_context",
            return_value=SimpleNamespace(governance=None),
        ):
            cc._policy(_ns(policy_action="show", ids=True))
        out = capsys.readouterr().out
        assert "aws-destructive-ec2-terminate-instances" in out
        assert "reverse-shell-nc" in out

    def test_show_with_no_governed_scopes(self, capsys: pytest.CaptureFixture[str]) -> None:
        ceiling = _fake_ceiling()
        ceiling.controls = {}
        with patch(
            "kiro_crew.platform.context.current_context",
            return_value=SimpleNamespace(governance=ceiling),
        ):
            cc._policy(_ns(policy_action="show"))
        assert "(no governed scopes)" in capsys.readouterr().out

    def test_validate_without_policy_or_profiles_dir(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with (
            patch(
                "kiro_crew.platform.context.current_context",
                return_value=SimpleNamespace(governance=None),
            ),
            patch(
                "kiro_crew.platform.governance_profiles._profiles_dir",
                return_value=tmp_path / "absent",
            ),
        ):
            cc._policy(_ns(policy_action="validate"))
        out = capsys.readouterr().out
        assert "nothing to validate" in out and "(no profiles directory)" in out and "valid" in out

    def test_validate_flags_invalid_profile(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        pdir = tmp_path / "profiles"
        pdir.mkdir()
        (pdir / "good.json").write_text("{}", encoding="utf-8")
        (pdir / "bad.json").write_text("{}", encoding="utf-8")

        def _get(stem: str) -> Any:
            return SimpleNamespace(name="_deny_all" if stem == "bad" else stem)

        with (
            patch(
                "kiro_crew.platform.context.current_context",
                return_value=SimpleNamespace(governance=_fake_ceiling()),
            ),
            patch("kiro_crew.platform.governance_profiles._profiles_dir", return_value=pdir),
            patch("kiro_crew.platform.governance_profiles.get_store_profile", side_effect=_get),
        ):
            cc._policy(_ns(policy_action="validate"))
        out = capsys.readouterr().out
        assert "Policy: v2 OK" in out
        assert "bad.json: INVALID→deny-all" in out
        assert "some profiles failed validation" in out

    def test_explain_unknown_scope_lists_catalog(self, capsys: pytest.CaptureFixture[str]) -> None:
        with (
            patch(
                "kiro_crew.platform.context.current_context",
                return_value=SimpleNamespace(governance=None),
            ),
            patch(
                "kiro_crew.platform.governance.SCOPE_CATALOG",
                {"capabilities.telemetry": object()},
            ),
        ):
            cc._policy(
                _ns(
                    policy_action="explain",
                    scope="nope.scope",
                    item="x",
                    session_key="s",
                    agent=None,
                    app=None,
                )
            )
        out = capsys.readouterr().out
        assert "Unknown scope" in out and "capabilities.telemetry" in out

    def test_explain_known_scope_prints_verdicts(self, capsys: pytest.CaptureFixture[str]) -> None:
        decision = SimpleNamespace(
            permitted=False, rule="deny", layer="policy", reason="pinned off"
        )
        gate = SimpleNamespace(permitted=True, reason="title allowed")
        with (
            patch(
                "kiro_crew.platform.context.current_context",
                return_value=SimpleNamespace(governance=_fake_ceiling()),
            ),
            patch(
                "kiro_crew.platform.governance.SCOPE_CATALOG",
                {"capabilities.telemetry": object()},
            ),
            patch(
                "kiro_crew.platform.governance_profiles.resolve_active_scope",
                return_value=SimpleNamespace(name="prof"),
            ),
            patch("kiro_crew.platform.governance.resolve", return_value=decision),
            patch("kiro_crew.platform.governance.gate_decision", return_value=gate),
        ):
            cc._policy(
                _ns(
                    policy_action="explain",
                    scope="capabilities.telemetry",
                    item="send",
                    session_key="sess-1",
                    agent="a",
                    app=None,
                )
            )
        out = capsys.readouterr().out
        assert "DENIED: capabilities.telemetry" in out
        assert "active profile : prof" in out
        assert "deny / policy" in out
        assert "gate verdict   : ALLOWED" in out

    def test_explain_without_active_profile(self, capsys: pytest.CaptureFixture[str]) -> None:
        with (
            patch(
                "kiro_crew.platform.context.current_context",
                return_value=SimpleNamespace(governance=None),
            ),
            patch("kiro_crew.platform.governance.SCOPE_CATALOG", {"s": object()}),
            patch(
                "kiro_crew.platform.governance_profiles.resolve_active_scope",
                return_value=None,
            ),
            patch(
                "kiro_crew.platform.governance.resolve",
                return_value=SimpleNamespace(
                    permitted=True, rule="allow", layer=None, reason="default"
                ),
            ),
            patch(
                "kiro_crew.platform.governance.gate_decision",
                return_value=SimpleNamespace(permitted=True, reason="ok"),
            ),
        ):
            cc._policy(
                _ns(
                    policy_action="explain",
                    scope="s",
                    item="i",
                    session_key="k",
                    agent=None,
                    app=None,
                )
            )
        out = capsys.readouterr().out
        assert "ALLOWED: s" in out and "(none — policy only)" in out and "allow / —" in out

    def test_profile_missing(self, capsys: pytest.CaptureFixture[str]) -> None:
        with (
            patch(
                "kiro_crew.platform.context.current_context",
                return_value=SimpleNamespace(governance=None),
            ),
            patch("kiro_crew.platform.governance_profiles.get_store_profile", return_value=None),
        ):
            cc._policy(_ns(policy_action="profile", name="ghost"))
        assert "No profile named 'ghost'" in capsys.readouterr().out

    def test_profile_prints_bind_and_scopes(self, capsys: pytest.CaptureFixture[str]) -> None:
        prof = SimpleNamespace(
            name="team",
            bind=SimpleNamespace(type="agent", id="kirocrew"),
            extends="base",
            controls={"capabilities.telemetry": "off"},
        )
        with (
            patch(
                "kiro_crew.platform.context.current_context",
                return_value=SimpleNamespace(governance=None),
            ),
            patch("kiro_crew.platform.governance_profiles.get_store_profile", return_value=prof),
        ):
            cc._policy(_ns(policy_action="profile", name="team"))
        out = capsys.readouterr().out
        assert "bind=agent:kirocrew" in out and "extends=base" in out
        assert "capabilities.telemetry: off" in out

    def test_profile_unbound_with_no_controls(self, capsys: pytest.CaptureFixture[str]) -> None:
        prof = SimpleNamespace(name="empty", bind=None, extends=None, controls={})
        with (
            patch(
                "kiro_crew.platform.context.current_context",
                return_value=SimpleNamespace(governance=None),
            ),
            patch("kiro_crew.platform.governance_profiles.get_store_profile", return_value=prof),
        ):
            cc._policy(_ns(policy_action="profile", name="empty"))
        out = capsys.readouterr().out
        assert "(unbound)" in out and "no governed scopes" in out

    def test_unknown_action_prints_usage(self, capsys: pytest.CaptureFixture[str]) -> None:
        with patch(
            "kiro_crew.platform.context.current_context",
            return_value=SimpleNamespace(governance=None),
        ):
            cc._policy(_ns(policy_action=None))
        assert "Usage: kirocrew policy" in capsys.readouterr().out
