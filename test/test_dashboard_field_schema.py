"""Nested shapes for agentic dashboard fields: declared, checked at load, enforced on write.

A field's top-level ``type`` alone let a Needs-you card written as ``{title, detail}``
land as "an array" while the page, which reads ``text``, dropped every row of it. These
cases pin the four places that close that gap: the manifest declares the shape, the
write is checked against it, ``dashboard_fields`` hands the shape and the current value
back, and the crewmate's turn names the page and its writable fields.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from kiro_crew import dashboard_agentic
from kiro_crew.dashboard_templates.manifest import ManifestError, load_template, parse_manifest
from kiro_crew.mcp_panel import _render_fields

BUILTIN = Path(__file__).resolve().parents[1] / "src/kiro_crew/dashboard_templates/builtin"
FIXTURES = Path(__file__).resolve().parent / "fixtures/dashboard_templates"


def _builtin(tid: str) -> Any:
    manifest, _html = load_template(BUILTIN / tid)
    return manifest


def _inst(tid: str) -> dashboard_agentic.Instance:
    return dashboard_agentic.Instance(manifest=_builtin(tid), instance_version=1)


def _raw(**field: Any) -> dict[str, Any]:
    return {
        "id": "demo",
        "version": 1,
        "title": "Demo",
        "description": "A demo",
        "source": "user",
        "fields": {"f": {"source": {"agentic": True}, **field}},
    }


def _refusal(tid: str, field: str, value: Any) -> dashboard_agentic.WriteRefused:
    with pytest.raises(dashboard_agentic.WriteRefused) as caught:
        dashboard_agentic.check_write(_inst(tid), field, value)
    return caught.value


# -------------------------------------------------------------------------- #
# the manifest declares a shape and the loader checks it
# -------------------------------------------------------------------------- #


class TestTheManifestShape:
    def test_every_builtin_manifest_still_loads(self) -> None:
        ids = [d.name for d in sorted(BUILTIN.iterdir()) if (d / "manifest.json").is_file()]
        assert ids, "no built-in template found"
        for tid in ids:
            assert _builtin(tid).id == tid

    @pytest.mark.parametrize(
        ("tid", "field"),
        [
            ("project-report", "for_you"),
            ("project-report", "verdict"),
            ("project-report", "ci"),
            ("office", "drafts"),
            ("office", "steps"),
            ("pr-watch", "lanes"),
        ],
    )
    def test_each_shaped_builtin_field_declares_its_shape(self, tid: str, field: str) -> None:
        spec = _builtin(tid).fields[field]
        assert spec.agentic and spec.shape is not None, f"{tid}.{field} declares no shape"

    def test_the_needs_you_shape_is_the_keys_the_page_reads(self) -> None:
        item = _builtin("project-report").fields["for_you"].shape.items
        assert item is not None and item.properties is not None
        assert set(item.properties) == {
            "text",
            "ask",
            "options",
            "context",
            "why",
            "how",
            "workstream",
        }
        assert item.required == ("text",)
        assert item.properties["ask"].enum == ("decide", "approve", "do")
        assert item.properties["options"].describe() == {
            "type": "array",
            "items": {"type": "string"},
        }

    @pytest.mark.parametrize(
        ("field", "needle"),
        [
            ({"type": "string", "items": {"type": "string"}}, "items is only for an array"),
            ({"type": "array", "items": {"type": "bogus"}}, "type 'bogus'"),
            ({"type": "array", "items": {"type": "string", "propertys": {}}}, "unknown shape key"),
            (
                {"type": "object", "properties": {"a": {"type": "string"}}, "required": ["b"]},
                "undeclared properties ['b']",
            ),
            (
                {
                    "type": "object",
                    "properties": {"a": {"type": "string"}},
                    "values": {"type": "string"},
                },
                "cannot both be declared",
            ),
            ({"type": "string", "enum": []}, "enum must list"),
            ({"type": "string", "enum": [1]}, "enum must list"),
            ({"type": "array", "enum": ["a"]}, "enum is only for"),
        ],
    )
    def test_a_bad_shape_is_refused_at_load(self, field: dict[str, Any], needle: str) -> None:
        with pytest.raises(ManifestError) as caught:
            parse_manifest(_raw(**field))
        assert needle in str(caught.value), str(caught.value)

    def test_a_shape_nested_past_the_bound_is_refused(self) -> None:
        shape: dict[str, Any] = {"type": "string"}
        for _ in range(9):
            shape = {"type": "array", "items": shape}
        with pytest.raises(ManifestError, match="deeper than"):
            parse_manifest(_raw(**shape))

    def test_a_fold_field_cannot_declare_a_shape(self) -> None:
        raw = _raw(type="string")
        raw["fields"]["g"] = {
            "type": "array",
            "items": {"type": "string"},
            "source": {"fold": "work", "path": "items"},
        }
        with pytest.raises(ManifestError, match="only an agentic field declares a shape"):
            parse_manifest(raw)


# -------------------------------------------------------------------------- #
# the write is checked against the shape
# -------------------------------------------------------------------------- #


class TestTheWriteChecksTheShape:
    def test_a_title_detail_card_is_refused_naming_text(self) -> None:
        refused = _refusal("project-report", "for_you", [{"title": "Pick", "detail": "a or b"}])
        assert refused.code == "wrong_shape"
        message = str(refused)
        assert "unknown key(s) 'detail', 'title'" in message, message
        assert "missing required key(s) 'text'" in message, message
        assert "text (required): string" in message, "the refusal names the valid keys"
        assert "ask: string (decide|approve|do)" in message, message

    def test_a_card_in_the_page_shape_is_accepted(self) -> None:
        card = {
            "text": "Pick a database",
            "ask": "decide",
            "options": ["Postgres", "SQLite"],
            "context": "two drafts",
            "why": "blocks the schema",
            "how": "I migrate tonight",
            "workstream": "storage",
        }
        entry = dashboard_agentic.check_write(_inst("project-report"), "for_you", [card])
        assert entry["value"] == {"v": [card]}

    def test_an_ask_outside_the_enum_is_refused(self) -> None:
        refused = _refusal("project-report", "for_you", [{"text": "x", "ask": "maybe"}])
        assert "for_you[0].ask is 'maybe', not one of decide|approve|do" in str(refused)

    def test_a_wrong_nested_type_is_refused_where_it_is(self) -> None:
        refused = _refusal("project-report", "for_you", [{"text": "x", "options": "a, b"}])
        assert "for_you[0].options wants array of string, and this is string" in str(refused)

    def test_a_verdict_without_its_state_is_refused(self) -> None:
        refused = _refusal("project-report", "verdict", {"headline": "fine"})
        assert "missing required key(s) 'state'" in str(refused)

    def test_a_keyed_map_checks_every_value(self) -> None:
        refused = _refusal("office", "steps", {"board-1": "drafting", "board-2": 3})
        assert "steps['board-2'] wants string" in str(refused)
        refused = _refusal("office", "drafts", {"it_1": {"file": "a.pptx", "kind": "deck"}})
        assert "unknown key(s) 'kind'" in str(refused)

    def test_a_pr_watch_lane_with_a_string_pr_is_refused(self) -> None:
        refused = _refusal("pr-watch", "lanes", [{"name": "lint", "state": "fail", "pr": "12"}])
        assert "lanes[0].pr wants number" in str(refused)

    @pytest.mark.parametrize(
        ("fixture", "tid"),
        [("sample_workstreams.json", "project-report"), ("sample_office.json", "office")],
    )
    def test_the_sample_pages_own_values_pass_their_shape(self, fixture: str, tid: str) -> None:
        values = json.loads((FIXTURES / fixture).read_text(encoding="utf-8"))["agentic"]
        for field, value in values.items():
            dashboard_agentic.check_write(_inst(tid), field, value)

    def test_the_refusal_names_only_the_first_few_problems(self) -> None:
        refused = _refusal("project-report", "for_you", [{"title": str(i)} for i in range(20)])
        assert str(refused).count("unknown key(s)") == dashboard_agentic.SHAPE_PROBLEMS_SHOWN

    def test_wrong_shape_is_a_value_mistake_answered_by_the_same_field(self) -> None:
        book = {"groups": [{"code": "wrong_shape", "field": "for_you", "count": 1}]}
        entry = dashboard_agentic.correction_entry("for_you", book)
        assert entry is not None and entry["corrects"] == ["for_you"]


# -------------------------------------------------------------------------- #
# dashboard_fields: the shape, the current value, a summary per fold field
# -------------------------------------------------------------------------- #


class TestWhatDashboardFieldsReturns:
    def _listing(self) -> dict[str, Any]:
        read_values = {
            "for_you": [{"text": "Pick a db"}],
            "items": [{"title": "a", "updated_at": "2026-10-01T00:00:00Z"}],
        }
        return dashboard_agentic.fields_for_agent(
            _inst("project-report"),
            {"groups": [{"code": "wrong_shape", "field": "for_you", "count": 2}]},
            values=read_values,
            written_at={"for_you": "2026-10-08T00:00:00Z"},
        )

    def test_every_field_carries_its_full_schema(self) -> None:
        rows = {row["field"]: row for row in self._listing()["fields"]}
        manifest = json.loads((BUILTIN / "project-report/manifest.json").read_text())
        for name, spec in manifest["fields"].items():
            want = {k: v for k, v in spec.items() if k != "source"}
            assert rows[name]["schema"] == want, name

    def test_an_agentic_field_carries_its_current_value(self) -> None:
        rows = {row["field"]: row for row in self._listing()["fields"]}
        assert rows["for_you"]["written"] is True
        assert rows["for_you"]["value"] == [{"text": "Pick a db"}]
        assert rows["for_you"]["written_at"] == "2026-10-08T00:00:00Z"
        assert rows["verdict"]["written"] is False and "value" not in rows["verdict"]

    def test_a_fold_field_carries_one_summary_line_not_its_data(self) -> None:
        rows = {row["field"]: row for row in self._listing()["fields"]}
        assert rows["items"]["summary"] == "1 item, latest 2026-10-01T00:00:00Z"
        assert "value" not in rows["items"]
        assert rows["omitted"]["summary"] == "no value"

    def test_the_mistake_book_stays(self) -> None:
        mistakes = self._listing()["mistakes"]
        assert mistakes and mistakes[0]["code"] == "wrong_shape"

    def test_the_tool_text_shows_the_shape_and_the_value(self) -> None:
        text = _render_fields(self._listing())
        assert '"required": ["text"]' in text, text
        assert 'current value (written 2026-10-08T00:00:00Z): [{"text": "Pick a db"}]' in text
        assert "verdict (object) -- YOURS to write" in text
        assert "now: 1 item, latest 2026-10-01T00:00:00Z" in text
        assert "Your mistake book" in text


# -------------------------------------------------------------------------- #
# the [DASHBOARD] block on the crewmate's turn
# -------------------------------------------------------------------------- #


class TestTheTurnBlock:
    def test_the_block_names_the_template_and_its_agentic_fields(self) -> None:
        block = dashboard_agentic.turn_block("code-reviewer")
        version = _builtin("project-report").version
        assert block.startswith("[DASHBOARD]\n"), block
        assert f"template project-report v{version}" in block
        # Which readers see it: the tab draws it only with the feature preview on.
        assert "'Dynamic Dashboard' Feature Preview on" in block
        assert "Fields you write: ci, for_you, verdict." in block
        assert "mistake" not in block.lower(), "the mistake book stays behind dashboard_fields"
        assert len(block.splitlines()) <= 4

    def test_the_member_section_carries_the_block(self) -> None:
        from kiro_crew.config.loader import KiroCrewAgentConfig, KiroCrewConfig
        from kiro_crew.context_assembly.member import build_member_section

        cfg = KiroCrewConfig()
        cfg.agents = {"code-reviewer": KiroCrewAgentConfig(kiro_agent="kirocrew-autofix")}
        with patch("kiro_crew.context.KiroCrewConfig.load", return_value=cfg):
            section = build_member_section("code-reviewer")
            withheld = build_member_section("code-reviewer", desk_withheld=True)
        start = section.index("[DASHBOARD]")
        block = section[start : section.index("[END MEMBER IDENTITY]")]
        assert "Fields you write: ci, for_you, verdict." in block
        assert "mistake" not in block.lower()
        assert "[DASHBOARD]" not in withheld, "a turn off the desk is not told about the tab"

    def test_an_unreadable_dashboard_costs_only_the_block(self) -> None:
        with patch("kiro_crew.dashboard_templates.instance.read", side_effect=RuntimeError("boom")):
            assert dashboard_agentic.turn_block("code-reviewer") == ""
