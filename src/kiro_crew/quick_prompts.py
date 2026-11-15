"""Quick prompts — slash tokens that expand into an instruction the agent reads.

A quick prompt is a MACRO, not a command. The user types ``/plain``; the agent
receives the written-out instruction that token stands for. Nothing is executed,
no session state changes, and no surface has to know the token exists.

Two properties are the reason this module sits in the core rather than in the
dashboard:

* Expansion happens inside :meth:`kiro_crew.context.ContextBuilder.build_message`,
  the one function every inbound surface funnels through, so one row reaches every
  surface at once instead of needing a branch per dispatcher. The dashboard-only
  text rewrites (``$skill`` append in ``dashboard/chat_turn/turn_dispatch_gates.py``,
  ``@prompt`` replacement in ``dashboard/chat_turn/turn_admission.py``) are deliberately
  NOT the model copied here — each of them buys one surface.
* The expansion is what the MODEL sees, not what is stored. The row persisted to
  history and echoed back into the transcript is still the ``/plain`` the user
  typed, so the scrollback stays readable and a replay does not re-run a wall of
  instructions.

Where a token actually ARRIVES is narrower than "every surface", and worth stating
precisely because the limit is not ours to fix from here. Expansion is reached
whenever the leading token survives to the gateway as message TEXT: the dashboard
composer, Telegram (an unknown ``/word`` is delivered to the bot), and every
non-client surface — a cron turn, a subagent, the task runner. Two clients capture
a leading ``/`` BEFORE the gateway is involved, and no backend placement changes
that:

* **Slack** reads a leading ``/`` as a workspace slash command. An unregistered
  token yields Slack's own "not a valid command" notice and the message is never
  posted, so nothing reaches ``build_message``.
* **Discord** routes a leading ``/`` into its native slash-command UI, which is why
  this product's own Discord aliases are documented as ``!``-prefixed.

Adding the next quick prompt is one row in :data:`QUICK_PROMPTS` plus one
description key in the composer menu. It is never a new code path.

A user adds their own row without patching the install: a saved prompt in the
global prompt library, ``~/.kiro/prompts/<name>.md``, whose frontmatter carries
``slash_command: true`` expands as ``/<name>`` (see :func:`user_quick_prompt`).

A quick-prompt token must NOT collide with a command some surface parses AHEAD of
the funnel — ``_SLASH_COMMANDS`` in ``dashboard/chat_utils.py``, or a channel's own
alias set in ``<channel>/commands.py``. A colliding token would be handled by that
surface and never expand: per-surface drift, the exact thing this layer exists to
end. A test unions every one of those sets and pins the disjointness, and the same
union is :data:`_RESERVED_TOKENS`, which a user prompt may not claim.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from kiro_crew.frontmatter import SKILL_LOADER, parse_frontmatter
from kiro_crew.hooks import FileTooLargeError, safe_read_file_bytes_nolink
from kiro_crew.security import redact
from kiro_crew.skills import SkillsLoader

__all__ = [
    "QUICK_PROMPTS",
    "QuickPrompt",
    "UserQuickPrompt",
    "expand_quick_prompt",
    "user_quick_prompt",
    "user_quick_prompt_menu",
]


# Shared by every row: the shape of the answer, as distinct from its subject.
# Split out because the subject is what differs between quick prompts while the
# discipline — a scannable line format, the reader's own language and habits, real
# names over analogies, a decision at the end — is the product itself and must not
# drift per row.
_STYLE_CONTRACT = """
FORMAT. Answer in 3 to 5 LINES, one thought per line, each on its own line with a
blank line between them. Not one paragraph: the line breaks ARE the structure, and a
single block of prose is the failure this format exists to prevent. Five lines is a
ceiling, not a target — if three say it, stop at three.

Keep every line short enough to take in at a glance: about one sentence, never more
than roughly thirty words. A line needing a semicolon and two clauses is either two
lines or a line to cut. No headers, no bold labels, no bullet markers, no preamble,
no restating the question back.

LANGUAGE AND REGISTER. Write in the language the reader is using in this
conversation, and in the register they write in — if they have been writing Chinese,
answer in Chinese. Follow their stated preferences and the corrections they have
taught you: how terse they want it, which vocabulary they use, what they have told
you never to do. Those win over anything here, which governs shape, not voice.

Ground every claim in the real thing — the actual identifiers, file paths with
line numbers, PR and issue numbers, and command output you have seen in this
session — and use this project's own vocabulary for them. Do not reach for an
analogy or a metaphor: the reader already knows the domain and needs the real names
to act on.

Where you are unsure, say so plainly instead of smoothing it over, and name the
one thing that would settle it.

The LAST line is the decision now in front of the reader: two named options, or the
single next step when nothing actually needs deciding. Label it in the reader's own
language (in English, "Decision: A or B").

Add a diagram only when the situation is genuinely structural: parallel branches,
a state machine, an ordering that turned out to matter. Then put one small
diagram after the lines. A straightforward status gets no diagram.

If the reader replies that they still do not follow, do NOT make the next answer
longer. Cut it differently: drop to a single concrete example, or narrow the
scope to the one part that matters.
"""


# ── /plain ────────────────────────────────────────────────────────────────────
# The zero-argument form is the PRIMARY one. "Where are we?" is the question that
# gets asked, and answering it well means reading the whole line of work rather
# than paraphrasing the previous tool result.
_PLAIN_ZERO_ARG = """
Bring the reader up to date on where this work stands right now.

The subject is the WHOLE line of work in this session — not the last turn, and
not the result of the most recent tool call. Cover where things actually stand,
what is settled and no longer in question, and what is still open or unverified.

State your LATEST understanding. When something you concluded earlier in this
session has since been disproved or replaced, say that it is superseded and give
only the current reading; never restate an abandoned explanation as though it
were still live.
"""

_PLAIN_WITH_ARG = """
Explain the following in plain language, grounded in this project rather than in
general terms:

{arg}

Where this session has already established something about it, use that. Where an
earlier reading of it has since been disproved, give only the current one and say
the old one is superseded.
"""


@dataclass(frozen=True)
class QuickPrompt:
    """One macro row.

    ``zero_arg`` is used when the token is the entire message; ``with_arg`` when
    text follows it, with the literal placeholder ``{arg}`` standing in for that
    text. The token itself is the key this row is stored under in
    :data:`QUICK_PROMPTS` and is deliberately NOT repeated as a field.
    """

    zero_arg: str
    with_arg: str


QUICK_PROMPTS: dict[str, QuickPrompt] = {
    "/plain": QuickPrompt(
        zero_arg=_PLAIN_ZERO_ARG,
        with_arg=_PLAIN_WITH_ARG,
    ),
}


# Anchored at the start of the message: a quick prompt is only a quick prompt
# when the user opened with it. ``/plain`` mentioned mid-sentence, or quoted
# inside a pasted log, is ordinary text and must stay ordinary text.
#
# The token charset is greedy over letters, digits and hyphens so a LONGER word
# starting with a registered token (``/plainly``) captures as ``/plainly`` and
# misses the registry, instead of matching ``/plain`` and swallowing "ly" as the
# argument. Case-insensitive because a phone keyboard will autocapitalise.
_QUICK_PROMPT_RE = re.compile(r"^[ \t]*(/[A-Za-z][A-Za-z0-9-]*)(?:\s+([\s\S]*))?\s*$")

# Header naming the macro that fired. Deliberately not one of the forgeable
# boundary markers neutralized by ``_structural_marker_spans`` — this text is
# assembled here, but it is spliced into the turn ahead of that scrub and must
# survive it unchanged.
_HEADER = "[QUICK PROMPT {token}]"


def expand_quick_prompt(text: str) -> str | None:
    """Expand a leading quick-prompt token in *text*.

    Returns the instruction to put in the turn's place, or ``None`` when *text*
    does not open with a registered token — in which case the caller must leave
    the turn exactly as it was.
    """
    match = _QUICK_PROMPT_RE.match(text)
    if not match:
        return None
    token = match.group(1).lower()
    arg = (match.group(2) or "").strip()
    header = _HEADER.format(token=token)
    row = QUICK_PROMPTS.get(token)
    if row is None:
        user = user_quick_prompt(token)
        if user is None:
            return None
        # The user's own prompt is the whole instruction: no style contract.
        # An argument with no ``{arg}`` to land in is appended, not dropped.
        body = user.body.replace("{arg}", arg) if "{arg}" in user.body else f"{user.body}\n\n{arg}"
        return f"{header}\n{body.strip()}\n"
    # str.replace, not str.format: the argument is the user's own text and may
    # legitimately contain braces (a JSON blob, an f-string, a Rust generic).
    # format() would raise KeyError/ValueError on those and lose the turn.
    body = row.with_arg.replace("{arg}", arg) if arg else row.zero_arg
    return f"{header}\n{body.strip()}\n\n{_STYLE_CONTRACT.strip()}\n"


# ── User quick prompts ────────────────────────────────────────────────────────
# Opt-in, because ``~/.kiro/prompts`` is also the ``@prompt`` library and most of
# it is not meant to be a command. Read by exact name on every use, so an edit
# applies to the next turn and nothing is cached.

# The frontmatter key that turns a saved prompt into ``/<file name>``.
_USER_PROMPT_FLAG = "slash_command"

# The cap the ``@prompt`` read applies (``prompts_authoring.MAX_PROMPT_BYTES``).
_USER_PROMPT_MAX_BYTES = 100_000

# The token shape, checked before the token names a file: no dot, no separator.
_USER_TOKEN_RE = re.compile(r"/[a-z][a-z0-9-]*")

# Every token a surface handles BEFORE ``build_message``: the dashboard's slash
# gate and composer, the commands the Claude harness lists, and each channel's
# own aliases. A user prompt with one of these names would expand on some
# surfaces only, so it is not a quick prompt at all. ``test_quick_prompts.py``
# pins this set against every source.
_RESERVED_TOKENS = frozenset(QUICK_PROMPTS) | frozenset("""
    /agent /agents /bg /btw /cancel /changelog /chat /clear /code /commands
    /compact /context /cron /crons /dashboard /editor /exit /experiment /fork /goal
    /help /hooks /incognito /init /issue /kb /kirocrew /link /logdump /mcp
    /model /models /new /onboarding /paste /ping /prompts /q /queue /quit
    /reply /reset /review /security-review /session /sessions /side /spawn
    /start /status /steer /stop /tangent /task /tasks /temporary /title /todos
    /tools /unlink /usage /voice /workflow /workflows /yolo
    """.split())


@dataclass(frozen=True)
class UserQuickPrompt:
    """An opted-in saved prompt: its menu description and its body."""

    description: str
    body: str


def _user_prompts_dir() -> Path:
    return Path.home() / ".kiro" / "prompts"


def _read_user_prompt(path: Path, root: str) -> UserQuickPrompt | None:
    """*path* as a quick prompt, or ``None`` unless its frontmatter opts in.

    Read through the same no-link, size-capped gate as an ``@prompt`` mention,
    and redacted like one, because the body is spliced into the turn.
    """
    try:
        raw = safe_read_file_bytes_nolink(
            str(path), within_root=root, max_bytes=_USER_PROMPT_MAX_BYTES
        )
    except FileTooLargeError:
        return None
    if raw is None:
        return None
    text = raw.decode("utf-8", errors="replace")
    try:
        meta = parse_frontmatter(text, SKILL_LOADER)
    except ValueError:
        return None
    if meta.get(_USER_PROMPT_FLAG, "").strip().lower() != "true":
        return None
    body = redact(SkillsLoader.strip_frontmatter(text))
    first_line = next((line for line in body.splitlines() if line.strip()), "")
    description = redact(meta.get("description", "")) or first_line.strip().lstrip("#").strip()
    return UserQuickPrompt(description=description, body=body)


def user_quick_prompt(token: str) -> UserQuickPrompt | None:
    """The opted-in ``~/.kiro/prompts/<name>.md`` behind *token*, or ``None``.

    File IO: callers on the event loop run it in a thread.
    """
    token = token.lower()
    if token in _RESERVED_TOKENS or not _USER_TOKEN_RE.fullmatch(token):
        return None
    prompts_dir = _user_prompts_dir()
    try:
        root = str(prompts_dir.resolve())
    except (OSError, RuntimeError):
        return None
    return _read_user_prompt(prompts_dir / f"{token[1:]}.md", root)


def user_quick_prompt_menu() -> list[dict[str, str]]:
    """``GET /api/slash-commands`` rows for the opted-in prompts in ``~/.kiro/prompts``.

    Each name goes through :func:`user_quick_prompt`, so the menu offers exactly
    what would expand. A directory scan: callers on the event loop run it in a
    thread.
    """
    tokens = sorted({f"/{path.stem.lower()}" for path in _user_prompts_dir().glob("*.md")})
    return [
        {"name": t, "description": p.description} for t in tokens if (p := user_quick_prompt(t))
    ]


def reserved_user_prompts() -> list[Path]:
    """The opted-in prompts in ``~/.kiro/prompts`` that a built-in command shadows.

    A file named after a reserved token never expands and has no menu row, so
    ``kirocrew doctor`` names it and asks for a rename. A directory scan.
    """
    prompts_dir = _user_prompts_dir()
    try:
        root = str(prompts_dir.resolve())
    except (OSError, RuntimeError):
        return []
    return [
        path
        for path in sorted(prompts_dir.glob("*.md"))
        if f"/{path.stem.lower()}" in _RESERVED_TOKENS and _read_user_prompt(path, root)
    ]
