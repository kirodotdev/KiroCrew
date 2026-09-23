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
# Per-spawn incarnation of a tree Kiro Crew spawned as its own session leader --
# an agent runtime, and an app backend -- set on the root's environment and
# inherited by its whole tree. KIROCREW_SPAWNED says "a Kiro Crew spawned this";
# this one says WHICH spawn, so a teardown that has lost its root can still tell
# the root's own tree from a fresh spawn that took the root's recycled pid.
KIROCREW_SPAWN_INSTANCE_ENV = "KIROCREW_SPAWN_INSTANCE"

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

# Tool-call budget for long subagent work. Shared by the config default, loader,
# manager fallback and tool description; the wall-clock deadline still bounds a
# run that makes little progress or spends a long time inside one tool.
DEFAULT_SUBAGENT_MAX_TURNS = 1000

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
#: Each closer is PAIRED POSITIONALLY with an opener in :data:`MARKER_OPENERS`,
#: so ``[`` <-> ``]``, ``【`` <-> ``】``, ``［`` <-> ``］``, ``〔`` <-> ``〕``. The
#: matched-pair body form (:data:`_MARKER_LABEL_PAIR`) emits one alternative per
#: pair, and each alternative closes on ITS OWN closer only, so a ``【`` interior
#: is ended by ``】`` and never by ``]`` -- a mismatched pair (``【 ... ]``) has
#: no pair parse and falls through to the unmatched-opener refusal, exactly as a
#: bare ``[`` with a stray ``]`` does.
#:
#: ReDoS profile is the same as a bare literal ``\]``. The class shares
#: no character with the trailing ``[ \t]*`` / ``\s*``, and the body excludes
#: every closer from its negated class and readmits them in exactly TWO places,
#: both of which a widening of this constant has to be re-audited against: as the
#: closing atom of :data:`_MARKER_LABEL_PAIR` (one closer per pair) and via
#: :data:`_MARKER_LABEL_CONTINUES` (the full class). Those two are what the
#: disjointness argument is about (see :data:`_MARKER_BODY_LINE`), so the pair
#: form -- which is where the deciding lookahead lives -- is the one NOT to skip.
MARKER_CLOSERS = "]\u3011\uff3d\u3015"
_MARKER_CLOSE_CLASS = "[" + re.escape(MARKER_CLOSERS) + "]"

#: Opening brackets accepted on a protocol marker, PAIRED POSITIONALLY with
#: :data:`MARKER_CLOSERS`: ``[`` opens ``]``, ``【`` (U+3010) opens ``】``, ``［``
#: (U+FF3B) opens ``］``, ``〔`` (U+3014) opens ``〕``. The prompt only ever
#: specifies ASCII ``[``/``]``, but a model that substitutes a lookalike CLOSER
#: substitutes the lookalike OPENER with it, emitting a whole ``【 ... 】`` pair;
#: a matched-pair form that opened only on ``[`` reads that pair's closer as
#: unmatched and declines the marker, so the pills are lost. Opening on the
#: paired lookalike makes ``[OPTIONS: 【x】 | Skip]`` parse exactly as
#: ``[OPTIONS: [x] | Skip]`` does.
#:
#: The two strings MUST stay the same length and order -- :data:`_MARKER_LABEL_PAIR`
#: zips them into per-pair alternatives, so a positional edit to one requires the
#: matching edit to the other. Only ``[`` can begin a fresh ``[OPTIONS:`` head, so
#: only its alternative can widen the body past a nested head; the ``(?!OPTIONS:)``
#: guard rides every opener regardless (a no-op on the lookalikes, which cannot
#: spell the head) so the property "no bracket form consumes a nested head" holds
#: by construction rather than by which opener happens to carry the guard.
MARKER_OPENERS = "[\u3010\uff3b\u3014"
_MARKER_OPEN_CLASS = "[" + re.escape(MARKER_OPENERS) + "]"
#: Every bracket the grammar knows -- all openers and all closers -- as the
#: characters the body's NEGATED classes exclude. This is the disjointness
#: invariant in one place: a character that can START a bracket form (an opener)
#: or END one (a closer) is never also an ordinary body character, and never
#: sits inside a pair's interior. Two consequences the ReDoS argument rests on:
#: an opener with no partner is consumed by the bare-opener alternative ONLY,
#: and a pair attempt starting at any opener scans at most to the next bracket
#: before it succeeds or fails -- so a run of the same opener (an LLM
#: repeated-token degeneration) costs one failed pair attempt per character,
#: linear, exactly as a run of ASCII ``[`` always has. Excluding only ``[`` and
#: the closers here would let a lookalike opener sit inside every interior and
#: turn that run quadratic.
_MARKER_BRACKETS = re.escape(MARKER_OPENERS + MARKER_CLOSERS)

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
#: Every glyph the marker grammar gives structural meaning.
#: A glued remainder that carries any of them is not plain prose.
MARKER_STRUCTURE_CHARS: frozenset[str] = frozenset(
    MARKER_OPENERS + MARKER_CLOSERS + MARKER_WRAPPERS + "|"
)

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
#:     [OPTIONS: Fix ]x logging | Skip]            unmatched -- no opener at all
#:     [OPTIONS: Fix list[dict[str, Any]] now | S] nesting deeper than one level
#:     [OPTIONS: 见【表1] 说明 | 跳过]                a MISMATCHED pair: ``【`` pairs
#:                                                 with ``】``, never with ``]``
#:     [OPTIONS: Fix [multi\nline] now | Skip]     TRAILER only -- the pair
#:                                                 interior excludes ``\n`` even
#:                                                 under DOTALL
#:
#: All four fail toward a VISIBLE marker, not toward deleted prose, and that
#: asymmetry is what makes them affordable: the user sees the marker they were
#: already seeing for the broken shapes, and nothing is removed from the
#: message. A MATCHED lookalike pair (``[OPTIONS: 【x】 | Skip]``) is NOT in this
#: list -- it parses, because :data:`MARKER_OPENERS` pairs ``【`` with ``】``.
#: Making the remaining shapes parse means matching brackets to arbitrary depth,
#: or pairing openers with closers this grammar deliberately keeps unpaired,
#: which a regex is the wrong tool for; the cost is bounded instead by the
#: direction it fails in.
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

#: A closer MATCHED by its paired opener earlier in the same label. One level
#: deep. There is one alternative PER opener/closer pair (see
#: :data:`MARKER_OPENERS`): each opens on its own opener, its interior excludes
#: EVERY opener and EVERY closer (:data:`_MARKER_BRACKETS`, so no bracket can be
#: swallowed into the interior and escape the rule, and a run of openers costs
#: one failed attempt each), and it closes on THAT pair's closer alone. Closing on
#: the paired closer only -- not the whole closer class -- is what makes a
#: mismatched pair (``【 ... ]``) decline: no alternative pairs ``【`` with ``]``,
#: so the ``]`` reads as unmatched and the marker falls through to the
#: unmatched-opener refusal, the same outcome a bare ``[`` with a stray ``]``
#: gets. The trailing negative lookahead (the full closer class) is what makes
#: this disjoint from :data:`_MARKER_LABEL_CONTINUES` rather than an alternative
#: spelling of it.
#:
#: Every opener carries the SAME ``(?!OPTIONS:)`` guard as the bare-opener
#: alternative. It is load-bearing on the ``[`` alternative -- without it that
#: form is the one place the union rule is LOOSER than the body it replaced,
#: because it can open on a nested head and pair it with that head's own closer.
#: ``Note [OPTIONS: see [OPTIONS: x] below | Skip]`` then matches from the OUTER
#: head and renders a pill whose label is a raw protocol marker, echoed back as
#: the user's reply when tapped. The guard makes "no bracket form may consume a
#: ``[`` that begins a fresh ``[OPTIONS:``" an absolute property of the body. The
#: lookalike openers cannot spell the head, so the guard is a no-op on them; it
#: rides them anyway so the alternatives are one shape and the property does not
#: depend on which opener carries it.
_MARKER_LABEL_PAIR = "|".join(
    rf"{re.escape(_open)}(?!OPTIONS:)[^{_MARKER_BRACKETS}\n]*"
    rf"{re.escape(_close)}(?![ \t]*[|,]|{_MARKER_CLOSE_CLASS})"
    for _open, _close in zip(MARKER_OPENERS, MARKER_CLOSERS)
)

#: The marker TAIL, spelled once so the patterns below and anything reasoning
#: about where a marker ends agree by construction. ``_MARKER_STRAY_TIC`` is the
#: tolerated markdown-link tic after the closer; ``_MARKER_WRAP_RUN`` is the
#: conditional half of :data:`MARKER_WRAPPERS`.
_MARKER_STRAY_TIC = r"(?:\([^\s()]*\))?"
#: The stray tic on its own, non-optional: what one ``(...)`` of the marker's
#: tail grammar looks like. A glued remainder that BEGINS with one is a second
#: tic, a continuation of that tail, not prose.
_STRAY_TIC_HEAD_RE = re.compile(r"\([^\s()]*\)")
_MARKER_WRAP_RUN = rf"{_MARKER_WRAP_CLASS}{{0,3}}"

#: Label body, spelled once per regex so LINE and TRAILER cannot drift. LINE
#: stops at a newline; TRAILER spans them (``DOTALL``, as the old ``.*`` did).
#: The one body-shaped pattern NOT derived from these is
#: :data:`_OPTIONS_TAIL_PREFIX_RE`, which is a prefix closure and has to stay
#: looser -- see the reason there before "fixing" it to match.
#:
#: ReDoS: the alternatives are mutually exclusive at every position. Each
#: matched-pair alternative begins at its OWN opener, so pairs for different
#: brackets never start at the same character. The ``[`` pair form and the
#: bare-``[`` form both begin at ``[`` (and both refuse a fresh ``[OPTIONS:``)
#: but cannot consume the same span -- the pair form's lookahead and the
#: continuation form's are each other's negation, and an unmatched opener of
#: ANY kind (``[`` or a lookalike) is left to the bare-opener form. The negated
#: class excludes every opener and every closer (:data:`_MARKER_BRACKETS`), so
#: an opener is never also an ordinary character: exactly one of "its pair form
#: succeeds" and "the bare-opener form takes it" holds at each opener, and a
#: failed pair attempt scans at most to the next bracket. So there is never more
#: than one way to consume a character, and each lookahead is entered only at a
#: bracket and bounded by the run it scans.
_MARKER_BODY_LINE = (
    rf"(?:{_MARKER_LABEL_PAIR}|{_MARKER_OPEN_CLASS}(?!OPTIONS:)"
    rf"|{_MARKER_CLOSE_CLASS}{_MARKER_LABEL_CONTINUES}"
    rf"|[^{_MARKER_BRACKETS}\n])*"
)
_MARKER_BODY_TRAILER = (
    rf"(?:{_MARKER_LABEL_PAIR}|{_MARKER_OPEN_CLASS}(?!OPTIONS:)"
    rf"|{_MARKER_CLOSE_CLASS}{_MARKER_LABEL_CONTINUES}"
    rf"|[^{_MARKER_BRACKETS}])*"
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
#:
#: Openers are TYPED, not fungible. ``【`` pairs with ``】`` and nothing else, so a
#: closer pops only the opener it partners; a closer of another kind is treated
#: exactly like an unmatched closer -- ignored. Counting every closer against every
#: opener admitted ``[OPTIONS: A 【x] | B]``: the ``]`` after ``x`` closed the ``【``
#: on the count, the candidate parsed, and a label with a half-open lookalike pair
#: rendered as options. Under typed pairing that ``【`` is still open at the
#: terminator, which is the bare-opener shape, and the candidate declines.
def _marker_labels_have_unmatched_opener(labels: str) -> bool:
    """Whether *labels* leave an opener unclosed, so the terminator is not theirs.

    Counts every opener in :data:`MARKER_OPENERS`, not only ASCII ``[``: the
    pair forms make the lookalikes grammatically significant, so a bare ``【``
    before the terminator is the same "partner would end the marker" shape as a
    bare ``[`` and must decline the same way, or the line is cut and its prose
    deleted -- the bare-opener terminator class, reached through a lookalike.

    Pairing is by type: a closer pops the innermost opener only when it is that
    opener's partner (``MARKER_OPENERS`` and ``MARKER_CLOSERS`` are index-aligned),
    so ``【x]`` leaves the ``【`` open rather than letting an ASCII ``]`` close it.
    """
    open_stack: list[str] = []
    for char in labels:
        if char in MARKER_OPENERS:
            open_stack.append(char)
        elif char in MARKER_CLOSERS and open_stack:
            if MARKER_OPENERS.index(open_stack[-1]) == MARKER_CLOSERS.index(char):
                open_stack.pop()
    return bool(open_stack)


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


#: The GLUE candidate: a line-leading complete marker whose closer is IMMEDIATELY
#: followed by non-whitespace on the same line -- the one shape
#: ``_RAW_OPTIONS_RE_LINE`` cannot match, because that pattern anchors the closer to
#: ``[ \t]*$`` (end of line).
#:
#: Leading indentation is zero to three spaces, matching CommonMark's limit before
#: block syntax. Four-space and tab-indented lines are indented code samples, so
#: they stay byte-for-byte literal instead of becoming active option pills.
#:
#: This is the marker's own line grammar (same head, same ``_MARKER_BODY_LINE``,
#: same close class, same stray-tic and wrapper tail, spelled from the SAME
#: fragments so it cannot drift from ``_RAW_OPTIONS_RE_LINE``) with the terminal
#: ``[ \t]*$`` replaced by ``(?=\S)`` -- a lookahead requiring same-line
#: non-whitespace DIRECTLY after the marker tail (``\S`` never matches ``\n``, so a
#: marker that legitimately ends its line is NOT a candidate here).
#:
#: An opened wrapper closes on the marker before glue can begin. An unclosed
#: wrapper means the marker sits inside a span such as inline code, so the line
#: is a sample and not a candidate.
#:
#: The abut is DIRECT (no ``[ \t]*`` before the lookahead) on purpose: the bug this
#: feeds is a concatenation seam that joins two spans with NO separator
#: (``...]Anytime.``). A closer followed by a SPACE then prose (``[OPTIONS: A | B]
#: documents the syntax``) is a human/model authoring a sentence ABOUT the marker,
#: not a glued reply, and reflowing it would turn a documentation example into live
#: pills; requiring direct abut leaves that -- and an indented code remainder
#: (``[OPTIONS: A | B]    code``) -- untouched.
#:
#: The tail (stray tic + closing wrapper run) is an ATOMIC group ``(?>...)``.
#: Both fragments are optional, so without atomicity the engine backtracks INTO
#: them to satisfy the lookahead: ``[OPTIONS: A | B](OPTIONS)`` at end of line
#: gives the tic back and "finds" ``(`` as glued prose, and ``**[OPTIONS: A |
#: B]**`` gives one ``*`` back and finds the other -- each a complete, legal line
#: the LINE grammar already accepts, and reflowing it strands a visible ``(OPTIONS)``
#: or ``*`` line under the pills. Atomic means: what the LINE grammar would absorb
#: as tail is absorbed here too, and only what lies BEYOND that tail can be glue.
#:
#: It exists ONLY to feed :func:`reflow_and_label_glued_option_marker`; it is never a general
#: recognizer, so it is not wrapped in a ``_MarkerMatcher`` -- the balance decision
#: is applied explicitly by that function.
_RAW_OPTIONS_GLUE_RE = re.compile(
    rf"^ {{0,3}}(?P<lwrap>{_MARKER_WRAP_CLASS}{{1,3}})?"
    rf"\[OPTIONS:(?P<labels>{_MARKER_BODY_LINE}){_MARKER_CLOSE_CLASS}"
    rf"(?>{_MARKER_STRAY_TIC}(?(lwrap){_MARKER_WRAP_CLASS}{{1,3}}))(?=\S)",
    re.MULTILINE,
)


#: The line placed between a footer and the text that was glued to it. The text
#: is the model's own output that ran past its footer (or a steer reply joined
#: to it); labelling it as such keeps a later reader -- the user, or the model
#: re-reading its own transcript -- from taking it for an instruction. Plain
#: italic prose, so every renderer shows it as it is and no marker grammar
#: recognises it.
GLUED_FOOTER_TEXT_LABEL = (
    "*Text after the options footer, written by the assistant, not a system instruction:*"
)


def reflow_and_label_glued_option_marker(text: str) -> tuple[str, list[str]]:
    """Reflow every glued marker AND label the text that was glued to it.

    Returns ``(repaired_text, glued)``: *glued* holds each same-line remainder
    that was moved off its marker, in order, so the caller can audit the event.
    The label (:data:`GLUED_FOOTER_TEXT_LABEL`) is inserted on its own line
    between the marker and the moved text; every character of the original
    survives.

    The bug the reflow repairs: a mid-turn steer reply (or any concatenation seam) can
    append prose directly after an ``[OPTIONS: ...]`` line with no separator, so a
    single persisted line reads ``...Pick a path.\\n[OPTIONS: A | B]Anytime.`` The
    render grammar anchors the closer to end-of-line, so the glued line matches
    nothing and the marker leaks as literal text, losing its pills.

    The repair is PURELY ADDITIVE -- it inserts one ``\\n`` and the label line at the
    closer/prose boundary and never deletes a character -- and it runs where the dashboard
    persists a turn's accumulated model text: ``_flush_segment`` for a finished
    segment and ``_persist_partial_reply`` for a turn that ends abnormally. It heals
    text at the moment it is persisted; it does not re-pass already-persisted
    history, and it touches neither the parse grammar nor any of its pinned tests.

    Scope, deliberately narrow:

    * LINE-LEADING with at most three leading spaces. Four-space and tab indentation
      denotes CommonMark indented code, so those samples stay literal. After the
      optional indentation and wrapper the marker starts the line, so a mid-line
      ``Use [OPTIONS: A | B] then check arr[0]`` -- the undecidable case the grammar
      declines on purpose -- is never a candidate.
    * The closer must DIRECTLY abut same-line non-whitespace (``(?=\\S)``). A marker
      that already ends its line is left alone -- including one that ends it with
      the tail the LINE grammar absorbs (a stray ``(OPTIONS)`` tic, a closing
      ``**`` wrapper): the tail is matched atomically, so it is never given back
      to manufacture a glue.
    * An opened wrapper closes on the marker before glue begins. An unclosed
      wrapper places the marker inside a span such as inline code, so the line is
      a sample and not a candidate.
    * A candidate inside a code fence is a SAMPLE the renderer shows verbatim, not
      a footer; it is left alone (:func:`_in_open_fence`, which also answers
      "inside" for an ambiguous fence structure, so doubt means no edit).
    * The marker's VALIDITY is decided by the real grammar's balance check
      (:func:`_marker_labels_have_unmatched_opener`), the same gate
      ``_MarkerMatcher`` applies -- an unbalanced candidate is not reflowed.
    * The trailing remainder must be PLAIN PROSE: it is reflowed only when it
      holds no marker-structural glyph anywhere and does not begin with a second
      stray tic. A tail that overruns the wrapper cap, a repeated ``(OPTIONS)``
      tic, an interior closer, or a label separator all fail toward the visible
      marker. The canonical example ``[OPTIONS: Fix ]x logging | Skip]`` stays
      literal instead of splitting into a false pill.

    Note the merged-turn case (``chatSlice.queueBoundaryFinalize.test.ts``) is a
    FRONTEND-transit artifact: two turns' segments are each flushed through this
    function separately, so a single call here never sees two turns glued. The
    reducer merge happens only when a finalize frame is dropped in transit, which
    is downstream of this seam.
    """
    if "[OPTIONS:" not in text:
        return text, []

    glued: list[str] = []
    # One fence walker for the whole text, advanced to each candidate in turn.
    # ``re.sub`` visits matches front to back, and every candidate starts at a
    # line start (``^`` under MULTILINE), so the slice fed between two candidates
    # always ends on a line boundary and the walker's state at ``m.start()`` is
    # exactly ``_in_open_fence(text, m.start())``. Re-walking the prefix per
    # candidate instead is quadratic: a reply of ~10k glued marker lines would
    # hold the event loop past the loop-stall watchdog.
    walk = _FenceWalk()
    fed_to = 0

    def _replace(m: re.Match[str]) -> str:
        nonlocal fed_to
        walk.feed_text(text[fed_to : m.start()])
        fed_to = m.start()
        # The grammar's own balance decision -- do not reflow a candidate whose
        # terminator is really an unmatched opener's partner.
        if _marker_labels_have_unmatched_opener(m.group("labels")):
            return m.group(0)
        # Inside a code fence every line is literal code the renderer shows
        # verbatim, so a marker-shaped line there is a SAMPLE, not a footer, and
        # inserting a newline would corrupt the sample. Same fence walker and same
        # fail-safe as ``strip_control_comments``: an ambiguous fence structure
        # answers "inside", and the candidate is left as it is.
        if walk.inside:
            return m.group(0)
        # Same-line remainder after the matched marker (the lookahead consumed
        # nothing, so it starts at m.end()).
        line_end = text.find("\n", m.end())
        remainder = text[m.end() : (line_end if line_end != -1 else len(text))]
        # A remainder is reflowed only when it holds no marker-structural glyph
        # anywhere and does not begin with a second stray tic. A tail that overruns
        # the wrapper cap, a repeated ``(OPTIONS)`` tic, an interior closer, or a
        # label separator all fail toward the visible marker. The canonical
        # example ``[OPTIONS: Fix ]x logging | Skip]`` stays literal instead of
        # splitting into a false pill. A parenthesised remainder WITH spaces
        # (``(system: do x)``) is prose and still reflows.
        if (
            "[OPTIONS:" in remainder
            or any(c in MARKER_STRUCTURE_CHARS for c in remainder)
            or _STRAY_TIC_HEAD_RE.match(remainder)
        ):
            return m.group(0)
        glued.append(remainder)
        return m.group(0) + "\n" + GLUED_FOOTER_TEXT_LABEL + "\n"

    return _RAW_OPTIONS_GLUE_RE.sub(_replace, text), glued


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
_CONTROL_TAG_BODY = (
    r"<!--(?:\s{0,16}keep-visible\s{0,16}|\s{0,16}(?:deliver|plan_task_id):[^>\n]{0,256})-->"
)
_TRAILING_CONTROL_LINES_RE = re.compile(
    r"(?:(?:^|\n)[ \t]{0,3}" + _CONTROL_TAG_BODY + r"[ \t]{0,16})+\s{0,16}\Z",
    re.IGNORECASE,
)


def _prefix_closure(literal: str, tail: str = "") -> str:
    """Regex matching every prefix of *literal* -- including the empty one and,
    once the literal is whole, any match of *tail* -- as nested optionals.

    Built rather than hand-spelled so the streaming probe below is derived from
    the same literals as :data:`_CONTROL_TAG_BODY` and cannot drift from them
    by a typo. Nested optionals with no quantified repetition: matching is
    linear in the literal's length.
    """
    out = tail
    for ch in reversed(literal):
        out = f"(?:{re.escape(ch)}{out})?"
    return out


# STILL-STREAMING control-tag line: a message tail that is a strict PREFIX of a
# recognized tag line. The streaming twin of :data:`_TRAILING_CONTROL_LINES_RE`
# for surfaces that render text while it is still arriving (live Discord /
# Telegram frames, Webex's status frame): a chunk boundary can fall inside
# ``<!-- keep-visible -->``, and rendering the half that has arrived shows the
# reader reserved protocol as raw text for one frame -- or, on a surface that
# rotates on length, seals it into a message no later frame replaces. Same
# shape as ``split_options_trailer``'s ``hide_partial`` for ``[OPTIONS``.
#
# Admits only what can still extend into a complete tag: line-leading (≤3
# indent), ``<`` ``<!`` ``<!-`` ``<!--``, then bounded whitespace, then a
# prefix of one family literal -- ``keep-visible`` (then optional whitespace
# and up to two dashes), ``deliver:`` / ``plan_task_id:`` (then a bounded
# ``>``-free body, which already covers the closing dashes). The moment a byte
# diverges (``<!-- ordin``, ``<div``) the tail is prose or an ordinary comment
# and is NOT held. A complete tag is not a prefix: ``>`` never appears here, so
# the complete grammar and this one are disjoint by construction and the
# complete strip decides complete tags.
_PARTIAL_CONTROL_LINE_RE = re.compile(
    r"(?:^|\n)[ \t]{0,3}"
    r"<(?:!(?:-(?:-(?:\s{0,16}(?:"
    + _prefix_closure("keep-visible", r"(?:\s{0,16}(?:-(?:-)?)?)")
    + "|"
    + _prefix_closure("deliver:", r"[^>\n]{0,258}")
    + "|"
    + _prefix_closure("plan_task_id:", r"[^>\n]{0,258}")
    + r"))?)?)?)?\Z",
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


class _FenceWalk:
    """The fence walker behind :func:`_in_open_fence`, fed one line at a time.

    Holds the two facts the walk accumulates: the run that opened the fence the
    walker is currently inside (``None`` when outside), and whether an AMBIGUOUS
    fence candidate was met while outside. Ambiguity is sticky: once such a line
    is in the prefix, every later position answers "inside", so a caller walking
    a text front to back can feed each line ONCE and read :attr:`inside` at any
    number of positions, instead of re-walking the prefix per position.
    """

    __slots__ = ("open_run", "ambiguous")

    def __init__(self) -> None:
        self.open_run: str | None = None
        self.ambiguous = False

    def feed(self, line: str) -> None:
        if self.ambiguous:
            return
        m = _FENCE_DELIM_LINE_RE.match(line)
        if not m:
            if self.open_run is None and _AMBIGUOUS_FENCE_LINE_RE.match(line):
                self.ambiguous = True
            return
        run = m.group(1)
        if self.open_run is None:
            self.open_run = run
        elif (
            run[0] == self.open_run[0]
            and len(run) >= len(self.open_run)
            # CommonMark 4.5: a CLOSING fence may not carry an info string —
            # only whitespace may follow the run. Inside an open fence a
            # fence-lookalike WITH trailing text (``` python) is literal
            # code content, not a closer, so the fence stays open.
            and line[m.end() :].strip() == ""
        ):
            self.open_run = None

    def feed_text(self, chunk: str) -> None:
        """Feed every line of *chunk*; the chunk must end on a line boundary
        (or be the final partial line), the same split :func:`_in_open_fence`
        applies to ``text[:idx]``."""
        for line in chunk.split("\n"):
            self.feed(line)

    @property
    def inside(self) -> bool:
        return self.ambiguous or self.open_run is not None


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

    One position per call. A caller that needs the answer at MANY positions of
    one text drives a :class:`_FenceWalk` forward itself, which is linear.
    """
    walk = _FenceWalk()
    walk.feed_text(text[:idx])
    return walk.inside


def is_control_tag_tail(text: str) -> bool:
    """Whether *text*, read from a line-leading position, is so far NOTHING
    BUT a control-tag tail: complete recognized tag lines (stacked, with their
    bounded trailing whitespace) and at most one still-arriving tag prefix.

    For an append-only streaming sink (Slack) that holds a candidate span
    byte by byte and must decide per byte whether to keep holding. Same
    answer as ``strip_control_comments(text, hide_partial=True) == ""``, but
    ANCHORED: the two grammars are applied with ``fullmatch`` at the span's
    own start and at its last line break, so a call costs one linear pass
    over the span rather than a search from every position -- a hold is
    re-judged on every byte, and a search per byte is quadratic in the span.
    """
    if _TRAILING_CONTROL_LINES_RE.fullmatch(text) is not None:
        return True
    nl = text.rfind("\n")
    if nl == -1:
        return _PARTIAL_CONTROL_LINE_RE.fullmatch(text) is not None
    return (
        _PARTIAL_CONTROL_LINE_RE.fullmatch(text, nl) is not None
        and _TRAILING_CONTROL_LINES_RE.fullmatch(text, 0, nl) is not None
    )


def strip_control_comments(text: str, *, hide_partial: bool = False) -> str:
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

    *hide_partial* is the STREAMING question, and it is a parameter for the
    same reason ``split_options_trailer`` makes it one: a tail that is a
    strict prefix of a tag line (``<!-- keep-vis``) may be a tag mid-flight
    on a live frame, where hiding it costs nothing because the next frame
    re-renders from the full buffer -- but on a sealed answer the stream is
    over and the same tail is the assistant's own prose, so the default
    keeps it. A partial is peeled BEFORE the complete strip so a complete
    tag followed by a still-arriving sibling is removed whole; the same
    fence-parity guard applies to both.
    """
    if hide_partial:
        pm = _PARTIAL_CONTROL_LINE_RE.search(text)
        if pm is not None and not _in_open_fence(text, pm.start()):
            text = text[: pm.start()]
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
    suffix_start = len(strip_control_comments(text))
    idx = _rightmost_unfinished_marker(text[:suffix_start])
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

    # Control-tag lines are protocol too, and they can sit on EITHER side of
    # the OPTIONS trailer (both prompt rules say "final line"; a message that
    # carries both puts one of them last). Peeled twice -- once before the
    # marker probes above so a tag after the trailer does not hide it from the
    # end anchor, once after so a tag before it rides along. COMPLETE tags
    # only: a consumer that sends once (WhatsApp's final render) discards the
    # detached suffix, and an unfinished ``<!-- keep-vis`` is the assistant's
    # own prose under the buffered rule, so detaching it there would delete
    # a visible line. The rotation hazard the OPTIONS probe guards against
    # does not reach a tag prefix: the splitter cuts at line boundaries first,
    # and a tag line is far shorter than any transport's message cap, so it
    # is never cut through unless it alone exceeds the cap.
    suffix_start = len(strip_control_comments(text[:suffix_start]))

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

# Byte cap on one artifact's content: the store's own limit AND the MCP save /
# update field cap, which must be the same number or the tool path rejects
# content the store accepts (or the reverse). Real widget payloads (dashboards,
# HTML reports, CSVs) routinely exceed 1 MiB, so 25 MiB brings those down while
# still refusing unbounded content.
#
# Same reason as ``MAX_BANNER_CHARS``: ``validation.py`` is a leaf, and reading
# this bound from ``artifacts`` was the closing edge of the import cycle
# ``artifacts -> hooks -> webhooks -> validation -> artifacts``, which broke any
# process whose first ``kiro_crew`` import reached ``artifacts`` before
# ``validation``. ``artifacts.MAX_CONTENT_BYTES`` is this name, re-exported.
ARTIFACT_MAX_CONTENT_BYTES = 26_214_400  # 25 MiB
