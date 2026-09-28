"""Jira issue reads over the REST API, with the credential resolved per host.

Jira is the one provider with no CLI: the gateway calls the instance itself, so
the token is resolved here -- vault first, then the protected ``.env`` -- and sent
only in the request header. Cloud descriptions arrive as Atlassian Document
Format and are rendered to markdown by the ``adf`` owner.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
from typing import Any
from urllib.parse import urlparse

import aiohttp

from kiro_crew.config.loader import (
    CRED_JIRA_API_TOKEN,
    KiroCrewConfig,
    config_dir,
    jira_global_token_applicable,
    jira_host_token_name,
    normalize_jira_host,
    read_env_file_credential,
)
from kiro_crew.dashboard.handlers._shared import read_capped_response
from kiro_crew.dashboard.source_providers import adf, projection, sanitize
from kiro_crew.dashboard.source_providers.contract import SourceProviderError, SourceRef
from kiro_crew.secrets import SecretVault

_JIRA_FETCH_TIMEOUT = 15  # seconds per HTTP call
_JIRA_MAX_COMMENTS = 50


def _get_jira_auth(host: str) -> tuple[str, str] | None:
    """Return (email, token) for *host* from config + vault/.env, or None.

    Host and email come from config.json (non-sensitive metadata). The token is
    resolved from the encrypted vault first (successor store, populated by
    ``kirocrew secrets import``), falling back to the protected .env file /
    environment for installs that have not migrated — following the same
    credential isolation pattern as Slack/Discord/Telegram tokens, never stored
    in the agent-readable config.json.

    Raises ValueError on config load failures so callers can distinguish
    "config is broken" from "no credentials configured" (None).
    """
    try:
        # Snapshot the process-environment value of JIRA_API_TOKEN BEFORE
        # KiroCrewConfig.load() / load_credentials() runs.  load_credentials()
        # calls os.environ.setdefault(CRED_JIRA_API_TOKEN, ...) which seeds the
        # .env global into os.environ when no real env override is present.
        # Reading os.environ["JIRA_API_TOKEN"] AFTER that call would treat a
        # merely-seeded .env value as a "live env override", causing the
        # single-host global branch to use the .env global instead of the vault
        # for a host that has its OWN per-host token in the vault.
        # Capturing the value here — before any setdefault — means only a real
        # operator-set env var (present before this call) counts as an override.
        _env_global_override = os.environ.get("JIRA_API_TOKEN")
        cfg = KiroCrewConfig.load()
        entries = cfg.dashboard.jira_auth
        # Token is resolved from .env / environment, not config.json
        creds = cfg.load_credentials()
    except Exception as exc:
        raise ValueError(f"jira_config_error: Could not load Jira configuration: {exc}") from exc
    normalized = normalize_jira_host(host)
    for entry in entries:
        entry_host = normalize_jira_host(entry.host)
        if entry_host == normalized:
            # Per-host token takes precedence. The shared helper owns the
            # collision-free normalization and hex transform used by every
            # producer/consumer of this key.
            per_host_name = jira_host_token_name(entry_host)
            # Resolution order: the encrypted vault first (the successor store,
            # populated by `kirocrew secrets import`), then the legacy .env /
            # environment value so existing installs keep working unchanged.
            #
            # EXCEPTION for the global `JIRA_API_TOKEN`: a nonempty PROCESS-
            # ENVIRONMENT value overrides even the vault. `load_credentials`
            # overlays `os.environ` over the .env for this key, so a live env
            # var is the effective credential at runtime — and `kirocrew secrets
            # import` deliberately SKIPS migrating the key while such an override
            # is set, precisely so it does not get pinned into the vault. But a
            # vault entry written by an EARLIER migration (before the override
            # existed) would otherwise be read vault-first and silently shadow
            # that override. Consulting the env override before the global vault
            # entry keeps the migrate-skip and the resolve-order consistent.
            # Per-host `JIRA_TOKEN_<HEX>` keys are NOT env-overlaid, so they are
            # unaffected and keep their vault-first order.
            #
            # We use `_env_global_override` (captured BEFORE load_credentials
            # ran) rather than a fresh os.environ read so that a value merely
            # seeded by load_credentials' setdefault — which is NOT a real
            # operator override — cannot masquerade as one here.
            #
            # HOWEVER: `GatewayOrchestrator.__init__` calls `load_credentials()`
            # at startup, which seeds the `.env` global into `os.environ` via
            # `setdefault` BEFORE any request handler runs. A subsequent call to
            # `_get_jira_auth` would then capture that `.env`-seeded value as
            # `_env_global_override`, indistinguishable from a real operator
            # override. Fix: after ruling out secret refs, compare the captured
            # env value against the current `.env` file value — an equal value
            # came from `.env` (stale, do not override the vault), a different
            # value means the operator set a distinct override at runtime (treat
            # as authoritative). `read_env_file_credential` blocks on I/O but
            # `_get_jira_auth` is called via `asyncio.to_thread` so that is safe.
            token = _resolve_jira_token_from_vault(per_host_name)
            if not token and jira_global_token_applicable(entries):
                # A `secret://` value is a vault REFERENCE, not a raw token.
                # After `secrets import --apply` the `.env` line becomes
                # `JIRA_API_TOKEN=secret://JIRA_API_TOKEN`, and `load_credentials`
                # propagates that into os.environ (and `creds`) via setdefault.
                # So an env/creds value that is a `secret://` ref must NOT be
                # used as the token — fall through to the vault. Only a real,
                # non-ref env value that DIFFERS from the `.env` file counts as
                # a genuine live override that beats the global vault entry.
                _env_file_val = read_env_file_credential("JIRA_API_TOKEN")
                _is_genuine_override = (
                    _env_global_override
                    and not _is_secret_ref(_env_global_override)
                    and _env_global_override != _env_file_val
                )
                if _is_genuine_override:
                    # _is_genuine_override is truthy only when _env_global_override
                    # is a non-empty str, so `or ""` is dead in practice — it only
                    # narrows str | None -> str for the type checker.
                    token = _env_global_override or ""
                else:
                    token = _resolve_jira_token_from_vault(CRED_JIRA_API_TOKEN)
            if not token:
                _c = creds.get(per_host_name, "")
                token = _c if not _is_secret_ref(_c) else ""
            if not token and jira_global_token_applicable(entries):
                _c = creds.get("JIRA_API_TOKEN", "")
                token = _c if not _is_secret_ref(_c) else ""
            if not token:
                return None
            return (entry.email or "", token)
    return None


def _is_secret_ref(value: str) -> bool:
    """True if *value* is a ``secret://`` vault reference rather than a raw token.

    After ``secrets import --apply`` the ``.env`` line for a migrated key becomes
    ``KEY=secret://KEY``, and ``load_credentials`` propagates that string into
    both ``os.environ`` and the returned creds dict. Such a value is a POINTER
    to the vault, not a usable credential, so the resolver must treat it as
    "look in the vault" and never hand it to Jira as the token.
    """
    return value.startswith("secret://")


def _resolve_jira_token_from_vault(name: str) -> str:
    """Return the vault secret *name*, or ``""`` if absent/unavailable.

    Best-effort: a missing vault, missing entry, or read error all yield the
    empty string so the caller falls back to the legacy .env / environment
    value rather than failing.
    """
    try:
        secret = SecretVault(config_dir()).get(name)
    except Exception:
        return ""
    return secret.reveal() if secret is not None else ""


def _jira_is_cloud(host: str) -> bool:
    """True if host is an Atlassian Cloud instance."""
    return host.lower().endswith(".atlassian.net")


def _jira_linked_changes(fields: dict[str, Any], base_url: str) -> list[dict[str, Any]]:
    """Parse Jira issuelinks into the linkedChanges format.

    Jira issue links have an inward and outward side.  Each link object
    contains either an ``inwardIssue`` or ``outwardIssue`` (never both).
    We normalise both directions into a flat list with the relationship
    type visible to the user.
    """
    raw_links = projection._as_list(fields.get("issuelinks"))
    changes: list[dict[str, Any]] = []
    seen: set[str] = set()
    for link in raw_links:
        if not isinstance(link, dict):
            continue
        link_type = projection._as_dict(link.get("type"))
        # Determine direction and extract the linked issue object
        if isinstance(link.get("outwardIssue"), dict):
            linked_issue = link["outwardIssue"]
            relation = str(link_type.get("outward") or "")
        elif isinstance(link.get("inwardIssue"), dict):
            linked_issue = link["inwardIssue"]
            relation = str(link_type.get("inward") or "")
        else:
            continue
        issue_key = str(linked_issue.get("key") or "")
        if not issue_key or issue_key in seen:
            continue
        seen.add(issue_key)
        # Derive browse URL from base_url
        url = f"{base_url}/browse/{issue_key}"
        # Extract state from statusCategory
        status_obj = projection._as_dict(
            linked_issue.get("fields", {}).get("status")
            if isinstance(linked_issue.get("fields"), dict)
            else {}
        )
        status_cat = projection._as_dict(status_obj.get("statusCategory"))
        cat_key = str(status_cat.get("key") or "").lower()
        state = "closed" if cat_key == "done" else "open"
        # Extract issue number (numeric portion after the dash)
        parts = issue_key.rsplit("-", 1)
        number = int(parts[1]) if len(parts) == 2 and parts[1].isdigit() else 0
        # Summary for title
        linked_fields = (
            linked_issue.get("fields") if isinstance(linked_issue.get("fields"), dict) else {}
        )
        title = str(linked_fields.get("summary") or "") if isinstance(linked_fields, dict) else ""
        changes.append(
            {
                "provider": "jira",
                "url": url,
                "number": number,
                "title": title or issue_key,
                "state": state,
                "relation": relation,
                "issueKey": issue_key,
            }
        )
    return changes


def _jira_fix_versions(fields: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the fix versions that can populate the milestone slot.

    A version is usable only when it is an object carrying a non-empty
    ``name``: the Issue panel gates the milestone chip on the object being
    truthy, so a nameless version would render an icon with no text.  A
    malformed leading entry therefore does not hide a usable one behind it.
    """
    usable: list[dict[str, Any]] = []
    for version in projection._as_list(fields.get("fixVersions")):
        if not isinstance(version, dict):
            continue
        if str(version.get("name") or "").strip():
            usable.append(version)
    return usable


def _jira_version_is_done(version: dict[str, Any]) -> bool:
    """A released or archived version takes no new work."""
    return bool(version.get("released")) or bool(version.get("archived"))


def _jira_pick_fix_version(fix_versions: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Pick the fix version that fills the one-slot milestone contract.

    The Issue panel's milestone chip renders only the version's name, with no
    released/archived signal, so surfacing a shipped release reads as if it
    were the pending one.  Prefer the first version that is
    neither released nor archived; when every version has shipped, fall back
    to the first usable entry so the ticket still shows a release rather than
    dropping to no milestone at all.
    """
    pending = [v for v in fix_versions if not _jira_version_is_done(v)]
    return (pending or fix_versions)[0] if fix_versions else None


def _jira_fix_version_milestone(version: dict[str, Any]) -> dict[str, str]:
    """Map one Jira fix version onto the ``IssueMilestone`` contract.

    Jira has no milestones; a fix version is the release a ticket is
    scheduled for, which is the same thing the panel's milestone chip
    communicates.  ``name`` becomes the title and ``releaseDate`` (ISO, unlike
    the locale-formatted ``userReleaseDate``) becomes ``dueOn``.

    ``state`` has only GitHub's two values to choose from.  A released version
    is done, and an archived one takes no more work, so both map to
    ``closed`` and everything else stays ``open``.
    """
    released = _jira_version_is_done(version)
    return {
        "title": str(version.get("name") or "").strip(),
        "state": "closed" if released else "open",
        "dueOn": str(version.get("releaseDate") or ""),
    }


async def _fetch_jira_issue(ref: SourceRef) -> dict[str, Any]:
    """Fetch a Jira issue via the REST API using configured credentials.

    Raises ValueError with a machine-readable prefix when no credentials are
    configured (frontend uses this to show the link-out fallback).
    """
    # Offload config I/O to a thread — KiroCrewConfig.load() is synchronous
    # (stats, reads, json.loads, jsonschema.validate) and must never run on
    # the event loop. Same discipline as hosts._load_source_link_settings.
    auth_pair = await asyncio.to_thread(_get_jira_auth, ref.host)
    if auth_pair is None:
        per_host_name = jira_host_token_name(ref.host)
        raise ValueError(
            "jira_no_credentials: No Jira credentials configured for "
            f"{ref.host}. Add a jira_auth entry to config.json and set "
            f"JIRA_API_TOKEN (or {per_host_name} for multi-host) "
            "in your .env file."
        )
    email, token = auth_pair
    is_cloud = _jira_is_cloud(ref.host)
    # Cloud uses API v3 (ADF description); Server/DC uses v2 (wiki/text).
    api_version = "3" if is_cloud else "2"
    issue_key = f"{ref.owner}-{ref.number}" if ref.owner else f"{ref.repo}-{ref.number}"
    # Preserve the context path prefix from the validated URL (e.g. /jira in
    # https://corp.example/jira/browse/PROJ-123) so Server/DC instances
    # behind a reverse proxy reach the correct REST endpoint.
    parsed = urlparse(ref.url)
    browse_idx = parsed.path.find("/browse/")
    context_path = parsed.path[:browse_idx] if browse_idx > 0 else ""
    base_url = f"https://{ref.host}{context_path}"
    issue_url = (
        f"{base_url}/rest/api/{api_version}/issue/{issue_key}"
        f"?fields=summary,status,issuetype,assignee,description,labels,"
        f"comment,priority,reporter,created,updated,resolution,resolutiondate,"
        f"issuelinks,fixVersions"
    )

    # Build auth header
    headers: dict[str, str] = {"Accept": "application/json"}
    if is_cloud and email:
        # Basic auth: email:token
        cred = base64.b64encode(f"{email}:{token}".encode()).decode()
        headers["Authorization"] = f"Basic {cred}"
    else:
        # Bearer auth (PAT) for Server/DC
        headers["Authorization"] = f"Bearer {token}"

    timeout = aiohttp.ClientTimeout(total=_JIRA_FETCH_TIMEOUT)
    try:
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
            async with session.get(issue_url, allow_redirects=False) as resp:
                if resp.status == 401:
                    raise SourceProviderError(
                        "Jira authentication failed. For Cloud, verify both email and API token in "
                        "dashboard.jira_auth settings."
                    )
                if resp.status == 403:
                    raise SourceProviderError(
                        "Jira access denied. The configured token may lack "
                        "permission to read this issue."
                    )
                if resp.status == 404:
                    raise SourceProviderError(f"Jira issue {issue_key} not found on {ref.host}.")
                if resp.status != 200:
                    raise SourceProviderError(f"Jira returned HTTP {resp.status} for {issue_key}.")
                # Bound response size to prevent memory exhaustion from an
                # oversized or malicious payload before JSON decoding. Streamed
                # to EOF: a single read(n) resolves on the first buffered chunk
                # of a chunked response and would hand json.loads a truncated
                # document.
                body = await read_capped_response(resp, sanitize._MAX_PAYLOAD_BYTES)
                if len(body) > sanitize._MAX_PAYLOAD_BYTES:
                    raise SourceProviderError(
                        f"Jira response for {issue_key} exceeds the size limit."
                    )
                try:
                    data = json.loads(body)
                except RecursionError:
                    raise SourceProviderError(
                        f"Jira response for {issue_key} is too deeply nested."
                    )
    except SourceProviderError:
        raise
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        raise SourceProviderError(
            f"Could not reach Jira at {ref.host}: {type(exc).__name__}"
        ) from exc
    except ValueError as exc:
        raise SourceProviderError(
            f"Jira returned an unparseable response for {issue_key}."
        ) from exc

    if not isinstance(data, dict):
        raise SourceProviderError("Jira returned an invalid issue payload")

    fields = data.get("fields")
    if not isinstance(fields, dict):
        fields = {}

    # Extract description
    raw_desc = fields.get("description")
    if isinstance(raw_desc, dict):
        # ADF (Cloud v3). The converter redacts internally, where it escapes:
        # `_adf_inline_sequence` for a run's text and `_adf_attr_label` for an
        # attribute. Both run inside its depth-capped traversal, so no unbounded
        # pre-pass walks a provider-controlled tree.
        description = adf._adf_to_markdown(raw_desc).strip()
    elif isinstance(raw_desc, str):
        # Plain text or wiki markup (Server v2)
        description = raw_desc
    else:
        description = ""

    # Extract status — use statusCategory.key which is locale-proof and
    # canonical ("done", "new", "indeterminate") rather than the English name.
    status_obj = projection._as_dict(fields.get("status"))
    status_category = projection._as_dict(status_obj.get("statusCategory"))
    category_key = str(status_category.get("key") or "").lower()
    state = "closed" if category_key == "done" else "open"

    # Resolution as state reason
    resolution = fields.get("resolution")
    state_reason = ""
    if isinstance(resolution, dict):
        state_reason = str(resolution.get("name") or "")

    # Reporter/author
    reporter = projection._as_dict(fields.get("reporter"))
    author = str(reporter.get("displayName") or reporter.get("name") or "")

    # Assignee
    assignee_obj = fields.get("assignee")
    assignees: list[str] = []
    if isinstance(assignee_obj, dict):
        name = str(assignee_obj.get("displayName") or assignee_obj.get("name") or "")
        if name:
            assignees.append(name)

    # Labels
    _raw_labels = fields.get("labels")
    raw_labels: list[Any] = _raw_labels if isinstance(_raw_labels, list) else []
    labels = [{"name": str(lbl), "color": "", "description": ""} for lbl in raw_labels if lbl]

    # Priority as a pseudo-label (Jira has no label colors)
    priority_obj = fields.get("priority")
    if isinstance(priority_obj, dict):
        pname = str(priority_obj.get("name") or "")
        if pname:
            labels.insert(0, {"name": f"Priority: {pname}", "color": "", "description": ""})

    # Issue type as a pseudo-label
    issuetype_obj = fields.get("issuetype")
    if isinstance(issuetype_obj, dict):
        tname = str(issuetype_obj.get("name") or "")
        if tname:
            labels.insert(0, {"name": tname, "color": "0052cc", "description": ""})

    # Comments
    comment_obj = fields.get("comment") or {}
    comment_list = projection._as_list(
        comment_obj.get("comments") if isinstance(comment_obj, dict) else []
    )
    partial_sections: list[str] = []
    total_comments = (
        projection._int_or_zero(comment_obj.get("total"))
        if isinstance(comment_obj, dict)
        else len(comment_list)
    )
    if total_comments > len(comment_list):
        projection._mark_partial(partial_sections, "comments")

    comments = []
    for c in comment_list[:_JIRA_MAX_COMMENTS]:
        c_author = projection._as_dict(c.get("author"))
        c_body_raw = c.get("body")
        if isinstance(c_body_raw, dict):
            c_body = adf._adf_to_markdown(c_body_raw).strip()
        elif isinstance(c_body_raw, str):
            c_body = c_body_raw
        else:
            c_body = ""
        comments.append(
            {
                "id": str(c.get("id") or ""),
                "author": str(c_author.get("displayName") or c_author.get("name") or ""),
                "body": c_body,
                "createdAt": str(c.get("created") or ""),
                "url": "",  # Jira comments have no standalone permalink
            }
        )

    # Fix versions -> the milestone slot. The contract holds exactly one, so a
    # ticket scheduled for several releases surfaces the pending one (falling
    # back to the first when all have shipped) and declares the rest partial
    # rather than dropping them silently.
    fix_versions = _jira_fix_versions(fields)
    chosen = _jira_pick_fix_version(fix_versions)
    milestone = _jira_fix_version_milestone(chosen) if chosen else None
    if len(fix_versions) > 1:
        projection._mark_partial(partial_sections, "fix versions")

    return {
        "provider": "jira",
        "url": ref.url,
        "number": ref.number,
        "title": str(fields.get("summary") or ""),
        "description": description,
        "state": state,
        "stateReason": state_reason,
        "author": author,
        "createdAt": str(fields.get("created") or ""),
        "updatedAt": str(fields.get("updated") or ""),
        "closedAt": str(fields.get("resolutiondate") or ""),
        "closedBy": "",  # Jira does not expose who resolved
        "labels": labels,
        "assignees": assignees,
        "milestone": milestone,  # Jira's Fix Version is its milestone equivalent
        "commentCount": total_comments,
        "locked": False,  # Jira has no issue locking concept
        "reactions": None,  # Jira has no reactions
        "comments": comments,
        "linkedChanges": _jira_linked_changes(fields, base_url),
        "partialSections": partial_sections,
    }
