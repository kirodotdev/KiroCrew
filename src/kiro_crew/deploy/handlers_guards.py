"""The refusals, redactions and audit records every deploy endpoint shares.

Every mutating deploy route refuses a restricted session through ``_deny_restricted``,
passes the strings it answers with through the credential and exfiltration-URL redaction
here (``_redact_text``, ``_sanitize_response`` and the profile and pending-entry
variants), turns an exception into safe response text with ``_safe_err``, and records each
permission decision in the SEL audit log through ``_audit``. The operator commands a
finite-TTL refusal hands over (``_reaper_remediation``, ``_base_remediation``) are rendered
here too, so the 409 and the setup guide print the same command. The route layer
(:mod:`kiro_crew.deploy.handlers`) and the site operations
(:mod:`kiro_crew.deploy.handlers_sites`) import these; this module imports neither.
"""

from __future__ import annotations

from typing import Any

from aiohttp import web

from kiro_crew.security import redact_credentials, redact_exfiltration_urls
from kiro_crew.sel import sel

# --- restricted-session guard (consistent across all mutating endpoints) -----


def _deny_restricted(request: "web.Request", operation: str) -> "web.Response | None":
    """Return a 403 Response if the request comes from a restricted session.

    Applies to ALL mutating deploy endpoints: config PUT, deploy, recall, destroy,
    teardown, profiles POST/PUT/DELETE, and verify (back-fills registry = write).
    Read-only handlers (GET config, list, iam-policy, profiles GET) are exempt.
    Returns None when access is allowed.

    Fail-closed: if the app or state cannot be resolved, access is DENIED.
    Test fixtures must install a proper state on the request app (see
    test_deploy_web_handlers.py _NonRestrictedReq for the canonical pattern).
    """
    from kiro_crew.dashboard.handlers._shared import _is_restricted_session

    app = getattr(request, "app", None)
    if app is None:
        _audit(operation, "", "denied", error="no app context")
        return web.json_response(
            {"error": "restricted session cannot perform this operation"}, status=403
        )
    state = app.get("state")
    if state is None:
        _audit(operation, "", "denied", error="missing dashboard state")
        return web.json_response(
            {"error": "restricted session cannot perform this operation"}, status=403
        )
    if _is_restricted_session(state, request):
        _audit(operation, "", "denied", error="restricted session")
        return web.json_response(
            {"error": "restricted session cannot perform this operation"}, status=403
        )
    return None


def _redact_text(s: str) -> str:
    """Redact credentials and internal URLs from any text returned in response bodies.

    Applied to scan summary strings, error messages, and any findings-carrying
    payload before it reaches LLM/dashboard callers. Defense-in-depth: even if
    _mask_credential is applied at Finding creation time, this catches any path
    where raw matched content leaks through.
    """
    s, _ = redact_credentials(s)
    s, _ = redact_exfiltration_urls(s)
    return s


def _sanitize_response(payload: Any) -> Any:
    """Recursively apply credential + exfiltration redaction to all str values in a response payload.

    Deploy handler error responses echo LLM-controlled values (local_dir,
    site_id, profile), which need BOTH redaction passes. This helper walks
    dict/list structures and applies _redact_text to every str leaf. Applied at
    the three chokepoint handlers (_handle_deploy, _handle_recall,
    _handle_destroy) so ALL paths through _do_* are covered in one place.
    """
    if isinstance(payload, str):
        return _redact_text(payload)
    if isinstance(payload, dict):
        return {k: _sanitize_response(v) for k, v in payload.items()}
    if isinstance(payload, list):
        return [_sanitize_response(v) for v in payload]
    return payload


def _redact_profile_fields(profiles: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Redact credential-containing strings from profile entries before response.

    Applied at serialization in every handler that returns registry entries.
    SEL audit keeps the raw data; dashboard/API responses are sanitized.
    """
    out: list[dict[str, Any]] = []
    for p in profiles:
        entry: dict[str, Any] = {}
        for k, v in p.items():
            if isinstance(v, str) and v:
                v = _redact_text(v)
            entry[k] = v
        out.append(entry)
    return out


def _redact_pending_entries(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Recursively redact every string value in pending entries before response.

    Defense-in-depth: pending entries contain LLM-controlled fields
    (site_id, local_dir, scan_summary, profile) that could carry injected
    credential strings. Same pipeline as _redact_profile_fields.
    """
    out: list[dict[str, Any]] = []
    for e in entries:
        redacted: dict[str, Any] = {}
        for k, v in e.items():
            if isinstance(v, str) and v:
                v = _redact_text(v)
            elif isinstance(v, dict):
                # Recurse one level for nested dicts
                v = {
                    dk: _redact_text(dv) if isinstance(dv, str) and dv else dv
                    for dk, dv in v.items()
                }
            redacted[k] = v
        out.append(redacted)
    return out


def _reaper_remediation(profile: str, region: str) -> str:
    """Exact operator command for the finite-TTL reaper precondition.

    Rendered with the request's real profile/region so the 409 payload is
    directly actionable. Installing the reaper is an operator step by design
    (the stack creates an IAM role, which Kiro Crew never does itself).

    The script is named by ABSOLUTE path, resolved from the live skills
    directory. ``install-reaper.sh`` is not on anyone's PATH, so a bare name is
    not a command the user can run, and the path is not a constant either: an
    install rooted at ``~/.kirocrew`` and one rooted at ``~/.kiro/crew`` both
    occur, so a hardcoded spelling is wrong on one of them. Falls back to the
    bare name if the skills root cannot be resolved.
    """
    script = "install-reaper.sh"
    try:
        from kiro_crew.skills import skills_dir

        candidate = skills_dir() / "artifact-deploy" / "scripts" / "install-reaper.sh"
        script = str(candidate)
    except Exception:  # noqa: BLE001 — remediation text must never break the 409
        pass
    parts = [script]
    if profile:
        parts += ["--profile", profile]
    if region:
        parts += ["--region", region]
    return " ".join(parts)


def _base_remediation(profile: str, region: str) -> str:
    """Operator commands for a finite-TTL deploy when the BASE stack is missing.

    ``install-reaper.sh`` reads the base stack's outputs first and exits with
    "base stack not found" when it is absent, so handing it over alone is a
    command that cannot succeed in that state. The base has to come first.
    ``deploy.sh`` is the only script that creates it, but it also publishes an
    app directory, so the remediation spells out its base step instead: the same
    ``cloudformation deploy`` of ``templates/base-stack.yaml`` that
    ``deploy.sh`` runs, then the reaper install.
    """
    template = "templates/base-stack.yaml"
    try:
        from kiro_crew.skills import skills_dir

        template = str(skills_dir() / "artifact-deploy" / "templates" / "base-stack.yaml")
    except Exception:  # noqa: BLE001 — remediation text must never break the 409
        pass
    parts = [
        "aws",
        "cloudformation",
        "deploy",
        "--stack-name",
        "kirocrew-deploy-base",
        "--template-file",
        template,
        "--no-fail-on-empty-changeset",
        "--tags",
        "kirocrew:managed=true",
    ]
    if profile:
        parts += ["--profile", profile]
    if region:
        parts += ["--region", region]
    return " ".join(parts) + " && " + _reaper_remediation(profile, region)


def _audit(action: str, site_id: str, outcome: str, *, error: str = "") -> None:
    """Emit a SEL audit event for a deploy-web permission decision.

    deploy/recall/destroy create public internet infrastructure, delete
    resources, and make content world-readable — each confirmed action is a
    significant permission decision and MUST be recorded (§9.3).
    """
    sel().log_api_access(
        caller="core:deploy",
        operation=f"deploy.{action}",
        outcome=outcome,
        source="builtin-app",
        resources=site_id,
        error=error[:200] if error else "",
    )


def _safe_err(exc: BaseException) -> str:
    """Return a safe error message with credentials/URLs redacted.

    AWS CLI stderr can contain credential fragments (access key ids, session
    tokens, etc.) — never surface the raw exception in response payloads.
    """
    return _redact_text(str(exc))
