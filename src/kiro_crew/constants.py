"""Shared constants used across cli and gateway modules."""

import os
import re

# Retained for backward compatibility; intentionally empty in the public build
# (no internal registry/package). Callers treat empty as "skip".
ARCC_REGISTRY = ""
ARCC_TOOLBOX_PACKAGE = ""

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
# defined here ONCE and imported everywhere (was five hand-mirrored copies kept
# in sync only by a "keep in lockstep" comment; a one-character slip flips the
# flag semantics or reintroduces the ReDoS class below on a single surface).
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
OPTIONS_RE_LINE = re.compile(r"\[OPTIONS:((?:[^[\n]|\[(?!OPTIONS:))*)\][ \t]*$", re.MULTILINE)

# TRAILER (``re.DOTALL``, ``\Z`` anchor) — for the Discord/Telegram/WeCom
# renderers, which match the marker only at the very END of the message and
# allow it to span newlines (the body keeps ``[^[]`` because the old ``.*``
# already spanned newlines under DOTALL). Trailing ``\s*`` before ``\Z``.
OPTIONS_RE_TRAILER = re.compile(r"\[OPTIONS:((?:[^[]|\[(?!OPTIONS:))*)\]\s*\Z", re.DOTALL)
