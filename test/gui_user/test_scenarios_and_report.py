"""Scenario DSL, harness bookkeeping and report rendering -- all offline."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from gui_user import harness, report, scenarios

SCENARIOS_DIR = Path(__file__).parent / "scenarios"


# --------------------------------------------------------------------------
# Scenario DSL
# --------------------------------------------------------------------------


class TestShippedScenarios:
    def test_every_shipped_scenario_loads(self) -> None:
        loaded = scenarios.load_all(SCENARIOS_DIR)
        assert {s.name for s in loaded} == {
            "settings-theme-toggle",
            "sessions-new-chat",
            "members-dm-hello",
        }

    def test_pr_smoke_tier_is_the_cheap_pair(self) -> None:
        smoke = scenarios.select(scenarios.load_all(SCENARIOS_DIR), tier="smoke")
        assert {s.name for s in smoke} == {"settings-theme-toggle", "sessions-new-chat"}
        # The bill for a PR run is bounded by the smoke tier's own limits.
        assert sum(s.max_steps for s in smoke) <= 30

    def test_nightly_includes_smoke(self) -> None:
        nightly = scenarios.select(scenarios.load_all(SCENARIOS_DIR), tier="nightly")
        assert len(nightly) == 3

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


def _write(tmp_path: Path, name: str, doc: dict) -> Path:
    p = tmp_path / f"{name}.yaml"
    p.write_text(yaml.safe_dump(doc), encoding="utf-8")
    return p


def _valid(name: str = "demo") -> dict:
    return {
        "name": name,
        "tier": "smoke",
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
            "| ❌ FAIL | `sessions-new-chat` | smoke | 14 | 90.0s | 2 | $0.12 | MAX_STEPS |" in md
        )
        assert "≈ $0.17 of $3.00 budget" in md
        assert "[screenshots + steps.jsonl](https://x/artifact)" in md
        # A failing scenario's final report is shown; a passing one only if it flagged UI issues.
        assert "no new row" in md
        assert "VERDICT: PASS" not in md

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
