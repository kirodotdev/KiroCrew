"""Durable template/member choices, separate from private-memory authority."""

from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.loader import _config_write_lock, resolve_agent_bindings
from kiro_crew.config.paths import config_dir
from kiro_crew.memory_stores import UnknownMemoryStore
from kiro_crew.session_pid_sig import _read_regular_nofollow

if TYPE_CHECKING:
    from kiro_crew.config.sections import ResolvedBindings

SelectionChange = tuple[dict[str, Any] | None, dict[str, Any]]


def _selection_path(session_key: str) -> Path:
    if not isinstance(session_key, str) or not session_key:
        raise UnknownMemoryStore("Conversation selection has no session identity")
    digest = hashlib.sha256(session_key.encode()).hexdigest()
    # This existing top-level directory is READONLY in every agent sandbox.
    # Transcript metadata never supplies a selection or authorizes its creation.
    path = config_dir().resolve() / "member-memory-bindings" / "agent-selections" / f"{digest}.json"
    if path.resolve() != path:
        raise UnknownMemoryStore("Conversation selection path is redirected")
    return path


def _read_selection(path: Path, session_key: str) -> dict[str, Any] | None:
    raw = _read_regular_nofollow(path)
    if raw is None:
        try:
            path.lstat()
        except FileNotFoundError:
            return None
        raise UnknownMemoryStore("Conversation selection is unreadable")
    try:
        row = json.loads(raw)
    except ValueError as exc:
        raise UnknownMemoryStore("Conversation selection is invalid") from exc
    if (
        not isinstance(row, dict)
        or type(row.get("version")) is not int
        or row["version"] != 1
        or row.get("session_key") != session_key
        or not isinstance(row.get("agent"), str)
        or not row["agent"]
        or row.get("kind") not in ("member", "template")
        or not isinstance(row.get("revision"), str)
        or not row["revision"]
    ):
        raise UnknownMemoryStore("Conversation selection is invalid")
    return row


def session_agent_selection_name(session_key: str) -> str | None:
    """Read the committed choice when restoring an editable transcript."""
    try:
        row = _read_selection(_selection_path(session_key), session_key)
    except (OSError, RuntimeError) as exc:
        raise UnknownMemoryStore("Conversation selection is unreadable") from exc
    return str(row["agent"]) if row is not None else None


def session_agent_selection_kind(session_key: str, agent_name: str) -> str:
    """Return positive protected provenance, never infer it from absent private memory."""
    try:
        row = _read_selection(_selection_path(session_key), session_key)
    except (OSError, RuntimeError) as exc:
        raise UnknownMemoryStore("Conversation selection is unreadable") from exc
    return str(row["kind"]) if row is not None and row["agent"] == agent_name else ""


def resolve_session_agent_bindings(
    resolver, config, session_key: str, agent_name: str | None, *project_dir: str | None
) -> ResolvedBindings:
    """Resolve in the recorded namespace; absence retains strict legacy resolution."""
    try:
        row = _read_selection(_selection_path(session_key), session_key)
    except (OSError, RuntimeError) as exc:
        raise UnknownMemoryStore("Conversation selection is unreadable") from exc
    selected = agent_name or config.default_agent
    if row is not None and row["agent"] != selected:
        raise UnknownMemoryStore(
            "Conversation agent conflicts with its protected selection; select the agent again"
        )
    kind = str(row["kind"]) if row is not None else ""
    try:
        if kind:
            bindings = resolver(config, agent_name, *project_dir, selection_kind=kind)
        else:
            bindings = resolver(config, agent_name, *project_dir)
    except StopIteration as exc:
        # asyncio cannot deliver StopIteration through the off-loop Future.
        raise UnknownMemoryStore("Conversation agent selection is unavailable") from exc
    bindings.selection_revision = row["revision"] if row is not None else ""
    return bindings


def record_provider_agent_switch(
    config,
    session_key: str,
    prior_agent: str | None,
    new_agent: str,
    project_dir: str | None,
) -> SelectionChange | None:
    """Publish a live provider switch before its name reaches transcript metadata."""
    prior = resolve_session_agent_bindings(
        resolve_agent_bindings, config, session_key, prior_agent, project_dir
    )
    # Provider events name provider templates, not configured member aliases.
    selected = resolve_agent_bindings(config, new_agent, project_dir, selection_kind="template")
    if not selected.requested_resolved:
        raise UnknownMemoryStore("Conversation agent selection is unavailable")
    selected.selection_revision = prior.selection_revision
    return record_agent_selection(session_key, new_agent, selected)


def record_agent_selection(
    session_key: str,
    agent_name: str | None,
    bindings: ResolvedBindings,
    *,
    replace: bool = False,
) -> SelectionChange | None:
    """Record a validated dispatch or an authorized explicit choice.

    This grants no memory access: private-session assignment and ownership are
    still checked independently before provider allocation. An automatic dispatch
    cannot replace a choice made while its resolution was in flight.
    """
    kind = getattr(bindings, "selection_kind", "")
    selected = agent_name or bindings.resolved_alias
    if kind not in ("member", "template") or not selected or not bindings.requested_resolved:
        return None
    path = _selection_path(session_key)
    row = {
        "version": 1,
        "session_key": session_key,
        "agent": str(selected),
        "kind": kind,
    }
    # Serialize the comparison and publication: a delayed automatic dispatch
    # must not overwrite an explicit owner pick committed during its lookup.
    with _config_write_lock(path):
        prior = _read_selection(path, session_key)
        if prior is not None and all(prior.get(k) == v for k, v in row.items()) and not replace:
            return None
        observed_revision = getattr(bindings, "selection_revision", None)
        current_revision = prior["revision"] if prior is not None else ""
        if not replace and current_revision != (observed_revision or ""):
            raise UnknownMemoryStore("Conversation selection changed during preparation")
        # Even a same-value explicit pick gets a token: rollback must not erase
        # another request's successful selection while its write awaited.
        row["revision"] = uuid.uuid4().hex
        atomic_write(path, json.dumps(row), mode=0o600, fsync=True)
        return prior, row


def restore_agent_selection(session_key: str, change: SelectionChange | None) -> None:
    """Undo only this request's publication after its slot commit loses ownership."""
    if change is None:
        return
    prior, published = change
    path = _selection_path(session_key)
    with _config_write_lock(path):
        if _read_selection(path, session_key) != published:
            return
        if prior is None:
            path.unlink()
        else:
            atomic_write(path, json.dumps(prior), mode=0o600, fsync=True)
