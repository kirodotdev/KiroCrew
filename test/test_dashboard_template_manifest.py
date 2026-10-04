"""The shared run-time template format: manifest validation and page parity."""

from __future__ import annotations

import json
import re

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


@pytest.mark.parametrize(
    "over, needle",
    [
        ({"fields": {"x": {"type": [], "source": {"agentic": True}}}}, "type []"),
        ({"fields": {"x": {"type": "string", "source": {"fold": {}, "path": "a"}}}}, "fold {}"),
        ({"source": ["builtin"]}, "source ['builtin']"),
    ],
)
def test_a_name_that_is_not_a_string_is_refused_like_any_other_bad_value(over, needle):
    """Every ``in`` check hashes its candidate, so a list or object is refused first."""
    with pytest.raises(ManifestError, match=re.escape(needle)):
        parse_manifest(_raw(**over))


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


def test_a_non_utf8_page_is_refused_as_a_manifest_error(tmp_path):
    """So ONE bad directory isolates itself instead of taking the registry down.

    The scanner's whole posture is that a template it cannot load is skipped and the
    others still load. A bare ``UnicodeDecodeError`` defeats that: it is a
    ``ValueError``, so it is neither an ``OSError`` nor a ``JSONDecodeError``, and it
    travels past the scanner's guard and out of the registry read -- which answers
    500 for every built-in dashboard too, none of which has anything to do with the
    one unreadable file.
    """
    (tmp_path / "manifest.json").write_text(json.dumps(_raw()))
    # Windows-1252, which a template edited on a non-UTF-8 host is saved as.
    (tmp_path / "template.html").write_bytes(
        PAGE.encode("utf-8") + "<p>caf\u00e9</p>".encode("cp1252")
    )
    with pytest.raises(ManifestError):
        load_template(tmp_path)


def test_a_non_utf8_manifest_is_refused_as_a_manifest_error(tmp_path):
    """The same for the other of the two files, which is read first."""
    # `ensure_ascii=False`, or json escapes the accent to \\u00e9 and the bytes come
    # out pure ASCII -- identical under both encodings, so the case would prove
    # nothing while passing.
    (tmp_path / "manifest.json").write_bytes(
        json.dumps(_raw(description="caf\u00e9"), ensure_ascii=False).encode("cp1252")
    )
    (tmp_path / "template.html").write_text(PAGE)
    with pytest.raises(ManifestError):
        load_template(tmp_path)


def test_a_trailing_newline_cannot_pass_an_id_a_field_or_a_path():
    """``\\Z``, not ``$``: Python's ``$`` also matches before a trailing newline.

    An id becomes a user-template DIRECTORY name and a registry key, so ``"report\\n"``
    passing would put a second template beside ``report`` that looks identical to a
    reader and collides with nothing. A field name becomes a DOM attribute and a path
    is walked key by key, so the same hole is worth closing on all three rather than
    on the one an importer happens to reach first.
    """
    from kiro_crew.dashboard_templates.manifest import _FIELD, _ID, _PATH

    for pattern, good in ((_ID, "report"), (_FIELD, "items"), (_PATH, "conductor.slot")):
        assert pattern.match(good), f"{pattern.pattern} refuses the valid {good!r}"
        assert not pattern.match(good + "\n"), (
            f"{pattern.pattern} accepts {good + chr(10)!r}, so a trailing newline "
            "rides through as part of the name"
        )


def test_a_manifest_id_with_a_trailing_newline_is_refused():
    """Through the real entry point, so the pattern is reached the way an import is."""
    with pytest.raises(ManifestError, match="must match"):
        parse_manifest(_raw(id="goal-board\n"))
