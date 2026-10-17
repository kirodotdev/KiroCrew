"""Crediting skill bodies the model read directly, and folding ledger aliases.

A file-read tool call or a shell ``cat`` delivers a skill body without passing
through the loader. ``resolve_tool_read_keys`` names the served skills such a
call delivers, recording nothing; ``credit_skill_reads`` records them once the
read has completed. Only content-delivering reads qualify, and a read through a
symlink is credited to the canonical served key.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kiro_crew.skills import SkillsLoader

logger = logging.getLogger("kiro_crew.skills")


def _tool_read_path_candidates(
    tool_name: str, raw_params: dict | None, command: str | None
) -> list[str]:
    """File targets of a tool call that DELIVERS file content to the model.

    Returns nothing for a call that merely names a path — a delete, move, line
    count, or grep. The ledger's hits mean "a body reached the model", so
    crediting a mention would re-create the mention-as-use conflation that the
    separate searches tally exists to avoid.

    Never raises on a malformed params dict — a tool's arguments are
    model-authored and may hold anything.
    """
    from kiro_crew import skills as sk  # circular import: the facade imports this module

    out: list[str] = []
    if isinstance(raw_params, dict) and tool_name in sk._CONTENT_READ_TOOLS:
        for key in sk._TOOL_READ_PATH_KEYS:
            value = raw_params.get(key)
            if isinstance(value, str):
                out.append(value)
            elif isinstance(value, (list, tuple)):
                out.extend(v for v in value if isinstance(v, str))
        # kiro-cli's `read` batches its targets. Only a Line operation returns a
        # file's text: Directory lists names and Image reads `image_paths`.
        operations = raw_params.get("operations")
        if isinstance(operations, (list, tuple)):
            for op in operations:
                if isinstance(op, dict) and op.get("mode") == "Line":
                    path = op.get("path")
                    if isinstance(path, str):
                        out.append(path)
    if isinstance(command, str) and command:
        for segment in _shell_segments_reading_content(command):
            out.extend(sk._SHELL_SKILL_PATH_RE.findall(segment))
    return out


def _shell_segments_reading_content(command: str) -> list[str]:
    """Segments of *command* whose leading verb delivers file content.

    A segment's verb is its first bare token; leading environment assignments
    (``FOO=bar cat x``) and absolute paths (``/bin/cat``) are tolerated.
    """
    from kiro_crew import skills as sk  # circular import: the facade imports this module

    reading: list[str] = []
    for segment in sk._SHELL_SEGMENT_RE.split(command):
        for token in segment.split():
            if "=" in token and not token.startswith("-"):
                continue  # leading VAR=value assignment
            verb = token.rsplit("/", 1)[-1]
            if verb in sk._SHELL_READ_VERBS:
                reading.append(segment)
            break  # only the segment's first bare token is its verb
    return reading


def _served_key_by_realpath(loader: SkillsLoader) -> dict[str, str]:
    """Map each served skill file's realpath to its canonical served key.

    Applies the same canonical rule as ``resolve_ledger_aliases`` — the real
    file's key beats a symlink's, then alphabetical — so a read through a
    symlinked skill is credited to the key the budget screen displays rather
    than splitting one file's cost across two rows. Uncached and
    resolve()-bound for the same reason stated there, so callers must gate
    it behind a cheap check rather than running it per tool call.
    """
    by_realpath: dict[str, list[tuple[str, Path]]] = {}
    for key, skill_file, _within in loader._iter():
        try:
            rp = str(skill_file.resolve())
        except (OSError, RuntimeError):
            # A cyclic symlink raises RuntimeError, not OSError.
            continue
        by_realpath.setdefault(rp, []).append((key, skill_file))
    return {
        rp: min(pairs, key=lambda p: (p[1].is_symlink(), p[0]))[0]
        for rp, pairs in by_realpath.items()
    }


def resolve_tool_read_keys(
    loader: SkillsLoader,
    tool_name: str = "",
    raw_params: dict | None = None,
    command: str | None = None,
) -> list[str]:
    """Served skill keys whose body a tool call is about to deliver.

    Resolution only — nothing is recorded, so the caller can run this off
    the event loop and credit later, once the read is confirmed to have
    completed. Returns keys deduped, so one command naming a file twice
    yields it once.

    Only content-delivering reads qualify (see
    ``_tool_read_path_candidates``): a tool call that merely names a skill
    path earns nothing, because the ledger's hits mean a body reached the
    model.

    Filesystem-bound (``_iter`` plus a ``resolve()`` per served skill), so
    candidates are filtered on the ``SKILL.md`` basename first and callers
    must keep this off the event loop.
    """
    from kiro_crew import skills as sk  # circular import: the facade imports this module

    if loader._usage is None:
        return []
    candidates = [
        c for c in _tool_read_path_candidates(tool_name, raw_params, command) if sk._SKILL_FILE in c
    ]
    if not candidates:
        # The read-intent allowlists (`_CONTENT_READ_TOOLS`,
        # `_SHELL_READ_VERBS`) encode the provider's current tool spellings.
        # A rename would silently restore the pre-existing undercount with
        # nothing failing, so a call that clearly names a skill yet yields no
        # candidate is logged — the one signal that distinguishes drift from
        # a legitimately non-reading tool call.
        if sk.names_skill_file(raw_params, command):
            logger.debug(
                "skill-read: %r names a skill but is not a content read "
                "(tool=%r); check the read-intent allowlists if the provider "
                "renamed its tools",
                command or raw_params,
                tool_name,
            )
        return []
    try:
        realpath_to_key = loader._served_key_by_realpath()
    except OSError:
        return []
    keys: list[str] = []
    for cand in candidates:
        try:
            rp = str(Path(cand).expanduser().resolve())
        except (OSError, RuntimeError, ValueError):
            continue
        key = realpath_to_key.get(rp)
        if key is not None and key not in keys:
            keys.append(key)
    return keys


def credit_skill_reads(loader: SkillsLoader, keys: list[str]) -> None:
    """Record a delivery for each key in *keys*. Best-effort, never raises.

    Separate from ``resolve_tool_read_keys`` so the credit lands only after
    the read has actually completed — a tool call that was denied or failed
    must not leave a delivery behind.
    """
    for key in keys:
        loader._record_use(key)


def resolve_ledger_aliases(loader: SkillsLoader) -> dict[str, list[str]]:
    """Map served skill keys to ledger keys that resolve to the same file.

    Returns ``{served_key: [alias_key, ...]}`` — only entries with at least
    one alias appear. Unresolvable ledger keys (no SKILL.md on disk) are
    dropped silently.

    The result is NOT cached. It depends on what each served path currently
    resolves to, so any sound cache key would have to resolve every served
    file — the same work the cache would save. `_iter()` has its own TTL, so
    repeat calls (e.g. dashboard refreshes) do not re-walk the skills tree.

    This is the public seam for *alias resolution* specifically — the budget
    endpoint does not build the map itself. It still reads other loader
    internals to assemble its rows, so this is one step out of that coupling,
    not the end of it. It deliberately does NOT live inside ``list_skills()``
    — that method guarantees one stat per skill and runs on the hot path
    during context assembly; filesystem resolution here is acceptable only
    at dashboard-refresh frequency.
    """
    if loader._usage is None:
        return {}

    snapshot = loader._usage.snapshot()
    if not snapshot:
        return {}

    # NOT cached, deliberately. The map is a function of the ledger's keys
    # AND of what each served path currently RESOLVES to, so a sound cache key
    # has to resolve every served file — exactly the work a cache would be
    # there to avoid. Keying on names alone was demonstrably unsound: deleting
    # an alias, or retargeting a served symlink, changes no name, so a hit
    # kept crediting deliveries to the wrong skill. A cache that is only
    # correct when nothing moved is worse than no cache, and `_iter()` already
    # carries its own TTL, so repeat calls do not re-walk the tree.
    # Root dropped at this boundary: the budget view only needs identity and
    # size, and never reads a body through the confined reader.
    skill_pairs = [(n, pth) for n, pth, _w in loader._iter()]

    # Group served keys by resolved path. Two served keys CAN name the same
    # file: a file-level symlink (`old/SKILL.md` -> `new/SKILL.md`) leaves
    # both directories real, so `_iter()` yields both. Treating each as its
    # own skill splits one file's cost across two rows, which is the very
    # thing this fold exists to prevent — so one key per file is canonical
    # and the rest are aliases.
    by_realpath: dict[str, list[tuple[str, Path]]] = {}
    for key, skill_file in skill_pairs:
        try:
            rp = str(skill_file.resolve())
        except (OSError, RuntimeError):
            # A cyclic symlink raises RuntimeError("Symlink loop from ..."),
            # NOT OSError, so it must be caught explicitly or one bad link
            # takes the whole endpoint down with a 500.
            continue
        by_realpath.setdefault(rp, []).append((key, skill_file))

    realpath_to_served: dict[str, str] = {}
    alias_map: dict[str, list[str]] = {}
    for rp, pairs in by_realpath.items():
        # The real file's key beats a symlink's, then alphabetical — so the
        # winner does not depend on directory iteration order.
        canonical, _ = min(pairs, key=lambda p: (p[1].is_symlink(), p[0]))
        realpath_to_served[rp] = canonical
        for key, _ in pairs:
            if key != canonical:
                alias_map.setdefault(canonical, []).append(key)

    # Roots to resolve a ledger key against. `_iter()` serves the main skills
    # dir AND every extra path (an installed app's own skills dir), and each
    # names its skills relative to its OWN root — so an app skill's alias key
    # only resolves under that app's root. Resolving against `_dir` alone
    # silently drops every app-skill alias.
    roots = [loader._dir, *loader._extra_paths]

    # A ledger key that does not name a served skill: resolve it on disk and
    # fold it into whichever served key shares its file.
    for ledger_key in snapshot:
        if ledger_key in realpath_to_served.values():
            continue  # Already the canonical key for its file.
        if any(ledger_key in a for a in alias_map.values()):
            continue  # Already folded as a served alias above.
        for root in roots:
            candidate = root / ledger_key / "SKILL.md"
            try:
                rp = str(candidate.resolve())
            except (OSError, RuntimeError):
                continue  # Unresolvable or a symlink loop — try the next root.
            if not Path(rp).exists():
                continue
            served_key = realpath_to_served.get(rp)
            if served_key is None:
                continue
            if ledger_key != served_key:
                alias_map.setdefault(served_key, []).append(ledger_key)
            break  # First root that resolves wins; a key names one file.

    for aliases in alias_map.values():
        aliases.sort()

    return alias_map


def _record_use(loader: SkillsLoader, key: str) -> None:
    """Best-effort usage bump for the lazy-load ranking. Never raises."""
    if loader._usage is None:
        return
    try:
        loader._usage.record(key)
    except Exception:  # pragma: no cover — telemetry must not break injection
        pass
