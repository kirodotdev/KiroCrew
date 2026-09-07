"""Pure transcript-search parsing and the composed session catalog projection.

The query helpers in this module have no storage side effects.  The projection
is deliberately composed with a ConversationLog facade instead of inheriting
from it: calls back through the facade preserve the narrow monkeypatch seams
used by the history race and cost tests while keeping one owner for each cache,
lock, and path.
"""

from __future__ import annotations

import itertools
import json
import logging
import math
import re
import time as _time
from collections.abc import Callable, Iterable, Iterator, Sequence
from datetime import datetime
from typing import TYPE_CHECKING, NamedTuple

from kiro_crew._sqlite_compat import is_cjk_char, script_runs
from kiro_crew.jsonl_util import bounded_records

if TYPE_CHECKING:
    from kiro_crew.history import ConversationLog


SEARCH_MIN_CHARS = 2  # shortest query string that triggers backend search
SEARCH_MAX_TOKENS = 12  # distinct REQUIRED needles per query — see parse_search_query
_SEARCH_MAX_SCORING_EXTRAS = 12  # distinct scoring-only needles (CJK bigrams) per query
_TITLE_BOOST = 10  # field-boost multiplier for title matches in search_sessions
_PHRASE_BOOST = 4  # extra weight per exact whole-query hit in a multi-word search
_SEARCH_SCAN_WINDOW = 500  # cap files scanned per search to bound I/O
# Recency boost bounds for search_sessions: a session modified now scores
# ×(1 + _RECENCY_MAX_BOOST); the extra weight halves every
# _RECENCY_HALF_WEIGHT_DAYS of age and decays toward ×1.0 — never a penalty
# that could bury an old exact match, only a bounded tiebreaker toward
# recent work. 1.5 is sized to the canonical complaint: a year-old session
# matching TWICE must lose to today's session matching once (2 hits × ~1.11
# aged boost ≈ 2.2 < 1 hit × 2.5 fresh ceiling), while a decisively better
# old match (3+ hits) still wins — a 1.0 ceiling left the stated 2:1 case
# unfixed.
_RECENCY_MAX_BOOST = 1.5
_RECENCY_HALF_WEIGHT_DAYS = 30.0
# Weight of a single CJK character needle that came from a longer run. Individual
# han/kana/hangul characters are extremely common (a function character like "的"
# can appear hundreds of times in one transcript), so counting them at full weight
# would let character noise drown out the bigram adjacency signal that actually
# ranks CJK results. They still gate the AND match at full strength — the weight
# only dampens their contribution to the relevance score.
_CJK_CHAR_WEIGHT = 0.25
# Weight of one forge-reference spelling hit contributed for RANKING a bare
# number query ("4411"). Such a query keeps its plain substring needle, so
# recall is untouched — the spellings only move the session that actually
# references pull request 4411 above one that happens to contain those digits
# inside a run id. Sized like _PHRASE_BOOST: strong enough that a single real
# reference outranks incidental digit noise, not so strong that a session
# repeating the digits many times can never win.
_FORGE_REF_WEIGHT = 4.0
# Forge expansions per query. Each one costs one substring scan of every scanned
# session PER SPELLING it carries — up to eight for a reference the query named,
# and up to thirteen for a bare number's ranking needle, which carries both
# families. Three expansions therefore top out around 39 substring scans per
# field over the scan window, each a single C-level ``str.count`` against
# already-folded, memoized text — which is why the cap stays at three rather
# than shrinking as the spelling sets grew: a query naming three references
# ("compare #1 #2 #3") is legitimate, and the scans it costs are not the
# expensive part of a search. A fourth forge-shaped token degrades to a plain
# needle.
_SEARCH_MAX_FORGE_REFS = 3
# ALTERNATIVE spellings ONE resolver answer may contribute for a single token.
# The collector returns the FIRST plugin to recognize the token, for every token
# shape, so exactly one provider's answer ever arrives here — this bounds that
# one answer, not a fan-in across providers. The unit of cost is the same as
# above: one ``str.count`` per spelling per scanned session per field, so an
# unbounded contribution would let a plugin multiply every search by a number
# nothing in core chose. Sized to the sibling per-plugin ceiling
# (_MAX_PLUGIN_PATH_MARKERS, also 8), which bounds the same kind of thing: what
# one plugin may hand core for one lookup. Lives here rather than beside the
# collector because the cost it bounds is this module's.
_MAX_SEARCH_REF_SPELLINGS = 8


def _is_cjk_char(ch: str) -> bool:
    """True for characters that written Chinese/Japanese/Korean does not space-separate.

    Whitespace tokenization silently degrades for these scripts — a whole CJK
    clause arrives as ONE "token" — so :func:`parse_search_query` segments runs
    of them instead. The ranges cover the Han ideograph blocks (unified,
    extension A, the astral extensions, compatibility) and the kana blocks:
    Chinese and Japanese are written without spaces, which is the named defect.
    Deliberately NOT Hangul — modern Korean IS space-separated, so its words
    already arrive as ordinary whitespace tokens and the substring rule serves
    them; segmenting them would change Korean ranking to fix a failure nobody
    has reported. Likewise not full-width forms, punctuation, or symbols.

    The ranges themselves live in :mod:`kiro_crew._sqlite_compat`, which the
    knowledge FTS dialect also segments by, so the set has ONE owner: two
    hand-maintained copies of it drifted apart silently, and this module and that
    one must agree on what "a spaceless script" means or the same query segments
    differently in session search and knowledge search.
    """
    return is_cjk_char(ch)


class SearchNeedle(NamedTuple):
    """One substring a session search scans for.

    ``required=True`` needles form the AND gate — a session missing one (in
    both title and content) is disqualified. ``required=False`` needles only
    contribute to the relevance score. ``weight`` scales each occurrence's
    contribution to that score.

    ``alts`` are ALTERNATIVE SPELLINGS of the same reference: the needle is
    satisfied by ``text`` or by any alt, and occurrences of every spelling
    count toward the score. That is a bounded OR *inside* one needle, not an OR
    over the query — it exists because one forge reference has several written
    forms (``#4411`` and ``…/pull/4411`` name the same pull request), and a
    transcript may carry any of them.

    ``digit_bounded`` rejects a match that sits inside a LONGER number, on
    either side — so ``4411`` matches ``PR 4411.`` but not ``#44110`` and not
    the run id ``1544110293``. Only meaningful for a spelling made of digits or
    ending in one.

    ``adjacency`` marks a needle as ADJACENCY EVIDENCE (a CJK bigram) — the only
    kind the adjacency floor in :meth:`ConversationLog.search_sessions` counts.
    Scoring-only needles that are not adjacency evidence (the forge spellings
    added for ranking a bare number) therefore cannot arm that floor, which
    would otherwise turn a ranking hint into a hidden gate.
    """

    text: str
    weight: float
    required: bool
    alts: tuple[str, ...] = ()
    digit_bounded: bool = False
    adjacency: bool = False


def count_needle(needle: SearchNeedle, folded_text: str) -> int:
    """Occurrences of any of *needle*'s spellings in *folded_text*.

    The single counter every matcher and ranker shares, so the alternation and
    the digit boundary cannot be honored by one caller and dropped by another.
    *folded_text* must already be casefolded (needle spellings are).

    An ordinary needle (no alts, unbounded) costs exactly one :meth:`str.count`,
    the same as before spellings existed — the scan cost that
    :func:`parse_search_query`'s needle caps are sized against. A needle with
    alts costs one scan per spelling, which is why forge expansions carry their
    own cap.
    """
    if not folded_text:
        return 0
    total = 0
    for text in (needle.text, *needle.alts):
        if not text:
            continue
        if not needle.digit_bounded:
            total += folded_text.count(text)
            continue
        # Non-overlapping scan, matching str.count, skipping a hit that sits
        # inside a longer number (``4411`` in ``1544110293``, ``#4411`` in
        # ``#44110``). The LEFT guard applies only to a spelling that starts with
        # a digit: for a delimited spelling the character before it says nothing
        # about the number's length, and demanding a non-digit there would refuse
        # ``#4411`` inside ``owner/repo2#4411`` — a repository whose name ends in
        # a digit, matched against the very reference the query named.
        left_bounded = text[0].isdigit()
        start = 0
        while True:
            found = folded_text.find(text, start)
            if found < 0:
                break
            end = found + len(text)
            before_ok = not left_bounded or found == 0 or not folded_text[found - 1].isdigit()
            after_ok = end >= len(folded_text) or not folded_text[end].isdigit()
            if before_ok and after_ok:
                total += 1
            start = end
    return total


# One owner for splitting text into same-script runs. The name stays because
# `history.py` re-exports it as part of its facade; only the duplicated body is
# gone. The shared version returns a list instead of yielding, which the single
# caller below (a plain `for`) cannot tell apart, and it tolerates an empty
# string where this copy raised IndexError on `token[0]`.
_script_runs = script_runs


# Words that only NAME a reference type in front of its number ("PR 4411",
# "merge request !12"). They are dropped from the gate when they introduce a
# number, because they are not part of the reference: requiring the literal
# "pr" would disqualify the very transcripts this expansion exists to find —
# one that names the pull request only by URL never contains those letters.
_FORGE_MR_WORDS = frozenset({"mr", "merge-request", "merge_request", "mergerequest"})
# "merge" and "request" name no type on their own: "merge 1234" and "requests 12"
# are ordinary prose, and treating either as a reference would drop the word from
# the gate and pull in every session mentioning that number. They qualify only
# TOGETHER ("merge request 12"), which does name GitLab's type.
_FORGE_REQUEST_WORDS = frozenset({"request", "requests"})
_FORGE_CHAIN_ONLY_WORDS = _FORGE_REQUEST_WORDS | frozenset({"merge"})
# Words that DO name a type by themselves, so one of them in the lead-in run is
# what makes a following number a reference.
_FORGE_TYPE_WORDS = (
    frozenset({"pr", "prs", "pull", "pulls", "pull-request", "pull_request", "pullrequest"})
    | frozenset({"issue", "issues"})
    | _FORGE_MR_WORDS
)
# The full lead-in vocabulary: type words plus the chain-only members, which must
# be droppable and visible to the family test even though neither names a type.
_FORGE_REF_WORDS = _FORGE_TYPE_WORDS | _FORGE_CHAIN_ONLY_WORDS


def _lead_names_merge_request(lead: tuple[str, ...]) -> bool:
    """True when the words introducing a number name a GitLab merge request.

    Either an unambiguous word ("MR 12", "merge_request 12") or the two-word
    form "merge request 12".
    """
    if any(word in _FORGE_MR_WORDS for word in lead):
        return True
    return "merge" in lead and any(word in _FORGE_REQUEST_WORDS for word in lead)


def _lead_names_a_type(lead: tuple[str, ...]) -> bool:
    """True when the lead-in run actually names a forge type.

    A run of chain-only words does not: "requests 12" is prose about requests,
    not a reference to item 12, and reading it as one would drop "requests" from
    the gate and admit every session mentioning ``#12``.
    """
    return any(word in _FORGE_TYPE_WORDS for word in lead) or _lead_names_merge_request(lead)


# Punctuation a reference collects from surrounding prose ("(#4411)," / "PR
# #4411."). Stripped before the shapes below are tried, and only from the edges,
# so the token's own delimiters survive. '!' is NOT in the leading set: it is
# GitLab's merge-request sigil, not decoration.
_FORGE_LEAD_PUNCT = "([{<\"'“‘"
_FORGE_TRAIL_PUNCT = ")]}>,.;:?\"'”’"
# A path-shaped reference, with or without a scheme/host: ``pull/4411``,
# ``https://github.com/o/r/pull/4411/files``, ``…/-/merge_requests/12``.
_FORGE_URL_RE = re.compile(
    r"^(?:\S*/)?(?P<kind>pull|pulls|merge_requests|merge-requests|issues)"
    r"/(?P<number>\d{1,9})(?:/\S*)?$"
)
# The owner/repo slug inside such a URL, used only for ranking.
_FORGE_URL_REPO_RE = re.compile(
    r"^(?:https?://)?[^/\s]+\.[^/\s]+"
    r"/(?P<repo>[a-z0-9._-]+(?:/[a-z0-9._-]+)+?)"
    r"(?:/-)?/(?:pull|pulls|merge_requests|merge-requests|issues)/\d"
)
# A sigil reference: ``#4411``, ``!12``, ``owner/repo#4411``, ``pr#4411``.
_FORGE_SIGIL_RE = re.compile(
    r"^(?:(?P<repo>[a-z0-9._-]+(?:/[a-z0-9._-]+)+)|(?P<word>pr|mr|pull|issue))?"
    r"(?P<sigil>[#!])(?P<number>\d{1,9})$"
)
# A glued word+number reference: ``pr4411``, ``pr-4411``, ``mr-12``.
_FORGE_WORD_NUM_RE = re.compile(r"^(?P<word>pr|mr|pull|issue)-?(?P<number>\d{1,9})$")


class _ForgeRef(NamedTuple):
    """A forge item (pull request / merge request / issue) named by a query.

    ``bare`` records that the QUERY spelled the number with no sigil ("PR 4411",
    "issue 42", "pr4411"). That decides whether plain digits are one of the
    item's spellings — see :func:`_forge_spellings`.

    """

    number: str
    merge_request: bool
    repo: str | None
    bare: bool = False


def _forge_spellings(ref: _ForgeRef) -> tuple[str, tuple[str, ...]]:
    """Return ``(canonical, alts)`` — how *ref* can be written in a transcript.

    The families are kept apart because the sigils are not interchangeable:
    GitHub draws pull requests and issues from ONE number sequence (``#4411``,
    ``/pull/4411`` and ``/issues/4411`` are the same item), while GitLab numbers
    merge requests separately from issues, which is why it spells them ``!12``
    and ``#12``. For a SIGIL query that separation is a guarantee — ``!12`` never
    matches ``#12``, a different object. A sigil-free query ("merge request 12")
    also carries the bare digits, which a ``#12`` mention satisfies, so there the
    separation governs ranking rather than exclusion.

    Path spellings carry no leading slash so they match both ``/pull/4411`` in a
    URL and a bare ``pull/4411`` written on its own.

    The prose spellings ("pr 4411", "pull request 4411", "pr4411") are included
    because a transcript often names the item in words rather than with a sigil,
    and a query that typed the sigil should still find it. They match as plain
    substrings, so a glued form can hit inside a longer word (``expr4411``
    satisfies ``pr4411``). That is deliberate: this module matches every other
    needle as a substring — ``cont`` hits ``contention`` — and adding word
    boundaries for some spellings and not others would make the rule harder to
    predict than the false positive it avoids, which ranks last anyway on a
    single hit. The digit boundary still bounds the numeric side.

    A bare digit spelling is a substring of its own sigil and glued spellings, so
    one mention of ``#4411`` counts twice inside a bare reference's needle. That
    only lifts the relevance score of a session that really does reference the
    item, and never gates.

    Plain digits are a spelling only for a ``bare`` reference, and that
    asymmetry is deliberate. A query that typed no sigil ("issue 42") is looking
    for the number as written, so the digits belong; a query that typed one
    ("#4411", a PR URL) was explicit, and admitting bare digits there would make
    ``!12`` match every session that mentions a standalone 12 — ordinary prose
    (a count, a date, a version).

    Recall relative to the plain substring gate this replaces does not rest on
    the list being exhaustive by inspection — three shapes slipped past that
    reasoning — but on a property test that drives every shape the parser
    accepts against a transcript quoting it verbatim. Concretely: a sigil shape
    contains its own canonical spelling (``owner/repo2#4411`` contains
    ``#4411``), a path or URL shape contains a path spelling, and a glued or
    two-token shape contains the bare digits. The one session the expansion can
    still drop is one whose only claim to the old match was the digits sitting
    INSIDE a longer number ("4411" within run id 1544110293) — and excluding that
    is the point of the digit boundary, since such a session never referenced
    the item.
    """
    number = ref.number
    bare = (number,) if ref.bare else ()
    if ref.merge_request:
        return (
            f"!{number}",
            (
                f"merge_requests/{number}",
                f"merge-requests/{number}",
                f"mr {number}",
                f"merge request {number}",
                f"mr{number}",
                *bare,
            ),
        )
    return (
        f"#{number}",
        (
            f"pull/{number}",
            f"pulls/{number}",
            f"issues/{number}",
            f"pr {number}",
            f"pull request {number}",
            f"pr{number}",
            *bare,
        ),
    )


def _forge_lead_in(parts: list[str], index: int) -> tuple[str, ...]:
    """The contiguous run of reference-vocabulary words before ``parts[index]``.

    Raw material for :func:`_forge_type_suffix`, which decides how much of the
    run is actually part of the reference.
    """
    back = index - 1
    while back >= 0 and parts[back] in _FORGE_REF_WORDS:
        back -= 1
    return tuple(parts[back + 1 : index])


def _forge_type_suffix(lead: tuple[str, ...]) -> tuple[str, ...]:
    """The part of *lead* that names the reference's type — its SHORTEST naming suffix.

    Only the words adjacent to the number belong to the reference; anything
    before them is the user's own search term. "merge issue 42" is a query about
    ``merge`` AND issue 42, so the reference is ``issue 42`` and ``merge`` stays
    in the gate — taking the whole run would drop it and return every session
    mentioning #42.

    SHORTEST, not longest: a longer suffix can still contain a type word without
    that word being the head of the phrase ("merge issue" would qualify on
    ``issue`` alone and swallow ``merge``). The shortest naming suffix is the
    complete type phrase and no more — which still admits the two-word forms,
    since neither "request" nor "merge" names a type by itself and only
    ("merge", "request") together qualify.

    Returns ``()`` when no suffix names a type, i.e. the number is not a
    reference at all.

    Two known limitations, accepted rather than special-cased. A chain-only word
    wedged BETWEEN the type word and the number ("issue merge 42") is swallowed,
    because the shortest naming suffix is ``("issue", "merge")``; the mirror case
    ("merge issue 42") is handled. And because the gate is keyed by term text, a
    query repeating a suffix word as its own search term ("pull the pull request
    12") loses that term when the suffix is dropped. Both need a query nobody
    writes, both only widen the result set, and closing them means keying the
    gate by token position rather than by text — a redesign of the needle map,
    not a fix here.
    """
    for size in range(1, len(lead) + 1):
        suffix = lead[len(lead) - size :]
        if _lead_names_a_type(suffix):
            return suffix
    return ()


def _parse_forge_ref(token: str, lead: tuple[str, ...]) -> _ForgeRef | None:
    """Parse *token* as a forge reference, or return ``None``.

    *token* is one casefolded whitespace-separated term; *lead* is the run of
    type-naming words before it (:func:`_forge_lead_in`), which is what makes
    the two-token form ("PR 4411", "merge request 12") a reference rather than a
    bare number, and which names the family when the token itself does not. A
    bare number with no such lead-in is NOT a reference — treating every number
    in a query as one would rewrite ordinary numeric content search (ports,
    error codes, dates).
    """
    token = token.lstrip(_FORGE_LEAD_PUNCT).rstrip(_FORGE_TRAIL_PUNCT)
    if not token:
        return None
    url = _FORGE_URL_RE.match(token)
    if url:
        repo_match = _FORGE_URL_REPO_RE.match(token)
        return _ForgeRef(
            url.group("number"),
            url.group("kind").startswith("merge"),
            repo_match.group("repo") if repo_match else None,
        )
    sigil = _FORGE_SIGIL_RE.match(token)
    if sigil:
        # The TYPED sigil decides the family, not the word before it: "#" names
        # the shared pull/issue sequence and "!" names GitLab's merge requests,
        # so letting a word override the sigil produced a reference ("mr#12")
        # none of whose spellings was the string the user typed.
        return _ForgeRef(
            sigil.group("number"),
            sigil.group("sigil") == "!",
            sigil.group("repo"),
        )
    glued = _FORGE_WORD_NUM_RE.match(token)
    if glued:
        return _ForgeRef(
            glued.group("number"),
            glued.group("word") in _FORGE_MR_WORDS,
            None,
            bare=True,
        )
    if token.isdigit() and len(token) <= 9:
        suffix = _forge_type_suffix(lead)
        if suffix:
            return _ForgeRef(token, _lead_names_merge_request(suffix), None, bare=True)
    return None


#: The resolver that recognizes a query token as some REGISTERED source
#: provider's item and answers with that item's spellings. The built-in forge
#: shapes above are tried first, so it can only ever claim a token no built-in
#: recognized.
#:
#: ONE slot, not a list: the only production registrant is
#: ``register_source_provider``, and the per-plugin fan-out already lives inside
#: the collector it publishes. A list would have bought ordered consultation
#: nothing in-tree can exercise.
#:
#: Deliberately a plain module-level callable: this module imports nothing but
#: the standard library, and the provider registry it serves lives in
#: ``kiro_crew.dashboard.handlers.source_providers`` — a 7k-line module that
#: imports aiohttp at module scope. Reaching UP to ask it would put the whole
#: dashboard HTTP stack on every search, including the CLI and the Discord
#: title-only gate, neither of which runs a web server. The dashboard therefore
#: PUSHES its collector down here at registration time instead.
_search_ref_resolver: Callable[[str], tuple[str, Sequence[str]] | None] | None = None

logger = logging.getLogger(__name__)


def register_search_ref_resolver(
    resolve: Callable[[str], tuple[str, Sequence[str]] | None],
) -> None:
    """Install the token -> ``(canonical, alts)`` resolver. Idempotent.

    Called at provider registration time, from the side that owns the provider
    registry. Re-registration is the expected case, not an error — the registry
    publishes the same collector on every provider registration — and the slot
    simply holds the latest.

    A resolver must be PURE and allocation-cheap: it is consulted for every term
    of every query, and both :func:`snippet_needles` and
    :meth:`ConversationLog.search_sessions` re-parse, so a search costs at least
    two passes over the tokens. Any I/O, config read or lock in a resolver
    becomes per-keystroke latency in the dashboard's search box. Nothing enforces
    that beyond this contract.
    """
    global _search_ref_resolver
    _search_ref_resolver = resolve


def reset_search_ref_resolver_for_tests() -> None:
    """Drop the resolver. Test-only: the slot is module state."""
    global _search_ref_resolver
    _search_ref_resolver = None


def _provider_search_ref(token: str) -> tuple[str, tuple[str, ...]] | None:
    """Ask the registered resolver to recognize *token*.

    Returns ``(canonical, alts)`` NORMALIZED — casefolded, de-duplicated, empties
    dropped, alts capped at :data:`_MAX_SEARCH_REF_SPELLINGS` — or ``None``.

    This is the ONLY normalizer. The dashboard collector hands the first answer
    through without judging it, so casefolding, the shape checks, the cap and the
    dedup all happen once, here, where the cost they bound is paid. It matters
    because the failure is SILENT — :func:`parse_search_query` casefolds its input
    and :func:`count_needle` requires already-folded needles, so a spelling that
    arrives with a capital letter produces a needle that matches nothing, with no
    error and no log; it just returns zero results.

    Every way a resolver can fail is contained, because a plugin defect must not
    make the search box stop working: raising when CALLED, returning a malformed
    answer, and raising while its ``alts`` are READ — the hook promises a
    ``Sequence``, which cannot do that, but a resolver ignoring the contract can,
    so the read sits inside a ``try`` and the answer is dropped whole. Every case
    logs — a traceback where an exception was raised, the offending value where a
    shape was merely wrong. None of them is silent: silence is the defect above.
    """
    resolve = _search_ref_resolver
    if resolve is None:
        return None
    try:
        found = resolve(token)
    except Exception:
        logger.debug("search ref resolver %r failed on %r", resolve, token, exc_info=True)
        return None
    if found is None:
        return None
    try:
        canonical, alts = found
    except Exception:
        # `Exception`, not just TypeError/ValueError: the ANSWER may itself be lazy,
        # so unpacking it runs plugin code that can raise anything at all.
        logger.debug("search ref resolver %r returned a malformed answer", resolve, exc_info=True)
        return None
    if not isinstance(canonical, str) or not canonical.strip():
        logger.debug("search ref resolver %r returned an invalid canonical %r", resolve, canonical)
        return None
    if isinstance(alts, str) or not isinstance(alts, Iterable):
        logger.debug("search ref resolver %r returned invalid alts %r", resolve, alts)
        return None
    folded = canonical.casefold()
    try:
        # islice, not tuple(alts): a resolver ignoring the `Sequence` contract can
        # hand back an endless iterable, and materializing it would hang the parse.
        candidates = tuple(itertools.islice(alts, _MAX_SEARCH_REF_SPELLINGS))
    except Exception:
        # A `Sequence` cannot raise here; a resolver ignoring the contract can,
        # and its answer is dropped whole rather than read half-way.
        logger.debug(
            "search ref resolver %r raised while its alts were read", resolve, exc_info=True
        )
        return None
    spellings: dict[str, None] = {}
    for alt in candidates:
        if isinstance(alt, str) and alt.strip():
            spellings[alt.casefold()] = None
        else:
            logger.debug("search ref resolver %r returned an invalid alt %r", resolve, alt)
    spellings.pop(folded, None)
    # Containment: at least one spelling must actually carry the token the user
    # typed, or the answer describes some other item and would gate (or rank) a
    # query on text it never named.
    typed = token.casefold()
    if typed not in folded and not any(typed in spelling for spelling in spellings):
        logger.debug("search ref resolver %r answered %r without the typed token", resolve, folded)
        return None
    return (folded, tuple(spellings))


def parse_search_query(query: str) -> tuple[list[SearchNeedle], str, bool]:
    """Return ``(needles, phrase, adjacency_floor)`` for a search *query*, casefolded.

    ``phrase`` is the whole normalized query. ``needles`` are derived from the
    DISTINCT whitespace-separated terms in first-seen order; every caller that
    matches (:meth:`ConversationLog.search_sessions`) or locates
    (:func:`snippet_needles` for the snippet builders) query text derives from
    this one parse so the halves of a query cannot drift apart.

    Non-CJK terms become one required, weight-1.0 needle each — the classic
    substring-AND behavior (``"cont"`` hits ``"contention"``). A run of CJK
    characters cannot keep that rule: CJK text is written without spaces, so
    requiring the run verbatim demands the user's exact sentence and a
    multi-word query like ``"修复内存泄漏"`` would only ever match transcripts
    containing that literal string. Each CJK run therefore expands to:

    * its individual characters as REQUIRED needles at :data:`_CJK_CHAR_WEIGHT`
      — maximal recall, since a document discussing all the query's words
      contains all of its characters regardless of word order; and
    * its overlapping character bigrams as ADJACENCY needles at weight 1.0 —
      the standard dictionary-free CJK adjacency signal. They are not
      individually required, but when a query carries any, a session must hit
      AT LEAST ONE to qualify (see :meth:`ConversationLog.search_sessions`):
      individual han/kana characters are so common that a pure character
      scatter is noise, so the adjacency floor keeps the character gate's
      recall (words apart still match — each word IS a bigram hit) without
      letting scatter-only sessions fill the result page. Each occurrence also
      scores, so documents keeping more of the query adjacent rank higher.
      A single-character run stays one required, weight-1.0 needle: it IS the
      whole term.

    Precision moves from the gate into the ranking, which is the module's
    existing philosophy for the substring-prefix looseness on ASCII terms.

    A term that names a FORGE ITEM — a pull request, merge request or issue —
    becomes ONE required needle carrying every spelling of that item instead of
    the literal term (see :func:`_parse_forge_ref`). ``#4411``, ``pr 4411``,
    ``pull/4411``, a full PR URL and ``owner/repo#4411`` therefore all find the
    same sessions, whichever form each transcript happens to use, and the
    spellings are digit-bounded so ``#4411`` never matches ``#44110``. A naming
    word that introduces a number ("PR 4411") is dropped from the gate: it is
    not part of the reference, and requiring the letters "pr" would disqualify a
    transcript that names the pull request only by URL. When the query spelled
    the number with no sigil, plain digits stay one of the spellings, so the
    expansion keeps the recall of the literal AND it replaces — the one session
    it can drop is a session whose digits merely sat inside a longer number,
    which is what the digit boundary is for. Only a run of words that actually
    NAMES a type makes a following number a reference: "requests 12" and
    "merge 1234" stay literal terms, since dropping such a word from the gate
    would trade a real term for every session mentioning that number. A BARE
    number with no naming word at all is not a reference either: it keeps its
    plain substring needle, so numeric content search is unchanged, and gains
    the spellings as scoring-only needles, so the session that actually
    references pull request 4411 outranks one that merely contains those digits
    inside a run id.

    Bounds (both exist because **every needle costs one full scan** of a
    session's text): required needles cap at :data:`SEARCH_MAX_TOKENS` and
    scoring extras at :data:`_SEARCH_MAX_SCORING_EXTRAS`. Deduplication is free
    correctness — a repeated term cannot change an AND match. Truncation in
    EITHER set makes the query only LOOSER — fewer required terms, and a
    truncated adjacency set additionally WAIVES the adjacency floor (the third
    tuple element comes back ``False``), because enforcing "at least one bigram
    hit" against a partial bigram set would EXCLUDE a session that matches only
    a dropped bigram — turning a cost cap into a hidden gate for exactly the
    long spaceless queries this parse exists to serve. Looser can admit extra
    results but never hide a session that would have matched — the safe
    direction when the alternative is a keystroke-driven search stalling for
    seconds.

    A whitespace-only query yields ``([], "", False)``; callers treat that as
    "matches nothing" rather than "matches everything".
    """
    parts = query.casefold().split()
    if not parts:
        return ([], "", False)
    phrase = " ".join(parts)
    required: dict[str, SearchNeedle] = {}
    extras: dict[str, SearchNeedle] = {}
    ranking: dict[str, SearchNeedle] = {}
    forge_budget = _SEARCH_MAX_FORGE_REFS
    # One ledger keyed by the item's canonical spelling, so a slot is charged per
    # ITEM rather than per needle. "#4411 4411" names one pull request twice — as
    # a required reference and as a bare number's ranking hint — and charging
    # both spent a phantom slot that could push a later distinct reference past
    # the cap. Keyed rather than counted so the order the two forms appear in
    # cannot change the outcome.
    charged: set[str] = set()
    for index, part in enumerate(parts):
        lead = _forge_lead_in(parts, index)
        ref = _parse_forge_ref(part, lead)
        if ref is None and not part.isdigit():
            # Built-ins always win: only a token no forge shape recognized is
            # offered to a registered provider. The extra `isdigit` guard is what
            # keeps a provider from turning a BARE NUMBER into a gate — the same
            # rule _parse_forge_ref applies to itself, and for the same reason.
            # Gating "987654321" on a provider spelling would demand that
            # spelling of every result, silently narrowing ordinary numeric
            # content search. So a bare number is never offered to a provider at
            # all, and the built-in ranking hint below carries built-in
            # spellings only.
            #
            # A provider contributes SPELLINGS ONLY, never lead-in vocabulary:
            # _forge_type_suffix's words are popped from the gate, and the common
            # English words a provider would want ("review", "cr") are far more
            # frequent in transcripts than "pr"/"mr", so admitting them would
            # trade a real search term for every session mentioning that number.
            # A prefixed token carries its own type and needs no lead-in.
            provider_ref = _provider_search_ref(part)
            if provider_ref is not None:
                canonical, alts = provider_ref
                if canonical in required:
                    # Already gated by an earlier spelling of the same item; the
                    # needle carries the whole set, so there is nothing to merge.
                    continue
                if canonical in charged or forge_budget:
                    if canonical not in charged:
                        charged.add(canonical)
                        forge_budget -= 1
                    required.setdefault(canonical, SearchNeedle(canonical, 1.0, True, alts, True))
                    # Continue like the built-in path does: the literal token must
                    # not ALSO survive into the gate through _script_runs, or a
                    # provider query would carry two required needles.
                    continue
                # Budget exhausted and this item has no slot — fall through and
                # degrade to a plain literal needle, exactly as an over-budget
                # forge token does.
        if ref is not None:
            canonical, alts = _forge_spellings(ref)
            if canonical in required:
                # The item is already gated, but this occurrence still carries
                # information: its own naming words must leave the gate, and a
                # sigil-free spelling contributes the bare digits the first
                # occurrence may not have had. Skipping outright let
                # "#42 issue 42" keep `issue` required AND lose the bare-digit
                # spelling — narrowing a query that named the item twice, which
                # the loosen-only contract forbids.
                for word in _forge_type_suffix(lead):
                    required.pop(word, None)
                if ref.bare:
                    seen = required[canonical]
                    if ref.number not in seen.alts:
                        required[canonical] = seen._replace(alts=(*seen.alts, ref.number))
                continue
            if canonical not in charged and not forge_budget:
                ref = None
        if ref is not None:
            if canonical not in charged:
                charged.add(canonical)
                forge_budget -= 1
            # The words that NAME the reference's type are not part of the search
            # text, and requiring them would disqualify a transcript that names
            # the item only by URL — one that never spells the letters "pr". Only
            # that naming suffix is dropped: in "merge issue 42" the reference is
            # "issue 42" and `merge` is the user's own term, so popping the whole
            # run would return every session mentioning #42.
            for word in _forge_type_suffix(lead):
                required.pop(word, None)
            required.setdefault(canonical, SearchNeedle(canonical, 1.0, True, alts, True))
            if ref.repo:
                # Ranking only: the repo slug appears in a URL mention but not in
                # a prose "#4411" one, so requiring it would hide real hits. It
                # breaks the tie between the same number in two repos.
                ranking.setdefault(ref.repo, SearchNeedle(ref.repo, 1.0, False))
            continue
        if part.isdigit() and len(part) <= 9:
            gh_text, gh_alts = _forge_spellings(_ForgeRef(part, False, None))
            # Charged against the shared ledger, so a number already gated as a
            # reference does not spend a second slot on a hint the dedup below
            # will discard anyway. This branch deliberately does NOT `continue` —
            # the plain digit needle added below is what keeps numeric content
            # search working, and it belongs in the gate whether or not the hint
            # fits in the budget.
            if gh_text not in charged and forge_budget:
                charged.add(gh_text)
                forge_budget -= 1
                mr_text, mr_alts = _forge_spellings(_ForgeRef(part, True, None))
                # Every sigil spelling of the number, both families: these only
                # score, so a wrong-family hit costs a little rank rather than
                # admitting a wrong object into the results.
                spellings = tuple(
                    dict.fromkeys(s for s in (gh_text, *gh_alts, mr_text, *mr_alts) if s != part)
                )
                ranking[gh_text] = SearchNeedle(
                    spellings[0], _FORGE_REF_WEIGHT, False, spellings[1:], True
                )
        for run, is_cjk in _script_runs(part):
            if not is_cjk or len(run) == 1:
                required.setdefault(run, SearchNeedle(run, 1.0, True))
                continue
            for ch in run:
                required.setdefault(ch, SearchNeedle(ch, _CJK_CHAR_WEIGHT, True))
            for i in range(len(run) - 1):
                bigram = run[i : i + 2]
                extras.setdefault(bigram, SearchNeedle(bigram, 1.0, False, adjacency=True))
    # A spelling that is already REQUIRED must not also score as a ranking hint:
    # a query naming the same item twice ("#4411 4411") would count its hits
    # twice over.
    for text in [t for t in ranking if t in required]:
        del ranking[text]
    needles = list(required.values())[:SEARCH_MAX_TOKENS]
    needles.extend(list(extras.values())[:_SEARCH_MAX_SCORING_EXTRAS])
    needles.extend(ranking.values())
    adjacency_floor = 0 < len(extras) <= _SEARCH_MAX_SCORING_EXTRAS
    return (needles, phrase, adjacency_floor)


def snippet_needles(query: str) -> list[str]:
    """Return *query*'s needles ordered for snippet centering, phrase first.

    The snippet builders try needles in order and center the excerpt on the
    first hit, so ordering is a display-quality decision: the exact phrase is
    the most meaningful anchor, then full-weight needles (whole terms and CJK
    bigrams) in first-seen order — preserving the pre-CJK behavior for ASCII
    queries, where a predictable first-typed-term fallback is part of the
    contract — and down-weighted lone CJK characters last, the anchor of last
    resort. Returns ``[]`` for a whitespace-only query.

    A forge-reference needle contributes every spelling it carries, right after
    its canonical form: the transcript that matched may name the item any of
    those ways, and centering the snippet on the mention is the whole point.
    """
    needles, phrase, _ = parse_search_query(query)
    if not needles:
        return []
    # Stable sort on weight only: within a weight class the parse's insertion
    # order (required terms first-seen, then bigrams) is the display order.
    ordered = sorted(needles, key=lambda n: -n.weight)
    out: list[str] = []
    for text in (phrase, *(t for n in ordered for t in (n.text, *n.alts))):
        if text not in out:
            out.append(text)
    return out


def needles_match_text(
    needles: list[SearchNeedle], folded_text: str, adjacency_floor: bool = True
) -> bool:
    """True when *folded_text* satisfies the query gate that *needles* encode.

    The single-string form of :meth:`ConversationLog.search_sessions`' match
    rule, for callers that filter one text field (e.g. Discord session
    resume's title-only fallback) and must not grow a second spelling of
    tokenization: every REQUIRED needle must appear as a substring, and when
    the query carries adjacency (bigram) needles at least one must hit — the
    same adjacency floor that keeps character scatter out of content search.
    Pass :func:`parse_search_query`'s ``adjacency_floor`` element: it is
    ``False`` when the bigram set was truncated, which WAIVES the floor here
    exactly as in ``search_sessions`` (a partial bigram set cannot prove
    "no adjacency anywhere"). *folded_text* must already be casefolded
    (needle texts are).

    Satisfaction is per NEEDLE, not per literal: a needle carrying alternative
    spellings (a forge reference) is satisfied by any one of them, via the
    shared :func:`count_needle`.
    """
    if not needles:
        return False
    adjacency_hit = False
    has_adjacency = False
    for needle in needles:
        if needle.required:
            if not count_needle(needle, folded_text):
                return False
        elif needle.adjacency:
            has_adjacency = True
            if count_needle(needle, folded_text):
                adjacency_hit = True
    return adjacency_hit or not has_adjacency or not adjacency_floor


# Hard ceilings on the memory the two search memos may hold, in real retained
# bytes as reported by ``str.__sizeof__`` — NOT in characters.
#
# ``len()`` is the wrong unit and dangerously so: CPython stores a ``str`` in the
# narrowest width its contents allow, so one character is 1 byte for latin-1, 2
# for the BMP, and 4 for astral planes. A ceiling of 160 MB counted in characters
# retains 168 MB of ASCII, 336 MB of CJK, or 671 MB of emoji — and CJK is the
# ordinary case for a non-English corpus, not a pathological one. The sizers
# therefore call ``__sizeof__`` per string, and the snippet memo also charges for
# its list container.
#
# Why bytes and not an entry count: a session is read up to
# ``_SESSION_MAX_BYTES`` (2 MB), so ``_SEARCH_SCAN_WINDOW`` entries is anywhere
# from a few MB to ~1 GB depending on the corpus. An entry count therefore
# bounds nothing that matters; it only *looked* safe because real sessions are
# small (a 171 MB / 230-session corpus folds to ~8 MB).
#
# The two are separate rather than one shared pool so a corpus that blows the
# snippet budget cannot starve the fold, which is the one that keeps matching
# off the critical path. Their sum is the ceiling to reason about.
_SEARCH_FOLD_BUDGET_BYTES = 96 * 1024 * 1024
_SEARCH_SNIPPET_BUDGET_BYTES = 64 * 1024 * 1024


def _facade_search_scan_window() -> int:
    """Read the facade's live scan limit without creating an import cycle.

    The import happens only when a search runs.  Keeping this as a getter is
    intentional: tests and callers historically patch
    ``kiro_crew.history._SEARCH_SCAN_WINDOW`` after construction.
    """
    from kiro_crew import history as history_facade

    return history_facade._SEARCH_SCAN_WINDOW


class SessionCatalogProjection:
    """List, aggregate, search, and snippet-project conversation sessions."""

    def __init__(self, log: "ConversationLog") -> None:
        self._log = log

    @staticmethod
    def _canonical_key(key: str) -> str:
        """Collapse stacked ``dashboard_`` prefixes to a single one.

        Files like ``dashboard_dashboard_chat-1-123`` are duplicates of
        ``dashboard_chat-1-123`` caused by resume round-trips.  Return
        the canonical (single-prefix) form so callers can deduplicate.
        """
        if not key.startswith("dashboard_"):
            return key
        stripped = key
        while stripped.startswith("dashboard_"):
            stripped = stripped[len("dashboard_") :]
        return f"dashboard_{stripped}" if stripped else key

    def list_sessions(self) -> list[dict]:
        """Return metadata for all session files, newest first.

        Deduplicates stacked ``dashboard_`` prefix files, keeping the
        most recently modified version.  Uses mtime-based metadata cache
        when available, falling back to reading only the first line for
        title extraction.
        """
        sessions: list[dict] = []
        if not self._log._dir.exists():
            return sessions
        # Deduplicate stacked dashboard_ prefixes by canonical key, keeping newer
        by_canon: dict[str, dict] = {}
        for path in self._log._dir.glob("*.jsonl"):
            key = path.stem
            # Snapshot the invalidation generation BEFORE the stat: the
            # first-line fill below publishes under this stat's mtime, and a
            # housekeeping rewrite restores the pre-write mtime
            # (``_restore_mtime``), so only the generation can prove the
            # stat → read → publish window stayed write-free for this key.
            gen = self._log._cache_gen(key)
            try:
                stat = path.stat()
            except OSError:
                continue
            # Skip symlinks — these are handoff aliases pointing to the real session
            if path.is_symlink():
                continue
            meta: dict = {
                "key": key,
                "messages": max(1, int(stat.st_size / 200)),
                "modified": stat.st_mtime,
                "created": datetime.fromtimestamp(stat.st_mtime).isoformat(),
            }
            # Try metadata cache first (populated by _read_metadata calls)
            cached_meta = self._log._meta_cache.get(key)
            if (
                cached_meta
                and cached_meta[0] == stat.st_mtime
                and cached_meta[1] == self._log._cache_gen(key)
            ):
                d = cached_meta[2]
                if d.get("created_at"):
                    meta["created"] = d["created_at"]
                if d.get("title"):
                    meta["title"] = d["title"]
                if d.get("agent"):
                    meta["agent"] = d["agent"]
                meta["memory_mode"] = d.get("memory_mode", "persistent")
                if d.get("folder_id"):
                    meta["folder_id"] = d["folder_id"]
            else:
                # Read only the first line for metadata
                try:
                    with open(path, encoding="utf-8") as f:
                        first_line = f.readline().strip()
                    if first_line:
                        d = json.loads(first_line)
                        if d.get("_type") == "metadata":
                            if d.get("created_at"):
                                meta["created"] = d["created_at"]
                            if d.get("title"):
                                meta["title"] = d["title"]
                            if d.get("agent"):
                                meta["agent"] = d["agent"]
                            meta["memory_mode"] = d.get("memory_mode", "persistent")
                            if d.get("folder_id"):
                                meta["folder_id"] = d["folder_id"]
                            # Guarded publish — discard the fill if a write
                            # invalidated this key inside the stat → read
                            # window (see the generation snapshot above).
                            self._log._publish_if_current(
                                self._log._meta_cache,
                                key,
                                (stat.st_mtime, gen, d),
                                key=key,
                                gen=gen,
                            )
                except Exception:
                    pass
            # Ensure memory_mode is always present (old sessions lack it)
            meta.setdefault("memory_mode", "persistent")
            # Extract first user message as title fallback
            if "title" not in meta:
                msg_cached = self._log._msg_cache.get(key)
                if (
                    msg_cached
                    and msg_cached[0] == stat.st_mtime
                    and msg_cached[1] == self._log._cache_gen(key)
                ):
                    for m in msg_cached[2]:
                        if m.get("role") == "user" and m.get("content"):
                            meta["title"] = m["content"][:80]
                            break
                else:
                    try:
                        with open(path, encoding="utf-8") as f:
                            for i, ln in enumerate(f):
                                if i > 20:
                                    break
                                ln = ln.strip()
                                if not ln:
                                    continue
                                try:
                                    d = json.loads(ln)
                                except json.JSONDecodeError:
                                    continue
                                if d.get("role") == "user" and d.get("content"):
                                    meta["title"] = d["content"][:80]
                                    break
                    except Exception:
                        pass
            if "title" not in meta:
                meta["title"] = key
            # Deduplicate: keep newer entry per canonical key
            canon = self._log._canonical_key(key)
            existing = by_canon.get(canon)
            if existing is None or stat.st_mtime >= existing["modified"]:
                by_canon[canon] = meta
        sessions = list(by_canon.values())
        sessions.sort(key=lambda s: s.get("modified", 0), reverse=True)
        return sessions

    def agent_usage(self) -> dict[str, tuple[int, float]]:
        """Return ``{agent_name: (session_count, last_used_mtime)}`` per agent.

        Built on top of :meth:`list_sessions` (not a fresh directory glob) so it
        inherits that method's canonical-session dedup and symlink-skip — counts
        are therefore per logical conversation, not per raw ``.jsonl`` file.
        Sessions whose metadata never recorded an agent are ignored.
        """
        usage: dict[str, tuple[int, float]] = {}
        for meta in self._log.list_sessions():
            agent = meta.get("agent")
            if not agent:
                continue
            count, last_used = usage.get(agent, (0, 0.0))
            usage[agent] = (count + 1, max(last_used, meta.get("modified", 0.0)))
        return usage

    def search_sessions(self, query: str, limit: int = 50) -> list[dict]:
        """Return session metadata for files whose message content matches *query*.

        This is the ONE ranking every transcript-search consumer shares — the
        dashboard history filter, the ``search_chat_history`` MCP tool, and
        Discord session resume — so the recency weighting and CJK adjacency
        floor below apply to all of them by design: "the best match for this
        query" should not depend on which surface asked.

        The query is parsed by :func:`parse_search_query` into needles, and a
        session matches only when **every** REQUIRED needle appears somewhere in
        its title or content — an AND over the whole document, not a single
        whole-query substring. That is what makes a natural multi-word query
        work: ``"ack contention hypotheses"`` finds a session discussing all
        three, which a whole-phrase match missed because those exact words never
        sit adjacent. A single-token query is unchanged by this — one token IS
        the phrase. CJK terms gate on their individual characters (CJK text has
        no spaces to split on, so the run itself would demand the user's exact
        sentence), rank on their character bigrams, and require at least one
        bigram hit (the adjacency floor) — see :func:`parse_search_query` for
        the recall/precision split.

        A term naming a forge item (``#4411``, ``pr 4411``, ``pull/4411``, a PR
        URL, ``owner/repo#4411``) gates on ANY spelling of that item rather than
        on the literal term, so the session that discussed the pull request is
        found whichever form its transcript used. A bare number additionally
        RANKS on those spellings while still gating on the plain digits.

        Each needle is matched case-insensitively as a SUBSTRING (so ``"cont"``
        hits ``"contention"``, which keeps search-as-you-type responsive) using
        full Unicode case folding via :meth:`str.casefold` (so e.g. German ``ß``
        folds to ``ss``).  Matching only on parsed ``content`` avoids false
        positives from JSON structural elements (e.g. the word ``"user"``
        matching every ``"role": "user"`` line).

        Ranking (higher is better)::

            score = ((title_hits * _TITLE_BOOST)
                  + (content_hits / sqrt(1 + doc_chars / 1024))) * recency

        where ``*_hits`` sum the per-needle weighted counts, plus
        ``_PHRASE_BOOST`` per occurrence of the exact whole query when it
        carries more than a single needle. The phrase bonus rewards adjacency:
        at comparable term frequency, the session containing the words TOGETHER
        as typed ranks above one that merely mentions them far apart.  It is
        deliberately a bonus and not an override — a session repeating one term
        far more often still wins on raw term frequency, exactly as it already
        did for a single-token query.  (Saturating term frequency, BM25-style,
        would change that; it would also re-rank every existing single-token
        query, so it is out of scope here.)

        ``recency`` is a bounded multiplicative boost — ``1 +
        _RECENCY_MAX_BOOST / (1 + age_days / _RECENCY_HALF_WEIGHT_DAYS)`` — so
        at comparable relevance today's session outranks last year's, while a
        decisively better old match still wins (the boost tops out at ×2.5 and
        never drops below ×1).

        Title matches get a strong field boost - titles are short and
        intentional, so a hit there is strong evidence.  Content matches
        are normalized by a sqrt length factor so a long session with a
        casual mention doesn't outrank a short, focused one.  (Simpler
        than BM25's ``(1-b) + b*(dl/avgdl)`` because we avoid the
        two-pass scan needed for corpus stats.)  Sessions missing any
        required needle are dropped.  Ties break by recency (existing
        ``list_sessions`` order - newest first).  Caps results at *limit*.
        Only the ``_SEARCH_SCAN_WINDOW`` most recent files are scored, so
        I/O stays bounded even with hundreds of sessions.
        """
        if not query or limit <= 0 or not self._log._dir.exists():
            return []
        needles, phrase, adjacency_floor = parse_search_query(query)
        if not needles:
            # Whitespace-only query: no needles to require, so nothing can match.
            # Returning [] keeps this from degenerating into "match everything",
            # and skips reading every session file to reach that conclusion.
            return []
        required = [n for n in needles if n.required]
        # True when the phrase carries more than the single needle does, so an
        # exact-phrase bonus is meaningful. Not a token-count test: dedup
        # collapses "a a" to one needle while its phrase is still two words, and
        # a single CJK term expands to several needles while the phrase is what
        # rewards its characters appearing together.
        multi = [n.text for n in required] != [phrase]
        now = _time.time()
        # (score, -rank, meta, needs_snippet)
        scored: list[tuple[float, int, dict, bool]] = []
        window = self._log.list_sessions()[: _facade_search_scan_window()]
        self._log._prune_search_memos({m["key"] for m in window})
        for rank, meta in enumerate(window):
            doc_chars, folded = self._log._folded_content(meta["key"])
            title_folded = (meta.get("title") or "").casefold()
            content_hits = 0.0
            title_hits = 0.0
            adjacency_hits = 0
            disqualified = False
            for needle in needles:
                in_content = count_needle(needle, folded)
                in_title = count_needle(needle, title_folded)
                if needle.required and not in_content and not in_title:
                    # AND semantics: one absent required needle disqualifies the
                    # session, so stop counting the rest.
                    disqualified = True
                    break
                if needle.adjacency:
                    adjacency_hits += in_content + in_title
                content_hits += in_content * needle.weight
                title_hits += in_title * needle.weight
            if disqualified or (not content_hits and not title_hits):
                continue
            if adjacency_floor and not adjacency_hits:
                # Adjacency floor: a CJK query whose characters ALL appear but
                # never once adjacently (no bigram hit anywhere) is a character
                # scatter, not a word hit. Individual han/kana characters are
                # common enough that ranking alone cannot keep such noise off a
                # result page whose real hits number fewer than *limit* — so
                # scatter-only sessions are excluded, not merely down-ranked. A
                # document containing the query's words apart still qualifies:
                # each word is itself a bigram hit. The parse WAIVES the floor
                # (adjacency_floor=False) when its bigram set was truncated —
                # judging "no adjacency anywhere" against a partial set would
                # hide long-query matches (see parse_search_query).
                continue
            if multi:
                # Reward the words appearing together, in order, as typed.
                if folded:
                    content_hits += folded.count(phrase) * _PHRASE_BOOST
                title_hits += title_folded.count(phrase) * _PHRASE_BOOST
            length_norm = math.sqrt(1 + doc_chars / 1024)
            score = title_hits * _TITLE_BOOST + content_hits / length_norm
            # Recency boost: multiplicative and bounded to (1.0, 2.5], so a
            # fresh session with comparable relevance outranks a stale one, but
            # an old session with a decisively better match still wins — the
            # boost can at most double a score, never zero one out. Halves its
            # extra weight every _RECENCY_HALF_WEIGHT_DAYS of age.
            age_days = max(0.0, now - meta.get("modified", 0.0)) / 86400
            score *= 1.0 + _RECENCY_MAX_BOOST / (1.0 + age_days / _RECENCY_HALF_WEIGHT_DAYS)
            # Negate rank so a smaller (newer) rank wins ties after score desc sort.
            scored.append((score, -rank, meta, content_hits > 0))
        scored.sort(reverse=True)

        # Snippets are attached AFTER the sort+slice, so the cost is proportional
        # to the rows actually returned rather than to every session that
        # matched. A snippet cannot come from the folded cache (it needs the
        # original text, so offsets line up), so building one per match put a
        # full re-read back on the hot path — dominating the query once the fold
        # itself was memoized.
        out: list[dict] = []
        for _score, _rank, meta, needs_snippet in scored[:limit]:
            snippet = self._log._content_snippet(meta["key"], query) if needs_snippet else ""
            out.append({**meta, "snippet": snippet} if snippet else meta)
        return out

    def _folded_content(self, key: str) -> tuple[int, str]:
        """Return ``(doc_chars, casefolded_content)`` for *key*, memoized by
        mtime plus invalidation generation.

        ``doc_chars`` counts the ORIGINAL (unfolded) characters, because it
        feeds the length normalizer in :meth:`search_sessions` and folding can
        change a string's length (``ß`` -> ``ss``).

        The folded blob joins the messages' string ``content`` fields in file
        order with ``\\x00``. That separator cannot appear in a user query, so
        it prevents a match spanning two messages while still allowing one
        ``count`` call over the whole session instead of one per message.

        Returns ``(0, "")`` for a missing/unreadable file or a session with no
        textual content. A read failure is deliberately NOT cached — see below.
        """
        path = self._log._path(key)
        try:
            mtime = path.stat().st_mtime
        except OSError:
            self._log._folded_cache.pop(key, None)
            self._log._snippet_cache.pop(key, None)
            return (0, "")
        cached = self._log._folded_cache.get(key)
        # The hit wants the LATEST generation (a moved counter means a write
        # landed, so a miss is the correct answer), so it is read at check
        # time rather than snapshotted earlier — matching ``_snippet_texts``.
        if cached and cached[0] == mtime and cached[1] == self._log._cache_gen(key):
            return (cached[2], cached[3])
        # Cold: serialize against this key's writers for the whole
        # stat -> read -> store sequence.
        #
        # The mtime guard cannot protect this window, because the housekeeping
        # rewrites deliberately RESTORE the pre-write mtime (``_restore_mtime``,
        # so compaction does not reorder ``list_sessions``). A fold that started
        # before such a rewrite and stored after its ``_invalidate_cache`` would
        # sit in the cache holding pre-rewrite text under a mtime the file still
        # has — undetectable, so the newly saved messages would be missing from
        # every later search for the life of the process.
        #
        # ``_file_lock`` is the same process-wide, path-keyed RLock every writer
        # takes first in ``_locked`` — shared across every ``ConversationLog``
        # instance over this file — so holding it here orders this fold against
        # append / rewrite / metadata edits for this key, whichever instance
        # performs them. It is acquired ONLY on the miss path: a warm search
        # never contends, and two threads racing the same cold key fold once
        # (the re-check below). What the lock CANNOT fix is invalidation reach:
        # a writer's ``_invalidate_cache`` pops only its own instance's caches,
        # so an entry already sitting warm in THIS instance survives a rewrite
        # performed through a different instance, mtime restored and all. That
        # is why entries carry the generation and the warm-hit checks above and
        # below require it to match.
        with self._log._file_lock(key):
            # Snapshot the fill baseline under the lock and BEFORE the stat:
            # the mtime that stat returns can survive a housekeeping rewrite
            # (``_restore_mtime``), so only an unmoved generation can prove the
            # stat → read → publish window stayed write-free. A writer that ran
            # between the lock-free probe and the acquire already bumped the
            # counter, and the fold below is ordered AFTER it, so its result is
            # current for this newer generation.
            gen = self._log._cache_gen(key)
            try:
                mtime = path.stat().st_mtime
            except OSError:
                self._log._folded_cache.pop(key, None)
                self._log._snippet_cache.pop(key, None)
                return (0, "")
            cached = self._log._folded_cache.get(key)
            if cached and cached[0] == mtime and cached[1] == self._log._cache_gen(key):
                return (cached[2], cached[3])
            built = self._log._build_folded(key, mtime, gen)
            if built is None:
                # The read failed rather than finding no content. Caching that
                # would be keyed by an mtime the file still has, so a session
                # made transiently unopenable (fd exhaustion, or a Windows
                # indexer / AV holding a just-written file — the same window
                # ``_METADATA_READ_ATTEMPTS`` exists for) would stay
                # unsearchable until something wrote to it again. Fail open:
                # report empty for this query and retry on the next one.
                return (0, "")
            self._log._publish_if_current(
                self._log._folded_cache, key, (mtime, gen, built[0], built[1]), key=key, gen=gen
            )
            return built

    def _prune_search_memos(self, live_keys: set[str]) -> None:
        """Free budget held by sessions that have left the scan window.

        Only runs for a memo that is currently refusing admissions, so an
        uncontended cache never pays the scan. Entries outside *live_keys* are
        unreachable by any future search (it walks only the
        ``_SEARCH_SCAN_WINDOW`` most recent sessions), so dropping them cannot
        cost a hit — which is what makes this safe where an LRU eviction would
        not be.

        Called before the walk, so a refusal raised *during* a walk is released
        on the NEXT query rather than that one: the prune needs the scan window,
        which the walk computes. The lag is one query (~20 ms for a keystroke
        caller) and self-clearing; what it prevents is budget staying pinned by
        aged-out sessions for the life of the process.
        """
        for cache in (self._log._folded_cache, self._log._snippet_cache):
            if cache.refused_since_prune():
                cache.retain(live_keys)

    def _build_folded(self, key: str, mtime: float, gen: int) -> tuple[int, str] | None:
        """Parse *key* and fold its content — the cache-miss half of
        :meth:`_folded_content`.

        Returns ``None`` when the file could not be read, which the caller must
        distinguish from ``(0, "")`` (a session with no textual content): the
        former must not be cached.

        Reads the file via :meth:`_iter_message_texts` rather than going through
        :meth:`_read_messages`, for two reasons.

        Memory: that method memoizes the PARSED message dicts, and a search
        touches every session in the scan window, so routing the fold through it
        pins the whole corpus's parsed form in gateway RSS as a side effect of
        searching. On a 136 MB / 125-session corpus that is ~330 MB of parsed
        dicts versus ~37 MB for the folded strings this actually needs.

        Correctness: ``_msg_cache`` is filled by callers that do not hold this
        key's write lock, so an entry can be a pre-rewrite parse stored under a
        restored (unchanged) mtime. Folding from it would launder that staleness
        into the search cache, which the caller's lock cannot prevent. Reading
        the file makes the fold a function of the file alone.

        The caller holds ``_file_lock``, which orders this read against writers
        in THIS process — the lock table is class-level and path-keyed, so that
        includes writers using other ``ConversationLog`` instances. A writer in
        another process holds only the cross-process flock, so it can still
        interleave; if it bumps the mtime, the caller's pre-read stat leaves the
        cached mtime older than the file's and the next access re-folds. A
        cross-process PRESERVED-mtime rewrite, however, is caught by neither
        the lock nor the generation (the counter lives in this process) — a
        known residual gap shared with every memo in this class. *gen* is the
        invalidation-generation snapshot the caller took alongside its stat;
        the snippet store below publishes under it and records it in the entry,
        which is what lets a warm hit notice an in-process rewrite performed
        through a different instance (whose ``_invalidate_cache`` pops only its
        own instance's caches).

        Separated from :meth:`_folded_content` so the memoization is observable:
        a caller (or a test) can count how often the expensive fold actually
        runs, independent of how many queries were served.
        """
        texts: list[str] = []
        try:
            texts = list(self._log._iter_message_texts(key))
        except OSError:
            return None
        if not texts:
            return (0, "")
        # Hand the same list to the snippet memo. The caller has already stat'ed
        # under ``_file_lock`` and passes that mtime and its generation
        # snapshot, so both memos are keyed by one observation of the file and
        # cannot disagree about which revision they hold. Storing here is why
        # the second corpus costs no extra read. The publish guard here is
        # generation-stamp hygiene: the lock already orders this store against
        # in-process writers, so its job is refusing to stamp an entry with an
        # already-superseded generation — the recorded generation is what the
        # warm-hit checks compare against.
        self._log._publish_if_current(
            self._log._snippet_cache, key, (mtime, gen, texts), key=key, gen=gen
        )
        return (sum(len(t) for t in texts), "\x00".join(texts).casefold())

    def _iter_message_texts(self, key: str) -> Iterator[str]:
        """Yield each message's non-empty string ``content`` from *key*'s file.

        One definition of "what counts as searchable text in a session file",
        shared by the fold and the snippet so their skip rules cannot drift apart
        as the on-disk format evolves. Yields in file order; skips blank lines,
        unparseable lines, non-object lines, and the metadata header.

        A generator rather than a list so a caller can stop early — closing it
        closes the file — which is what lets :meth:`_content_snippet` read only as
        far as its first match. Propagates ``OSError``: callers distinguish "could
        not read" from "no text", and that distinction is load-bearing (a read
        failure must not be cached).
        """
        path = self._log._path(key)
        with open(path, "rb") as handle:
            for line in bounded_records(handle, path, label="history_search"):
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(data, dict) or data.get("_type") == "metadata":
                    continue
                content = data.get("content")
                if isinstance(content, str) and content:
                    yield content

    #: Characters of context kept before / after a snippet's match.
    _SNIPPET_LEAD = 40
    _SNIPPET_TRAIL = 100
    #: Hard cap on a returned snippet.
    _SNIPPET_MAX = 200

    def _snippet_texts(self, key: str) -> Iterator[str]:
        """Yield *key*'s message texts for snippet extraction, memo first.

        Prefers ``_snippet_cache`` — filled by :meth:`_build_folded` from the same
        read that produced the fold — and falls back to re-reading the file.

        The memo is validated against the file's current mtime AND the current
        invalidation generation (:meth:`_cache_gen`), so it degrades to the
        file read rather than serving a stale snippet. The mtime alone cannot
        catch a preserved-mtime rewrite performed through a DIFFERENT
        ``ConversationLog`` instance (its ``_invalidate_cache`` pops only its
        own instance's caches); the generation clause is what unhits such an
        entry. Both checks are cheap relative to the parse they avoid, and
        unlike the fold this path does NOT need ``_file_lock``: a snippet is
        display-only, so the worst case for a preserved-mtime rewrite racing
        here is one stale preview line, not a session that stops matching. The
        fold — which decides whether a row appears at all — keeps the lock.

        Falls back for four reasons, all of which must stay non-fatal: the entry
        was refused admission by the byte budget, the fold cached ``(0, "")`` for
        a session with no text and so stored nothing, the file changed since
        the fold, or the entry's generation was superseded by a write.
        Propagates ``OSError`` from the fallback read, which
        :meth:`_content_snippet` already treats as "no snippet".
        """
        cached = self._log._snippet_cache.get(key)
        if cached is not None:
            try:
                mtime_now = self._log._path(key).stat().st_mtime
                if cached[0] == mtime_now and cached[1] == self._log._cache_gen(key):
                    return iter(cached[2])
            except OSError:
                # Let the fallback read raise the OSError the caller handles,
                # rather than deciding here what a vanished file means.
                pass
        return self._log._iter_message_texts(key)

    def _content_snippet(self, key: str, query: str) -> str:
        """Return a match-centered window of *key*'s content around *query*.

        Streams the file and stops at the FIRST matching message, so a query that
        hits early reads only as far as the hit — rather than parsing the whole
        transcript, which on the largest sessions dominated the query. Sharing
        :meth:`_iter_message_texts` with the fold (instead of reading the file
        directly) also keeps search from pinning a parsed transcript in
        ``_msg_cache`` for every row it returns, and keeps the two halves of a
        query agreeing on what counts as searchable text.

        The window is confined to the matching message, which keeps a snippet from
        reading as one sentence when it actually spans two — consistent with
        :meth:`_folded_content`, where the ``\\x00`` join stops a match from
        bridging messages in the first place.

        Display-only and best effort: ``casefold`` is used for the search so it
        agrees with the hit detection in :meth:`search_sessions` (``.lower()``
        misses matches ``casefold`` finds, e.g. ``ß`` / ``İ``, which would yield
        an empty snippet despite a nonzero hit count), but folding can change a
        string's length, so the window may be off by a character or two.

        For a multi-word query the exact phrase is tried first and each needle
        is tried as a fallback (via :func:`snippet_needles`, highest-signal
        first), mirroring :meth:`search_sessions`' AND-over-needles matching —
        otherwise every session that matched on scattered words would come back
        with no snippet at all. Needles are tried per message, so the first
        MESSAGE containing any of them wins rather than the best needle overall;
        that preserves the streaming early-exit above, and this string is
        display-only.

        Returns ``''`` when no needle is locatable in any single message, or
        when the file cannot be read (display-only — never raises at the caller).
        """
        needles = snippet_needles(query)
        if not needles:
            return ""
        try:
            for text in self._log._snippet_texts(key):
                folded = text.casefold()
                for needle in needles:
                    pos = folded.find(needle)
                    if pos < 0:
                        continue
                    start = max(0, pos - self._log._SNIPPET_LEAD)
                    end = min(len(text), pos + len(needle) + self._log._SNIPPET_TRAIL)
                    frag = " ".join(text[start:end].split())
                    prefix = "…" if start > 0 else ""
                    suffix = "…" if end < len(text) else ""
                    return (prefix + frag + suffix)[: self._log._SNIPPET_MAX]
        except OSError:
            return ""
        return ""
