"""Shared constants used across cli and gateway modules."""

import os
import re

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
# turn). Sized to match the inner ACP ``_DEFAULT_PROMPT_TIMEOUT`` (7200s) in
# ``acp/client.py`` so the dashboard layer doesn't bound below the transport.
# Wedged-session detection is handled by ``_STALE_TURN_TIMEOUT`` (90s, also in
# ``acp/client.py``); this cap is the upper safety ceiling for genuinely runaway
# work, not a "this turn took too long" guard.
CHAT_TURN_TIMEOUT = 7200.0

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


# ── Canonical "[OPTIONS: a | b | c]" trailer parsers ────────────────────────
# The agent emits a trailing ``[OPTIONS: choice1 | choice2 | ...]`` marker that
# every surface renders as tappable choices. Two variants exist because the
# surfaces scan differently, but their GRAMMAR must stay identical — so both are
# defined here ONCE and imported everywhere: a hand-mirrored copy risks a
# one-character slip that flips the flag semantics or reintroduces the ReDoS
# class below on a single surface.
#
# Body: a TEMPERED greedy repetition that allows every bracket EXCEPT a ``[``
# that begins a fresh ``[OPTIONS:``. This matters for ReDoS (py/polynomial-redos):
# a plain greedy ``.*`` body can itself consume a ``[`` that also starts the outer
# ``[OPTIONS:`` literal, so over untrusted text with many ``[OPTIONS:`` prefixes
# ``search()``/``findall()`` re-explore the body from each position — polynomial
# backtracking. The tempered body is unambiguous (linear) while still capturing a
# literal ``]`` and any other inner ``[`` inside an option ("Fix [x] logging",
# "a[1]"). This parser runs over untrusted LLM/relayed text before Slack, the
# dashboard, Discord, Telegram, and WeCom render it.
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
#: ReDoS profile is unchanged from the previous literal ``\]``. The class shares
#: no character with the trailing ``[ \t]*`` / ``\s*``, and the tempered body
#: already admitted ``]`` via ``[^[\n]``, so adding these three codepoints
#: introduces no new ambiguity.
MARKER_CLOSERS = "]\u3011\uff3d\u3015"
_MARKER_CLOSE_CLASS = "[" + re.escape(MARKER_CLOSERS) + "]"

OPTIONS_RE_LINE = re.compile(
    rf"\[OPTIONS:((?:[^[\n]|\[(?!OPTIONS:))*){_MARKER_CLOSE_CLASS}(?:\([^\s()]*\))?[ \t]*$",
    re.MULTILINE,
)

# TRAILER (``re.DOTALL``, ``\Z`` anchor) — for the Discord/Telegram/WeCom
# renderers, which match the marker only at the very END of the message and
# allow it to span newlines (the body keeps ``[^[]`` because the old ``.*``
# already spanned newlines under DOTALL). Trailing ``\s*`` before ``\Z``. Carries
# the same optional markdown-link close as LINE (same ``[^\s()]`` inner class, so it
# shares no character with the trailing ``\s*`` — ReDoS-safe) so the grammar stays
# identical.
OPTIONS_RE_TRAILER = re.compile(
    rf"\[OPTIONS:((?:[^[]|\[(?!OPTIONS:))*){_MARKER_CLOSE_CLASS}(?:\([^\s()]*\))?\s*\Z",
    re.DOTALL,
)

# CONTROL-TAG HTML COMMENTS — canonical grammar (single source of truth).
#
# Agent control tags ride in HTML comments, which the dashboard's markdown
# pipeline renders as nothing (rehype-raw emits comment nodes the react
# renderer skips). Three families exist in ``src/``:
#   * ``<!-- keep-visible -->``       — collapse-all exemption (#7948)
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


def split_trailing_protocol_suffix(text: str) -> tuple[str, str]:
    """Detach protocol trailers before a renderer length-splits ``text``.

    A still-streaming ``[STEERING`` or ``[OPTIONS`` fragment normally breaks
    :data:`OPTIONS_RE_TRAILER`'s end-of-buffer anchor. If a complete OPTIONS
    block immediately precedes that fragment, detaching only the unfinished
    marker leaves the complete block eligible for a mid-token chunk split.
    Return the visible prefix plus the entire protocol suffix so renderers can
    keep both markers together on the surviving tail.
    """
    suffix_start = len(text)
    idx = max(text.rfind("[STEERING"), text.rfind("[OPTIONS"))
    # DELIBERATELY ASCII-ONLY -- do not widen this to ``MARKER_CLOSERS``.
    # This asks "is the tail an UNFINISHED marker?", and mere PRESENCE of a
    # closer is not completeness: a closer sitting inside a still-streaming
    # label (``[OPTIONS: Use 】 the bracket``) would read as finished, the
    # fragment would not be detached, and a length rotation could split the
    # marker so raw fragments render and the pills are lost. Completeness is
    # decided by ``OPTIONS_RE_TRAILER`` on the next line, which DOES accept the
    # lookalikes -- so a complete lookalike-closed block is still pulled into
    # the suffix. Widening here buys nothing (both paths already yield the same
    # split for a complete tail) and reintroduces that bug.
    if idx != -1 and "]" not in text[idx:]:
        suffix_start = idx

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
# Stamping the facts here means a reword of the header PROSE below can no longer
# silently break card rendering: the card reads this meta and the prose regexes
# demote to a legacy-scrollback fallback (issue #1792).
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

# AWS named-profile name shape — the SINGLE SOURCE OF TRUTH (#6063). The
# charset lived as seven hand-copied compiled patterns, and the copies
# reintroduced the missing-'+' defect twice (#6042, #6055). Every in-package
# validator now derives from these; the two standalone artifact-deploy scripts
# (which cannot import the package) embed AWS_PROFILE_NAME_PATTERN verbatim
# under a byte-equality drift guard in test/test_aws_profile_charset.py.
#
# Semantics (settled by #6051/#6055):
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
#: it already worked (#6514), one level up.
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
