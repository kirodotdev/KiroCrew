"""The dashboard package renderer: both catalogs, every block, every field type.

The tests that matter here ENUMERATE the catalogs rather than restating them. A
hand-written list of block types would pass forever after someone adds an
eleventh, and the symptom of that is a blank panel on an operator's own page
with every other gate green.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

import pytest

from kiro_crew.artifact_store.dashboard_package import (
    data_type_catalog,
    parse_package,
    validate_package,
    view_block_catalog,
)
from kiro_crew.artifact_store.model import ArtifactValidationError
from kiro_crew.dashboard_frame import (
    BINDING_ATTRIBUTE,
    DATA_MESSAGE_TYPE,
    PAGE_EVENT,
    WINDOW_GLOBAL,
    read_payload,
)
from kiro_crew.dashboard_package_render import (  # noqa: F401
    _EM_DASH,
    _FORMATTERS,
    BLOCK_PATCH_MESSAGE_TYPE,
    BLOCK_RENDERERS,
    RENDER_CSP,
    THEMES,
    VENDOR_FILES,
    VENDOR_VERSIONS,
    _as_number,
    block_patch,
    blocks_reading,
    display_values,
    render_block,
    render_dashboard,
    theme_css,
    theme_tokens,
    unrenderable_types,
    vendor_source,
)

# --------------------------------------------------------------------------- #
# Fixtures built FROM the catalog, so a type added upstream is exercised here
# without an edit. The screenshot harness imports these same builders, so the
# pictures and the assertions are about the same documents.
# --------------------------------------------------------------------------- #

#: One sample value per data type, and the shape keys that type requires.
SAMPLES: dict[str, dict[str, Any]] = {
    "number": {"value": 1842.5, "keys": {"unit": "cr", "precision": 1}},
    "text": {"value": "Two dashboard PRs are green and need one approval.", "keys": {}},
    "timestamp": {"value": "2026-10-09T23:30:00Z", "keys": {}},
    "enum": {"value": "waiting", "keys": {"choices": ["green", "waiting", "red"]}},
    "bool": {"value": True, "keys": {}},
}

_FOLD_SOURCE = {"fold": "work", "path": "summary.done"}


def _field(kind: str, index: int) -> tuple[str, dict[str, Any], Any]:
    """A model field of *kind*, its spec, and a value for it.

    A number's value RISES with its index. Equal numbers would make every bar
    full and every gauge complete, so the share arithmetic -- the one thing a
    bar and a ring are -- would be drawn identically whether it worked or not.
    """
    sample = SAMPLES[kind]
    name = f"{kind}_{index}"
    spec: dict[str, Any] = {
        "type": kind,
        "label": f"{kind.title()} {index}",
        "source": dict(_FOLD_SOURCE),
        **sample["keys"],
    }
    value = sample["value"]
    if kind == "number":
        value = round(value * (1 + 0.42 * index), 1)
    return name, spec, value


def kinds_for_block(block_type: str, focus: str | None = None) -> list[str]:
    """The data types to place in one block of *block_type*.

    With no *focus*, every type the block accepts -- which is the package a
    screenshot of that block should show. With a *focus*, that type and only the
    ``requires`` types beside it, because a block whose ``max_fields`` is
    narrower than its ``accepts`` (``stat`` takes one field and draws five
    types) cannot show them all at once and a per-pair package is the only way
    to assert each one.
    """
    entry = view_block_catalog()[block_type]
    required = sorted(entry.requires)
    rest = [focus] if focus else sorted(entry.accepts - entry.requires)
    wanted = required + [k for k in rest if k != focus or focus not in required]
    if focus and focus in required:
        wanted = required
    filler = wanted[-1] if wanted else "number"
    while len(wanted) < entry.min_fields:
        wanted.append(filler)
    # A block that accepts ONE data type (bars, gauge, orbit) would otherwise be
    # exercised and photographed with a single field, where a bar is always full
    # and a ring has no total -- the two cases that cannot fail. Fill it to four.
    if len(set(wanted)) == 1:
        while len(wanted) < min(entry.max_fields, 4):
            wanted.append(filler)
    return wanted[: entry.max_fields]


def package_for_block(
    block_type: str, focus: str | None = None
) -> tuple[dict[str, Any], dict[str, Any]]:
    """A valid package holding ONE block of *block_type*, and values for its fields.

    Built from the block's own catalog entry -- ``accepts``, ``requires``,
    ``min_fields``, ``max_fields`` -- so it is a package the write gate accepts
    rather than one a test asserts about and the product would refuse.
    """
    wanted = kinds_for_block(block_type, focus)
    entry = view_block_catalog()[block_type]
    types: dict[str, Any] = {}
    values: dict[str, Any] = {}
    names: list[str] = []
    for index, kind in enumerate(wanted):
        name, spec, value = _field(kind, index)
        types[name] = spec
        values[name] = value
        names.append(name)
    raw = {
        "kind": "dashboard",
        "bound_to": "crewmate:kirocrew-lead",
        "model": {"types": types},
        "view": {
            "blocks": [
                {
                    "id": block_type.replace("_", "-"),
                    "type": block_type,
                    "fields": names,
                    "title": entry.summary[:110],
                    "caption": f"Accepts {', '.join(sorted(entry.accepts))}.",
                }
            ]
        },
        "theme": {"tokens": {"--accent": "#0f766e"}},
    }
    return validate_package(raw), values


def every_block_package() -> tuple[dict[str, Any], dict[str, Any]]:
    """One package placing EVERY block type in the catalog, each over its accepts."""
    types: dict[str, Any] = {}
    values: dict[str, Any] = {}
    blocks: list[dict[str, Any]] = []
    for block_type in sorted(view_block_catalog()):
        one, one_values = package_for_block(block_type)
        prefix = block_type.replace("_", "")
        for name, spec in one["model"]["types"].items():
            types[f"{prefix}_{name}"] = spec
        for name, value in one_values.items():
            values[f"{prefix}_{name}"] = value
        block = dict(one["view"]["blocks"][0])
        block["fields"] = [f"{prefix}_{n}" for n in block["fields"]]
        blocks.append(block)
    raw = {
        "kind": "dashboard",
        "bound_to": "crewmate:kirocrew-lead",
        "model": {"types": types},
        "view": {"blocks": blocks},
        "theme": {"tokens": {"--accent": "#0f766e"}},
    }
    return validate_package(raw), values


BLOCK_TYPES = sorted(view_block_catalog())
TYPE_PAIRS = sorted(
    (block, kind) for block, entry in view_block_catalog().items() for kind in entry.accepts
)


def _render(package: dict[str, Any], values: dict[str, Any], theme: str = "light") -> str:
    return render_dashboard(package, read_payload(values), theme=theme)


_ISLAND_RE = re.compile(r'<script type="application/json".*?</script>', re.S)


def markup_of(html: str) -> str:
    """The document minus its data islands and minus the inlined vendor sources.

    What is left is the markup and the script THIS module wrote. The separation
    matters for two kinds of assertion that would otherwise be unwritable: a
    vendored library's own source legitimately contains ``https://`` in a
    comment (three.js r159 opens with a deprecation notice linking to its own
    docs), and the inert JSON island legitimately carries a value's raw text.
    Neither is markup the renderer emitted, and conflating them hides a real
    finding behind a noisy one.

    The vendor sources are removed by their EXACT bytes rather than by a pattern
    over ``<script>``: a pattern that happened to stop early would silently
    leave library source in the string every one of these assertions reads.
    """
    stripped = _ISLAND_RE.sub("", html)
    for name in VENDOR_FILES:
        stripped = stripped.replace(vendor_source(name), "")
    return stripped


def tags_of(html: str) -> list[str]:
    """Every tag in the markup, in order -- the document's structure as a list."""
    return re.findall(r"<[a-zA-Z/!][^>]*>", markup_of(html))


def island_of(html: str) -> dict[str, Any]:
    """The read payload the document carries, parsed back out of it."""
    found = re.search(r'id="kirocrew-dashboard-data">(.*?)</script>', html, re.S)
    assert found is not None
    return json.loads(found.group(1))


# --------------------------------------------------------------------------- #
# The two halves agree
# --------------------------------------------------------------------------- #


def test_the_renderers_are_exactly_the_block_catalog():
    """Both directions: no block type without a renderer, no renderer without a type."""
    assert set(BLOCK_RENDERERS) == set(view_block_catalog())


def test_nothing_either_catalog_admits_is_unrenderable():
    assert unrenderable_types() == {"blocks": [], "fields": []}


def test_the_formatter_table_is_the_data_catalog():
    """A data type the data line adds with no formatter here is named by this test."""
    assert set(_FORMATTERS) == set(data_type_catalog())


def test_the_catalog_is_no_longer_the_four_type_stub():
    """The starter set the package line left behind is replaced, not extended."""
    assert set(BLOCK_TYPES) > {"stat", "table", "list", "timeline"}
    assert len(BLOCK_TYPES) >= 10


@pytest.mark.parametrize("block_type", BLOCK_TYPES)
def test_every_block_type_renders_its_own_markup(block_type):
    package, values = package_for_block(block_type)
    html = _render(package, values)
    assert f'class="pkg-block pkg-block--{block_type}"' in html
    # The CLASS, not the word: `.pkg-unrenderable` is a rule in the stylesheet
    # of every document, so the bare word is present whether a block fell back
    # or not and the assertion would never have failed.
    assert 'class="pkg-unrenderable"' not in html


@pytest.mark.parametrize(("block_type", "kind"), TYPE_PAIRS)
def test_every_accepted_data_type_renders_inside_the_block(block_type, kind):
    """A block that accepts a type must DRAW it, not drop it.

    The marker is the CLASS ATTRIBUTE ``class="pkg-v pkg-v--<type>"``, written
    only by the branch of ``_value_cell`` that actually rendered that type -- so
    its presence says the type was recognised, and the binding beside it says
    the host can refill it.

    Not the bare string ``pkg-v--<type>``: the stylesheet of every document
    carries ``.pkg-v--timestamp`` as a RULE, so the bare form is present whether
    the value rendered as a timestamp or fell through to the unknown branch. A
    mutation renaming the timestamp branch left this test green until the
    assertion moved to the attribute.
    """
    package, values = package_for_block(block_type, focus=kind)
    html = _render(package, values)
    name = next(n for n, s in package["model"]["types"].items() if s["type"] == kind)
    marker = f'class="pkg-v pkg-v--{kind}"'
    assert marker in html, f"{block_type} dropped a {kind} field"
    assert f'{BINDING_ATTRIBUTE}="{name}"' in html
    assert "pkg-v--unknown" not in markup_of(html)


@pytest.mark.parametrize(("block_type", "kind"), TYPE_PAIRS)
def test_an_accepted_data_type_renders_in_both_themes(block_type, kind):
    package, values = package_for_block(block_type, focus=kind)
    marker = f'class="pkg-v pkg-v--{kind}"'
    for theme in THEMES:
        assert marker in _render(package, values, theme)


# --------------------------------------------------------------------------- #
# accepts / requires are a GATE, not a docstring
# --------------------------------------------------------------------------- #


def _package_with(block_type: str, kinds: list[str]) -> dict[str, Any]:
    types: dict[str, Any] = {}
    names: list[str] = []
    for index, kind in enumerate(kinds):
        name, spec, _ = _field(kind, index)
        types[name] = spec
        names.append(name)
    return {
        "kind": "dashboard",
        "bound_to": "crewmate:a",
        "model": {"types": types},
        "view": {"blocks": [{"id": "b", "type": block_type, "fields": names}]},
        "theme": {"tokens": {}},
    }


@pytest.mark.parametrize(
    ("block_type", "kinds", "word"),
    [
        ("bars", ["text"], "draws ['number']"),
        ("gauge", ["enum"], "draws ['number']"),
        ("orbit", ["number", "text"], "draws ['number']"),
        ("pills", ["number"], "draws ['bool', 'enum']"),
        ("timeline", ["number"], "draws ['enum', 'text', 'timestamp']"),
    ],
)
def test_a_block_is_refused_a_data_type_it_cannot_draw(block_type, kinds, word):
    with pytest.raises(ArtifactValidationError) as caught:
        validate_package(_package_with(block_type, kinds))
    assert word in str(caught.value)


def test_a_timeline_with_nothing_to_order_by_is_refused():
    with pytest.raises(ArtifactValidationError) as caught:
        validate_package(_package_with("timeline", ["text", "enum"]))
    assert "at least one field of type ['timestamp']" in str(caught.value)


def test_a_timeline_with_a_timestamp_is_accepted():
    package = validate_package(_package_with("timeline", ["timestamp", "text"]))
    assert package["view"]["blocks"][0]["type"] == "timeline"


@pytest.mark.parametrize("block_type", BLOCK_TYPES)
def test_the_fixture_every_block_type_builds_is_one_the_gate_accepts(block_type):
    """The pictures and the assertions use packages the product would really store."""
    package, _ = package_for_block(block_type)
    assert parse_package(json.dumps(package)) == package


# --------------------------------------------------------------------------- #
# Theme: proved over the RENDERED document
# --------------------------------------------------------------------------- #


def test_a_package_token_is_declared_in_the_document_after_the_base_palette():
    package, values = package_for_block("stat")
    html = _render(package, values)
    root = re.search(r":root\{(.*?)\}", html, re.S)
    assert root is not None
    declared = root.group(1)
    assert "--accent:#0f766e;" in declared
    # ONE declaration of it, and it is the package's: a base value left in place
    # after the override would win or lose by source order rather than by rule.
    assert declared.count("--accent:") == 1


def test_the_two_themes_declare_different_palettes():
    package, values = package_for_block("stat")
    light = theme_tokens(package, "light")
    dark = theme_tokens(package, "dark")
    assert light["--bg"] != dark["--bg"]
    assert light["--text-strong"] != dark["--text-strong"]
    # Every token one theme declares, the other declares too: a token with a
    # value in one mode only is a rule that silently does nothing in the other.
    assert set(light) == set(dark)
    assert "--bg:#faf9f7;" in _render(package, values, "light")
    assert "--bg:#12141a;" in _render(package, values, "dark")


def test_an_unknown_theme_name_falls_back_rather_than_raising():
    package, values = package_for_block("stat")
    assert _render(package, values, "sepia") == _render(package, values, "light")


#: Token values that must never be written into the document. Each would, if
#: emitted verbatim, close the declaration it sits in or the element around it.
HOSTILE_TOKENS = {
    "--x": "red; } body{display:none",
    "--y": "red</style><script>fetch(1)</script>",
    "--z": "red;background:url(http://example.invalid)",
    "--w": "red\n}  html{opacity:0",
}


def test_a_hostile_theme_token_cannot_reach_the_document():
    """Built BY HAND, deliberately not through the gate.

    Going through ``validate_package`` would test the gate a second time and the
    renderer not at all -- and the renderer is what makes the rendered bytes
    safe, which is the property a second write path could otherwise take away.
    """
    package, values = package_for_block("stat")
    package = json.loads(json.dumps(package))
    package["theme"]["tokens"].update(HOSTILE_TOKENS)
    html = _render(package, values)
    for name, value in HOSTILE_TOKENS.items():
        assert value not in html
        assert f"{name}:" not in html
    # The document still has exactly one stylesheet, opened and closed once.
    assert html.count("<style>") == 1
    assert html.count("</style>") == 1


def test_a_well_formed_package_token_with_a_comma_is_kept():
    """The guard refuses statement punctuation, not legitimate CSS values."""
    package, values = package_for_block("stat")
    package = json.loads(json.dumps(package))
    package["theme"]["tokens"]["--display-font"] = '"Palatino", P052, Georgia, serif'
    assert '--display-font:"Palatino", P052, Georgia, serif;' in _render(package, values)


@pytest.mark.parametrize(
    "css",
    [
        "@import url(http://example.invalid/x.css);",
        "body{background:url(http://example.invalid/x.png)}",
        "body{color:red}</style><script>fetch('http://example.invalid')</script>",
        "a{background:expression(alert(1))}",
        "a{behavior:url(#default#time2)}",
    ],
)
def test_theme_css_that_fetches_or_escapes_is_dropped_from_the_document(css):
    package, values = package_for_block("stat")
    package = json.loads(json.dumps(package))
    package["theme"]["css"] = css
    html = _render(package, values)
    assert theme_css(package) == ""
    assert "example.invalid" not in html
    assert html.count("</style>") == 1


def test_plain_theme_css_is_kept():
    package, values = package_for_block("stat")
    package = json.loads(json.dumps(package))
    package["theme"]["css"] = ".pkg-figure{letter-spacing:-.02em}"
    assert ".pkg-figure{letter-spacing:-.02em}" in _render(package, values)


# --------------------------------------------------------------------------- #
# A value is never markup
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "value",
    [
        "<img src=x onerror=alert(1)>",
        "</script><script>alert(1)</script>",
        "</title><style>html{opacity:0}</style>",
        'a" onmouseover="alert(1)',
    ],
)
def test_a_value_cannot_become_markup(value):
    """The test is STRUCTURAL, because a safe rendering still shows the characters.

    ``&lt;img src=x onerror=alert(1)&gt;`` is the correct rendering of that
    value: escaped, inert, and still readable as what the log recorded. So
    "the value does not appear" is the wrong assertion -- it does, as text. What
    must not change is the document's SHAPE, so the tag sequence is compared
    against the same page rendered with a harmless value of no structural
    meaning. A new element or attribute shows up as a longer list.
    """
    package, values = package_for_block("list")
    name = next(n for n, s in package["model"]["types"].items() if s["type"] == "text")
    benign = tags_of(_render(package, {**values, name: "a plain line of text"}))
    hostile_html = _render(package, {**values, name: value})
    assert tags_of(hostile_html) == benign
    markup = markup_of(hostile_html)
    # The host's own <style> and <title> are in every document, so only a tag
    # the VALUE could have introduced is named here; the sequence check above is
    # what covers the general case.
    assert "<img" not in markup
    assert markup.count("<script") == markup.count("</script>")
    # The characters a tag is made of arrived escaped, which is why the shape
    # above is unchanged.
    if "<" in value:
        assert "&lt;" in markup


@pytest.mark.parametrize(
    "value",
    [
        "</script><script>alert(1)</script>",
        "</SCRIPT ><img src=x>",
        "\u2028alert(1)",
    ],
)
def test_a_value_cannot_end_the_data_island_it_travels_in(value):
    """The island carries RAW text, so the test is that it cannot be left.

    ``<`` is escaped to ``\\u003c`` -- still the same JSON, still the same value
    after ``JSON.parse`` -- so the HTML parser never sees a tag inside the
    island and everything after the value stays data rather than becoming
    markup.
    """
    package, values = package_for_block("list")
    name = next(n for n, s in package["model"]["types"].items() if s["type"] == "text")
    html = _render(package, {**values, name: value})
    raw = re.search(r'id="kirocrew-dashboard-data">(.*?)</script>', html, re.S).group(1)
    assert "<" not in raw
    assert "\u2028" not in raw
    # ...and it is still the value the read carried.
    assert json.loads(raw)["fields"][name] == value


def test_a_label_cannot_become_markup():
    package, values = package_for_block("table")
    package = json.loads(json.dumps(package))
    name = next(iter(package["model"]["types"]))
    package["model"]["types"][name]["label"] = "<b>bold</b>"
    html = _render(package, values)
    assert "<b>bold</b>" not in html
    assert "&lt;b&gt;bold&lt;/b&gt;" in html


# --------------------------------------------------------------------------- #
# Containment: no network, nothing fetched, the libraries inlined
# --------------------------------------------------------------------------- #


def test_the_document_policy_denies_everything_it_does_not_need():
    assert RENDER_CSP.startswith("default-src 'none'")
    assert "connect-src 'none'" in RENDER_CSP
    assert "unsafe-eval" not in RENDER_CSP
    assert "https://" not in RENDER_CSP
    assert "script-src 'unsafe-inline'" in RENDER_CSP


def test_no_element_in_the_document_fetches_anything():
    """Over the markup this module wrote, with the vendored sources set aside.

    three.js r159 opens with a deprecation notice that links to its own docs, so
    a scan over the whole document finds an ``https://`` that is a string inside
    a comment rather than anything the page requests. Scoping to
    :func:`markup_of` is what makes the assertion mean "nothing is fetched".
    """
    package, values = every_block_package()
    markup = markup_of(_render(package, values))
    assert "<script src" not in markup
    assert "<link" not in markup
    assert "http://" not in markup
    assert "https://" not in markup


def test_the_animation_library_is_inlined_in_every_document():
    package, values = package_for_block("stat")
    html = _render(package, values)
    assert "anime.js v3.2.2" in html
    assert "typeof anime !== 'function'" in html


def test_three_js_is_inlined_only_for_a_block_that_draws_in_3d():
    flat, flat_values = package_for_block("table")
    spatial, spatial_values = package_for_block("orbit")
    # The library\'s own copyright line is the marker, not the word THREE: the
    # page script names THREE in the branch that falls back when it is absent,
    # so that word is in every document by design.
    assert "Three.js Authors" not in _render(flat, flat_values)
    html = _render(spatial, spatial_values)
    assert "Three.js Authors" in html
    assert "new THREE.WebGLRenderer" in html


def test_a_page_without_the_3d_block_is_far_smaller_than_one_with_it():
    """The reason three.js is conditional, stated as a number."""
    flat, flat_values = package_for_block("table")
    spatial, spatial_values = package_for_block("orbit")
    small = len(_render(flat, flat_values).encode("utf-8"))
    large = len(_render(spatial, spatial_values).encode("utf-8"))
    assert small < 120_000
    assert large - small > 600_000


@pytest.mark.parametrize("name", sorted(VENDOR_FILES))
def test_a_vendored_library_is_the_version_named_and_carries_its_licence(name):
    source = vendor_source(name)
    assert VENDOR_VERSIONS[name].rsplit(".", 1)[0] in source or VENDOR_VERSIONS[name] in source
    lowered = source[:4000].lower()
    assert "license" in lowered or "licence" in lowered
    assert "mit" in lowered


def test_the_vendored_bytes_are_the_ones_the_readme_records():
    """Provenance is checkable, so an upgrade cannot arrive unannounced."""
    recorded = {
        "three": "7b1c5d75b28d9de15042e2b374f83566d8c7146697af8fdeb4558b0fb528a585",
        "anime": "b5ce1be3c3f530f192e0f2571d1942846096d66119cbada34bfdc912c4873f35",
    }
    for name, digest in recorded.items():
        raw = vendor_source(name).encode("utf-8")
        assert hashlib.sha256(raw).hexdigest() == digest


def test_an_inlined_library_cannot_end_the_script_element_it_sits_in():
    package, values = package_for_block("stat")
    html = _render(package, values)
    opens = html.count("<script")
    closes = html.count("</script>")
    assert opens == closes


# --------------------------------------------------------------------------- #
# The wire contract is one definition, not two
# --------------------------------------------------------------------------- #


def test_the_page_speaks_the_hosts_own_refill_contract():
    package, values = package_for_block("stat")
    html = _render(package, values)
    for constant in (BINDING_ATTRIBUTE, DATA_MESSAGE_TYPE, PAGE_EVENT, WINDOW_GLOBAL):
        assert json.dumps(constant) in html
    assert "event.source !== parent" in html


def test_the_refill_checks_the_sender_before_the_message_type():
    """A sibling frame can post here; the identity test is what stops it.

    Checked against EVERY type branch the handler has, not against one spelling
    of the type test: a second message type added below the identity check is
    fine, and one added above it is the bug this guards.
    """
    package, values = package_for_block("stat")
    html = _render(package, values)
    sender = html.index("event.source !== parent")
    for branch in ("data.type === MESSAGE", "data.type !== PATCH"):
        assert sender < html.index(branch), f"{branch} is tested before the sender"


# --------------------------------------------------------------------------- #
# Values: one formatter, and the shares computed server-side
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("kind", "value", "keys", "expected"),
    [
        ("number", 1842.5, {"unit": "cr", "precision": 1}, "1,842.5 cr"),
        ("number", 8, {}, "8"),
        ("number", None, {}, "\u2014"),
        ("number", "nonsense", {}, "\u2014"),
        ("text", "a line", {}, "a line"),
        ("text", "abcdefghij", {"max_len": 5}, "abcd\u2026"),
        ("timestamp", "2026-10-09T23:30:00Z", {}, "Oct 9, 23:30"),
        ("timestamp", "not a time", {}, "not a time"),
        ("enum", "waiting", {"choices": ["green", "waiting"]}, "waiting"),
        ("enum", "surprise", {"choices": ["green", "waiting"]}, "surprise"),
        ("bool", True, {}, "Yes"),
        ("bool", False, {}, "No"),
        ("bool", None, {}, "\u2014"),
    ],
)
def test_one_formatter_decides_what_a_reader_sees(kind, value, keys, expected):
    spec = {"type": kind, "source": dict(_FOLD_SOURCE), **keys}
    package = {"model": {"types": {"f": spec}}}
    assert display_values(package, {"f": value}) == {"f": expected}


def test_display_values_is_the_seam_a_refill_is_expected_to_use():
    """It covers exactly the fields the model declares, and no others."""
    package, values = package_for_block("table")
    shown = display_values(package, {**values, "not_in_the_model": 1})
    assert set(shown) == set(package["model"]["types"])


def test_the_document_carries_the_formatted_strings_for_a_refill():
    package, values = package_for_block("table")
    html = _render(package, values)
    assert island_of(html)["display"] == display_values(package, values)


def test_a_bar_is_a_share_of_the_largest_and_is_computed_before_javascript_runs():
    types = {
        "big": {"type": "number", "source": dict(_FOLD_SOURCE)},
        "half": {"type": "number", "source": dict(_FOLD_SOURCE)},
    }
    raw = {
        "kind": "dashboard",
        "bound_to": "crewmate:a",
        "model": {"types": types},
        "view": {"blocks": [{"id": "b", "type": "bars", "fields": ["big", "half"]}]},
        "theme": {"tokens": {}},
    }
    html = _render(validate_package(raw), {"big": 200, "half": 50})
    assert "width:100.00%" in html
    assert "width:25.00%" in html


def test_a_gauge_fills_the_ring_by_value_over_total():
    types = {
        "spent": {"type": "number", "source": dict(_FOLD_SOURCE)},
        "budget": {"type": "number", "source": dict(_FOLD_SOURCE)},
    }
    raw = {
        "kind": "dashboard",
        "bound_to": "crewmate:a",
        "model": {"types": types},
        "view": {"blocks": [{"id": "g", "type": "gauge", "fields": ["spent", "budget"]}]},
        "theme": {"tokens": {}},
    }
    html = _render(validate_package(raw), {"spent": 75, "budget": 300})
    circumference = float(re.search(r'data-pkg-circumference="([\d.]+)"', html).group(1))
    offset = float(re.search(r'stroke-dashoffset="([\d.]+)"', html).group(1))
    assert offset == pytest.approx(circumference * 0.75, abs=0.01)


def test_a_gauge_with_no_total_draws_an_empty_ring_rather_than_a_full_one():
    package, values = package_for_block("gauge")
    one = json.loads(json.dumps(package))
    one["view"]["blocks"][0]["fields"] = one["view"]["blocks"][0]["fields"][:1]
    html = _render(one, values)
    circumference = float(re.search(r'data-pkg-circumference="([\d.]+)"', html).group(1))
    offset = float(re.search(r'stroke-dashoffset="([\d.]+)"', html).group(1))
    assert offset == pytest.approx(circumference, abs=0.01)


def test_a_timeline_is_ordered_by_its_instants_not_by_the_authors_order():
    types = {
        "late": {"type": "timestamp", "label": "Late", "source": dict(_FOLD_SOURCE)},
        "early": {"type": "timestamp", "label": "Early", "source": dict(_FOLD_SOURCE)},
    }
    raw = {
        "kind": "dashboard",
        "bound_to": "crewmate:a",
        "model": {"types": types},
        "view": {"blocks": [{"id": "t", "type": "timeline", "fields": ["late", "early"]}]},
        "theme": {"tokens": {}},
    }
    html = _render(
        validate_package(raw),
        {"late": "2026-10-09T20:00:00Z", "early": "2026-10-01T08:00:00Z"},
    )
    assert html.index("Early") < html.index("Late")


def test_a_state_is_never_colour_alone():
    """Every enum and bool chip carries a glyph and the word, not just a hue."""
    package, values = package_for_block("pills")
    html = _render(package, values)
    for name, spec in package["model"]["types"].items():
        assert f'{BINDING_ATTRIBUTE}="{name}"' in html
    assert html.count('class="pkg-glyph"') == len(package["model"]["types"])
    assert 'data-pkg-state="warn"' in html


def test_an_integer_too_big_for_a_float_renders_as_absent_not_a_crash():
    """A fold may carry an int of any size; a float tops out near 1e308.

    `float(10**400)` raises OverflowError, which is not a ValueError, so the
    string arm's guard does not cover it. One oversized value must not take the
    whole block down with it -- it reads as a value the page does not have.
    """
    huge = 10**400
    # The conversion itself is the failure, so the guard belongs here.
    assert _as_number(huge) is None
    # And the number formatter answers with the absent marker rather than
    # raising, which is what keeps one bad value out of the whole block.
    assert _FORMATTERS["number"](huge, {"type": "number"}) == _EM_DASH


def test_the_numbers_in_the_3d_block_are_also_reachable_as_text():
    package, values = package_for_block("orbit")
    html = _render(package, values)
    flat = re.search(r'<ol class="pkg-orbit-flat">(.*?)</ol>', html, re.S)
    assert flat is not None
    for name in package["view"]["blocks"][0]["fields"]:
        assert f'{BINDING_ATTRIBUTE}="{name}"' in flat.group(1)


def test_the_3d_block_falls_back_to_text_when_webgl_is_absent():
    package, values = package_for_block("orbit")
    html = _render(package, values)
    assert "typeof THREE === 'undefined'" in html
    assert "data-pkg-view', 'flat'" in html


def test_motion_runs_once_per_change_and_honours_a_reduced_motion_preference():
    package, values = package_for_block("stat")
    html = _render(package, values)
    assert "prefers-reduced-motion: reduce" in html
    assert "prefers-reduced-motion:reduce" in html
    # No anime call asks for a loop: every animation here reports a change.
    assert "loop:" not in markup_of(html)


# --------------------------------------------------------------------------- #
# Degrading honestly
# --------------------------------------------------------------------------- #


def test_a_block_type_this_build_cannot_draw_says_so_by_name():
    """A package written by a newer build must not leave an unexplained hole."""
    package, _ = package_for_block("stat")
    block = {"id": "x", "type": "constellation", "fields": list(package["model"]["types"])}
    html = render_block(block, package, read_payload({}))
    assert "pkg-unrenderable" in html
    assert "constellation" in html


def test_a_field_the_read_has_no_value_for_is_marked_rather_than_blanked():
    package, _ = package_for_block("table")
    html = _render(package, {})
    assert "data-dashboard-missing" in html
    assert "\u2014" in html


def test_the_stale_band_is_in_the_document_and_hidden_until_the_read_says_stale():
    package, values = package_for_block("stat")
    html = render_dashboard(package, read_payload(values), theme="light")
    assert 'id="kirocrew-stale-band"' in html
    assert "hidden>" in html
    assert island_of(html)["stale"] is False
    stale = render_dashboard(
        package, read_payload(values, stale=True, missing=["a", "b"]), theme="light"
    )
    read = island_of(stale)
    assert read["stale"] is True
    assert read["missing"] == ["a", "b"]


# --------------------------------------------------------------------------- #
# The single-block path the controller's push needs
# --------------------------------------------------------------------------- #


def test_one_block_renders_on_its_own_as_the_section_the_page_holds():
    """``render_block`` is the per-block half: the same markup, nothing around it."""
    package, values = package_for_block("bars")
    block = package["view"]["blocks"][0]
    fragment = render_block(block, package, read_payload(values))
    assert fragment.startswith('<section class="pkg-block pkg-block--bars"')
    assert fragment.endswith("</section>")
    assert "<style" not in fragment and "<script" not in fragment
    # And it is the SAME markup the whole page carries for that block, so a
    # pushed block and a first-painted block cannot look different.
    assert fragment in _render(package, values)


def test_a_block_patch_carries_only_the_blocks_that_read_the_moved_fields():
    package, values = every_block_package()
    moved = [n for n, s in package["model"]["types"].items() if s["type"] == "number"][:1]
    patch = block_patch(package, {moved[0]: 99}, seq=7)
    assert patch["type"] == BLOCK_PATCH_MESSAGE_TYPE
    assert patch["seq"] == 7
    assert set(patch["blocks"]) == set(blocks_reading(package, moved))
    assert patch["blocks"]
    expected = display_values(package, {moved[0]: 99})[moved[0]]
    assert expected == "99.0 cr", "the field's own unit and precision apply"
    for block_id, body in patch["blocks"].items():
        assert set(body["fields"]) == set(moved)
        assert body["display"][moved[0]] == expected


def test_a_block_patch_gives_a_block_only_the_fields_that_block_renders():
    """A patch must not hand a value to a block the view never put it on."""
    package, values = every_block_package()
    patch = block_patch(package, values)
    by_id = {str(b["id"]): set(b["fields"]) for b in package["view"]["blocks"]}
    for block_id, body in patch["blocks"].items():
        assert set(body["fields"]) <= by_id[block_id]


def test_a_block_patch_ignores_a_field_the_model_does_not_declare():
    package, values = package_for_block("table")
    patch = block_patch(package, {"not_in_the_model": 1})
    assert patch["blocks"] == {}


def test_a_block_patch_formats_with_the_same_one_formatter_as_the_first_render():
    package, values = package_for_block("table")
    patch = block_patch(package, values)
    shown = display_values(package, values)
    for body in patch["blocks"].values():
        for name, text in body["display"].items():
            assert text == shown[name]


def test_a_block_patch_carries_no_markup():
    """The reason this boundary cannot introduce an element."""
    package, values = every_block_package()
    blob = json.dumps(block_patch(package, values))
    assert "<" not in blob
    assert "section" not in blob


def test_the_page_paints_only_the_blocks_a_patch_names():
    package, values = package_for_block("bars")
    html = _render(package, values)
    assert "for (var t = 0; t < touched.length; t++) fill(touched[t]);" in html
    assert "function fill(root)" in html


def test_the_3d_scene_is_built_once_however_often_the_painter_runs():
    """A patched block re-runs orbits(); a second canvas would be two scenes."""
    package, values = package_for_block("orbit")
    html = _render(package, values)
    assert "var existing = box.querySelector('.pkg-orbit-stage canvas');" in html
    assert "if (existing) return;" in html
    # ...and the painter really does reach the 3D block, or a patched one would
    # render an empty stage.
    assert "orbits(scope);" in html


def test_every_catalog_row_has_a_renderer_and_accepts_something():
    """What the deleted `block_catalog_summary` asserted, read off the catalogs direct.

    `test_nothing_either_catalog_admits_is_unrenderable` already covers the renderer
    half through `unrenderable_types()`. This keeps the other half -- that no block
    type admits an empty set of data types, which would be a block nothing can ever
    be placed on.
    """
    catalog = view_block_catalog()
    assert sorted(catalog) == sorted(BLOCK_TYPES)
    assert all(entry.accepts for entry in catalog.values())
