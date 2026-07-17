"""Durable on-disk store for dynamic-workflow runs (FIX-21).

The ``RunRegistry`` is in-memory and loop-affine; without this, every run (its
authored script, event stream, result, and resume cache) is lost on a gateway
restart. This store persists each run as ONE JSON file under ``workflows.dir`` so
that across restarts the ``/workflows`` list, ``workflow_result``, and
rerun / restart-subtree all keep working — and a successful run's script stays
reusable.

Layout: ``<workflows.dir>/runs/<run_id>.json`` — one self-contained file per run
(``RunHandle.to_store_json``). JSON only (BSC12 — never pickle/marshal). Writes are
atomic (temp file + ``os.replace``) so a crash mid-write can't corrupt a run file.

The store is a thin, side-effect-only persistence layer: the registry owns the
truth in memory and calls ``save``/``delete`` on changes; ``load_all`` rehydrates
on startup. All methods are best-effort — a storage failure must never break a run
(the registry stays authoritative in memory).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path
from typing import Optional

from kiro_crew.config.paths import config_dir
from kiro_crew.security import redact_credentials, redact_exfiltration_urls

# Optional dependency (gate F1): the workflows engine must stay importable without
# the full app/config stack. Imported at module top via try/except so the
# top-level-imports rule is satisfied; ``None`` when config isn't available, in
# which case default_workflows_dir() falls back to config_dir()/workflows.
try:
    from kiro_crew.config.loader import KiroCrewConfig
except ImportError:  # pragma: no cover - config layer optional for standalone engine
    KiroCrewConfig = None  # type: ignore[assignment,misc]

logger = logging.getLogger(__name__)

# Default subdirectory under the resolved workflows dir.
_RUNS_SUBDIR = "runs"


def default_workflows_dir() -> Path:
    """Resolve ``workflows.dir`` (config key) with a per-user default.

    Honors the ``workflows.dir`` config entry when set; otherwise defaults to
    ``<config_dir>/workflows`` (e.g. ``~/.kirocrew/workflows``, or the
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
        except Exception:  # noqa: BLE001
            logger.debug("workflow store: cannot create %s", self._runs_dir, exc_info=True)
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
        return self._runs_dir / f"{safe}.json"

    def save(self, run_id: str, store_json: dict) -> None:
        """Persist one run's full JSON form (atomic write). Best-effort."""
        if not run_id or not self._ensure_dir():
            return
        try:
            payload = json.dumps(_redact(store_json), default=str)
        except Exception:  # noqa: BLE001
            logger.debug("workflow store: serialize failed for %s", run_id, exc_info=True)
            return
        path = self._path_for(run_id)
        tmp = path.with_suffix(".json.tmp")
        try:
            tmp.write_text(payload, encoding="utf-8")
            os.replace(tmp, path)  # atomic on POSIX
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass
        except Exception:  # noqa: BLE001 - persistence must never break a run
            logger.debug("workflow store: write failed for %s", run_id, exc_info=True)
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass

    def delete(self, run_id: str) -> None:
        """Remove a run's file (e.g. when evicted from the in-memory registry)."""
        try:
            self._path_for(run_id).unlink(missing_ok=True)
        except Exception:  # noqa: BLE001
            logger.debug("workflow store: delete failed for %s", run_id, exc_info=True)

    def load_all(self) -> list[dict]:
        """Return every persisted run's JSON, oldest file first (by mtime).

        Bad/corrupt files are skipped (never raise). The registry decides how to
        rehydrate (e.g. demote a 'running' run to failed-interrupted).
        """
        if not self._runs_dir.is_dir():
            return []
        out: list[tuple[float, dict]] = []
        for f in self._runs_dir.glob("*.json"):
            try:
                obj = json.loads(f.read_text(encoding="utf-8"))
                if isinstance(obj, dict) and obj.get("run_id"):
                    out.append((f.stat().st_mtime, obj))
            except Exception:  # noqa: BLE001 - skip corrupt files
                logger.debug("workflow store: skip unreadable %s", f, exc_info=True)
        out.sort(key=lambda t: t[0])
        return [obj for _, obj in out]
