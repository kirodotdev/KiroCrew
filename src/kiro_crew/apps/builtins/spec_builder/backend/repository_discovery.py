"""Spec Builder's specs on disk: discovery, phase, documents and new spec directories.

Discovery adopts spec-shaped directories under the project roots the index knows; the
list and detail views read each spec's phase and its redacted documents from here; and
create and duplicate resolve, validate and create a new spec directory. From the rest of
the backend this module imports only :mod:`repository` and :mod:`parsers`.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from .parsers import _PHASE_FILES, _parse_tasks, _redact, _sha256_text, _usable_name
from .repository import (
    _INDEX_LOCK,
    _contained,
    _load_deleted,
    _load_index,
    _load_settings,
    _new_slot_key,
    _read_spec_text,
    _resolve_spec_dir,
    _safe_dir,
    _safe_dir_optional,
    _save_index,
    _spec_file,
    logger,
)


def _derive_phase(spec_dir: Path) -> str:
    for phase, fname in _PHASE_FILES:
        if _spec_file(spec_dir, fname) is not None and (spec_dir / fname).is_file():
            return phase
    return "new"


def _read_spec_files(spec_dir: Path) -> tuple[dict, dict, list[dict]]:
    """Read the documents once, returning ``(files, docs, tasks)``.

    ``files`` is what the browser renders: the text with credentials REDACTED.
    ``docs`` carries the ON-DISK hash that binds approvals to the version that
    was reviewed. ``tasks`` carries redacted labels but raw-text identity hashes.
    Documents remain read-only here because the agent and IDE write the same files
    without participating in a dashboard lock; no portable compare-and-swap can
    prevent a direct write between a hash check and replace.
    """
    files: dict[str, str | None] = {}
    docs: dict[str, dict] = {}
    tasks: list[dict] = []
    for _phase, fname in _PHASE_FILES:
        text = _read_spec_text(spec_dir, fname)
        if text is None:
            files[fname] = None
            continue
        files[fname] = _redact(text)
        docs[fname] = {"hash": _sha256_text(text)}
        if fname == "tasks.md":
            tasks = _parse_tasks(text)
    return files, docs, tasks


def _discover_folder_specs(index: dict) -> bool:
    """Scan known project folders' ``.kiro/specs/`` for specs created outside
    the app (Kiro CLI/IDE, other tools) and auto-register them in the index.

    Candidate roots are the working dirs the app already knows. A directory
    counts as a spec when it contains any of the three Kiro markdown files.
    Returns True when new entries were added (caller persists).
    """
    roots: set[str] = {str(meta.get("working_dir", "")) for meta in index.values()}
    known_dirs: set[str] = {str(meta.get("spec_dir", "")) for meta in index.values()}
    # A directory the user deleted is not a discovery candidate. Without this, a
    # delete that (by design) leaves the .md files in place was undone by the very
    # next list scan whenever a sibling spec kept the project root indexed.
    known_dirs |= set(_load_deleted())
    added = False
    for root in filter(None, roots):
        # The indexed working_dir is app state on disk, so it is untrusted (same
        # reasoning as _ensure_worker_slot): a tampered entry pointing at a
        # credential tree would otherwise be statted and ENUMERATED here, outside
        # the sensitive-path gate, and any spec-shaped directory inside it would be
        # adopted into the index. Validate the derived scan root itself, so a
        # symlinked `.kiro/specs` cannot redirect the walk either.
        safe_root = _safe_dir(root)
        if safe_root is None:
            logger.warning("skipping discovery for unusable indexed root %s", _redact(root))
            continue
        specs_base = _safe_dir(str(safe_root / ".kiro" / "specs"))
        if specs_base is None:
            continue
        try:
            children = sorted(specs_base.iterdir())
        except OSError:
            continue
        for child in children:
            if not child.is_dir() or str(child) in known_dirs:
                continue
            if not any((child / f).is_file() for f in ("requirements.md", "design.md", "tasks.md")):
                continue
            name = child.name
            # _usable_name for the same reason as create: discovery WRITES
            # index[name] below, so admitting on the grammar alone would re-add an
            # entry that the next load drops, rediscovering it on every call.
            if name in index or not _usable_name(name):
                continue
            try:
                created = child.stat().st_mtime
            except OSError:
                created = time.time()
            index[name] = {
                "working_dir": root,
                "spec_dir": str(child),
                "spec_type": "feature",
                "status": "planning",
                "slot_key": _new_slot_key(name),
                "worktree_branch": "",
                "repo_root": "",
                "discovered": True,
                "created_at": created,
                "updated_at": created,
            }
            known_dirs.add(str(child))
            added = True
    return added


def _prepare_spec_dir(
    working_dir: str,
    safe_wd: Path,
    name: str,
    import_existing: bool,
    *,
    create: bool = True,
    expected_dir: Path | None = None,
) -> tuple[Path, str]:
    """Resolve + validate + create the spec directory. BLOCKING -- one hop.

    Returns ``(spec_dir, refusal)``; ``refusal`` is ``""`` on success, else
    ``"escape"``, ``"moved"``, ``"existing:<files>"`` or ``"mkdir:<reason>"``.
    """
    spec_dir = _resolve_spec_dir(working_dir, name)
    # Duplication reserves this exact path before any files are copied. Refuse
    # if a concurrent settings change resolves the destination elsewhere.
    if expected_dir is not None and os.path.normcase(str(spec_dir)) != os.path.normcase(
        str(expected_dir)
    ):
        return spec_dir, "moved"
    # The spec dir must land under its declared root -- either the settings
    # base_path or the validated working dir (which is the WORKTREE when one was
    # just created). _NAME_RE already forbids '.' and '/', so this can only fail
    # if one of those invariants regresses; assert it here rather than trusting a
    # regex defined elsewhere.
    settings_base = _safe_dir_optional(_load_settings().get("base_path", ""))
    expected_root = settings_base if settings_base else safe_wd
    if not _contained(spec_dir, expected_root):
        return spec_dir, "escape"
    # Containment alone is not enough: it only says "under the declared root".
    # If that root is (or grows) a symlink into a credential tree, BOTH paths
    # resolve through it, so the containment test passes while the spec files
    # would be created inside the credential directory. Re-validate the RESOLVED
    # destination through the same chokepoint every caller-supplied directory
    # goes through -- must_exist=False, because the spec dir is what we are about
    # to create, and that variant also tests the nearest existing ancestor.
    if _safe_dir_optional(str(spec_dir)) is None:
        return spec_dir, "escape"
    # Refuse to adopt-by-overwrite: a spec dir that already holds Kiro markdown
    # was created by the IDE/CLI or another tool, and handing it to an agent
    # would let it rewrite files the index never knew about. Opting in is
    # explicit.
    if not import_existing:
        existing = [f for _p, f in _PHASE_FILES if (spec_dir / f).is_file()]
        if existing:
            return spec_dir, "existing:" + ", ".join(sorted(existing))
    if create:
        try:
            spec_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return spec_dir, f"mkdir:{exc}"
    return spec_dir, ""


def _load_index_with_discovery() -> tuple[dict, dict[str, str]]:
    """Load the index, fold in specs found on disk, and derive every phase --
    all in ONE thread hop, under the index lock.

    BLOCKING -- call via ``asyncio.to_thread``. The list endpoint is polled every
    15s, while discovery walks every known project root and phase derivation stats
    up to three files per spec. Returns ``(index, {name: phase})``.
    """
    with _INDEX_LOCK:
        index = _load_index()
        if _discover_folder_specs(index):
            _save_index(index)
    phases = {name: _derive_phase(Path(m.get("spec_dir", ""))) for name, m in index.items()}
    return index, phases
