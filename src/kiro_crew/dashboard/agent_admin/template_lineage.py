"""Template spec files and crew lineage, shared by fork, publish, reset, the default-agent write and the crew writers: the exclusive spec write, the template scan, the locked rebind, the private-copy ownership read and the referenced-copy cleanup."""

from __future__ import annotations

import contextlib
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from kiro_crew.dashboard.handlers.agents import (
        _WINDOWS_RESERVED_NAMES,
        CapabilityError,
        _read_agent_spec,
        _roster_mask,
        agent_spec_candidates,
        agent_state,
        agents_spec_lock,
        config_local_path,
        iter_agent_spec_files,
        kiro_agents_dir_path,
        logger,
        require_unmanaged_template,
        sanitize_agent_config_governance,
        spec_str,
        update_config_locked,
    )


def _spec_stem_on_disk(agents_dir: Path, name: str) -> bool:
    """True when ``<name>.json`` OR ``<name>.md`` exists in *agents_dir*.

    Every writer that mints a new ``<name>.json`` asks this rather than testing
    the JSON path alone: a markdown spec with the same stem is the same agent,
    and writing a JSON twin beside it would list one name twice.
    """
    return any(p.exists() for p in agent_spec_candidates(agents_dir, name))


def _is_reserved_basename(name: str) -> bool:
    return name.split(".", 1)[0].upper() in _WINDOWS_RESERVED_NAMES


def _write_spec_file(dest: Path, data: dict) -> None:
    """Exclusive create: 'x' refuses an existing destination — including a
    differing-case sibling on case-insensitive filesystems — instead of
    truncating it. Serialization happens here too so callers offload BOTH
    to a thread; a near-limit spec would otherwise stall the event loop.

    Governance runs HERE, at the single writer both fork and publish use:
    a copied spec carries its source's ``allowedTools``/``autoApprove``
    verbatim, and those are the two routes that skip the PreToolUse gate —
    per ``sanitize_agent_config_governance``'s contract, every whole-config
    writer must filter immediately before persisting.
    """
    sanitize_agent_config_governance(data)
    try:
        with open(dest, "x", encoding="utf-8") as f:
            f.write(json.dumps(data, indent=2) + "\n")
    except FileExistsError:
        # The exclusive create lost the race: dest is a CONCURRENT CREATOR'S
        # file, never ours to remove.
        raise
    except BaseException:
        # A partial write (ENOSPC) would leave a truncated, unparseable spec
        # that every later create refuses as name_taken. The create is ours
        # (exclusive), so removing it on failure is always safe. Unlink AFTER
        # the with-block closed the handle — Windows refuses to unlink an
        # open file with a sharing violation.
        with contextlib.suppress(OSError):
            dest.unlink()
        raise


class _AmbiguousTemplateName(Exception):
    """More than one spec file resolves to the requested name.

    ``paths`` lists those files when the raiser scanned them, so a refusal can
    name what the operator has to tidy.
    """

    def __init__(self, name: str, paths: tuple[Path, ...] = ()):
        super().__init__(name)
        self.paths = paths


def _load_template_specs(
    agents_dir: Path, name: str, operation: str
) -> tuple[dict[str, Any] | None, str, set[str], Path | None]:
    """Find the source spec and every name a new spec must not collide with.

    ``taken`` is case-folded: 'Reviewer' and 'reviewer' are the same file on
    the case-insensitive filesystems macOS and Windows default to. The source
    PATH is returned too so create closures can RE-READ it inside
    ``agents_spec_lock`` — this pre-lock snapshot can go stale against a
    concurrent fork refresh. A name matching MORE THAN ONE file (one by stem,
    another by declared name) raises ``_AmbiguousTemplateName``: glob order
    would otherwise pick silently, and the copy could carry the wrong
    template's contents.
    """
    source: dict[str, Any] | None = None
    source_name = name
    source_path: Path | None = None
    taken: set[str] = set()
    matches: list[Path] = []
    for f in iter_agent_spec_files(agents_dir):
        # An unreadable spec still occupies its filename.
        taken.add(f.stem.lower())
        spec = _read_agent_spec(f, operation=operation, source="dashboard")
        if spec is None:
            continue
        declared = spec_str(spec, "name")
        if declared:
            taken.add(declared.lower())
        if declared == name or f.stem == name:
            matches.append(f)
            if source is None:
                source = spec
                source_name = declared or f.stem
                source_path = f
    if len(matches) > 1:
        raise _AmbiguousTemplateName(name, tuple(matches))
    return source, source_name, taken, source_path


class _StaleBinding(Exception):
    """The crew moved off the expected template between validation and write."""


class _ForeignPrivateCopy(Exception):
    """The target became another crew's private copy before the write landed."""

    def __init__(self, owner: str):
        super().__init__(owner)
        self.owner = owner


class _PublishNameBound(Exception):
    """A crew binding references the requested publish name with no file
    behind it — creating the file would make that binding resolve to it."""


class _ForkBookkeepingFailed(Exception):
    """Sidecar lineage recording failed inside the fork's lock hold; the
    created file was already unwound."""


def _rebind_crew_locked(
    crew: str,
    expected: tuple[str, ...] | None,
    new_target: str,
    require_path: Path | None = None,
) -> None:
    """Apply ONLY the binding delta to config.json, under the advisory lock.

    A full ``cfg.save()`` snapshot races every other config writer (CLI,
    settings PUTs): it re-writes fields from a load taken before this
    handler's awaits, silently reverting concurrent changes. The stale-binding
    check re-runs inside the critical section, so the 409 also covers a rebind
    that landed after the handler's own validation read. ``expected=None``
    skips the staleness check (a deliberate last-write-wins switch); the write
    stays binding-only and locked either way.

    ``require_path``: the target's spec file, rechecked for existence INSIDE
    the critical section under the spec lock. A cross-process delete between a
    handler's validation and this write would otherwise persist a binding to
    nothing; a vanished file raises ``FileNotFoundError``.
    """

    def _mutate(data: dict) -> dict | None:
        entry = data.get("agents", {}).get(crew)
        if not isinstance(entry, dict) or (
            expected is not None and entry.get("kiro_agent") not in expected
        ):
            raise _StaleBinding()
        if require_path is not None:
            with agents_spec_lock(require_path.parent):
                if not require_path.exists():
                    raise FileNotFoundError(new_target)
        if entry.get("kiro_agent") == new_target:
            return None
        try:
            require_unmanaged_template(entry.get("kiro_agent", ""))
        except CapabilityError as exc:
            if exc.code == "capabilities_editor_required" and exc.status == 409:
                raise _StaleBinding() from None
            raise
        # Checked INSIDE the critical section, like the staleness check: a
        # fork recording lineage after a handler's pre-validation must not
        # slip another crew's private copy into this binding.
        if owner := _foreign_private_copy_owner(crew, new_target):
            raise _ForeignPrivateCopy(owner)
        entry["kiro_agent"] = new_target
        return data

    update_config_locked(mutate=_mutate)


class _UnverifiableLineage(Exception):
    """Whether a binding target is a private copy cannot be verified, so the
    binding is refused rather than allowed.

    ``reason`` names the failure class, because each one is repaired
    differently and only one of them clears by itself:

    * ``ambiguous_template_name``: two or more spec files resolve to the target
      (``paths`` lists them). It persists until all but one are removed or
      given another name.
    * ``ownership_record_unreadable``: the lineage sidecar (``paths``) exists
      but does not read as a JSON object, or its permissions refuse the read.
      It persists until the file is restored.
    * ``read_failed``: any other read error, which a retry can clear.

    ``cause`` is the underlying error, whose type and text the refusal quotes.
    """

    def __init__(
        self,
        target: str,
        reason: str = "read_failed",
        *,
        paths: tuple[Path, ...] = (),
        cause: BaseException | None = None,
    ):
        super().__init__(target)
        self.target = target
        self.reason = reason
        self.paths = paths
        self.cause = cause


def _sidecar_unverifiable(target: str, exc: BaseException) -> _UnverifiableLineage:
    """Classify a failed strict read of the lineage sidecar.

    ``agent_state._read`` raises ``ValueError`` for a file that is not JSON, not
    an object, not a single-link regular file, or over its size cap, and a
    ``PermissionError`` when the mode refuses the open. Each is the file's own
    state and meets the next read unchanged. Any other ``OSError`` can clear by
    itself, and so can a Windows ``PermissionError`` carrying a sharing or lock
    violation: an indexer, antivirus scanner or concurrent writer holding the
    file open for a moment, not a fault in the file.
    """
    # 32 ERROR_SHARING_VIOLATION, 33 ERROR_LOCK_VIOLATION.
    transient = getattr(exc, "winerror", None) in (32, 33)
    if isinstance(exc, (ValueError, PermissionError)) and not transient:
        try:
            where: tuple[Path, ...] = (agent_state._state_path(),)
        except Exception:
            where = ()
        return _UnverifiableLineage(target, "ownership_record_unreadable", paths=where, cause=exc)
    return _UnverifiableLineage(target, cause=exc)


def _lineage_unverifiable_error(name: str, exc: _UnverifiableLineage) -> str:
    """The ``error`` sentence of a ``lineage_unverifiable`` refusal for *name*.

    Only ``read_failed`` asks for a retry; the two persistent causes name the
    file to change, since a retry meets the same state. The sidecar's remedy is
    restoring it and never deleting it: with the file gone, every recorded
    private copy reads as a shared template, which is what this refusal guards.
    The quoted error is the reader's own (a JSON position, an ``OSError``),
    never file contents. Each externally written part -- the name, every path,
    the error text -- goes through ``_roster_mask``, the control the agents API
    applies to every string it returns from the agents directory: spec file
    names are written by whoever writes that directory, so a credential-shaped
    one arrives as the mask sentinel rather than verbatim.
    """
    head = f"Cannot verify whether '{_roster_mask(name)}' is a private copy"
    why = (
        f" ({type(exc.cause).__name__}: {_roster_mask(str(exc.cause))})"
        if exc.cause is not None
        else ""
    )
    if exc.reason == "ambiguous_template_name":
        listed = f" ({', '.join(_roster_mask(str(p)) for p in exc.paths)})" if exc.paths else ""
        return (
            f"{head}: more than one agent spec file resolves to that name{listed}. "
            "Keep one of them, or give each other copy its own name and file name, "
            "then try again."
        )
    if exc.reason == "ownership_record_unreadable":
        where = _roster_mask(str(exc.paths[0])) if exc.paths else agent_state._STATE_FILENAME
        return (
            f"{head}: its ownership record {where} cannot be read{why}. "
            "Restore that file as a plain JSON object under "
            f"{agent_state.STATE_MAX_BYTES // (1024 * 1024)} MiB (from a backup, "
            "or by fixing its syntax or permissions, or by replacing a symlinked or "
            "hard-linked file with a regular copy), then try again. Do not delete it: "
            "without it every crew's private copy reads as a shared template."
        )
    return f"{head}{why}; retry."


def _foreign_private_copy_owner(crew: str, target: str) -> str | None:
    """The owning crew's name when *target* is ANOTHER crew's private copy.

    Lineage means one crew's edits land on that file: binding a second crew to
    it has publish/reset cleanup delete the second crew's live template out
    from under it. STRICT read: a lenient read let a corrupt
    sidecar degrade to an allowed bind, and the spawn gate — which validates
    governance, not ownership — would then run the foreign crew's sessions on
    the private definition once the sidecar recovered. An unverifiable read
    raises ``_UnverifiableLineage``, classified by which read failed and how;
    every binding writer maps it to a 409 whose sentence follows that ``reason``.
    """
    try:
        info = agent_state.get_fork_info(target, strict=True)
    except Exception as exc:
        raise _sidecar_unverifiable(target, exc) from exc
    if info is None and target:
        # Lineage is keyed by the DECLARED name, but a binding can carry
        # the file STEM where the two differ — and that binding resolves
        # the same file. Resolve the target to its declared name before
        # concluding "not a copy" (the bind-side twin of the
        # cleanup's stem coverage). Ambiguity or an unreadable dir raises
        # like an unreadable sidecar — fail closed.
        try:
            _spec, declared, _taken, spec_path = _load_template_specs(
                kiro_agents_dir_path(), target, "foreign_private_copy_check"
            )
        except _AmbiguousTemplateName as exc:
            raise _UnverifiableLineage(
                target, "ambiguous_template_name", paths=exc.paths, cause=exc
            ) from exc
        except Exception as exc:
            raise _UnverifiableLineage(target, cause=exc) from exc
        if spec_path is not None and declared != target:
            try:
                info = agent_state.get_fork_info(declared, strict=True)
            except Exception as exc:
                raise _sidecar_unverifiable(target, exc) from exc
    owner = (info or {}).get("private_to")
    return owner if isinstance(owner, str) and owner and owner != crew else None


def _reserved_binding_names(cfg_data: dict) -> set[str]:
    """Every name the config currently resolves a session against, case-folded:
    each crew's ``kiro_agent`` plus the legacy global fallback
    ``agent.default_agent``. Creating a spec file under any of
    these makes a dangling reference resolve to it, so destination-name checks
    treat them all as reserved."""
    names = {
        str(entry.get("kiro_agent")).lower()
        for entry in cfg_data.get("agents", {}).values()
        if isinstance(entry, dict) and entry.get("kiro_agent")
    }
    agent_section = cfg_data.get("agent")
    if isinstance(agent_section, dict):
        fallback = agent_section.get("default_agent")
        if isinstance(fallback, str) and fallback:
            names.add(fallback.lower())
    return names


def _unlink_copy_unless_referenced(copy_file: Path, agents_dir: Path, *names: str) -> str:
    """Delete a superseded private copy UNLESS a crew binding still resolves it
    — reference check and unlink as ONE critical section under the config
    advisory lock, so a concurrent binding write cannot land between them.

    Cleanup callers rebound their own crew away before asking, so any hit is a
    FOREIGN binding (pre-dating the bind-time guard) and deleting the file
    would break that crew's sessions with "Mode not found". Bindings are read
    from BOTH config layers: the ``config.json`` document the lock hands over
    and the user-owned ``config.local.json`` overlay, which deep-merges over
    it at load time and can therefore hold a crew's effective ``kiro_agent``
    on its own. The overlay has its own sidecar lock (``config set --local``
    writes under it, not under the base's), so it is read inside a nested hold
    of that lock and the unlink runs while both are held — an overlay binding
    cannot land between the check and the unlink. An unreadable overlay fails
    closed like an unreadable base: a binding that cannot be ruled out keeps
    the file. Returns the
    outcome — ``"deleted"``, ``"referenced"``, or ``"error"`` (unlink failure
    or unreadable config, both failing closed with the file kept) — because
    callers treat the retention reasons differently: a REFERENCED file is in
    live use and must stay as it is, while an error-retained one may need
    lineage marking so private content does not surface as a shared template.
    """
    outcome = "error"
    targets = set(names)

    def _referenced(agents: object) -> bool:
        if not isinstance(agents, dict):
            return False
        return any(
            isinstance(entry, dict) and entry.get("kiro_agent") in targets
            for entry in agents.values()
        )

    def _check_then_unlink(data: dict) -> None:
        nonlocal outcome
        if _referenced(data.get("agents")):
            logger.warning(
                "another crew is still bound to private copy %r; leaving it in place",
                copy_file.stem,
            )
            outcome = "referenced"
            return None

        def _check_overlay_then_unlink(local_data: dict) -> None:
            nonlocal outcome
            if _referenced(local_data.get("agents")):
                logger.warning(
                    "another crew is still bound to private copy %r via config.local.json; "
                    "leaving it in place",
                    copy_file.stem,
                )
                outcome = "referenced"
                return None
            try:
                with agents_spec_lock(agents_dir):
                    copy_file.unlink(missing_ok=True)
            except OSError:
                logger.debug("could not remove superseded copy %r", copy_file.stem, exc_info=True)
                return None
            outcome = "deleted"
            return None

        # Nested hold of the overlay's own sidecar lock: the reference check
        # and the unlink run with BOTH layers pinned. An absent overlay reads
        # as {}; a malformed one raises and is caught below as an unreadable
        # config. None from either mutate: nothing is written to either file —
        # the locks are held for isolation against binding writers only.
        update_config_locked(config_local_path(), mutate=_check_overlay_then_unlink)
        return None

    try:
        update_config_locked(mutate=_check_then_unlink)
    except Exception:
        logger.warning(
            "config unreadable during private-copy cleanup; keeping %r",
            copy_file.stem,
            exc_info=True,
        )
        return "error"
    return outcome
