"""Broker overlays for crewmate files, and the worker session's lookup of them.

A crewmate's worker runs on ``kirocrew-mate-worker``, whose base spec and the
crewmate's own file both live in ``~/.kiro/mates/agents/`` (``rfc-mate-owned-agent-
config``). The agents rewrite (:func:`kiro_crew.mcp_gateway.rewriter.rewrite_agents`)
reads ``~/.kiro/agents/`` plus the conductor spec only
(:func:`with_conductor_source`), so a stubbed server the crewmate file declares had
no overlay: the worker session got no stub, and KAS declared the server directly,
where its non-``${NAME}`` ``env`` is withheld and it starts without credentials.

This module lets the SAME pass cover the crewmate files, with the same reader,
the same launch approvals and the same sidecar ledger, under three separations:

* Overlays go to ``<overlay_dir>/mates/<id>.json``, a subdirectory no by-agent-name
  overlay lookup reads, so a crewmate id can never answer a session of an
  ``~/.kiro/agents`` agent of that name, nor the reverse.
* The stub's ``--agent`` identity is ``crewmate:<id>``, so its env sidecar
  (``env_sidecar_name(agent, server)``) and its caller identity are the
  crewmate's own. The ``crewmate:`` prefix is reserved: an ``~/.kiro/agents``
  spec whose name carries it gets no overlay of its own (``rewrite_agents``), so
  no stub of an ordinary session carries a crewmate identity, and one DECLARING
  a crewmate's identity is also a collision for which the crewmate gets no
  overlay. App-callback governance
  reads that identity back off the spool record, and
  :func:`governance_agents` turns it into the agent names whose task profiles
  apply: the worker's own ``kirocrew-mate-worker``, the profile its session's
  tool calls get, AND the identity itself. Every one must permit the call.
* A worker session finds its overlay through its own execution record
  (:func:`worker_overlay_spec`), never through the shared agent name, and only the
  servers the crewmate ADDS reach it, carrying the governed merged spec's
  ``disabledTools`` rather than the file's and never an ``autoApprove`` (on KAS
  the tool restriction itself is the merged spec's ``excludedTools``).
* A crewmate whose file id an ``~/.kiro/agents`` spec also has gets no overlay:
  ``mate_worker.mate_file_for_member`` refuses that binding, so its stub and
  credential sidecar would serve no session.

Every doubt fails closed: an unverifiable listing, owner record, file or overlay
yields no stub and a log line. The session then declares the server itself, as it
did before this module, which is never broader than the stub would be. A fault
anywhere in the crewmate half is caught by the pass, which then drops every
crewmate overlay and sidecar it wrote and runs the ``~/.kiro/agents`` half as it
did before.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Collection

from kiro_crew import platform_compat
from kiro_crew.agent_files import (
    MATE_CONDUCTOR_AGENT_FILENAME,
    MATE_WORKER_AGENT_FILENAME,
    is_mate_service_agent,
)
from kiro_crew.mcp_gateway.rewriter_launch import _WRAPPER_MARKER, _WRAPPER_MARKER_LEGACY
from kiro_crew.mcp_gateway.rewriter_overlay import _source_sig

# Logged under the rewriter's logger so an operator reading one pass reads one stream.
logger = logging.getLogger("kiro_crew.mcp_gateway.rewriter")

#: The agent a crewmate's worker sessions run on.
MATE_WORKER_AGENT = Path(MATE_WORKER_AGENT_FILENAME).stem
#: The overlay subdirectory crewmate overlays are written to.
MATE_OVERLAY_SUBDIR = "mates"
#: The prefix of a crewmate stub's ``--agent`` identity.
MATE_IDENTITY_PREFIX = "crewmate:"


def mate_identity(file_id: str) -> str:
    """The stub identity of crewmate file *file_id*: ``crewmate:<id>``."""
    return f"{MATE_IDENTITY_PREFIX}{file_id}"


def mate_overlay_dir(overlay_dir: Path | str) -> Path:
    """Where the crewmate overlays of *overlay_dir* live."""
    return Path(overlay_dir) / MATE_OVERLAY_SUBDIR


@dataclass(frozen=True)
class MateSources:
    """The crewmate files one pass rewrites.

    ``complete`` is False when something deciding the set could not be read (the
    listing, the owner record): the pass then writes no crewmate overlay and is
    not cached, so the next one retries.
    """

    files: tuple[Path, ...] = ()
    complete: bool = True


def governance_agents(identity: str) -> tuple[str, ...] | None:
    """The agent names whose task profiles govern an app callback from *identity*.

    *identity* is the producing stub's ``--agent``, read off the spool record
    (``app_call._agent_for_call``). Any identity but a crewmate's is its own one
    name, as before. A crewmate stub (``crewmate:<id>``) runs inside a
    crewmate's worker session, whose own tool calls are governed as
    ``kirocrew-mate-worker``, so that name applies; the identity itself applies
    too, so a profile bound to it (or to an ``~/.kiro/agents`` agent that
    declares that name) is not dropped. The caller resolves the first name fully
    and each later one by its task bind only, and denies when any denies, the
    narrowest of them. ``None`` when *identity* is a
    crewmate identity whose id is not a file id: the caller denies.
    """
    if not identity.startswith(MATE_IDENTITY_PREFIX):
        return (identity,)
    file_id = identity[len(MATE_IDENTITY_PREFIX) :]
    if not _is_file_id(file_id):
        return None
    return (MATE_WORKER_AGENT, identity)


def _is_file_id(name: str) -> bool:
    """Whether *name* names one file in a folder rather than a path."""
    return bool(name) and "/" not in name and "\\" not in name and name not in (".", "..")


def mate_dir_refused(target: Path) -> bool:
    """Whether the crewmate overlay folder *target* must not be used.

    True, with a log line, when it exists as a link (a symlink or a Windows
    junction) or as anything but a folder: following a planted link would have
    the gateway write credential-bearing overlays, unlink files, or read stubs
    somewhere else. An absent folder is not refused. Each pass checks it before
    it creates, writes into, prunes or reads the folder. This is check-then-use:
    the later path-following operation does not re-check, so a swap landing in
    between is not caught. The top-level overlay folder has the same race.
    """
    if platform_compat.is_link_or_junction(target):
        logger.warning("crewmate overlay folder %s is a link; refusing to use it", target)
        return True
    try:
        mode = os.lstat(target).st_mode
    except FileNotFoundError:
        return False
    except OSError as exc:
        logger.warning(
            "crewmate overlay folder %s cannot be checked (%s); refusing it", target, exc
        )
        return True
    if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
        logger.warning("crewmate overlay folder %s is not a plain folder; refusing it", target)
        return True
    return False


def _is_plain_file(path: Path) -> bool:
    """Whether *path* is a regular file and not a symlink (``lstat``)."""
    try:
        return stat.S_ISREG(os.lstat(path).st_mode)
    except OSError:
        return False


def with_conductor_source(sources: list[Path], mate_dir: Path | None) -> list[Path]:
    """*sources*, the ``~/.kiro/agents`` specs, plus the crewmate conductor's spec.

    Every crewmate's chat runs the one stock conductor spec in *mate_dir* and its
    session looks stubs up by that agent name, so the agents half rewrites the spec
    like an ``~/.kiro/agents`` one, under its own name. It takes the place of a
    source with the same stem, which no crewmate chat runs. *sources* comes back
    unchanged when *mate_dir* is ``None``, a symlink or not a folder (as
    :func:`list_mate_sources` refuses it), or holds no plain conductor file.
    Ordered by file name, the order the cached path serves overlays in.
    """
    if mate_dir is None:
        return sources
    try:
        mode = os.lstat(mate_dir).st_mode
    except OSError:
        return sources
    conductor = mate_dir / MATE_CONDUCTOR_AGENT_FILENAME
    if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode) or not _is_plain_file(conductor):
        return sources
    kept = [p for p in sources if p.stem.casefold() != conductor.stem.casefold()]
    return sorted([*kept, conductor], key=lambda p: p.name)


def list_mate_sources(mate_dir: Path) -> MateSources:
    """The crewmate files under *mate_dir* this pass may rewrite.

    Taken: a plain ``<id>.json`` / ``<id>.md`` that is not the mate conductor or
    worker spec and that the crewmate owner record names (an id this instance
    wrote). Skipped with a log line, and treated as absent rather than as a
    doubt: a symlinked or non-folder *mate_dir*, a symlinked file, and an id the
    record does not name. The set stays complete (cacheable) and sets no
    ``live_incomplete``, as the agents loop treats a source its hardened reader
    refuses. Only an unreadable listing or owner record makes it incomplete.
    Blocking.
    """
    from kiro_crew.agent_spec_format import iter_agent_spec_files_strict

    try:
        mode = os.lstat(mate_dir).st_mode
    except FileNotFoundError:
        return MateSources()
    except OSError as exc:
        logger.warning(
            "crewmate folder %s cannot be read (%s); no crewmate overlays", mate_dir, exc
        )
        return MateSources(complete=False)
    if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
        logger.warning(
            "crewmate folder %s is a symlink or not a folder; no crewmate overlays", mate_dir
        )
        return MateSources()
    try:
        listing = iter_agent_spec_files_strict(mate_dir)
    except OSError as exc:
        logger.warning(
            "crewmate folder %s cannot be listed (%s); no crewmate overlays", mate_dir, exc
        )
        return MateSources(complete=False)
    from kiro_crew.mate_config import MateConfigError, crewmate_file_on_record

    taken: list[Path] = []
    for path in listing:
        if is_mate_service_agent(path.stem):
            continue
        if not _is_plain_file(path):
            logger.warning("crewmate file %s is a symlink or not a file; no overlay for it", path)
            continue
        try:
            on_record = crewmate_file_on_record(path.stem)
        except MateConfigError as exc:
            logger.warning("%s; no crewmate overlays this pass", exc)
            return MateSources(complete=False)
        if not on_record:
            logger.warning(
                "crewmate file %s is not on this instance's crewmate record; no overlay for it",
                path,
            )
            continue
        taken.append(path)
    return MateSources(tuple(taken))


def establish_mate_sources(mate_dir: Path) -> tuple[MateSources, dict[str, Any]]:
    """:func:`list_mate_sources` and its fingerprint input, never raising.

    Any fault yields an incomplete, empty set: the crewmate half must never cost
    the ``~/.kiro/agents`` half of the pass its run.
    """
    try:
        sources = list_mate_sources(mate_dir)
        return sources, mate_sources_fingerprint(mate_dir, sources)
    except Exception:  # noqa: BLE001 - a crewmate fault never breaks the agents pass
        logger.warning(
            "crewmate folder %s could not be established; no crewmate overlays",
            mate_dir,
            exc_info=True,
        )
        empty = MateSources(complete=False)
        return empty, {"dir": str(mate_dir), "complete": False, "files": {}}


def mate_sources_fingerprint(mate_dir: Path, sources: MateSources) -> dict[str, Any]:
    """The fingerprint input the crewmate files contribute to one pass."""
    return {
        "dir": str(mate_dir),
        "complete": sources.complete,
        "files": {p.name: _source_sig(p) for p in sources.files},
    }


@dataclass
class MatePass:
    """What the crewmate half of one pass wrote."""

    written: set[str] = field(default_factory=set)
    results: dict[str, int] = field(default_factory=dict)
    incomplete: bool = False


def rewrite_mate_overlays(
    sources: MateSources,
    *,
    overlay_dir: Path,
    taken_identities: Collection[str] | None,
    publish: Callable[[dict[str, Any], Path], int | None],
    agents_dir: Path | None = None,
) -> MatePass:
    """Write the crewmate overlays of one pass, then prune the stale ones.

    *taken_identities* is every agent identity the ``~/.kiro/agents`` half of the
    pass published, or ``None`` when it could not be established (a kept overlay
    that cannot be read): then no crewmate overlay is written, since a collision
    could not be ruled out. *agents_dir* is that half's source folder: a crewmate
    whose file id it also holds a spec for gets no overlay, the binding
    ``mate_worker.mate_file_for_member`` refuses. *publish* rewrites one spec and
    writes it to the given path, returning its wrapped-server count or ``None``
    when the write failed.
    """
    out = MatePass(incomplete=not sources.complete)
    target_dir = mate_overlay_dir(overlay_dir)
    if mate_dir_refused(target_dir):
        # Nothing is written into, or pruned from, a folder that is not ours.
        out.incomplete = True
        return out
    platform_compat.make_owner_only_dir(target_dir)
    if mate_dir_refused(target_dir):
        # Planted between the check and the create.
        out.incomplete = True
        return out
    if taken_identities is None and sources.files:
        logger.warning(
            "an agent overlay kept this pass cannot be read, so a crewmate identity "
            "collision cannot be ruled out; no crewmate overlays this pass"
        )
        out.incomplete = True
    else:
        _write_each(sources, target_dir, set(taken_identities or ()), publish, out, agents_dir)
    for stale in target_dir.glob("*.json"):
        if stale.name not in out.written:
            with contextlib.suppress(OSError):
                stale.unlink()
    return out


def drop_mate_overlays(overlay_dir: Path) -> None:
    """Remove every crewmate overlay, never raising. Nothing is pruned through a link."""
    try:
        target_dir = mate_overlay_dir(overlay_dir)
        if mate_dir_refused(target_dir):
            return
        for stale in target_dir.glob("*.json"):
            with contextlib.suppress(OSError):
                stale.unlink()
    except Exception:  # noqa: BLE001 - best effort on a pass already failing closed
        logger.warning("crewmate overlays could not be removed", exc_info=True)


def _write_each(
    sources: MateSources,
    target_dir: Path,
    taken: set[str],
    publish: Callable[[dict[str, Any], Path], int | None],
    out: MatePass,
    agents_dir: Path | None = None,
) -> None:
    from kiro_crew.agent_discovery import read_agent_spec_strict
    from kiro_crew.mcp_gateway.rewriter import _overlay_names_collide

    # Overlay names by casefold, as the agents loop keeps them: a second name
    # that folds to a claimed one is skipped only when the two really are one
    # file in *target_dir* (a case-insensitive folder), never just for folding.
    claimed: dict[str, str] = {}
    for path in sources.files:
        identity = mate_identity(path.stem)
        overlay_name = f"{path.stem}.json"
        if identity in taken:
            logger.warning(
                "crewmate %r: an agent in ~/.kiro/agents declares the name %r, which is "
                "this crewmate's broker identity; no overlay for the crewmate until one "
                "is renamed",
                path.stem,
                identity,
            )
            continue
        shadowed = _shadowed_by_agent(agents_dir, path.stem)
        if shadowed is None:
            out.incomplete = True
            continue
        if shadowed:
            logger.warning(
                "crewmate %r: an agent in ~/.kiro/agents has the same name, so no "
                "session binds this crewmate file; no overlay for it until one is renamed",
                path.stem,
            )
            continue
        first = claimed.get(overlay_name.casefold())
        if first is not None and _overlay_names_collide(target_dir, first, overlay_name):
            logger.warning(
                "crewmate %r: its overlay is the same file as %s on this case-insensitive "
                "folder; no overlay for it",
                path.stem,
                first,
            )
            continue
        try:
            spec = read_agent_spec_strict(path, operation="mcp_overlay_rewrite", source="unknown")
        except FileNotFoundError:
            out.incomplete = True
            continue
        except (OSError, ValueError) as exc:
            # Fail closed for a transient fault too: no stub, and the pass is not
            # cached, so the next one retries.
            logger.warning("crewmate file %s unreadable (%s); no overlay for it", path, exc)
            out.incomplete = out.incomplete or not isinstance(exc, ValueError)
            continue
        if not isinstance(spec, dict):
            logger.warning("crewmate file %s is not an agent spec; no overlay for it", path)
            continue
        claimed.setdefault(overlay_name.casefold(), overlay_name)
        # The file's own ``name`` is not trusted for the identity: it is the id.
        spec["name"] = identity
        wrapped = publish(spec, target_dir / overlay_name)
        if wrapped is None:
            out.incomplete = True
            continue
        out.written.add(overlay_name)
        if wrapped:
            out.results[f"{MATE_OVERLAY_SUBDIR}/{overlay_name}"] = wrapped


def _shadowed_by_agent(agents_dir: Path | None, file_id: str) -> bool | None:
    """Whether *agents_dir* holds a spec for *file_id*; ``None`` when it cannot tell.

    The same question ``mate_worker.mate_file_for_member`` asks of the user-level
    folder before it lets a binding own the file.
    """
    if agents_dir is None:
        return False
    from kiro_crew.agent_sdk import agent_specs

    try:
        return not agent_specs.agent_spec_absent(agents_dir, file_id)
    except Exception as exc:  # noqa: BLE001 - an unscannable folder rules nothing out
        logger.warning(
            "crewmate %r: %s cannot be checked for a spec of that name (%s); no overlay for it",
            file_id,
            agents_dir,
            exc,
        )
        return None


def kept_identities(overlay_dir: Path, names: Collection[str]) -> set[str] | None:
    """The ``name`` of each kept agent overlay in *names*, ``None`` if one is unreadable."""
    found: set[str] = set()
    for name in names:
        try:
            spec = json.loads((overlay_dir / name).read_text(encoding="utf-8"))
        except FileNotFoundError:
            continue  # kept, but there was none: nothing it could collide with
        except (OSError, ValueError):
            return None
        if not isinstance(spec, dict) or not isinstance(spec.get("name"), str):
            return None
        found.add(spec["name"])
    return found


def _read_mate_overlay(path: Path) -> dict[str, Any] | None:
    if not _is_plain_file(path):
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        logger.warning("crewmate overlay %s is unreadable; no stubs for it", path)
        return None
    return data if isinstance(data, dict) else None


def _servers(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def worker_overlay_spec(overlay_dir: Path, session_key: str | None) -> dict[str, Any] | None:
    """The overlay worker session *session_key* injects stubs from, or ``None``.

    Positive identity: the session's own execution record must be a crewmate's
    worker (``mate_worker.is_own_worker_execution``), its crewmate must own a file
    (``mate_file_for_member``), and that file's overlay must carry its identity.
    Only a wrapped server the crewmate ADDS is returned (a base server always wins
    the merge), and only one the governed merged worker spec still mounts
    unmuted and not registry-governed (``mcp_cleanup.mcp_entry_is_registry_governed``),
    so a stub from an earlier pass is not injected for an entry since retyped
    ``type: registry``. Each takes that spec's ``disabledTools`` in place of the file's and
    drops ``autoApprove``, which the KAS projection strips too, since a
    host-approved call skips the PreToolUse gate; KAS discards ``disabledTools`` on a projected entry, so what actually
    restricts the worker's tools there is the merged spec's ``excludedTools`` on
    the session's custom agent, not this copy. The overlay is the
    last pass's, so a crewmate file edited since runs on its last approved stub
    until the next pass, as an ``~/.kiro/agents`` spec does. Blocking.
    """
    if not session_key:
        return None
    from kiro_crew import mate_worker
    from kiro_crew.agent_sdk import agent_specs
    from kiro_crew.config import paths as paths_mod
    from kiro_crew.execution_context import read_session_execution
    from kiro_crew.mcp_cleanup import mcp_entry_is_muted, mcp_entry_is_registry_governed

    try:
        execution = read_session_execution(session_key)
        if not mate_worker.is_own_worker_execution(execution):
            return None
        name = mate_worker.mate_file_for_member(str(execution.member_id))  # type: ignore[union-attr]
        if not _is_file_id(name):
            # No file, or a binding that names a path rather than a file id.
            return None
        mates = mate_overlay_dir(overlay_dir)
        if mate_dir_refused(mates):
            return None
        overlay = _read_mate_overlay(mates / f"{name}.json")
        if overlay is None:
            return None
        if overlay.get("name") != mate_identity(name):
            logger.warning("crewmate overlay for %r carries another identity; no stubs", name)
            return None
        base = agent_specs.load_agent_spec(paths_mod.mate_agents_dir(), MATE_WORKER_AGENT)
        merged = mate_worker.worker_session_spec({**base, "prompt": ""}, session_key)
    except Exception:  # noqa: BLE001 - anything unverifiable injects nothing
        logger.warning(
            "worker session %s: crewmate overlay not verifiable; no stubs",
            session_key,
            exc_info=True,
        )
        return None
    base_servers = _servers(base.get("mcpServers"))
    governed = _servers(merged.get("mcpServers"))
    stubs: dict[str, Any] = {}
    for server, entry in _servers(overlay.get("mcpServers")).items():
        if not isinstance(entry, dict) or not (
            entry.get(_WRAPPER_MARKER) or entry.get(_WRAPPER_MARKER_LEGACY)
        ):
            continue
        mounted = governed.get(server)
        if (
            server in base_servers
            or not isinstance(mounted, dict)
            or mcp_entry_is_muted(mounted)
            or mcp_entry_is_registry_governed(mounted)
        ):
            continue
        shaped = dict(entry)
        # ``autoApprove`` is never sent: a host-approved call skips the PreToolUse
        # gate, and the KAS projection strips it for the same reason
        # (``kas_agents._project_mcp_servers``). ``disabledTools`` is the governed
        # spec's value, never the file's.
        shaped.pop("autoApprove", None)
        value = mounted.get("disabledTools")
        if isinstance(value, list):
            shaped["disabledTools"] = list(value)
        else:
            shaped.pop("disabledTools", None)
        stubs[server] = shaped
    return {"name": overlay["name"], "mcpServers": stubs}
