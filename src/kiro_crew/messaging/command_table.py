"""A channel's command vocabulary as DATA, shared by the slash-command channels.

A channel declares its commands once, as a tuple of :class:`CommandSpec` rows,
and both its matcher and its ``/help`` card read that tuple, so a command cannot
be added without appearing in help. Weixin and Feishu use it.

Only the table SHAPE lives here. Each channel keeps its own rows, its own help
header and footer, and its own reply strings in its ``commands.py``, because
which words a channel accepts and what it says back are that channel's.

Dependency direction is ``<channel> -> messaging`` (never the reverse).
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class CommandSpec:
    """One command: its canonical name, what it does, and how it is spelled.

    ``name`` is the value a dispatcher switches on; the displayed form adds the
    ``/``. ``aliases`` are EXTRA spellings beyond ``f"/{name}"``, which is always
    accepted: native-language words (`新对话`), and short forms (`cancel` for
    `stop`).
    """

    name: str
    help_text: str
    aliases: tuple[str, ...] = field(default=())


def match_command(text: str, specs: tuple[CommandSpec, ...]) -> str | None:
    """Return the ``name`` of the command *text* invokes, else ``None``.

    Matching is EXACT against the whole stripped message, so a message that
    merely begins with a command word stays ordinary prose the model answers.
    ``f"/{name}"`` is matched case-insensitively (a phone keyboard capitalises),
    while a non-ASCII alias is compared as written because casing is meaningless
    for it and ``str.lower()`` on some scripts is not a no-op.

    The ``/`` is hardcoded because every adopter uses it. Discord prefixes with
    ``!`` (its client swallows a bare ``/``) but does not consume this module; the
    day it migrates is the day a prefix parameter earns its place.
    """
    stripped = (text or "").strip()
    if not stripped:
        return None
    lowered = stripped.lower()
    for spec in specs:
        if lowered == f"/{spec.name}":
            return spec.name
        for alias in spec.aliases:
            if stripped == alias or lowered == alias.lower():
                return spec.name
    return None


def build_help_text(header: str, specs: tuple[CommandSpec, ...], footer: str = "") -> str:
    """Render a help card from *specs*.

    Aliases are shown beside their command, because a user who was taught `新对话`
    needs to see it is the same thing as `/new` rather than discovering two
    commands. The caller puts anything else it needs to explain into *footer*.
    """
    lines = [header, ""]
    for spec in specs:
        spelled = f"/{spec.name}"
        if spec.aliases:
            spelled += " (" + " / ".join(spec.aliases) + ")"
        lines.append(f"{spelled} — {spec.help_text}")
    if footer:
        lines += ["", footer]
    return "\n".join(lines)
