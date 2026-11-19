"""An organisation's ``ask`` rule on web fetch or web search reaches a person.

In an LTS build KAS enforces the policy-language terms ``web_fetch`` and
``web_search`` itself. A rule whose effect is ``ask`` makes KAS send Crew a
``session/request_permission``, and Crew must not answer it on its own: session
trust, YOLO, a scoped grant or any other auto-approve would let a user skip the
organisation's ask by turning trust on. Two sources say a call is asked about, and either is
enough: KAS's own consent block on the request (an ``ask`` rule the
organisation set), and the policy fetch's record of which of the two terms an
``ask`` rule names, in the ``unattended_web_tools`` scope
(``enterprise_policy_translate.web_ask_ruleset``). :func:`needs_person` reads
both, and every auto-approve that can answer such a request asks it first.

Only KAS requests are judged: the web terms are KAS-only (RFC D8), so another
harness keeps today's behaviour. A KAS request is identified by the tool id the
engine stamps on it (``_meta.kiro.toolId``), never by its title. A KAS request
with no tool id cannot be told apart from a web call, so while an ``ask`` rule
is in force it reaches a person too. An ``oss`` build, or an LTS build whose
policy has no such rule, is unchanged.
"""

from __future__ import annotations

from typing import Any, Mapping

from kiro_crew import edition

#: The scope the policy fetch writes: which web terms may be approved with no
#: person asked. A term an ``ask`` rule names is denied in it.
SCOPE = "unattended_web_tools"

#: The approval card's meta field marking a card :func:`needs_person` raised, so
#: a trust grant's sweep of pending cards leaves it open.
CARD_META_KEY = "web_ask_rule"

#: The policy-language web terms, in the order a record lists them.
TERMS: tuple[str, ...] = ("web_fetch", "web_search")

#: KAS's tool id for each web term (``test/fixtures/kas_builtin_tool_ids.json``):
#: web search is served by KAS's remote tool wrapper, which prefixes its id.
_TERM_BY_KAS_TOOL_ID: Mapping[str, str] = {
    "web_fetch": "web_fetch",
    "remote_web_search": "web_search",
}


def asked_terms() -> frozenset[str]:
    """The web terms an installed ``ask`` rule names; empty outside an LTS build."""
    if not edition.is_lts():
        return frozenset()
    from kiro_crew.edition_locks import policy_permits

    return frozenset(term for term in TERMS if not policy_permits(SCOPE, term))


def needs_person(event: Any) -> str:
    """The web term a person must approve *event* under, or ``""``.

    A KAS web call reaches a person when either source says ``ask``: KAS's own
    consent block (``event.kas_managed_ask``) or the installed policy's record
    (:func:`asked_terms`). ``"unidentified"`` when *event* is a KAS request with no
    tool id while the policy asks about a web term: it may be a web call, so it is
    not auto-approved.
    """
    if not edition.is_lts() or not getattr(event, "kas_builtin_ids", False):
        return ""
    tool_id = getattr(event, "harness_tool_id", "") or ""
    if not isinstance(tool_id, str):
        tool_id = ""
    term = _TERM_BY_KAS_TOOL_ID.get(tool_id, "")
    if term and getattr(event, "kas_managed_ask", False) is True:
        return term
    if not term and tool_id:
        return ""
    asked = asked_terms()
    if not asked:
        return ""
    if not tool_id:
        return "unidentified"
    return term if term in asked else ""


def held_phrases(event: Any) -> list[str]:
    """*event*'s :func:`needs_person` answer as a held limit, for the surfaces that
    already route a held Bot limit (``mate_held_rules.held_for``) to a person: no
    auto-approve answers it, the approver is asked as a person's (``held_call``),
    and a surface with no approver refuses it. ``[]`` when nobody must approve it.
    """
    term = needs_person(event)
    if not term:
        return []
    if term == "unidentified":
        return [_UNIDENTIFIED_PHRASE]
    return [f"the organization's policy asks before {_LABELS[term]}"]


def unattended_refusal(event: Any) -> str:
    """The host's reason for refusing *event* on a surface with nobody to ask, or ``""``
    when :func:`needs_person` names no web term for it."""
    phrases = held_phrases(event)
    if not phrases:
        return ""
    return "; ".join(phrases) + ", so a person must approve it, and this surface has no approver"


_LABELS: Mapping[str, str] = {"web_fetch": "web fetch", "web_search": "web search"}
_UNIDENTIFIED_PHRASE = (
    "the organization's policy asks before web fetch or web search, and this tool "
    "could not be identified"
)


def card_notice(term: str) -> str:
    """The line the approval card shows for a request :func:`needs_person` named."""
    if term == "unidentified":
        return (
            "Auto-approve not applied: your organization's policy asks before web "
            "fetch or web search, and this tool could not be identified."
        )
    label = "web fetch" if term == "web_fetch" else "web search"
    return f"Auto-approve not applied: your organization's policy asks before {label}."
