"""Pin the numbers and tool names in ``docs/connections.md`` to their sources.

The shipped Connections page states counts a reader will act on — how many
providers the catalogue holds, how many render a card, which providers get tool
renames, and the consecutive-failure threshold before a server is quarantined.
Every one of those moves when someone edits ``registry.json`` or a config
default, and a stale number in a user-facing doc is worse than no number: it
reads as authoritative.

These tests READ THE DOC and compare what it says against the live source. That
direction matters: a test that only compared ``registry.json`` to itself would
pass while the prose said anything at all.

Every provider-name check is **anchored to the one clause that makes the claim**,
never to the whole document. A mutation probe proved why: GitLab is named in four
places, so a whole-document substring check stayed green when GitLab was deleted
from the list of providers that ship a Connect button. If you reword one of the
anchored clauses, these tests fail with the anchor they could not find — update
the anchor here in the same change.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.connections import registry as registry_mod
from kiro_crew.connections.tool_aliases import derived_alias

_REPO = Path(__file__).resolve().parents[1]
_DOC = _REPO / "src/kiro_crew/docs/connections.md"
_REGISTRY = _REPO / "src/kiro_crew/connections/registry.json"

#: The doc writes every provider list as ``<Word> entries: A, B, C``, one per row
#: of the visibility table. Anchoring on the row's first cell is what keeps a
#: name deleted from one row from being satisfied by the same name in another.
_ROW_ANCHORS = {
    "gate_passed": "| Launch gate passed |",
    "preregistered": "| Needs an operator-registered OAuth app |",
    "vendor_blocked": "| Vendor admits clients by allowlist or waitlist |",
}


@pytest.fixture(scope="module")
def doc() -> str:
    return _DOC.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def flat(doc: str) -> str:
    """The doc with newlines flattened, so an anchor may span a wrapped line."""
    return re.sub(r"\s+", " ", doc)


@pytest.fixture(scope="module")
def entries() -> list[dict]:
    return json.loads(_REGISTRY.read_text(encoding="utf-8"))


def _bold_ints(text: str) -> set[int]:
    """Every integer the doc emphasises, which is how it writes a count."""
    return {int(m) for m in re.findall(r"\*\*(\d+)\*\*", text)}


def _split_names(listing: str) -> set[str]:
    """``"Notion, Linear and GitLab"`` -> ``{"Notion", "Linear", "GitLab"}``."""
    parts = re.split(r",| and ", listing)
    return {p.strip(" .|") for p in parts if p.strip(" .|")}


def _names_listed_in_row(doc: str, key: str) -> set[str]:
    """The provider names one visibility-table row enumerates, and only that row."""
    anchor = _ROW_ANCHORS[key]
    rows = [ln for ln in doc.splitlines() if ln.startswith(anchor)]
    assert len(rows) == 1, (
        f"connections.md: expected exactly one table row starting {anchor!r}, found "
        f"{len(rows)}. The visibility table was reworded; update _ROW_ANCHORS."
    )
    match = re.search(r"\b\w+ entries: ([^|]+)", rows[0])
    assert match, (
        f"connections.md: the {anchor!r} row no longer ends in "
        "'<Word> entries: A, B, C'. Update this anchor with the rewording."
    )
    return _split_names(match.group(1))


class TestCatalogueCounts:
    def test_total_provider_count_is_stated(self, doc: str, entries: list[dict]) -> None:
        total = len(entries)
        assert total in _bold_ints(doc), (
            f"connections.md does not state the catalogue size ({total}) as a bold count; "
            "update the prose when registry.json gains or loses an entry"
        )

    def test_visible_card_count_is_stated(self, doc: str) -> None:
        visible = len(registry_mod.get_visible_providers())
        assert visible in _bold_ints(doc), (
            f"connections.md does not state how many providers render a card ({visible}); "
            "get_visible_providers() moved and the prose did not"
        )

    def test_launch_gate_passed_row_lists_exactly_those_providers(
        self, doc: str, entries: list[dict]
    ) -> None:
        expected = {e["name"] for e in entries if e["launch_gate_passed"]}
        assert expected, "no launch-gate-passed provider: this test's premise is gone"
        assert _names_listed_in_row(doc, "gate_passed") == expected, (
            "connections.md's 'Launch gate passed' row does not list exactly the entries "
            "whose launch_gate_passed is set — those are the ones that render Connect"
        )

    def test_vendor_blocked_row_lists_exactly_those_providers(
        self, doc: str, entries: list[dict]
    ) -> None:
        expected = {e["name"] for e in entries if e["vendor_approval_pending"]}
        assert expected, "no vendor-blocked provider: this test's premise is gone"
        assert _names_listed_in_row(doc, "vendor_blocked") == expected, (
            "connections.md's vendor-blocked row does not list exactly the entries whose "
            "vendor_approval_pending is set — those are the ones hidden as a dead end"
        )

    def test_preregistered_row_lists_exactly_those_providers(
        self, doc: str, entries: list[dict]
    ) -> None:
        expected = {
            e["name"] for e in entries if (e.get("auth") or {}).get("mode") == "preregistered"
        }
        assert expected, "no pre-registered provider: this test's premise is gone"
        assert _names_listed_in_row(doc, "preregistered") == expected, (
            "connections.md's pre-registered row does not list exactly the entries carrying "
            "auth.mode 'preregistered' — those are the ones needing an operator's OAuth app"
        )

    def test_category_bucket_count_matches_the_stated_word(
        self, doc: str, entries: list[dict]
    ) -> None:
        buckets = {e["category"] for e in entries}
        assert "eight buckets" in doc.lower(), (
            "connections.md no longer says 'eight buckets'; update this test's anchor "
            f"together with the prose (registry.json has {len(buckets)} categories)"
        )
        assert len(buckets) == 8, (
            f"registry.json now has {len(buckets)} category buckets, not 8; "
            "connections.md says 'eight buckets' and must be updated"
        )


class TestToolAliases:
    #: The one clause naming which providers declare renames. Anchored because
    #: each of these names also appears in the collision example beside it.
    _CLAUSE = re.compile(
        r"declared in the catalogue for (?P<count>\w+) providers today — (?P<names>[^—]+) —"
    )

    def test_clause_lists_exactly_the_alias_declaring_providers(
        self, flat: str, entries: list[dict]
    ) -> None:
        expected = {e["name"] for e in entries if e.get("tool_aliases")}
        assert expected, "no provider declares tool_aliases: this test's premise is gone"
        match = self._CLAUSE.search(flat)
        assert match, (
            "connections.md no longer carries the clause 'declared in the catalogue for "
            "<N> providers today — A, B and C —'; update this test's anchor with the "
            "rewording so the list stays pinned"
        )
        assert _split_names(match.group("names")) == expected, (
            "connections.md names the wrong providers as declaring tool renames; "
            f"registry.json declares tool_aliases for {sorted(expected)}"
        )

    def test_clause_count_word_matches(self, flat: str, entries: list[dict]) -> None:
        declaring = [e for e in entries if e.get("tool_aliases")]
        words = {1: "one", 2: "two", 3: "three", 4: "four", 5: "five", 6: "six"}
        match = self._CLAUSE.search(flat)
        assert match, "the alias clause anchor is gone; see the sibling test's message"
        assert match.group("count").lower() == words.get(len(declaring)), (
            f"{len(declaring)} providers declare tool_aliases but connections.md says "
            f"'{match.group('count')} providers today'"
        )

    def test_every_alias_the_doc_shows_is_the_derived_spelling(
        self, doc: str, entries: list[dict]
    ) -> None:
        """A rename printed in the doc must be one the code would actually emit."""
        declared = {alias for e in entries for alias in (e.get("tool_aliases") or {}).values()}
        shown = {m for m in re.findall(r"\b[a-z0-9]+_[a-z0-9_]+\b", doc) if m in declared}
        assert shown, (
            "connections.md shows no declared tool alias; the rename section is "
            "supposed to print real examples"
        )
        for e in entries:
            for tool, alias in (e.get("tool_aliases") or {}).items():
                if alias in shown:
                    assert alias == derived_alias(
                        e["slug"], tool
                    ), f"{alias} is not derived_alias({e['slug']!r}, {tool!r})"


class TestQuarantineThreshold:
    def test_default_threshold_is_stated(self, doc: str) -> None:
        default = KiroCrewConfig.load().agent.mcp_quarantine_after_failures
        assert default in _bold_ints(doc), (
            f"connections.md does not state the quarantine threshold ({default}) as a bold "
            "count; agent.mcp_quarantine_after_failures moved and the prose did not"
        )

    def test_the_config_key_is_named(self, doc: str) -> None:
        assert (
            "agent.mcp_quarantine_after_failures" in doc
        ), "connections.md must name the config key so a reader can change the threshold"


class TestCrossReferences:
    @pytest.mark.parametrize(
        "target",
        ["secrets-vault.md", "mcp-apps.md", "blocked-commands.md"],
    )
    def test_required_sibling_link_resolves(self, doc: str, target: str) -> None:
        assert f"]({target})" in doc, f"connections.md does not link {target}"
        assert (_DOC.parent / target).is_file(), f"{target} is not in the shipped docs tree"

    def test_the_doc_is_reachable_from_both_indexes(self) -> None:
        for index in ("index.md", "README.md"):
            text = (_DOC.parent / index).read_text(encoding="utf-8")
            assert "](connections.md)" in text, f"connections.md is not linked from {index}"

    def test_the_doc_is_in_the_tips_allowlist(self) -> None:
        from kiro_crew.tips_allowlist import TIP_DOC_ALLOWLIST

        assert "connections.md" in TIP_DOC_ALLOWLIST
