"""Setting, clearing and reporting ``agent.spec_pins``, for the CLI and the dashboard.

:mod:`kiro_crew.agent_spec_pins` holds the store and the rule every reader applies.
This module is the one write path both surfaces use:

* :func:`check_pin_target` refuses a pin the resolver could not honour (an invalid
  agent or file name, a file that is not in the user-level agents directory, or one
  that does not declare that name), so a pin is never stored broken;
* :func:`write_spec_pin` sets or clears one pin under the config file lock;
* :func:`contested_agents` lists every name two or more specs declare, with each
  file's name, modification time and installing package, which ``kirocrew agent
  pin`` prints;
* :func:`ambiguity_meta` turns a refused session start into the structured
  ``agent_spec_ambiguous`` payload the chat card reads.

A pin names a file the package installer owns; nothing here writes a spec.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable, Mapping

from kiro_crew import agent_spec_pins

#: The error code a refused start, and the dashboard card it raises, carry.
AGENT_SPEC_AMBIGUOUS_CODE = "agent_spec_ambiguous"

#: At most this many claimants are reported per name.
_MAX_CLAIMANTS = 32

#: A claimant's ``description`` is cut to this many characters for the chooser.
_MAX_DESCRIPTION = 200


class PinRefused(ValueError):
    """A pin the resolver could not honour; ``code`` names why, the message the fix."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _agents_dir(agents_dir: Path | None) -> Path:
    from kiro_crew.config.paths import kiro_agents_dir

    return agents_dir if agents_dir is not None else kiro_agents_dir()


def _read_for(path: Path, source: str) -> dict[str, Any] | None:
    """*path* through the hardened reader, labelled for the surface that asked."""
    from kiro_crew.agent_discovery import _read_agent_spec

    # Literal labels at each call, so the reader's label ratchet attributes both.
    if source == "cli":
        return _read_agent_spec(path, operation="agent_spec_pin", source="cli")
    return _read_agent_spec(path, operation="agent_spec_pin", source="dashboard")


def check_pin_target(
    agent: str, file_name: str, *, source: str, agents_dir: Path | None = None
) -> None:
    """Raise :class:`PinRefused` unless pinning *agent* to *file_name* is honourable.

    The file must be a bare name (:func:`agent_spec_pins.valid_pin_file_name`), be
    one of the specs the agents directory lists (so a ``.md`` shadowed by its
    ``.json`` twin, an AppleDouble sidecar or a link the reader refuses does not
    qualify), parse through the hardened reader, and declare ``name == agent``.
    """
    if not agent_spec_pins.valid_pin_agent_name(agent):
        raise PinRefused("invalid_agent_name", f"{agent!r} is not a valid agent name")
    if not agent_spec_pins.valid_pin_file_name(file_name):
        raise PinRefused(
            "invalid_file_name",
            f"{file_name!r} is not a spec file name: give the bare name of a .json or .md "
            "file in the agents directory, with no path",
        )
    from kiro_crew.agent_spec_format import iter_agent_spec_files_strict

    directory = _agents_dir(agents_dir)
    path = directory / file_name
    if not directory.is_dir():
        raise PinRefused("file_not_found", f"{file_name!r} is not a spec in the agents directory")
    try:
        # By exact name: every reader matches a pin by the listed file name, so a
        # spelling a case-insensitive filesystem would also open is not that file.
        # Strict: a listing that fails is unreadable, never an empty directory.
        listed = {entry.name for entry in iter_agent_spec_files_strict(directory)}
    except OSError as exc:
        raise PinRefused("agents_dir_unreadable", f"the agents directory cannot be read: {exc}")
    if file_name not in listed:
        raise PinRefused("file_not_found", f"{file_name!r} is not a spec in the agents directory")
    spec = _read_for(path, source)
    if spec is None:
        raise PinRefused("file_unreadable", f"{file_name!r} could not be read as an agent spec")
    declared = spec.get("name")
    if declared != agent:
        raise PinRefused(
            "file_declares_other_name",
            f"{file_name!r} declares the name {declared!r}, not {agent!r}",
        )


def _refuse_overlay_owned_pin(agent: str, file_name: str | None) -> None:
    """Raise :class:`PinRefused` when ``config.local.json`` decides *agent*'s pin.

    The overlay deep-merges over ``config.json``, the file this module writes, so a
    pin it sets keeps applying whatever is written here: report that instead of a
    change that never takes effect.
    """
    import json

    from kiro_crew.config.loader_locations import config_local_path, read_endorsed_config_text

    path = config_local_path()
    try:
        doc = json.loads(read_endorsed_config_text(path))
    except (OSError, ValueError):
        return
    section = doc.get("agent") if isinstance(doc, dict) else None
    pins = section.get(agent_spec_pins.SPEC_PINS_KEY) if isinstance(section, dict) else None
    if not isinstance(pins, dict) or agent not in pins or pins[agent] == file_name:
        return
    raise PinRefused(
        "pin_overlay_owned",
        f"{path.name} sets agent.spec_pins.{agent} to {pins[agent]!r}, and it overrides "
        f"config.json: change or remove it there",
    )


def write_spec_pin(agent: str, file_name: str | None) -> bool:
    """Set *agent*'s pin to *file_name*, or clear it for ``None``. Blocking.

    One locked read-modify-write of ``config.json``
    (:func:`kiro_crew.config.loader.update_config_locked`), so a concurrent writer's
    change is kept. Returns whether a pin was there before. Validation is the
    caller's (:func:`check_pin_target`); an invalid agent name is still refused.
    """
    if not agent_spec_pins.valid_pin_agent_name(agent):
        raise PinRefused("invalid_agent_name", f"{agent!r} is not a valid agent name")
    if file_name is not None and not agent_spec_pins.valid_pin_file_name(file_name):
        raise PinRefused("invalid_file_name", f"{file_name!r} is not a spec file name")
    from kiro_crew.config import loader

    _refuse_overlay_owned_pin(agent, file_name)
    existed = False

    def mutate(doc: dict) -> dict:
        nonlocal existed
        section = loader.coerce_dict_section(doc, "agent")
        raw = section.get(agent_spec_pins.SPEC_PINS_KEY)
        pins = dict(raw) if isinstance(raw, dict) else {}
        existed = agent in pins
        if file_name is None:
            pins.pop(agent, None)
        elif not existed and len(pins) >= agent_spec_pins.MAX_SPEC_PINS:
            # The reader keeps at most this many pins, so a further one would be
            # stored and then dropped: refuse it rather than report a pin that
            # never applies.
            raise PinRefused(
                "too_many_pins",
                f"{len(pins)} agent spec pins are already set, the most that apply; "
                f"clear one first with: {agent_spec_pins.pin_command(agent, clear=True)}",
            )
        else:
            pins[agent] = file_name
        if pins:
            section[agent_spec_pins.SPEC_PINS_KEY] = pins
        else:
            section.pop(agent_spec_pins.SPEC_PINS_KEY, None)
        return doc

    loader.update_config_locked(loader.config_path(), mutate=mutate, stamp_meta=False)
    return existed


def _differing_fields(specs: list[dict[str, Any] | None]) -> list[list[str]]:
    """For each parse, the shown fields whose value differs from some other parse.

    An unreadable file (``None``) differs in nothing it can be shown to differ in.
    """
    from kiro_crew.user_json import strict_json_equal

    out: list[list[str]] = []
    for index, spec in enumerate(specs):
        if spec is None:
            out.append([])
            continue
        others = [s for i, s in enumerate(specs) if i != index and s is not None]
        out.append(
            [
                key
                for key in agent_spec_pins.SHOWN_FIELDS
                if any(not strict_json_equal(spec.get(key), other.get(key)) for other in others)
            ]
        )
    return out


def omitted_claimants(paths: tuple[Path, ...] | list[Path]) -> int:
    """How many of *paths* :func:`claimant_rows` leaves out at its bound."""
    return max(0, len(paths) - _MAX_CLAIMANTS)


def _redacted(text: str) -> str:
    """*text* with exfiltration URLs and credentials redacted.

    Description and package label are file content an agent may have written, and
    the chooser carries them in live chat metadata, which no later redactor sees.
    """
    from kiro_crew.security import redact_credentials, redact_exfiltration_urls

    text, _ = redact_exfiltration_urls(text)
    text, _ = redact_credentials(text)
    return text


def _redacted_description(description: str) -> str:
    """*description* redacted (:func:`_redacted`), then capped."""
    return _redacted(description)[:_MAX_DESCRIPTION]


def _redacted_label(label: str | None) -> str | None:
    """A display-only package label, redacted (:func:`_redacted`)."""
    return _redacted(label) if label else label


def claimant_rows(
    paths: tuple[Path, ...] | list[Path],
    packages: dict[Path, str | None] | None = None,
    *,
    specs: Mapping[Path, dict[str, Any]] | None = None,
    source: str = "dashboard",
    read: bool = True,
) -> list[dict[str, Any]]:
    """Display rows for the files declaring one name, so a person can tell them apart.

    Each row is ``{"file", "mtime", "package", "description", "differs"}``. ``mtime``
    is the link's own modification time in whole seconds (``lstat``, never following
    it), or ``None`` when it cannot be read. ``package`` is
    :func:`agent_spec_pins.spec_package`'s answer as the resolver recorded it.
    ``description`` is the spec's own ``description`` (capped), or ``None``.
    ``differs`` lists the spec fields (``agent_spec_pins.SHOWN_FIELDS``) whose value
    differs from another file's. Parses come from *specs*; a path missing there is
    read through the hardened reader, unless *read* is false (a caller on the event
    loop), when that row shows no description and differs in nothing.
    """
    kept = list(paths)[:_MAX_CLAIMANTS]
    parsed: list[dict[str, Any] | None] = []
    for path in kept:
        spec = (specs or {}).get(path)
        if spec is None and read:
            spec = _read_for(path, source)
        parsed.append(spec if isinstance(spec, dict) else None)
    rows: list[dict[str, Any]] = []
    for path, spec, differs in zip(kept, parsed, _differing_fields(parsed)):
        try:
            mtime: int | None = int(os.lstat(path).st_mtime)
        except OSError:
            mtime = None
        description = spec.get("description") if spec is not None else None
        rows.append(
            {
                "file": path.name,
                "mtime": mtime,
                "package": _redacted_label((packages or {}).get(path)),
                "description": (
                    _redacted_description(description)
                    if isinstance(description, str) and description.strip()
                    else None
                ),
                "differs": differs,
            }
        )
    return rows


def contested_agents(*, source: str, agents_dir: Path | None = None) -> list[dict[str, Any]]:
    """Every name two or more user-level specs declare, with its pin and claimants.

    One walk of the agents directory through the hardened reader. Each entry is
    ``{"agent", "pin", "pin_state", "divergent", "omitted", "claimants"}``; ``pin_state``
    is ``"none"``, ``"ok"`` (the pin names a claimant) or ``"broken"``, and ``omitted``
    counts the files past :func:`claimant_rows`' bound. Sorted by name.
    """
    from kiro_crew.agent_spec_format import iter_agent_spec_files_strict
    from kiro_crew.user_json import strict_json_equal

    directory = _agents_dir(agents_dir)
    groups: dict[str, list[tuple[Path, dict[str, Any]]]] = {}
    if not directory.is_dir():
        return []
    # Strict: a listing that fails raises, never reads as an empty directory.
    for path in iter_agent_spec_files_strict(directory):
        spec = _read_for(path, source)
        name = spec.get("name") if isinstance(spec, dict) else None
        if isinstance(spec, dict) and isinstance(name, str):
            groups.setdefault(name, []).append((path, spec))
    pins = agent_spec_pins.spec_pins()
    out: list[dict[str, Any]] = []
    for name in sorted(groups):
        members = groups[name]
        if len(members) < 2:
            continue
        first = members[0][1]
        divergent = any(not strict_json_equal(spec, first) for _path, spec in members[1:])
        decision, pin = agent_spec_pins.pin_decision(name, [p.name for p, _s in members], pins)
        out.append(
            {
                "agent": name,
                "pin": pin,
                "pin_state": {"use": "ok", "broken": "broken"}.get(decision, "none"),
                "divergent": divergent,
                "omitted": omitted_claimants([p for p, _s in members]),
                "claimants": claimant_rows(
                    [p for p, _s in members],
                    {p: agent_spec_pins.spec_package(s) for p, s in members},
                    specs=dict(members),
                    source=source,
                ),
            }
        )
    return out


def _ambiguity_in(exc: BaseException | None) -> Any:
    from kiro_crew.agent_discovery import AmbiguousAgentSpecError

    seen: set[int] = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        if isinstance(exc, AmbiguousAgentSpecError):
            return exc
        exc = exc.__cause__ or exc.__context__
    return None


def ambiguity_meta(exc: BaseException) -> dict[str, Any] | None:
    """The ``agent_spec_ambiguous`` row meta for a start refused over contested specs.

    Walks *exc*'s cause chain for the resolver's
    :class:`~kiro_crew.agent_discovery.AmbiguousAgentSpecError` (its
    :class:`~kiro_crew.agent_discovery.AgentSpecPinError` subclass included).
    ``None`` when there is none, or it names no agent or no files.
    """
    found = _ambiguity_in(exc)
    if found is None or not found.agent or not found.paths:
        return None
    return {
        "kind": AGENT_SPEC_AMBIGUOUS_CODE,
        "code": AGENT_SPEC_AMBIGUOUS_CODE,
        "agent": found.agent,
        "pin": found.pin,
        "omitted": omitted_claimants(found.paths),
        # Rendered on the event loop: the resolver's own parses only, no file read.
        "claimants": claimant_rows(found.paths, found.packages, specs=found.summaries, read=False),
    }


def audit_pin_write(
    log: Callable[..., Any], *, caller: str, source: str, agent: str, file_name: str | None
) -> None:
    """Record a pin write, or a clear (*file_name* ``None``), in the SEL through *log*
    (``sel().log_api_access``). Never raises."""
    try:
        if file_name is None:
            log(
                caller=caller,
                operation="agent_spec_pin.clear",
                outcome="success",
                source=source,
                resources=agent,
            )
        else:
            log(
                caller=caller,
                operation="agent_spec_pin.write",
                outcome="success",
                source=source,
                resources=f"{agent}={file_name}",
            )
    except Exception:  # noqa: BLE001 -- the audit must not undo a committed write
        agent_spec_pins.logger.debug("SEL audit of a spec pin write failed", exc_info=True)
