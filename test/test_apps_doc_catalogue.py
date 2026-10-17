"""The packaged Apps page's shipped-app catalogue is pinned to ``apps/builtins/``.

``src/kiro_crew/docs/apps.md`` carries a user-facing table of every app that ships
inside the wheel. That kind of list has one failure mode and it is silent: an app is
added, removed or renamed, and the catalogue keeps reading as complete while it no
longer is. ``scripts/docs_lint.py`` cannot catch it — the page's links and symbols all
still resolve — so the drift only surfaces when a reader looks for an app the page
never mentions, or enables one the page names and the wheel does not ship.

This module is that ratchet. Every claim it asserts is read from the manifests
(``app.json``) rather than restated here, so the test never becomes a second list to
keep in sync: adding a builtin fails the test until the page gains a row, and its
resource contributions must reach the agents, skills, MCP and scheduled-jobs bullets.

The slug is what is matched in catalogue rows, not the display name, because the slug
is the identity the CLI takes (``kirocrew app enable md-notebook``). The resource
bullets name apps by their manifest display names instead.

Both directions are asserted for every claim: the manifests decide what the truth is,
and the page's own text has to carry it. A test that only compared manifests against
each other would stay green while the sentence stating the fact was deleted, which is
the drift this module exists to stop.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import cast

import pytest

REPO = Path(__file__).resolve().parent.parent
DOCS = REPO / "src" / "kiro_crew" / "docs"
DOC = DOCS / "apps.md"
INDEX = DOCS / "index.md"
BUILTINS = REPO / "src" / "kiro_crew" / "apps" / "builtins"

#: A catalogue cell names its app as ``**Display Name** (`slug`)``. Anchored on the
#: bolded-name-then-backticked-slug pair so a slug mentioned in running prose or in a
#: shell example is not mistaken for a catalogue row.
_ROW_RE = re.compile(r"\*\*(?P<display>[^*]+)\*\* \(`(?P<slug>[a-z0-9-]+)`\)")

#: How the page spells a count in its lead paragraph. Only the range the wheel could
#: plausibly reach is listed; a count outside it fails loudly rather than silently
#: passing an unspelled number.
_COUNT_WORDS = {
    20: "Twenty",
    21: "Twenty-one",
    22: "Twenty-two",
    23: "Twenty-three",
    24: "Twenty-four",
    25: "Twenty-five",
    26: "Twenty-six",
    27: "Twenty-seven",
    28: "Twenty-eight",
    29: "Twenty-nine",
    30: "Thirty",
}

#: Agent counts are prose, too; this is vocabulary, not a second per-app catalogue.
_AGENT_COUNT_WORDS = {
    1: "one",
    2: "two",
    3: "three",
    4: "four",
    5: "five",
    6: "six",
    7: "seven",
    8: "eight",
    9: "nine",
    10: "ten",
}


def _manifests() -> dict[str, dict[str, object]]:
    """Every shipped app's manifest, keyed by its declared name."""
    out: dict[str, dict[str, object]] = {}
    for path in sorted(BUILTINS.glob("*/app.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        out[str(data["name"])] = data
    return out


def _requires_desktop_app(manifest: dict[str, object]) -> bool:
    platform = manifest.get("platform")
    return isinstance(platform, dict) and platform.get("requiresDesktopApp") is True


def _resource_claim(doc_text: str, label: str) -> str:
    """The first sentence of one resource bullet, with wrapped lines joined."""
    section = doc_text.partition("## Agents, skills and MCP tools an app brings\n")[2]
    section = section.split("\n## ", 1)[0]
    bullet = re.search(rf"^- \*\*{re.escape(label)}\.\*\* (.*(?:\n  .*)*)", section, re.M)
    return " ".join(bullet.group(1).split()).split(". ", 1)[0] if bullet else ""


@pytest.fixture(scope="module")
def doc_text() -> str:
    return DOC.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def index_text() -> str:
    return INDEX.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def catalogue_section(doc_text: str) -> str:
    """The ``## What ships`` section only — the part that claims to be exhaustive."""
    assert "## What ships" in doc_text, "the apps page lost its ## What ships section"
    body = doc_text.split("## What ships", 1)[1]
    return body.split("\n## ", 1)[0]


@pytest.fixture(scope="module")
def catalogued_rows(catalogue_section: str) -> dict[str, str]:
    """slug -> display name, as the catalogue section states them."""
    return {m.group("slug"): m.group("display") for m in _ROW_RE.finditer(catalogue_section)}


def test_every_shipped_app_has_a_catalogue_row(catalogued_rows: dict[str, str]) -> None:
    """A new builtin fails here until the page gains a line for it."""
    missing = sorted(set(_manifests()) - set(catalogued_rows))
    assert not missing, (
        f"apps.md's catalogue does not mention shipped app(s): {missing}. "
        "Add a row under the group it belongs to, with the app's own manifest description."
    )


def test_the_catalogue_names_no_app_that_is_not_shipped(catalogued_rows: dict[str, str]) -> None:
    """A removed or renamed builtin fails here until the page drops its row."""
    phantom = sorted(set(catalogued_rows) - set(_manifests()))
    assert not phantom, (
        f"apps.md's catalogue names app(s) this wheel does not ship: {phantom}. "
        "Remove the row, or fix the slug to the name in the app's app.json."
    )


def test_each_row_uses_the_manifest_display_name(catalogued_rows: dict[str, str]) -> None:
    """The bolded name is the one the store shows, so a rename must reach the page."""
    manifests = _manifests()
    wrong = {
        slug: (shown, manifests[slug].get("displayName"))
        for slug, shown in catalogued_rows.items()
        if slug in manifests and shown != manifests[slug].get("displayName")
    }
    assert not wrong, f"display name in apps.md differs from the manifest: {wrong}"


def test_both_pages_state_the_shipped_count(doc_text: str, index_text: str) -> None:
    """The count is spelled in two places; neither may be left behind.

    ``index.md`` carries its own wording, so asserting only ``apps.md`` would let a
    maintainer fix the failing file and ship the other one telling users a wrong number.
    """
    total = len(_manifests())
    expected = _COUNT_WORDS.get(total)
    assert expected, (
        f"{total} apps ship and this test has no spelling for that; extend `_COUNT_WORDS` "
        "and update both pages."
    )
    assert (
        f"{expected} apps ship inside the Kiro Crew package" in doc_text
    ), f"apps.md's lead paragraph must say {expected!r} — {total} apps ship today."
    assert (
        f"{expected} apps ship in the package" in index_text
    ), f"index.md's Apps row must say {expected!r} — {total} apps ship today."


def test_the_page_names_the_default_on_apps(doc_text: str) -> None:
    """``defaultEnabled`` is the one claim a reader acts on before enabling anything."""
    on = sorted(
        str(m["displayName"]) for m in _manifests().values() if m.get("defaultEnabled") is True
    )
    assert on == ["Command Bar", "Task Runner"], (
        f"the set of default-enabled shipped apps changed to {on}; update the "
        "'on from a fresh install' sentence in apps.md and this expectation"
    )
    sentence = [ln for ln in doc_text.splitlines() if "on from a fresh install" in ln]
    assert sentence, "apps.md no longer states which shipped apps start enabled"
    for name in on:
        assert (
            f"**{name}**" in sentence[0]
        ), f"apps.md's default-on sentence does not name {name!r}: {sentence[0]!r}"


def test_the_page_names_the_hidden_apps(doc_text: str) -> None:
    """A hidden builtin is unreachable from the store while disabled, so the page says so."""
    hidden = sorted(str(m["displayName"]) for m in _manifests().values() if m.get("hidden") is True)
    assert hidden == ["Channels", "Workflows"], (
        f"the set of hidden shipped apps changed to {hidden}; update the "
        "'hidden from Discover and Library' sentence in apps.md and this expectation"
    )
    paragraph = doc_text.split("hidden from Discover and Library")
    assert len(paragraph) == 2, "apps.md no longer explains that some shipped apps are hidden"
    for name in hidden:
        assert (
            name in paragraph[0][-400:] or name in paragraph[1][:200]
        ), f"apps.md's hidden-app sentence does not name {name!r}"


def test_the_page_names_the_apps_that_need_the_desktop_app(doc_text: str) -> None:
    """``requiresDesktopApp`` decides whether enabling in a browser does anything visible."""
    desktop = sorted(
        str(m["displayName"]) for m in _manifests().values() if _requires_desktop_app(m)
    )
    assert desktop == ["Crew Companion", "Mochi"], (
        f"the set of desktop-app-only shipped apps changed to {desktop}; update the "
        "desktop-app sentence in apps.md and this expectation"
    )
    sentence = [ln for ln in doc_text.splitlines() if "need the Kiro Crew **desktop app**" in ln]
    assert sentence, "apps.md no longer states which shipped apps need the desktop app"
    for name in desktop:
        assert (
            name in sentence[0]
        ), f"apps.md's desktop-app sentence does not name {name!r}: {sentence[0]!r}"


def test_the_agents_bullet_names_every_app_and_its_count(doc_text: str) -> None:
    """Changing an app's agent roster must reach its spelled count in the page."""
    claim = re.sub(r" \([^)]*\)", "", _resource_claim(doc_text, "Agents"))
    # Split on the separator FIRST, then parse each piece. Matching name-and-count
    # straight out of the running sentence made ``, and `` / `` and `` read as part of
    # the app's name, so writing the list the way the Skills bullet below already
    # writes it failed a test whose facts were all correct.
    stated = {}
    for piece in re.split(r", and | and |, ", claim):
        named = re.fullmatch(r"(.+?) (?:ships )?([a-z-]+)", piece.strip())
        if named:
            stated[named.group(1)] = named.group(2)
    expected = {
        str(m["displayName"]): _AGENT_COUNT_WORDS.get(len(cast(list[object], m["agents"])))
        for m in _manifests().values()
        if m.get("agents")
    }
    assert stated == expected, (
        "Update the Agents bullet under 'Agents, skills and MCP tools an app brings' "
        "in src/kiro_crew/docs/apps.md to match each app.json agents roster "
        f"(extend _AGENT_COUNT_WORDS if needed): stated={stated}, expected={expected}"
    )


@pytest.mark.parametrize(
    ("label", "field", "end"),
    [
        ("Skills", "skills", " each add"),
        ("MCP tools", "mcpServers", " each run"),
        ("Scheduled jobs", "crons", " declares crons"),
    ],
    ids=["skills", "mcp", "crons"],
)
def test_resource_bullet_names_exactly_the_contributing_apps(
    doc_text: str, label: str, field: str, end: str
) -> None:
    """Adding or removing an app's resource must reach the bullet that names its owners."""
    names = _resource_claim(doc_text, label).split(end, 1)[0]
    stated = set(re.split(r", | and ", names))
    expected = {str(m["displayName"]) for m in _manifests().values() if m.get(field)}
    assert stated == expected, (
        f"Update the {label} bullet under 'Agents, skills and MCP tools an app brings' "
        f"in src/kiro_crew/docs/apps.md to name exactly the apps with non-empty {field} "
        f"in app.json: stated={sorted(stated)}, expected={sorted(expected)}"
    )
