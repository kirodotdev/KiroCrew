"""The shared run-time template format: manifest validation and page parity."""

from __future__ import annotations

import json

import pytest

from kiro_crew.dashboard_templates.manifest import (
    ManifestError,
    check_parity,
    load_template,
    parse_manifest,
)


def _raw(**over):
    raw = {
        "id": "goal-board",
        "version": 1,
        "title": "Goal board",
        "description": "What the crewmate is driving.",
        "source": "builtin",
        "fields": {
            "credits": {"type": "number", "source": {"fold": "usage", "path": "credits"}},
            "phase": {"type": "string", "source": {"agentic": True}},
        },
    }
    raw.update(over)
    return raw


PAGE = '<div><b data-dashboard-field="credits"></b><i data-dashboard-field="phase"></i></div>'


def test_a_valid_manifest_names_its_folds():
    m = parse_manifest(_raw())
    assert m.folds == {"usage"}
    assert m.fields["phase"].agentic and m.fields["phase"].fold is None


def test_every_problem_is_reported_at_once():
    with pytest.raises(ManifestError) as err:
        parse_manifest(
            _raw(
                id="Bad Id",
                version=0,
                fields={
                    "x": {"type": "date", "source": {"fold": "nope", "path": "a..b"}},
                },
            )
        )
    text = str(err.value)
    for needle in ("id", "version", "date", "nope", "a..b"):
        assert needle in text


def test_an_agentic_source_may_not_also_name_a_fold():
    with pytest.raises(ManifestError, match="agentic source"):
        parse_manifest(
            _raw(fields={"p": {"type": "string", "source": {"agentic": True, "fold": "usage"}}})
        )


def test_parity_refuses_both_directions():
    m = parse_manifest(_raw())
    check_parity(m, PAGE)
    with pytest.raises(ManifestError, match="declares 'phase'"):
        check_parity(m, '<div><b data-dashboard-field="credits"></b></div>')
    with pytest.raises(ManifestError, match="binds 'extra'"):
        check_parity(m, PAGE.replace("</div>", '<s data-dashboard-field="extra"></s></div>'))


def test_a_page_may_carry_a_script(tmp_path):
    (tmp_path / "manifest.json").write_text(json.dumps(_raw()))
    (tmp_path / "template.html").write_text(
        PAGE + "<script>const f = window.kirocrew.fields; void f;</script>"
    )
    manifest, html = load_template(tmp_path)
    assert manifest.id == "goal-board" and "<script>" in html
