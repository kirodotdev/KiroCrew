"""Shared constants used across cli and gateway modules."""

from __future__ import annotations

import os
import re
from collections.abc import Iterator

# Positive-identity marker injected into the environment of every subprocess
# tree KiroCrew spawns (the ACP provider, MCP probes, gateway pool backends).
# Children inherit the environment, so marking the provider process
# transitively marks every MCP server it launches. The untracked-orphan sweep
# (``session_pid.py``) reads it back from ``/proc/<pid>/environ`` to positively
# identify escaped MCP launcher processes whose *cmdline* carries no KiroCrew
# fingerprint (e.g. ``npx @playwright/mcp`` -> node) without ever risking a
# kill of a user's own identically-named processes. Constant by design: it must
# never vary per session/agent, both so the check is a simple presence test and
# so injecting it into MCP-gateway backend env cannot split pooled-backend
# identity (PoolKey hashes env).
KIROCREW_SPAWNED_ENV = "KIROCREW_SPAWNED"
KIROCREW_SPAWNED_VALUE = "1"

# Canonical truthy set for boolean environment variables (KIROCREW_NO_JAIL,
# KIROCREW_DEV_MODE, …).  Use ``env_flag_enabled`` rather than ``bool(os.environ
# .get(...))`` — a bare bool() treats ``"0"``/``"false"`` as truthy, which for a
# security toggle (e.g. KIROCREW_NO_JAIL) is a silent-bypass footgun.
ENV_TRUTHY = frozenset({"1", "true", "yes", "on"})


# Minimum supported Node.js MAJOR version for every Python-side check
# (``kirocrew doctor``, the frontend-build probe in ``cli.py``, the TUI
# launcher in ``cli_chat.py``). Single source of truth so doctor and chat can
# never disagree about the floor. 22 is the oldest non-EOL line the frontend
# bundler supports (``ensure-node.sh`` enforces the finer-grained 22.12 floor;
# ``.nvmrc`` pins the recommended 24 LTS).
MIN_NODE_MAJOR = 22


def env_flag_enabled(name: str) -> bool:
    """Return True iff env var *name* is set to a truthy value (case/space-insensitive)."""
    return os.environ.get(name, "").strip().lower() in ENV_TRUTHY


DATA_WARNING = (
    "⚠️  Do not enter sensitive, secret, or regulated data into KiroCrew.\n"
    "   Treat anything you send as potentially logged or processed by the\n"
    "   configured model provider."
)

# Outer wall-clock cap on a single ``_run_chat`` invocation (any dispatch site:
# primary user turn, queue-drain, cron injection, subagent injection, Slack first
# turn). Sized to match the inner ACP ``_DEFAULT_PROMPT_TIMEOUT`` (14400s) in
# ``acp/client.py`` so the dashboard layer doesn't bound below the transport.
# Four hours is the longest single turn the shipped budgets can legitimately
# produce (the task runner's 90-minute test command plus a fix and a re-run, or a
# blocking subagent wave at its 2h wait cap plus synthesis); work that outlives
# it belongs to the loop mechanisms, which end the turn between cycles.
# Wedged-session detection is handled by ``_STALE_TURN_TIMEOUT`` (90s, also in
# ``acp/client.py``); this cap is the upper safety ceiling for genuinely runaway
# work, not a "this turn took too long" guard.
CHAT_TURN_TIMEOUT = 14400.0

# How long the dashboard chat path parks a turn waiting for a human to answer a
# tool-approval prompt, when config is unavailable (tests, early bootstrap).
# Deliberately far below ``CHAT_TURN_TIMEOUT``: a window at or above the turn
# ceiling can never fire, because the turn is cut first and reports itself as a
# turn timeout, so the real cause (nobody approved) is never named. It also has
# to leave the turn enough time to act on a late answer — an approval granted at
# the ceiling buys a turn that is already over. ``agent.tool_approval_timeout_secs``
# overrides it and is clamped below the turn ceiling at load time.
TOOL_APPROVAL_TIMEOUT = 600.0

# How long any caller waits for a compaction to report completed/failed —
# the default of ``LLMProvider.wait_for_compaction`` and the cap on the
# automatic context-threshold compaction in ``session.py``. Manual (/compact,
# !compact) and automatic compaction deliberately share this single budget:
# the operation is identical, so a shorter manual budget only reports
# "timed out" on work that is still running and subsequently succeeds.
COMPACT_WAIT_TIMEOUT_SECS = 300.0

# Wall-clock ceiling on one subagent execution: the default of
# ``agent.subagent_timeout_secs`` and the fallback every consumer falls back to
# when config is unavailable or the key is 0. Owned here rather than in
# ``config/sections.py`` because three unrelated layers need the same number
# without importing the config tree: the manager's ``asyncio.wait_for``, the
# reaper's force-kill deadline, and the MCP gateway's hard-wedge ceiling, which
# has to sit ABOVE it or a blocking ``spawn_sub_agents`` awaiting a legitimately
# long subagent is recycled out from under its caller. Sized for work a
# subagent is actually given (a full test suite, a large refactor, a wide
# investigation); the reaper still force-kills at the deadline.
SUBAGENT_TIMEOUT_SECS = 10800

# Load-time clamp for ``agent.subagent_timeout_secs``. Same reason as the other
# resource knobs in ``_SECURITY_BOUNDED_FIELDS``: the value governs how long one
# subagent may hold a concurrency slot, so an inflated on-disk value (a direct
# ``config.json`` edit by any same-uid process, including a prompt-injected
# agent) is a denial-of-service vector rather than a preference. The max matches
# ``CHAT_TURN_TIMEOUT_MAX``, since a subagent outliving the longest legal chat
# turn cannot be awaited by anything; the min keeps the backstop from being set
# so low it cuts ordinary work.
SUBAGENT_TIMEOUT_MIN = 60
SUBAGENT_TIMEOUT_MAX = 86400


# ── Canonical "[OPTIONS: a | b | c]" trailer parsers ────────────────────────
# The agent emits a trailing ``[OPTIONS: choice1 | choice2 | ...]`` marker that
# every surface renders as tappable choices. Two variants exist because the
# surfaces scan differently, but their GRAMMAR must stay identical — so both are
# defined here ONCE and imported everywhere: a hand-mirrored copy risks a
# one-character slip that flips the flag semantics or reintroduces the ReDoS
# class below on a single surface.
#
# Body: a TEMPERED greedy repetition. No alternative in it may consume a ``[``
# that begins a fresh ``[OPTIONS:`` — both bracket forms carry that guard. This
# matters for ReDoS (py/polynomial-redos): a plain greedy ``.*`` body can itself
# consume a ``[`` that also starts the outer ``[OPTIONS:`` literal, so over
# untrusted text with many ``[OPTIONS:`` prefixes ``search()``/``findall()``
# re-explore the body from each position — polynomial backtracking. The tempered
# body is unambiguous (linear) while still capturing an inner ``[`` inside an
# option ("Fix [x] logging", "a[1]"). A CLOSER is admitted CONDITIONALLY, not
# freely: only where an earlier ``[`` in the same label matches it or the label
# list continues after it (see :data:`_MARKER_LABEL_CONTINUES` for why
# an unconditional ``]`` made the body run past the marker and delete prose).
# This parser runs over untrusted LLM/relayed text before Slack, the dashboard,
# Discord, Telegram, and WeCom render it.
#
# LINE (``re.MULTILINE``, ``$`` anchor) — for Slack/dashboard, where the marker
# ends a LINE (not necessarily the whole message). The negated class EXCLUDES
# ``\n`` (``[^[\n]``): in Python ``re`` a negated class matches ``\n`` regardless
# of DOTALL, so ``[^[]`` here would silently widen the single-line body to span
# lines (deleting/splitting a multi-line span the old single-line ``.*`` never
# matched). Trailing class is ``[ \t]`` (NOT ``\s``, which under MULTILINE would
# also match ``\n``).
#
# OPTIONAL MARKDOWN-LINK CLOSE ``(?:\(...\))?`` after the ``]``: models sometimes
# append a stray ``(OPTIONS)`` (or any ``(...)``) right after the marker, e.g.
# ``[OPTIONS: A | B | C](OPTIONS)``. That does TWO bad things at once: the extra
# text after ``]`` breaks the end anchor so the marker leaks unparsed, AND
# ``[label](url)`` is valid Markdown so the dashboard renders the whole thing as a
# clickable link instead of buttons. Absorbing a single tightly-attached ``(...)``
# here (it stays OUTSIDE the captured label group, so choices are unaffected)
# makes the parser resilient to that tic. The ``(`` must follow the ``]`` with no
# gap, so genuine trailing prose (``] and then...``) or a spaced note (``] (note)``)
# still fails the anchor and is left intact — the deliberate "trailing note on the
# same line" behaviour is preserved. The inner class is ``[^\s()]`` (NOT ``[^)\n]``)
# so it shares NO character with the trailing ``[ \t]*`` — that keeps the added group
# unambiguous and avoids a polynomial-ReDoS (``py/polynomial-redos``) backtracking
# path over ``[OPTIONS:`` + a long whitespace run. The real tic (``(OPTIONS)``, a
# bare ``(url)``) contains no whitespace or nested parens, so nothing is lost.
#: Closing brackets accepted on a protocol marker. ASCII ``]`` is the only form
#: the prompt ever specifies, but a model intermittently substitutes a fullwidth
#: or CJK lookalike — U+3011 ``】`` is the observed one; U+FF3D ``］`` and U+3015
#: ``〕`` are the same class of slip. A single wrong codepoint otherwise breaks
#: the end anchor, so the whole marker leaks into the visible message as literal
#: text and the turn silently loses its follow-up pills. Label content is
#: unaffected either way, so accepting the lookalike costs nothing.
#:
#: ONE definition, shared by both regexes below. Deliberately NOT used by
#: :func:`split_trailing_protocol_suffix`'s unfinished-marker check, which stays
#: ASCII-only on purpose -- see the comment there. That asymmetry is the point:
#: completeness is decided by the trailer regex, not by whether some closer
#: character happens to appear in the tail.
#:
#: ReDoS profile is the same as a bare literal ``\]``. The class shares
#: no character with the trailing ``[ \t]*`` / ``\s*``, and the body excludes it
#: from its negated class and readmits it in exactly TWO places, both of which a
#: widening of this constant has to be re-audited against: as the final atom of
#: :data:`_MARKER_LABEL_PAIR`, and via :data:`_MARKER_LABEL_CONTINUES`. Those two
#: are what the disjointness argument is about (see :data:`_MARKER_BODY_LINE`),
#: so the pair form -- which is where the deciding lookahead lives -- is the one
#: NOT to skip. Both readmit all four codepoints at once, which is why adding
#: these three introduces no ambiguity that ASCII ``]`` did not already have.
MARKER_CLOSERS = "]\u3011\uff3d\u3015"
_MARKER_CLOSE_CLASS = "[" + re.escape(MARKER_CLOSERS) + "]"

#: Markdown WRAPPER characters tolerated around a complete marker line.
#: A model sometimes wraps the whole marker in inline code or emphasis --
#: ``\`[OPTIONS: A | B]\``` or ``**[OPTIONS: A | B]**``. The wrapper character
#: lands AFTER the closer, breaks the end anchor, and the marker leaks into the
#: visible message as literal text while the turn silently loses its pills --
#: the same class of model tic as the stray ``](OPTIONS)`` suffix the grammar
#: already absorbs. Scope is deliberately tight so real prose never matches:
#: a LEADING wrapper is accepted only at line start (after optional indent), so
#: emphasis belonging to preceding prose (``**Choose:** [OPTIONS: ...]``) is
#: never eaten, and a TRAILING wrapper only when the marker itself OPENED one:
#: the ``(?(lwrap)...)`` conditional arms only when the ``lwrap`` group
#: captured a nonempty line-leading run. This is the invariant that makes every
#: reviewed corruption shape unreachable at once (mid-line code span
#: ``\`Use [OPTIONS: A | B]\```, a streaming frame's stray run, a MULTILINE
#: emphasis closer ``**Choose one\n[OPTIONS: A | B]**``): a run at line start
#: can only OPEN emphasis under CommonMark flanking rules (preceded by a
#: newline, it is not right-flanking), while a run after the closer can only
#: CLOSE something -- and if the marker did not open it, it belongs to the
#: enclosing prose and must survive the strip. A bare or mid-line marker keeps
#: the pre-widening grammar exactly. Leading-only stays absorbed (nothing
#: follows the closer, so nothing can be stolen); trailing-only does not
#: match and renders literally, as it did before the widening. Runs are capped
#: at 3 (``***`` is the longest CommonMark emphasis run; 4+ is not a wrapper).
#:
#: ReDoS profile unchanged: the class shares no character with the trailing
#: ``[ \t]*`` / ``\s*`` or the indent class, and both wrapper positions are
#: anchored by the required ``[OPTIONS:`` literal, so no new ambiguity exists.
MARKER_WRAPPERS = "`*_"
_MARKER_WRAP_CLASS = "[" + re.escape(MARKER_WRAPPERS) + "]"

#: A closer may stay INSIDE a label only where it CONTINUES the label list
#: A label may legitimately carry a closer -- ``[OPTIONS: Alpha ] |
#: Bravo ]]`` is a supported shape -- so the body has to admit one. Admitting it
#: UNCONDITIONALLY (the old ``[^[\n]``, which includes ``]``) made the body run to
#: the LAST closer in range instead of the first plausible one, so an ordinary
#: final line that mentions a bracket after the marker matched across BOTH:
#:
#:     Use [OPTIONS: A | B] then check arr[0]
#:
#: matched whole, and since every consumer removes the whole match -- ``slack.
#: format`` and ``messaging.renderer`` cut the visible text at ``match.start()``,
#: and ``whatsapp.turn_renderer`` PERSISTS the cut turn -- the sentence vanished
#: from the message and came back as a pill label. Under TRAILER (``DOTALL``) the
#: body crossed blank lines too, so the whole final paragraph went with it.
#:
#: This is the SAME discriminator the streaming probe already applies
#: (``CONTINUES_LABELS_RE`` in ``website/src/app-sdk/protocol/optionMarker.ts``)
#: to decide whether an arriving closer ended the marker, so the regex and the
#: probe now answer that question the same way instead of two different ways.
#:
#: Continuation ALONE is too strict, though: ``[OPTIONS: Fix [x] logging |
#: Skip]`` is a first-class supported shape (pinned by
#: ``test_options_buttons.py`` and ``test_parse_options.py``, whose comments say
#: so outright), and there the closer is followed by an ordinary word. So the
#: body admits a closer under EITHER of two conditions -- it is MATCHED by a
#: ``[`` earlier in the same label (:data:`_MARKER_LABEL_PAIR`), or the list
#: CONTINUES after it (:data:`_MARKER_LABEL_CONTINUES`). Neither test alone
#: separates the three shapes; the union does:
#:
#:     [OPTIONS: Fix [x] logging | Skip]   matched pair      -> parses
#:     [OPTIONS: Alpha ] | Bravo ]]        list continues    -> parses
#:     Use [OPTIONS: A | B] then check arr[0]   neither      -> declined
#:
#: The two alternatives are made disjoint by what FOLLOWS the closer -- the pair
#: form requires that its closer NOT be followed by a separator or another
#: closer, which is exactly when the continuation form applies. So no span of
#: input ever has two parses, which is what keeps the body linear despite two
#: bracket alternatives (see :data:`_MARKER_BODY_LINE`).
#:
#: RESIDUAL COST. Every shape the union gives up is a closer that satisfies
#: NEITHER half and has ordinary words after it, so at that closer the input is
#: genuinely indistinguishable from "marker ended, prose followed on the same
#: line". There is more than one way to be that closer, and all of them parsed
#: on the old body:
#:
#:     [OPTIONS: Fix ]x logging | Skip]            unmatched -- no ``[`` at all
#:     [OPTIONS: Fix list[dict[str, Any]] now | S] nesting deeper than one level
#:     [OPTIONS: 【重要】修复 | 跳过】               a lookalike PAIR: ``【`` is not
#:                                                 an opener, only ``[`` is
#:     [OPTIONS: Fix [multi\nline] now | Skip]     TRAILER only -- the pair
#:                                                 interior excludes ``\n`` even
#:                                                 under DOTALL
#:
#: All four fail toward a VISIBLE marker, not toward deleted prose, and that
#: asymmetry is what makes them affordable: the user sees the marker they were
#: already seeing for the broken shapes, and nothing is removed from the
#: message. Making them parse means matching brackets to arbitrary depth and
#: over an opener set this grammar does not have, which a regex is the wrong
#: tool for; the cost is bounded instead by the direction it fails in.
#:
#: A declined marker leaves the text intact only because every partial-cut gate
#: downstream tests for a closer over :data:`MARKER_CLOSERS` rather than ASCII
#: ``]`` -- see the gate in ``messaging.renderer.split_options_trailer``, which
#: this rule is what made load-bearing.
#:
#: NOT reachable by this rule: the separator-tail form (``Done. [OPTIONS: Merge |
#: Wait], details in CHANGELOG[1]``). ``], `` DOES continue the list, by the very
#: rule that makes ``[OPTIONS: Alpha ], Bravo]`` legal, so no guard applied at the
#: INTERNAL closer can tell them apart. What decides that shape is the terminator
#: gate on the bare opener (see :data:`_MARKER_BARE_OPENER_GATE_LINE`), which
#: reaches it from the other end -- the ``[`` of ``CHANGELOG[1]`` is the opener
#: whose partner would end the marker, so the line is declined and left whole.
_MARKER_LABEL_CONTINUES = rf"(?=[ \t]*[|,]|{_MARKER_CLOSE_CLASS})"

#: A closer MATCHED by a ``[`` earlier in the same label. One level deep, and its
#: interior excludes ``[`` and EVERY closer (not just ASCII ``]``, so a lookalike
#: cannot be swallowed into the interior and escape the rule), which makes its
#: match from any given ``[`` unique. The trailing negative lookahead is what
#: makes this disjoint from :data:`_MARKER_LABEL_CONTINUES` rather than an
#: alternative spelling of it.
#:
#: The opening ``\[`` carries the SAME ``(?!OPTIONS:)`` guard as the bare-``[``
#: alternative, and for the same reason: without it this form is the one place
#: the union rule is LOOSER than the body it replaced, because it can open on a
#: nested head and pair it with that head's own closer. ``Note [OPTIONS: see
#: [OPTIONS: x] below | Skip]`` then matches from the OUTER head -- where the
#: old body matched nothing at all -- and renders a pill whose label is a raw
#: protocol marker, echoed back as the user's reply when tapped. The guard
#: restores "no bracket form may consume a ``[`` that begins a fresh
#: ``[OPTIONS:``" as an absolute property of the body rather than one that holds
#: only for sibling heads.
_MARKER_LABEL_PAIR = (
    rf"\[(?!OPTIONS:)[^[{re.escape(MARKER_CLOSERS)}\n]*{_MARKER_CLOSE_CLASS}"
    rf"(?![ \t]*[|,]|{_MARKER_CLOSE_CLASS})"
)

#: The marker TAIL, spelled once so the patterns below and anything reasoning
#: about where a marker ends agree by construction. ``_MARKER_STRAY_TIC`` is the
#: tolerated markdown-link tic after the closer; ``_MARKER_WRAP_RUN`` is the
#: conditional half of :data:`MARKER_WRAPPERS`.
_MARKER_STRAY_TIC = r"(?:\([^\s()]*\))?"
_MARKER_WRAP_RUN = rf"{_MARKER_WRAP_CLASS}{{0,3}}"

#: Label body, spelled once per regex so LINE and TRAILER cannot drift. LINE
#: stops at a newline; TRAILER spans them (``DOTALL``, as the old ``.*`` did).
#: The one body-shaped pattern NOT derived from these is
#: :data:`_OPTIONS_TAIL_PREFIX_RE`, which is a prefix closure and has to stay
#: looser -- see the reason there before "fixing" it to match.
#:
#: ReDoS: the four alternatives are mutually exclusive at every position. The
#: two bracket forms both begin at ``[`` (and both refuse a fresh ``[OPTIONS:``)
#: but cannot consume the same span -- the pair form's lookahead and the
#: continuation form's are each other's negation -- an unmatched ``[`` is left to
#: the bare-``[`` form, and the negated class excludes both ``[`` and every
#: closer. So there is never more than one way to consume a character, and each
#: lookahead is entered only at a bracket and bounded by the run it scans.
_MARKER_BODY_LINE = (
    rf"(?:{_MARKER_LABEL_PAIR}|\[(?!OPTIONS:)"
    rf"|{_MARKER_CLOSE_CLASS}{_MARKER_LABEL_CONTINUES}"
    rf"|[^[{re.escape(MARKER_CLOSERS)}\n])*"
)
_MARKER_BODY_TRAILER = (
    rf"(?:{_MARKER_LABEL_PAIR}|\[(?!OPTIONS:)"
    rf"|{_MARKER_CLOSE_CLASS}{_MARKER_LABEL_CONTINUES}"
    rf"|[^[{re.escape(MARKER_CLOSERS)}])*"
)

# The ``labels`` group is NAMED because the ``lwrap`` conditional group
# necessarily precedes it, shifting positional numbering: consumers read
# ``group("labels")`` (and iterate with ``finditer``, since ``findall`` on a
# multi-group pattern yields tuples).
_RAW_OPTIONS_RE_LINE = re.compile(
    rf"(?:^[ \t]*(?P<lwrap>{_MARKER_WRAP_CLASS}{{1,3}}))?"
    rf"\[OPTIONS:(?P<labels>{_MARKER_BODY_LINE}){_MARKER_CLOSE_CLASS}"
    rf"{_MARKER_STRAY_TIC}(?(lwrap){_MARKER_WRAP_RUN})[ \t]*$",
    re.MULTILINE,
)

# TRAILER (``re.DOTALL``, ``\Z`` anchor) — for the Discord/Telegram/WeCom
# renderers, which match the marker only at the very END of the message and
# allow it to span newlines (the body omits ``\n`` from its negated class, as the
# old ``.*`` spanned newlines under DOTALL). Trailing ``\s*`` before ``\Z``. Carries
# the same optional markdown-link close as LINE (same ``[^\s()]`` inner class, so it
# shares no character with the trailing ``\s*`` — ReDoS-safe) so the grammar stays
# identical.
# ``re.MULTILINE`` is added ONLY so the optional leading-wrapper group can
# anchor ``^`` at the marker's own line start; the pattern has no ``$`` and
# ``\Z`` is unaffected by the flag, so nothing else changes.
_RAW_OPTIONS_RE_TRAILER = re.compile(
    rf"(?:^[ \t]*(?P<lwrap>{_MARKER_WRAP_CLASS}{{1,3}}))?"
    rf"\[OPTIONS:(?P<labels>{_MARKER_BODY_TRAILER}){_MARKER_CLOSE_CLASS}"
    rf"{_MARKER_STRAY_TIC}(?(lwrap){_MARKER_WRAP_RUN})\s*\Z",
    re.DOTALL | re.MULTILINE,
)


#: A marker's labels must have BALANCED brackets.
#:
#: The two patterns above find CANDIDATE markers; this decides which candidates are
#: markers, and it is the whole reason the raw patterns are private. An unmatched
#: opener in the labels means the closer the pattern consumed as the terminator is
#: really that opener's partner -- so the marker was never closed and the candidate
#: is refused.
#:
#: What it prevents: ``[OPTIONS: A | B then check arr[0]``, where the only closer on
#: the line belongs to ``arr[0]``. The body runs on through the prose, that ``]``
#: becomes the terminator, and since every consumer removes the whole match, the
#: line leaves the message and comes back as the pill label ``B then check arr[0``.
#:
#: WHY THE PATTERN CANNOT DO IT. Balance is not a regular language at unbounded
#: depth: a lookahead sees one nesting level, so ``list[dict[str, int]]`` defeats a
#: one-level rule and ``a[b[c[d]]]`` a two-level one. Encoding depths is a
#: treadmill, so the decision lives here and the patterns stay candidates.
#:
#: WHY THE RULE IS TOTAL -- no separator escape hatch. An earlier form accepted an
#: unmatched opener when a ``|`` followed it, on the theory that the opener was then
#: inside a label with the list continuing past it. That hatch was defeated three
#: times, most recently by a ``|`` INSIDE the unmatched bracket
#: (``[OPTIONS: A | B then inspect dict[str | int]``), and each time the shape it
#: readmitted was structurally identical to the shape it was meant to protect. The
#: hatch was the defect, not its spelling.
#:
#: THE COST, which is exactly one shape: ``[OPTIONS: Fix [x logging | Skip]`` -- a
#: label carrying an unclosed ``[`` -- is refused. Admitting it means admitting
#: ``[OPTIONS: A | B then check arr[0]`` too, since both hold one unmatched opener
#: and a closer at the end anchor, and admitting the second deletes a line of prose.
#: A dropped bracket renders the marker as visible text instead, which is the
#: direction every cost in this grammar fails in.
#:
#: An unmatched CLOSER is ignored rather than counted negative: a label may
#: legitimately carry one (``[OPTIONS: Alpha ] | Bravo ]]`` is a supported, tested
#: shape), so it says nothing about the terminator.
def _marker_labels_have_unmatched_opener(labels: str) -> bool:
    """Whether *labels* leave an opener unclosed, so the terminator is not theirs."""
    depth = 0
    for char in labels:
        if char == "[":
            depth += 1
        elif char in MARKER_CLOSERS and depth:
            depth -= 1
    return depth > 0


class _MarkerMatcher:
    """A candidate pattern plus the balance decision the pattern cannot make.

    Deliberately shaped like the compiled pattern it replaced -- ``search``,
    ``finditer``, ``sub`` and ``pattern`` are the only members anything used -- so
    every call site reads the same and none of them can opt out of the check by
    forgetting to call it. That is the point of the indirection: the raw patterns
    are private, so there is no supported way to get an unchecked match.
    """

    __slots__ = ("_pattern",)

    def __init__(self, pattern: re.Pattern[str]) -> None:
        self._pattern = pattern

    @property
    def pattern(self) -> str:
        """The candidate pattern's source, for the tests that pin its shape."""
        return self._pattern.pattern

    def finditer(self, text: str) -> Iterator[re.Match[str]]:
        """Every candidate whose terminator is its own, in order."""
        for match in self._pattern.finditer(text):
            if not _marker_labels_have_unmatched_opener(match.group("labels")):
                yield match

    def search(self, text: str) -> re.Match[str] | None:
        """The first accepted marker, or ``None``.

        A refused candidate cannot hide an accepted one inside its span: the body
        refuses a nested ``[OPTIONS:``, so no candidate ever contains another head.
        """
        return next(self.finditer(text), None)

    def sub(self, repl: str, text: str) -> str:
        """Remove every accepted marker, leaving refused candidates in the text."""
        out: list[str] = []
        cursor = 0
        for match in self.finditer(text):
            out.append(text[cursor : match.start()])
            out.append(repl)
            cursor = match.end()
        out.append(text[cursor:])
        return "".join(out)


#: The marker matchers callers use. Same four members as the patterns they wrap,
#: so the grammar and the balance decision can never be applied separately.
OPTIONS_RE_LINE = _MarkerMatcher(_RAW_OPTIONS_RE_LINE)
OPTIONS_RE_TRAILER = _MarkerMatcher(_RAW_OPTIONS_RE_TRAILER)

# CONTROL-TAG HTML COMMENTS — canonical grammar (single source of truth).
#
# Agent control tags ride in HTML comments, which the dashboard's markdown
# pipeline renders as nothing (rehype-raw emits comment nodes the react
# renderer skips). Three families exist in ``src/``:
#   * ``<!-- keep-visible -->``       — collapse-all exemption
#   * ``<!-- deliver:<route> -->``    — heartbeat routing
#   * ``<!-- plan_task_id:<id> -->``  — task-planner Apply-to-Tasks anchor
#
# ONE GRAMMAR, TAIL-ANCHORED + FENCE-GUARDED, case-insensitive, both
# recognizers (this regex and ``website/src/app-sdk/protocol/
# keepVisibleMarker.ts``): only standalone tag lines at the message tail are
# control tags, and a tail inside an UNTERMINATED fence is visible code (see
# ``_in_open_fence``). Message-tail producers: the prompt rule ("as its
# final line") and the task-planner appender (newline-prefixed). The
# heartbeat's ``deliver:`` tags are HEARTBEAT.md FILE-format suffixes on
# checklist lines, not message-tail emissions — echoed into a message body
# they are mid-body content, which the dashboard renders as nothing and this
# strip deliberately leaves alone. Position-independent stripping was tried
# and retired: rounds 5–8 each surfaced another quoted-code dialect it
# corrupted.
#
# Tag-line leading indent is ≤3 (CommonMark: 4+ spaces renders as an
# indented code block — visible content, never a control tag).
# ReDoS note: every quantifier is BOUNDED (whitespace ≤16, tag body ≤256 —
# generous for real emissions like ``<!-- deliver:dashboard -->``), so a
# failed match attempt does constant work and total matching stays linear
# even on adversarial repetition input (CodeQL py/polynomial-redos: an
# UNBOUNDED body with a failing ``-->`` suffix rescans per start position —
# quadratic). An unterminated ``<!--`` is NOT matched: swallowing to
# end-of-text on a missing ``-->`` silently deletes visible prose. A tag
# body over the bound is not a real control tag and stays visible.
_TRAILING_CONTROL_LINES_RE = re.compile(
    r"(?:(?:^|\n)[ \t]{0,3}"
    r"<!--(?:\s{0,16}keep-visible\s{0,16}|\s{0,16}(?:deliver|plan_task_id):[^>\n]{0,256})-->"
    r"[ \t]{0,16})+\s{0,16}\Z",
    re.IGNORECASE,
)


# Fence-delimiter lines (CommonMark: 3+ backticks or tildes, ≤3 leading
# spaces). Used for the open-fence parity guard below.
_FENCE_DELIM_LINE_RE = re.compile(r"^[ \t]{0,3}(`{3,}|~{3,})")

# Over-approximate fence-open CANDIDATES the exact walker cannot classify:
# a fence run preceded only by whitespace and CommonMark container-marker
# characters — list bullets (``- ```` ``), ordered-list digits/punctuation
# (``1. ```` ``), blockquote markers (``> ```` ``) — or by 4+ spaces (an
# indented code block at top level, but a REAL fence inside a list
# continuation). Classifying these correctly needs full CommonMark
# container tracking (nesting, lazy continuation, per-container indent
# budgets); each conformance round surfaced another sibling. Instead of
# deciding, the walker VETOES: a candidate seen while outside any tracked
# fence makes the message's fence structure ambiguous and the strip does
# nothing. Over-matching is safe by construction — the failure modes are
# asymmetric: wrongly stripping deletes visible fence-interior content,
# wrongly not stripping leaves an HTML comment the renderer never shows —
# so a false veto costs at most a feature-miss, never content.
# Single bounded character class then a literal run: linear, no
# backtracking (class and fence characters are disjoint).
_AMBIGUOUS_FENCE_LINE_RE = re.compile(r"^[ \t>+*\-\d.)]{0,40}(`{3,}|~{3,})")


def _in_open_fence(text: str, idx: int) -> bool:
    """True when position *idx* falls inside an UNTERMINATED code fence —
    or when the fence structure before *idx* is AMBIGUOUS.

    Walks fence-delimiter lines before *idx* with CommonMark's close rule
    (same character, run at least as long as the opener). Inside an open
    fence the renderer shows every line as literal code — including a line
    that lexes like a control tag — so the strip must not touch it.

    STRIP ONLY WHEN PROVABLY OUTSIDE: a container-prefixed or over-indented
    fence candidate (``_AMBIGUOUS_FENCE_LINE_RE``) encountered while the
    walker believes it is outside any fence may be a real opener this
    grammar cannot see, so the walk answers True — do nothing — rather
    than risk deleting fence-interior content. Inside a tracked fence the
    same line shape is literal code under every interpretation and does
    not veto, so a closed plain fence quoting container-fence examples
    still strips normally.
    """
    open_run: str | None = None
    for line in text[:idx].split("\n"):
        m = _FENCE_DELIM_LINE_RE.match(line)
        if not m:
            if open_run is None and _AMBIGUOUS_FENCE_LINE_RE.match(line):
                return True
            continue
        run = m.group(1)
        if open_run is None:
            open_run = run
        elif (
            run[0] == open_run[0]
            and len(run) >= len(open_run)
            # CommonMark 4.5: a CLOSING fence may not carry an info string —
            # only whitespace may follow the run. Inside an open fence a
            # fence-lookalike WITH trailing text (``` python) is literal
            # code content, not a closer, so the fence stays open.
            and line[m.end() :].strip() == ""
        ):
            open_run = None
    return open_run is not None


def strip_control_comments(text: str) -> str:
    """Remove trailing control-tag lines from *text* for a plain-text
    projection (preview, TTS, channel delivery).

    TAIL-ANCHORED with a FENCE-PARITY guard — the same grammar as the
    frontend recognizer (``keepVisibleMarker.ts``), case-insensitive on
    both sides: only standalone tag lines ENDING the message are control
    tags, and a tail that sits inside an UNTERMINATED fence is visible
    code, not a tag (the renderer shows it literally). Every producer
    emits at the tail — the prompt rule says "as its final line" and the
    task-planner appends a newline-prefixed tag — so nothing real is
    missed, and a tag quoted anywhere in the body (prose, inline code, any
    fence dialect) is structurally untouchable rather than guarded by a
    code-span grammar this module would have to keep re-deriving (rounds
    5–8 each found another dialect). Stacked trailing tags are all
    removed. This is the ONE backend strip implementation.
    """
    m = _TRAILING_CONTROL_LINES_RE.search(text)
    if m is None or _in_open_fence(text, m.start()):
        return text
    return text[: m.start()]


#: Prefix closures of the marker grammars, for
#: :func:`split_trailing_protocol_suffix`'s unfinished-marker probe: a tail is
#: a STILL-STREAMING marker only when every byte it holds so far could extend
#: into a complete marker. ``[OPTIONS`` must be followed by ``:`` and then a
#: PREFIX CLOSURE of :data:`OPTIONS_RE_TRAILER`'s body (DOTALL; ``[`` admitted
#: only when not opening a nested ``[OPTIONS:``) -- deliberately LOOSER than
#: that body, and the one place the "spelled once" rule in
#: :data:`_MARKER_BODY_LINE` does not apply. It has to be: a prefix of a legal
#: body need not itself be a legal body. ``[OPTIONS: A ]`` mid-stream holds a
#: closer that satisfies neither half of the closer rule YET, and becomes legal
#: the moment ``| B]`` arrives, so a probe spelled as the real body would call
#: that tail dead and publish the marker as raw text. Widening this to the
#: grammar is what ``test_options_marker_closers.py``'s
#: ``test_closer_inside_an_unfinished_label_is_still_unfinished`` forbids.
#: ``[STEERING`` follows the steer-ack
#: grammar (``messaging/driver.py``): whitespace gap, literal ``steer-``, a
#: nonempty hex/dash id, then an optional ``:`` summary -- spelled as nested
#: optionals so every cut point of the literal run is admitted, while a tail
#: that diverges from the grammar (``[OPTIONSDOC``, ``[STEERING
#: acknowledgment``, ``steer-:``) is prose and stays visible. Case-sensitive
#: on purpose: these probe the exact sentinels the detach walk locates.
_OPTIONS_TAIL_PREFIX_RE = re.compile(
    r"\[OPTIONS(?::(?:[^[]|\[(?!OPTIONS:))*)?\Z",
    re.DOTALL,
)
_STEERING_TAIL_PREFIX_RE = re.compile(
    r"\[STEERING(?:\s+(?:s(?:t(?:e(?:e(?:r(?:-(?:[0-9a-f-]+(?:\s*(?::\s*.*)?)?)?)?)?)?)?)?)?)?\Z",
    re.DOTALL,
)
_MARKER_SENTINELS = (
    ("[STEERING", _STEERING_TAIL_PREFIX_RE),
    ("[OPTIONS", _OPTIONS_TAIL_PREFIX_RE),
)


def _rightmost_unfinished_marker(text: str) -> int:
    """Start of the rightmost tail that is a strict prefix of a marker grammar.

    Occurrences are probed RIGHTMOST-FIRST so label bytes that merely contain
    a sentinel (a bare ``[OPTIONS`` without its colon is legal label content)
    cannot shadow the genuine fragment start to their left. Each probe is
    cheap: the ASCII ``]`` gate is one precomputed ``rfind`` comparison, and
    the prefix regexes are anchored at the occurrence and die on the first
    diverging byte, so an adversarial buffer repeating failing sentinels
    walks linearly. Returns ``-1`` when no admissible occurrence exists.
    """
    last_close = text.rfind("]")
    cursors = []
    for sentinel, prefix_re in _MARKER_SENTINELS:
        pos = text.rfind(sentinel)
        if pos != -1:
            cursors.append((pos, sentinel, prefix_re))
    while cursors:
        cursors.sort()
        pos, sentinel, prefix_re = cursors.pop()  # rightmost overall
        if pos <= last_close:
            # ASCII-only unfinished gate (see the closer comment in
            # ``split_trailing_protocol_suffix``): a ``]`` at/after this
            # occurrence means the tail is not still-streaming -- and every
            # remaining occurrence sits further left of that closer too.
            break
        if prefix_re.match(text, pos) is not None:
            return pos
        nxt = text.rfind(sentinel, 0, pos)
        if nxt != -1:
            cursors.append((nxt, sentinel, prefix_re))
    return -1


def _leading_wrapper_start(text: str, idx: int) -> int:
    """Start of a line-leading Markdown wrapper run abutting *idx*, else *idx*.

    Mirrors the regexes' optional leading-wrapper group (see
    :data:`MARKER_WRAPPERS`) for the STILL-STREAMING path: a wrapped marker's
    head is located at its ``[``, and without this the leading wrapper stays in
    the visible half, where a length rotation can split it from the marker it
    belongs to. The run must abut *idx*, be at most 3 characters, and carry
    only indent before it on its line -- a mid-line wrapper belongs to prose
    (the completed regex leaves it visible too) and a 4+ run is not a wrapper.
    """
    run = idx
    while run > 0 and idx - run < 3 and text[run - 1] in MARKER_WRAPPERS:
        run -= 1
    if run == idx:
        return idx
    line_start = text.rfind("\n", 0, run) + 1
    if text[line_start:run].strip(" \t") == "":
        return run
    return idx


def split_trailing_protocol_suffix(text: str) -> tuple[str, str]:
    """Detach protocol trailers before a renderer length-splits ``text``.

    A still-streaming ``[STEERING`` or ``[OPTIONS`` fragment normally breaks
    :data:`OPTIONS_RE_TRAILER`'s end-of-buffer anchor. If a complete OPTIONS
    block immediately precedes that fragment, detaching only the unfinished
    marker leaves the complete block eligible for a mid-token chunk split.
    Return the visible prefix plus the entire protocol suffix so renderers can
    keep both markers together on the surviving tail.

    An occurrence is judged against the marker GRAMMAR, never by bare
    substring location: a mid-prose mention of ``[OPTIONS`` or ``[STEERING``
    whose tail cannot extend into a complete marker stays visible, instead of
    being detached and silently dropped from the rendered cut.
    """
    suffix_start = len(text)
    idx = _rightmost_unfinished_marker(text)
    # DELIBERATELY ASCII-ONLY -- do not widen the helper's gate to
    # ``MARKER_CLOSERS``. It asks "is the tail an UNFINISHED marker?", and
    # mere PRESENCE of a closer is not completeness: a closer sitting inside
    # a still-streaming label (``[OPTIONS: Use 】 the bracket``) would read as
    # finished, the fragment would not be detached, and a length rotation
    # could split the marker so raw fragments render and the pills are lost.
    # Completeness is decided by ``OPTIONS_RE_TRAILER`` on the next line,
    # which DOES accept the lookalikes -- so a complete lookalike-closed block
    # is still pulled into the suffix. Widening there buys nothing (both paths
    # already yield the same split for a complete tail) and reintroduces that
    # bug.
    if idx != -1:
        suffix_start = _leading_wrapper_start(text, idx)

    options = OPTIONS_RE_TRAILER.search(text[:suffix_start])
    if options:
        suffix_start = options.start()

    if suffix_start == len(text):
        return text, ""
    return text[:suffix_start], text[suffix_start:]


# Wire markers opening an injected sub-agent completion turn. They live in this
# leaf module rather than beside the dashboard's other transcript prefixes so a
# CORE module can import them at module scope: `subagent.py` composes them too,
# and a core module must not import the dashboard layer at import time.
#
# The batch marker is a SIBLING of the per-agent one, not an extension of it, so
# a `startswith` written against one silently misses the other.
SUBAGENT_COMPLETION_PREFIX = "[Subagent completion event]"
SUBAGENT_BATCH_COMPLETION_PREFIX = "[Subagent batch completion event]"

# Key under a completion message's ``meta`` where the gateway stamps the
# structured header facts (outcome, tallies, chunk index, agent id) the
# dashboard card reads. Mirrors ``META_KEY`` in
# website/src/pages/chat/subagentCompletion.ts — the two are one wire contract.
# Stamping the facts here means a reword of the header PROSE below cannot
# silently break card rendering: the card reads this meta and the prose regexes
# demote to a legacy-scrollback fallback.
SUBAGENT_COMPLETION_META_KEY = "subagentCompletion"


# Windows reserved device names, lowercase stems. Windows resolves these inside
# EVERY directory, so no file OR directory may be named after one — the rule is
# part of the documented Win32 file-naming contract, not a quirk of one build,
# and it applies to any host the identifier might travel to.
#
# ONE definition on purpose. Every Kiro Crew identifier that becomes a path
# component on disk — a git branch (a loose ref FILE under `.git/refs/heads/`),
# an app name (a directory under the apps root) — has to refuse the same set,
# and two copies would drift. Callers lowercase before testing; a caller whose
# own grammar already forces lowercase can test membership directly.
#
# Only `com1`-`com9` and `lpt1`-`lpt9` are reserved: `com10` is an ordinary name.
WINDOWS_DEVICE_STEMS = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{n}" for n in range(1, 10)}
    | {f"lpt{n}" for n in range(1, 10)}
)

# AWS named-profile name shape — the SINGLE SOURCE OF TRUTH. Hand-copying the
# charset into separate compiled patterns reintroduced the missing-'+' defect
# twice, so every in-package
# validator derives from these; the two standalone artifact-deploy scripts
# (which cannot import the package) embed AWS_PROFILE_NAME_PATTERN verbatim
# under a byte-equality drift guard in test/test_aws_profile_charset.py.
#
# Semantics:
# * '+' admitted — IAM Identity Center derives "<account>+<permission-set>"
#   profile names.
# * The first char excludes '-' so a stored name is never option-shaped when it
#   later reaches a discrete ``--profile <value>`` argv element.
# * \Z anchor — '$' matches just before a trailing newline; \Z rejects it.
#   Call sites that match a raw (unstripped) value rely on this.
# * Length capped at 128 inside the pattern, matching the FieldSpec
#   ``max_len=128`` the deploy boundaries enforce.
#
# A site with a DELIBERATE semantic difference (e.g. aws_consent.py's wider
# legacy continuation charset) derives its character class from these
# fragments rather than re-spelling them. COMPOSE FROM AWS_PROFILE_FIRST_CHARS
# ONLY (it carries no literal '-', so extra chars may follow it safely, e.g.
# rf"[{AWS_PROFILE_FIRST_CHARS}@=-]"). AWS_PROFILE_CHARS ends with a literal
# '-' and is safe ONLY in terminal position — appending anything after it
# turns the trailing '-' into a RANGE (e.g. "+-@" spans 0x2B-0x40, silently
# admitting '/', ':' and ';'). test_aws_profile_charset.py pins this contract.
AWS_PROFILE_FIRST_CHARS = "A-Za-z0-9_.+"
AWS_PROFILE_CHARS = "A-Za-z0-9_.+-"
AWS_PROFILE_NAME_PATTERN = f"^[{AWS_PROFILE_FIRST_CHARS}][{AWS_PROFILE_CHARS}]{{0,127}}\\Z"
AWS_PROFILE_NAME_RE = re.compile(AWS_PROFILE_NAME_PATTERN)

SLACK_NAMESPACE = "slack"

#: Session-key namespaces owned by a messaging channel, i.e. every prefix a
#: conversation started OUTSIDE the dashboard can carry. Slack keys are
#: ``slack:<thread_ts>``; every other transport uses
#: ``{channel}:{agent}:{chatType}:{user}[:genN]`` (see
#: ``messaging.link.build_dm_session_key``), plus the ``unified:`` bucket that
#: ``dm_scope="unified"`` collapses direct DMs into.
#:
#: Deliberately excludes the non-channel namespaces that also contain a colon
#: (``dashboard:``, ``cron:``, ``hook:``, ``subagent:``, ``channel:``) — those
#: are surfaced by their own owners, not by the channel-session reconciler.
#:
#: NOTE: ``autonudge._CHANNEL_KEY_PREFIXES`` is a SEPARATE hand-kept copy. It is
#: often described as narrower; as of this writing it is not -- both hold the same
#: 11 namespaces. It answers a different question (does this key SHAPE belong to a
#: channel rather than a dashboard slot), which is why it lists namespaces nothing
#: can currently be delivered to. Deriving it from here would be sound and is
#: deliberately left out of the change that homed this roster; until then, do not
#: assume the two have diverged, and do not assume they are kept in step either.
#:
#: HOMED HERE, not in ``messaging.link``, because the roster has readers on both
#: sides of an import cycle. ``messaging.link`` is itself stdlib-only, but
#: importing anything from it executes ``messaging/__init__.py`` first, which
#: pulls in ``driver`` -> ``acp`` -> ``hooks``; a reader that ``hooks`` is already
#: mid-import for (``hooks`` -> ``webhooks`` -> ``validation``) then fails with a
#: partially-initialized ``hooks``. This module imports only ``os`` and ``re``, so
#: it can be read from anywhere. ``messaging.link`` re-exports both names, which
#: is where the rest of the codebase still reads them from.
CHANNEL_SESSION_NAMESPACES: tuple[str, ...] = (
    SLACK_NAMESPACE,
    "discord",
    "telegram",
    "whatsapp",
    "webex",
    "wecom",
    "teams",
    "weixin",
    "imessage",
    "feishu",
    "unified",
)

#: The channels a PROACTIVE send may name -- ``send_message``'s ``channel_type``
#: and its channel ``session`` values. Derived ONCE here rather than subtracted at
#: each reader: the same subtraction was spelled in three places, which is the
#: drift shape that made a Webex owner DM unreachable while the gateway leg behind
#: it already worked, one level up.
#:
#: Two members of the roster cannot be a send target:
#:
#: * ``slack`` has its own client and streaming path and is deliberately absent
#:   from ``state.channel_transports``, so the shared ladder skips it. It is
#:   spelled ``session="slack"``.
#: * ``unified`` is the session-key bucket ``dm_scope="unified"`` collapses DMs
#:   into, not a transport; no ``ChannelLink`` ever carries it as a channel type.
CHANNEL_SEND_NAMESPACES: tuple[str, ...] = tuple(
    sorted(set(CHANNEL_SESSION_NAMESPACES) - {SLACK_NAMESPACE, "unified"})
)

#: The channels an OWNER-DM may be inferred for -- ``send_message``'s channel
#: ``session`` values. A strict subset of :data:`CHANNEL_SEND_NAMESPACES`, because
#: the two ask different questions and only one of them needs an owner.
#:
#: ``channel_type`` names a conversation: the one the calling session already
#: belongs to, or an explicit ``target_id`` the agent supplies. Neither infers a
#: recipient. A channel ``session`` DOES infer one, from
#: ``configured_targets()`` via ``_owner_dm_target``, whose safety claim is that
#: the agent can only reach somebody the USER configured.
#:
#: ``weixin`` and ``wecom`` are excluded because that claim is false on both. Each
#: folds identities LEARNED from inbound traffic into ``configured_targets()`` --
#: Weixin's ``_known_users`` (``_allowed | _known_users``) and WeCom's
#: ``_warm_chats``, which under ``wecom.allow_all_users`` become the list outright
#: ("there is no configured list to draw on, so the warm peers ARE the list"). So a
#: peer who messaged the bot once can be the single available direct target, which
#: is exactly what ``_owner_dm_target`` reads as "the owner". Nothing downstream
#: catches it: both transports' ``may_send_to`` returns True unconditionally under
#: their open policy (Weixin's promise to consult ``_allowed`` alone holds only on
#: its ``allowlist`` branch), and ``resolve_configured_target`` accepts the learned
#: set too. So private agent output would reach an arbitrary peer, not the operator.
#:
#: The other seven transports draw ``configured_targets()`` from configured state
#: alone; ``test_no_owner_dm_channel_advertises_learned_identities`` is the ratchet
#: that keeps this subtraction honest rather than hand-kept, so a transport that
#: starts mixing learned identities in fails the gate instead of silently becoming
#: an owner-DM target.
#:
#: This is a per-channel CAPABILITY gap, not drift: the exclusion is derived from
#: the send roster and carries its reason, the way ``slack`` and ``unified`` do. A
#: channel graduates by distinguishing configured recipients from learned peers in
#: ``configured_targets()`` -- at which point deleting it from this subtraction is
#: the whole change.
CHANNEL_OWNER_DM_NAMESPACES: tuple[str, ...] = tuple(
    sorted(set(CHANNEL_SEND_NAMESPACES) - {"weixin", "wecom"})
)

# The product wordmark, figlet `small`. ONE definition on purpose: copy-pasting
# it into cli.py and cli_chat.py risks a rename leaving a stale product name in
# the two most-seen surfaces (bare `kirocrew`, the chat REPL). Import it; never
# re-inline it. `cloud/ui.py` keeps its own art because it renders a different
# wordmark ("Kiro Crew Cloud") with ANSI color.
BANNER = r"""
   _  ___            ___
  | |/ (_)_ _ ___   / __|_ _ _____ __ __
  | ' <| | '_/ _ \ | (__| '_/ -_) V  V /
  |_|\_\_|_| \___/  \___|_| \___|\_/\_/

  👻 Your personal AI agent
"""

# Max length of an auto-nudge loop's ``banner`` -- the SHORT transcript row shown
# in place of a long recurring instruction. Unrelated to ``BANNER`` above, which
# is the product wordmark; this is a per-loop user string.
#
# It lives here, in a leaf that imports only ``os`` and ``re``, because three
# modules need the same bound and one of them is ``validation.py``: importing it
# from ``autonudge`` pulled a service module into a validation leaf and made the
# bound's home depend on import order. Every enforcement site -- the two REST
# authorizers, the MCP tool schemas, and the store loader -- reads THIS name, so
# there is one definition and no path can drift to a different cap.
MAX_BANNER_CHARS = 500
