"""Persistence layer for the Writing Review app.

Owns the on-disk layout under ``<crew_home>/apps/writing-review/data/``:

    data/
      reviews/<review_id>.json      # one file per scan result
      settings.json                 # user's scanner toggles and defaults

Every function accepts an optional ``data_dir`` override so tests can
target a temporary directory without touching the operator's real home.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Any

from kiro_crew.apps.builtins.writing_review import (
    ALWAYS_ON_SCANNERS,
    CONDITIONAL_SCANNERS,
    FailedScanner,
    Finding,
    RelatedLocation,
    ReviewContext,
    ScanResult,
)

# Mirror sage's ``sage_lib/store.py``: prefer the Kiro Crew runtime resolver
# so the app follows any future migration of the data home, but fall back
# to the ``KIROCREW_HOME`` env var + ``~/.kiro/crew`` default when the
# module is loaded standalone (test collection, one-off scripts).
try:
    from kiro_crew.config.paths import config_dir as _config_dir_resolver
except ImportError:  # pragma: no cover - standalone fallback
    _config_dir_resolver = None  # type: ignore[assignment]


APP_NAME = "writing-review"


def crew_home() -> Path:
    """Resolve the active Kiro Crew data root."""
    if _config_dir_resolver is not None:
        return _config_dir_resolver()
    override_home = os.environ.get("KIROCREW_HOME")
    return Path(override_home) if override_home else Path.home() / ".kiro" / "crew"


def _app_data_root(data_dir: Path | None) -> Path:
    """Return the writable ``.../apps/writing-review/data`` directory."""
    if data_dir is not None:
        data_dir.mkdir(parents=True, exist_ok=True)
        return data_dir
    resolved_root = crew_home() / "apps" / APP_NAME / "data"
    resolved_root.mkdir(parents=True, exist_ok=True)
    return resolved_root


def _reviews_dir(data_dir: Path | None) -> Path:
    reviews_directory = _app_data_root(data_dir) / "reviews"
    reviews_directory.mkdir(parents=True, exist_ok=True)
    return reviews_directory


def _settings_path(data_dir: Path | None) -> Path:
    return _app_data_root(data_dir) / "settings.json"


# --- Reviews ----------------------------------------------------------------


def _serialize_finding(finding: Finding) -> dict[str, Any]:
    return asdict(finding)


def _serialize_scan_result(review_id: str, scan_result: ScanResult) -> dict[str, Any]:
    return {
        "id": review_id,
        "doc_name": scan_result.doc_name,
        "doc_path": scan_result.doc_path,
        "status": "active",
        "created_at": int(time.time()),
        "verdict": scan_result.verdict,
        "scanners_run": list(scan_result.scanners_run),
        "context": {
            "audience": scan_result.doc_context.audience,
            "doc_type": scan_result.doc_context.doc_type,
            "tone": scan_result.doc_context.tone,
            "ask": scan_result.doc_context.ask,
            "additional_context": list(scan_result.doc_context.additional_context),
        },
        "findings": [_serialize_finding(finding) for finding in scan_result.findings],
        "partial_failure": scan_result.partial_failure,
        "failed_scanners": [
            asdict(failed_scanner) for failed_scanner in scan_result.failed_scanners
        ],
        "log_reference": dict(scan_result.log_reference),
    }


def save_review(scan_result: ScanResult, data_dir: Path | None = None) -> str:
    """Write a scan result to disk as a JSON file. Returns the new review id."""
    review_id = uuid.uuid4().hex[:16]
    record = _serialize_scan_result(review_id, scan_result)
    review_path = _reviews_dir(data_dir) / f"{review_id}.json"
    review_path.write_text(json.dumps(record, indent=2), encoding="utf-8")
    return review_id


def load_review(review_id: str, data_dir: Path | None = None) -> dict[str, Any] | None:
    """Return the review record, or ``None`` if no such review exists."""
    review_path = _reviews_dir(data_dir) / f"{review_id}.json"
    if not review_path.is_file():
        return None
    return json.loads(review_path.read_text(encoding="utf-8"))


def list_reviews(data_dir: Path | None = None) -> list[dict[str, Any]]:
    """Return summary records for every stored review, newest first.

    A "summary" drops the ``findings`` array so the list endpoint stays
    small even when a review has hundreds of findings; the full detail
    is available via :func:`load_review`.
    """
    reviews_directory = _reviews_dir(data_dir)
    review_summaries: list[dict[str, Any]] = []
    for review_json_path in reviews_directory.glob("*.json"):
        try:
            record = json.loads(review_json_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        review_summaries.append(_summarize_review(record))
    review_summaries.sort(key=lambda summary: summary.get("created_at", 0), reverse=True)
    return review_summaries


def _summarize_review(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": record.get("id"),
        "doc_name": record.get("doc_name"),
        "verdict": record.get("verdict"),
        "finding_count": len(record.get("findings", [])),
        "scanners_run": record.get("scanners_run", []),
        "created_at": record.get("created_at", 0),
    }


def delete_review(review_id: str, data_dir: Path | None = None) -> bool:
    """Remove a review from disk. Returns ``True`` when a file was deleted."""
    review_path = _reviews_dir(data_dir) / f"{review_id}.json"
    if not review_path.is_file():
        return False
    review_path.unlink()
    return True


# --- Settings ---------------------------------------------------------------


def _default_settings() -> dict[str, Any]:
    scanner_toggles: dict[str, bool] = {name: True for name in ALWAYS_ON_SCANNERS}
    # Conditional scanners default OFF; the user opts them in per doc type
    # in the New Review dialog. Values are still surfaced so the UI can
    # render the full picker without having to hard-code the list.
    for conditional_scanner_name in set(CONDITIONAL_SCANNERS.values()):
        scanner_toggles.setdefault(conditional_scanner_name, False)
    from kiro_crew.apps.builtins.writing_review.pool import (
        DEFAULT_MAX_CONCURRENT,
    )

    return {
        "default_audience": "",
        "default_doc_type": "",
        "default_tone": "",
        "scanner_toggles": scanner_toggles,
        "max_concurrent": DEFAULT_MAX_CONCURRENT,
    }


def load_settings(data_dir: Path | None = None) -> dict[str, Any]:
    """Return user settings, filling in defaults for any missing keys."""
    settings_path = _settings_path(data_dir)
    if not settings_path.is_file():
        return _default_settings()
    stored_settings = json.loads(settings_path.read_text(encoding="utf-8"))
    merged_settings = _default_settings()
    merged_settings.update(
        {key: value for key, value in stored_settings.items() if key != "scanner_toggles"}
    )
    stored_toggles = stored_settings.get("scanner_toggles")
    if isinstance(stored_toggles, dict):
        merged_settings["scanner_toggles"].update(stored_toggles)
    return merged_settings


def update_settings(patch_payload: dict[str, Any], data_dir: Path | None = None) -> dict[str, Any]:
    """Merge ``patch_payload`` into the stored settings and return the result.

    ``max_concurrent`` is clamped to ``[1, MAX_CONCURRENT_CEIL]`` on the way
    in so a malicious or fat-fingered patch cannot request 5000 concurrent
    kiro-cli sessions.
    """
    from kiro_crew.apps.builtins.writing_review.pool import MAX_CONCURRENT_CEIL

    current_settings = load_settings(data_dir)
    for key, value in patch_payload.items():
        if key == "scanner_toggles" and isinstance(value, dict):
            current_settings["scanner_toggles"].update(value)
        elif key == "max_concurrent":
            try:
                clamped_max_concurrent = max(1, min(int(value), MAX_CONCURRENT_CEIL))
            except (TypeError, ValueError):
                continue  # ignore malformed values, keep prior
            current_settings["max_concurrent"] = clamped_max_concurrent
        else:
            current_settings[key] = value
    settings_path = _settings_path(data_dir)
    settings_path.write_text(json.dumps(current_settings, indent=2), encoding="utf-8")
    return current_settings


def build_scan_result_from_record(record: dict[str, Any]) -> ScanResult:
    """Rehydrate a stored review record into a :class:`ScanResult` object.

    Used by the artifact-post path (Slice 6) and the discussion context
    endpoint (Slice 4). Handles backward compatibility with pre-V2
    records: missing ``confidence`` on findings gets a default, and
    ``failed_scanners`` may arrive as either the old flat ``list[str]``
    or the new ``list[dict]`` structured form.
    """
    context = ReviewContext(
        audience=record.get("context", {}).get("audience", ""),
        doc_type=record.get("context", {}).get("doc_type", ""),
        tone=record.get("context", {}).get("tone", ""),
        # ``.get("ask", "")`` defaults to empty for pre-Ask records so
        # they load without a KeyError. New records carry the value the
        # user supplied in the New Review dialog.
        ask=record.get("context", {}).get("ask", ""),
        additional_context=list(
            record.get("context", {}).get(
                "additional_context",
                # Backward compat: pre-rename records on disk used the
                # ``exceptions`` key. Fall through so records saved
                # BEFORE the field rename still hydrate cleanly.
                record.get("context", {}).get("exceptions", []),
            )
        ),
    )

    findings: list[Finding] = []
    for finding_dict in record.get("findings", []):
        finding_kwargs = dict(finding_dict)
        # Old records may lack ``confidence``; the dataclass default is
        # "medium" but ``Finding(**dict)`` requires exact keys.
        finding_kwargs.setdefault("confidence", "medium")
        rehydrated_finding = Finding(**finding_kwargs)
        # ``dataclasses.asdict`` flattens nested ``RelatedLocation`` records
        # into plain dicts at serialise time, so persisted records carry a
        # ``list[dict]`` for ``related_locations``. Rehydrate each dict into
        # a ``RelatedLocation`` instance so downstream attribute access
        # (``location.section`` etc.) works. An entry that is already a
        # ``RelatedLocation`` (an already-typed record from an in-process
        # caller) passes through untouched. The explicit ``for`` form (over
        # a comprehension with a ternary) is what lets mypy narrow ``entry``
        # to ``dict`` in the else branch before ``**entry`` unpacks it.
        rehydrated_related_locations: list[RelatedLocation] = []
        for entry in rehydrated_finding.related_locations:
            if isinstance(entry, RelatedLocation):
                rehydrated_related_locations.append(entry)
            elif isinstance(entry, dict):
                rehydrated_related_locations.append(RelatedLocation(**entry))
        rehydrated_finding.related_locations = rehydrated_related_locations
        findings.append(rehydrated_finding)

    failed_scanners: list[FailedScanner] = []
    for entry in record.get("failed_scanners", []):
        if isinstance(entry, str):
            # Pre-V2 flat form -- promote to the structured record so the
            # frontend and discussion agent can render it uniformly.
            failed_scanners.append(
                FailedScanner(
                    name=entry,
                    reason_class="other",
                    message="pre-V2 record",
                    at="",
                    duration_ms=0,
                )
            )
        elif isinstance(entry, dict):
            failed_scanners.append(FailedScanner(**entry))

    log_reference = record.get("log_reference") or {}

    from kiro_crew.apps.builtins.writing_review import Section

    return ScanResult(
        doc_path=record.get("doc_path", ""),
        doc_name=record.get("doc_name", ""),
        doc_context=context,
        sections=[Section(heading="", body="")],
        findings=findings,
        verdict=record.get("verdict", "green"),
        scanners_run=list(record.get("scanners_run", [])),
        partial_failure=bool(record.get("partial_failure", False)),
        failed_scanners=failed_scanners,
        log_reference=log_reference,
    )
