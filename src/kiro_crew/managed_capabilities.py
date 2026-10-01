"""Render fail-closed managed guidance status from provider receipts.

The public core consumes immutable, edition-supplied catalog data and a receipt
bound to one live provider incarnation.  It never discovers edition package
layouts or treats startup inputs as proof that a provider loaded a document.
"""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from dataclasses import dataclass
from typing import Sequence

OPEN_MARKER = "[MANAGED CAPABILITIES -- current-session provider evidence]"
CLOSE_MARKER = "[END MANAGED CAPABILITIES]"

_MAX_CAPABILITIES = 128
_MAX_DOCUMENTS = 256
_MAX_DOCUMENT_BYTES = 524_288
_MAX_PROBLEMS = 64
_MAX_AVAILABILITY_CHARS = 16
_MAX_LABEL_CHARS = 96
_MAX_REASON_CHARS = 180
_CAPABILITY_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,159}$")


@dataclass(frozen=True)
class ManagedCapabilityDeclaration:
    """One capability and the exact provider representation expected for it."""

    capability_id: str
    label: str
    expected_document: str = ""
    availability: str = "expected"
    reason: str = ""


@dataclass(frozen=True)
class ManagedCapabilityCatalog:
    """Immutable catalog snapshot supplied through the composed platform seam."""

    declarations: tuple[ManagedCapabilityDeclaration, ...] = ()
    problems: tuple[str, ...] = ()
    present: bool = False


@dataclass(frozen=True)
class ManagedCapabilityReceipt:
    """Documents a provider confirms loaded for one context incarnation."""

    context_incarnation: object
    documents: tuple[str, ...] = ()


@dataclass(frozen=True)
class _Prepared:
    capability_id: str
    label: str
    availability: str
    digest: str = ""
    reason: str = ""


@dataclass(frozen=True)
class _Status:
    capability_id: str
    label: str
    state: str
    evidence: str


def _string_or_default(value: object, default: str) -> object:
    return default if isinstance(value, str) and value == "" else value


def _metadata_text(value: object, *, subject: str, limit: int) -> tuple[str, str]:
    if not isinstance(value, str):
        return "", f"{subject} is not text"
    if len(value) > limit:
        return "", f"{subject} exceeds the render ceiling"
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return "", f"{subject} is not valid UTF-8"
    text = " ".join(value.split())[:limit]
    # Circular: context imports this module's public types at module load.
    from kiro_crew.context import _neutralize_structural_markers

    neutralized = _neutralize_structural_markers(text)
    if neutralized != text:
        return neutralized, f"{subject} contains structural markers"
    return neutralized, ""


def _availability(value: object) -> tuple[str, str]:
    text, error = _metadata_text(
        value, subject="managed capability availability", limit=_MAX_AVAILABILITY_CHARS
    )
    return text.casefold(), error


def _normalise_document(body: str) -> str:
    """Normalize representation only; authoritative frontmatter is content."""
    body = body.replace("\r\n", "\n").replace("\r", "\n")
    normalized = "\n".join(line.rstrip(" \t") for line in body.split("\n")).strip("\n")
    return normalized + ("\n" if normalized else "")


def _document_error(body: object, *, subject: str) -> str:
    if not isinstance(body, str):
        return f"{subject} is not text"
    if len(body) > _MAX_DOCUMENT_BYTES:
        return f"{subject} exceeds the read ceiling"
    try:
        encoded = body.encode("utf-8")
    except UnicodeEncodeError:
        return f"{subject} is not valid UTF-8"
    if len(encoded) > _MAX_DOCUMENT_BYTES:
        return f"{subject} exceeds the read ceiling"
    if not _normalise_document(body):
        return f"{subject} is empty"
    return ""


def _digest(body: str) -> str:
    return hashlib.sha256(_normalise_document(body).encode("utf-8")).hexdigest()


def _status(item: _Prepared, state: str, evidence: str) -> _Status:
    return _Status(item.capability_id, item.label, state, evidence)


def _problem(index: int, reason: object) -> _Status:
    evidence, error = _metadata_text(
        _string_or_default(reason, "catalog problem is empty"),
        subject="catalog problem",
        limit=_MAX_REASON_CHARS,
    )
    return _Status(
        f"catalog.problem.{index:03d}",
        "Managed context catalog",
        "UNVERIFIED",
        error or evidence,
    )


def _prepare_declaration(value: object, index: int) -> tuple[_Prepared | None, _Status | None]:
    if not isinstance(value, ManagedCapabilityDeclaration):
        return None, _problem(index, "managed capability declaration has the wrong type")
    capability_id = value.capability_id
    if not isinstance(capability_id, str) or not _CAPABILITY_ID_RE.fullmatch(capability_id):
        return None, _problem(index, "managed capability id is invalid")
    label, label_error = _metadata_text(
        _string_or_default(value.label, capability_id),
        subject="managed capability label",
        limit=_MAX_LABEL_CHARS,
    )
    if label_error:
        return None, _problem(index, label_error)
    availability, availability_error = _availability(value.availability)
    item = _Prepared(capability_id, label, availability)
    if availability_error:
        return None, _status(item, "UNVERIFIED", availability_error)
    if availability == "unavailable":
        reason, reason_error = _metadata_text(
            _string_or_default(value.reason, "trusted catalog reports unavailable"),
            subject="managed capability reason",
            limit=_MAX_REASON_CHARS,
        )
        if reason_error:
            return None, _status(item, "UNVERIFIED", reason_error)
        return _Prepared(capability_id, label, availability, reason=reason), None
    if availability != "expected":
        return None, _status(item, "UNVERIFIED", "catalog availability is invalid")
    error = _document_error(value.expected_document, subject="expected provider document")
    if error:
        return None, _status(item, "UNVERIFIED", error)
    return _Prepared(capability_id, label, availability, _digest(value.expected_document)), None


def _catalog_problems(catalog: ManagedCapabilityCatalog) -> list[_Status]:
    if not isinstance(catalog.problems, tuple):
        return [_problem(1, "managed catalog problems are not immutable")]
    statuses = [
        _problem(index, value) for index, value in enumerate(catalog.problems[:_MAX_PROBLEMS], 1)
    ]
    if len(catalog.problems) > _MAX_PROBLEMS:
        statuses.append(_problem(_MAX_PROBLEMS + 1, "managed catalog problem ceiling reached"))
    return statuses


def _catalog_present_flag(catalog: ManagedCapabilityCatalog, statuses: list[_Status]) -> bool:
    if not isinstance(catalog.present, bool):
        statuses.append(_problem(len(statuses) + 1, "managed catalog presence is not boolean"))
        return False
    if not catalog.present and (catalog.declarations or catalog.problems):
        statuses.append(
            _problem(len(statuses) + 1, "managed catalog presence flag is inconsistent")
        )
    return catalog.present


def _prepare_catalog(
    catalog: object,
) -> tuple[tuple[_Prepared, ...], tuple[_Status, ...], bool]:
    if not isinstance(catalog, ManagedCapabilityCatalog):
        return (), (_problem(1, "managed catalog snapshot has the wrong type"),), True
    if not isinstance(catalog.declarations, tuple):
        return (), (_problem(1, "managed catalog declarations are not immutable"),), True
    if not isinstance(catalog.problems, tuple):
        return (), (_problem(1, "managed catalog problems are not immutable"),), True
    statuses = _catalog_problems(catalog)
    present_flag = _catalog_present_flag(catalog, statuses)
    raw = catalog.declarations[:_MAX_CAPABILITIES]
    if len(catalog.declarations) > _MAX_CAPABILITIES:
        statuses.append(_problem(len(statuses) + 1, "managed capability ceiling reached"))
    prepared: list[_Prepared] = []
    for index, declaration in enumerate(raw, 1):
        item, problem = _prepare_declaration(declaration, index)
        if item is not None:
            prepared.append(item)
        if problem is not None:
            statuses.append(problem)
    present = present_flag or bool(catalog.declarations) or bool(catalog.problems) or bool(statuses)
    return tuple(prepared), tuple(statuses), present


def _receipt_digest_counts(documents: Sequence[str]) -> tuple[dict[str, int], str]:
    if not isinstance(documents, tuple):
        return {}, "provider receipt documents are not immutable"
    if len(documents) > _MAX_DOCUMENTS:
        return {}, "provider receipt document ceiling exceeded"
    counts: dict[str, int] = {}
    for document in documents:
        error = _document_error(document, subject="provider receipt document")
        if error:
            return {}, error
        digest = _digest(document)
        counts[digest] = counts.get(digest, 0) + 1
    return counts, ""


def _evaluate_expected(
    item: _Prepared,
    receipt_counts: dict[str, int],
    receipt_problem: str,
    declaration_digest_count: int,
) -> _Status:
    if declaration_digest_count != 1:
        return _status(item, "UNVERIFIED", "ambiguous authoritative catalog content")
    if receipt_problem:
        return _status(item, "UNVERIFIED", receipt_problem)
    matches = receipt_counts.get(item.digest, 0)
    if matches == 1:
        return _status(item, "AVAILABLE", "exact normalized content matched one provider receipt")
    if matches > 1:
        return _status(item, "UNVERIFIED", "ambiguous duplicate provider receipts")
    return _status(item, "UNVERIFIED", "no current-session provider receipt matched the catalog")


def _evaluate(
    prepared: tuple[_Prepared, ...],
    receipt_counts: dict[str, int],
    receipt_problem: str,
    catalog_problem: bool,
) -> tuple[_Status, ...]:
    id_counts = Counter(item.capability_id for item in prepared)
    digest_counts = Counter(item.digest for item in prepared if item.digest)
    statuses: list[_Status] = []
    for item in prepared:
        if catalog_problem:
            statuses.append(
                _status(item, "UNVERIFIED", "catalog snapshot is incomplete or inconsistent")
            )
        elif id_counts[item.capability_id] != 1:
            statuses.append(_status(item, "UNVERIFIED", "duplicate capability declaration"))
        elif item.availability == "unavailable":
            statuses.append(_status(item, "UNAVAILABLE", item.reason))
        else:
            statuses.append(
                _evaluate_expected(
                    item, receipt_counts, receipt_problem, digest_counts[item.digest]
                )
            )
    return tuple(statuses)


def _nonempty(statuses: tuple[_Status, ...]) -> tuple[_Status, ...]:
    if statuses:
        return statuses
    return (
        _Status(
            "managed-context",
            "Managed context catalog",
            "UNVERIFIED",
            "no current managed context declarations were verifiable",
        ),
    )


def _cache_miss_status() -> _Status:
    return _Status(
        "managed-context-cache",
        "Managed context session evidence",
        "UNVERIFIED",
        "cached current-session attestation is unavailable after compaction",
    )


def _render(statuses: tuple[_Status, ...]) -> str:
    ordered = tuple(sorted(statuses, key=lambda item: (item.capability_id, item.label)))
    shown = ordered[:_MAX_CAPABILITIES]
    lines = [
        OPEN_MARKER,
        "This reports managed guidance availability for this provider session; "
        "it is not authorization and does not prove tool or deployment availability.",
    ]
    lines.extend(
        f"- {item.label} ({item.capability_id}): {item.state} — {item.evidence}." for item in shown
    )
    if len(ordered) > len(shown):
        lines.append(
            f"- Managed context catalog: UNVERIFIED — {len(ordered) - len(shown)} "
            "additional entries exceeded the rendered ceiling."
        )
    return "\n".join([*lines, CLOSE_MARKER]) + "\n\n"


def build_managed_capabilities_block(
    catalog: ManagedCapabilityCatalog,
    documents: tuple[str, ...] = (),
    *,
    cache_miss: bool = False,
    receipt_problem: str = "",
) -> str:
    """Render status from a catalog snapshot and provider-confirmed documents."""
    prepared, catalog_statuses, present = _prepare_catalog(catalog)
    if not present:
        return ""
    if cache_miss:
        return _render((_cache_miss_status(),))
    receipt_counts, document_problem = _receipt_digest_counts(documents)
    receipt_text, receipt_metadata_error = _metadata_text(
        receipt_problem,
        subject="provider receipt problem",
        limit=_MAX_REASON_CHARS,
    )
    problem = receipt_metadata_error or receipt_text or document_problem
    statuses = (
        *catalog_statuses,
        *_evaluate(prepared, receipt_counts, problem, bool(catalog_statuses)),
    )
    return _render(_nonempty(statuses))


__all__ = [
    "CLOSE_MARKER",
    "ManagedCapabilityCatalog",
    "ManagedCapabilityDeclaration",
    "ManagedCapabilityReceipt",
    "OPEN_MARKER",
    "build_managed_capabilities_block",
]
