"""Scenario DSL, harness bookkeeping and report rendering -- all offline."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from gui_user import harness, report, scenarios

SCENARIOS_DIR = Path(__file__).parent / "scenarios"
#: The markdown notes boot.sh stages at the fixed folder path the Knowledge
#: scenario types (docs/build/gui-user-test.md, "Seeds").
KNOWLEDGE_NOTES_DIR = (
    Path(__file__).resolve().parents[2] / "scripts" / "gui-user-test" / "knowledge-notes"
)


# --------------------------------------------------------------------------
# Scenario DSL
# --------------------------------------------------------------------------


SHIPPED_SMOKE = {
    "apps-discover-enable-research-lab",
    "artifacts-library-table-and-kind-filter",
    "auth-sign-in-card-signed-out",
    "capabilities-agents-list-and-open-editor",
    "capabilities-skills-filter-and-open-builtin",
    "chat-activity-side-panel-toggle",
    "chat-files-side-panel-browse",
    "chat-session-title",
    "chat-sessions-page",
    "chat-switch-seeded-sessions",
    "chat-turn-stats-footer",
    "connections-services-search-and-mcp-list",
    "memory-open-browser-from-overview",
    "notifications-bell-sheet-open-close",
    "notifications-center-empty-state",
    "schedule-list-calendar-executions-views",
    "search-everywhere-jump-to-setting",
    "sessions-new-chat",
    "settings-chat-toggle-show-timestamps",
    "settings-developer-panel-dev-mode-toggle",
    "settings-search-jump-to-theme",
    "settings-security-docs-section",
    "settings-security-layers-section",
    "settings-security-rail-navigation",
    "settings-security-rules-custom-deny",
    "settings-security-trusted-apps-toggle",
    "settings-shortcuts",
    "settings-tab-rail-navigation",
    "settings-theme-toggle",
    "sidebar-folders-and-older-sessions",
    "sidebar-rail-collapse-expand",
    "taskrunner-projects-page-compose",
}
SHIPPED = SHIPPED_SMOKE | {
    "knowledge-add-folder-source-and-scan",
    "members-dm-hello",
    "members-private-memory-keeps-thread",
}


class TestShippedScenarios:
    def test_every_shipped_scenario_loads(self) -> None:
        loaded = scenarios.load_all(SCENARIOS_DIR)
        assert {s.name for s in loaded} == SHIPPED

    def test_smoke_tier_is_the_short_core_paths(self) -> None:
        smoke = scenarios.select(scenarios.load_all(SCENARIOS_DIR), tier="smoke")
        assert {s.name for s in smoke} == SHIPPED_SMOKE
        # A smoke scenario is a core path in a handful of actions; the tier's bill is
        # the sum of the scenarios' own limits, which the nightly tier then inherits.
        # The totals are pinned, not bounded, so a new smoke scenario moves them on
        # purpose and the cost note in docs/build/gui-user-test.md is re-read with them.
        # Re-pin by summing `max_steps` / `max_seconds` over this `smoke` selection;
        # the assertion messages print the live totals, so a stale pin names its fix.
        for s in smoke:
            assert len(s.steps) <= 5, s.name
            assert s.max_steps <= 14, s.name
        smoke_steps = sum(s.max_steps for s in smoke)
        smoke_seconds = sum(s.max_seconds for s in smoke)
        assert smoke_steps == 307, f"smoke max_steps total is {smoke_steps}; re-pin"
        assert smoke_seconds == 8420, f"smoke max_seconds total is {smoke_seconds}; re-pin"

    def test_nightly_includes_smoke(self) -> None:
        nightly = scenarios.select(scenarios.load_all(SCENARIOS_DIR), tier="nightly")
        assert {s.name for s in nightly} == SHIPPED
        assert SHIPPED_SMOKE < SHIPPED

    def test_knowledge_sample_notes_are_the_count_the_scenario_asserts(self) -> None:
        # Each staged note is one chunk, so the file count IS the item count the
        # scenario reads off the source row. A note added or removed without the
        # scenario moving -- or a chunker change that splits a note -- fails here,
        # not as a paid nightly run.
        from kiro_crew.knowledge.chunker import HeadingAwareChunker

        notes = sorted(KNOWLEDGE_NOTES_DIR.glob("*.md"))
        assert len(notes) == 3
        chunker = HeadingAwareChunker()
        for note in notes:
            assert len(chunker.chunk(note.read_text(encoding="utf-8"))) == 1, note.name
        sc = scenarios.load_scenario(SCENARIOS_DIR / "knowledge-add-folder-source-and-scan.yaml")
        assert any("3 supported files found" in step for step in sc.steps)
        assert any('"3 items"' in exp for exp in sc.expectations)
        assert any("/tmp/kirocrew-gui-user-test/team-notes" in step for step in sc.steps)

    def test_rich_seed_artifacts_are_the_ones_the_scenario_reads(self) -> None:
        # The Artifacts scenario names the three artifacts the `rich` seed ships and
        # narrows the table to the one markdown artifact by slug and kind. An
        # artifact renamed, re-kinded or dropped from the fixture without the
        # scenario moving fails here rather than as a paid nightly run. The set is
        # a copy of the `artifacts-library` fixture's, and the two are held equal
        # so the copies cannot drift apart silently.
        from kiro_crew.artifacts import ArtifactStore
        from kiro_crew.testing.fixtures import seeded_home

        def fingerprint(fixture: str) -> dict[str, tuple[str, str, int]]:
            with seeded_home(fixture):
                return {a.slug: (a.name, a.kind, a.version) for a in ArtifactStore().list()}

        rich = fingerprint("rich")
        assert rich == fingerprint("artifacts-library")
        assert {slug: kind for slug, (_, kind, _) in rich.items()} == {
            "pagination-design": "markdown",
            "queue-badge": "svg",
            "release-checklist": "widget",
        }
        sc = scenarios.load_scenario(SCENARIOS_DIR / "artifacts-library-table-and-kind-filter.yaml")
        first_step = sc.steps[0]
        for name, _, _ in rich.values():
            assert name in first_step, name
        markdown = [(slug, *rest) for slug, rest in rich.items() if rest[1] == "markdown"]
        assert len(markdown) == 1
        slug, name, _, version = markdown[0]
        assert any(
            name in exp and f'"{slug}"' in exp and '"markdown"' in exp for exp in sc.expectations
        )
        assert any(f'"v{version}"' in exp for exp in sc.expectations)

    def test_explicit_name_selection(self) -> None:
        picked = scenarios.select(scenarios.load_all(SCENARIOS_DIR), names=["members-dm-hello"])
        assert [s.name for s in picked] == ["members-dm-hello"]
        with pytest.raises(scenarios.ScenarioError, match="unknown scenario"):
            scenarios.select(scenarios.load_all(SCENARIOS_DIR), names=["nope"])

    def test_task_prompt_is_built_only_from_the_yaml(self) -> None:
        sc = scenarios.load_scenario(SCENARIOS_DIR / "sessions-new-chat.yaml")
        prompt = sc.task_prompt()
        assert prompt.startswith("TASK: ")
        for step in sc.steps:
            assert step in prompt
        for exp in sc.expectations:
            assert exp in prompt
        assert f"at most {sc.max_steps} actions" in prompt
        # The catalog fields describe the scenario to people, not to the model.
        assert sc.user_story not in prompt

    def test_every_shipped_scenario_is_classified(self) -> None:
        for sc in scenarios.load_all(SCENARIOS_DIR):
            assert sc.feature in scenarios.FEATURES
            assert sc.user_story.startswith("As a "), sc.name
            assert sc.docs_url.startswith("docs/") or sc.docs_url.startswith("https://")
            if sc.docs_url.startswith("docs/"):
                assert (Path(__file__).parents[2] / sc.docs_url.split("#")[0]).is_file(), sc.name

    def test_shipped_scenarios_group_by_feature(self) -> None:
        groups = scenarios.by_feature(scenarios.load_all(SCENARIOS_DIR))
        assert {slug: [s.name for s in g] for slug, g in groups.items()} == {
            "chat": [
                "chat-session-title",
                "chat-switch-seeded-sessions",
                "chat-turn-stats-footer",
                "sessions-new-chat",
            ],
            "side-panel": ["chat-activity-side-panel-toggle"],
            "sidebar": [
                "chat-sessions-page",
                "sidebar-folders-and-older-sessions",
                "sidebar-rail-collapse-expand",
            ],
            "search": ["search-everywhere-jump-to-setting"],
            "members": ["members-dm-hello", "members-private-memory-keeps-thread"],
            "capabilities": [
                "capabilities-agents-list-and-open-editor",
                "capabilities-skills-filter-and-open-builtin",
            ],
            "connections": ["connections-services-search-and-mcp-list"],
            "memory": ["memory-open-browser-from-overview"],
            "knowledge": ["knowledge-add-folder-source-and-scan"],
            "artifacts": ["artifacts-library-table-and-kind-filter"],
            "files": ["chat-files-side-panel-browse"],
            "apps": ["apps-discover-enable-research-lab"],
            "task-runner": ["taskrunner-projects-page-compose"],
            "schedule": ["schedule-list-calendar-executions-views"],
            "notifications": [
                "notifications-bell-sheet-open-close",
                "notifications-center-empty-state",
            ],
            "auth": ["auth-sign-in-card-signed-out"],
            "settings": [
                "settings-chat-toggle-show-timestamps",
                "settings-developer-panel-dev-mode-toggle",
                "settings-search-jump-to-theme",
                "settings-shortcuts",
                "settings-tab-rail-navigation",
                "settings-theme-toggle",
            ],
            "security": [
                "settings-security-docs-section",
                "settings-security-layers-section",
                "settings-security-rail-navigation",
                "settings-security-rules-custom-deny",
                "settings-security-trusted-apps-toggle",
            ],
        }
        # FEATURES order, not alphabetical: chat is the product's primary surface.
        assert list(groups) == [
            "chat",
            "side-panel",
            "sidebar",
            "search",
            "members",
            "capabilities",
            "connections",
            "memory",
            "knowledge",
            "artifacts",
            "files",
            "apps",
            "task-runner",
            "schedule",
            "notifications",
            "auth",
            "settings",
            "security",
        ]

    def test_members_scenario_holds_across_the_crew_mode_retirement(self) -> None:
        """The Feature Previews card carries two titles across the Crew Mode retirement; the steps name both."""
        sc = scenarios.load_scenario(SCENARIOS_DIR / "members-dm-hello.yaml")
        preview_step = next(
            s for s in sc.steps if "preview" in s.lower() and "turn on" in s.lower()
        )
        assert 'starts with "Crew Members"' in preview_step
        assert (
            "Crew Members and Crew Mode" in preview_step
        )  # the longer title is still a valid reading
        assert any('"Crew Members" item appears in the left rail' in s for s in sc.steps)

    def test_members_scenarios_hedge_the_card_label(self) -> None:
        """A seeded member has no display name, so its card shows the id; both members scenarios say so."""
        for name in ("members-dm-hello", "members-private-memory-keeps-thread"):
            sc = scenarios.load_scenario(SCENARIOS_DIR / f"{name}.yaml")
            card_steps = [s for s in sc.steps if "Nova Sky" in s]
            assert card_steps, name
            for step in card_steps:
                assert 'may read "nova-sky"' in step, (name, step)
            assert not any('named "Nova Sky"' in s for s in sc.steps), name


def _write(tmp_path: Path, name: str, doc: dict) -> Path:
    p = tmp_path / f"{name}.yaml"
    p.write_text(yaml.safe_dump(doc), encoding="utf-8")
    return p


def _valid(name: str = "demo") -> dict:
    return {
        "name": name,
        "tier": "smoke",
        "feature": "settings",
        "user_story": "As a user, I want to do a thing, so that the thing is done.",
        "summary": "do a thing",
        "preconditions": {"seed": "rich", "members": ["nova-sky"], "start_url": "/settings"},
        "steps": ["click the thing"],
        "expectations": ["the thing is clicked"],
        "max_steps": 5,
        "max_seconds": 60,
    }


class TestScenarioValidation:
    def test_valid_document_round_trips(self, tmp_path: Path) -> None:
        sc = scenarios.load_scenario(_write(tmp_path, "demo", _valid()))
        assert sc.members == ("nova-sky",) and sc.start_url == "/settings" and sc.seed == "rich"
        assert sc.feature == "settings" and sc.user_story.startswith("As a user")
        assert sc.docs_url == ""

    def test_docs_url_accepts_https_and_repo_docs_paths(self, tmp_path: Path) -> None:
        for url in (
            "https://github.com/kirodotdev/KiroCrew/issues/9578",
            "docs/system-specs/modules/themes.md",
            "docs/build/gui-user-test.md#adding-a-scenario",
        ):
            doc = _valid()
            doc["docs_url"] = url
            assert scenarios.load_scenario(_write(tmp_path, "demo", doc)).docs_url == url

    def test_defaults(self, tmp_path: Path) -> None:
        doc = _valid()
        for k in ("tier", "preconditions", "max_steps", "max_seconds"):
            doc.pop(k)
        sc = scenarios.load_scenario(_write(tmp_path, "demo", doc))
        assert (sc.tier, sc.seed, sc.members, sc.start_url, sc.max_steps, sc.max_seconds) == (
            "nightly",
            "rich",
            (),
            "/",
            15,
            300,
        )

    @pytest.mark.parametrize(
        "mutate,match",
        [
            (lambda d: d.update(name="Demo"), "lowercase slug"),
            (lambda d: d.update(name="other"), "file stem"),
            (lambda d: d.update(tier="weekly"), "tier"),
            (lambda d: d.pop("feature"), "'feature' is required"),
            (lambda d: d.update(feature="Settings"), "'feature' is required"),
            (lambda d: d.update(feature="not-a-feature"), "'feature' is required"),
            (lambda d: d.pop("user_story"), "'user_story' is required"),
            (lambda d: d.update(user_story="   "), "'user_story' is required"),
            (lambda d: d.update(user_story="x" * (scenarios.USER_STORY_MAX + 1)), "user_story"),
            (lambda d: d.update(docs_url="http://insecure.example"), "docs_url"),
            (lambda d: d.update(docs_url="docs/../secret.md"), "docs_url"),
            (lambda d: d.update(docs_url="README.md"), "docs_url"),
            (lambda d: d.update(docs_url=7), "docs_url"),
            (lambda d: d.update(summary=""), "summary"),
            (lambda d: d.update(steps=[]), "steps"),
            (lambda d: d.update(expectations=[""]), "expectations"),
            (lambda d: d.update(max_steps=0), "max_steps"),
            (lambda d: d.update(max_steps=scenarios.MAX_STEPS_CEILING + 1), "max_steps"),
            (lambda d: d.update(max_seconds=scenarios.MAX_SECONDS_CEILING + 1), "max_seconds"),
            (lambda d: d.update(max_seconds=True), "max_seconds"),
            (lambda d: d.update(bonus=1), "unknown keys"),
            (lambda d: d["preconditions"].update(start_url="settings"), "start_url"),
            (lambda d: d["preconditions"].update(start_url="/x?token=1"), "start_url"),
            (lambda d: d["preconditions"].update(members=["Nova Sky"]), "members"),
            (lambda d: d["preconditions"].update(seed="../etc"), "seed"),
            (lambda d: d["preconditions"].update(display=":0"), "unknown preconditions"),
        ],
    )
    def test_rejects_malformed_documents(self, tmp_path: Path, mutate, match: str) -> None:
        doc = _valid()
        mutate(doc)
        with pytest.raises(scenarios.ScenarioError, match=match):
            scenarios.load_scenario(_write(tmp_path, "demo", doc))

    def test_non_mapping_and_invalid_yaml(self, tmp_path: Path) -> None:
        p = tmp_path / "demo.yaml"
        p.write_text("- just\n- a list\n", encoding="utf-8")
        with pytest.raises(scenarios.ScenarioError, match="mapping"):
            scenarios.load_scenario(p)
        p.write_text("name: [unclosed\n", encoding="utf-8")
        with pytest.raises(scenarios.ScenarioError, match="invalid YAML"):
            scenarios.load_scenario(p)

    def test_empty_directory_is_an_error(self, tmp_path: Path) -> None:
        with pytest.raises(scenarios.ScenarioError, match="no \\*.yaml"):
            scenarios.load_all(tmp_path)


# --------------------------------------------------------------------------
# Harness bookkeeping (no Bedrock, no display)
# --------------------------------------------------------------------------


class TestToolShapes:
    def test_native_tool_advertises_the_screenshot_size(self) -> None:
        from gui_user import x11

        tools = harness.native_tools(x11.Geometry(1600, 1000, 1280))
        assert tools == [
            {
                "type": harness.COMPUTER_TOOL_TYPE,
                "name": "computer",
                "display_width_px": 1280,
                "display_height_px": 800,
            }
        ]

    def test_custom_tools_cover_the_backend_vocabulary_and_nothing_else(self) -> None:
        from gui_user import x11

        names = {t["name"] for t in harness.custom_tools()}
        assert names <= x11.ACTIONS
        assert {"screenshot", "left_click", "type", "key", "scroll", "wait"} <= names
        for t in harness.custom_tools():
            assert t["input_schema"]["additionalProperties"] is False

    def test_decode_native_and_custom_tool_use(self) -> None:
        native = {
            "type": "tool_use",
            "id": "t1",
            "name": "computer",
            "input": {"action": "left_click", "coordinate": [1, 2]},
        }
        assert harness.decode_tool_use(native) == ("left_click", {"coordinate": [1, 2]})
        custom = {"type": "tool_use", "id": "t2", "name": "key", "input": {"text": "Return"}}
        assert harness.decode_tool_use(custom) == ("key", {"text": "Return"})
        assert harness.decode_tool_use({"name": "computer", "input": None}) == ("", {})


class TestConversation:
    def test_trim_images_keeps_the_newest_n_across_user_and_tool_result_blocks(self) -> None:
        img = harness.image_block(b"png")
        messages = [
            {"role": "user", "content": [{"type": "text", "text": "task"}, dict(img)]},
            {
                "role": "assistant",
                "content": [{"type": "tool_use", "id": "a", "name": "computer", "input": {}}],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "a",
                        "content": [dict(img), {"type": "text", "text": "x"}],
                    }
                ],
            },
            {
                "role": "assistant",
                "content": [{"type": "tool_use", "id": "b", "name": "computer", "input": {}}],
            },
            {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": "b", "content": [dict(img)]}],
            },
            {
                "role": "assistant",
                "content": [{"type": "tool_use", "id": "c", "name": "computer", "input": {}}],
            },
            {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": "c", "content": [dict(img)]}],
            },
        ]
        assert harness.trim_images(messages, keep=2) == 2
        assert messages[0]["content"][1]["type"] == "text"
        assert messages[2]["content"][0]["content"][0]["type"] == "text"
        assert messages[4]["content"][0]["content"][0]["type"] == "image"
        assert messages[6]["content"][0]["content"][0]["type"] == "image"
        # Structure stays valid: tool_result still has a content list.
        assert isinstance(messages[2]["content"][0]["content"], list)
        # Idempotent once under the cap.
        assert harness.trim_images(messages, keep=2) == 0

    def test_parse_verdict(self) -> None:
        assert harness.parse_verdict("VERDICT: PASS\nEXPECTATIONS:\n- x : MET") == "PASS"
        assert harness.parse_verdict("some prose\n  verdict: fail -- could not find it") == "FAIL"
        assert harness.parse_verdict("I think it passed") is None

    def test_usage_cost(self) -> None:
        u = harness.Usage()
        u.add({"input_tokens": 1_000_000, "output_tokens": 100_000})
        u.add({"input_tokens": 0})
        assert u.calls == 2
        assert u.usd(3.0, 15.0) == pytest.approx(3.0 + 1.5)


# --------------------------------------------------------------------------
# Report
# --------------------------------------------------------------------------


def _summary() -> dict:
    return {
        "model": "test-model",
        "region": "us-west-2",
        "tool_mode": "custom",
        "tier": "smoke",
        "seconds": 123.4,
        "usage": {"input_tokens": 50_000, "output_tokens": 1_200, "calls": 9},
        "usd": 0.17,
        "budget_usd": 3.0,
        "scenarios": [
            {
                "name": "settings-theme-toggle",
                "tier": "smoke",
                "feature": "settings",
                "user_story": "As a user, I want to switch theme | quickly.",
                "summary": "s",
                "status": "PASS",
                "attempts": [
                    {
                        "status": "PASS",
                        "steps": 4,
                        "seconds": 40.0,
                        "input_tokens": 1,
                        "output_tokens": 1,
                        "usd": 0.05,
                        "final_text": "VERDICT: PASS\nEXPECTATIONS:\n- ok : MET\nUI-ISSUES: none",
                        "error": "",
                        "shots_dir": "a",
                    }
                ],
            },
            {
                "name": "sessions-new-chat",
                "tier": "smoke",
                "feature": "chat",
                "user_story": "As a user, I want a new chat.",
                "summary": "s",
                "status": "FAIL",
                "attempts": [
                    {
                        "status": "FAIL",
                        "steps": 9,
                        "seconds": 80.0,
                        "input_tokens": 1,
                        "output_tokens": 1,
                        "usd": 0.06,
                        "final_text": "VERDICT: FAIL\nEXPECTATIONS:\n- x : NOT MET -- no new row",
                        "error": "",
                        "shots_dir": "b1",
                    },
                    {
                        "status": "MAX_STEPS",
                        "steps": 14,
                        "seconds": 90.0,
                        "input_tokens": 1,
                        "output_tokens": 1,
                        "usd": 0.06,
                        "final_text": "",
                        "error": "",
                        "shots_dir": "b2",
                    },
                ],
            },
        ],
    }


class TestReport:
    def test_overall(self) -> None:
        assert report.overall(_summary()) == "FAIL"
        s = _summary()
        s["scenarios"][1]["status"] = "PASS"
        assert report.overall(s) == "PASS"
        s["scenarios"][1]["status"] = "ERROR"
        assert report.overall(s) == "ERROR"
        assert report.overall({"scenarios": []}) == "ERROR"

    def test_markdown_has_one_row_per_scenario_and_the_cost_line(self) -> None:
        md = report.render_markdown(
            _summary(), artifact_url="https://x/artifact", run_url="https://x/run"
        )
        assert md.count("| `settings-theme-toggle` |") == 1
        assert (
            "| ❌ FAIL | `sessions-new-chat` | As a user, I want a new chat. | smoke "
            "| 14 | 90.0s | 2 | $0.12 | MAX_STEPS |" in md
        )
        assert "≈ $0.17 of $3.00 budget" in md
        assert "[screenshots + steps.jsonl](https://x/artifact)" in md
        # A failing scenario's final report is shown; a passing one only if it flagged UI issues.
        assert "no new row" in md
        assert "VERDICT: PASS" not in md

    def test_markdown_is_grouped_by_feature_in_registry_order(self) -> None:
        md = report.render_markdown(_summary())
        chat = md.index("### Chat sessions (`chat`) — ❌ FAIL 0/1")
        settings = md.index("### Settings (`settings`) — ✅ PASS 1/1")
        assert chat < settings  # FEATURES order, even though the fixture lists settings first
        assert md.count("| | Scenario | User story | Tier |") == 2
        # A pipe inside a repo-authored user story cannot break the table.
        assert "switch theme / quickly." in md and "theme | quickly" not in md

    def test_pre_feature_summaries_still_render(self) -> None:
        s = _summary()
        for sc in s["scenarios"]:
            sc.pop("feature")
            sc.pop("user_story")
        md = report.render_markdown(s)
        assert "### Unclassified (`unclassified`) — ❌ FAIL 1/2" in md
        assert md.count("| `") == 2
        assert report.group_by_feature([]) == {}

    def test_console_groups_too(self) -> None:
        out = report.render_console(_summary())
        assert out.splitlines()[0].startswith("GUI user test: FAIL")
        assert "  [chat] Chat sessions: FAIL" in out
        assert "  [settings] Settings: PASS" in out

    def test_features_catalog_lists_stories_verdicts_and_gaps(self) -> None:
        catalog = scenarios.load_all(SCENARIOS_DIR)
        md = report.render_features(catalog, _summary(), run_url="https://x/run")
        assert md.startswith("# GUI user-test feature catalog\n")
        assert (
            f"_18 of {len(scenarios.FEATURES)} features covered · 35 scenarios (32 smoke / 3 nightly)._"
            in md
        )
        assert (
            "_Latest verdict: **FAIL** on tier `smoke` with `test-model` ([workflow run](https://x/run))._"
            in md
        )
        # Sections in FEATURES order, each with its stories; the nightly-only member
        # scenario was not selected by this smoke run and says so.
        assert (
            md.index("## Chat sessions (`chat`)")
            < md.index("## Crew Members (`members`)")
            < md.index("## Settings (`settings`)")
        )
        assert "| ❌ FAIL | As a user, I want to start a new chat" in md
        assert "| ▫️ not run | As a user with several agents" in md
        assert (
            "| `settings-theme-toggle` | smoke | [docs](docs/system-specs/modules/themes.md) |"
            in md
        )
        # Uncovered features are the backlog.
        assert "## Not yet covered" in md
        assert "- `terminal` Terminal panel" in md
        assert "- `chat` Chat sessions" not in md
        assert "- `files` File viewer & project files" not in md

    def test_features_catalog_without_a_run(self) -> None:
        md = report.render_features(scenarios.load_all(SCENARIOS_DIR))
        assert "_No run attached" in md
        assert md.count("▫️ not run") == len(SHIPPED) and "✅" not in md and "❌" not in md

    def test_neutralize_defangs_fences_mentions_and_control_chars(self) -> None:
        raw = "ok\n```\n@maintainer see <img src=x onerror=1>\x07\r~~~\n" + "z" * 50
        out = report.neutralize(raw, max_chars=40)
        assert "```" not in out and "~~~" not in out
        assert "@maintainer" not in out and "@\u200bmaintainer" in out
        assert "\x07" not in out and "\r" not in out
        assert out.endswith("…") and len(out) == 41

    def test_model_text_is_only_ever_rendered_inside_a_neutralized_fence(self) -> None:
        s = _summary()
        s["scenarios"][1]["attempts"][0][
            "final_text"
        ] = "VERDICT: FAIL\n```\n@everyone [x](https://evil)"
        s["scenarios"][0]["attempts"][0][
            "final_text"
        ] = "VERDICT: PASS\nUI-ISSUES: ``` overlap @you"
        md = report.render_markdown(s)
        # Each model block opens with our fence and the model's own fences are gone.
        assert md.count("```text\n") == 2
        assert "\n```\n@everyone" not in md
        assert "@\u200beveryone" in md and "@\u200byou" in md
        assert md.count("```") == 4  # two opens, two closes: nothing broke out

    def test_comment_carries_marker_and_head_proof(self) -> None:
        body = report.render_comment(
            _summary(), head_sha="abc123", artifact_url=None, run_url="https://x/run"
        )
        assert body.startswith(report.COMMENT_MARKER + "\n")
        assert "❌ FAIL" in body.splitlines()[1]
        assert "Advisory — does not block merge" in body
        assert body.rstrip().endswith(f"{report.REVIEWED_MARKER} abc123")

    def test_issue_title_names_the_failures(self) -> None:
        title, body = report.render_issue(_summary(), sha="abc", run_url=None, artifact_url=None)
        assert title == "Nightly GUI user test FAIL: sessions-new-chat"
        assert "#9578" in body

    def test_cli_formats(self, tmp_path: Path, capsys) -> None:
        p = tmp_path / "summary.json"
        p.write_text(json.dumps(_summary()), encoding="utf-8")
        assert report.main(["--summary", str(p), "--format", "verdict"]) == 0
        assert capsys.readouterr().out.strip() == "FAIL"
        assert (
            report.main(["--summary", str(p), "--format", "comment", "--head-sha", "deadbeef"]) == 0
        )
        assert report.COMMENT_MARKER in capsys.readouterr().out
        assert report.main(["--summary", str(tmp_path / "missing.json")]) == 2
        capsys.readouterr()
        # Every non-catalog format needs a summary.
        assert report.main(["--format", "verdict"]) == 2
        assert "--summary is required" in capsys.readouterr().err

    def test_cli_features_with_and_without_summary(
        self, tmp_path: Path, capsys, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        p = tmp_path / "summary.json"
        p.write_text(json.dumps(_summary()), encoding="utf-8")
        assert report.main(["--format", "features", "--summary", str(p)]) == 0
        out = capsys.readouterr().out
        assert "# GUI user-test feature catalog" in out and "Latest verdict: **FAIL**" in out
        assert report.main(["--format", "features"]) == 0
        assert "_No run attached" in capsys.readouterr().out
        # A malformed shipped scenario is a hard error, not a half catalog.
        (tmp_path / "bad.yaml").write_text("name: bad\n", encoding="utf-8")
        monkeypatch.setattr(report, "SCENARIOS_DIR", tmp_path)
        assert report.main(["--format", "features"]) == 2
        assert "could not load scenarios" in capsys.readouterr().err


class TestScenarioResultCarriesTheCatalogFields:
    def test_for_scenario_copies_feature_and_story(self) -> None:
        sc = scenarios.load_scenario(SCENARIOS_DIR / "members-dm-hello.yaml")
        res = harness.ScenarioResult.for_scenario(sc, "SKIPPED")
        assert (res.name, res.tier, res.status) == ("members-dm-hello", "nightly", "SKIPPED")
        assert res.feature == "members" and res.user_story == sc.user_story
        assert res.attempts == []
