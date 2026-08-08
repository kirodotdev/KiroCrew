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
