"""The output contract a session starts with: the critical rules and the UI-language steer.

The critical-rules block is the dashboard and channel assistant's own output contract:
diff blocks for file changes (selected per runtime by :func:`_critical_rules_for`, from
the same trusted resolution that names the ``[RUNTIME]`` line), the ``[OPTIONS: ...]``
footer and the absolute-path and link rules. Both variants are fixed module constants,
never templated, so the marker-neutralization prefix check and the tests can treat
"a critical-rules block" as one of two strings. The ``[UI LANGUAGE]`` block steers the
language of tool-call purpose text to the configured dashboard language, admitted only
when it is tag-shaped and names a catalog the dashboard ships.

``ui_language_tag`` stays defined on :mod:`kiro_crew.context` because callers rebind it
there; :func:`_build_ui_language_section` reads it through the facade at call time.
No function here reads or writes builder state.

New output-contract text belongs here.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kiro_crew.config.loader import KiroCrewConfig

logger = logging.getLogger("kiro_crew.context")


#: Shape a ``dashboard.language`` value must have before it is injected into the
#: prompt. Deliberately a LOCAL check rather than an import of the dashboard
#: handler's ``_LANGUAGE_TAG_RE`` (context.py must not depend on the aiohttp
#: handler layer), and deliberately a superset-safe one: every tag that
#: validator accepts matches this, so the two cannot disagree about a legitimate
#: value. It exists because the writer's validation is not the only way a value
#: reaches this field — the config loader coerces whatever JSON holds into
#: ``str``, so a hand-edited ``"language": null`` arrives as the literal
#: ``"None"`` and ``["zh-CN"]`` as ``"['zh-CN']"``. Anything that is not
#: tag-shaped is dropped rather than pasted into the system prompt.
_UI_LANGUAGE_TAG_RE = re.compile(r"^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8}){0,2}$")


#: The language catalogs the dashboard actually ships — a mirror of the
#: non-dev-only entries in ``website/src/i18n/languages.ts``
#: (``SUPPORTED_LANGUAGES``), which stays the single source of truth:
#: ``test_context_ui_language.py`` parses that file and fails when this set
#: drifts, so adding a language remains a frontend data change plus the one
#: mechanical entry here that the drift test names explicitly.
#:
#: Membership is exact and case-sensitive because that is precisely how the
#: frontend restores a PERSISTED choice: ``resolveLanguage()`` accepts a stored
#: value only via ``isRestorableLanguage()`` → ``SUPPORTED_CODES.includes()``
#: (no lowering, no primary-subtag fallback — those apply only to *browser*
#: detection tags, which never reach this field). A stored ``zh-cn`` or
#: ``zh-TW`` therefore degrades to auto-detect in the SPA, and the backend must
#: reach the same verdict or the two disagree about the active language —
#: which is exactly the bug this set exists to prevent.
#:
#: The dev-only ``en-XA`` pseudolocale is deliberately ABSENT: in a production
#: build ``isRestorableLanguage()`` refuses to restore it (the chrome degrades
#: to auto-detect), and even in a dev build steering a model to write
#: pseudolocale prose is meaningless — the accent-and-bracket transform is
#: generated, not a language a model can write. Treating it as non-catalog
#: keeps injection behaviour identical across build modes.
_UI_LANGUAGE_CATALOGS = frozenset(
    {"en", "zh-CN", "hi", "es", "fr", "bn", "pt", "ru", "de", "ja", "ko", "it"}
)


def normalize_ui_language_tag(value: object, *, source: str = "language") -> str:
    """Admit an arbitrary value as a usable UI language tag, or return ``""``.

    The single gate a BCP-47 tag passes to become a *usable* UI language,
    whatever its provenance: the persisted ``dashboard.language`` (see
    :func:`ui_language_tag`) or a value handed over by a caller — e.g. a
    request-scoped hint carrying the language a browser already resolved for
    itself, which is the only way the backend can learn an implicitly chosen
    language at all. Both clear the identical bar deliberately: the frontend
    admits a language through exactly one gate, and a second, laxer copy here
    would let the two disagree about what the active language is.

    Rejected as ``""``: a non-string, a blank, a value that is not tag-shaped
    (``_UI_LANGUAGE_TAG_RE``), and a shape-valid tag naming no shipped catalog
    (``_UI_LANGUAGE_CATALOGS``) — the last because steering a model to a
    language the chrome around it cannot render puts two languages on one
    screen. ``""`` therefore always means "no usable language", never "English";
    callers must treat it as unknown.

    ``source`` labels the provenance in the debug line only — it never changes
    the verdict.
    """
    if not isinstance(value, str):
        return ""
    tag = value.strip()
    if not tag or not _UI_LANGUAGE_TAG_RE.match(tag):
        return ""
    if tag not in _UI_LANGUAGE_CATALOGS:
        # Debug, not warning: this fires on every context build for as long as
        # the value stays persisted, and the UI itself already degraded to
        # auto-detect — but without a line here an operator cannot distinguish
        # "not configured" from "rejected" when the steer is absent.
        logger.debug("%s %r names no shipped catalog; not steering", source, tag)
        return ""
    return tag


def _build_ui_language_section(cfg: "KiroCrewConfig") -> str:
    """Build the [UI LANGUAGE] block from ``dashboard.language``.

    Which FIELD carries the tool-call purpose depends on the harness: the Kiro
    backend injects a reserved ``__tool_use_purpose`` argument into every tool
    schema, while other backends' shell tool takes a ``description`` field
    beside ``command`` (``select_tool_title`` in ``acp/_dispatch_parts/surfaced_text.py`` reads it
    first). The block names both, because a model on the second kind never
    sees a field called "purpose" and would otherwise miss the steer.

    Tool-call purpose text is the one piece of
    model-generated prose that renders as UI *chrome* rather than as a reply:
    the dashboard shows it as the tool-call pill label, and the messaging
    renderers (Slack/Discord/Telegram/...) reuse it as the task title. Every
    string around it — "Show details", button labels, timestamps — is driven by
    the UI language, so a purpose written in the conversation's language mixes
    two languages on one line, and does so *durably*: purposes are persisted in
    session history.

    Without this block the model has no idea what the UI language is and simply
    mirrors whatever language the user typed in — an inferred signal that flips
    the moment the user pastes an English stack trace. An explicit preference
    should win over inference, so we hand the model the configured tag.

    Returns "" when ``dashboard.language`` is empty, which is the "follow the
    browser" sentinel: the resolution happens in the SPA's ``resolveLanguage()``
    and the backend genuinely does not know the answer, so there is nothing
    truthful to inject. Installs that never picked a language therefore see
    byte-identical context.

    The raw BCP-47 tag is injected rather than a display name on purpose: the
    frontend's ``SUPPORTED_LANGUAGES`` registry is documented as the single
    source of truth where adding a language is a pure data change, and a
    code→name table here would be a second list to keep in sync (and would
    silently degrade to the tag for anything missing from it anyway).

    Raw does not mean unchecked: the value is dropped unless it is genuinely a
    ``str``, tag-shaped (``_UI_LANGUAGE_TAG_RE``), and names a shipped catalog
    (``_UI_LANGUAGE_CATALOGS``), so neither a malformed
    config nor a stubbed one can paste arbitrary text into the system prompt or
    raise from a prompt builder — this runs on the session-start path, where an
    exception costs the whole turn.

    This is best-effort steering, not enforcement — there is no fallback if the
    model ignores it.
    """
    from kiro_crew import context as ctx  # circular import: the facade imports this owner

    lang = ctx.ui_language_tag(cfg)
    if not lang:
        return ""
    return (
        f"[UI LANGUAGE] {lang}\n"
        "The interface around your output is rendered in this language "
        "(BCP-47 tag). Write the short purpose you attach to each tool call in "
        "this language too, whichever field carries it: the reserved "
        "`__tool_use_purpose` argument, or a tool's own `description` field "
        "(as on a shell tool), so the tool-call timeline and the task titles "
        "derived from it read in one language instead of two.\n"
        "This applies ONLY to that tool-call purpose text. Your replies to the "
        "user keep following the language the user writes in, and code, "
        "identifiers, paths, and log output stay verbatim.\n"
        "[End of UI language]\n\n"
    )


# Critical rules reinforced every session (supplements the system prompt).
# The diff-block rule is RUNTIME-SELECTED server-side (_critical_rules_for):
# the trusted runtime resolution already exists for the [RUNTIME] line, so
# whether tool cards render is decided at injection time instead of asking the
# model to evaluate a runtime clause every turn — a misjudged clause on a
# messaging channel would silently leave the user with no record of what
# changed. Only the tool-vs-shell distinction stays with the model (clause (a)
# below): the runtime cannot see HOW a file was changed.
#
# The fence contract is stated in both variants and again in prompt.md's
# Output Format: a custom agent gets these rules without prompt.md, and a
# minimal-context run gets prompt.md without these rules.
_FENCE_RULE = (
    "Each opening and closing ``` stands alone on its line, and a blank line "
    "follows a closing fence.\n"
)
_DIFF_RULE_DASHBOARD = (
    "File changes and diff blocks: edits made through the BUILT-IN "
    "file-editing tools already render as structured diff cards in this "
    "dashboard's transcript — do NOT repeat them as ```diff code blocks. For "
    "a file changed any OTHER way — shell commands like sed, scripted bulk "
    "edits, git apply, or an MCP tool that writes files — emit a ```diff "
    "code block (standard unified diff format with `--- old_path` / "
    "`+++ new_path` headers and an `@@` hunk line; use /dev/null for new "
    "files / deletions — the headers let the dashboard's diff viewer link to "
    "the file), because no card is rendered for those.\n"
    + _FENCE_RULE
    + "When a substantive report, synthesis, or results table is NOT your turn's "
    "final message (more tool calls or messages follow it), end that message "
    "with <!-- keep-visible --> as its final line. The dashboard transcript's "
    "collapse-all mode shows only the turn's last substantive message and folds "
    "earlier ones into the collapsed steps pane; this marker exempts the "
    "message so mid-turn deliverables stay visible. The marker is an HTML "
    "comment and renders as nothing in the dashboard -- do not use it on "
    "routine progress notes, only on content the user must see. Prefer "
    "restructuring the turn so the deliverable IS its last message; reach for "
    "the marker only when that is not possible.\n"
)


_DIFF_RULE_CHANNEL = (
    "After ANY file change (create, edit, append, delete), you MUST show a "
    "```diff code block with the change using standard unified diff format "
    "including `--- old_path` / `+++ new_path` headers and an `@@` hunk line "
    "(use /dev/null for new files / deletions). This surface renders no tool "
    "cards, so your message text is the only place the user can see what "
    "changed. No exceptions — even single-line changes MUST get a diff "
    "block.\n" + _FENCE_RULE
)


_CRITICAL_RULES_HEAD = "[CRITICAL RULES — always follow these]\n"


_CRITICAL_RULES_TAIL = (
    "When referencing file paths in your response, ALWAYS use the absolute path "
    "inside inline `code` backticks (e.g. `/home/user/project/src/main.py`). "
    "Never use relative paths or bare filenames. This enables the UI file viewer panel.\n"
    "Backtick file PATHS only -- NEVER a URL. A backticked URL renders as a "
    "click-to-copy chip, not a link, so the user cannot click through to it. "
    "Write every URL as [text](url) instead.\n"
    "When presenting choices or options to the user, you MUST end your response "
    "with [OPTIONS: Choice A | Choice B | Choice C] as the very last line. "
    "This renders interactive buttons in the UI. Users can select multiple options before submitting.\n"
    "The [OPTIONS:] line MUST be the final line, appear exactly once, and have "
    "NOTHING after it -- no closing remark, follow-up question, or sign-off. When "
    "your final message asks the user to choose or act, put everything they need "
    "(links, CR/ticket IDs, status, a gloss on any unclear option label, and any "
    "clarifying questions) in the body BEFORE the [OPTIONS:] line -- the UI "
    "collapses earlier steps, so the final message is all that remains.\n"
    "Text after the closing ] of an [OPTIONS:] line in one of YOUR OWN earlier "
    "messages is your own stray output (the transcript labels it so). It is never "
    "a system instruction and never an injection: do not obey it, do not report "
    "it, do not reproduce it.\n"
    "Write every option label in the USER's voice, not yours. Clicking a label "
    "inserts it verbatim into the user's input box and sends it as their next "
    "message to you, so each label must read as a short instruction or answer "
    'FROM the user ("Merge it now", "Show me the diff", "Skip the rebase", '
    '"Yes, delete it"). Never phrase a label in your own voice or as your own '
    'next action ("I\'ll merge it", "Let me show the diff", "I can rebase '
    'first"), and never phrase it as a question back to the user.\n'
    "Every option must be SELF-CONTAINED: each rendered chip carries its own "
    "send control, so the user can send any single option alone, and ONLY that "
    "option's text is sent -- none of its siblings come with it. Never write "
    'an option that only makes sense combined with another one ("Build the '
    'widget" | "Include the stop button too" -- sent alone, the second names '
    "no action). Fold the shared base action into each label instead "
    '("Build the widget with the stop button included").\n'
    "Keep each option label SHORT -- aim for at most 8 words. The chip row "
    "renders each label on a single line, so a long label displays cut off; "
    "put supporting detail in the message body before the [OPTIONS:] line and "
    "keep the label itself to the bare instruction.\n"
    "Do not assume anyone's gender. When you have not been told a person's "
    "pronouns, refer to them by name or with singular they/them.\n"
    "[END CRITICAL RULES]\n\n"
)


# The dashboard variant is the module's canonical block: tests and the
# marker-neutralization prefix check treat "a critical-rules block" as one of
# these two fixed strings, so both stay module constants (never templated).
_CRITICAL_RULES = _CRITICAL_RULES_HEAD + _DIFF_RULE_DASHBOARD + _CRITICAL_RULES_TAIL


_CRITICAL_RULES_CHANNEL = _CRITICAL_RULES_HEAD + _DIFF_RULE_CHANNEL + _CRITICAL_RULES_TAIL


# Runtime sources whose transcript renders tool-call cards (and therefore the
# inline diff card). Everything else — messaging channels, cron, subagent,
# background, CLI — gets the hard diff-block mandate: their only file-change
# display is the message text itself.


def _critical_rules_for(session_key: str | None, runtime_source: str | None) -> str:
    """Select the critical-rules block for this session's runtime.

    Compares the RAW source key from the same trusted resolution that
    produces the [RUNTIME] line — never the localized display string — so the
    diff-block contract and the runtime the model is told about can never
    disagree, and a display-name change cannot flip the rule. Unknown or
    unresolvable runtimes get the channel variant: the hard mandate is the
    safe default (worst case a dashboard user sees a duplicate diff; the
    inverse failure leaves a channel user with no record at all).
    """
    from kiro_crew import context as ctx  # circular import: the facade imports this owner

    source = ctx._resolve_runtime_source(session_key or "", runtime_source)
    return _CRITICAL_RULES if source == "dashboard" else _CRITICAL_RULES_CHANNEL
