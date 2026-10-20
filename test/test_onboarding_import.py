"""Onboarding import: source detection, the Gemini home, and Codex and OpenClaw preview.

``TestSourceDetection`` pins where ``detect_sources`` finds each home: OpenClaw's state
directory, profiles and legacy root, the Windows ``USERPROFILE`` home, and Hermes under
``LOCALAPPDATA``. ``TestGeminiSource`` pins the ``~/.gemini`` home that Gemini CLI and
Antigravity share: MCP shapes, project-file workspaces, refused relative, overlong, UNC and
malformed paths, and credential diagnostics. ``TestPreview`` pins what ``preview_import``
counts, classifies and refuses for Codex and OpenClaw homes, the counts across several homes
at once, and that an explicitly empty selection imports nothing. The predecessor, Claude
Code and Hermes previews live in
``test_onboarding_import_preview_predecessor_claude_hermes.py``; ``apply_import``'s
per-category writes in ``test_onboarding_import_apply_categories.py``; the import's merge,
parsing and transcript floors in ``test_onboarding_import_safety_floors.py``; instruction
import and destination conflicts in
``test_onboarding_import_instructions_and_conflicts.py``; the shared fixture and helpers in
``onboarding_import_helpers.py``.
"""

from __future__ import annotations

import importlib
import json
import sqlite3
from pathlib import Path
from types import ModuleType

import pytest
from onboarding_import_helpers import (  # noqa: F401 -- the autouse fixture registers by import
    _categories,
    _register_predecessor_source,
    _select,
    _source,
    _write_jsonl,
    _write_openclaw_session,
)

from kiro_crew import onboarding_plan, onboarding_scan, onboarding_sources, platform_compat
from kiro_crew.onboarding_sources import gemini as gemini_source
from kiro_crew.platform_compat import IS_WINDOWS

# Keep apply_import's MCP sidecar lock off the real ~/.kiro/settings (see the
# shared fixture in test/conftest.py).
pytestmark = pytest.mark.usefixtures("isolate_mcp_host_paths")


def _api() -> ModuleType:
    try:
        return importlib.import_module("kiro_crew.onboarding_import")
    except ModuleNotFoundError:
        pytest.fail("kiro_crew.onboarding_import is not implemented")


class TestSourceDetection:
    def test_openclaw_home_uses_dot_openclaw_but_state_dir_is_exact(self, tmp_path: Path) -> None:
        openclaw_home = tmp_path / "openclaw-home"
        home_state = openclaw_home / ".openclaw"
        exact_state = tmp_path / "openclaw-state"
        home_state.mkdir(parents=True)
        exact_state.mkdir()

        home_result = _api().detect_sources(
            home=tmp_path / "unused-home",
            env={"OPENCLAW_HOME": str(openclaw_home)},
        )
        state_result = _api().detect_sources(
            home=tmp_path / "unused-home",
            env={"OPENCLAW_STATE_DIR": str(exact_state)},
        )

        assert _source(home_result, "openclaw")["root"] == str(home_state)
        assert _source(state_result, "openclaw")["root"] == str(exact_state)

    def test_userprofile_is_a_windows_home_fallback(self, tmp_path: Path) -> None:
        windows_home = tmp_path / "Users" / "Ada"
        (windows_home / ".codex").mkdir(parents=True)
        (windows_home / ".claude").mkdir()

        result = _api().detect_sources(
            env={"USERPROFILE": str(windows_home), "HOMEDRIVE": "C:", "HOMEPATH": "\\Users\\Ada"}
        )

        assert _source(result, "codex")["root"] == str(windows_home / ".codex")
        assert _source(result, "claude_code")["root"] == str(windows_home / ".claude")

    def test_windows_prefers_userprofile_when_home_is_also_set(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        api = _api()
        posix_home = tmp_path / "home-from-shell"
        windows_home = tmp_path / "Users" / "Ada"
        (posix_home / ".codex").mkdir(parents=True)
        (windows_home / ".codex").mkdir(parents=True)
        monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)

        result = api.detect_sources(
            env={
                "HOME": str(posix_home),
                "USERPROFILE": str(windows_home),
            }
        )

        assert _source(result, "codex")["root"] == str(windows_home / ".codex")

    def test_openclaw_legacy_home_fallback(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        legacy_root = home / ".clawdbot"
        legacy_root.mkdir(parents=True)

        result = _api().detect_sources(home=home, env={})

        assert _source(result, "openclaw")["root"] == str(legacy_root)

    def test_openclaw_does_not_discover_undocumented_moltbot_root(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        (home / ".moltbot").mkdir(parents=True)

        result = _api().detect_sources(home=home, env={})

        assert not any(source["id"] == "openclaw" for source in result["sources"])

    def test_openclaw_profile_selects_profile_state_and_default_workspace(
        self, tmp_path: Path
    ) -> None:
        home = tmp_path / "home"
        state = home / ".openclaw-review"
        workspace = home / ".openclaw" / "workspace-review"
        skill = workspace / "skills" / "review"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("# Profile review\n", encoding="utf-8")
        (workspace / "MEMORY.md").write_text(
            "Remember the profile workspace.",
            encoding="utf-8",
        )
        state.mkdir(parents=True)
        (state / "openclaw.json").write_text("{}\n", encoding="utf-8")

        result = _api().detect_sources(
            home=home,
            env={"OPENCLAW_PROFILE": "review"},
        )

        assert _source(result, "openclaw")["root"] == str(state)
        assert _categories(result, "openclaw") == {
            "memories": 1,
            "skills": 1,
            "workspaces": 1,
        }

    def test_openclaw_profile_is_normalized_for_state_discovery(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        state = home / ".openclaw-review"
        state.mkdir(parents=True)

        result = _api().detect_sources(
            home=home,
            env={"OPENCLAW_PROFILE": "Review"},
        )

        assert _source(result, "openclaw")["root"] == str(state)

    def test_openclaw_default_profile_uses_unprofiled_state(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        state = home / ".openclaw"
        state.mkdir(parents=True)

        result = _api().detect_sources(
            home=home,
            env={"OPENCLAW_PROFILE": "default"},
        )

        assert _source(result, "openclaw")["root"] == str(state)

    def test_hermes_prefers_localappdata_on_windows_style_home(self, tmp_path: Path) -> None:
        windows_home = tmp_path / "Users" / "Ada"
        local_app_data = tmp_path / "AppData" / "Local"
        hermes = local_app_data / "hermes"
        hermes.mkdir(parents=True)

        result = _api().detect_sources(
            env={
                "USERPROFILE": str(windows_home),
                "LOCALAPPDATA": str(local_app_data),
            }
        )

        assert _source(result, "hermes")["root"] == str(hermes)


class TestGeminiSource:
    """Gemini CLI and its Antigravity successor share the ``~/.gemini`` home."""

    def test_one_home_covers_both_gemini_cli_and_antigravity(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        root = home / ".gemini"
        workspace = tmp_path / "work" / "proj"
        workspace.mkdir(parents=True)
        (root / "antigravity").mkdir(parents=True)
        (root / "settings.json").write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "notes": {"command": "npx", "args": ["-y", "notes-mcp"]},
                        "remote": {"httpUrl": "https://mcp.example.com/mcp/"},
                    },
                    "projects": {str(workspace): {}},
                }
            ),
            encoding="utf-8",
        )
        (root / "antigravity" / "mcp_config.json").write_text(
            json.dumps({"mcpServers": {"tickets": {"command": "tickets-mcp"}}}),
            encoding="utf-8",
        )
        (root / gemini_source._GEMINI_CONTEXT_FILENAME).write_text(
            "Always run the linter before committing.\n", encoding="utf-8"
        )

        result = _api().detect_sources(home=home, env={})

        source = _source(result, "gemini")
        assert source["name"] == "Gemini CLI / Antigravity"
        assert source["root"] == str(root)
        counts = _categories(result, "gemini")
        # Two stdio servers plus the httpUrl endpoint normalized to ``url``.
        assert counts["mcp_servers"] == 3
        assert counts["instructions"] == 1
        assert counts["workspaces"] == 1

    def test_http_url_rename_does_not_widen_credentials(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        root = home / ".gemini"
        root.mkdir(parents=True)
        (root / "settings.json").write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "guarded": {
                            "httpUrl": "https://api.example.com/mcp/",
                            "headers": {"Authorization": "token-value"},
                        }
                    }
                }
            ),
            encoding="utf-8",
        )

        result = _api().detect_sources(home=home, env={})

        assert _categories(result, "gemini").get("mcp_servers", 0) == 0

    def test_antigravity_home_override_wins_over_the_default_root(self, tmp_path: Path) -> None:
        root = tmp_path / "agy-data"
        root.mkdir()

        result = _api().detect_sources(
            home=tmp_path / "unused-home",
            env={"ANTIGRAVITY_HOME": str(root)},
        )

        assert _source(result, "gemini")["root"] == str(root)

    def test_declared_workspace_fan_out_is_capped(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        root = home / ".gemini"
        root.mkdir(parents=True)
        over_limit = onboarding_plan._MAX_WORKSPACES + 1
        (root / "settings.json").write_text(
            json.dumps(
                {"projects": {str(tmp_path / f"proj-{index}"): {} for index in range(over_limit)}}
            ),
            encoding="utf-8",
        )

        result = _api().detect_sources(home=home, env={})

        assert any(
            item["source_id"] == "gemini"
            and item["category_id"] == "workspaces"
            and item["reason"] == "item_count_limit"
            and item["count"] == over_limit
            for item in result["skipped"]
        )

    def test_relative_declared_workspace_is_refused(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        root = home / ".gemini"
        root.mkdir(parents=True)
        (root / "settings.json").write_text(
            json.dumps({"projects": {"relative/not/absolute": {}}}),
            encoding="utf-8",
        )

        result = _api().detect_sources(home=home, env={})

        assert _categories(result, "gemini").get("workspaces", 0) == 0
        assert any(
            item["source_id"] == "gemini"
            and item["category_id"] == "workspaces"
            and item["reason"] == "workspace_not_absolute"
            for item in result["skipped"]
        )

    def test_real_antigravity_mcp_shape_crosses_and_secrets_do_not(self, tmp_path: Path) -> None:
        """Pin the shapes a real ``~/.gemini/config/mcp_config.json`` writes.

        Captured from an actual Antigravity install: stdio entries carry a
        ``$typeName`` protobuf discriminator plus ``env`` (often empty), and
        remote entries use ``serverUrl`` rather than ``httpUrl``. Before these
        were handled, a real install imported ZERO of its servers.
        """
        home = tmp_path / "home"
        root = home / ".gemini"
        (root / "config").mkdir(parents=True)
        (root / "config" / "mcp_config.json").write_text(
            json.dumps(
                {
                    "mcpServers": {
                        # stdio: protobuf marker + empty env -> both dropped
                        "chrome-devtools-mcp": {
                            "$typeName": "exa.cascade_plugins_pb.CascadePluginCommandTemplate",
                            "command": "npx",
                            "args": ["-y", "chrome-devtools-mcp"],
                            "env": {},
                        },
                        # remote: serverUrl -> url
                        "antimetal": {"serverUrl": "https://mcp.antimetal.com/mcp"},
                        # populated env -> refused, credentials never cross
                        "github-mcp-server": {
                            "$typeName": "exa.cascade_plugins_pb.CascadePluginCommandTemplate",
                            "command": "docker",
                            "args": ["run", "-i", "--rm", "ghcr.io/github/github-mcp-server"],
                            "env": {"GITHUB_PERSONAL_ACCESS_TOKEN": "ghp_examplevalue"},
                        },
                        # unsupported auth mode -> refused rather than reshaped
                        "lovable": {
                            "serverUrl": "https://mcp.lovable.dev/mcp",
                            "authProviderType": "dynamic_discovery",
                        },
                    }
                }
            ),
            encoding="utf-8",
        )

        result = _api().detect_sources(home=home, env={})
        scan = onboarding_sources._scan_source("gemini", root, home)
        accepted = sorted(item.payload["name"] for item in scan.items["mcp_servers"])

        assert accepted == ["antimetal", "chrome-devtools-mcp"]
        assert _categories(result, "gemini")["mcp_servers"] == 2
        # The protobuf marker and the empty env must not survive onto the spec.
        by_name = {item.payload["name"]: item.payload["spec"] for item in scan.items["mcp_servers"]}
        # ``disabled`` is added by the importer — servers arrive switched off.
        assert by_name["chrome-devtools-mcp"] == {
            "command": "npx",
            "args": ["-y", "chrome-devtools-mcp"],
            "disabled": True,
        }
        assert by_name["antimetal"] == {
            "url": "https://mcp.antimetal.com/mcp",
            "disabled": True,
        }
        # No credential value may appear anywhere in the projected payloads.
        assert "ghp_examplevalue" not in json.dumps(
            [item.payload for item in scan.items["mcp_servers"]]
        )

    def test_antigravity_project_files_become_workspaces(self, tmp_path: Path) -> None:
        """Antigravity stores one project per file, as a percent-encoded file:// URI."""
        home = tmp_path / "home"
        root = home / ".gemini"
        (root / "config" / "projects").mkdir(parents=True)
        workspace = tmp_path / "Project" / "AWS Blocks"
        workspace.mkdir(parents=True)
        (root / "config" / "projects" / "bddafe2d-4682.json").write_text(
            json.dumps(
                {
                    "id": "bddafe2d-4682",
                    "name": "AWS Blocks",
                    "projectResources": {
                        "resources": [
                            {"folderUri": "file://" + str(workspace).replace(" ", "%20")}
                        ]
                    },
                }
            ),
            encoding="utf-8",
        )

        result = _api().detect_sources(home=home, env={})
        scan = onboarding_sources._scan_source("gemini", root, home)

        assert _categories(result, "gemini")["workspaces"] == 1
        assert [item.payload for item in scan.items["workspaces"]] == [str(workspace)]

    def test_overlong_workspace_path_does_not_raise(self, tmp_path: Path) -> None:
        """An over-long folderUri must be refused, not crash the scan.

        ``Path.exists()`` re-raises ENAMETOOLONG (pathlib only treats
        ENOENT/ENOTDIR/EBADF/ELOOP as "absent"), so a foreign-supplied path with a
        component over NAME_MAX escapes as an OSError and surfaces as HTTP
        500 from the scan endpoint — for every source, not just this one. The
        trigger is per-COMPONENT (255), so this fixture stays far below the 4096
        total ceiling to prove a total-length guard alone is insufficient.
        """
        home = tmp_path / "home"
        root = home / ".gemini"
        (root / "config" / "projects").mkdir(parents=True)
        long_component = "q" * 300
        # Anchor the over-long component under tmp_path and let ``as_uri`` build
        # the URI, so the decoded path is absolute on every platform: a rootless
        # "/qqq..." carries no drive on Windows, so _bounded_workspaces would
        # refuse it as workspace_not_absolute before ever reaching the length
        # check, and this test would pass for the wrong reason.
        overlong = tmp_path / long_component
        assert len(str(overlong)) < 4096
        (root / "config" / "projects" / "p.json").write_text(
            json.dumps(
                {"projectResources": {"resources": [{"folderUri": overlong.as_uri()}]}}
            ),
            encoding="utf-8",
        )

        result = _api().detect_sources(home=home, env={})

        assert _categories(result, "gemini").get("workspaces", 0) == 0
        assert any(
            item["source_id"] == "gemini"
            and item["category_id"] == "workspaces"
            and item["reason"] == "workspace_path_too_long"
            for item in result["skipped"]
        )

    def test_probe_helpers_absorb_unstattable_paths(self, tmp_path: Path) -> None:
        """The shared probe helpers must answer "absent" rather than raise.

        This is the layer that also protects the other scanners, which build
        candidate paths from their own foreign configs the same way.

        The raw-``Path`` precondition holds on POSIX only. Windows already folds
        an over-long name into "absent": pathlib's ``_ignore_error`` consults
        ``_IGNORED_WINERRORS``, which covers ERROR_INVALID_NAME (123) and
        ERROR_FILENAME_EXCED_RANGE (206), so there is no OSError to absorb there.
        The helpers' own contract is asserted on every platform.
        """
        unstattable = tmp_path / ("z" * 300) / "settings.json"

        if not IS_WINDOWS:
            with pytest.raises(OSError):
                unstattable.exists()

        assert onboarding_scan._exists_safe(unstattable) is False
        assert onboarding_scan._is_file_safe(unstattable) is False
        assert onboarding_scan._is_dir_safe(unstattable) is False

    def test_skills_dir_that_yields_nothing_is_reported(self, tmp_path: Path) -> None:
        """A skills dir with no SKILL.md package must surface a skip reason."""
        home = tmp_path / "home"
        root = home / ".gemini"
        (root / "skills" / "some-skill").mkdir(parents=True)
        (root / "skills" / "some-skill" / "instructions.txt").write_text("hi", encoding="utf-8")
        (root / gemini_source._GEMINI_CONTEXT_FILENAME).write_text("Run the linter.\n", encoding="utf-8")

        result = _api().detect_sources(home=home, env={})

        assert _categories(result, "gemini").get("skills", 0) == 0
        assert any(
            item["source_id"] == "gemini"
            and item["category_id"] == "skills"
            and item["reason"] == "unsupported_category"
            for item in result["skipped"]
        )

    def test_empty_and_malformed_configs_degrade_quietly(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        root = home / ".gemini"
        (root / "antigravity").mkdir(parents=True)
        (root / "settings.json").write_text("{ not json at all", encoding="utf-8")
        (root / "antigravity" / "mcp_config.json").write_text("{}", encoding="utf-8")

        result = _api().detect_sources(home=home, env={})

        source = _source(result, "gemini")
        assert source["root"] == str(root)
        assert _categories(result, "gemini") == {}

    def test_malformed_project_uri_is_refused_not_raised(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A folderUri that ``url2pathname`` cannot decode must skip, not 500.

        On Windows ``url2pathname`` raises OSError for a URI whose path
        component cannot map to a drive path (e.g. ``file:///::/x``); on POSIX
        it is plain unquoting and never raises for that input. Simulate the
        Windows failure on every platform by patching the module's binding, so
        the except path is exercised deterministically rather than only on the
        Windows shards.
        """
        api = _api()
        home = tmp_path / "home"
        root = home / ".gemini"
        (root / "config" / "projects").mkdir(parents=True)
        (root / "config" / "projects" / "p.json").write_text(
            json.dumps({"projectResources": {"resources": [{"folderUri": "file:///::/x"}]}}),
            encoding="utf-8",
        )

        def _raise(_: str) -> str:
            raise OSError("Bad URL")

        monkeypatch.setattr(gemini_source, "url2pathname", _raise)
        result = api.detect_sources(home=home, env={})

        assert _categories(result, "gemini").get("workspaces", 0) == 0
        assert any(
            item["source_id"] == "gemini"
            and item["category_id"] == "workspaces"
            and item["reason"] == "workspace_uri_invalid"
            for item in result["skipped"]
        )

    def test_unc_workspace_from_any_config_shape_is_refused(self, tmp_path: Path) -> None:
        """UNC paths must be refused at the shared chokepoint, not per entry point.

        A foreign config can name a network path through several shapes — a
        ``projects`` key, a workspace map, or a decoded project-file URI. A
        ``\\\\server\\share`` string IS absolute on Windows, so it passed the
        not-absolute refusal in ``_bounded_workspaces`` and reached the
        config probes, which on Windows open an SMB connection (and
        authenticate) to the named host. The refusal lives in
        ``_bounded_workspaces`` so EVERY entry point is covered at once.
        """
        home = tmp_path / "home"
        root = home / ".gemini"
        root.mkdir(parents=True)
        (root / "settings.json").write_text(
            json.dumps(
                {
                    "projects": {
                        "\\\\attacker\\share": {},
                        "//attacker2/share": {},
                    }
                }
            ),
            encoding="utf-8",
        )

        result = _api().detect_sources(home=home, env={})

        assert _categories(result, "gemini").get("workspaces", 0) == 0
        assert any(
            item["source_id"] == "gemini"
            and item["category_id"] == "workspaces"
            and item["reason"] == "network_workspace_excluded"
            for item in result["skipped"]
        )

    def test_unc_project_uri_is_refused_without_probing(self, tmp_path: Path) -> None:
        """A ``file:////server/share`` folderUri must never become a workspace.

        The decoded UNC path IS absolute on Windows, so it would pass the
        not-absolute refusal and reach the workspace/config probes — and
        merely probing a UNC path makes Windows open an SMB connection (and
        authenticate) to a host named by a foreign config file. It must be
        refused at decode time, before any candidate path is built.
        """
        home = tmp_path / "home"
        root = home / ".gemini"
        (root / "config" / "projects").mkdir(parents=True)
        (root / "config" / "projects" / "p.json").write_text(
            json.dumps(
                {
                    "projectResources": {
                        "resources": [{"folderUri": "file:////attacker/share"}]
                    }
                }
            ),
            encoding="utf-8",
        )

        result = _api().detect_sources(home=home, env={})

        assert _categories(result, "gemini").get("workspaces", 0) == 0
        assert any(
            item["source_id"] == "gemini"
            and item["category_id"] == "workspaces"
            and item["reason"] == "network_workspace_excluded"
            for item in result["skipped"]
        )

    def test_antigravity_config_wins_over_stale_gemini_settings(self, tmp_path: Path) -> None:
        """The live tool's definition must beat the retired tool's stale copy.

        ``_add_mcp_configs`` keeps the FIRST definition of a server name, so
        the root-config probe order is precedence. A user who migrated from
        Gemini CLI keeps a stale ``settings.json`` next to the Antigravity
        config; with legacy-first order the retired tool's definition was
        imported and the live one silently dropped.
        """
        home = tmp_path / "home"
        root = home / ".gemini"
        (root / "config").mkdir(parents=True)
        (root / "settings.json").write_text(
            json.dumps(
                {"mcpServers": {"shared": {"command": "stale-legacy-command"}}}
            ),
            encoding="utf-8",
        )
        (root / "config" / "mcp_config.json").write_text(
            json.dumps(
                {"mcpServers": {"shared": {"command": "live-antigravity-command"}}}
            ),
            encoding="utf-8",
        )

        scan = onboarding_sources._scan_source("gemini", root, home)

        by_name = {item.payload["name"]: item.payload["spec"] for item in scan.items["mcp_servers"]}
        assert by_name["shared"]["command"] == "live-antigravity-command"

    def test_empty_env_alone_yields_no_credential_diagnostic(self, tmp_path: Path) -> None:
        """An empty ``env`` must not report excluded credential fields.

        The key name matches ``_SECRET_KEY_RE``, so diagnosing the RAW config
        scored ``env: {}`` as a credential field even though normalization
        drops it and the server imports whole — a spurious "credentials
        excluded" row for an install that holds no secret at all. The
        diagnosis now runs on the normalized view; a POPULATED env (covered by
        ``test_real_antigravity_mcp_shape_crosses_and_secrets_do_not``) still
        fires it.
        """
        home = tmp_path / "home"
        root = home / ".gemini"
        (root / "config").mkdir(parents=True)
        (root / "config" / "mcp_config.json").write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "chrome-devtools-mcp": {
                            "command": "npx",
                            "args": ["-y", "chrome-devtools-mcp"],
                            "env": {},
                        }
                    }
                }
            ),
            encoding="utf-8",
        )

        result = _api().detect_sources(home=home, env={})

        assert _categories(result, "gemini")["mcp_servers"] == 1
        assert not any(
            item["source_id"] == "gemini" and item["reason"] == "credential_fields_excluded"
            for item in result["skipped"]
        )


class TestPreview:
    def test_codex_counts_supported_items_without_exposing_private_data(
        self, tmp_path: Path
    ) -> None:
        home = tmp_path / "home"
        codex = home / ".codex"
        project = tmp_path / "private" / "customer-project"
        project.mkdir(parents=True)
        secret = "sk-ant-api03-this-must-never-leave-preview"
        project_toml = str(project).replace("\\", "\\\\")
        codex.mkdir(parents=True, exist_ok=True)
        (codex / "config.toml").write_text(
            "\n".join(
                [
                    'model = "gpt-test"',
                    'approval_policy = "on-request"',
                    f'api_key = "{secret}"',
                    "[mcp_servers.local]",
                    'command = "local-mcp"',
                    f'env = {{ TOKEN = "{secret}" }}',
                    f'[projects."{project_toml}"]',
                    'trust_level = "trusted"',
                ]
            ),
            encoding="utf-8",
        )
        skill = codex / "skills" / "review"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("# Review\n", encoding="utf-8")

        plan = _api().preview_import(home=home, env={})
        counts = _categories(plan, "codex")
        serialized = json.dumps(plan)

        assert counts == {
            "workspaces": 1,
            "skills": 1,
        }
        assert set(counts) <= set(_api().CATEGORY_IDS)
        assert "credentials" not in counts
        assert any(
            item["source_id"] == "codex"
            and item["category_id"] == "credentials"
            and item["reason"] == "credential_fields_excluded"
            for item in plan["skipped"]
        )
        assert "telemetry" not in serialized
        assert secret not in serialized
        assert plan["secret_count"] >= 2

    def test_codex_archives_user_skills_and_unstable_memory_are_classified(
        self, tmp_path: Path
    ) -> None:
        home = tmp_path / "home"
        codex = home / ".codex"
        codex.mkdir(parents=True, exist_ok=True)
        (codex / "memories_1.sqlite").write_bytes(b"not a stable public schema")
        bundled_skill = codex / "skills" / ".system" / "bundled"
        bundled_skill.mkdir(parents=True)
        (bundled_skill / "SKILL.md").write_text("# Bundled\n", encoding="utf-8")
        user_skill = codex / "skills" / "mine"
        user_skill.mkdir(parents=True)
        (user_skill / "SKILL.md").write_text("# Mine\n", encoding="utf-8")

        plan = _api().preview_import(home=home, env={})

        assert _categories(plan, "codex") == {"skills": 1}
        assert plan["unsupported_count"] >= 1
        assert any(
            item["source_id"] == "codex"
            and item["category_id"] == "memories"
            and item["reason"] == "unstable_memory_store"
            for item in plan["skipped"]
        )

    def test_codex_override_keeps_skills_rooted_at_codex_home(self, tmp_path: Path) -> None:
        user_home = tmp_path / "user"
        codex_home = tmp_path / "overridden-codex"
        codex_home.mkdir()
        skill = codex_home / "skills" / "review"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("# Review\n", encoding="utf-8")

        plan = _api().preview_import(
            home=user_home,
            env={"CODEX_HOME": str(codex_home)},
        )

        assert _source(plan, "codex")["root"] == str(codex_home)
        assert _source(plan, "codex")["user_home"] == str(user_home)
        assert _categories(plan, "codex") == {"skills": 1}

    def test_codex_rrule_automations_are_diagnosed_not_approximated(self, tmp_path: Path) -> None:
        database = tmp_path / "home" / ".codex" / "sqlite" / "codex-dev.db"
        database.parent.mkdir(parents=True)
        with sqlite3.connect(database) as connection:
            connection.executescript(
                """
                CREATE TABLE automations (
                    id TEXT PRIMARY KEY,
                    name TEXT,
                    prompt TEXT,
                    rrule TEXT
                );
                INSERT INTO automations VALUES
                    ('daily', 'daily review', 'review the project', 'FREQ=DAILY;BYHOUR=9');
                """
            )

        plan = _api().preview_import(home=tmp_path / "home", env={})

        assert "schedules" not in _categories(plan, "codex")
        assert any(
            item["source_id"] == "codex"
            and item["category_id"] == "schedules"
            and item["reason"] == "unsupported_schedule_semantics"
            and item["count"] == 1
            for item in plan["skipped"]
        )

    def test_codex_automation_database_rejects_unsafe_sidecar_before_open(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        api = _api()
        database = tmp_path / "home" / ".codex" / "sqlite" / "codex-dev.db"
        database.parent.mkdir(parents=True)
        with sqlite3.connect(database) as connection:
            connection.execute("CREATE TABLE automations (id TEXT, rrule TEXT)")
        Path(f"{database}-wal").mkdir()

        def fail_if_opened(*_args: object, **_kwargs: object) -> None:
            raise AssertionError("unsafe Codex automation database was opened")

        monkeypatch.setattr(api.sqlite3, "connect", fail_if_opened)

        plan = api.preview_import(home=tmp_path / "home", env={})

        assert any(
            item["source_id"] == "codex"
            and item["category_id"] == "schedules"
            and item["reason"] == "unsafe_database_sidecar"
            for item in plan["skipped"]
        )

    def test_explicitly_empty_selection_imports_nothing(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        _write_jsonl(
            home / ".codex" / "sessions" / "a.jsonl",
            [{"role": "user", "content": "not selected"}],
        )
        plan = _api().preview_import(home=home, env={})
        plan["selection"] = []

        result = _api().apply_import(plan, data_home=tmp_path / "destination")

        assert result["imported_count"] == 0
        assert not (tmp_path / "destination" / "sessions").exists()

    def test_claude_openclaw_and_hermes_structures_are_counted(self, tmp_path: Path) -> None:
        home = tmp_path / "home"

        claude = home / ".claude"
        project_dir = claude / "projects" / "-work-demo"
        claude_workspace = tmp_path / "private-project"
        claude_workspace.mkdir()
        _write_jsonl(
            project_dir / "session.jsonl",
            [{"type": "user", "message": {"role": "user", "content": "hello"}}],
        )
        (project_dir / "memory").mkdir()
        (project_dir / "memory" / "MEMORY.md").write_text("Remember this.", encoding="utf-8")
        (claude / "skills" / "writer").mkdir(parents=True)
        (claude / "skills" / "writer" / "SKILL.md").write_text("# Writer\n", encoding="utf-8")
        (home / ".claude.json").write_text(
            json.dumps(
                {
                    "projects": {str(claude_workspace): {}},
                    "mcpServers": {"claude-helper": {"command": "claude-helper"}},
                }
            ),
            encoding="utf-8",
        )

        openclaw = home / ".openclaw"
        _write_openclaw_session(openclaw, session_id="legacy")
        (openclaw / "cron").mkdir()
        (openclaw / "cron" / "jobs.json").write_text(
            json.dumps(
                {
                    "jobs": [
                        {
                            "name": "legacy check",
                            "message": "check status",
                            "schedule": {"kind": "cron", "cron_expr": "0 9 * * *"},
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        (openclaw / "openclaw.json").write_text(
            """
            {
              // JSON5 comment
              mcpServers: {helper: {command: 'open-helper',},},
              timezone: "UTC",
            }
            """,
            encoding="utf-8",
        )

        hermes = home / ".hermes"
        hermes.mkdir()
        hermes_workspace = tmp_path / "hermes-project"
        hermes_workspace.mkdir()
        with sqlite3.connect(hermes / "hermes.db") as connection:
            connection.executescript(
                f"""
                CREATE TABLE sessions (
                    id TEXT PRIMARY KEY,
                    source TEXT,
                    parent_session_id TEXT,
                    cwd TEXT
                );
                CREATE TABLE messages (
                    id INTEGER PRIMARY KEY,
                    session_id TEXT,
                    role TEXT,
                    content TEXT
                );
                INSERT INTO sessions VALUES
                    ('c1', 'cli', NULL, '{hermes_workspace.as_posix()}');
                INSERT INTO messages(session_id, role, content)
                    VALUES ('c1', 'user', 'database hello');
                """
            )
        (hermes / "memory").mkdir()
        (hermes / "memory" / "notes.md").write_text("Hermes memory.", encoding="utf-8")
        (hermes / "skills" / "helper").mkdir(parents=True)
        (hermes / "skills" / "helper" / "SKILL.md").write_text("# Helper\n", encoding="utf-8")
        (hermes / "cron").mkdir()
        (hermes / "cron" / "morning.md").write_text(
            "---\nname: morning\nschedule: 0 8 * * *\n---\nPrepare a digest.\n",
            encoding="utf-8",
        )
        (hermes / "config.yaml").write_text(
            "timezone: America/Los_Angeles\n"
            "mcp_servers:\n"
            "  helper:\n"
            "    command: hermes-mcp\n",
            encoding="utf-8",
        )

        plan = _api().preview_import(home=home, env={})

        assert _categories(plan, "claude_code") == {
            "memories": 1,
            "workspaces": 1,
            "mcp_servers": 1,
            "skills": 1,
        }
        assert _categories(plan, "openclaw") == {
            "mcp_servers": 1,
            "schedules": 1,
            "settings": 1,
        }
        # With transcripts out of scope, Hermes workspaces come only from
        # ``projects.db`` and explicit config, and this fixture has neither.
        assert _categories(plan, "hermes") == {
            "mcp_servers": 1,
            "skills": 1,
            "settings": 1,
        }

    def test_openclaw_nested_mcp_and_current_session_db_are_classified(
        self, tmp_path: Path
    ) -> None:
        openclaw = tmp_path / "home" / ".clawdbot"
        openclaw.mkdir(parents=True)
        (openclaw / "openclaw.json").write_text(
            json.dumps({"mcp": {"servers": {"helper": {"command": "open-helper"}}}}),
            encoding="utf-8",
        )
        session_db = openclaw / "agents" / "main" / "agent" / "openclaw-agent.sqlite"
        session_db.parent.mkdir(parents=True)
        with sqlite3.connect(session_db) as connection:
            connection.execute("CREATE TABLE current_sessions (payload BLOB)")

        plan = _api().preview_import(home=tmp_path / "home", env={})

        assert _categories(plan, "openclaw") == {"mcp_servers": 1}
        # Conversation history is not an import category, so the agent's session
        # database is neither opened nor reported.
        assert not any(item["category_id"] == "sessions" for item in plan["skipped"])

    def test_openclaw_reads_explicit_and_legacy_config_paths(self, tmp_path: Path) -> None:
        state = tmp_path / "openclaw-state"
        state.mkdir()
        (state / "clawdbot.json").write_text(
            json.dumps({"mcpServers": {"legacy": {"command": "legacy-mcp"}}}),
            encoding="utf-8",
        )
        explicit_config = tmp_path / "custom-openclaw.json"
        explicit_config.write_text(
            json.dumps({"mcpServers": {"explicit": {"command": "explicit-mcp"}}}),
            encoding="utf-8",
        )

        plan = _api().preview_import(
            home=tmp_path / "unused-home",
            env={
                "OPENCLAW_STATE_DIR": str(state),
                "OPENCLAW_CONFIG_PATH": str(explicit_config),
            },
        )

        assert _categories(plan, "openclaw") == {"mcp_servers": 1}

        legacy_home = tmp_path / "legacy-home"
        legacy = legacy_home / ".clawdbot"
        legacy.mkdir(parents=True)
        (legacy / "clawdbot.json").write_text(
            json.dumps({"mcpServers": {"legacy": {"command": "legacy-mcp"}}}),
            encoding="utf-8",
        )

        legacy_plan = _api().preview_import(home=legacy_home, env={})

        assert _categories(legacy_plan, "openclaw") == {"mcp_servers": 1}

    def test_openclaw_explicit_config_path_accepts_json5_with_any_extension(
        self, tmp_path: Path
    ) -> None:
        state = tmp_path / "openclaw-state"
        state.mkdir()
        explicit_config = tmp_path / "operator-config.conf"
        explicit_config.write_text(
            """
            {
              // OpenClaw parses its explicit config path as JSON5.
              mcpServers: {
                explicit: {
                  command: "explicit-mcp",
                },
              },
            }
            """,
            encoding="utf-8",
        )

        plan = _api().preview_import(
            home=tmp_path / "unused-home",
            env={
                "OPENCLAW_STATE_DIR": str(state),
                "OPENCLAW_CONFIG_PATH": str(explicit_config),
            },
        )

        assert _categories(plan, "openclaw") == {"mcp_servers": 1}

    def test_openclaw_explicit_sensitive_config_path_is_rejected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew import security

        home = tmp_path / "home"
        state = home / ".openclaw"
        state.mkdir(parents=True)
        sensitive_config = home / ".docker" / "config.json"
        sensitive_config.parent.mkdir()
        sensitive_config.write_text(
            json.dumps({"mcpServers": {"credential-leak": {"command": "never-run"}}}),
            encoding="utf-8",
        )
        monkeypatch.setattr(security.Path, "home", staticmethod(lambda: home))

        plan = _api().preview_import(
            home=home,
            env={
                "OPENCLAW_STATE_DIR": str(state),
                "OPENCLAW_CONFIG_PATH": str(sensitive_config),
            },
        )

        assert _categories(plan, "openclaw") == {}
        assert any(
            item["source_id"] == "openclaw"
            and item["category_id"] == "settings"
            and item["reason"] == "sensitive_path_rejected"
            for item in plan["skipped"]
        )
        assert "credential-leak" not in json.dumps(plan)

    def test_openclaw_ignores_undocumented_configs_sessions_and_guessed_databases(
        self, tmp_path: Path
    ) -> None:
        state = tmp_path / "home" / ".openclaw"
        state.mkdir(parents=True)
        for filename in ("openclaw.json5", "config.json", "mcp.json"):
            (state / filename).write_text(
                json.dumps({"mcpServers": {filename: {"command": "undocumented"}}}),
                encoding="utf-8",
            )
        _write_jsonl(
            state / "sessions" / "orphan.jsonl",
            [{"role": "user", "content": "undocumented top-level session"}],
        )
        for filename in ("sessions.db", "state.db", "openclaw.db"):
            with sqlite3.connect(state / filename) as connection:
                connection.execute("CREATE TABLE guessed (payload BLOB)")

        plan = _api().preview_import(home=tmp_path / "home", env={})

        assert _categories(plan, "openclaw") == {}
        assert not any(
            item["source_id"] == "openclaw" and item["reason"] == "unsupported_session_database"
            for item in plan["skipped"]
        )

    def test_openclaw_canonical_databases_are_safely_diagnosed_not_opened(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        api = _api()
        state = tmp_path / "home" / ".openclaw"
        session_db = state / "agents" / "main" / "agent" / "openclaw-agent.sqlite"
        schedule_db = state / "openclaw.sqlite"
        for database in (session_db, schedule_db):
            database.parent.mkdir(parents=True, exist_ok=True)
            with sqlite3.connect(database) as connection:
                connection.execute("CREATE TABLE current_state (payload BLOB)")
        Path(f"{session_db}-shm").mkdir()

        def fail_if_opened(*_args: object, **_kwargs: object) -> None:
            raise AssertionError("unsupported OpenClaw SQLite database was opened")

        monkeypatch.setattr(api.sqlite3, "connect", fail_if_opened)

        plan = api.preview_import(home=tmp_path / "home", env={})

        # The agent session database is not probed at all (transcripts are
        # out of scope), so only the schedule database is diagnosed here. The
        # monkeypatched ``sqlite3.connect`` still guards the real invariant: an
        # unsupported database must be classified from its filesystem metadata
        # without ever being opened.
        assert any(
            item["source_id"] == "openclaw"
            and item["category_id"] == "schedules"
            and item["reason"] == "unsupported_schedule_database"
            for item in plan["skipped"]
        )
        assert not any(item["category_id"] == "sessions" for item in plan["skipped"])
        assert any(
            item["source_id"] == "openclaw"
            and item["category_id"] == "schedules"
            and item["reason"] == "unsupported_schedule_database"
            for item in plan["skipped"]
        )

    def test_openclaw_agents_entries_and_documented_workspace_defaults_are_scanned(
        self, tmp_path: Path
    ) -> None:
        state = tmp_path / "home" / ".openclaw"
        entry_workspace = tmp_path / "entry-workspace"
        defaults_root = tmp_path / "default-workspaces"
        defaults_workspace = defaults_root / "reviewer"
        profile_workspace = tmp_path / "profile-workspace"
        for workspace, skill_name in (
            (entry_workspace, "entry-skill"),
            (defaults_workspace, "default-skill"),
            (profile_workspace, "profile-skill"),
        ):
            skill = workspace / "skills" / skill_name
            skill.mkdir(parents=True)
            (skill / "SKILL.md").write_text(f"# {skill_name}\n", encoding="utf-8")
            (workspace / "MEMORY.md").write_text(
                f"Remember {skill_name}.",
                encoding="utf-8",
            )
            (workspace / "AGENTS.md").write_text("Always run the tests.\n", encoding="utf-8")
        state.mkdir(parents=True, exist_ok=True)
        (state / "openclaw.json").write_text(
            json.dumps(
                {
                    "agents": {
                        "defaults": {"workspace": str(defaults_root)},
                        "entries": {
                            "main": {"workspace": str(entry_workspace)},
                            "reviewer": {},
                        },
                    },
                    "profiles": {
                        "review": {"workspace": str(profile_workspace)},
                    },
                }
            ),
            encoding="utf-8",
        )

        plan = _api().preview_import(home=tmp_path / "home", env={})

        assert _categories(plan, "openclaw") == {
            "instructions": 1,
            "memories": 3,
            "workspaces": 3,
            "skills": 3,
        }
        # Identical AGENTS.md text across three workspaces collapses to ONE
        # directive: the instruction key carries a content digest, so the
        # in-scan dedupe folds the duplicates.
        assert any(
            item["source_id"] == "openclaw" and item["category_id"] == "instructions"
            for item in plan["selection"]
        )

    def test_openclaw_canonical_workspace_memory_skills_and_settings(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        openclaw = home / ".openclaw"
        workspace = tmp_path / "openclaw-workspace"
        workspace.mkdir()
        (openclaw / "openclaw.json").parent.mkdir(parents=True)
        (openclaw / "openclaw.json").write_text(
            json.dumps(
                {
                    "agents": {
                        "defaults": {
                            "workspace": str(workspace),
                            "userTimezone": "America/Los_Angeles",
                        }
                    },
                    "controlUi": {"prefs": {"themeMode": "dark"}},
                }
            ),
            encoding="utf-8",
        )
        (workspace / "MEMORY.md").write_text(
            "Remember the canonical workspace overview.",
            encoding="utf-8",
        )
        workspace_memory = workspace / "memory" / "notes.md"
        workspace_memory.parent.mkdir()
        workspace_memory.write_text(
            "Remember the canonical workspace details.",
            encoding="utf-8",
        )
        workspace_skill = workspace / "skills" / "review"
        workspace_skill.mkdir(parents=True)
        (workspace_skill / "SKILL.md").write_text("# Workspace review\n", encoding="utf-8")
        managed_skill = openclaw / "skills" / "managed"
        managed_skill.mkdir(parents=True)
        (managed_skill / "SKILL.md").write_text("# Managed state skill\n", encoding="utf-8")

        plan = _api().preview_import(home=home, env={})
        selected = _select(
            plan,
            ("openclaw", "settings"),
            ("openclaw", "skills"),
        )
        _api().apply_import(selected, data_home=tmp_path / "destination")
        config = json.loads((tmp_path / "destination" / "config.json").read_text(encoding="utf-8"))
        imported_skills = tmp_path / "destination" / "skills" / "imported" / "openclaw"

        assert _categories(plan, "openclaw") == {
            "memories": 2,
            "workspaces": 1,
            "skills": 1,
            "settings": 1,
        }
        assert config["timezone"] == "America/Los_Angeles"
        assert config["dashboard"]["theme_mode"] == "dark"
        assert (imported_skills / "review" / "SKILL.md").is_file()
        assert not (imported_skills / "managed").exists()
