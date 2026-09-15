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
#: Every opener carries the SAME head guard as the bare-opener alternative.
#: It is load-bearing on the ``[`` alternative -- without it that
#: form is the one place the union rule is LOOSER than the body it replaced,
#: because it can open on a nested head and pair it with that head's own closer.
#: ``Note [OPTIONS: see [OPTIONS: x] below | Skip]`` then matches from the OUTER
#: head and renders a pill whose label is a raw protocol marker, echoed back as
#: the user's reply when tapped. The guard makes "no bracket form may consume a
#: ``[`` that begins a fresh ``[OPTIONS:``" an absolute property of the body. The
#: lookalike openers cannot spell the head, so the guard is a no-op on them; it
#: rides them anyway so the alternatives are one shape and the property does not
#: depend on which opener carries it.
#: Spelled ONCE here for all three guards below. The singular ``OPTION:`` is
#: deliberate parity: the frontend head is ``OPTION(S)?:`` and it refuses it too.
_MARKER_BODY_TEMPER = r"(?!OPTION-ACTIONS:|OPTIONS?:)"
_MARKER_LABEL_PAIR = "|".join(
    rf"{re.escape(_open)}{_MARKER_BODY_TEMPER}[^{_MARKER_BRACKETS}\n]*"
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
#: Both bracket alternatives refuse EVERY head, not just ``OPTIONS:``. A body that
#: tempers against only its own head still consumes the other one: given
#: ``"[OPTIONS: a | b]\n[OPTION-ACTIONS: close=X]"`` it crossed the second marker and
#: captured its raw text as a BUTTON LABEL. The head list is spelled once more as
#: :data:`_MARKER_HEAD_ALT` below; :data:`_MARKER_BODY_TEMPER` is the temper's own
#: single spelling, and a test pins the relation between the two.
_MARKER_BODY_LINE = (
    rf"(?:{_MARKER_LABEL_PAIR}|{_MARKER_OPEN_CLASS}{_MARKER_BODY_TEMPER}"
    rf"|{_MARKER_CLOSE_CLASS}{_MARKER_LABEL_CONTINUES}"
    rf"|[^{_MARKER_BRACKETS}\n])*"
)
_MARKER_BODY_TRAILER = (
    rf"(?:{_MARKER_LABEL_PAIR}|{_MARKER_OPEN_CLASS}{_MARKER_BODY_TEMPER}"
    rf"|{_MARKER_CLOSE_CLASS}{_MARKER_LABEL_CONTINUES}"
    rf"|[^{_MARKER_BRACKETS}])*"
)
# The wrapper run is spelled once as a LEAD and a conditional CLOSER so the
# ``[OPTIONS:`` patterns below can carry it without the action forms doing so.
_MARKER_WRAP_LEAD = rf"(?:^[ \t]*(?P<lwrap>{_MARKER_WRAP_CLASS}{{1,3}}))?"
_MARKER_WRAP_TRAIL = rf"(?(lwrap){_MARKER_WRAP_CLASS}{{0,3}})"

# The ``labels`` group is NAMED because the ``lwrap`` conditional group
# necessarily precedes it, shifting positional numbering: consumers read
# ``group("labels")`` (and iterate with ``finditer``, since ``findall`` on a
# multi-group pattern yields tuples).
#: Every protocol head a tempered marker body must refuse to cross, as ONE
#: alternation shared by all four patterns below.
#:
#: The tempering exists for ReDoS (see the block above), but once a SECOND head
#: exists it also carries a correctness property the single-head version never
#: had to: a body that forbids only its OWN head still happily consumes the
#: OTHER one. That is not theoretical -- it was MEASURED on the pre-existing
#: ``OPTIONS_RE_TRAILER`` before this list was introduced. Given
#: ``"[OPTIONS: a | b]\n[OPTION-ACTIONS: close=Close this tab]"``, its body
#: crossed the second marker and captured
#: ``" a | b]\n[OPTION-ACTIONS: close=Close this tab"``, so the SECOND marker's
#: raw text became a channel BUTTON LABEL and the real second choice was lost.
#: The mirror case (action head first, ``[OPTIONS:`` last) does the same to the
#: action pattern. Both are silent: the regex matches, the anchor is satisfied,
#: and only the captured label is wrong.
#:
#: Only the ``\Z``-anchored TRAILER forms can actually hit this -- the LINE
#: bodies exclude ``\n`` and both heads own their line -- but the exclusion is
#: applied to BOTH forms anyway, because "which variant is currently reachable"
#: is a property of today's call sites, not of the grammar, and a per-variant
#: exception is exactly the drift these shared definitions exist to prevent.
#:
#: Adding a head here is behaviour-preserving for any text that does not contain
#: it, and stays linear: at each position either the character is not ``[`` (first
#: branch) or it is, and the lookahead alone decides -- the branches remain
#: mutually exclusive, so no new backtracking path is created.
_MARKER_HEADS = ("OPTIONS:", "OPTION-ACTIONS:")
#: Heads matched case-INSENSITIVELY wherever the shared alternation below is used —
#: the line tail's sibling lookahead, NOT the temper. PER-HEAD, because the two
#: markers genuinely differ here: ``OPTION_ACTIONS_RE_*`` carry ``re.IGNORECASE`` to
#: match the ``i`` flag the frontend twin WILL carry: a mixed-case action marker
#: becomes a live chip only once that half lands — while ``OPTIONS_RE_*`` stay
#: case-sensitive by a deliberate, documented decision (see below).
#:
#: A case-SENSITIVE temper against a head that its own pattern matches
#: case-insensitively WOULD be a contradiction, corrupting data rather than merely
#: missing a match. Absent the two rescues below, on that same input: the
#: mixed-case sibling is not recognised as a head, so the temper's negative
#: lookahead SUCCEEDS, the content body consumes straight through it, and the
#: captured label becomes ``" A] [Option-Actions: close=B"`` — the action marker
#: delivered to every client as a user-visible option label. Both rescues are pinned.
#:
#: ``OPTIONS:`` is deliberately NOT in this set. Widening the content head here
#: would change how every pre-existing ``[OPTIONS:]`` marker on every streamed
#: channel message parses, which is a far larger blast radius than this fix; that
#: divergence from the frontend is pre-existing and stays a called-out follow-up.
_CASE_INSENSITIVE_MARKER_HEADS = frozenset({"OPTION-ACTIONS:"})


def _marker_head_atom(head: str) -> str:
    """One alternation branch, scoped to its own casing rule.

    ``(?i:…)`` is a SCOPED inline flag, so it applies to this branch alone and
    leaves the leading ``\\[OPTIONS:`` literal of ``OPTIONS_RE_*`` untouched — the
    fix must not widen that. Inside the already-``IGNORECASE`` action patterns the
    scope is a no-op, so one shared alternation still serves both.
    """
    escaped = re.escape(head)
    return f"(?i:{escaped})" if head in _CASE_INSENSITIVE_MARKER_HEADS else escaped


def marker_prefix_is_case_insensitive(prefix: str) -> bool:
    """Whether *prefix*'s own compiled pattern matches its head case-insensitively.

    Reads the SAME authority the patterns are built from rather than restating the
    rule, because the streaming path must hold exactly what the batch parser will
    strip. Holding MORE than the parser strips is not a harmless over-match: with a
    streaming consumer wired, the live bubble has the run excised, then the final
    message —
    whose text comes from the case-SENSITIVE ``extract_options`` — puts it back, so
    the marker appears as a visible pop-in of raw protocol text that did not happen
    before the streaming helpers existed.

    Takes a :data:`MARKER_PREFIXES` entry (``"[OPTIONS"``), which carries a leading
    bracket and no colon, and normalises it to the head spelling this set uses
    (``"OPTIONS:"``). Defined here beside that set so the two cannot drift apart.
    """
    return f"{prefix.lstrip('[')}:" in _CASE_INSENSITIVE_MARKER_HEADS


#: The head alternation, escaped once and shared by the temper and the line tail so a
#: new head reaches both. Per-head casing via ``_marker_head_atom``.
_MARKER_HEAD_ALT = "|".join(_marker_head_atom(h) for h in _MARKER_HEADS)
_TEMPER = r"\[(?!" + _MARKER_HEAD_ALT + ")"
#: The tempered bodies are defined ONCE, above, in their conditional-closer
#: form. They are not respelled here: two definitions of the same name would leave
#: the later one silently winning, and the closer rule would be the half that lost.
#: Shared tail: the closer class, the optional stray markdown-link close, and
#: the trailing-whitespace run before the anchor.
#:
#: The LINE form terminates at the end of the line OR immediately before a SIBLING
#: MARKER on the same line. Requiring ``$`` alone meant only the TRAILING marker of a
#: shared line could match, so the leading one was left unmatched -- and on this side
#: an unmatched marker is not merely unparsed, it is passed through VERBATIM: posted
#: raw into a Slack body, spoken by TTS, and left in the sidebar preview. When the
#: surviving marker is a destructive ``close``, that is the affordance the user is
#: handed. A LOOKAHEAD rather than a consuming alternative, so the sibling stays
#: available to its own pattern and both parse from one line; O(1) at the terminator,
#: and the body is still tempered against every head. Trailing PROSE still does not
#: terminate a marker -- only a sibling marker does -- so a sentence discussing the
#: syntax is left alone.
_MARKER_CLOSE_RUN = rf"{_MARKER_CLOSE_CLASS}{_MARKER_STRAY_TIC}"
_MARKER_LINE_END = rf"[ \t]*(?:$|(?=\[(?:{_MARKER_HEAD_ALT})))"
_MARKER_TAIL_LINE = _MARKER_CLOSE_RUN + _MARKER_LINE_END
_MARKER_TAIL_TRAILER = _MARKER_CLOSE_RUN + r"\s*\Z"
#: The ``[OPTIONS:`` tails splice the conditional wrapper closer in before the anchor;
#: the action forms reuse the plain tails, having no ``lwrap`` group to test.
_MARKER_TAIL_LINE_WRAPPED = _MARKER_CLOSE_RUN + _MARKER_WRAP_TRAIL + _MARKER_LINE_END
_MARKER_TAIL_TRAILER_WRAPPED = _MARKER_CLOSE_RUN + _MARKER_WRAP_TRAIL + r"\s*\Z"

_RAW_OPTIONS_RE_LINE = re.compile(
    rf"{_MARKER_WRAP_LEAD}\[OPTIONS:(?P<labels>{_MARKER_BODY_LINE})"
    rf"{_MARKER_TAIL_LINE_WRAPPED}",
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
    rf"{_MARKER_WRAP_LEAD}\[OPTIONS:(?P<labels>{_MARKER_BODY_TRAILER})"
    rf"{_MARKER_TAIL_TRAILER_WRAPPED}",
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

    def match(self, text: str, pos: int = 0, endpos: int | None = None) -> re.Match[str] | None:
        """An ACCEPTED marker anchored at *pos*, or ``None``.

        Anchored, so it cannot be expressed as :meth:`search` over a slice: the
        suffix splitter asks whether a marker begins exactly at a candidate head,
        and a scan would answer yes for one further along. ``pos``/``endpos`` are
        forwarded rather than the text being sliced, because slicing would move
        ``^``/``\\Z`` and the anchors are load-bearing in both forms.

        The balance decision is applied here too — that is the whole point of the
        wrapper, and a member that skipped it would be the bypass the private
        pattern exists to close.
        """
        raw = self._pattern.match(text, pos, len(text) if endpos is None else endpos)
        if raw is None:
            return None
        return None if _marker_labels_have_unmatched_opener(raw.group("labels")) else raw


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


# ── "[OPTION-ACTIONS: close=label]" — the zero-turn UI-action marker ─────────
# A SIBLING of the OPTIONS marker, not an extension of it, and the distinct head
# is the entire mechanism. Only the dashboard frontend acts on this one: it
# renders a button that runs a LOCAL UI action (currently just ``close``) with no
# LLM turn. Body is exactly ONE ``<action>=<label>`` entry, where the action is a
# strict enum and the label — everything after the FIRST ``=`` — is free
# text. Both consumer roles arrive with the consumer stack: the renderers will
# STRIP the marker, and a presence check will answer whether it renders a chip.
#
# WHY a separate head instead of a reserved label or prefix inside ``[OPTIONS:]``:
# option labels are model-emitted prose, so any in-band encoding means an agent
# that merely WRITES ABOUT this feature would emit a live close button and tear
# down the user's tab. The action therefore occupies its own field, and the label
# is never load-bearing.
#
# WHY these patterns are needed at all, given the head is inert for every
# existing parser: inert does not mean invisible. A parser keyed on the
# literal ``[OPTIONS:`` does not MANGLE a non-matching marker — it passes it
# through VERBATIM as visible text. So without a matching strip the marker is
# posted raw into Slack, shown in the sidebar preview, and READ ALOUD by TTS.
# Inertness buys safety from misparsing and costs a leak on every surface; these
# patterns pay that cost back.
#
# Grammar is deliberately IDENTICAL to the OPTIONS pair — same shared tempered
# body, same ``MARKER_CLOSERS`` class incl. the CJK lookalikes, same optional
# stray markdown-link close, same anchors — because the failure modes are the
# same failure modes. A model that substitutes ``】`` for ``]`` or appends a
# ``(OPTIONS)`` tic does so regardless of which head it just wrote, and here the
# consequence of a broken end anchor is strictly worse than a lost button: the
# marker leaks as literal text on every surface listed above. Sharing the pieces
# rather than re-spelling them is what keeps that true as the grammar evolves.
#
# NON-COLLISION, in both directions, is the property the whole design rests on,
# and it is structural rather than incidental: ``OPTIONS_RE_*`` requires the
# literal ``OPTIONS:`` immediately after ``[``, and ``[OPTION-`` cannot supply it;
# these patterns require the literal ``OPTION-ACTIONS:``, which a bare
# ``[OPTIONS:`` cannot supply. Neither can ever parse the other's marker as its
# own, so an action marker never yields content choices and a content marker
# never yields an action.
#: IGNORECASE, mirroring the `gim` the frontend twin will carry: it will render a chip for
#: `[Option-Actions: close=…]`, so a case-sensitive backend pattern reports the wrong
#: `has_options` for that row AND leaves the marker in every stripped surface — Slack,
#: TTS, the sidebar preview — where the raw text then reaches the user. Both forms
#: carry the flag, because a divergence between the LINE and TRAILER spellings of ONE
#: marker is the same defect one level down.
#:
#: Deliberately NOT applied to ``OPTIONS_RE_*``: that head has the identical
#: divergence from the frontend, but it is pre-existing and independent of this
#: change, and widening the content marker's grammar here would re-roll a much larger
#: blast radius than the action marker's. It is called out as a follow-up.
_RAW_OPTION_ACTIONS_RE_LINE = re.compile(
    rf"\[OPTION-ACTIONS:(?P<labels>{_MARKER_BODY_LINE}){_MARKER_TAIL_LINE}",
    re.MULTILINE | re.IGNORECASE,
)

_RAW_OPTION_ACTIONS_RE_TRAILER = re.compile(
    rf"\[OPTION-ACTIONS:(?P<labels>{_MARKER_BODY_TRAILER}){_MARKER_TAIL_TRAILER}",
    re.DOTALL | re.IGNORECASE,
)

#: Wrapped like the ``OPTIONS`` pair, and the asymmetry was a hole: the body admits a
#: bare closer, so a label's own bracket could otherwise terminate the marker.
OPTION_ACTIONS_RE_LINE = _MarkerMatcher(_RAW_OPTION_ACTIONS_RE_LINE)
OPTION_ACTIONS_RE_TRAILER = _MarkerMatcher(_RAW_OPTION_ACTIONS_RE_TRAILER)

#: The SUPPRESSION head scan — "is there an UNCLOSED head before this offset?" — and
#: deliberately NOT ``_MARKER_HEAD_ALT``. It mirrors the frontend head scan AS THE
#: CONSUMER STACK WILL WIDEN IT (that side knows no action head yet), because
#: the two sides decide INDEPENDENTLY whether a nested action is a marker. A narrower
#: scan here accepts an action the frontend refuses, so the backend raises
#: ``waiting_for_input`` for a chip that never renders — a turn that waits on nothing.
#:
#: Widening is safe precisely BECAUSE this scan is not the marker grammar: its only
#: consumer is :func:`_unclosed_marker_flags`, which is only ever asked about ACTION
#: marker offsets. The singular head is NOT in ``_MARKER_HEADS``, which feeds the line
#: tail, the trailer scan and the strip — it would be spoken and previewed away there.
_MARKER_SUPPRESSION_HEAD_RE = re.compile(r"\[(?:OPTION-ACTIONS:|OPTIONS?:)", re.IGNORECASE)
#: The two CONTENT/ACTION heads as a standalone scan, derived from the SAME alternation the
#: trailer patterns are built from so a new head reaches this without a second definition.
#: Distinct from the suppression scan above, which deliberately carries the singular head.
_MARKER_TRAILER_HEAD_SCAN_RE = re.compile(r"\[(?:" + _MARKER_HEAD_ALT + ")")
_MARKER_CLOSE_SCAN_RE = re.compile(_MARKER_CLOSE_CLASS)
_MARKER_OPEN_SCAN_RE = re.compile(_MARKER_OPEN_CLASS)
#: Newlines as a scan too, so the line a match sits on is found by an advancing pointer
#: rather than an ``rfind`` per match -- which was itself linear in the prefix.
_MARKER_NEWLINE_SCAN_RE = re.compile("\n")


def _unclosed_marker_flags(text: str, starts: list[int]) -> list[bool]:
    """For each ASCENDING offset in *starts*, whether it sits inside an unclosed head.

    Linear BY CONSTRUCTION, and the naive shape is why. Re-scanning the whole line
    prefix once per match would cost O(n*k): a 104k-character single-line model
    response with 4000 action markers stalls the gateway event loop for 1.6 SECONDS
    on that shape, on text the model controls. Indexing each class once and walking
    three monotonic pointers is O(n + k) -- the same input costs 10ms, measured.

    Callers must pass offsets in ascending order, which both consumers below do:
    ``finditer`` and ``sub`` each traverse left to right. The pointers only advance, so
    an out-of-order offset would silently read a stale window rather than fail loudly --
    hence the requirement stated here rather than left to the reader.

    The predicate is the same one spelled out on :func:`_is_inside_unclosed_marker`:
    a per-line bracket DEPTH, not a comparison of the last head against the last closer.
    That pairwise form was wrong for a BALANCED nested pair inside an unclosed head --
    the pair's own closer became the last closer and its head the last head, so it read
    as closed and a following sibling was accepted while the outer head stayed open.

    Depth counts HEAD brackets only, and a closer pops whichever bracket is innermost.
    A bare count was wrong the same way one step down: a citation ``[1]`` inside an open
    head supplied a closer that cancelled the head, so
    ``[OPTIONS: see [1] for details [OPTION-ACTIONS: close=X]`` rendered a live close
    chip from syntax that matches no content marker at all, while the identical line
    without the citation suppressed it. A stray closer with no opener still cannot
    cancel a real head -- it pops an empty stack, which is a no-op.
    """
    heads = [m.start() for m in _MARKER_SUPPRESSION_HEAD_RE.finditer(text)]
    openers = [m.start() for m in _MARKER_OPEN_SCAN_RE.finditer(text)]
    closers = [m.start() for m in _MARKER_CLOSE_SCAN_RE.finditer(text)]
    newlines = [m.start() for m in _MARKER_NEWLINE_SCAN_RE.finditer(text)]
    head_i = open_i = close_i = line_i = 0
    # Innermost-last: True marks a marker head, False any other bracket. `depth` tracks
    # how many of the frames are heads, so the flag stays O(1) per offset.
    stack: list[bool] = []
    depth = 0
    flags: list[bool] = []
    for start in starts:
        # Advance all three ascending scans in OFFSET order -- a closer must pop the bracket
        # it actually closes, so draining openers first would mispair them. Still linear.
        while True:
            o = openers[open_i] if open_i < len(openers) and openers[open_i] < start else None
            c = closers[close_i] if close_i < len(closers) and closers[close_i] < start else None
            n = newlines[line_i] if line_i < len(newlines) and newlines[line_i] < start else None
            nxt = min((v for v in (o, c, n) if v is not None), default=None)
            if nxt is None:
                break
            if nxt == n:
                stack.clear()  # both heads are LINE forms; an open head stops at its newline
                depth = 0
                line_i += 1
            elif nxt == o:
                # A head IS an opener, so the ascending head pointer classifies it in O(1).
                while head_i < len(heads) and heads[head_i] < nxt:
                    head_i += 1
                is_head = head_i < len(heads) and heads[head_i] == nxt
                if is_head:
                    head_i += 1
                    depth += 1
                stack.append(is_head)
                open_i += 1
            else:
                if stack.pop() if stack else False:
                    depth -= 1
                close_i += 1
        flags.append(depth > 0)
    return flags


def _is_inside_unclosed_marker(text: str, match_start: int) -> bool:
    """Whether *match_start* sits inside a marker head that never closed.

    The single-offset spelling of :func:`_unclosed_marker_flags`, sharing its
    implementation rather than restating the rule: two copies of this predicate that
    disagreed would put a chip on screen for text still visible, or hide text with no
    chip to show for it. Consumers scanning many offsets must call the plural form,
    which is linear across the whole scan; this one indexes the text per call.

    The action pattern scans INDEPENDENTLY of the content pattern, so a nested span
    matches on its own even when the marker enclosing it is broken. On
    ``[OPTIONS: dropped closer [OPTION-ACTIONS: close=X]`` the content marker does not
    match at all -- its body is tempered against every head, so it stops at the ``[``
    and then finds no closer before it -- while the action marker does. Keying chip
    presence on that match reports choices for a row that renders no chip, which is
    the same class of defect the out-of-enum action guard covers, arriving by a
    different route.

    Scoped to the LINE, because a marker is a line-local construct: an unclosed head
    on an earlier line does not reach across the newline.

    MIRRORS THE FRONTEND AS THE STACK WILL LEAVE IT, deliberately: the suppression scan
    is ``_MARKER_SUPPRESSION_HEAD_RE``, which matches the singular head and either
    casing, as the frontend head scan will once its half lands — at this tip that
    regex is still ``\\[OPTIONS?:``. So ``[options: broken
    [OPTION-ACTIONS: close=X]`` and ``[OPTION: broken [OPTION-ACTIONS: close=X]`` are
    BOTH seen here as an unclosed head, and both sides refuse the nested action. They
    were they to disagree, the backend would raise ``waiting_for_input`` for a chip the
    frontend never rendered.

    This does NOT widen the content head, which stays case-sensitive and plural-only —
    the divergence documented at ``_CASE_INSENSITIVE_MARKER_HEADS`` is about what counts
    as a MARKER and is untouched here. The two are separable because this predicate is
    only ever asked about ACTION offsets.
    """
    return _unclosed_marker_flags(text, [match_start])[0]


def match_action_markers(text: str) -> list[re.Match[str]]:
    """Every action marker in *text* that is genuinely a marker.

    The scan a consumer should use. Matching ``OPTION_ACTIONS_RE_LINE`` directly
    re-introduces the nested-in-a-broken-marker defect this filter exists to close.
    The frontend twin it must agree with arrives with the consumer stack and does not
    exist at this tip; once both exist they must agree, because the backend decides
    ``waiting_for_input`` for a chip only the frontend renders.
    """
    matches = list(OPTION_ACTIONS_RE_LINE.finditer(text))
    flags = _unclosed_marker_flags(text, [m.start() for m in matches])
    return [match for match, inside in zip(matches, flags) if not inside]


def strip_action_markers(text: str) -> str:
    """Remove every genuine action marker from *text*, leaving rejected spans intact.

    PAIRED with :func:`match_action_markers` on purpose: a span the matcher refuses is
    not a marker, so it must stay VISIBLE rather than be silently excised. Stripping
    what the matcher rejects would delete text the user is meant to see -- the broken
    syntax is the only cue that a marker was intended -- while matching what the
    stripper removes would leave raw protocol text in the prompt.

    Built ON that matcher rather than restating its filters, so the pairing is
    structural. Two independent refusals now have to agree -- the balance decision
    inside :class:`_MarkerMatcher` and the unclosed-head scan -- and a ``sub`` over the
    pattern could only re-apply the second.
    """
    out: list[str] = []
    cursor = 0
    for match in match_action_markers(text):
        out.append(text[cursor : match.start()])
        cursor = match.end()
    out.append(text[cursor:])
    return "".join(out)


#: Body for the STRIP pattern below. Distinct from ``_MARKER_BODY_LINE`` on purpose,
#: and the difference is a MEASURED defect in each direction.
#:
#: ``_MARKER_BODY_LINE``'s class is ``[^[\n]`` — it excludes ``[`` and newline but NOT
#: ``]`` — which is safe only because the LINE forms then require ``_MARKER_TAIL_LINE``
#: (end of line, or an abutting sibling marker). The strip pattern has no such anchor,
#: so reusing that body let a greedy match run PAST the marker's own closer to the last
#: closer on the line. MEASURED: ``"See [OPTIONS: A | B] for details [1]"`` was spoken
#: as ``"See"`` — a citation, and every word before it, deleted from the utterance.
#:
#: A lazy ``[^\]]*`` is the other trap, and it was the ORIGINAL defect: it stops at the
#: FIRST closer, so a label carrying a bracket — ``[OPTIONS: do [x] now]`` — left
#: ``now]`` behind to be spoken.
#:
#: So neither existing body serves, and this one stops at the first UNMATCHED closer:
#: either a character that is neither bracket nor newline, or a tempered bracketed span
#: taken as one ATOM. Nothing in the body can consume a bare ``]``, which is precisely
#: why a match cannot cross one.
#:
#: The span appears TWICE, and that is a ReDoS fix rather than duplication. It was one
#: branch whose closer was OPTIONAL, which let a run of plain characters after a ``[`` be
#: divided between the span and the single-character branch in ANY proportion — and that
#: choice multiplies per ``[``, so the cost was exponential in the number of fragments.
#: MEASURED on ``"[OPTIONS: " + "[x" * n``: 0.2ms at n=10, 3.1s at n=24, over 5s at
#: n=26 — a 62-character string, and this body runs on the TTS path, where the caller is
#: ``voice_reply.strip_markdown`` on the gateway's own loop.
#:
#: Splitting it removes the ambiguity because each branch now has a FORCED length: the
#: complete span's closer is REQUIRED, so its run can only end at the one closer it
#: reaches, and the bare branch consumes the ``[`` alone. Complete is tried FIRST, so a
#: balanced label still matches as one atom, while an unbalanced ``[`` falls through to
#: the bare branch — the case the old ``?`` bought, kept without the backtracking that
#: paid for it. Equivalence is not an argument: the two forms were compared over 10,947
#: inputs, 9,849 of which the expression rewrites, and they agree on every one.
_MARKER_BODY_STRIP = rf"(?:[^[\]\n]|{_TEMPER}[^[\]\n]*{_MARKER_CLOSE_CLASS}|{_TEMPER})*"

#: A marker head ANYWHERE in a line — for surfaces whose job is to REMOVE protocol
#: noise rather than to parse a dispatchable marker. Those are different questions and
#: conflating them loses either way.
#:
#: The dispatch patterns above require a marker to END its line, deliberately, so that
#: a sentence merely DISCUSSING the syntax is not treated as a marker and dispatched.
#: But a stripping surface has the opposite duty: it must delete the bracketed text
#: precisely BECAUSE it is prose the user never meant to hear — while leaving the rest
#: of the sentence, which the user did.
#:
#: SEPARATE from the anchored LINE form, which REFUSES a mid-prose marker: repointing
#: ``voice_reply`` to it would leave one READ ALOUD in full — the worst surface for it,
#: since a synthesised utterance cannot be scrolled past or re-rendered.
#:
#: ``IGNORECASE`` because a lowercase pseudo-marker is just as unwanted in speech, and
#: widening a strip can only remove noise — it dispatches nothing.
MARKER_STRIP_ANYWHERE_RE = re.compile(
    rf"\[(?:{_MARKER_HEAD_ALT}){_MARKER_BODY_STRIP}{_MARKER_CLOSE_CLASS}{_MARKER_STRAY_TIC}",
    re.IGNORECASE,
)

#: Every protocol-marker prefix a raw ``str.find``/``startswith`` scan must look
#: for, longest-distinguishing first. Exists because several surfaces cannot use
#: the regexes at all — a character-at-a-time streaming filter and an
#: unfinished-marker check have no complete marker to match yet — and each of
#: those was written against the single literal ``"[OPTIONS"``. That literal is
#: NOT a prefix of ``"[OPTION-ACTIONS"``: the strings diverge at ``S`` vs ``-``,
#: so ``"[OPTION-ACTIONS: …".startswith("[OPTIONS:")`` is False and every such
#: scan silently misses the new marker while looking like it covers both. Iterate
#: this tuple instead of spelling a literal, so adding a head reaches them.
MARKER_PREFIXES = ("[OPTION-ACTIONS", "[OPTIONS")

#: Prefix-form head scan, each head cased by its OWN rule (the action patterns
#: carry ``re.IGNORECASE``; the content ones deliberately do not).
_MARKER_PREFIX_SCAN_RE = re.compile(
    "|".join(
        f"(?i:{re.escape(p)})" if marker_prefix_is_case_insensitive(p) else re.escape(p)
        for p in MARKER_PREFIXES
    )
)


def marker_head_len(text: str, idx: int) -> int:
    """Length of the marker head AT *idx*, for a caller about to slice past it.

    :func:`rfind_marker_head` finds either head, and the two are not prefixes of one
    another -- they diverge at ``S`` vs ``-``. A caller that assumed ``[OPTIONS``
    landed mid-``-ACTIONS:`` on an action head, so the grammar probe read ``-`` where
    the marker's own ``:`` was, judged a live fragment to be prose, and rendered
    reserved protocol as raw text.

    Cased to match :func:`rfind_marker_head`: the action head is found
    case-insensitively, so its length must be too. No index is derived from a folded
    string -- the comparison is against a FIXED-length literal, so a codepoint whose
    case change alters length simply fails to match instead of shifting a cut.
    """
    for head in MARKER_PREFIXES:
        segment = text[idx : idx + len(head)]
        if segment == head or segment.upper() == head:
            return len(head)
    return len(MARKER_PREFIXES[-1])


def rfind_marker_head(text: str) -> int:
    """Offset of the LAST marker head in *text*, or ``-1``.

    Cased PER HEAD, so a mixed-case ``[option-actions`` fragment is found by the
    same rule that will later STRIP it. A plain ``rfind`` is case-sensitive and
    missed one, so an unfinished mixed-case action marker was never detached: the
    fragment stayed in the length-split path, where a rotation cuts it mid-marker
    and the surface seals the halves as raw protocol text. That text is permanent
    on a channel that cannot edit a sent message.

    Regex offsets rather than ``text.lower().rfind(...)`` on purpose: ``str.lower``
    is NOT length-preserving for every codepoint, and every caller slices ``text``
    on this index, so a folded haystack can shift the cut.
    """
    return max((m.start() for m in _MARKER_PREFIX_SCAN_RE.finditer(text)), default=-1)


def starts_with_marker_head(text: str) -> bool:
    """Whether *text* STARTS with a marker head, each cased by its own rule.

    The anchored twin of :func:`rfind_marker_head`, for the table-run terminator:
    a lowercase ``[option-actions:`` line carries pipes, so a case-sensitive
    ``startswith`` let it through and the table above absorbed it as a body row —
    rendering the user's choices as card data.

    Marker heads only, and no vararg: ``[STEERING`` is the only other literal a
    caller would want, and it reaches the detach walk as its own
    :data:`_MARKER_SENTINELS` entry, carrying its own case-sensitive locator there.
    """
    return _MARKER_PREFIX_SCAN_RE.match(text) is not None


#: Characters a marker head can END with, and the longest head's length. Together they
#: bound the suffix check in :func:`excise_marker_spans`: it runs on a window of at
#: most this many characters, and only when the character just appended could complete
#: a head at all. Derived from :data:`MARKER_PREFIXES` under that set's OWN per-head
#: casing rule rather than spelled out, so adding a head reaches this scan too.
_MARKER_HEAD_FINAL_CHARS = frozenset(
    char
    for prefix in MARKER_PREFIXES
    for char in (
        (prefix[-1].upper(), prefix[-1].lower())
        if marker_prefix_is_case_insensitive(prefix)
        else (prefix[-1],)
    )
)
_MARKER_HEAD_MAX_LEN = max(len(prefix) for prefix in MARKER_PREFIXES)
#: The head alternation anchored at END of string. The other scans ask "does a head
#: start here?"; this one asks "did one just finish?", which is what a left-to-right
#: build needs to notice a head the moment its last character lands.
_MARKER_HEAD_END_RE = re.compile(
    "(?:"
    + "|".join(
        f"(?i:{re.escape(p)})" if marker_prefix_is_case_insensitive(p) else re.escape(p)
        for p in MARKER_PREFIXES
    )
    + ")$"
)


def _paired_marker_closer(text: str, index: int) -> int | None:
    """The closer that ends the head open at *index*, or ``None`` if it never closes.

    DEPTH, not the first closer. Taking the first one let a citation cancel the head it sat
    inside: ``[OPTION-ACTIONS: close=See [1] later`` treated the ``]`` of ``[1]`` as the
    marker's own, excised ``close=See [1]`` with it, and released `` later`` as answer text
    -- a marker head streamed to the user as prose. This is the same predicate
    :func:`_unclosed_marker_flags` already applies on the batch path, where the identical
    citation case was fixed; only this streaming twin still took the first closer.

    Linear overall: the caller resumes AFTER the returned offset, so each character is
    scanned by at most one of these walks.
    """
    depth = 1
    position = index
    end = len(text)
    while position < end:
        char = text[position]
        if char in MARKER_OPENERS:
            depth += 1
        elif char in MARKER_CLOSERS:
            depth -= 1
            if depth == 0:
                return position
        position += 1
    return None


def excise_marker_spans(text: str) -> str:
    """*text* with each marker head-to-closer span cut out, built in ONE pass.

    LINEAR, and that is the point. Re-deriving the whole string per span --
    ``residue[:head] + residue[closer + 1:]`` in a loop -- would cost O(n*k) in an
    O(n) copy each time. The streaming hold is model-controlled and unbounded until
    a newline arrives, so one long line of ``"[OPTIONS: a] y "`` repeated
    accumulates the entire line and then pays that quadratic at the flush, on the
    gateway's event loop. :func:`_unclosed_marker_flags` refuses the same shape on
    the batch path; both land here together, so neither fixes anything on main.

    A head whose bracket CLOSED is excised span-wise and the rest kept, and an
    UNTERMINATED head drops everything from the head onward -- the contract this
    function is specified to have, matching the strip the consumer stack will call
    it for.

    Appending character by character rather than jumping between ``finditer`` hits
    is deliberate: excising a span JOINS the text on either side of it, and that
    join can spell a head that NEITHER side contained. ``[OPTI[OPTIONS: a]ONS: b]``
    excises the inner marker and leaves ``[OPTIONS: b]``, a head formed entirely at
    the seam. A pass that only visited heads found in the original string would walk
    straight past it and release raw protocol text. Building left to right and
    asking after each character whether a head just COMPLETED catches the seam case
    for free, because the seam is simply where the next character lands.

    Completion order is start order here, so "the head that finishes first" is also
    "the leftmost head" that the superseded loop selected: every head begins with
    ``[`` and no head CONTAINS a second one, so two heads can never overlap.
    """
    kept: list[str] = []
    index = 0
    end = len(text)
    while index < end:
        char = text[index]
        kept.append(char)
        index += 1
        if char not in _MARKER_HEAD_FINAL_CHARS:
            continue
        window = "".join(kept[-_MARKER_HEAD_MAX_LEN:])
        head = _MARKER_HEAD_END_RE.search(window)
        if head is None:
            continue
        del kept[len(kept) - (head.end() - head.start()) :]
        closer = _paired_marker_closer(text, index)
        if closer is None:
            return "".join(kept)
        index = closer + 1
    return "".join(kept)


#: Prefix closures of the marker grammars, for
#: :func:`split_trailing_protocol_suffix`'s unfinished-marker probe: a tail is
#: a STILL-STREAMING marker only when every byte it holds so far could extend
#: into a complete marker. ``[OPTIONS`` must be followed by ``:`` and then a
#: PREFIX CLOSURE of :data:`OPTIONS_RE_TRAILER`'s body (DOTALL; ``[`` admitted
#: only when :data:`_MARKER_BODY_TEMPER` admits it) -- otherwise deliberately
#: LOOSER than that body, and the one place the "spelled once" rule in
#: :data:`_MARKER_BODY_LINE` does not apply. It has to be: a prefix of a legal
#: body need not itself be a legal body. ``[OPTIONS: A ]`` mid-stream holds a
#: closer that satisfies neither half of the closer rule YET, and becomes legal
#: the moment ``| B]`` arrives, so a probe spelled as the real body would call
#: that tail dead and publish the marker as raw text. Widening this to the
#: grammar is what ``test_options_marker_closers.py``'s
#: ``test_closer_inside_an_unfinished_label_is_still_unfinished`` forbids.
#: The temper is the ONE part that must agree: a nested head sits at a position
#: already in the buffer, so no continuation can rescue such a fragment.
#: ``[STEERING`` follows the steer-ack
#: grammar (``messaging/driver.py``): whitespace gap, literal ``steer-``, a
#: nonempty hex/dash id, then an optional ``:`` summary -- spelled as nested
#: optionals so every cut point of the literal run is admitted, while a tail
#: that diverges from the grammar (``[OPTIONSDOC``, ``[STEERING
#: acknowledgment``, ``steer-:``) is prose and stays visible. Case-sensitive
#: on purpose: these probe the exact sentinels the detach walk locates.
_OPTIONS_TAIL_PREFIX_RE = re.compile(
    rf"\[OPTIONS(?::(?:[^[]|\[{_MARKER_BODY_TEMPER})*)?\Z",
    re.DOTALL,
)
_STEERING_TAIL_PREFIX_RE = re.compile(
    r"\[STEERING(?:\s+(?:s(?:t(?:e(?:e(?:r(?:-(?:[0-9a-f-]+(?:\s*(?::\s*.*)?)?)?)?)?)?)?)?)?)?\Z",
    re.DOTALL,
)
_OPTION_ACTIONS_TAIL_PREFIX_RE = re.compile(
    rf"\[OPTION-ACTIONS(?::(?:[^[]|\[{_MARKER_BODY_TEMPER})*)?\Z",
    re.DOTALL | re.IGNORECASE,
)

#: Third element: the LOCATOR for this head. A lowercased COPY cannot be searched --
#: ``'\u0130'.lower()`` is two codepoints, so a length-changing fold shifts every index.
_MARKER_SENTINELS = (
    ("[STEERING", _STEERING_TAIL_PREFIX_RE, re.compile(re.escape("[STEERING"))),
    # Its own entry, not a case of ``[OPTIONS``: the two literals diverge at ``S`` vs
    # ``-``, so an action head is invisible to that sentinel.
    (
        "[OPTION-ACTIONS",
        _OPTION_ACTIONS_TAIL_PREFIX_RE,
        re.compile(re.escape("[OPTION-ACTIONS"), re.IGNORECASE),
    ),
    ("[OPTIONS", _OPTIONS_TAIL_PREFIX_RE, re.compile(re.escape("[OPTIONS"))),
)


def _head_starts(locator: "re.Pattern[str]", text: str) -> list[int]:
    """Ascending starts of every *locator* occurrence in *text*, in TEXT space.

    Indices come from the matches rather than from a folded copy, so a
    case-insensitive head is located without moving any offset.
    """
    return [match.start() for match in locator.finditer(text)]


def _rightmost_head_start(starts: list[int], span: int, end: int) -> int:
    """Rightmost start whose whole occurrence fits in ``text[:end]``, or ``-1``.

    CONSUMES *starts*, so one leftward walk costs one scan in total rather than a
    fresh scan per step. Dropping a start here is safe because *end* only moves
    left, so a start too far right for this step is too far right for every later
    one as well.
    """
    while starts and starts[-1] + span > end:
        starts.pop()
    return starts.pop() if starts else -1


def _rightmost_unfinished_marker(text: str) -> int:
    """Start of the rightmost tail that is a strict prefix of a marker grammar.

    Occurrences are probed RIGHTMOST-FIRST so label bytes that merely contain
    a sentinel (a bare ``[OPTIONS`` without its colon is legal label content)
    cannot shadow the genuine fragment start to their left. Each probe is
    cheap: each head's occurrences are located ONCE and then consumed right to
    left, the ASCII ``]`` gate is one precomputed ``rfind``, and the prefix
    regexes are anchored at the occurrence and die on the first diverging byte --
    so an adversarial buffer repeating failing sentinels walks linearly. Returns
    ``-1`` when no admissible occurrence exists.
    """
    last_close = text.rfind("]")
    cursors = []
    for sentinel, prefix_re, locator in _MARKER_SENTINELS:
        starts = _head_starts(locator, text)
        pos = _rightmost_head_start(starts, len(sentinel), len(text))
        if pos != -1:
            cursors.append((pos, sentinel, prefix_re, starts))
    while cursors:
        cursors.sort(key=lambda cursor: cursor[0])
        pos, sentinel, prefix_re, starts = cursors.pop()  # rightmost overall
        if pos <= last_close:
            # ASCII-only unfinished gate (see the closer comment in
            # ``split_trailing_protocol_suffix``): a ``]`` at/after this
            # occurrence means the tail is not still-streaming -- and every
            # remaining occurrence sits further left of that closer too.
            break
        if prefix_re.match(text, pos) is not None:
            return pos
        # Case-INSENSITIVE for the head that needs it: a case-sensitive re-probe
        # walked past a lowercase occurrence and reported no marker at all.
        nxt = _rightmost_head_start(starts, len(sentinel), pos)
        if nxt != -1:
            cursors.append((nxt, sentinel, prefix_re, starts))
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


def _marker_match_anchor(text: str, idx: int) -> int:
    """Offset the trailer patterns must be anchored at to still see *idx*'s marker.

    A line-leading wrapper belongs to the marker it opens, but anchoring on the
    wrapper is not enough: the patterns spell that lead as ``^[ \\t]*(?P<lwrap>...)``,
    and ``^`` matches only at a line start, so an anchor placed AFTER the indent
    leaves both alternatives unmatchable -- the group cannot match mid-line and the
    head is not at the wrapper. An indented wrapped trailer then stays in the
    visible half, where a length rotation splits it and posts raw protocol text.
    The superseded ``search`` form absorbed the indent through that same
    ``[ \\t]*``, so returning the line start is what preserves its boundary.
    """
    wrapper = _leading_wrapper_start(text, idx)
    if wrapper == idx:
        return idx
    return text.rfind("\n", 0, wrapper) + 1


def split_trailing_protocol_suffix(text: str) -> tuple[str, str]:
    """Detach protocol trailers before a renderer length-splits ``text``.

    A still-streaming ``[STEERING``, ``[OPTIONS`` or ``[OPTION-ACTIONS``
    fragment normally breaks the trailer regexes' end-of-buffer anchor. If a complete OPTIONS
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

    # Both trailer forms are consulted, and the walk repeats until NEITHER
    # matches, so a message ending in one marker preceded by the other keeps the
    # whole run together on the tail instead of leaving the earlier marker
    # exposed to a mid-token split.
    #
    # LINEAR, and it has to be. The shape this replaces re-sliced the whole
    # prefix and re-ran a ``\Z``-anchored search once per trailing marker, so a
    # tail of *k* markers cost O(n*k) on text the MODEL controls: measured
    # 4.9 s at k=4000 growing 4x per doubling, so ~16k markers clears a 25 s
    # watchdog and takes the gateway with it.
    #
    # One head scan, then a BACKWARD walk anchored at each head. ``endpos``
    # honours ``\Z`` (verified), so ``match(text, head, suffix_start)`` asks the
    # SAME question the old ``search(text[:suffix_start])`` asked while reading
    # only the one marker: the shared body cannot cross another head, so the
    # earlier heads the old leftmost search rejected are exactly the ones this
    # walk never reaches. Reuses the trailer patterns rather than a second
    # grammar, so a head or closer change still lands in one place.
    heads = [m.start() for m in _MARKER_TRAILER_HEAD_SCAN_RE.finditer(text)]
    for head in reversed(heads):
        if head >= suffix_start:
            continue
        anchor = _marker_match_anchor(text, head)
        if not any(
            pattern.match(text, anchor, suffix_start)
            for pattern in (OPTIONS_RE_TRAILER, OPTION_ACTIONS_RE_TRAILER)
        ):
            break
        suffix_start = anchor

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

#: Why a tool call was denied, for the in-band notice's cause-specific wording
#: (``dashboard.state.build_refusal_steer_notice``). Defined in this leaf rather
#: than in ``dashboard.state`` because the messaging core (``messaging.driver``
#: and the channel approval deciders) has to name a cause without importing the
#: dashboard: a decider that lets its prompt expire records
#: ``DENY_CAUSE_APPROVAL_TIMEOUT`` and the TurnDriver steers that cause before
#: it rejects. ``dashboard.state`` re-exports every name, so its importers are
#: unchanged.
DENY_CAUSE_POLICY = "policy"
DENY_CAUSE_INVALID_NAME = "invalid_name"
DENY_CAUSE_HOOK_ERROR = "hook_error"
DENY_CAUSE_BATCH_CASCADE = "batch_cascade"
DENY_CAUSE_APPROVAL_TIMEOUT = "approval_timeout"
DENY_CAUSE_APPROVAL_NO_BUDGET = "approval_no_budget"
DENY_CAUSE_APPROVAL_UNDELIVERABLE = "approval_undeliverable"

#: Upper bound on the best-effort in-band deny notice steered into a running
#: turn before a permission rejection goes back on the wire. Every deny site
#: (dashboard chat runner, native Slack handler, messaging TurnDriver) runs
#: ``reject_tool`` plus a SEL audit write AFTER the steer, and an unbounded await
#: on a backpressured ACP stdin would stall the reject that unblocks the turn.
#: One number so the three surfaces cannot drift apart.
STEER_NOTICE_BOUND_SECS = 5.0
