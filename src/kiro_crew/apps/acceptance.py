"""Host-owned execution of app-contributed work-ledger acceptance checks.

The caller supplies no acceptance input here. The work-ledger route loads the stored
condition, then hands that exact object to :func:`evaluation`, which takes the app
lifecycle lock to snapshot the provider, releases it for the provider request, and
retakes it to revalidate before the route commits. A declaration can select only one
app-relative HTTP route and a flat scalar input
schema; no command, executable, URL, MCP server, or tool selector reaches this path.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import re
import unicodedata
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from typing import Any

import aiohttp

from kiro_crew.apps.admission import app_admission_decision
from kiro_crew.apps.backend import get_app_backend_identity
from kiro_crew.apps.execution import (
    app_execution_denied,
    repository_bound_grant_denied,
    shipped_builtin_app_root,
    trusted_app_names,
)
from kiro_crew.apps.manager import (
    APP_MANIFEST_FILENAME,
    _read_installed,
    app_dir,
    get_app_manifest,
)
from kiro_crew.apps.manifest import AcceptanceKindContribution, AppManifest
from kiro_crew.apps.proxy_auth import sign_proxy_request
from kiro_crew.context_assembly.markers import (
    _apply_marker_spans,
    _marker_spans,
    neutralize_untrusted_text,
)
from kiro_crew.platform.context import redact_via_context
from kiro_crew.sel import sel
from kiro_crew.session_directive import neutralize_markers
from kiro_crew.validation import sanitize_string
from kiro_crew.work_vocab import (
    WORK_VERDICTS,
    canonical_json_digest,
    split_app_acceptance_kind,
)

logger = logging.getLogger(__name__)

PROVIDER_TIMEOUT_SECS = 30.0
PROVIDER_CONNECT_TIMEOUT_SECS = 5.0
PROVIDER_RESPONSE_MAX_BYTES = 16 * 1024
PROVIDER_EVIDENCE_MAX_CHARS = 500

# ``neutralize_untrusted_text`` removes boundary and fence markers, but the display-only
# ``[CURRENT USER]`` line is intentionally not one of those prompt boundaries. Provider
# evidence is rendered beside ledger state rather than inside the user's own frame, so it
# must not be able to impersonate that line either.
_EVIDENCE_USER_MARKER_RE = re.compile(r"\[\s*CURRENT\s+USER\s*\]", re.IGNORECASE)
_EVIDENCE_MARKER_REMOVED = "[marker-removed]"
_EVIDENCE_REDACTION_FAILED = "[provider evidence removed: redaction unavailable]"


def _neutralize_evidence_user_marker(text: str) -> str:
    """Neutralize the display-only user marker on the shared normalized view."""
    spans = _marker_spans(text, (_EVIDENCE_USER_MARKER_RE,))
    return _apply_marker_spans(text, spans, _EVIDENCE_MARKER_REMOVED)


@dataclass(frozen=True)
class ProviderEvaluation:
    """A provider verdict plus the provenance the ledger persists beside it."""

    verdict: str
    evidence: str
    provider: str
    kind: str
    version: str = ""
    manifest_digest: str = ""
    backend_generation: str = ""
    acceptance_digest: str = ""
    authority: str = ""
    endpoint: str = ""

    def provenance(self) -> dict[str, str]:
        return {
            "provider": self.provider,
            "kind": self.kind,
            "version": self.version,
            "manifest_digest": self.manifest_digest,
            "backend_generation": self.backend_generation,
            "acceptance_digest": self.acceptance_digest,
            "authority": self.authority,
            "endpoint": self.endpoint,
            "evidence": self.evidence,
        }


def manifest_digest(manifest: AppManifest) -> str:
    """Digest the full parsed manifest, including its contribution and signature."""
    return canonical_json_digest(manifest.to_dict())


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Build one JSON object, refusing duplicate member names."""
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON member: {key}")
        value[key] = item
    return value


def _safe_evidence(value: str) -> str:
    """Neutralize model-facing markers, redact secrets, and cap one evidence line."""
    text = sanitize_string(unicodedata.normalize("NFKC", value))
    text = neutralize_untrusted_text(text)
    text = neutralize_markers(text)
    text = _neutralize_evidence_user_marker(text)
    text = " ".join(text.splitlines())
    try:
        text = redact_via_context(text)
    except Exception:  # noqa: BLE001 - dropping evidence is the fail-closed fallback
        text = _EVIDENCE_REDACTION_FAILED
    if len(text) <= PROVIDER_EVIDENCE_MAX_CHARS:
        return text
    marker = "... [truncated]"
    return text[: PROVIDER_EVIDENCE_MAX_CHARS - len(marker)] + marker


def _failure(
    kind: str,
    verdict: str,
    evidence: str,
    *,
    version: str = "",
    manifest_hash: str = "",
    backend_generation: str = "",
    acceptance_hash: str = "",
    authority: str = "",
    endpoint: str = "",
) -> ProviderEvaluation:
    parts = split_app_acceptance_kind(kind)
    return ProviderEvaluation(
        verdict=verdict,
        evidence=_safe_evidence(evidence),
        provider=parts[0] if parts else "",
        kind=kind,
        version=version,
        manifest_digest=manifest_hash,
        backend_generation=backend_generation,
        acceptance_digest=acceptance_hash,
        authority=authority,
        endpoint=endpoint,
    )


def _find_contribution(
    manifest: AppManifest, app_name: str, kind_id: str
) -> AcceptanceKindContribution | None:
    matches = [
        contribution
        for contribution in manifest.contributes.acceptanceKinds
        if contribution.qualified_name(app_name) == f"{app_name}:{kind_id}"
    ]
    return matches[0] if len(matches) == 1 else None


def _provider_version(meta: Any, manifest: AppManifest) -> str:
    """Return one agreed provider version, or empty on metadata drift."""
    metadata_version = str(getattr(meta, "version", "") or "")
    manifest_version = manifest.version
    if metadata_version and manifest_version and metadata_version != manifest_version:
        return ""
    return metadata_version or manifest_version


def _provider_manifest(app_name: str, backend_identity: Any) -> AppManifest | None:
    """Load the manifest authorized by the executing backend's provenance."""
    if not (backend_identity and backend_identity.admitted_builtin):
        return get_app_manifest(app_name)
    builtin_root = shipped_builtin_app_root(app_name)
    if builtin_root is None:
        return None
    try:
        return AppManifest.from_json_file(builtin_root / APP_MANIFEST_FILENAME)
    except (OSError, UnicodeError, ValueError, TypeError):
        return None


def _read_app_secret(name: str) -> str:
    """Read through the reverse proxy's cache so both gateway call paths agree."""
    # Local import avoids acceptance -> routes -> backend/manager -> acceptance during
    # route composition; the secret is read only after the gateway has initialized.
    from kiro_crew.apps.routes import _get_app_secret

    return _get_app_secret(name)


def _machine_authority(
    app_name: str,
    manifest: AppManifest,
    *,
    caller: str,
    admitted_builtin: bool,
) -> tuple[str, str]:
    """Return ``(authority, denial)`` using only existing trust decisions."""
    builtin_root = shipped_builtin_app_root(app_name) if admitted_builtin else None
    root = builtin_root or app_dir(app_name)
    admission_denial, admission_authority = app_admission_decision(
        app_name,
        manifest,
        action="acceptance_evaluate",
    )
    if admission_denial:
        return "", admission_denial
    execution_denial = app_execution_denied(
        app_name,
        action="acceptance_evaluate",
        app_root=root,
        caller=caller,
    )
    if execution_denial:
        return "", execution_denial
    if builtin_root is not None:
        return "builtin", ""
    if admission_authority:
        return admission_authority, ""
    if app_name in trusted_app_names() and repository_bound_grant_denied(app_name) is None:
        return "trusted-app", ""
    return (
        "",
        "the app may run, but it has no per-app trust grant, verified signature, "
        "or explicit App admission allowlist entry for machine-authoritative acceptance",
    )


def _snapshot(
    acceptance: dict[str, Any], *, caller: str
) -> tuple[dict[str, Any] | None, ProviderEvaluation | None]:
    """Resolve and validate one provider from current installed state."""
    kind = acceptance.get("kind")
    parts = split_app_acceptance_kind(kind)
    if parts is None:
        return None, _failure(str(kind or ""), "error", "not an app-contributed acceptance kind")
    assert isinstance(kind, str)  # guaranteed by split_app_acceptance_kind
    app_name, kind_id = parts
    acceptance_hash = canonical_json_digest(acceptance)
    if not acceptance_hash:
        return None, _failure(kind, "error", "stored acceptance is not canonical JSON")
    if set(acceptance) != {"kind", "input"} or not isinstance(acceptance.get("input"), dict):
        return None, _failure(
            kind,
            "error",
            "a contributed acceptance must contain exactly kind and an input object",
            acceptance_hash=acceptance_hash,
        )

    meta = _read_installed(app_name)
    backend_identity = get_app_backend_identity(app_name)
    manifest = _provider_manifest(app_name, backend_identity)
    if meta is None or manifest is None:
        return None, _failure(
            kind,
            "error",
            "acceptance provider is not installed or its manifest is unreadable",
            acceptance_hash=acceptance_hash,
        )
    version = _provider_version(meta, manifest)
    if not version:
        return None, _failure(
            kind,
            "error",
            "acceptance provider metadata and manifest versions are missing or disagree",
            acceptance_hash=acceptance_hash,
        )
    digest = manifest_digest(manifest)
    if not digest:
        return None, _failure(
            kind,
            "error",
            "acceptance provider manifest cannot be canonicalized",
            version=version,
            acceptance_hash=acceptance_hash,
        )
    manifest_errors = manifest.validate()
    if manifest_errors:
        return None, _failure(
            kind,
            "error",
            "acceptance provider manifest does not validate",
            version=version,
            manifest_hash=digest,
            acceptance_hash=acceptance_hash,
        )
    contribution = _find_contribution(manifest, app_name, kind_id)
    if contribution is None:
        return None, _failure(
            kind,
            "error",
            "acceptance kind is not declared exactly once by the installed provider",
            version=version,
            manifest_hash=digest,
            acceptance_hash=acceptance_hash,
        )
    input_errors = contribution.inputSchema.validate_input(acceptance["input"])
    if input_errors:
        return None, _failure(
            kind,
            "error",
            "; ".join(input_errors),
            version=version,
            manifest_hash=digest,
            acceptance_hash=acceptance_hash,
            endpoint=contribution.endpoint,
        )
    if not meta.enabled:
        return None, _failure(
            kind,
            "refused",
            "acceptance provider is disabled",
            version=version,
            manifest_hash=digest,
            acceptance_hash=acceptance_hash,
            endpoint=contribution.endpoint,
        )
    authority, trust_denial = _machine_authority(
        app_name,
        manifest,
        caller=caller,
        admitted_builtin=bool(backend_identity and backend_identity.admitted_builtin),
    )
    if trust_denial:
        return None, _failure(
            kind,
            "refused",
            trust_denial,
            version=version,
            manifest_hash=digest,
            backend_generation=(backend_identity.generation if backend_identity else ""),
            acceptance_hash=acceptance_hash,
            endpoint=contribution.endpoint,
        )
    if backend_identity is None:
        return None, _failure(
            kind,
            "error",
            "acceptance provider has no healthy gateway-tracked backend with "
            "complete execution identity",
            version=version,
            manifest_hash=digest,
            acceptance_hash=acceptance_hash,
            authority=authority,
            endpoint=contribution.endpoint,
        )
    port = backend_identity.port
    backend_generation = backend_identity.generation
    try:
        secret = _read_app_secret(app_name)
    except OSError:
        secret = ""
    if not secret:
        return None, _failure(
            kind,
            "error",
            "acceptance provider has no usable gateway proxy secret",
            version=version,
            manifest_hash=digest,
            backend_generation=backend_generation,
            acceptance_hash=acceptance_hash,
            authority=authority,
            endpoint=contribution.endpoint,
        )
    return (
        {
            "app": app_name,
            "kind": kind,
            "version": version,
            "manifest_digest": digest,
            "backend_generation": backend_generation,
            "acceptance_digest": acceptance_hash,
            "authority": authority,
            "endpoint": contribution.endpoint,
            "input": acceptance["input"],
            "port": port,
            "secret": secret,
        },
        None,
    )


async def _post_to_provider(snapshot: dict[str, Any]) -> tuple[int, bytes]:
    endpoint = snapshot["endpoint"]
    target = f"/api/{endpoint}"
    body = json.dumps(
        {"kind": snapshot["kind"], "input": snapshot["input"]},
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    header = sign_proxy_request(
        method="POST",
        target=target,
        body=body,
        secret=snapshot["secret"],
    )
    timeout = aiohttp.ClientTimeout(
        total=PROVIDER_TIMEOUT_SECS,
        connect=PROVIDER_CONNECT_TIMEOUT_SECS,
        sock_read=PROVIDER_TIMEOUT_SECS,
    )
    url = f"http://127.0.0.1:{snapshot['port']}{target}"
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(
            url,
            data=body,
            headers={
                "Content-Type": "application/json",
                "X-KiroCrew-Proxy": header,
            },
            allow_redirects=False,
        ) as response:
            try:
                payload = await response.content.readexactly(PROVIDER_RESPONSE_MAX_BYTES + 1)
            except asyncio.IncompleteReadError as exc:
                payload = exc.partial
            return response.status, payload


def _parse_response(snapshot: dict[str, Any], status: int, payload: bytes) -> ProviderEvaluation:
    base = {
        "version": snapshot["version"],
        "manifest_hash": snapshot["manifest_digest"],
        "backend_generation": snapshot["backend_generation"],
        "acceptance_hash": snapshot["acceptance_digest"],
        "authority": snapshot["authority"],
        "endpoint": snapshot["endpoint"],
    }
    if status != 200:
        return _failure(snapshot["kind"], "error", f"provider returned HTTP {status}", **base)
    if len(payload) > PROVIDER_RESPONSE_MAX_BYTES:
        return _failure(
            snapshot["kind"], "error", "provider response exceeded the byte limit", **base
        )
    try:
        decoded = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_unique_json_object,
        )
    except (UnicodeDecodeError, ValueError, RecursionError):
        return _failure(snapshot["kind"], "error", "provider response was not valid JSON", **base)
    if not isinstance(decoded, dict) or set(decoded) != {"verdict", "evidence"}:
        return _failure(
            snapshot["kind"],
            "error",
            "provider response must contain exactly verdict and evidence",
            **base,
        )
    verdict = decoded.get("verdict")
    evidence = decoded.get("evidence")
    if not isinstance(verdict, str) or verdict not in WORK_VERDICTS:
        return _failure(snapshot["kind"], "error", "provider returned an unknown verdict", **base)
    if not isinstance(evidence, str):
        return _failure(snapshot["kind"], "error", "provider evidence must be a string", **base)
    return _failure(snapshot["kind"], verdict, evidence, **base)


def _snapshot_still_current(snapshot: dict[str, Any], *, caller: str) -> bool:
    """Revalidate provider identity and trust after its response arrives."""
    try:
        meta = _read_installed(snapshot["app"])
        backend_identity = get_app_backend_identity(snapshot["app"])
        manifest = _provider_manifest(snapshot["app"], backend_identity)
        if not (
            meta
            and meta.enabled
            and manifest
            and _provider_version(meta, manifest) == snapshot["version"]
            and manifest_digest(manifest) == snapshot["manifest_digest"]
            and _find_contribution(
                manifest,
                snapshot["app"],
                snapshot["kind"].split(":", 1)[1],
            )
            is not None
            and backend_identity is not None
            and backend_identity.port == snapshot["port"]
            and backend_identity.generation == snapshot["backend_generation"]
            and hmac.compare_digest(_read_app_secret(snapshot["app"]), snapshot["secret"])
        ):
            return False
        authority, denial = _machine_authority(
            snapshot["app"],
            manifest,
            caller=caller,
            admitted_builtin=backend_identity.admitted_builtin,
        )
        return not denial and authority == snapshot["authority"]
    except Exception:  # noqa: BLE001 - any revalidation fault discards the verdict
        logger.warning("acceptance provider revalidation failed", exc_info=True)
        return False


def provider_pass_is_current(
    acceptance: dict[str, Any],
    evaluation: dict[str, str],
    *,
    caller: str,
) -> bool:
    """Whether a stored pass still describes the current authorized provider.

    Accepted close is a separate transaction from evaluation. Re-resolve the
    provider under its lifecycle lock so a disable, update, backend loss, or trust
    revocation between those transactions cannot turn historical proof into current
    authority.
    """
    try:
        snapshot, failure = _snapshot(acceptance, caller=caller)
    except Exception:  # noqa: BLE001 - unreadable provider state fails closed
        return False
    if failure is not None or snapshot is None:
        return False
    expected = {
        "provider": snapshot["app"],
        "kind": snapshot["kind"],
        "version": snapshot["version"],
        "manifest_digest": snapshot["manifest_digest"],
        "backend_generation": snapshot["backend_generation"],
        "acceptance_digest": snapshot["acceptance_digest"],
        "authority": snapshot["authority"],
        "endpoint": snapshot["endpoint"],
    }
    return all(evaluation.get(name) == value for name, value in expected.items())


async def _prepare(
    acceptance: dict[str, Any], *, caller: str
) -> tuple[dict[str, Any] | None, ProviderEvaluation | None]:
    """Resolve the provider snapshot; the caller holds its lifecycle lock."""
    try:
        return await asyncio.to_thread(_snapshot, acceptance, caller=caller)
    except Exception:  # noqa: BLE001 - unreadable provider state must fail closed
        kind = acceptance.get("kind")
        return None, _failure(
            kind if isinstance(kind, str) else "",
            "error",
            "acceptance provider state could not be read",
            acceptance_hash=canonical_json_digest(acceptance),
        )


async def _call_provider(snapshot: dict[str, Any]) -> ProviderEvaluation:
    """Run the bounded provider request; no lifecycle lock is held."""
    try:
        status, payload = await _post_to_provider(snapshot)
    except (asyncio.TimeoutError, TimeoutError):
        return _failure(
            snapshot["kind"],
            "error",
            "acceptance provider timed out",
            version=snapshot["version"],
            manifest_hash=snapshot["manifest_digest"],
            backend_generation=snapshot["backend_generation"],
            acceptance_hash=snapshot["acceptance_digest"],
            authority=snapshot["authority"],
            endpoint=snapshot["endpoint"],
        )
    except (aiohttp.ClientError, OSError, ValueError):
        return _failure(
            snapshot["kind"],
            "error",
            "acceptance provider request failed",
            version=snapshot["version"],
            manifest_hash=snapshot["manifest_digest"],
            backend_generation=snapshot["backend_generation"],
            acceptance_hash=snapshot["acceptance_digest"],
            authority=snapshot["authority"],
            endpoint=snapshot["endpoint"],
        )
    return _parse_response(snapshot, status, payload)


async def _confirm(
    snapshot: dict[str, Any], result: ProviderEvaluation, *, caller: str
) -> ProviderEvaluation:
    """Keep *result* only if the provider is unchanged; the caller holds its lock."""
    current = await asyncio.to_thread(
        _snapshot_still_current,
        snapshot,
        caller=caller,
    )
    if not current:
        return _failure(
            snapshot["kind"],
            "error",
            "acceptance provider changed, stopped, was disabled, or lost trust "
            "during evaluation",
            version=snapshot["version"],
            manifest_hash=snapshot["manifest_digest"],
            backend_generation=snapshot["backend_generation"],
            acceptance_hash=snapshot["acceptance_digest"],
            authority=snapshot["authority"],
            endpoint=snapshot["endpoint"],
        )
    return result


@asynccontextmanager
async def evaluation(
    acceptance: dict[str, Any],
    *,
    caller: str,
    lifecycle_lock: AbstractAsyncContextManager[Any],
) -> AsyncIterator[ProviderEvaluation]:
    """Evaluate *acceptance* and yield the result with *lifecycle_lock* held.

    The provider snapshot is taken under the lock. The lock is then released for
    the provider request, because the provider app's own lifecycle-locked routes
    (a notification push, for example) and an operator's disable or update must not
    wait on its HTTP answer. The lock is taken again to revalidate the provider
    against current state, and stays held while the caller's body runs, so the
    caller commits a verdict for a provider that cannot change until the commit
    finishes.
    """
    async with lifecycle_lock:
        snapshot, failure = await _prepare(acceptance, caller=caller)
    if failure is not None:
        async with lifecycle_lock:
            yield failure
        return
    assert snapshot is not None
    provisional = await _call_provider(snapshot)
    async with lifecycle_lock:
        yield await _confirm(snapshot, provisional, caller=caller)


def audit_evaluation(caller: str, result: ProviderEvaluation) -> None:
    """Record one bounded provider decision without its evidence payload."""
    try:
        sel().log_api_access(
            caller=caller,
            operation="app_acceptance_evaluate",
            outcome=result.verdict,
            source="work_ledger",
            resources=(
                f"provider={result.provider} kind={result.kind} version={result.version} "
                f"manifest={result.manifest_digest[:12]} authority={result.authority}"
            ),
        )
    except Exception:  # noqa: BLE001 - audit failure cannot turn an error into a pass
        logger.debug("app acceptance evaluation audit failed", exc_info=True)
