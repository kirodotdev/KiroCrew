#!/usr/bin/env python3
"""Local result-record store (design §3 component 4, §6).

One JSON file per reviewed change under ``data/results/``. This is the loop's
output and the Focus Report's input — the durable source of truth. Writes are
atomic (temp + ``os.replace``) and mode ``0600`` (results may quote private
diff snippets). Records follow the findings JSON contract in the skill.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import threading
from pathlib import Path

from sage_lib import store

# Serializes the read-merge-write of the reviewed index so two overlapping
# repo-review runs (finalizing on separate threads) can't clobber each other's
# entries. The write itself is atomic; this guards the read+merge before it.
_REVIEWED_LOCK = threading.Lock()

REQUIRED_TOP = ("schema", "version", "change_id", "platform", "repo_identity", "phase1")
REQUIRED_PHASE1 = ("gate_verdict", "design_risk", "criticality")
VALID_VERDICTS = {"PASS", "CONCERNS", "BLOCK"}
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


def results_dir(root: Path | None = None) -> Path:
    return store.data_dir(root) / "results"


def reviewed_path(root: Path | None = None) -> Path:
    """Durable cross-run 'already reviewed' index (repo-review dedup).

    A single flat file ``data/reviewed.json`` keyed by change id ->
    ``{head_sha, reviewed_at, run_id}``. UNLIKE the per-change result records
    (transient scratch the driver clears after each run), this index is durable
    and is the source of truth for skipping PRs whose head SHA has not changed
    since their last review."""
    return store.data_dir(root) / "reviewed.json"


def read_reviewed(root: Path | None = None) -> dict:
    """Load the reviewed index; ``{}`` if missing or unreadable."""
    try:
        return json.loads(reviewed_path(root).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def write_reviewed(index: dict, root: Path | None = None) -> Path:
    """Atomically write the reviewed index (mode 0600), mirroring write_result."""
    store.ensure_layout(root)
    path = reviewed_path(root)
    data = json.dumps(index, indent=2).encode("utf-8")
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        try:
            os.write(fd, data)
        finally:
            os.close(fd)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return path


def mark_reviewed(entries: dict, root: Path | None = None) -> Path:
    """Upsert ``entries`` ({change_id: {head_sha, reviewed_at, run_id}}) into the
    durable index (read-merge-atomic-write, serialized by ``_REVIEWED_LOCK`` so
    overlapping runs merge instead of clobber). Returns the index path."""
    with _REVIEWED_LOCK:
        idx = read_reviewed(root)
        idx.update(entries)
        return write_reviewed(idx, root)


def safe_change_id(change_id: str) -> str:
    """Sanitize a change id into a filesystem-safe stem (prevents traversal)."""
    stem = _UNSAFE.sub("_", str(change_id)).strip("_")
    return stem or "unknown"


def result_path(change_id: str, root: Path | None = None) -> Path:
    return results_dir(root) / f"{safe_change_id(change_id)}.json"


def validate_result(record: dict) -> list[str]:
    """Return a list of contract violations (empty == valid)."""
    errs: list[str] = []
    if not isinstance(record, dict):
        return ["record must be an object"]
    for k in REQUIRED_TOP:
        if k not in record:
            errs.append(f"missing top-level key: {k}")
    p1 = record.get("phase1")
    if not isinstance(p1, dict):
        errs.append("phase1 must be an object")
    else:
        for k in REQUIRED_PHASE1:
            if k not in p1:
                errs.append(f"missing phase1.{k}")
        if p1.get("gate_verdict") not in VALID_VERDICTS:
            errs.append(f"phase1.gate_verdict must be one of {sorted(VALID_VERDICTS)}")
    findings = record.get("findings", [])
    if findings and not isinstance(findings, list):
        errs.append("findings must be a list")
    return errs


def write_result(record: dict, root: Path | None = None) -> Path:
    """Validate then atomically write the record (mode 0600). Raises ValueError."""
    errs = validate_result(record)
    if errs:
        raise ValueError("invalid result record: " + "; ".join(errs))
    store.ensure_layout(root)
    path = result_path(record["change_id"], root)
    data = json.dumps(record, indent=2).encode("utf-8")
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        try:
            os.write(fd, data)
        finally:
            os.close(fd)  # always close the fd, even if os.write raised
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return path


def read_result(change_id: str, root: Path | None = None) -> dict | None:
    path = result_path(change_id, root)
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def list_results(root: Path | None = None) -> list[dict]:
    rd = results_dir(root)
    if not rd.exists():
        return []
    out = []
    for p in sorted(rd.glob("*.json")):
        try:
            out.append(json.loads(p.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError):
            continue
    return out


def clear_results(root: Path | None = None) -> int:
    """Delete all result records. Called after a report has folded them in and
    been durably archived — the records are intermediates (their content lives
    in the report summary and as draft CR comments). Returns the count removed."""
    rd = results_dir(root)
    if not rd.exists():
        return 0
    removed = 0
    for p in rd.glob("*.json"):
        try:
            p.unlink()
            removed += 1
        except OSError:
            pass
    return removed
