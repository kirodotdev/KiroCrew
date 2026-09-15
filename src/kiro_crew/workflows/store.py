"""Durable on-disk store for dynamic-workflow runs.

The ``RunRegistry`` is in-memory and loop-affine; without this, every run (its
authored script, event stream, result, and resume cache) is lost on a gateway
restart. This store persists each run as ONE JSON file under ``workflows.dir`` so
that across restarts the ``/workflows`` list, ``workflow_result``, and
rerun / restart-subtree all keep working — and a successful run's script stays
reusable.

Layout: ``<workflows.dir>/runs/<run_id>.json`` — one self-contained file per run
(``RunHandle.to_store_json``). JSON only — never pickle/marshal. Writes are
atomic (temp file + ``os.replace``) so a crash mid-write can't corrupt a run file.

The store is a thin, side-effect-only persistence layer: the registry owns the
truth in memory and calls ``save``/``delete`` on changes; ``load_all`` rehydrates
on startup. Live checkpoint failures leave execution authoritative in memory and
are surfaced by the registry. Inventory or protected-binding recovery failures
propagate: an incomplete authorized inventory must not look like an empty one.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import stat
from pathlib import Path
from typing import Optional

from kiro_crew import platform_compat
from kiro_crew.config.paths import config_dir
from kiro_crew.pinned_fs import fd_real_path
from kiro_crew.security import redact_credentials, redact_exfiltration_urls
from kiro_crew.workflow_memory import private_payload_path, read_binding

# Optional dependency (gate F1): the workflows engine must stay importable without
# the full app/config stack. Imported at module top via try/except so the
# top-level-imports rule is satisfied; ``None`` when config isn't available, in
# which case default_workflows_dir() falls back to config_dir()/workflows.
try:
    from kiro_crew.config.loader import KiroCrewConfig
except ImportError:  # pragma: no cover - config layer optional for standalone engine
    KiroCrewConfig = None  # type: ignore[assignment,misc]

logger = logging.getLogger(__name__)

WORKFLOW_LIBRARY_DIR_NAME = "workflow_library"

# Default subdirectory under the resolved workflows dir.
_RUNS_SUBDIR = "runs"


class WorkflowInventoryError(OSError):
    """Recovery cannot establish the complete authorized run inventory."""


def default_workflows_dir() -> Path:
    """Resolve ``workflows.dir`` (config key) with a per-user default.

    Honors the ``workflows.dir`` config entry when set; otherwise defaults to
    ``<config_dir>/workflows`` (e.g. ``~/.kiro/crew/workflows``, or the
    ``KIROCREW_HOME`` override used by the dev instance).
    """
    # KiroCrewConfig is an optional dependency resolved at module top (gate F1);
    # when present, honor workflows.dir, else fall back to config_dir()/workflows.
    if KiroCrewConfig is not None:
        try:
            cfg = KiroCrewConfig.load()
            configured = getattr(getattr(cfg, "workflows", None), "dir", "") or ""
            if configured:
                return Path(configured).expanduser()
        except Exception:  # noqa: BLE001 - config optional / may not declare the key yet
            logger.debug("workflows.dir config lookup failed; using default", exc_info=True)
    return config_dir() / "workflows"


def default_workflow_library_dir() -> Path:
    """Return the agent-protected global definition-library directory."""
    return config_dir() / WORKFLOW_LIBRARY_DIR_NAME


def _redact(obj):
    """Recursively redact credentials / exfiltration URLs in a JSON-able value
    before it touches disk (defense-in-depth, mirrors the broadcast/persist rule)."""
    if isinstance(obj, str):
        s, _ = redact_exfiltration_urls(obj)
        s, _ = redact_credentials(s)
        return s
    if isinstance(obj, dict):
        return {k: _redact(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_redact(v) for v in obj]
    return obj


class WorkflowRunStore:
    """JSON-per-run durable store for workflow runs (one file per ``run_id``)."""

    def __init__(self, base_dir: Optional[Path] = None) -> None:
        self._base = Path(base_dir) if base_dir else default_workflows_dir()
        self._runs_dir = self._base / _RUNS_SUBDIR

    @property
    def runs_dir(self) -> Path:
        return self._runs_dir

    def _ensure_dir(self) -> bool:
        try:
            self._runs_dir.mkdir(parents=True, exist_ok=True)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.debug("workflow store: directory unavailable (%s)", type(exc).__name__)
            return False

    def _path_for(self, run_id: str) -> Path:
        # run_ids are gateway-generated (wf_NNNNNN) or wf_<hex>; sanitize defensively
        # so a malformed id can never escape the runs dir via path traversal.
        safe = "".join(c for c in run_id if c.isalnum() or c in ("_", "-"))
        # Sanitizing is lossy: distinct ids that differ only in stripped chars
        # (e.g. "wf/1" vs "wf1") would otherwise collapse to the same file and
        # clobber each other. When sanitization changed the id, disambiguate with
        # a short hash of the original so the mapping stays injective. Well-formed
        # ids are unchanged, so their on-disk paths are preserved.
        if safe != run_id:
            digest = hashlib.sha256(run_id.encode("utf-8")).hexdigest()[:12]
            safe = f"{safe}-{digest}" if safe else digest
        binding = read_binding(run_id)
        if binding is not None and binding["memory_store"]:
            return private_payload_path(run_id)
        return self._runs_dir / f"{safe}.json"

    def save(self, run_id: str, store_json: dict) -> None:
        """Persist one run atomically; report failures to the registry's health view."""
        if not run_id:
            return
        read_binding(run_id, required=bool(store_json.get("execution_binding_version")))
        path = self._path_for(run_id)
        if path.parent == self._runs_dir:
            if not self._ensure_dir():
                raise OSError("Workflow run directory unavailable")
        else:
            platform_compat.make_owner_only_dir(path.parent)
            platform_compat.restrict_dir_to_owner(path.parent)
        try:
            redacted = _redact(store_json)
            redacted["source_is_original"] = bool(store_json.get("source_is_original")) and (
                redacted.get("source") == store_json.get("source")
            )
            payload = json.dumps(redacted, default=str)
        except Exception as exc:  # noqa: BLE001
            logger.debug("workflow store: serialization failed (%s)", type(exc).__name__)
            raise OSError("Workflow snapshot serialization failed") from None
        tmp = path.with_suffix(".json.tmp")
        try:
            tmp.write_text(payload, encoding="utf-8")
            os.replace(tmp, path)  # atomic on POSIX
            try:
                # POSIX tightening only, deliberately still NOT
                # ``platform_compat.restrict_to_owner``: on POSIX that helper IS
                # this exact call, so a swap would add only the Windows
                # owner-only DACL — and ``save`` runs on the event loop via the
                # registry's persist hooks, where a DACL write to a UNC or
                # mapped-drive path costs an unbounded SMB round-trip. The
                # payload is already passed through ``_redact`` above, so
                # what a wider Windows DACL could expose is the redacted
                # run record, not credentials.
                os.chmod(path, 0o600)  # lockdown-ok: unbounded SMB round-trip on the loop
            except OSError:
                pass
        except Exception as exc:  # noqa: BLE001 - registry reports the durability failure
            logger.debug("workflow store: write failed (%s)", type(exc).__name__)
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            raise OSError("Workflow checkpoint write failed") from None

    def delete(self, run_id: str) -> None:
        """Remove a run's file (e.g. when evicted from the in-memory registry)."""
        try:
            self._path_for(run_id).unlink(missing_ok=True)
        except Exception:  # noqa: BLE001
            logger.debug("workflow store: delete failed for %s", run_id, exc_info=True)

    def load_all(self) -> list[dict]:
        """Return every persisted run's JSON, oldest file first (by mtime).

        Bad/corrupt files are skipped individually. Inventory access failures
        propagate so startup cannot mistake an unreadable store for an empty
        one. The registry decides how to rehydrate interrupted runs.
        """
        roots = [self._runs_dir]
        try:
            roots.append(private_payload_path("discovery").parent)
        except Exception as exc:
            logger.debug(
                "workflow store: private discovery root unavailable (%s)", type(exc).__name__
            )
            raise OSError(
                "Workflow run inventory unavailable; repair storage and restart"
            ) from None
        out: list[tuple[float, dict]] = []
        for root in roots:
            try:
                try:
                    root_info = root.stat()
                except FileNotFoundError:
                    # Windows may report a missing path beneath a plain file.
                    # Only a missing tree under an existing directory is empty.
                    for ancestor in root.parents:
                        try:
                            ancestor_info = ancestor.stat()
                        except FileNotFoundError:
                            continue
                        if not stat.S_ISDIR(ancestor_info.st_mode):
                            raise NotADirectoryError(
                                "Workflow inventory ancestor is not a directory"
                            )
                        break
                    else:
                        raise OSError("Workflow inventory has no accessible ancestor")
                    continue  # A first boot has no persisted directory yet.
                if not stat.S_ISDIR(root_info.st_mode):
                    raise NotADirectoryError("Workflow run inventory is not a directory")
                resolved_root = root.resolve()
                files = list(root.glob("*.json"))
            except Exception as exc:
                logger.debug("workflow store: discovery root unavailable (%s)", type(exc).__name__)
                # Fail startup without exposing a private path or exception payload.
                raise OSError(
                    "Workflow run inventory unavailable; repair storage and restart"
                ) from None
            for f in files:
                try:
                    # Resolve the trusted root once, not each candidate: legitimate
                    # data-home aliases are allowed, redirects after this point are not.
                    # Validate and read the same inode, including its mtime. Workflow
                    # payloads exceed the small identity-record reader's size cap.
                    fd = platform_compat.open_file_no_reparse(f, nonblocking=True)
                    try:
                        info = os.fstat(fd)
                        opened = fd_real_path(fd)
                        if (
                            not stat.S_ISREG(info.st_mode)
                            or info.st_nlink != 1
                            or (
                                platform_compat.IS_POSIX
                                and info.st_uid != platform_compat.local_user_id()
                            )
                            or opened is None
                            or Path(opened) != resolved_root / f.name
                        ):
                            continue
                        with os.fdopen(fd, "r", encoding="utf-8", closefd=False) as stream:
                            obj = json.load(stream)
                    finally:
                        os.close(fd)
                    if not isinstance(obj, dict) or not isinstance(obj.get("run_id"), str):
                        continue
                    run_id = obj["run_id"]
                    try:
                        binding = read_binding(
                            run_id,
                            required=root != self._runs_dir
                            or bool(obj.get("execution_binding_version")),
                        )
                    except Exception as exc:
                        logger.debug(
                            "workflow store: protected binding unavailable (%s)",
                            type(exc).__name__,
                        )
                        # Do not hide a bound run behind an empty list,
                        # or reconstruct authority from its editable payload.
                        raise WorkflowInventoryError(
                            "Workflow run inventory unavailable; repair storage and restart"
                        ) from None
                    private = binding is not None and bool(binding["memory_store"])
                    if private != (root != self._runs_dir):
                        continue
                    if private and f != private_payload_path(run_id):
                        continue
                    out.append((info.st_mtime, obj))
                except WorkflowInventoryError:
                    raise
                except Exception as exc:  # malformed data grants no restored execution authority
                    logger.debug(
                        "workflow store: skip unreadable record %s (%s)",
                        hashlib.sha256(f.name.encode("utf-8", errors="surrogatepass")).hexdigest()[
                            :12
                        ],
                        type(exc).__name__,
                    )
        out.sort(key=lambda t: t[0])
        return [obj for _, obj in out]
