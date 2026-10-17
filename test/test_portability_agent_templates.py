"""Agent templates in a crew config export: opt-in, values redacted."""

from __future__ import annotations

import io
import json
import os
import zipfile
from pathlib import Path
from unittest.mock import patch

import pytest

from kiro_crew import portability

AGENTS_DIR = portability.AGENT_TEMPLATES_DIR

#: Stand-ins for the values a template's MCP servers carry; obviously not real.
FAKE_ENV_VALUE = "fake-env-value-for-tests"
FAKE_HEADER_VALUE = "fake-header-value-for-tests"


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A minimal install whose crews name two templates, one of which names a third."""
    mc = tmp_path / ".kirocrew"
    mc.mkdir()
    (mc / "config.json").write_text(
        json.dumps(
            {
                "agent": {"default_agent": "kirocrew"},
                "agents": {
                    "alpha": {"kiro_agent": "researcher"},
                    "beta": {"kiro_agent": "by-declared-name"},
                    "gamma": {"kiro_agent": "only-on-the-source"},
                },
            }
        ),
        encoding="utf-8",
    )
    agents = tmp_path / "kiro_agents"
    agents.mkdir()
    (agents / "researcher.json").write_text(
        json.dumps(
            {
                "name": "researcher",
                "allowedTools": ["@governed-server/search", "@free-server"],
                "mcpServers": {
                    "search": {
                        "command": "search-mcp",
                        "env": {"SEARCH_TOKEN": FAKE_ENV_VALUE},
                        "headers": {"Authorization": FAKE_HEADER_VALUE},
                    },
                    "odd": "not-an-object",
                },
                "toolsSettings": {
                    "subagent": {
                        "availableAgents": ["helper", "help*"],
                        "trustedAgents": ["researcher"],
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    (agents / "helper.json").write_text(
        json.dumps(
            {
                "name": "helper",
                "mcpServers": {"web": {"url": "https://example.invalid/mcp", "headers": "flat"}},
                "toolsSettings": {"subagent": {"availableAgents": ["researcher"]}},
            }
        ),
        encoding="utf-8",
    )
    (agents / "file.json").write_text(json.dumps({"name": "by-declared-name"}), encoding="utf-8")
    (agents / "unrelated.json").write_text(json.dumps({"name": "unrelated"}), encoding="utf-8")
    (agents / "kirocrew.json").write_text(json.dumps({"name": "kirocrew"}), encoding="utf-8")
    monkeypatch.setattr(portability, "kiro_agents_dir", lambda: agents)
    with patch("kiro_crew.portability.config_dir", return_value=mc):
        with patch.dict(os.environ, {"KIROCREW_HOME": str(mc)}):
            yield mc


def _members(zip_bytes: bytes) -> dict[str, bytes]:
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        return {info.filename.split("/", 1)[1]: zf.read(info) for info in zf.infolist()}


def _stable(manifest: dict) -> dict:
    return {k: v for k, v in manifest.items() if k != "created_at"}


class TestExportOff:
    def test_off_by_default_and_unchanged(self, home):
        default_bytes, default_manifest = portability.create_export_zip()
        off_bytes, off_manifest = portability.create_export_zip(include_agent_templates=False)
        default_members = _members(default_bytes)
        off_members = _members(off_bytes)
        assert sorted(default_members) == sorted(off_members)
        assert not any(name.startswith("agents/") for name in default_members)
        assert "agent_templates" not in default_manifest
        assert "agent_template_count" not in default_manifest["contents"]
        assert _stable(default_manifest) == _stable(off_manifest)
        for name, data in default_members.items():
            if name != "MANIFEST.json":
                assert off_members[name] == data, name

    def test_memory_only_export_carries_no_template(self, home):
        zip_bytes, manifest = portability.create_export_zip(
            memory_only=True, include_agent_templates=True
        )
        assert not any(name.startswith("agents/") for name in _members(zip_bytes))
        assert "agent_templates" not in manifest


class TestExportOn:
    def test_carries_named_templates_and_the_ones_they_name(self, home):
        zip_bytes, manifest = portability.create_export_zip(include_agent_templates=True)
        members = _members(zip_bytes)
        carried = sorted(n for n in members if n.startswith("agents/"))
        assert carried == ["agents/file.json", "agents/helper.json", "agents/researcher.json"]
        assert manifest["agent_templates"] == ["file", "helper", "researcher"]
        assert manifest["contents"]["agent_template_count"] == 3

    def test_env_and_header_values_are_placeholders(self, home):
        zip_bytes, _ = portability.create_export_zip(include_agent_templates=True)
        members = _members(zip_bytes)
        for data in members.values():
            assert FAKE_ENV_VALUE.encode() not in data
            assert FAKE_HEADER_VALUE.encode() not in data
        spec = json.loads(members["agents/researcher.json"])
        server = spec["mcpServers"]["search"]
        placeholder = portability.AGENT_TEMPLATE_SECRET_PLACEHOLDER
        assert server["env"] == {"SEARCH_TOKEN": placeholder}
        assert server["headers"] == {"Authorization": placeholder}
        assert server["command"] == "search-mcp"
        assert spec["mcpServers"]["odd"] == "not-an-object"
        helper = json.loads(members["agents/helper.json"])
        assert helper["mcpServers"]["web"]["headers"] == {}

    def test_redaction_leaves_the_source_spec_untouched(self, home, tmp_path):
        portability.create_export_zip(include_agent_templates=True)
        source = json.loads((tmp_path / "kiro_agents" / "researcher.json").read_text())
        assert source["mcpServers"]["search"]["env"] == {"SEARCH_TOKEN": FAKE_ENV_VALUE}

    def test_linked_spec_is_not_carried(self, home, tmp_path):
        agents = tmp_path / "kiro_agents"
        outside = tmp_path / "outside.json"
        outside.write_text(json.dumps({"name": "linked"}), encoding="utf-8")
        try:
            (agents / "linked.json").symlink_to(outside)
        except OSError:
            pytest.skip("symlinks unavailable")
        cfg = json.loads((home / "config.json").read_text(encoding="utf-8"))
        cfg["agents"]["delta"] = {"kiro_agent": "linked"}
        (home / "config.json").write_text(json.dumps(cfg), encoding="utf-8")
        assert "linked" not in [stem for stem, _ in portability.bundled_agent_templates()[0]]

    def test_walk_is_bounded_and_counts_what_the_cap_left_out(self, home, tmp_path, monkeypatch):
        # Crews name researcher and file; researcher names helper, which no crew names.
        monkeypatch.setattr(portability, "MAX_BUNDLED_AGENT_TEMPLATES", 2)
        carried, past_cap = portability.bundled_agent_templates()
        assert [stem for stem, _ in carried] == ["file", "researcher"]
        assert past_cap == 1
        zip_bytes, manifest = portability.create_export_zip(include_agent_templates=True)
        assert manifest["contents"]["agent_templates_past_cap"] == 1
        assert "agents/helper.json" not in _members(zip_bytes)
        monkeypatch.setattr(portability, "MAX_BUNDLED_AGENT_TEMPLATES", 1)
        carried, past_cap = portability.bundled_agent_templates()
        assert len(carried) == 1
        # The crew-named template left out is warned about by name, so only helper counts.
        assert past_cap == 1

    def test_nothing_left_out_adds_no_manifest_key(self, home):
        _zip, manifest = portability.create_export_zip(include_agent_templates=True)
        assert "agent_templates_past_cap" not in manifest["contents"]

    def test_a_non_string_declared_name_past_the_cap_counts_as_unnamed(
        self, home, tmp_path, monkeypatch
    ):
        helper = tmp_path / "kiro_agents" / "helper.json"
        spec = json.loads(helper.read_text(encoding="utf-8"))
        spec["name"] = []
        helper.write_text(json.dumps(spec), encoding="utf-8")
        monkeypatch.setattr(portability, "MAX_BUNDLED_AGENT_TEMPLATES", 2)
        carried, past_cap = portability.bundled_agent_templates()
        assert [stem for stem, _ in carried] == ["file", "researcher"]
        assert past_cap == 1

    def test_an_oauth_client_secret_is_a_placeholder(self, home, tmp_path):
        spec_path = tmp_path / "kiro_agents" / "helper.json"
        spec = json.loads(spec_path.read_text(encoding="utf-8"))
        spec["mcpServers"]["web"]["oauth"] = {"clientId": "id-1", "clientSecret": FAKE_ENV_VALUE}
        spec_path.write_text(json.dumps(spec), encoding="utf-8")
        zip_bytes, _ = portability.create_export_zip(include_agent_templates=True)
        members = _members(zip_bytes)
        assert all(FAKE_ENV_VALUE.encode() not in data for data in members.values())
        oauth = json.loads(members["agents/helper.json"])["mcpServers"]["web"]["oauth"]
        assert oauth == {
            "clientId": "id-1",
            "clientSecret": portability.AGENT_TEMPLATE_SECRET_PLACEHOLDER,
        }

    def test_a_carried_template_is_not_warned_about_by_stem_or_declared_name(self, home):
        # "beta" names the spec in file.json by its declared name.
        assert portability.unbundled_agent_templates() == (
            ["by-declared-name", "only-on-the-source", "researcher"],
            0,
        )
        assert portability.unbundled_agent_templates(["file", "researcher"]) == (
            ["only-on-the-source"],
            0,
        )

    def test_the_left_out_count_covers_only_templates_still_left_out(self, home, monkeypatch):
        monkeypatch.setattr(portability, "MAX_TEMPLATE_WARNINGS", 1)
        assert portability.unbundled_agent_templates() == (["by-declared-name"], 2)
        assert portability.unbundled_agent_templates(["file"]) == (["only-on-the-source"], 1)


def _bundle(tmp_path, home, monkeypatch, *, include: bool = True) -> Path:
    """Export from *home*, then empty its agents directory to stand for the target."""
    zip_bytes, _ = portability.create_export_zip(include_agent_templates=include)
    path = tmp_path / "bundle.zip"
    path.write_bytes(zip_bytes)
    target = tmp_path / "target_agents"
    target.mkdir()
    monkeypatch.setattr(portability, "kiro_agents_dir", lambda: target)
    return path


class TestImport:
    def test_installs_nothing_unless_picked(self, home, tmp_path, monkeypatch):
        bundle = _bundle(tmp_path, home, monkeypatch)
        summary = portability.apply_import_zip(bundle, "merge")
        assert list((tmp_path / "target_agents").iterdir()) == []
        assert "agent_templates_installed" not in summary
        assert not (home / AGENTS_DIR).exists()

    def test_installs_only_the_picked_templates(self, home, tmp_path, monkeypatch):
        bundle = _bundle(tmp_path, home, monkeypatch)
        summary = portability.apply_import_zip(bundle, "merge", agent_templates=("researcher",))
        target = tmp_path / "target_agents"
        assert sorted(p.name for p in target.iterdir()) == ["researcher.json"]
        installed = json.loads((target / "researcher.json").read_text(encoding="utf-8"))
        placeholder = portability.AGENT_TEMPLATE_SECRET_PLACEHOLDER
        assert installed["mcpServers"]["search"]["env"] == {"SEARCH_TOKEN": placeholder}
        assert summary["agent_templates_installed"] == ["researcher"]
        assert summary["agent_templates_skipped"] == []
        assert "agent templates (1 installed)" in summary["items"]

    def test_an_installed_template_passes_this_hosts_governance_ceiling(
        self, home, tmp_path, monkeypatch
    ):
        from kiro_crew.platform import governance as gov

        bundle = _bundle(tmp_path, home, monkeypatch)
        monkeypatch.setattr(gov, "may_skip_gate_now", lambda ref: not ref.startswith("@governed"))
        portability.apply_import_zip(bundle, "merge", agent_templates=("researcher",))
        written = json.loads(
            (tmp_path / "target_agents" / "researcher.json").read_text(encoding="utf-8")
        )
        assert written["allowedTools"] == ["@free-server"]

    def test_replace_mode_installs_picked_and_writes_nothing_under_the_home(
        self, home, tmp_path, monkeypatch
    ):
        bundle = _bundle(tmp_path, home, monkeypatch)
        summary = portability.apply_import_zip(bundle, "replace", agent_templates=("helper",))
        assert summary["agent_templates_installed"] == ["helper"]
        assert not (home / AGENTS_DIR).exists()

    def test_same_name_template_is_skipped_untouched_and_named(self, home, tmp_path, monkeypatch):
        bundle = _bundle(tmp_path, home, monkeypatch)
        target = tmp_path / "target_agents"
        (target / "researcher.json").write_text("local copy", encoding="utf-8")
        (target / "other.json").write_text(json.dumps({"name": "helper"}), encoding="utf-8")
        summary = portability.apply_import_zip(
            bundle, "merge", agent_templates=("researcher", "helper", "file", "absent")
        )
        assert (target / "researcher.json").read_text(encoding="utf-8") == "local copy"
        assert not (target / "helper.json").exists()
        assert summary["agent_templates_installed"] == ["file"]
        assert summary["agent_templates_skipped"] == [
            {"name": "absent", "reason": "not_in_archive"},
            {"name": "helper", "reason": "exists"},
            {"name": "researcher", "reason": "exists"},
        ]

    def test_a_template_created_mid_import_is_not_overwritten(self, home, tmp_path, monkeypatch):
        bundle = _bundle(tmp_path, home, monkeypatch)
        target = tmp_path / "target_agents"
        monkeypatch.setattr(portability, "_installed_template_names", lambda: set())
        (target / "helper.json").write_text("raced in", encoding="utf-8")
        summary = portability.apply_import_zip(bundle, "merge", agent_templates=("helper",))
        assert (target / "helper.json").read_text(encoding="utf-8") == "raced in"
        assert summary["agent_templates_skipped"] == [{"name": "helper", "reason": "exists"}]

    def test_old_bundle_imports_as_before(self, home, tmp_path, monkeypatch):
        bundle = _bundle(tmp_path, home, monkeypatch, include=False)
        summary = portability.apply_import_zip(bundle, "merge", agent_templates=("researcher",))
        assert list((tmp_path / "target_agents").iterdir()) == []
        assert summary["agent_templates_installed"] == []
        assert summary["agent_templates_skipped"] == [
            {"name": "researcher", "reason": "not_in_archive"}
        ]
        plain = portability.apply_import_zip(bundle, "merge")
        assert "agent_templates_installed" not in plain
        assert "agent_templates_skipped" not in plain

    def test_preview_lists_bundled_templates_and_which_exist(self, home, tmp_path, monkeypatch):
        bundle = _bundle(tmp_path, home, monkeypatch)
        (tmp_path / "target_agents" / "helper.json").write_text("{}", encoding="utf-8")
        assert portability.preview_agent_templates(bundle) == (
            [
                {"name": "file", "exists": False},
                {"name": "helper", "exists": True},
                {"name": "researcher", "exists": False},
            ],
            0,
        )

    def test_unsafe_or_oversized_members_are_not_installed(self, home, tmp_path, monkeypatch):
        bundle = tmp_path / "crafted.zip"
        good, _ = portability.create_export_zip()
        with zipfile.ZipFile(io.BytesIO(good)) as src, zipfile.ZipFile(bundle, "w") as dst:
            prefix = src.namelist()[0].split("/", 1)[0]
            for info in src.infolist():
                dst.writestr(info, src.read(info))
            dst.writestr(f"{prefix}/agents/big.json", "{" + " " * (2 * 1024 * 1024) + "}")
            dst.writestr(f"{prefix}/agents/list.json", "[1]")
            dst.writestr(f"{prefix}/agents/.hidden.json", "{}")
        target = tmp_path / "target_agents"
        target.mkdir()
        monkeypatch.setattr(portability, "kiro_agents_dir", lambda: target)
        summary = portability.apply_import_zip(
            bundle, "merge", agent_templates=("big", "list", ".hidden")
        )
        assert list(target.iterdir()) == []
        assert summary["agent_templates_skipped"] == [
            {"name": ".hidden", "reason": "not_in_archive"},
            {"name": "big", "reason": "invalid"},
            {"name": "list", "reason": "invalid"},
        ]

    def test_a_corrupt_template_stream_is_skipped_not_raised(self, home, tmp_path, monkeypatch):
        good, _ = portability.create_export_zip()
        buf = io.BytesIO()
        with (
            zipfile.ZipFile(io.BytesIO(good)) as src,
            zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as dst,
        ):
            prefix = src.namelist()[0].split("/", 1)[0]
            for info in src.infolist():
                dst.writestr(info, src.read(info))
            dst.writestr(f"{prefix}/agents/broken.json", json.dumps({"name": "broken"}) * 50)
        raw = bytearray(buf.getvalue())
        with zipfile.ZipFile(io.BytesIO(bytes(raw))) as zf:
            info = zf.getinfo(f"{prefix}/agents/broken.json")
        start = info.header_offset + 30 + len(info.filename.encode()) + len(info.extra)
        # BFINAL=1 with the reserved block type 11: an invalid deflate stream.
        raw[start] = 0xFF
        bundle = tmp_path / "corrupt.zip"
        bundle.write_bytes(bytes(raw))
        target = tmp_path / "target_agents"
        target.mkdir()
        monkeypatch.setattr(portability, "kiro_agents_dir", lambda: target)
        assert portability.preview_agent_templates(bundle) == ([], 0)
        summary = portability.apply_import_zip(bundle, "merge", agent_templates=("broken",))
        assert list(target.iterdir()) == []
        assert summary["agent_templates_skipped"] == [{"name": "broken", "reason": "invalid"}]

    def test_templates_past_the_cap_are_counted_and_named_over_limit(
        self, home, tmp_path, monkeypatch
    ):
        bundle = _bundle(tmp_path, home, monkeypatch)
        monkeypatch.setattr(portability, "MAX_BUNDLED_AGENT_TEMPLATES", 2)
        rows, more = portability.preview_agent_templates(bundle)
        assert [row["name"] for row in rows] == ["file", "helper"]
        assert more == 1
        summary = portability.apply_import_zip(
            bundle, "merge", agent_templates=("researcher", "absent")
        )
        assert summary["agent_templates_skipped"] == [
            {"name": "absent", "reason": "not_in_archive"},
            {"name": "researcher", "reason": "over_limit"},
        ]
        assert list((tmp_path / "target_agents").iterdir()) == []

    def test_memory_only_import_installs_none(self, home, tmp_path, monkeypatch):
        bundle = _bundle(tmp_path, home, monkeypatch)
        summary = portability.apply_import_zip(
            bundle, "merge", memory_only=True, agent_templates=("researcher",)
        )
        assert list((tmp_path / "target_agents").iterdir()) == []
        assert "agent_templates_installed" not in summary
