"""``agent.spec_pins``: which file an agent name runs when several specs declare it.

A package installer that ships agents writes each one as ``<package>-<name>.json`` in
the user-level agents directory while the declared ``name`` stays bare, so two packages
can each install a different spec under one name. Kiro Crew never picks one of them on
its own. The operator chooses with a pin, ``agent.spec_pins: {"<name>": "<file name>"}``
in ``config.json``, written by ``kirocrew agent pin`` or the dashboard's chooser.

A pin holds a bare file name, never a path or a digest, so it keeps naming the same
package's file across that package's updates. This module is the store and the rule:

* :func:`spec_pins` and :func:`spec_pin_for` read the pins at call time, so a change
  applies to the next session start without a restart;
* :func:`pin_decision` is the one selection rule every reader applies to the files
  that declare a name;
* :func:`ambiguous_message` and :func:`broken_pin_message` are the refusal texts, each
  naming the exact command that repairs it;
* :func:`pick_spec_row` applies the rule for the first-match readers (model and flag
  lookups) that take whichever row answers first.

It imports nothing from agent discovery or the config loader at module level, so both
can call it.
"""

from __future__ import annotations

import logging
import shlex
import threading
from pathlib import Path
from typing import Any, Callable, Iterable, Literal, Mapping, Sequence

logger = logging.getLogger(__name__)

#: The ``agent`` section key the pins live under.
SPEC_PINS_KEY = "spec_pins"

#: The spec fields the chooser compares between claimants, in display order. The
#: resolver keeps these of each claimant's parse on its refusal, so the chooser
#: says how the files differ without reopening them.
SHOWN_FIELDS = (
    "description",
    "prompt",
    "model",
    "tools",
    "allowedTools",
    "mcpServers",
    "resources",
    "hooks",
)

#: At most this many pins are read; a hand-edited map past it keeps the first ones.
MAX_SPEC_PINS = 256

#: The spec file suffixes a pin may name (the two forms ``agent_spec_format`` reads).
_PIN_SUFFIXES = (".json", ".md")

#: The longest file name a pin may hold, the common filesystem name limit.
_MAX_PIN_FILE_CHARS = 255

#: Agent names whose ignored pin was already logged in this process.
_IGNORED_LOGGED: set[str] = set()
_IGNORED_LOCK = threading.Lock()

#: Set while this thread reads the pins, so a config load that reaches a spec reader
#: does not read them again from inside itself.
_READING = threading.local()

Decision = Literal["none", "use", "broken"]


def valid_pin_agent_name(value: object) -> bool:
    """Whether *value* can be the agent name a pin is keyed by."""
    from kiro_crew.validation import is_registered_agent_name

    return is_registered_agent_name(value)


def valid_pin_file_name(value: object) -> bool:
    """Whether *value* is a bare spec file name a pin may hold.

    A single path component: no ``/`` or ``\\``, not ``.`` or ``..``, no NUL or
    control character, not an AppleDouble sidecar (``._``), ending in ``.json`` or
    ``.md``, at most 255 characters.
    """
    if not isinstance(value, str) or not value or len(value) > _MAX_PIN_FILE_CHARS:
        return False
    if "/" in value or "\\" in value or value in (".", "..") or value.startswith("._"):
        return False
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value):
        return False
    if ".." in value:
        return False
    return value.lower().endswith(_PIN_SUFFIXES) and Path(value).stem != ""


def coerce_spec_pins(raw: object) -> dict[str, str]:
    """The pin map *raw* holds, keeping only valid ``name -> file`` entries.

    An invalid entry is dropped with a warning, so a hand-edited typo is visible in
    the log and never reaches a reader. At most :data:`MAX_SPEC_PINS` entries.
    """
    if not isinstance(raw, dict):
        return {}
    out: dict[str, str] = {}
    for name, file_name in raw.items():
        if len(out) >= MAX_SPEC_PINS:
            logger.warning(
                "agent.spec_pins: more than %d pins; the rest are ignored", MAX_SPEC_PINS
            )
            break
        if valid_pin_agent_name(name) and valid_pin_file_name(file_name):
            out[name] = file_name
        else:
            logger.warning(
                "agent.spec_pins: ignoring the entry %r -> %r (a pin is an agent name and a "
                "bare .json or .md file name)",
                name,
                file_name,
            )
    return out


def _derive_pins(config: Any) -> dict[str, str]:
    pins = getattr(getattr(config, "agent", None), SPEC_PINS_KEY, None)
    return dict(pins) if isinstance(pins, dict) else {}


def spec_pins() -> dict[str, str]:
    """The pins in the live config, read at call time. Never raises.

    Served from :func:`kiro_crew.config.load_memo.memoized` while the config files
    are unchanged. A config that cannot be loaded reads as no pins: every reader then
    refuses an ambiguous name, the fail-closed answer, and never runs another file.
    """
    if getattr(_READING, "active", False):
        return {}
    _READING.active = True
    try:
        from kiro_crew.config import load_memo
        from kiro_crew.config.loader_config import KiroCrewConfig

        return dict(load_memo.memoized("agent.spec_pins", KiroCrewConfig.load, _derive_pins))
    except Exception:  # noqa: BLE001 -- no pins is the fail-closed reading
        logger.warning("agent.spec_pins could not be read; no pin applies", exc_info=True)
        return {}
    finally:
        _READING.active = False


def spec_pin_for(agent: str, pins: Mapping[str, str] | None = None) -> str | None:
    """The file *agent* is pinned to, or ``None``. *pins* overrides the live read."""
    current = spec_pins() if pins is None else pins
    pin = current.get(agent)
    return pin if isinstance(pin, str) and pin else None


def pinned_memo_key(agent: str, pins: Mapping[str, str] | None = None) -> str:
    """*agent* as a directory-revision memo key, with its pin folded in.

    An ``AgentsDirMemo`` answer is pinned to the agents directory's revision, which
    a pin change in ``config.json`` does not move. Keying by the pin as well makes a
    changed pin a miss. ``NUL`` cannot occur in an agent name or a pin. A caller that
    also resolves passes one *pins* snapshot to both, so a pin written between the
    two reads cannot file one choice's answer under the other's key.
    """
    pin = spec_pin_for(agent, pins)
    return agent if pin is None else f"{agent}\0pin={pin}"


def _note_ignored(agent: str) -> None:
    with _IGNORED_LOCK:
        if agent in _IGNORED_LOGGED:
            return
        _IGNORED_LOGGED.add(agent)
    # Names stay out of the record: `kirocrew agent pin` lists the pin, and the
    # line only has to say that one was set aside.
    logger.info(
        "an agent.spec_pins entry names the only spec that declares its agent, so it "
        "has nothing to choose and is ignored (`kirocrew agent pin` lists the pins)"
    )


def pin_decision(
    agent: str, claimant_names: Sequence[str], pins: Mapping[str, str] | None = None
) -> tuple[Decision, str | None]:
    """How the pin for *agent* applies to the files that declare it.

    *claimant_names* are the file names (not paths) of every spec that declares
    *agent*. Returns:

    * ``("none", None)``: no pin, or the pinned file is the only claimant (a pin with
      nothing to choose is ignored, logged once per process);
    * ``("use", pin)``: the pinned file is one of two or more claimants and is the
      one to run;
    * ``("broken", pin)``: the pinned file is not a claimant (it is missing,
      unreadable, or declares another name), however many are left, none
      included. The caller refuses; it never runs a file the operator did not pin.
    """
    pin = spec_pin_for(agent, pins)
    if pin is None:
        return "none", None
    if pin not in claimant_names:
        return "broken", pin
    if len(claimant_names) < 2:
        _note_ignored(agent)
        return "none", None
    return "use", pin


def _shown(value: str, valid: Callable[[object], bool]) -> str:
    """*value* shell-quoted when it is a name a pin accepts, else its ``repr``.

    Names come from a user-writable directory and reach a terminal: ``repr`` escapes
    any control character a name the pin grammar refuses may carry.
    """
    return shlex.quote(value) if valid(value) else repr(value)


def pin_command(agent: str, file_name: str | None = None, *, clear: bool = False) -> str:
    """The ``kirocrew agent pin`` command line for *agent*, shell-quoted."""
    shown_agent = _shown(agent, valid_pin_agent_name)
    if clear:
        return f"kirocrew agent pin {shown_agent} --clear"
    shown_file = _shown(file_name or "", valid_pin_file_name)
    # A file named like an option (``-helper.json``) would be parsed as one: ``--``
    # ends option parsing, so the command written here is the command that pins.
    separator = "-- " if shown_file.startswith("-") else ""
    return f"kirocrew agent pin {shown_agent} {separator}{shown_file}"


def _command_lines(agent: str, file_names: Iterable[str]) -> str:
    return "\n".join(f"  {pin_command(agent, name)}" for name in file_names)


def ambiguous_message(agent: str, shown: Sequence[str], file_names: Sequence[str]) -> str:
    """The refusal for differing claimants and no pin.

    *shown* is how the first sentence names each file (full paths for a terminal, bare
    names for a wire reason); *file_names* are the names the pin commands take. Each
    is ``repr``'d or shell-quoted: file names in the agents directory are untrusted.
    """
    names = ", ".join(repr(item) for item in shown)
    return (
        f"{len(shown)} specs declare the name {agent!r} with differing content: {names}. "
        "Kiro Crew will not pick one, because each can carry its own tools and prompt. "
        "Choose the file this agent runs with one of:\n"
        f"{_command_lines(agent, file_names)}\n"
        'Or keep exactly one: delete the others, or change the "name" field inside all '
        "but one. Renaming a file alone does not help, because agents are matched by the "
        '"name" they declare.'
    )


def broken_pin_message(agent: str, pin: str, file_names: Sequence[str]) -> str:
    """The refusal for a pin that names none of the files declaring *agent*."""
    return (
        f"agent.spec_pins pins {agent!r} to {pin!r}, but no spec in the agents directory "
        f"named {pin!r} declares the name {agent!r}: it is missing, unreadable, or declares "
        "another name. Kiro Crew will not fall back to another file. Pin one of the files "
        "that declares it, or clear the pin:\n"
        f"{_command_lines(agent, file_names)}\n"
        f"  {pin_command(agent, clear=True)}"
    )


def pick_spec_row(
    rows: Iterable[tuple[Any, Path]],
    agent: str,
    pins: Mapping[str, str] | None = None,
) -> dict[str, Any] | None:
    """The row a first-match reader answers *agent* from, with the pin applied.

    *rows* are ``(parsed spec, path)`` pairs in the reader's own order; a row whose
    spec is not a dict is skipped. With no pin for *agent* this is the reader's own
    rule, the first row that declares *agent* or is named after it, and the iteration
    stops there. With a pin the rows are read whole: when two or more declare
    *agent*, the pinned file's row is the answer, or ``None`` when the pin names none
    of them (the session start refuses that case; a reader never answers it from
    another file). With fewer than two the reader's own rule applies.
    """
    pin = spec_pin_for(agent, pins)
    if pin is None:
        for data, path in rows:
            if isinstance(data, dict) and (data.get("name") == agent or path.stem == agent):
                return data
        return None
    first: dict[str, Any] | None = None
    pinned: dict[str, Any] | None = None
    declared: list[str] = []
    for data, path in rows:
        if not isinstance(data, dict):
            continue
        if data.get("name") == agent:
            declared.append(path.name)
            if path.name == pin:
                pinned = data
        if first is None and (data.get("name") == agent or path.stem == agent):
            first = data
    decision, _pin = pin_decision(agent, declared, {agent: pin})
    if decision == "use":
        return pinned
    if decision == "broken":
        return None
    return first


#: The hook arguments a package installer's metrics hook names its package with.
_PACKAGE_FLAGS = ("--agent-package", "--agent-source-package")
_MAX_PACKAGE_CHARS = 128


def _clean_package(value: str) -> str | None:
    if 0 < len(value) <= _MAX_PACKAGE_CHARS and all(ch.isalnum() or ch in "._-@/+" for ch in value):
        return value
    return None


def _hook_commands(hooks: object) -> Iterable[str]:
    if not isinstance(hooks, dict):
        return
    for entries in hooks.values():
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if isinstance(entry, dict):
                command = entry.get("command")
                if isinstance(command, str):
                    yield command
                args = entry.get("args")
                if isinstance(args, list) and all(isinstance(a, str) for a in args):
                    yield shlex.join(args)


def spec_package(spec: object) -> str | None:
    """The package that installed *spec*, from its metrics hook arguments, or ``None``.

    A package installer that ships agents adds a hook whose command carries
    ``--agent-package <name>`` (or ``--agent-source-package <name>``). The value is
    display data only, bounded and limited to package-name characters; anything else
    reads as unknown.
    """
    if not isinstance(spec, dict):
        return None
    found: dict[str, str] = {}
    for command in _hook_commands(spec.get("hooks")):
        try:
            tokens = shlex.split(command)
        except ValueError:
            continue
        for index, token in enumerate(tokens):
            for flag in _PACKAGE_FLAGS:
                value: str | None = None
                if token == flag and index + 1 < len(tokens):
                    value = tokens[index + 1]
                elif token.startswith(flag + "="):
                    value = token[len(flag) + 1 :]
                if value is not None and flag not in found:
                    cleaned = _clean_package(value)
                    if cleaned is not None:
                        found[flag] = cleaned
    for flag in _PACKAGE_FLAGS:
        if flag in found:
            return found[flag]
    return None
