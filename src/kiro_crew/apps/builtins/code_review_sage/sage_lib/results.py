#!/usr/bin/env python3
"""Local result-record store.

One JSON file per reviewed change under ``data/results/``. This is the loop's
output and the Focus Report's input — the durable source of truth. Writes are
atomic (temp + ``os.replace``) and mode ``0600`` (results may quote private
diff snippets). Records follow the findings JSON contract in the skill.
"""

from __future__ import annotations

import json
import os
import re
import threading
from pathlib import Path

from sage_lib import store

# Guarded reader, mirroring pipeline.py's optional import: present in the runtime,
# absent when the app is driven standalone outside it.
try:
    from kiro_crew import hooks
except ImportError:  # pragma: no cover - standalone fallback
    hooks = None  # type: ignore

# Serializes the read-merge-write of the reviewed index so two overlapping
# repo-review runs (finalizing on separate threads) can't clobber each other's
# entries. The write itself is atomic; this guards the read+merge before it.
_REVIEWED_LOCK = threading.Lock()

REQUIRED_TOP = ("schema", "version", "change_id", "platform", "repo_identity", "phase1")
REQUIRED_PHASE1 = ("gate_verdict", "design_risk", "criticality")
VALID_VERDICTS = {"PASS", "CONCERNS", "BLOCK"}
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")
_HANDOFF_BEGIN = "<code-review-sage-result>"
_HANDOFF_END = "</code-review-sage-result>"
# A result record is a small JSON document; anything larger is not one.
_RECORD_MAX_BYTES = 4 * 1024 * 1024


def results_dir(root: Path | None = None, run_id: str | None = None) -> Path:
    """Where result records live.

    With ``run_id``, the run's PRIVATE dir (``data/runs/<id>/results``) — which is
    what lets concurrent runs write without clearing each other's records. Without
    it, the legacy shared ``data/results`` (kept so a standalone driver invocation
    and the existing CLI path still work)."""
    if run_id:
        return store.run_dir(run_id, root) / "results"
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
    # Guarded like the result readers: this index decides which PRs are treated as
    # already reviewed, so a planted link that swaps it lets a worker suppress or
    # force re-reviews. store.data_dir is the containing root here.
    return _read_json_nolink(reviewed_path(root), store.data_dir(root)) or {}


def write_reviewed(index: dict, root: Path | None = None) -> Path:
    """Atomically write the reviewed index (mode 0600), mirroring write_result."""
    store.ensure_layout(root)
    path = reviewed_path(root)
    data = json.dumps(index, indent=2).encode("utf-8")
    store.atomic_write_locked(path, data)
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


def result_path(change_id: str, root: Path | None = None, run_id: str | None = None) -> Path:
    return results_dir(root, run_id) / f"{safe_change_id(change_id)}.json"


def validate_result(record: dict) -> list[str]:
    """Return a list of contract violations (empty == valid)."""
    errs: list[str] = []
    if not isinstance(record, dict):
        return ["record must be an object"]
    if "result_capability" in record:
        errs.append("result_capability is not accepted")
    for k in REQUIRED_TOP:
        if k not in record:
            errs.append(f"missing top-level key: {k}")
    p1 = record.get("phase1")
    if not isinstance(p1, dict):
        errs.append("phase1 must be an object")
    else:
        # SHAPE FIRST, before any membership or string test below. `phase1` is
        # uniformly string-valued per the result schema, and its values are
        # consumed as dict KEYS (`_RISK_W.get(design_risk)`,
        # `_BLAST_W.get(rating)`) or with string operations
        # (`band_override_reason.strip()`, `html.escape(gate_verdict)`), so a
        # non-scalar killed report generation after the review was paid for.
        #
        # The ordering is not cosmetic: `gate_verdict not in VALID_VERDICTS`
        # tests membership of a SET, so an unhashable value raised
        # `TypeError: unhashable type: 'list'` INSIDE this validator — the
        # function whose entire job is to refuse malformed records crashed on
        # one instead of reporting it. Screening the shape first means every
        # check after this point is operating on scalars.
        #
        # Checked by shape rather than by naming the fields: naming them is what
        # let this same class recur three times (`counts` values, finding field
        # values, and now these), so a new phase1 field is covered by default.
        #
        # STRING, not "any scalar". Every phase1 field in the worker's contract is
        # text (gate_verdict, design_risk, criticality, design_headline, problem,
        # why_it_matters, solution_assessment), and the readers consume them as
        # text: `.strip()` in classify, `html.escape()` in the renderer. Accepting
        # a number here let both crash on a record this function had approved --
        # and it also skipped the verdict-vocabulary check below, because that
        # check used to be guarded by an isinstance() test that a number failed.
        # Refusing the record is the fail-closed direction: a numeric phase1 value
        # is malformed per the contract, and a rejected record is re-reviewed
        # rather than rendered half-broken.
        for k, v in p1.items():
            if not isinstance(v, (str, type(None))):
                errs.append(f"phase1.{k} must be a string")
        for k in REQUIRED_PHASE1:
            if k not in p1:
                errs.append(f"missing phase1.{k}")
        # The isinstance() guard stays: shape errors above are APPENDED, not raised,
        # so execution reaches this line with the bad value still in hand, and
        # `not in VALID_VERDICTS` tests a SET — an unhashable list/dict would raise
        # TypeError inside the validator whose whole job is to report malformed
        # records. It is no longer a hole the way it was under the "any scalar" rule:
        # every non-string value now fails the string check above, so the guard only
        # suppresses a duplicate complaint, never the only one.
        if (
            isinstance(p1.get("gate_verdict"), (str, type(None)))
            and p1.get("gate_verdict") not in VALID_VERDICTS
        ):
            errs.append(f"phase1.gate_verdict must be one of {sorted(VALID_VERDICTS)}")
    findings = record.get("findings", [])
    if findings and not isinstance(findings, list):
        errs.append("findings must be a list")
    elif isinstance(findings, list):
        # Every entry is dereferenced as an object downstream (`_redact_finding`,
        # then `f.get("severity"/"file"/"line"/...)` when rendering), so a
        # non-object entry raises AttributeError mid-report rather than being
        # rejected here.
        for i, f in enumerate(findings):
            if not isinstance(f, dict):
                errs.append(f"findings[{i}] must be an object")
                continue
            # Field VALUES must be scalars, not just the finding itself an object.
            # The report redactor scrubs strings; a dict or list under any key
            # carried an injected secret past it and into report.json and the
            # dashboard. The contract has only scalar fields (prose, file, line),
            # so a non-scalar is malformed and is refused here rather than being
            # sanitized downstream — the same widening `counts` needed, where the
            # object check passed and the values were unguarded.
            #
            # `line` is the ONLY numeric field. Every other key is prose the report
            # renderer feeds to `html.escape()`, which raises on a non-string: a
            # numeric `snippet` passed a scalars-only check, was adopted, and then
            # crashed report generation, so the run finished COMPLETED with no report
            # at all. Refusing it at the boundary keeps the failure legible and the
            # record retryable.
            for k, v in f.items():
                if k == "line":
                    if not isinstance(v, (int, float, type(None))) or isinstance(v, bool):
                        errs.append(f"findings[{i}].line must be a number")
                elif not isinstance(v, (str, type(None))):
                    errs.append(f"findings[{i}].{k} must be a string")
            # `line` is checked in the loop above rather than here, so a missing key
            # and a wrong-typed one produce one message each, not two for the latter.
            # The renderer and every consumer treat it as a line number, and the
            # report redactor no longer exempts it -- a credential written into it as
            # a string used to ride that exemption to the dashboard.
    # `counts` and `blast_radius` are read with `.get()` in pipeline.py,
    # report.py and review_driver.py. A worker that wrote either as a list or a
    # scalar passed validation, was adopted, and then aborted the run with an
    # AttributeError at render time -- after the review had already been paid
    # for, and with no report to show. Type-check them at the boundary instead.
    for key in ("counts", "blast_radius"):
        value = record.get(key)
        if value is not None and not isinstance(value, dict):
            errs.append(f"{key} must be an object")
    # The object check above is not enough for `counts`: its VALUES are used in
    # arithmetic (`counts.get("red", 0) * 15 + counts.get("yellow", 0) * 5` in
    # report.py, and again in review_driver.py), so a worker writing
    # `{"red": "1"}` passed validation, was adopted, and then raised TypeError at
    # scoring time — the same shape of failure as a non-object `counts`, one level
    # deeper. Every value is checked, not just red/yellow, so a new band cannot
    # reintroduce the gap. `bool` is excluded even though it is an int subclass:
    # `True` would multiply fine but a boolean finding count is not a count.
    counts = record.get("counts")
    if isinstance(counts, dict):
        for band, n in counts.items():
            if isinstance(n, bool) or not isinstance(n, (int, float)):
                errs.append(f"counts.{band} must be a number")
    # `blast_radius` cannot take the phase1 blanket rule: `signals` is
    # legitimately a nested object in the schema (`sensitive_hits`,
    # `loc_added`, ...). Only `rating` is consumed as a string, via
    # `_BLAST_W.get(rating)` — a dict lookup, so an unhashable value there
    # raises exactly like the phase1 case.
    br = record.get("blast_radius")
    if isinstance(br, dict):
        rating = br.get("rating")
        if rating is not None and not isinstance(rating, str):
            errs.append("blast_radius.rating must be a string")
    return errs


def write_result(record: dict, root: Path | None = None, run_id: str | None = None) -> Path:
    """Validate then atomically write the record (mode 0600). Raises ValueError."""
    errs = validate_result(record)
    if errs:
        raise ValueError("invalid result record: " + "; ".join(errs))
    if run_id:
        store.ensure_run_layout(run_id, root)
        path = result_path(record["change_id"], root, run_id)
    else:
        store.ensure_layout(root)
        path = result_path(record["change_id"], root, run_id)
    data = json.dumps(record, indent=2).encode("utf-8")
    store.atomic_write_locked(path, data)
    return path


def _read_json_nolink(path: Path, within: Path) -> dict | None:
    """No-link JSON read, shared with every other reader in this app.

    Kept as a module-private alias so this module's call sites read the same as
    before; the implementation and its rationale live in `store`, which owns the
    data directory these paths are confined to.
    """
    return store.read_json_nolink(path, within)


def read_result(change_id: str, root: Path | None = None, run_id: str | None = None) -> dict | None:
    path = result_path(change_id, root, run_id)
    return _valid_result(_read_json_nolink(path, results_dir(root, run_id)))


def _valid_result(rec: dict | None) -> dict | None:
    """Return the record only if it satisfies the result contract.

    The driver persists these files, so being a JSON object is not enough: a record
    can be a dict and still be unusable (``phase1`` absent, or not an object).
    ``classify()`` indexes into exactly those fields, so letting one through raises
    at render time and leaves a COMPLETED run with no report -- the silent failure
    this module exists to prevent. Callers already treat ``None`` as "no record",
    which degrades to a run reported as failed rather than one that claims success
    over an empty report.
    """
    if rec is None or validate_result(rec):
        return None
    return rec


def result_from_handoff(output: object, change_id: str) -> dict | None:
    """Validate one reviewer response received through the dispatcher.

    Worker sessions share a uid, so a mode-restricted staging directory cannot
    authenticate one worker to another. The dispatcher response is the only
    per-session channel the driver can bind to the turn that produced it.
    """
    if not isinstance(output, str):
        return None
    start = output.rfind(_HANDOFF_BEGIN)
    end = output.find(_HANDOFF_END, start + len(_HANDOFF_BEGIN))
    if start < 0 or end < 0:
        return None
    raw = output[start + len(_HANDOFF_BEGIN) : end].strip()
    try:
        record = json.loads(raw)
    except (TypeError, ValueError, UnicodeDecodeError):
        return None
    if not isinstance(record, dict) or record.get("change_id") != change_id:
        return None
    return _valid_result(record)


def list_results(root: Path | None = None, run_id: str | None = None) -> list[dict]:
    rd = results_dir(root, run_id)
    if not rd.exists():
        return []
    out = []
    for p in sorted(rd.glob("*.json")):
        # Same guards as read_result: one planted link in this dir would otherwise
        # inject a foreign record into every consumer that lists the run, and one
        # contract-violating record would abort the whole report.
        rec = _valid_result(_read_json_nolink(p, rd))
        if rec is not None:
            out.append(rec)
    return out


def clear_results(root: Path | None = None, run_id: str | None = None) -> int:
    """Delete all result records. Called after a report has folded them in and
    been durably archived — the records are intermediates (their content lives
    in the report summary and as draft CR comments). Returns the count removed."""
    rd = results_dir(root, run_id)
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
