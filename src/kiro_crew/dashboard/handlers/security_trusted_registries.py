"""Registry owner-trust grants: the ``/api/security/trusted-registries`` routes.

The operator's assertion that an app registry they added may clone with the
machine's git identity, written to the keystone ``registry_trust.json`` and keyed
by the registry's repository URL, and the snapshot the Security page renders.

Served through :mod:`kiro_crew.dashboard.handlers.security`, which callers and
tests import and patch; this module never imports it.
"""

from __future__ import annotations

import asyncio
import logging

from aiohttp import web

from kiro_crew.apps.registry import (
    _expire_cache_file,
    _external_registry_cache_identity,
    _external_registry_cache_path,
    _git_target_is_unsupported,
    _git_url_host,
    _is_supported_registry_transport,
    _pinned_registries,
    _same_git_target,
    _strip_git_target_userinfo,
)
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.dashboard.handlers.agents import _get_config_lock
from kiro_crew.dashboard.handlers.security_audit import ConfigCorruptError, _audit

logger = logging.getLogger("kiro_crew.dashboard.handlers.security")


# ──────────────────────────────────────────────────────────────────────────
# Registry owner-trust grants — Settings > Security opt-IN surface
# ──────────────────────────────────────────────────────────────────────────
# An app registry the operator adds by hand is read at the ``index`` tier: its
# rows are index-author content, so every app it lists clones credential-free
# (``anonymous_git_env``), and an app in a private repository shows no store art
# and cannot be installed. Only the build could lift a registry to ``owner`` —
# the tier that clones with the machine's git identity — because the row itself
# lives in the agent-writable ``config.json`` and a tier read from there would be
# whatever a prompt-injected shell last wrote (``registry._registry_trust_tier``).
#
# These endpoints are the operator's own way to make that assertion. A grant is
# written to the KEYSTONE ``registry_trust.json`` (``registry_trust_path``), on
# the same read+write floor as ``denied_commands.json``, and is keyed by the
# registry's credential-free REPOSITORY URL rather than its name: a rewritten
# config row stops matching and is ``index`` again, so the agent cannot redirect
# a standing grant at an index it controls. The runtime reader is
# ``registry._granted_owner_repos``; these three endpoints — snapshot, grant and
# revoke — are the operator surface.
#
# Blast radius, stated plainly: granting a registry lets ITS AUTHORS choose which
# of the operator's reachable private repositories get cloned — the same reach a
# build-pinned ``owner`` registry has. The dialog that reaches these endpoints
# says so, and every mutation is SEL-audited.


def _operator_registry_rows() -> list:
    """The operator's ``config.json`` registry rows (never the build-pinned ones)."""
    return list(KiroCrewConfig.load().registries)


def _matching_operator_registry(repo: str) -> object | None:
    """The configured operator row whose repository is *repo*, or None.

    Matched with the same predicate the runtime reader uses
    (``_same_git_target`` on the credential-free URL), so a grant this accepts is
    one ``_registry_trust_tier`` will honour, and a pinned registry's repository
    is refused by the caller before this is consulted.
    """
    for row in _operator_registry_rows():
        if _same_git_target(_strip_git_target_userinfo(row.repo), repo):
            return row
    return None


def _served_state_for_key(
    key: str, effective_by_key: dict, pinned_keys: set
) -> tuple[bool, object | None, str | None]:
    """Whether the merge serves the operator row with identity *key*, and the served row.

    The ONE served-state projection both :func:`build_trusted_registries_snapshot`
    (per row, over precomputed sets) and :func:`_registry_served_state` (the grant
    gate, which builds the sets first) call, so the snapshot and the gate can never
    disagree about whether a row is served. Given the effective-registry map keyed
    by casefolded identity and the build-pinned identity keys, returns
    ``(served, served_row, reason)``:

    * a key contested by a build-pinned registry is ``(False, None, "pinned_name")``
      — the effective row under that key is the BUILD's, not the operator's, and the
      pinned contest is the first thing ``_effective_registries`` resolves, so it is
      checked first here too;
    * a key present in the effective map is ``(True, <row>, None)``;
    * anything else is ``(False, None, None)``.

    The ``served_row`` the snapshot needs for its tier read is returned alongside,
    so the loop does not resolve the row a second time.
    """
    if key in pinned_keys:
        return False, None, "pinned_name"
    served_row = effective_by_key.get(key)
    if served_row is not None:
        return True, served_row, None
    return False, None, None


def _registry_served_state(repo: str) -> tuple[bool, str | None]:
    """Whether the merge SERVES the operator row for *repo*, and why not if it does not.

    Resolves the operator row against :func:`_effective_registries` exactly as
    :func:`build_trusted_registries_snapshot` does — same casefolded identity key,
    same build-pinned-name exclusion — by building the same sets and calling the
    shared :func:`_served_state_for_key`, so the grant gate and the snapshot agree
    on whether a row is served. Returns ``(served, reason)`` where ``reason`` is
    ``None`` when served, or ``"pinned_name"`` when the row's name is contested by
    a build-pinned registry. A row *repo* names no operator row for is reported
    served (the caller decides what an unknown repo means).
    """
    from kiro_crew.apps.registry import (
        _effective_registries,
        _pinned_registries,
        _registry_identity_key,
    )

    row = _matching_operator_registry(repo)
    if row is None:
        return True, None
    key = _registry_identity_key(getattr(row, "name", "") or getattr(row, "repo", ""))
    pinned_keys = {_registry_identity_key(reg.name or reg.repo) for reg in _pinned_registries()}
    try:
        effective = list(_effective_registries())
    except Exception:
        logger.debug(
            "could not load the effective registry list to resolve served state", exc_info=True
        )
        effective = []
    effective_by_key = {_registry_identity_key(reg.name or reg.repo): reg for reg in effective}
    served, _served_row, reason = _served_state_for_key(key, effective_by_key, pinned_keys)
    return served, reason


def build_trusted_registries_snapshot() -> dict:
    """The operator registries with the grant in force for each (blocking I/O).

    The snapshot answers the RUNTIME's question, not the raw config map's. Each
    operator row is projected against :func:`_effective_registries` — the list the
    pipeline actually serves — because two cases the raw map cannot see decide
    whether a grant does anything. A name contested with a build-pinned row is
    dropped by the merge and served by neither: ``served: false`` with
    ``not_served_reason: "pinned_name"``, and ``trusted`` is ``False`` even when
    the operator granted the row.

    ``trusted`` is then the tier the install path itself computes:
    ``_registry_trust_tier_of`` on the effective row equals ``_TRUST_OWNER``. That
    also folds in the plaintext-transport refusal a hand-edited grant cannot
    escape, so there is one source of truth for the badge and the credential
    decision.

    Each row also carries ``granted``: whether a stored grant names its repository,
    read straight off the keystone record and INDEPENDENT of ``served``. A row can
    be granted and then dropped (a build-pinned registry takes its name); its grant
    is dormant and re-arms when the contest resolves, so the panel offers Revoke
    on any granted row while Grant is offered only on a served, ungranted row. A corrupt keystone reads as no grants, so every
    row is ``granted: false`` until it is fixed.

    A stored grant whose repository matches NO config row is also listed, as a
    synthetic row with ``served: false`` / ``not_served_reason: "not_configured"``,
    ``granted: true`` and ``trusted: false``. Removing a registry (in the editor or
    by a direct ``config.json`` edit) leaves its grant in place; without this the
    grant is invisible and would re-arm if the row ever returned, so it is
    surfaced with a Revoke.

    When ``registry_trust.json`` is CORRUPT (bad JSON, unknown version, wrong
    ``owner_trusted`` shape — the alias case included, since the strict read raises
    for it too), the snapshot carries top-level ``corrupt: true`` and
    ``corrupt_detail`` and every row is ``trusted: false``: the runtime already
    fails closed on a corrupt store, and the flag lets the Security page show a
    notice instead of a healthy-looking all-untrusted list. No product writer
    produces a corrupt file, so the remedy is the operator's own: fix or delete it.

    Imported from the ``kiro_crew.apps.registry`` facade at call time to keep the
    ``apps/``-imports-``dashboard``-only-at-call-time layering (the module-level
    imports above already follow it).
    """
    from kiro_crew.apps.registry import (
        _TRUST_OWNER,
        _effective_registries,
        _pinned_registries,
        _registry_identity_key,
        _registry_trust_tier_of,
    )
    from kiro_crew.apps.registry_trust import (
        RegistryTrustCorruptError,
        read_registry_trust_strict,
    )
    from kiro_crew.config.loader import registry_trust_path

    # Read the keystone strictly FIRST, so a corrupt trust file is visible on the
    # very page that fixes it. A corrupt store degrades every row to untrusted
    # (the runtime reader `_granted_owner_repos` already fails closed on it), but
    # without this flag the Security page would show a healthy-looking all-untrusted
    # list and send the operator to a surface that looks fine. `corrupt`/`corrupt_detail`
    # let the panel render a notice.
    corrupt = False
    corrupt_detail = ""
    granted_repos: list[str] = []
    try:
        record = read_registry_trust_strict()
        granted_repos = [r for r in (record.get("owner_trusted") or []) if isinstance(r, str) and r]
    except RegistryTrustCorruptError as exc:
        corrupt = True
        corrupt_detail = str(exc)

    try:
        effective = list(_effective_registries())
    except Exception:
        logger.debug("could not load the effective registry list for the snapshot", exc_info=True)
        effective = []
    effective_by_key = {_registry_identity_key(reg.name or reg.repo): reg for reg in effective}
    pinned_keys = {_registry_identity_key(reg.name or reg.repo) for reg in _pinned_registries()}

    rows = []
    for row in _operator_registry_rows():
        public = _strip_git_target_userinfo(row.repo)
        key = _registry_identity_key(row.name or row.repo)
        # ONE served-state projection, shared with the grant gate
        # (`_registry_served_state`): a pinned registry wins a name contest, so the
        # effective row under a contested key is the BUILD's, not this operator row
        # (`pinned_name`).
        served, served_row, not_served_reason = _served_state_for_key(
            key, effective_by_key, pinned_keys
        )
        if served:
            # Two config rows can share one identity key; read the tier off THIS
            # row's own effective copy, not whichever one the key map kept.
            served_row = next(
                (
                    reg
                    for reg in effective
                    if _registry_identity_key(reg.name or reg.repo) == key
                    and (reg.repo, reg.branch) == (row.repo, row.branch)
                ),
                served_row,
            )
        # A corrupt keystone confers no trust at all — every row is untrusted until
        # it is fixed — so short-circuit the tier read, which would itself fail
        # closed but for a different reason.
        trusted = (
            (not corrupt)
            and served_row is not None
            and _registry_trust_tier_of(served_row) == _TRUST_OWNER
        )
        # Whether a stored grant names this row's repository, independent of
        # whether the merge SERVES the row. A row can be granted and then dropped
        # by the merge (an operator adds a same-name registry): its grant is
        # dormant and re-arms when the collision resolves, so the panel must still
        # offer Revoke for it. A corrupt keystone has no readable grants, so every
        # row reads ungranted until it is fixed.
        granted = (not corrupt) and any(_same_git_target(public, g) for g in granted_repos)
        entry = {
            "name": row.name,
            "repo": public,
            "branch": row.branch,
            "host": _git_url_host(public),
            "served": served,
            # A stored grant exists for this repository. Independent of `served`:
            # the panel renders Revoke whenever a row is granted (served or not),
            # so a dropped-but-granted row can still be cleared.
            "granted": granted,
            # The badge answers the RUNTIME's question: a row the merge drops is
            # not being listed, and a hand-edited grant on a plaintext transport
            # sits in the keystone but the tier refuses it — neither badges.
            "trusted": trusted,
        }
        if not served:
            # A dropped row lost to a build-pinned row of the same name.
            entry["not_served_reason"] = not_served_reason
        rows.append(entry)

    # Orphan grants: a stored grant whose repository matches NO config row. The
    # revoke sweep runs only in the registries PUT, so a row deleted by a direct
    # ``config.json`` edit keeps its keystone grant — and the loop above iterates
    # config rows only, so nothing would list it and it would re-arm the moment the
    # row returns. Append one row per such grant so the panel can Revoke it (the
    # panel renders Revoke for any ``granted`` row). ``served: false`` /
    # ``not_served_reason: "not_configured"`` distinguishes it from a dropped-config
    # row; it is never ``trusted`` (its repo is served by no effective row) and
    # never carries a ``branch``. A corrupt keystone has no readable grants, so
    # ``granted_repos`` is empty and no orphan rows appear until it is fixed.
    for raw_grant in granted_repos:
        # A hand-edited grant may carry userinfo (a token pasted with the URL);
        # never echo it. The runtime ignores such a grant anyway.
        grant = _strip_git_target_userinfo(raw_grant)
        if any(_same_git_target(grant, r["repo"]) for r in rows):
            continue
        host = _git_url_host(grant)
        rows.append(
            {
                # No config row names this grant, so there is no operator-chosen
                # name to show. Use the credential-free repository URL itself as
                # the display name (it is the host+path the operator would
                # recognise), or "" when the host cannot be parsed.
                "name": grant if host else "",
                "repo": grant,
                "branch": "",
                "host": host,
                "served": False,
                "not_served_reason": "not_configured",
                "granted": True,
                "trusted": False,
            }
        )

    snapshot: dict = {"registries": rows}
    if corrupt:
        snapshot["corrupt"] = True
        snapshot["corrupt_detail"] = corrupt_detail
        # The fix is the operator's own (delete the file), so name exactly which
        # file: the data home can be relocated (``KIROCREW_HOME``).
        snapshot["corrupt_path"] = str(registry_trust_path())
    return snapshot


async def _trusted_registries_response() -> web.Response:
    snapshot = await asyncio.get_running_loop().run_in_executor(
        None, build_trusted_registries_snapshot
    )
    return web.json_response(snapshot)


def _expire_registry_index_cache(row: object) -> None:
    """Make the next store listing re-fetch *row*'s index after a trust change.

    The tier itself is consumed on the install path (``_owner_tier_confirmed``)
    and by the store-art prewarm that runs when an index is FRESHLY fetched for an
    ``owner``-tier registry -- the index cache carries no tier and, left alone,
    would keep serving the pre-grant listing until it expires on its own. Expiring
    (not deleting) forces that fresh fetch on the next listing while keeping the
    rows as a stale fallback if the re-fetch fails. On a build whose listing has no
    such prewarm the grant changes only the install path; the store rows are
    unchanged either way.
    """
    try:
        _expire_cache_file(_external_registry_cache_path(_external_registry_cache_identity(row)))
    except Exception:
        logger.debug(
            "could not expire the registry index cache after a trust change", exc_info=True
        )


async def _trusted_registry_repo_from_body(request: web.Request, op: str) -> str | web.Response:
    """The ``repo`` a grant/revoke names, or the 400 that refuses the body."""
    try:
        body = await request.json()
    except Exception:
        _audit(request, operation=op, outcome="denied", resources="invalid_json")
        return web.json_response(
            {"error": "body must be a JSON object naming a repo", "code": "invalid_body"},
            status=400,
        )
    repo = body.get("repo") if isinstance(body, dict) else None
    if not isinstance(repo, str) or not repo.strip():
        _audit(request, operation=op, outcome="denied", resources="missing_repo")
        return web.json_response(
            {"error": "repo must be a non-empty string", "code": "invalid_repo"}, status=400
        )
    repo = repo.strip()
    if _git_target_is_unsupported(repo) or _strip_git_target_userinfo(repo) != repo:
        _audit(request, operation=op, outcome="denied", resources="bad_repo")
        return web.json_response(
            {
                "error": "repo must be the credential-free repository URL as listed",
                "code": "invalid_repo",
            },
            status=400,
        )
    return repo


async def api_trusted_registries_list(request: web.Request) -> web.Response:
    """GET /api/security/trusted-registries — operator registries + grants (read).

    Open to any authenticated dashboard caller, like every other read in this
    module (``test_r6_owner_gate_regression`` pins that the owner gate covers
    write verbs, never reads): the snapshot carries the same registry rows
    ``GET /api/apps/registries`` already returns, plus one boolean per row.
    """
    return await _trusted_registries_response()


async def api_trusted_registry_grant(request: web.Request) -> web.Response:
    """POST /api/security/trusted-registries — grant ``owner`` trust to one registry.

    Body ``{"repo": <credential-free url as listed>}``. The repo must be one of the
    operator's configured rows: the grant is a decision about a registry the
    operator can see in Settings, never about an arbitrary URL, and a build-pinned
    registry is refused because its tier is the build's to state. Idempotent.
    """
    op = "security.trusted_registries.grant"
    from kiro_crew.dashboard.handlers._shared import require_owner_dashboard_request

    denied = await require_owner_dashboard_request(request, op)
    if denied is not None:
        return denied
    repo = await _trusted_registry_repo_from_body(request, op)
    if isinstance(repo, web.Response):
        return repo
    # A plaintext ``http://``, ``git://`` or ``ext::`` repo must never carry an
    # ``owner`` grant: the tier this grant confers clones with the machine's git
    # identity, and an unauthenticated transport lets anything on the network path
    # substitute the app code that identity then fetches. This is the same gate the
    # registries PUT already applies (``_is_supported_registry_transport``), so a
    # repo that could not be added as a registry cannot be granted trust either.
    if not _is_supported_registry_transport(repo):
        _audit(request, operation=op, outcome="denied", resources="unsupported_transport")
        return web.json_response(
            {
                "error": "repo must be fetched over https or ssh, never a plaintext transport",
                "code": "unsupported_transport",
            },
            status=400,
        )

    def _validate_and_grant() -> str:
        """Under the shared lock: re-read config, then write the grant.

        Returns an outcome code the caller maps to a response. The config read
        (pinned check + matching operator row) and the keystone read-modify-write
        run in this one locked, executor-backed step, so a registries PUT cannot
        remove the row between validation and write and leave an orphan grant the
        snapshot never lists. Blocking I/O only — no event-loop file reads.
        """
        from kiro_crew.apps.registry_trust import (
            RegistryTrustCorruptError,
            _write_registry_trust_record,
            add_owner_grant,
            read_registry_trust_strict,
        )

        if any(
            _same_git_target(_strip_git_target_userinfo(p.repo), repo) for p in _pinned_registries()
        ):
            return "pinned_registry"
        if _matching_operator_registry(repo) is None:
            return "unknown_registry"
        # A configured row can still be dropped by the merge — its name contested
        # by a build-pinned registry, or an identity-key collision with another
        # config row — and a dropped row is served by NEITHER claimant. Recording a
        # grant on it would be invisible (the snapshot reports it not-trusted, the
        # button stays Grant, Revoke is never offered) yet would arm the moment the
        # collision is resolved. Refuse it here, where the snapshot resolves served
        # state, so the operator fixes the registries editor first.
        served, reason = _registry_served_state(repo)
        if not served:
            return f"not_served:{reason}"
        try:
            data = read_registry_trust_strict()
        except RegistryTrustCorruptError as exc:
            raise ConfigCorruptError(str(exc)) from exc
        # The version-1 record stores ``owner_trusted`` as a deduped LIST of repo
        # URLs; the grant is the value itself, since SEL timestamps each grant and
        # no reader consumes a per-repo body. The revoke handler mirrors this exact shape —
        # strict read, mutate the record, publish through the shared writer.
        add_owner_grant(data, repo)
        _write_registry_trust_record(data)
        return "success"

    loop = asyncio.get_running_loop()
    async with _get_config_lock():
        try:
            outcome = await loop.run_in_executor(None, _validate_and_grant)
        except ConfigCorruptError as exc:
            _audit(request, operation=op, outcome="error", resources="corrupt")
            return web.json_response(
                {
                    "error": (
                        f"{exc} — fix or delete registry_trust.json in the Kiro Crew data folder, "
                        "then grant again"
                    ),
                    "code": "corrupt",
                },
                status=500,
            )
    if outcome == "pinned_registry":
        _audit(request, operation=op, outcome="denied", resources="pinned_registry")
        return web.json_response(
            {
                "error": "this registry's trust is stated by the build and cannot be changed here",
                "code": "pinned_registry",
            },
            status=400,
        )
    if outcome == "unknown_registry":
        _audit(request, operation=op, outcome="denied", resources="unknown_registry")
        return web.json_response(
            {"error": "repo is not one of the configured registries", "code": "unknown_registry"},
            status=400,
        )
    if outcome.startswith("not_served:"):
        reason = outcome.split(":", 1)[1]
        _audit(request, operation=op, outcome="denied", resources=f"not_served={reason}")
        return web.json_response(
            {
                "error": (
                    "this registry is not listed: " f"{reason} — fix the registries editor first"
                ),
                "code": "not_served",
                "reason": reason,
            },
            status=400,
        )
    row = await loop.run_in_executor(None, _matching_operator_registry, repo)
    if row is not None:
        await loop.run_in_executor(None, _expire_registry_index_cache, row)
    _audit(request, operation=op, outcome="success", resources=f"repo={repo}")
    return await _trusted_registries_response()


async def api_trusted_registry_revoke(request: web.Request) -> web.Response:
    """POST /api/security/trusted-registries/revoke — drop a grant. Idempotent.

    Body ``{"repo": ...}``. A repo absent from config is still accepted, so a
    grant whose row the operator already deleted can be cleaned up.
    """
    op = "security.trusted_registries.revoke"
    from kiro_crew.dashboard.handlers._shared import require_owner_dashboard_request

    denied = await require_owner_dashboard_request(request, op)
    if denied is not None:
        return denied
    repo = await _trusted_registry_repo_from_body(request, op)
    if isinstance(repo, web.Response):
        return repo

    def _read_modify_write() -> None:
        """Under the shared lock: strict read, drop the grant, publish the record.

        The same concrete read-modify-write the grant handler runs — strict read,
        mutate the record with :func:`drop_owner_grants`, publish through the
        shared :func:`_write_registry_trust_record` — rather than a callback. Runs
        as one locked, executor-backed step, so a registries PUT cannot interleave
        between the read and the write. Blocking I/O only.
        """
        from kiro_crew.apps.registry_trust import (
            RegistryTrustCorruptError,
            _write_registry_trust_record,
            drop_owner_grants,
            read_registry_trust_strict,
        )

        try:
            data = read_registry_trust_strict()
        except RegistryTrustCorruptError as exc:
            raise ConfigCorruptError(str(exc)) from exc
        drop_owner_grants(data, repo)
        _write_registry_trust_record(data)

    loop = asyncio.get_running_loop()
    async with _get_config_lock():
        try:
            await loop.run_in_executor(None, _read_modify_write)
        except ConfigCorruptError as exc:
            _audit(request, operation=op, outcome="error", resources="corrupt")
            return web.json_response(
                {
                    "error": f"{exc} — fix or delete registry_trust.json in the Kiro Crew data folder",
                    "code": "corrupt",
                },
                status=500,
            )
    row = await loop.run_in_executor(None, _matching_operator_registry, repo)
    if row is not None:
        await loop.run_in_executor(None, _expire_registry_index_cache, row)
    _audit(request, operation=op, outcome="success", resources=f"repo={repo}")
    return await _trusted_registries_response()
