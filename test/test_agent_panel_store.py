"""Tests for the crew webview store.

The load-bearing ones are the escaping tests: a conductor ingests issue bodies
and review comments unattended, so a published string is untrusted input that
reaches a rendered document. Everything else here is caps, identity and order.
"""

from __future__ import annotations

import json
import os
import shutil
from fnmatch import fnmatch
from pathlib import Path
from typing import Any

import pytest

from kiro_crew import agent_panel

CREW = "fleet-crew"

#: A crew whose name matches a template the OPERATOR installed. The name-match
#: rule is a property of the store, so it is proven against a template this test
#: drops on disk rather than against any shipped consumer's artifact -- which
#: also exercises the override directory, the seam a shipped template bypasses.
BESPOKE = "bespoke-crew"

#: Shaped like a real bespoke template -- markup around the island and a script
#: that renders the published values -- so the element-count and escaping
#: assertions have structure to count. Every value reaches the DOM as text.
_BESPOKE_HTML = f"""\
<div class="panel"><h2 id="t"></h2><ul id="rows"></ul></div>
<style>.panel {{ color: var(--kc-fg); }}</style>
{agent_panel.DATA_MARKER}
<script>
  var node = document.getElementById('{agent_panel._DATA_ELEMENT_ID}');
  var d = {{}};
  try {{ d = JSON.parse(node.textContent || '{{}}'); }} catch (e) {{ d = {{}}; }}
  document.getElementById('t').textContent = String(d.title || '');
  var rows = document.getElementById('rows');
  (d.workers || []).forEach(function (w) {{
    var li = document.createElement('li');
    li.textContent = String(w.scope) + ' ' + String(w.note);
    rows.appendChild(li);
  }});
</script>
"""


def _install_template(template_id: str, body: str) -> Path:
    """Install *body* as a template an operator dropped on disk.

    Idempotent, so a parametrized case may call it for a template it does not
    use. The data home is per-test, so nothing here escapes the test.
    """
    over = agent_panel.override_templates_dir()
    over.mkdir(parents=True, exist_ok=True)
    path = over / f"{template_id}.html"
    path.write_text(body, encoding="utf-8")
    return path


def _shipped_template_ids() -> list[str]:
    """Every template the package ships, read from the directory itself.

    Derived rather than listed so the rules below (marker, no markup from data,
    body fragment) apply to a template added later without anyone remembering to
    extend a literal. ``shipped_templates_dir()`` is package-relative, so this is
    stable at collection time.
    """
    return sorted(p.stem for p in agent_panel.shipped_templates_dir().glob("*.html"))


SHIPPED = _shipped_template_ids()


def _publish(**over):
    kwargs = {
        "template": "default",
        "data": {"cycle": 47, "holding": 5},
        "title": "fleet",
        "crew": CREW,
    }
    kwargs.update(over)
    return agent_panel.publish(CREW, **kwargs)


# ---------------------------------------------------------------- round trip


def test_publish_then_read_roundtrips():
    written = _publish()
    assert written["schema"] == agent_panel.SCHEMA_VERSION
    got = agent_panel.read(CREW)
    assert got is not None
    assert got["template"] == "default"
    assert got["title"] == "fleet"
    assert got["crew"] == CREW
    assert got["data"] == {"cycle": 47, "holding": 5}
    assert got["published_at"]


def test_the_panel_lives_under_the_gateway_only_trust_root():
    """Not beside the crew's other state, and that is deliberate.

    The record is an ownership authority and a redacted copy of untrusted text, so
    it sits where agent file tools cannot reach it -- the same trust root
    ``members.dm_binding_path`` uses for the DM binding, and for the same reason.
    """
    _publish()
    path = agent_panel.panel_path(CREW)
    assert path.parent == agent_panel.panel_dir()
    assert path.is_file()


def test_read_is_none_for_a_crew_that_never_published():
    assert agent_panel.read("some-other-crew") is None


def test_two_crews_do_not_share_a_panel():
    _publish(data={"mine": 1})
    agent_panel.publish("research-lab", template="default", data={"theirs": 2}, crew="Research Lab")
    assert agent_panel.read(CREW)["data"] == {"mine": 1}
    assert agent_panel.read("research-lab")["data"] == {"theirs": 2}


def test_publish_replaces_rather_than_merges():
    _publish(data={"cycle": 1, "stale": "gone"})
    _publish(data={"cycle": 2})
    assert agent_panel.read(CREW)["data"] == {"cycle": 2}


@pytest.mark.parametrize("bad", ["", "a/b", "a\\b", "..", "has space", "Upper.Case"])
def test_a_path_hostile_crew_slug_is_refused(bad):
    with pytest.raises(agent_panel.CrewSlugError):
        agent_panel.panel_path(bad)


# ------------------------------------------------- which template a crew gets


def test_a_crew_named_after_a_template_gets_that_template():
    """How a bespoke view reaches its crew with no registry and no mapping.

    Installing ``<crew-name>.html`` is the entire act of wiring it up.
    """
    _install_template(BESPOKE, _BESPOKE_HTML)
    assert agent_panel.template_for_crew(BESPOKE) == BESPOKE


def test_a_crew_with_no_template_of_its_own_falls_back_to_the_generic_one():
    assert agent_panel.template_for_crew("research-lab") == agent_panel.DEFAULT_TEMPLATE_ID
    assert agent_panel.template_for_crew("") == agent_panel.DEFAULT_TEMPLATE_ID


def test_a_crew_name_that_could_traverse_never_selects_a_template():
    for hostile in ("../../etc/passwd", "..", "default.html"):
        assert agent_panel.template_for_crew(hostile) == agent_panel.DEFAULT_TEMPLATE_ID


# ------------------------------------------------- redaction before storage


#: Shapes both scanners actually recognise, verified against the redactors
#: themselves rather than guessed. ``SECRET`` is a credential literal;
#: ``EXFIL`` is an OAuth authorize URL carrying a state/code_challenge pair,
#: which is the shape ``redact_exfiltration_urls`` flags -- a plain
#: suspicious-looking host is NOT flagged, and a test built on one would have
#: passed vacuously while proving nothing.
SECRET = "AKIAIOSFODNN7EXAMPLE"
EXFIL = (
    "https://api.notion.com/v1/oauth/authorize?client_id=client123"
    "&response_type=code&state=s3cr3tstate0123456789abcdef0123456789"
    "&code_challenge=chal0123456789abcdef0123456789abcdef01234"
    "&code_challenge_method=S256"
)


def test_a_credential_in_published_data_never_reaches_the_record():
    """A panel is assembled unattended from issue bodies and command output.

    Redacted at PUBLISH, not at render, so the credential is not on disk at all:
    it cannot be read back by a later reader, cannot survive in halves across the
    record's byte ceiling, and is not waiting for the next reader that forgets.
    """
    _publish(data={"note": f"token {SECRET} leaked"})
    stored = agent_panel.read(CREW)
    assert stored is not None
    assert SECRET not in json.dumps(stored)


def test_an_exfiltration_url_in_published_data_never_reaches_the_record():
    _publish(data={"note": f"posting to {EXFIL}"})
    assert EXFIL not in json.dumps(agent_panel.read(CREW))


def test_the_title_is_redacted_too():
    _publish(title=f"cycle 47 {SECRET}")
    assert SECRET not in json.dumps(agent_panel.read(CREW))


def test_redaction_reaches_nested_values_and_keys():
    """A KEY is rendered as a heading, so a token in a field NAME would print
    just as surely as one in a value."""
    _publish(
        data={
            "rows": [{"detail": f"see {SECRET}"}],
            "nested": {"inner": {"deep": f"{EXFIL} here"}},
            f"key-{SECRET}": "value",
        }
    )
    blob = json.dumps(agent_panel.read(CREW))
    assert SECRET not in blob
    assert EXFIL not in blob


def test_redaction_does_not_disturb_ordinary_values():
    """The scrubber must not rewrite text that carries no secret, or every panel
    would read as though something had been withheld."""
    _publish(data={"cycle": 47, "phase": "await ci", "ok": True, "nil": None})
    assert agent_panel.read(CREW)["data"] == {
        "cycle": 47,
        "phase": "await ci",
        "ok": True,
        "nil": None,
    }


def test_the_rendered_document_carries_no_credential():
    """End to end: the composed document is what actually reaches the operator."""
    _publish(data={"note": f"token {SECRET}"})
    doc = agent_panel.render_record(agent_panel.read(CREW))
    assert doc is not None
    assert SECRET not in doc


# ------------------------------------------- ownership is the exact crew name


def test_a_colliding_crew_name_cannot_overwrite_another_crews_panel():
    """``Oncall`` and ``oncall`` slugify to ONE slug, so one panel.json.

    Without this check either crew would silently overwrite the other and the
    operator would read one crew's state under the other's name. Ownership is the
    exact name, following the member layer's existing answer to the same
    lossiness (``record_activity`` matches on session AND the exact member name).
    """
    agent_panel.publish(CREW, template="default", data={"mine": 1}, crew="Oncall")
    with pytest.raises(agent_panel.PanelError) as exc:
        agent_panel.publish(CREW, template="default", data={"theirs": 2}, crew="oncall")
    assert exc.value.code == "crew_slug_collision"
    # The first crew's panel is untouched, which is the property that matters.
    assert agent_panel.read(CREW)["data"] == {"mine": 1}


def test_the_owning_crew_can_still_republish():
    agent_panel.publish(CREW, template="default", data={"cycle": 1}, crew="Oncall")
    agent_panel.publish(CREW, template="default", data={"cycle": 2}, crew="Oncall")
    assert agent_panel.read(CREW)["data"] == {"cycle": 2}


def test_publishing_without_a_crew_is_refused():
    """Ownership is not optional, and this test used to assert the opposite.

    It previously published with ``crew=""`` and asserted that the next publish
    simply won, justified as "a record written before ownership was recorded has
    nothing to compare". That justification was fiction: this schema has never
    shipped, so no stored record predates the ownership field. What the permissive
    path really allowed was a record with an empty ``crew_key``, which BOTH guards
    then skipped -- readable by any crew and overwritable by any crew.
    """
    with pytest.raises(agent_panel.PanelError) as exc:
        agent_panel.publish(CREW, template="default", data={"cycle": 1}, crew="")
    assert exc.value.code == "crew_required"


def test_an_unowned_record_is_replaceable_so_a_forgery_cannot_wedge_a_slug():
    """Unowned records are NOT served, but they ARE replaceable.

    The asymmetry is deliberate. Serving a record nobody owns shows a viewer
    content they cannot attribute, so the forgery succeeds; replacing it destroys
    those bytes and stamps a real owner, so the forgery fails. Refusing the write
    as well would let one forged record permanently deny the real crew its own
    panel -- a containment breach turned into a denial of service.
    """
    agent_panel.publish(CREW, template="default", data={"cycle": 1}, crew="Oncall")
    path = agent_panel.panel_path(CREW)
    forged = json.loads(path.read_text(encoding="utf-8"))
    forged["crew_key"] = ""
    forged["data"] = {"planted": "by nobody"}
    path.write_text(json.dumps(forged), encoding="utf-8")

    agent_panel.publish(CREW, template="default", data={"cycle": 2}, crew="Oncall")
    record = agent_panel.read(CREW)
    assert record["crew_key"] == agent_panel.crew_key("Oncall")
    assert record["data"] == {"cycle": 2}


# ---------------------------------------------------------------- data caps


def test_data_must_be_an_object():
    for bad in ([1, 2], "text", 7, None):
        with pytest.raises(agent_panel.PanelError) as exc:
            _publish(data=bad)
        assert exc.value.code == "data_not_object"


def test_data_over_the_byte_cap_is_refused():
    with pytest.raises(agent_panel.PanelError) as exc:
        _publish(data={"k": "x" * (agent_panel._MAX_DATA_BYTES + 10)})
    assert exc.value.code == "data_too_large"


def test_deeply_nested_data_is_refused():
    node: dict = {}
    cursor = node
    for _ in range(agent_panel._MAX_DATA_DEPTH + 3):
        child: dict = {}
        cursor["n"] = child
        cursor = child
    with pytest.raises(agent_panel.PanelError) as exc:
        _publish(data=node)
    assert exc.value.code == "data_too_deep"


def test_non_serializable_data_is_refused():
    with pytest.raises(agent_panel.PanelError) as exc:
        _publish(data={"when": object()})
    assert exc.value.code == "data_not_serializable"


def test_nan_is_refused():
    # JSON.parse in the frame would throw on NaN, so it is refused at publish
    # rather than rendering a webview that dies in the browser.
    with pytest.raises(agent_panel.PanelError) as exc:
        _publish(data={"ratio": float("nan")})
    assert exc.value.code == "data_not_serializable"


def test_title_is_clamped():
    assert len(_publish(title="t" * 5000)["title"]) == agent_panel._MAX_TITLE


# ---------------------------------------------------------------- templates


@pytest.mark.parametrize(
    "hostile",
    ["../../../../etc/passwd", "..", "de fault", "Default", "default.html", "", "-lead"],
)
def test_a_hostile_template_id_is_refused(hostile):
    with pytest.raises(agent_panel.PanelError) as exc:
        _publish(template=hostile)
    assert exc.value.code == "bad_template_id"


def test_an_unknown_template_is_refused():
    with pytest.raises(agent_panel.PanelError) as exc:
        _publish(template="no-such-template")
    assert exc.value.code == "unknown_template"


@pytest.mark.parametrize("template_id", SHIPPED)
def test_every_shipped_template_resolves_and_carries_the_marker(template_id):
    assert agent_panel.DATA_MARKER in agent_panel.resolve_template(template_id)


def test_the_shipped_set_is_not_empty():
    """Guards the three rules parametrized over :data:`SHIPPED`.

    An empty glob would make each of them collect zero cases and report green
    while asserting nothing, so the set itself is pinned.
    """
    assert agent_panel.DEFAULT_TEMPLATE_ID in SHIPPED


def test_an_installed_template_joins_the_available_set():
    """What makes the name-match rule reachable: listing is directory-driven, so
    an operator adds a template by dropping a file in and nothing else."""
    assert BESPOKE not in agent_panel.available_templates()
    _install_template(BESPOKE, _BESPOKE_HTML)
    assert {agent_panel.DEFAULT_TEMPLATE_ID, BESPOKE} <= set(agent_panel.available_templates())


def test_an_operator_override_wins_over_the_shipped_template():
    over = agent_panel.override_templates_dir()
    over.mkdir(parents=True, exist_ok=True)
    (over / "default.html").write_text("MINE" + agent_panel.DATA_MARKER, encoding="utf-8")
    assert agent_panel.resolve_template("default").startswith("MINE")


def test_a_template_without_the_marker_is_refused():
    over = agent_panel.override_templates_dir()
    over.mkdir(parents=True, exist_ok=True)
    (over / "markerless.html").write_text("<p>no marker</p>", encoding="utf-8")
    with pytest.raises(agent_panel.PanelError) as exc:
        _publish(template="markerless")
    assert exc.value.code == "template_missing_marker"


def test_only_the_first_marker_is_filled():
    out = agent_panel.compose(agent_panel.DATA_MARKER + "|" + agent_panel.DATA_MARKER, "{}")
    assert out.count('id="kirocrew-panel-data"') == 1


# ------------------------------------------------------- the escaping boundary


HOSTILE = '</script><img src=x onerror="alert(1)"><script>'


def _island(doc: str) -> str:
    """The raw text inside the data island, as the HTML parser would see it.

    Slicing to the FIRST ``</script>`` is the point: if published data could
    close the element early, this returns truncated JSON and the parse below
    fails -- which is exactly the regression the test is guarding.
    """
    after = doc.split(f'id="{agent_panel._DATA_ELEMENT_ID}">', 1)[1]
    return after.split("</script>", 1)[0]


def test_published_data_cannot_close_the_script_island():
    """The one that matters: a hostile issue body reaching a rendered webview."""
    _publish(data={"note": HOSTILE})
    doc = agent_panel.render_record(agent_panel.read(CREW))
    assert doc is not None
    island = _island(doc)
    assert json.loads(island) == {"note": HOSTILE}, "the payload was truncated"
    for ch in ("<", ">", "&"):
        assert ch not in island
    assert "\\u003c" in island


@pytest.mark.parametrize("template_id", [agent_panel.DEFAULT_TEMPLATE_ID, BESPOKE])
def test_a_hostile_payload_injects_no_element(template_id):
    """Asserted by COUNTING elements, not by looking for attribute names.

    An escaped payload legitimately still contains the text ``onerror=`` inside a
    JSON string, which is harmless because there is no tag around it. What would
    be a real defect is the element count changing.

    Run against the generic template and against an operator-installed one: the
    guarantee comes from ``compose``, so it must not depend on which template
    happens to be in play.
    """
    _install_template(BESPOKE, _BESPOKE_HTML)
    baseline = agent_panel.compose(agent_panel.resolve_template(template_id), "{}")
    _publish(
        template=template_id,
        data={"title": HOSTILE, "workers": [{"scope": HOSTILE, "note": HOSTILE}]},
    )
    doc = agent_panel.render_record(agent_panel.read(CREW))
    assert doc is not None
    for tag in ("<script", "</script>", "<img", "<div", "<style", "<"):
        assert doc.count(tag) == baseline.count(tag), f"data changed the count of {tag!r}"


def test_escaping_preserves_the_value_exactly():
    data = {"note": HOSTILE, "unicode": "caf\u00e9 \u2028 \u2029", "n": 1.5}
    escaped = agent_panel.escape_json_for_html(json.dumps(data, ensure_ascii=False))
    assert json.loads(escaped) == data


@pytest.mark.parametrize("template_id", SHIPPED)
def test_no_shipped_template_builds_markup_from_data(template_id):
    """Every value must reach the DOM through ``textContent``.

    Asserted on the source because it is a rule about how a template is written,
    not about one payload. Parametrized over the shipped set rather than over a
    template this file wrote, which would only prove the fixture is clean.
    """
    html = agent_panel.resolve_template(template_id)
    for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write"):
        assert sink not in html, f"{template_id} reaches the DOM through {sink}"


@pytest.mark.parametrize("template_id", SHIPPED)
def test_every_shipped_template_is_a_body_fragment(template_id):
    """A whole document would be mangled: the drawer parses this as a fragment
    and wraps it with the strict CSP and the theme variables."""
    html = agent_panel.resolve_template(template_id).lower()
    for tag in ("<html", "<head", "<body", "<!doctype"):
        assert tag not in html, f"{template_id} is a document, not a fragment"


# ------------------------------------------------------------- field order


ORDERED = ["cycle", "holding", "merged", "credits", "aardvark"]


def test_publish_preserves_the_published_field_order():
    """Field order is presentation, not an implementation detail.

    A template renders a stat strip in key order, so serializing with sorted keys
    silently alphabetises the operator's dashboard and leaves the crew no way to
    control it. Caught by looking at a real render: the tiles came out CREDITS,
    CYCLE, HOLDING, MERGED where the crew published cycle first. ``aardvark`` is
    last on purpose -- it sorts first, so a regression cannot pass by accident.
    """
    _publish(data={k: 1 for k in ORDERED})
    assert list(agent_panel.read(CREW)["data"].keys()) == ORDERED


def test_nested_field_order_is_preserved_too():
    _publish(data={"stats": {k: 1 for k in ORDERED}})
    assert list(agent_panel.read(CREW)["data"]["stats"].keys()) == ORDERED


def test_the_rendered_island_carries_the_published_order():
    _publish(data={k: 1 for k in ORDERED})
    doc = agent_panel.render_record(agent_panel.read(CREW))
    assert doc is not None
    island = _island(doc)
    positions = [island.index(f'"{k}"') for k in ORDERED]
    assert positions == sorted(positions), "the island reordered the fields"


# ---------------------------------------------------------------- robustness


def test_read_is_none_for_a_malformed_record():
    _publish()
    agent_panel.panel_path(CREW).write_text("{not json", encoding="utf-8")
    assert agent_panel.read(CREW) is None, "a broken record must show an empty state"


def test_read_is_none_when_the_record_lost_its_data_object():
    _publish()
    agent_panel.panel_path(CREW).write_text(
        json.dumps({"template": "default", "data": "nope"}), encoding="utf-8"
    )
    assert agent_panel.read(CREW) is None


def test_read_is_none_when_the_stored_template_id_is_hostile():
    _publish()
    agent_panel.panel_path(CREW).write_text(
        json.dumps({"template": "../../etc/passwd", "data": {}}), encoding="utf-8"
    )
    assert agent_panel.read(CREW) is None


def test_render_is_none_when_the_template_disappears():
    over = agent_panel.override_templates_dir()
    over.mkdir(parents=True, exist_ok=True)
    (over / "temporary.html").write_text(agent_panel.DATA_MARKER, encoding="utf-8")
    _publish(template="temporary")
    (over / "temporary.html").unlink()
    assert agent_panel.render_record(agent_panel.read(CREW)) is None


def test_render_reflects_a_template_edited_after_publishing():
    """Composition happens on read so an operator editing a template sees it on
    the next drawer open instead of waiting for another crew cycle."""
    _publish()
    over = agent_panel.override_templates_dir()
    over.mkdir(parents=True, exist_ok=True)
    (over / "default.html").write_text("EDITED" + agent_panel.DATA_MARKER, encoding="utf-8")
    doc = agent_panel.render_record(agent_panel.read(CREW))
    assert doc is not None and doc.startswith("EDITED")


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_the_record_is_written_owner_only():
    _publish()
    assert agent_panel.panel_path(CREW).stat().st_mode & 0o077 == 0


# ------------------------------------------------------------ what ships

REPO_ROOT = Path(__file__).resolve().parents[1]


def _declared_package_data() -> list[str]:
    """The ``kiro_crew`` globs from setup.cfg's ``[options.package_data]``."""
    import configparser

    cfg = configparser.ConfigParser()
    cfg.read(REPO_ROOT / "setup.cfg", encoding="utf-8")
    raw = cfg.get("options.package_data", "kiro_crew", fallback="")
    return [
        line.strip()
        for line in raw.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def test_every_shipped_template_is_covered_by_the_declared_package_data():
    """A shipped template that pip does not copy makes the feature 400 everywhere.

    ``resolve_template`` reads the package-relative directory, which EXISTS in a
    source checkout and in the test suite -- so the whole feature can be green
    locally while ``panel_publish`` returns ``unknown_template`` on every
    pip/PyPI/DMG install, because the templates were never copied into
    site-packages. Nothing else in the suite can see that: every other test reads
    the same source tree that hides it.

    Asserted per FILE against the declared globs rather than by looking for one
    known line, so adding ``pipeline-conductor.html`` without extending the
    declaration fails here instead of shipping a dead feature.
    """
    declared = _declared_package_data()
    shipped = sorted(agent_panel.shipped_templates_dir().glob("*.html"))
    assert shipped, "no shipped templates found -- this test would be vacuous"
    pkg = REPO_ROOT / "src" / "kiro_crew"
    for path in shipped:
        rel = path.relative_to(pkg).as_posix()
        assert any(fnmatch(rel, glob) for glob in declared), (
            f"{rel} ships in the repo but no [options.package_data] glob covers it, "
            "so pip will not copy it into site-packages"
        )


def test_the_sdist_manifest_also_ships_the_templates():
    """setup.cfg alone is not enough: ``python -m build`` builds the wheel FROM
    the sdist, and the sdist takes its contents from MANIFEST.in."""
    manifest = (REPO_ROOT / "MANIFEST.in").read_text(encoding="utf-8")
    assert "agent_panel_templates" in manifest, (
        "MANIFEST.in does not ship the template directory, so the sdist -- and "
        "therefore the published wheel built from it -- would omit it"
    )


# ---------------------------------------------------------------- the fence


def test_the_template_directory_is_fenced_from_agent_file_tools():
    """A crew must not be able to write its own template.

    The whole value of splitting template from data is that layout is authored by
    a human and only data comes from the crew. An agent file tool that could drop
    a .html into the template directory would collapse that distinction and hand
    a hostile issue body a way to author markup directly.
    """
    from kiro_crew import security

    assert agent_panel.TEMPLATES_DIRNAME in security._CREW_SECRET_LEAVES


def test_the_record_is_fenced_from_agent_file_tools_too():
    """The RECORD, not just the template. This is the cross-crew forgery guard.

    ``members/`` is deliberately unfenced -- a crew owns its own published data --
    and that reasoning holds for a crew's OWN panel and fails for everyone else's:
    under ``members/<slug>/`` nothing stopped one crew writing
    ``members/<other-crew>/panel.json`` directly, forging another crew's state
    past ownership resolution AND past the redactors in a single write, with the
    drawer rendering the result.
    """
    from kiro_crew import security

    assert agent_panel._PANELS_DIRNAME in security._CREW_SECRET_LEAVES
    assert agent_panel._PANELS_DIRNAME in agent_panel.panel_path(CREW).parts


def test_the_record_is_masked_from_a_sandboxed_command():
    """The write path the FILE-TOOL fence cannot reach.

    A sandboxed command builds its own path at runtime, so there is no literal for
    command matching to catch -- the tool gate above is simply not on that path.
    Only an OS-level mask closes it.

    This is why the record is not under ``trust/``: ``trust`` is a declared
    read-write exception for the sandbox, so a record beneath it stayed writable.
    """
    from kiro_crew import sandbox

    assert agent_panel._PANELS_DIRNAME in sandbox._CREW_HIDDEN_LEAVES
    assert agent_panel._PANELS_DIRNAME not in sandbox._CREW_SANDBOX_VISIBLE_LEAVES
    assert agent_panel._PANELS_DIRNAME not in sandbox._CREW_READONLY_LEAVES
    # Not under any leaf the sandbox deliberately keeps writable.
    for visible in sandbox._CREW_SANDBOX_VISIBLE_LEAVES:
        assert (
            not agent_panel.panel_dir()
            .as_posix()
            .endswith(f"/{visible}/{agent_panel._PANELS_DIRNAME}")
        ), f"the record sits under {visible!r}, which the sandbox keeps read-write"


def test_the_mask_is_materialised_so_it_applies_on_a_fresh_install():
    """A hidden disposition only holds for a path that EXISTS.

    The Linux mask is a bind mount and its loop guards on ``isdir``, so an absent
    directory is skipped silently -- and skipped exactly on the fresh install where
    the agent could create it first and write what the gateway later reads back as
    authoritative. The W5 lesson, one list over.
    """
    from pathlib import PurePath

    from kiro_crew import sandbox

    assert agent_panel._PANELS_DIRNAME in sandbox._CREW_PRECREATE_HIDDEN_DIR_LEAVES
    dir_targets, _files = sandbox._sealable_absent_ceilings()
    # Compared by path COMPONENT, not by a "/"-prefixed suffix: these are built with
    # ``os.path.join``, so on Windows the separator is a backslash and a hard-coded
    # "/" made this pass on Linux and fail there for a reason that had nothing to do
    # with the property. The list membership itself holds on every platform -- only
    # the bind-mount that consumes it is POSIX-only, and that is asserted in
    # ``test_sandbox_absent_ceiling_seal.py`` behind its own POSIX gate.
    assert any(
        PurePath(t).name == agent_panel._PANELS_DIRNAME for t in dir_targets
    ), "the records directory is not among the paths the launcher materialises"


def test_the_publishing_tool_never_writes_the_record_itself():
    """Write path 1 of 3: the MCP route runs in the GATEWAY, not the crew.

    This is what makes masking the directory free rather than a breakage: the tool
    module does not import this store at all, it POSTs to the gateway, so hiding
    the bytes from every sandboxed process costs no live consumer. If a future
    edit imports the store into the tool module, that import IS the regression --
    the tool would then need the directory writable from inside the sandbox.
    """
    from pathlib import Path as _P

    src = (_P(agent_panel.__file__).parent / "mcp_panel.py").read_text()
    assert "agent_panel" not in src, (
        "mcp_panel imports the panel store; the record directory is masked from "
        "sandboxed processes, so writes must stay on the gateway's HTTP route"
    )
    assert "/api/agent-panel/publish" in src


def test_the_record_no_longer_lives_in_the_unfenced_member_space():
    """Pinned as a NEGATIVE so a future tidy-up cannot move it back."""
    from kiro_crew import members as members_mod

    assert members_mod.MEMBERS_DIR_NAME not in agent_panel.panel_path(CREW).parts


def test_a_hostile_slug_cannot_escape_the_records_directory():
    for bad in ("", "a/b", "a\\b", "..", "has space", "Upper.Case", "../../etc/passwd"):
        with pytest.raises(agent_panel.CrewSlugError):
            agent_panel.panel_path(bad)


def test_the_ownership_check_happens_under_the_lock():
    """The TOCTOU guard, asserted on the SOURCE because a race is not reproducible.

    ``read`` before the lock is not a weaker check, it is no check: two crews
    colliding on one slug both observe "no owner", both pass, and the later write
    silently overwrites the first -- the exact outcome the check exists to
    prevent. Asserting on order-in-source is crude but it is the property, and a
    timing test would pass on a machine that happened not to interleave.
    """
    import inspect

    src = inspect.getsource(agent_panel.publish)
    lock_at = src.index("with _locked(")
    read_at = src.index("existing = read(slug)")
    assert read_at > lock_at, (
        "the ownership read happens BEFORE the lock is taken, which makes the "
        "collision check a TOCTOU race rather than a guard"
    )


# ------------------------------------------------------------- the import cycle


def test_the_publish_stamp_carries_a_zone_offset():
    """A zone-less stamp is read as the BROWSER's local time by the drawer.

    ``new Date('2026-09-04T22:30:18')`` is local-time in JS. On the loopback
    dashboard that is the same clock that wrote it, so the bug is invisible; from a
    remote browser every age is skewed by the offset between the two zones. Pinned
    because it is invisible in exactly the configuration a developer tests in.
    """
    from datetime import datetime

    stamp = agent_panel._now_iso()
    parsed = datetime.fromisoformat(stamp)
    assert parsed.tzinfo is not None, f"{stamp!r} has no zone; a remote drawer will skew it"
    assert parsed.utcoffset() is not None
    # And it is what lands in the record.
    assert datetime.fromisoformat(str(_publish()["published_at"])).tzinfo is not None


def test_a_payload_deeper_than_the_cap_is_refused_not_a_recursion_error():
    """The cap must be reached BEFORE anything recurses without one.

    ``_scrub_published`` walks the payload with no depth limit of its own, so with
    the scrub running first a deep object blew the stack inside the scrubber --
    an uncaught RecursionError and a 500, for input the cap exists to refuse
    politely. Built well past the interpreter's own limit so a regression cannot
    pass by happening to fit.
    """
    deep: dict[str, Any] = {"leaf": 1}
    for _ in range(2000):
        deep = {"nest": deep}

    with pytest.raises(agent_panel.PanelError) as caught:
        _publish(data=deep)
    assert caught.value.code == "data_too_deep"


def test_a_deep_payload_inside_a_list_is_refused_too():
    """Lists recurse as well; a cap that only counted dicts would miss this."""
    deep: Any = {"leaf": 1}
    for _ in range(2000):
        deep = [deep]

    with pytest.raises(agent_panel.PanelError) as caught:
        _publish(data={"rows": deep})
    assert caught.value.code == "data_too_deep"


def test_keys_that_collide_after_redaction_are_refused_not_silently_merged():
    """Redaction is many-to-one, so it must not be used as an identity.

    Two distinct credential-shaped field names both become the same redacted
    string. A dict comprehension keeps the last, and the operator reads a panel
    quietly missing a field with nothing on screen saying so -- the same failure
    as the slug collision, one layer down.
    """
    # Assembled so no literal credential is committed; see the note in
    # test_mcp_panel_runtime.py.
    a = "-".join(["xoxb", "111111111111", "1111111111111", "aaaaaaaaaaaaaaaaaaaaaaaa"])
    b = "-".join(["xoxb", "222222222222", "2222222222222", "bbbbbbbbbbbbbbbbbbbbbbbb"])
    assert agent_panel._scrub(a) == agent_panel._scrub(b), "fixture no longer collides"

    with pytest.raises(agent_panel.PanelError) as caught:
        _publish(data={a: 1, b: 2})
    assert caught.value.code == "redacted_key_collision"


def test_a_collision_nested_inside_the_payload_is_also_refused():
    """The check lives in the recursive walk, not only at the top level."""
    a = "-".join(["xoxb", "111111111111", "1111111111111", "aaaaaaaaaaaaaaaaaaaaaaaa"])
    b = "-".join(["xoxb", "222222222222", "2222222222222", "bbbbbbbbbbbbbbbbbbbbbbbb"])

    with pytest.raises(agent_panel.PanelError) as caught:
        _publish(data={"outer": {a: 1, b: 2}})
    assert caught.value.code == "redacted_key_collision"


def test_distinct_keys_that_survive_redaction_are_all_kept():
    """The refusal above must not become "reject anything that redacts".

    Ordinary keys are untouched, and two keys that merely both CONTAIN a redacted
    value stay distinct as long as their names do.
    """
    record = _publish(data={"cycle": 1, "holding": 2, "credits": 3})
    assert list(record["data"].keys()) == ["cycle", "holding", "credits"]


def test_a_symlinked_records_directory_is_refused_not_followed(tmp_path):
    """The STORE half of the same guard, independent of the launcher.

    ``.resolve()`` followed the link, so the record was written through to the
    target while the fence and the mask attached to the link name. Refused here as
    well as at spawn time, because a tool or script can reach this store without a
    sandbox ever being launched.
    """
    root = agent_panel.data_home() / agent_panel._PANELS_DIRNAME
    if root.exists():
        shutil.rmtree(root)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    root.symlink_to(elsewhere, target_is_directory=True)

    with pytest.raises(agent_panel.PanelError) as caught:
        agent_panel.panel_dir()
    assert caught.value.code == "panel_dir_is_a_symlink"

    # And publishing through it does not happen either.
    with pytest.raises(agent_panel.PanelError):
        _publish()
    assert not list(elsewhere.iterdir()), "a record was written through the link"


def test_a_symlinked_template_directory_is_refused_too(tmp_path):
    """Same guard, and the consequence here is markup rather than a record.

    The override template directory is where a human authors the layout; the
    template/data split is the containment story, so a directory a sandboxed
    process can substitute is a path to authoring markup directly.
    """
    root = agent_panel.data_home() / agent_panel.TEMPLATES_DIRNAME
    if root.exists():
        shutil.rmtree(root)
    elsewhere = tmp_path / "tpl-elsewhere"
    elsewhere.mkdir()
    root.symlink_to(elsewhere, target_is_directory=True)

    with pytest.raises(agent_panel.PanelError) as caught:
        agent_panel.override_templates_dir()
    assert caught.value.code == "panel_dir_is_a_symlink"


def test_an_absent_records_directory_is_still_fine():
    """Absent is not aliased. `publish` creates it; only an ALIAS is refused."""
    root = agent_panel.data_home() / agent_panel._PANELS_DIRNAME
    if root.exists():
        shutil.rmtree(root)
    assert agent_panel.panel_dir() == root  # no raise
    record = _publish()
    assert record["data"]
    assert agent_panel.panel_path(CREW).is_file()


def test_a_real_records_directory_is_accepted():
    """The refusal must not reject the ordinary case."""
    (agent_panel.data_home() / agent_panel._PANELS_DIRNAME).mkdir(parents=True, exist_ok=True)
    assert _publish()["data"]


def test_the_module_docstring_does_not_describe_the_abandoned_storage():
    """A docstring that teaches the rejected security model is worse than none.

    This module's storage moved twice (out of ``members/``, then out of ``trust/``)
    and the docstring was updated partly both times, leaving a reader to derive the
    model the code had rejected -- including that published data is unfenced, which
    is now the opposite of the property. Pinned against the CODE rather than against
    a wording, so it fails on the next move too.
    """
    doc = agent_panel.__doc__ or ""
    assert doc, "no module docstring -- this test would be vacuous"

    # The abandoned layout must not be described as where a panel lives.
    assert "member_dir(slug)/panel.json" not in doc
    assert "trust/crew-panels" not in doc
    # The live layout must be.
    assert f"{agent_panel._PANELS_DIRNAME}/<slug>.json" in doc

    from kiro_crew import sandbox, security

    # And every disposition the docstring claims is actually declared.
    assert agent_panel._PANELS_DIRNAME in security._CREW_SECRET_LEAVES
    assert agent_panel._PANELS_DIRNAME in sandbox._CREW_HIDDEN_LEAVES
    assert agent_panel.TEMPLATES_DIRNAME in security._CREW_SECRET_LEAVES


def test_every_protected_leaf_this_store_uses_refuses_an_alias():
    """The invariant, asserted across both leaves at once.

    Four findings probed the same fence in four ways. This is the derived form of
    the answer: whatever leaf this store depends on must be declared no-alias, so a
    fifth leaf cannot be added with the guard silently skipped.
    """
    from kiro_crew import sandbox

    for leaf in (agent_panel._PANELS_DIRNAME, agent_panel.TEMPLATES_DIRNAME):
        assert leaf in sandbox._CREW_NO_ALIAS_LEAVES, f"{leaf} may be aliased"


def test_the_mirrored_member_layout_matches_members():
    """The store derives its paths itself; this is the anti-drift pin.

    Only the SLUG PATTERN is mirrored now. The records directory is no longer a
    sibling of anything ``members`` owns -- it is its own sandbox-masked leaf -- so
    there is no directory name left to keep in step, and pinning one would invent a
    coupling the layout does not have. The slug is still shared: both modules key
    per-crew state by it, and a widened pattern on one side would let a name
    through that the other refuses.
    """
    from kiro_crew import members as members_mod

    assert agent_panel._SLUG_RE.pattern == members_mod._SLUG_RE.pattern


def test_the_store_works_as_an_entry_point(tmp_path):
    """``members`` must not be imported at this module's scope.

    It pulls in ``artifacts`` -> ``hooks`` -> ``webhooks`` -> ``validation``,
    which imports ``artifacts`` back. The cycle is LATENT whenever something
    else has already imported that chain -- which the test suite always has --
    so a module-scope import reads as green here and raises ``ImportError`` the
    moment this module is the first ``kiro_crew`` import in a process. Run in a
    subprocess for exactly that reason: importing it in-process proves nothing.
    """
    import subprocess
    import sys
    from pathlib import Path

    src = str(Path(__file__).resolve().parents[1] / "src")
    # Reaches a PATH, not just the import: a lazy module-scope import would pass
    # an import-only probe and still fail here, which is how the first attempt at
    # this fix read as green.
    probe = (
        "from kiro_crew import agent_panel;"
        " print(agent_panel.panel_path('fleet-crew'));"
        " print(agent_panel.SCHEMA_VERSION)"
    )
    # The child gets a HOME OF ITS OWN, and the env is built by copying rather
    # than replacing. A bare ``env={"PYTHONPATH": ..., "PATH": ...}`` drops
    # ``KIROCREW_HOME``, so ``panel_dir`` resolved ``data_home()`` to the
    # OPERATOR's real directory and the probe created it -- a test writing
    # outside its sandbox, on the machine of whoever ran the suite. ``cwd`` is
    # moved under ``tmp_path`` too, so anything the child resolves relatively
    # also lands in the sandbox.
    home = tmp_path / "child-home"
    home.mkdir()
    env = dict(os.environ)
    env["PYTHONPATH"] = src
    env["KIROCREW_HOME"] = str(home)
    out = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        # Pinned rather than left to the locale: without it the child's output
        # decodes with the Windows ANSI code page, and this probe compares the
        # decoded path against `tmp_path`.
        encoding="utf-8",
        timeout=120,
        env=env,
        cwd=str(tmp_path),
    )
    assert out.returncode == 0, out.stderr[-1500:]
    resolved, schema = out.stdout.strip().splitlines()
    assert schema == str(agent_panel.SCHEMA_VERSION)
    # The path the child resolved must sit under the sandbox home. This is the
    # assertion that keeps the env from silently re-escaping: it fails if a
    # future edit drops ``KIROCREW_HOME`` again, instead of quietly creating a
    # directory in the operator's home the way the first version did.
    assert Path(resolved).is_relative_to(home.resolve()), resolved


def test_a_multi_word_crew_name_reaches_its_own_template(tmp_path, monkeypatch):
    """The documented wiring act -- install ``<crew-name>.html`` -- must work for
    a name with a space in it.

    This is the case the mechanism is FOR: a bespoke template is worth writing
    for "Pipeline Conductor", not for a crew whose name is already one lowercase
    word. Lowercasing alone left the space in place, ``TEMPLATE_ID_RE`` rejects
    a space, and every such crew silently got the generic template no matter
    what was installed -- the feature was dead for its whole intended audience.
    """
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    d = agent_panel.override_templates_dir()
    d.mkdir(parents=True, exist_ok=True)
    (d / "pipeline-conductor.html").write_text("<p>bespoke</p>", encoding="utf-8")

    assert agent_panel.template_for_crew("Pipeline Conductor") == "pipeline-conductor"
    # Case and surrounding whitespace are equally not the crew's problem.
    assert agent_panel.template_for_crew("  PIPELINE   Conductor ") == "pipeline-conductor"
    # A crew with no bespoke template still falls back, rather than erroring.
    assert agent_panel.template_for_crew("Some Other Crew") == agent_panel.DEFAULT_TEMPLATE_ID


def test_the_mirrored_slugify_matches_members(tmp_path):
    """`_slug_candidate` is a mirror; this is its anti-drift pin.

    ``members.slug_for_name`` is the authority for how a crew name becomes a
    slug, but it cannot be imported here (its module is on the import cycle this
    store deliberately stays off). The mirror is therefore pinned against it
    over a table of names, so a change on either side fails a test rather than
    silently sending a crew to the wrong template.
    """
    from kiro_crew import members as members_mod

    for name in [
        "Pipeline Conductor",
        "Issue Radar",
        "crew",
        "Crew  With   Runs",
        "Ünïcode Crew",
        "trailing-punctuation!!!",
        "MiXeD CaSe Name",
    ]:
        mirrored = agent_panel._slug_candidate(name)
        assert mirrored == members_mod.slug_for_name(name), (
            f"mirror drifted from members.slug_for_name for {name!r}: "
            f"{mirrored!r} != {members_mod.slug_for_name(name)!r}"
        )


def test_an_orphaned_record_is_taken_over_rather_than_wedging_the_slug():
    """A renamed or deleted crew must not own its slug forever.

    Strict ownership, on its own, made one: the departed crew's record kept the
    slug, every later crew reaching that slug was refused, and the refusal advised
    renaming a crew that no longer exists. The only recovery left was hand-deleting
    a file in a directory that is gateway-only AND hidden from the agent sandbox --
    a containment guard turned into a permanent denial of service.
    """
    agent_panel.publish(CREW, template="default", data={"theirs": 1}, crew="Departed Crew")

    # Nobody by that name is on the roster any more.
    record = agent_panel.publish(
        CREW,
        template="default",
        data={"mine": 2},
        crew="New Crew",
        owner_is_live=lambda _key: False,
    )
    assert record["data"] == {"mine": 2}
    assert agent_panel.read(CREW)["crew_key"] == agent_panel.crew_key("New Crew")


def test_a_live_owner_still_refuses_the_colliding_write():
    """The takeover must not weaken the guard it is carved out of.

    When the owning crew DOES still exist the refusal is correct and its advice is
    actionable, because there is another crew to rename.
    """
    agent_panel.publish(CREW, template="default", data={"theirs": 1}, crew="Oncall")
    with pytest.raises(agent_panel.PanelError) as exc:
        agent_panel.publish(
            CREW,
            template="default",
            data={"mine": 2},
            crew="oncall",
            owner_is_live=lambda _key: True,
        )
    assert exc.value.code == "crew_slug_collision"
    assert agent_panel.read(CREW)["data"] == {"theirs": 1}


def test_omitting_the_liveness_check_keeps_the_strict_refusal():
    """The default is the strict one: a caller that cannot answer the liveness
    question does not get a takeover by accident."""
    agent_panel.publish(CREW, template="default", data={"theirs": 1}, crew="Oncall")
    with pytest.raises(agent_panel.PanelError) as exc:
        agent_panel.publish(CREW, template="default", data={"mine": 2}, crew="oncall")
    assert exc.value.code == "crew_slug_collision"
