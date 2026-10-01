"""Stable conduct sections: text derived from config and the trusted runtime.

These renderers produce the deterministic blocks every session carries whatever its
memory holds -- the critical-rules contract, the runtime identity, the user profile,
the reply-style preferences, the workspace identity, the runtime refresh a follow-up
turn needs and the widget pointer an agent prompt's ``{{WIDGET_BLOCK}}`` resolves
to. Each takes its inputs as arguments and reads no transcript, store or file, so a
render is a pure function of config and the trusted dispatcher metadata.

The critical-rules contract and the UI-language renderers stay on
:mod:`kiro_crew.context`: the contract's text is pinned in place there, and the
UI-language catalog mirror is where its drift test names it.

New conduct blocks belong here; a block that reads memory or a transcript does not.
"""

from __future__ import annotations

import unicodedata
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kiro_crew.config.loader import KiroCrewConfig

# Display names for runtime environments, keyed by trusted dispatcher source
# tags and session namespaces. Kept close to the injection site so new
# transports can extend it alongside their dispatcher wiring.
_RUNTIME_DISPLAY = {
    "dashboard": "KiroCrew dashboard",  # brand-ok: prompt bytes the model reads
    "cron": "KiroCrew cron job",  # brand-ok: prompt bytes the model reads
    "subagent": "KiroCrew subagent",  # brand-ok: prompt bytes the model reads
    "taskrunner": "KiroCrew task runner",  # brand-ok: prompt bytes the model reads
    "background": "KiroCrew background",  # brand-ok: prompt bytes the model reads
    "heartbeat": "KiroCrew heartbeat",  # brand-ok: prompt bytes the model reads
    "cli": "CLI terminal",
    "slack": "Slack",
    "discord": "Discord",
    "telegram": "Telegram",
    "wecom": "WeCom",
    "weixin": "Weixin",
    "whatsapp": "WhatsApp",
    "feishu": "Feishu",
    "webex": "Webex",
    "teams": "Microsoft Teams",
    "imessage": "iMessage",
}


def _resolve_runtime_source(session_key: str, runtime_source: str | None = None) -> str:
    """Resolve the canonical runtime source key for a session.

    ``runtime_source`` is the authoritative transport for the current turn.
    It is intentionally separate from ``session_key``: a dashboard session can
    be resumed from Discord, and ``messaging.dm_scope="unified"`` deliberately
    removes the transport from the stable session key.

    Without an explicit source, infer the runtime from namespaced session keys.
    Unknown/bare keys retain the historical Slack fallback for legacy Slack
    thread timestamps.
    """
    source = (runtime_source or "").strip().lower()
    if source:
        return source

    if session_key.startswith("dashboard:") or session_key.startswith("dashboard_"):
        source = "dashboard"
    elif session_key.startswith("cron:") or session_key.startswith("cron_"):
        source = "cron"
    elif session_key.startswith("subagent:"):
        source = "subagent"
    elif session_key.startswith("taskrunner"):
        source = "taskrunner"
    elif session_key == "_bg":
        source = "background"
    elif session_key == "_hb":
        source = "heartbeat"
    elif session_key == "cli_chat":
        source = "cli"
    else:
        source = "slack"
        lowered_key = session_key.lower()
        for namespace in (
            "discord",
            "telegram",
            "wecom",
            "weixin",
            "whatsapp",
            "feishu",
            "webex",
            "teams",
            "imessage",
            "slack",
        ):
            if lowered_key.startswith((f"{namespace}:", f"{namespace}_")):
                source = namespace
                break
    return source


def _runtime_display_name(session_key: str, runtime_source: str | None = None) -> str:
    """Map a session_key to a human-readable runtime name.

    Display mapping over :func:`_resolve_runtime_source` — resolution
    semantics live there so the [RUNTIME] line and every source-keyed
    decision (e.g. the diff-block rule selection) can never disagree.
    """
    return _RUNTIME_DISPLAY.get(
        _resolve_runtime_source(session_key, runtime_source),
        _resolve_runtime_source(session_key, runtime_source),
    )


# Slug → prompt-ready description maps for the [USER PROFILE] block. Slugs are
# the values enforced by the dashboard.user_role / dashboard.user_technical_level
# enums in handlers/core.py _EDITABLE_CONFIG; keep the three places in sync.
# "" role and "" level contribute nothing — the block only names what the user
# actually told us. "other" has no entry here on purpose: it routes to the
# free-text dashboard.user_role_other instead (see _role_description).
_USER_ROLE_DESCRIPTIONS: dict[str, str] = {
    "developer": "a software developer",
    "designer": "a UX / product designer",
    "product-manager": "a product manager",
    "data-ml": "a data / ML practitioner",
    "it-ops": "an IT / operations professional",
}

_TECHNICAL_LEVEL_DESCRIPTIONS: dict[str, str] = {
    "codes": "writes code daily — comfortable with full technical depth",
    "somewhat-technical": ("somewhat technical — reads some code but doesn't write it daily"),
    "non-technical": "not technical — prefers plain language over code and jargon",
}

#: Longest free-text role rendered into the prompt. Mirrors the ``max_len`` on
#: ``dashboard.user_role_other`` in handlers/core.py, re-applied here because the
#: writer's validation is not the only way a value reaches this field — a
#: hand-edited config.json bypasses the PATCH allowlist entirely.
_ROLE_OTHER_MAX_LEN = 60


def _sanitize_free_text_role(raw: str) -> str:
    """Return ``raw`` reduced to a single safe prompt-sized phrase, or ``""``.

    ``dashboard.user_role_other`` is the ONLY user-authored string in the
    [USER PROFILE] block — every other value is a slug the UI picks — so it is
    the only one that could carry newlines and bracket markers shaped like the
    block delimiters the model is taught to trust. Whitespace (including
    newlines and tabs) is collapsed to single spaces, every character outside
    :func:`_is_allowed_role_char` is dropped, and the result is length-capped. A
    value that sanitizes to nothing is treated as unset rather than rendered
    empty.
    """
    if not raw:
        return ""
    cleaned = "".join(ch if not ch.isspace() and _is_allowed_role_char(ch) else " " for ch in raw)
    cleaned = " ".join(cleaned.split())
    return cleaned[:_ROLE_OTHER_MAX_LEN].strip()


#: Punctuation a job title legitimately needs. ``#`` earns its place from real
#: titles ("C# Developer"), as ``+`` does for "C++". Deliberately excludes ``:`` —
#: ``LABEL:`` is the shape the protocol markers themselves use — and every
#: bracket form.
_ROLE_PUNCT_ALLOWED = "-'.,/&()+_#"


def _is_allowed_role_char(ch: str) -> bool:
    """True for the only characters free text may contribute to the prompt.

    An ALLOWLIST, per BSC1 Input Validation: a denylist of known-bad characters
    is always incomplete, and this one was. The previous version dropped ASCII
    ``[`` and ``]`` but passed every Unicode lookalike — ``】`` U+3011, ``］``
    U+FF3D, ``〕`` U+3015 and others — each of which renders as a bracket and so
    could still impersonate a ``[BLOCK]`` delimiter in the assembled prompt.
    Inverting the test closes that whole tail instead of three codepoints.

    Letters and combining marks are admitted by Unicode category rather than an
    ASCII range, so a non-Latin job title survives; the product ships ten UI
    locales and a Latin-only filter would silently blank those users' input.
    """
    if ch in _ROLE_PUNCT_ALLOWED:
        return True
    category = unicodedata.category(ch)
    return category.startswith("L") or category.startswith("M") or category == "Nd"


def _role_description(cfg: "KiroCrewConfig") -> str:
    """Resolve the role half of the profile block to a prompt-ready phrase.

    A picked slug maps through ``_USER_ROLE_DESCRIPTIONS``. "other" has no
    canned description, so it falls back to the free text the user typed —
    quoted, and framed as something they said rather than as a fact the product
    asserts, since nothing validated that it names a real profession.
    """
    role = cfg.dashboard.user_role
    if role == "other":
        custom = _sanitize_free_text_role(cfg.dashboard.user_role_other)
        return f'described by the user as "{custom}"' if custom else ""
    return _USER_ROLE_DESCRIPTIONS.get(role, "")


def _build_user_profile_section(cfg: "KiroCrewConfig") -> str:
    """Build the [USER PROFILE] block from onboarding answers.

    Collected by onboarding step 2 (and editable in Settings > General >
    About You), stored as dashboard.user_role / dashboard.user_role_other /
    dashboard.user_technical_level.
    Returns "" when the user skipped both questions so un-profiled installs
    see byte-identical context.

    The wording deliberately calibrates HOW the agent communicates, not WHAT
    it may do — a designer who asks for code must still get code.
    """
    role_desc = _role_description(cfg)
    tech_desc = _TECHNICAL_LEVEL_DESCRIPTIONS.get(cfg.dashboard.user_technical_level, "")
    if not role_desc and not tech_desc:
        return ""
    facts: list[str] = []
    if role_desc:
        facts.append(f"The user is {role_desc}.")
    if tech_desc:
        facts.append(f"Technical comfort: {tech_desc}.")
    return (
        "[USER PROFILE]\n" + " ".join(facts) + "\n"
        "Calibrate communication to this profile: match vocabulary, depth of "
        "explanation, and examples to their role and technical comfort. Explain "
        "concepts outside their domain plainly; skip basic explanations inside "
        "it. This adjusts HOW you communicate, never WHAT you can do — if they "
        "ask for code, provide code.\n"
        "[End of user profile]\n\n"
    )


_RESPONSE_PREFERENCES_HEADER = "[RESPONSE PREFERENCES — MANDATORY]"
_RESPONSE_PREFERENCES_FOOTER = "[END RESPONSE PREFERENCES]"


def _reply_style_rules(level: str) -> str:
    """Return the rule text for one ``dashboard.verbosity`` level.

    ``""`` for ``default`` and for any value the enum does not know, so a
    config edited by hand to an unrecognised level injects nothing rather
    than a half-formed block.
    """
    if level == "ultra":
        return (
            "## Reply style: Ultra-Brief (ADHD reader)\n\n"
            "Before responding, simulate the reader: they will read the "
            "first 2 sentences, scan for bold text and code blocks, then "
            "close the tab. Anything they won't reach is wasted tokens. "
            "Structure for THAT reader, not an attentive one.\n\n"
            "You have a strong bias toward completeness. Override it. The "
            "reader's time costs more than your thoroughness. An answer "
            "that's 80% complete in 2 lines beats 100% complete in 20 "
            "lines. Missing a caveat is acceptable. Missing an edge case "
            "is acceptable.\n\n"
            "Rules:\n"
            "- Open with THE answer in 1–2 sentences. Bold the single most "
            "critical point.\n"
            "- Supporting bullets only if the reader would be STUCK without "
            "them. Max 3. Each bullet is one short sentence.\n"
            '- Take a position. Name your pick. Resolve "it depends" '
            "immediately.\n"
            "- Do NOT add: tables, headers, numbered lists > 3 items, "
            '"common pitfalls", "also consider", multi-section layouts, '
            'or any content that fails the test: "would the reader be '
            'stuck without this line?"\n'
            "- Code blocks and commands are the answer — never cut them.\n"
            "- Stakes change what you must not omit, never the length: "
            "security warnings and irreversible-action confirmations "
            "always appear, each as one line naming the call, the risk, "
            "and whether it can be undone; the mechanism and the failure "
            "modes are not required. Ordered multi-step instructions "
            "where a dropped step causes a mistake stay complete, and "
            "code, commands, paths, identifiers and error strings stay "
            "verbatim.\n"
            "- When the user ASKS for something long (design doc, tutorial, "
            "full implementation), ignore these constraints and deliver "
            "what was asked.\n"
            "- Required output formats are sacred and never cut: "
            "[OPTIONS:] lines, diff blocks for file changes, full PR/MR "
            "URLs, and any format the rendering surface "
            "needs. These go in their required position regardless of "
            "brevity.\n"
            "- Preserve the user's language."
        )
    if level == "concise":
        return (
            "## Reply style: Concise\n\n"
            "Concise mode is on. Reduce length without losing substance:\n"
            "- Lead with the answer or result. Skip preamble, filler, and "
            'pleasantries (e.g. "Sure!", "Great question", "I\'d be happy '
            'to", "basically", "let me…").\n'
            "- Keep progress signal brief, not absent: a short high-level note "
            "of what you're doing or will do next is fine (it builds confidence "
            "about what's happening underneath), but skip step-by-step "
            "play-by-play and low-level detail that isn't needed for a quick "
            "understanding. Favor the outcome; mention process only at a high "
            "level.\n"
            "- Prefer short sentences and fragments; cut hedging and "
            "repetition; state each fact once.\n"
            "- Structure over sprawl: tight bullets, surface the "
            "recommendation, take a position instead of dumping every option.\n"
            "- Don't paste long logs, file dumps, or command output unless "
            "asked — quote the shortest decisive line.\n"
            "- Keep code, commands, paths, identifiers, and error strings "
            "verbatim and complete. Brevity is for prose, never correctness.\n"
            "- Preserve the user's language; compress the style, not the "
            "content.\n\n"
            "Stakes change what concise mode must not omit, never how "
            "long it may run: security warnings and irreversible-action "
            "confirmations always appear, each as one line naming the "
            "call, the risk, and whether it can be undone; the mechanism "
            "and the failure modes are not required. Likewise, multi-step "
            "instructions where order or omissions could cause a mistake "
            "stay complete."
        )
    if level == "answer_only":
        return (
            "## Reply style: Answer Only\n\n"
            "Say only the answer. Write for a five-year-old: the smallest "
            "words that are still true, one idea per sentence, no term that "
            "is not itself the fact. Short paragraphs. Break lines only where "
            "structure needs it (list, step, heading) — never one sentence "
            "per line.\n\n"
            "Run three checks, in order, before you write:\n\n"
            "1. Shape check. Does the answer have a shape — steps, "
            "before/after, cases and verdicts, sizes? Then draw it. A "
            "picture is payload, not prose: it replaces the words, never "
            "repeats them. Plain labels and numbers: a markdown table. With "
            "an Inline Widgets section, a picture needing color, layout or "
            "motion IS an inline widget (an HTML artifact when large) — never "
            "a table of sentences. Elsewhere (a chat channel, a CLI) a plain "
            "table: widget markup lands there as raw text. A picture holds labels of one "
            "to three words and numbers, never a sentence. If a sentence is "
            "needed, it goes under the picture, once.\n"
            "2. Word check. Each sentence: at most 12 words. Each word: one "
            "the user has used, or one a child knows. A word that fails "
            "both is swapped, unless it names a real part: keep that name, "
            "glossed in three words once.\n"
            "3. Cut check. Delete: preamble, what you did, where you found "
            "it, why, options you rejected, caveats, offers to help. Keep: "
            "the answer; code, commands and paths the user asked for or "
            "must run, verbatim; every step of an ordered procedure, in "
            "order; any required format ([OPTIONS:], diffs, PR links); one "
            "undo line for anything destructive; one risk line for anything "
            "touching security, data or spend.\n\n"
            "Asked why? Show the real parts by name and how they connect: "
            "a chain (A -> B -> C) or a part-and-job table. The objection is "
            "the step where it breaks. The reasons, numbered, one short line "
            "each, naming the parts. An everyday picture needs a map: each "
            "picture thing beside its real part. End: what it is, one line. "
            "Word check still runs. Cut check spares the chain, the map and "
            "the reasons. This reply may run long.\n"
            'Asked for depth (a doc, a walkthrough, "in detail")? This '
            "mode is off for that reply.\n\n"
            "Reply in the user's language."
        )
    return ""


def _response_preferences_apply(session_key: str, runtime_source: str | None = None) -> bool:
    """Whether the reply-style block belongs in this session's context.

    The rules describe how the PERSON wants to read replies, so they apply to
    every session whose final message a person reads — dashboard, every
    messaging channel, a cron digest. A ``subagent:`` session is the one kind
    whose final message is read by its PARENT agent instead: the parent needs
    the caveats and edge cases the ``ultra`` and ``answer_only`` levels tell
    the writer to drop, so the block is withheld there. Resolved through the
    same runtime-source seam as ``[RUNTIME]``, so a sub-agent is recognised the
    way every other transport is.
    """
    return _resolve_runtime_source(session_key or "", runtime_source) != "subagent"


def _build_response_preferences_section(cfg: "KiroCrewConfig") -> str:
    """Build the [RESPONSE PREFERENCES] block from ``dashboard.verbosity``.

    The setting describes how the PERSON wants replies to read, so it is
    chrome for every person-facing agent — built-in, custom, and cron — rather
    than a token an agent prompt has to opt into. Sub-agents are the exception:
    their final message is read by a parent agent that needs full detail.

    ``build_message`` must mint this trusted frame only after it scrubs session
    context. Both frame markers are structural markers, so placing the genuine
    frame inside the scrubbed context would neutralize it along with forgeries.
    The earlier ``{{VERBOSITY_BLOCK}}`` token reached 7 of the 84 agent specs on
    one real install; the 77 others ran with the setting silently ignored.

    The wrapper is deliberately loud (a bracketed MANDATORY header, an explicit
    precedence sentence) because the block competes with a long agent prompt
    that carries its own style guidance; a bare ``##`` heading in the middle of
    the context would have no stated rank against it.

    Returns ``""`` when the level is ``default`` or unrecognised, so installs
    that never touched the setting see byte-identical context.
    """
    level = getattr(getattr(cfg, "dashboard", None), "verbosity", "default")
    rules = _reply_style_rules(level if isinstance(level, str) else "default")
    if not rules:
        return ""
    return (
        f"{_RESPONSE_PREFERENCES_HEADER}\n"
        "The user chose how your replies must read. These rules bind EVERY "
        "reply in this session, on every surface, for every agent, and they "
        "OUTRANK any response-style guidance in your agent prompt. They shape "
        "prose only: code, commands, paths, identifiers, error strings and any "
        "required output format stay exactly as they are.\n\n"
        f"{rules}\n"
        f"{_RESPONSE_PREFERENCES_FOOTER}\n\n"
    )


def runtime_identity_block(agent_label: str, runtime: str) -> str:
    """The session-start ``[CURRENT AGENT]`` / ``[RUNTIME]`` block.

    Without it the model cannot tell the dashboard from kiro-cli and may tell the
    user to "go to the dashboard" when it IS the dashboard.
    """
    return (
        f"[CURRENT AGENT] {agent_label}\n"
        f"[RUNTIME] {runtime}\n"
        f"You ARE this agent running in {runtime}. "
        f"Prefer solutions native to this runtime. "
        f"Only suggest switching interfaces if the user asks "
        f"or the task requires it.\n\n"
    )


def workspace_identity_block(ws_name: str, ws_path: object) -> str:
    """The ``[WORKSPACE IDENTITY]`` block (the ``kirocrew`` agent only).

    Deliberately does NOT advertise scope="workspace" for lessons. That scope does
    not reach a prompt, so telling the agent to use it would make it save
    corrections that silently never apply.
    """
    return (
        "[WORKSPACE IDENTITY]\n"
        f"You are operating in workspace: {ws_name}\n"
        f"Workspace path: {ws_path}\n"
        "A workspace is a shared space holding your knowledge base, "
        "preferences, project notes, daily history and files.\n\n"
        "Lessons saved with the learn_add tool apply across all "
        "workspaces. Use them for durable corrections and preferences, "
        "not for one-off facts.\n"
        "[End of workspace identity]\n\n"
    )


def runtime_refresh_blocks(session_key: str, runtime_source: str) -> list[str]:
    """The follow-up turn's ``[RUNTIME]`` refresh from trusted dispatcher metadata.

    The stable session key describes conversation identity, not necessarily the
    interface carrying this turn. Cross-surface resume keeps the original key (for
    native ACP history fidelity), so every follow-up names its runtime again.
    """
    runtime = _runtime_display_name(session_key, runtime_source)
    blocks = [
        f"[RUNTIME] {runtime}\n"
        "This is the interface carrying the current user message and is "
        "authoritative for this turn, even if the session originated on "
        "another interface.\n\n"
    ]
    # A session that started on the dashboard carries the relaxed
    # diff-block rule from session start, but this turn may arrive
    # from a surface that renders no tool cards. Re-assert the hard
    # mandate for THIS turn. Deliberately asymmetric: only the
    # channel mandate is ever injected mid-session (a dashboard turn
    # in a channel-started session at worst duplicates a diff, which
    # is cosmetic; the inverse — a channel turn under the relaxed
    # rule — leaves the user with no record of what changed).
    if _resolve_runtime_source(session_key, runtime_source) != "dashboard":
        blocks.append(
            "For THIS turn: this surface renders no tool cards, so "
            "after ANY file change you MUST include a ```diff code "
            "block in your message text — it is the only place the "
            "user can see what changed.\n\n"
        )
    return blocks


def widget_block(density: str) -> str:
    """What an agent prompt's ``{{WIDGET_BLOCK}}`` resolves to on a dashboard surface.

    ``density == "more"`` is the full pointer; every other value is the short one
    that prefers plain markdown.
    """
    if density == "more":
        return (
            "## Inline Widgets\n\n"
            "You can render rich HTML inline using "
            '`<mcwidget title="Title">HTML</mcwidget>` tags. Load the `widgets` '
            "skill for theme variables, format rules, interactive widgets, and "
            "best practices when emitting one. The frame is themed: its body "
            "already carries the active theme's background and text color, so "
            "color every surface with the theme's CSS variables, never a fixed "
            "palette (`bg-white`, `bg-green-50`, a literal hex), and always set "
            "a background together with its text color. A half-set pair renders "
            "unreadable in dark mode. Animate only when the motion carries "
            "information (change over time, ordered steps, flow, "
            "before/after) or the user asks; if one frozen frame loses "
            "nothing, keep it still. "
            "An animation longer than a few seconds gets a pause control; "
            "every animation respects the reduced-motion setting and stops "
            "when done unless purely decorative.\n\n"
            "## Artifacts\n\n"
            "Every widget auto-registers as an UNPINNED artifact as its "
            "response segment finalizes — do not "
            "`@kirocrew-core/artifact_save` one you rendered. The user's "
            "star pins it; unpinned ones are pruned oldest-first, and "
            "registration is skipped in a restricted (incognito or "
            "temporary) session. Save explicitly only for content you "
            "never emitted as a widget; iterate with `artifact_get`, "
            "`artifact_update`, `artifact_revert`. Load the `artifacts` skill."
        )
    return (
        "## Inline Widgets\n\n"
        "You can render rich HTML inline using `<mcwidget>` tags, but prefer "
        "plain markdown by default. Load the `widgets` skill when a widget is "
        "genuinely warranted. The frame is themed: color every surface with "
        "the theme's CSS variables, never a fixed palette, and set each "
        "background together with its text color. Animate only when the "
        "motion carries information or the user asks; add a pause "
        "control and respect the reduced-motion setting.\n\n"
        "## Artifacts\n\n"
        "Every widget auto-registers as an unpinned artifact, so do not "
        "`@kirocrew-core/artifact_save` one you rendered. Load the "
        "`artifacts` skill to save other content, iterate or list."
    )
