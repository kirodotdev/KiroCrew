"""What a transcript-search query means: its needles, and the single-text match rule.

Pure parsing with no storage side effects. :func:`parse_search_query` turns a query into
the needles every matcher and ranker shares: whole terms, CJK characters and bigrams (or
the Chinese words a segmenter installed through :func:`register_cjk_segmenter` splits a
run into), and forge references (pull requests, merge requests, issues) expanded into
every spelling a transcript may use, plus the spellings a registered source provider
contributes through :func:`register_search_ref_resolver`. :func:`count_needle` is the one
counter they all use, :func:`snippet_needle_tiers` orders the snippet anchors and
:func:`tiered_first_hit` finds the best one, and :func:`needles_match_text` is the query
gate for one string.

:mod:`kiro_crew.history_search` re-exports every name defined here and is the import path
callers use; its ``SessionCatalogProjection`` scores sessions with these needles. This
module imports no other member of the search family.
"""

from __future__ import annotations

import itertools
import logging
import re
import threading
import time as _time
from collections import OrderedDict
from collections.abc import Callable, Iterable, Sequence
from typing import NamedTuple

from kiro_crew._sqlite_compat import is_cjk_char, is_kana_char, script_runs

SEARCH_MAX_TOKENS = 12  # distinct REQUIRED needles per query — see parse_search_query
_SEARCH_MAX_SCORING_EXTRAS = 12  # distinct scoring-only needles (CJK bigrams) per query
# Weight of a single CJK character needle that came from a longer run. Individual
# han/kana/hangul characters are extremely common (a function character like "的"
# can appear hundreds of times in one transcript), so counting them at full weight
# would let character noise drown out the bigram adjacency signal that actually
# ranks CJK results. They still gate the AND match at full strength — the weight
# only dampens their contribution to the relevance score.
_CJK_CHAR_WEIGHT = 0.25
# A short ASCII needle (one or two casefolded characters) has its CONTENT
# contribution saturated — ``log1p`` of its length-normalized hit count, in
# place of the raw count divided by the length norm. Such a needle is a
# substring match, and agent transcripts are dense in exactly the text it
# matches incidentally — ISO timestamps, account ids, commit hashes, instance
# types — so a long session can rack up thousands of hits on ``5`` without
# once being about "case 5". Raw frequency there is noise, not relevance, and
# it outgrows the ``_TITLE_BOOST`` field boost: a title containing the whole
# query scores ~60 while a 500 KB transcript scores 100-300 on stray digits
# alone. LENGTH is the whole test, digits included: incidental substring
# frequency falls by roughly 10x per extra character (``5`` matches thousands
# of times in a 500 KB transcript, ``55`` hundreds, ``555`` tens, ``4411`` a
# handful), so a run of three or more characters is specific enough that its
# frequency measures relevance and its raw hits already sit far below one
# title hit — saturating ``4411`` would cap the very term that tells a
# specific query ("timeout 50051") apart from its ordinary co-term, letting
# the co-term's linear frequency drive body-only ranking instead.
# Saturation, not a smaller multiplier, because
# the noise density varies by orders of magnitude between transcripts and no
# constant is right for all of them; saturating the NORMALIZED count (hit
# density) rather than the raw one keeps body-only matches ordered as before
# — a long substantive discussion still beats one stray mention in a short
# session — without dividing by the length norm twice. 10,000 incidental hits
# in a 45 KB transcript come to about 7 points, so no digit density can outrank
# one title hit. The needle keeps FULL strength for the AND gate (presence still
# qualifies a session) and full weight in the title (short and intentional, so
# one hit there is real evidence). CJK needles are exempt: a bigram is two
# characters by construction and is the module's intended adjacency signal, and
# lone characters already carry _CJK_CHAR_WEIGHT. Longer words keep raw
# frequency, so no existing query without a short token is re-ranked.
_SHORT_NEEDLE_MAX_CHARS = 2
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
    forms (``pr 4411`` and ``…/pull/4411`` name the same pull request), and a
    transcript may carry any of them.

    ``digit_bounded`` rejects a match that sits inside a LONGER number, on
    either side — so ``4411`` matches ``pr 4411.`` but not ``pull/44110`` and not
    the run id ``1544110293``. Only meaningful for a spelling made of digits or
    ending in one.

    ``adjacency`` marks a needle as ADJACENCY EVIDENCE (a CJK bigram) — the only
    kind the adjacency floor in :meth:`ConversationLog.search_sessions` counts.
    Scoring-only needles that are not adjacency evidence (the forge spellings
    added for ranking a bare number) therefore cannot arm that floor, which
    would otherwise turn a ranking hint into a hidden gate.

    ``saturate_body`` saturates the needle's CONTENT hit count (``log1p``)
    before weighting — title hits and the AND gate ignore it. Set only for the
    short ASCII terms :func:`parse_search_query` marks (see
    :data:`_SHORT_NEEDLE_MAX_CHARS`), whose raw body frequency is
    incidental-text noise rather than relevance.

    ``word`` marks a required needle that is one dictionary word of a pure-Han
    run (see :func:`register_cjk_segmenter`). ``verbatim`` marks the whole run
    such a split came from: scoring-only, it neither gates nor adds score, and a
    session holding it ranks ahead of every session that does not (see
    :meth:`SessionCatalogProjection._search_parsed`).
    """

    text: str
    weight: float
    required: bool
    alts: tuple[str, ...] = ()
    digit_bounded: bool = False
    adjacency: bool = False
    saturate_body: bool = False
    word: bool = False
    verbatim: bool = False


def _is_short_term(term: str) -> bool:
    """True for an ASCII term whose body frequency is noise, not relevance.

    One or two casefolded characters (``5``, ``s3``, ``pr``): each is a
    substring that incidental transcript text — timestamps, ids, hashes —
    contains far more often than prose about the thing does. Length is the
    whole test, digits included: incidental substring frequency falls by
    roughly 10x per extra character, so a run of three or more (``555``,
    ``4411``) is specific enough that its frequency measures relevance, and
    saturating it would cap the very term that tells a specific query
    ("timeout 50051") apart from its ordinary co-term. ASCII only: a
    two-character Hangul word (``한글``) is a whole word, not noise, and
    Hangul is not a CJK run here (modern Korean is space-separated, see
    :func:`_is_cjk_char`), so it arrives on this path. Callers apply it to
    non-CJK runs only.
    """
    return term.isascii() and len(term) <= _SHORT_NEEDLE_MAX_CHARS


def _saturates(spellings: Iterable[str]) -> bool:
    """``count_needle`` sums every spelling, so one short spelling makes the
    whole needle's body count incidental-noise dominated.

    A needle whose shortest spelling is three or more characters
    (hash-prefixed ``4411``, ``pull/4411``, ``4411``) keeps raw frequency.
    """
    return any(_is_short_term(spelling) for spelling in spellings)


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
        # inside a longer number (``4411`` in ``1544110293``, ``pull/4411`` in
        # ``pull/44110``). The LEFT guard applies only to a spelling that starts with
        # a digit: for a delimited spelling the character before it says nothing
        # about the number's length, and demanding a non-digit there would refuse
        # the ``#`` spelling inside ``owner/repo2#4411`` — a repository whose name ends in
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


# Words that only NAME a reference type in front of its number ("pr 4411",
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


# Punctuation a reference collects from surrounding prose ("(#12)," / "PR
# #12."). Stripped before the shapes below are tried, and only from the edges,
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
# A sigil reference: ``#12``, ``!12``, ``owner/repo#12``, ``pr#12``.
_FORGE_SIGIL_RE = re.compile(
    r"^(?:(?P<repo>[a-z0-9._-]+(?:/[a-z0-9._-]+)+)|(?P<word>pr|mr|pull|issue))?"
    r"(?P<sigil>[#!])(?P<number>\d{1,9})$"
)
# A glued word+number reference: ``pr4411``, ``pr-4411``, ``mr-12``.
_FORGE_WORD_NUM_RE = re.compile(r"^(?P<word>pr|mr|pull|issue)-?(?P<number>\d{1,9})$")


class _ForgeRef(NamedTuple):
    """A forge item (pull request / merge request / issue) named by a query.

    ``bare`` records that the QUERY spelled the number with no sigil ("pr 4411",
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
    GitHub draws pull requests and issues from ONE number sequence (a hash-prefixed ``4411``,
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
    one mention of ``pr4411`` counts twice inside a bare reference's needle. That
    only lifts the relevance score of a session that really does reference the
    item, and never gates.

    Plain digits are a spelling only for a ``bare`` reference, and that
    asymmetry is deliberate. A query that typed no sigil ("issue 42") is looking
    for the number as written, so the digits belong; a query that typed one
    ("#42", a PR URL) was explicit, and admitting bare digits there would make
    ``!12`` match every session that mentions a standalone 12 — ordinary prose
    (a count, a date, a version).

    Recall relative to the plain substring gate this replaces does not rest on
    the list being exhaustive by inspection — three shapes slipped past that
    reasoning — but on a property test that drives every shape the parser
    accepts against a transcript quoting it verbatim. Concretely: a sigil shape
    contains its own canonical spelling (``owner/repo2#4411`` ends with
    it), a path or URL shape contains a path spelling, and a glued or
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
    the two-token form ("pr 4411", "merge request 12") a reference rather than a
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
#: ``kiro_crew.dashboard.source_providers.plugins``, which imports this module.
#: Reaching UP to ask it would invert that dependency and put a dashboard
#: import on every search, including the CLI and the Discord title-only gate,
#: neither of which runs a web server. The dashboard therefore PUSHES its
#: collector down here at registration time instead.
_search_ref_resolver: Callable[[str], tuple[str, Sequence[str]] | None] | None = None

logger = logging.getLogger("kiro_crew.history_search")


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


#: Most pure-Han runs one query sends to the dictionary, and the longest run it
#: sends. A run past either bound keeps the character path. The gateway route
#: that answers mcp-core refuses a request past them.
CJK_SEGMENT_MAX_RUNS = SEARCH_MAX_TOKENS
CJK_SEGMENT_MAX_RUN_CHARS = 64
#: Runs whose words this process keeps. One query re-parses once per returned
#: row (the snippet builders), so without it every row would ask again.
_CJK_WORDS_MEMO_RUNS = 512
#: Seconds a segmenter that failed is left alone. An unreachable gateway then
#: costs one timeout a minute rather than one per query.
_CJK_SEGMENTER_BACKOFF_SECS = 60.0

CjkSegmenter = Callable[[list[str]], object]


class _CjkSegmenterSlot:
    """The installed segmenter, and when a failed one may be asked again.

    One instance, changed in place and never rebound, so the facade and this
    module always hold the same object (:mod:`kiro_crew._facade_mirror`).
    """

    __slots__ = ("segment", "retry_at")

    def __init__(self) -> None:
        self.segment: CjkSegmenter | None = None
        self.retry_at = 0.0


_cjk_slot = _CjkSegmenterSlot()
_cjk_words_memo: "OrderedDict[str, tuple[str, ...]]" = OrderedDict()
_cjk_words_lock = threading.Lock()


def register_cjk_segmenter(segment: CjkSegmenter | None) -> None:
    """Install the function that splits pure-Han runs into dictionary words.

    ``segment(runs)`` answers one word list per run, in order, or ``None``. The
    gateway installs its in-process dictionary; an mcp-core process installs a
    client that asks the gateway, so the dictionary is loaded once per host. With
    nothing installed (tests, CLI tools) every run keeps the character path.
    Installing replaces the slot and forgets every answer the old one gave.
    """
    with _cjk_words_lock:
        _cjk_slot.segment = segment
        _cjk_slot.retry_at = 0.0
        _cjk_words_memo.clear()


def reset_cjk_segmenter_for_tests() -> None:
    """Drop the installed segmenter and its answers. Test-only: module state."""
    register_cjk_segmenter(None)


def is_han_run(run: str) -> bool:
    """True for a run the Chinese dictionary splits: CJK characters, no kana."""
    return bool(run) and all(is_cjk_char(ch) and not is_kana_char(ch) for ch in run)


def _valid_words(run: str, words: object) -> tuple[str, ...] | None:
    """*words* as a tuple when it is a split of *run*, else ``None``.

    A split is a list of non-empty strings that join back to the run exactly.
    The check is what lets any segmenter, local or remote, be trusted: an answer
    that drops, adds or reorders a character cannot become a needle.
    """
    if not isinstance(words, (list, tuple)) or not words:
        return None
    if not all(isinstance(w, str) and w for w in words):
        return None
    if "".join(words) != run:
        return None
    return tuple(words)


def _cjk_words(runs: list[str]) -> dict[str, tuple[str, ...]]:
    """Dictionary words for each of *runs* the segmenter split, memoized per run.

    This is the ONLY normalizer of segmenter answers. A run is answered only by a
    valid split (:func:`_valid_words`). No segmenter, a raise, a timeout or a
    malformed answer leaves the run out, and :func:`parse_search_query` then
    keeps today's character path for it: the dictionary can only add precision,
    never take a result away by failing. A failed call is not repeated for
    :data:`_CJK_SEGMENTER_BACKOFF_SECS`.

    The lock guards the memo and the slot, never the call, so a slow segmenter
    does not hold up a query whose runs are all memoized.
    """
    out: dict[str, tuple[str, ...]] = {}
    with _cjk_words_lock:
        segment = _cjk_slot.segment
        if segment is None:
            return out
        missing: list[str] = []
        for run in runs:
            words = _cjk_words_memo.get(run)
            if words is None:
                missing.append(run)
                continue
            _cjk_words_memo.move_to_end(run)
            out[run] = words
        if not missing or _time.monotonic() < _cjk_slot.retry_at:
            return out
    try:
        answer = segment(missing)
    except Exception:
        logger.debug("cjk segmenter %r failed", segment, exc_info=True)
        answer = None
    with _cjk_words_lock:
        if _cjk_slot.segment is not segment:
            # Replaced while it ran: its answer belongs to a slot that is gone.
            return out
        if not isinstance(answer, (list, tuple)) or len(answer) != len(missing):
            _cjk_slot.retry_at = _time.monotonic() + _CJK_SEGMENTER_BACKOFF_SECS
            return out
        for run, words in zip(missing, answer):
            valid = _valid_words(run, words)
            if valid is None:
                logger.debug("cjk segmenter %r answered %r for %r", segment, words, run)
                continue
            _cjk_words_memo[run] = valid
            out[run] = valid
        while len(_cjk_words_memo) > _CJK_WORDS_MEMO_RUNS:
            _cjk_words_memo.popitem(last=False)
    return out


def parse_search_query(query: str) -> tuple[list[SearchNeedle], str, bool]:
    """Return ``(needles, phrase, adjacency_floor)`` for a search *query*, casefolded.

    ``phrase`` is the whole normalized query. ``needles`` are derived from the
    DISTINCT whitespace-separated terms in first-seen order; every caller that
    matches (:meth:`ConversationLog.search_sessions`) or locates
    (:func:`snippet_needles` for the snippet builders) query text derives from
    this one parse so the halves of a query cannot drift apart.

    Non-CJK terms become one required, weight-1.0 needle each — the classic
    substring-AND behavior (``"cont"`` hits ``"contention"``). A term of one or
    two characters additionally carries
    ``saturate_body=True``: it still gates and title-scores at full strength,
    but its content frequency is saturated (``log1p``) because such a
    substring matches incidental transcript text (timestamps, ids, hashes)
    far more often than it marks relevance; a run of three or more
    characters, digits included (``4411``), keeps raw frequency (see
    :data:`_SHORT_NEEDLE_MAX_CHARS`). A run of CJK
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

    When a segmenter is installed (:func:`register_cjk_segmenter`), a pure-Han
    run of 2..:data:`CJK_SEGMENT_MAX_RUN_CHARS` characters is split into
    dictionary words instead. Each word is a REQUIRED needle marked ``word``, the
    way an English word is: words may sit apart, but a word's own characters must
    be adjacent. A one-character word carries :data:`_CJK_CHAR_WEIGHT`, like a
    lone character today. A run split into more than one word adds itself as a
    ``verbatim`` scoring needle, so a session holding the run as typed ranks
    first. The run keeps the character path when no segmenter answers it, when it
    holds kana (the dictionary is Chinese-only), or when every word is a single
    character (the split then adds nothing to the character gate).

    A term that names a FORGE ITEM — a pull request, merge request or issue —
    becomes ONE required needle carrying every spelling of that item instead of
    the literal term (see :func:`_parse_forge_ref`). A hash-prefixed ``4411``, ``pr 4411``,
    ``pull/4411``, a full PR URL and ``owner/repo#4411`` therefore all find the
    same sessions, whichever form each transcript happens to use, and the
    spellings are digit-bounded so ``pull/4411`` never matches ``pull/44110``. A naming
    word that introduces a number ("pr 4411") is dropped from the gate: it is
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
    long spaceless queries this parse exists to serve. The floor is also off
    when a required dictionary word of two or more characters survives the
    cut: every session the AND gate admits holds that word, which is adjacency
    already, so a kana run's bigrams beside it do not gate. Looser can admit extra
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
    verbatim: dict[str, SearchNeedle] = {}
    # One batched segmenter call for every run the loop below may split.
    cjk_chunks = (chunk for part in parts for chunk, is_cjk in _script_runs(part) if is_cjk)
    han_runs = [
        run
        for run in dict.fromkeys(cjk_chunks)
        if 1 < len(run) <= CJK_SEGMENT_MAX_RUN_CHARS and is_han_run(run)
    ][:CJK_SEGMENT_MAX_RUNS]
    cjk_words = _cjk_words(han_runs) if han_runs else {}
    forge_budget = _SEARCH_MAX_FORGE_REFS
    # One ledger keyed by the item's canonical spelling, so a slot is charged per
    # ITEM rather than per needle. "pr4411 4411" names one pull request twice — as
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
                    required.setdefault(
                        canonical,
                        SearchNeedle(
                            canonical,
                            1.0,
                            True,
                            alts,
                            True,
                            saturate_body=_saturates((canonical, *alts)),
                        ),
                    )
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
                        alts = (*seen.alts, ref.number)
                        required[canonical] = seen._replace(
                            alts=alts,
                            saturate_body=_saturates((seen.text, *alts)),
                        )
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
            required.setdefault(
                canonical,
                SearchNeedle(
                    canonical,
                    1.0,
                    True,
                    alts,
                    True,
                    saturate_body=_saturates((canonical, *alts)),
                ),
            )
            if ref.repo:
                # Ranking only: the repo slug appears in a URL mention but not in
                # a prose "pr 4411" one, so requiring it would hide real hits. It
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
                    spellings[0],
                    _FORGE_REF_WEIGHT,
                    False,
                    spellings[1:],
                    True,
                    saturate_body=_saturates(spellings),
                )
        for run, is_cjk in _script_runs(part):
            if not is_cjk:
                # Short ASCII terms gate and title-score at full strength but
                # count their body hits saturated — see _SHORT_NEEDLE_MAX_CHARS
                # for why raw frequency there is noise.
                needle = SearchNeedle(run, 1.0, True, saturate_body=_is_short_term(run))
                required.setdefault(run, needle)
                continue
            if len(run) == 1:
                required.setdefault(run, SearchNeedle(run, 1.0, True))
                continue
            words = cjk_words.get(run)
            if words is not None and any(len(word) > 1 for word in words):
                for word in words:
                    weight = 1.0 if len(word) > 1 else _CJK_CHAR_WEIGHT
                    required.setdefault(word, SearchNeedle(word, weight, True, word=True))
                if len(words) > 1:
                    verbatim.setdefault(run, SearchNeedle(run, 0.0, False, verbatim=True))
                continue
            for ch in run:
                required.setdefault(ch, SearchNeedle(ch, _CJK_CHAR_WEIGHT, True))
            for i in range(len(run) - 1):
                bigram = run[i : i + 2]
                extras.setdefault(bigram, SearchNeedle(bigram, 1.0, False, adjacency=True))
    # A spelling that is already REQUIRED must not also score as a ranking hint:
    # a query naming the same item twice — the bare number and then its GitHub
    # sigil form — would count its hits twice over. The by-key sweep catches the
    # SAME-FAMILY repeat, where the hint's own key is the required spelling.
    for text in [t for t in ranking if t in required]:
        del ranking[text]
    # A CROSS-FAMILY repeat ("4411 !4411") slips past that sweep: the sigil
    # gates the GitLab spelling while the bare number's hint stays keyed on the
    # GitHub form, so the required spelling survives inside the hint's ALTS and
    # one mention would be scored by the required needle AND the hint. Required
    # needles carry alts of their own (count_needle scores every spelling), so
    # the purge covers those too, not only the keys. A hint whose every
    # spelling is already required contributes nothing and is dropped whole —
    # built-in spellings alone cannot get there (the hint's canonical form is
    # only ever a required KEY, which the by-key sweep already handled), but a
    # provider's alts can blanket the rest. A hint that keeps any spelling
    # still ranks on what survives.
    required_spellings = {s for n in required.values() for s in (n.text, *n.alts)}
    for key, needle in list(ranking.items()):
        spellings = tuple(s for s in (needle.text, *needle.alts) if s not in required_spellings)
        if not spellings:
            del ranking[key]
        elif spellings != (needle.text, *needle.alts):
            ranking[key] = needle._replace(text=spellings[0], alts=spellings[1:])
    needles = list(required.values())[:SEARCH_MAX_TOKENS]
    needles.extend(list(extras.values())[:_SEARCH_MAX_SCORING_EXTRAS])
    needles.extend(list(verbatim.values())[:_SEARCH_MAX_SCORING_EXTRAS])
    needles.extend(ranking.values())
    # Bigrams arm the floor; a verbatim run is not adjacency evidence, and
    # counting one here would arm a floor that no session could meet. A REQUIRED
    # word of 2+ characters that survived the cut already proves adjacency in
    # every session the AND gate admits, so it meets the floor: arming it on a
    # kana run's bigrams alone would drop a session holding that word.
    adjacency_floor = 0 < len(extras) <= _SEARCH_MAX_SCORING_EXTRAS and not any(
        n.word and len(n.text) > 1 for n in needles
    )
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

    This is :func:`snippet_needle_tiers` flattened, tier by tier.
    """
    return [text for tier in snippet_needle_tiers(query) for text in tier]


def snippet_needle_tiers(query: str) -> list[list[str]]:
    """*query*'s snippet needles grouped into tiers, best tier first.

    The builders take the FIRST message holding any needle of the best tier that
    any message holds. Within a tier the first message wins, as it always has;
    across tiers a later message holding a better needle wins. Returns ``[]`` for
    a whitespace-only query.

    A query with no dictionary words is ONE tier, in :func:`snippet_needles`
    order, so its snippet is exactly what the first matching message gives.
    A query with dictionary words has three: the phrase and each verbatim run,
    then the full-weight needles (words of two or more characters, whole terms),
    then the down-weighted ones (one-character words, lone characters). That is
    what keeps a lone ``普`` in an early ``普通`` from beating ``吉普力`` later on.
    """
    needles, phrase, _ = parse_search_query(query)
    if not needles:
        return []
    return _snippet_tiers(needles, phrase)


def _snippet_tiers(needles: Sequence[SearchNeedle], phrase: str) -> list[list[str]]:
    """:func:`snippet_needle_tiers` from an already-parsed query."""
    # Stable sort on weight only: within a weight class the parse's insertion
    # order (required terms first-seen, then bigrams) is the display order.
    ordered = sorted((n for n in needles if not n.verbatim), key=lambda n: -n.weight)

    def spellings(group: Iterable[SearchNeedle]) -> list[str]:
        return [text for n in group for text in (n.text, *n.alts)]

    if any(n.word or n.verbatim for n in needles):
        tiers = [
            [phrase, *(n.text for n in needles if n.verbatim)],
            spellings(n for n in ordered if n.weight >= 1.0),
            spellings(n for n in ordered if n.weight < 1.0),
        ]
    else:
        tiers = [[phrase, *spellings(ordered)]]
    seen: set[str] = set()
    out: list[list[str]] = []
    for tier in tiers:
        kept = []
        for text in tier:
            if text not in seen:
                seen.add(text)
                kept.append(text)
        if kept:
            out.append(kept)
    return out


def tiered_first_hit(
    texts: Iterable[str], tiers: Sequence[Sequence[str]]
) -> tuple[str, int, str] | None:
    """``(text, position, needle)`` of the best snippet anchor in *texts*, or ``None``.

    The anchor is the first text holding a needle of the best tier any text
    holds; within that text the tier's needles are tried in order, as the flat
    list always was. Positions are into ``text.casefold()``. Reading stops at
    the first tier-0 hit, so a one-tier query reads exactly as far as its first
    match. An ``OSError`` while reading ends the walk with the best hit so far.
    """
    best: tuple[int, str, int, str] | None = None
    try:
        for text in texts:
            folded = text.casefold()
            for rank in range(best[0] if best is not None else len(tiers)):
                hit = next(
                    ((pos, needle) for needle in tiers[rank] if (pos := folded.find(needle)) >= 0),
                    None,
                )
                if hit is not None:
                    best = (rank, text, *hit)
                    break
            if best is not None and best[0] == 0:
                break
    except OSError:
        pass
    return None if best is None else best[1:]


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
    "no adjacency anywhere"), and when a required dictionary word of two or
    more characters already proves it. *folded_text* must already be casefolded
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


#: The query prefix that scopes a search to session titles (see :func:`split_title_scope`).
_TITLE_SCOPE_PREFIX = "title:"


class TitleScope(NamedTuple):
    """A query's leading ``title:`` term, split off by :func:`split_title_scope`."""

    #: The title term, casefolded with its whitespace collapsed; "" when the prefix
    #: names nothing yet (``title:`` typed on its own).
    term: str
    #: True for ``title:"..."``: the term must appear in the title as typed.
    quoted: bool
    #: The query text after the term, searched as usual (title or content).
    rest: str

    def title_filter(self) -> Callable[[str], bool]:
        """A predicate over one session title, parsed once for the whole scan.

        An unquoted term goes through :func:`parse_search_query` and
        :func:`needles_match_text`, the one-string form of the content gate, so
        a forge reference or a CJK term matches a title the way the same term
        matches a title in a plain search. A quoted term is an exact substring of
        the title, compared with whitespace collapsed on both sides.
        """
        term = self.term
        if not term:
            return lambda _title: False
        if self.quoted:
            return lambda title: term in " ".join(title.split()).casefold()
        needles, _phrase, adjacency_floor = parse_search_query(term)
        return lambda title: needles_match_text(needles, title.casefold(), adjacency_floor)


def split_title_scope(query: str) -> TitleScope | None:
    """Split a leading ``title:`` term off *query*, or None when it has none.

    The prefix is matched case-insensitively at the start of the query only, so
    ``title:`` later in a query stays ordinary search text. Its term is the
    next whitespace-delimited token (``title:parity``), or a double-quoted
    phrase (``title:"wave plan"``) whose closing quote may still be missing
    while the user types. Whitespace after the colon is skipped, so
    ``title: parity`` reads the same. Whatever follows the term is returned as
    ``rest`` for the normal search (``title:parity deploy``).
    """
    text = query.lstrip()
    if text[: len(_TITLE_SCOPE_PREFIX)].casefold() != _TITLE_SCOPE_PREFIX:
        return None
    after = text[len(_TITLE_SCOPE_PREFIX) :].lstrip()
    if after.startswith('"'):
        inner, _quote, rest = after[1:].partition('"')
        return TitleScope(" ".join(inner.split()).casefold(), True, rest.strip())
    parts = after.split(None, 1)
    term = parts[0].casefold() if parts else ""
    return TitleScope(term, False, parts[1].strip() if len(parts) > 1 else "")
