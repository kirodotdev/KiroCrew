"""Per-app third-party trust grants: the ``/api/security/trusted-apps`` routes.

The operator's opt-in to run a named third-party app's code (``agent.apps_trusted``
and its local and repository bindings) and the blanket ``apps_allow_third_party``
flag, the snapshot the Security page renders from them, and stopping the apps
that run on a grant once it is withdrawn.

Served through :mod:`kiro_crew.dashboard.handlers.security`, which callers and
tests import and patch; this module never imports it.
"""

from __future__ import annotations

import asyncio
import json
import logging

from aiohttp import web

from kiro_crew.apps.execution import (
    APP_NAME_RE,
    _repository_grant_denied_for_binding,
    builtin_app_names,
    trusted_app_names,
)
from kiro_crew.apps.manager import disable_app, get_app, list_apps
from kiro_crew.apps.official_catalog import CatalogUnavailable
from kiro_crew.apps.registry import (
    _entry_git_url,
    _git_target_is_unsupported,
    _normalize_git_target,
    get_registry_app,
    resolve_installed_trust_repository,
)
from kiro_crew.apps.routes import app_lifecycle_lock
from kiro_crew.apps.teardown import teardown_app_runtime
from kiro_crew.config.loader import (
    ConfigReadError,
    KiroCrewConfig,
    _invalidate_config_cache,
    config_local_path,
    config_path,
    read_config_text,
    update_config_locked,
)
from kiro_crew.dashboard.handlers.agents import _get_config_lock
from kiro_crew.dashboard.handlers.security_audit import ConfigCorruptError, _audit, _run_off_loop

logger = logging.getLogger("kiro_crew.dashboard.handlers.security")


# ──────────────────────────────────────────────────────────────────────────
# Per-app third-party trust grants — Settings > Security opt-IN surface
# ──────────────────────────────────────────────────────────────────────────
# The narrow counterpart to ``agent.apps_allow_third_party``: instead of
# admitting every third-party app's code at once, the operator grants ONE named
# app at a time (``agent.apps_trusted``). ``apps.execution.trusted_app_names``
# is the enforcement reader; these 4 endpoints are the write surface.
#
# Unlike the denied-commands opt-out in the facade, grants live in ``config.json`` rather
# than the keystone file. That is a deliberate call by the operator, but NOT
# because an agent needs to write them: measured, an agent can reach the grant
# through neither leg — ``is_sensitive_write_path(<home>/config.json)`` is True
# so its file-edit tool is blocked, and these routes sit in neither
# ``_STRICT_INTERNAL_API_PATHS`` nor ``_MIXED_INTERNAL_API_PATHS``, so
# ``token_auth`` default-denies an agent with ``Token required``. The reason is
# co-location: the blanket ``apps_allow_third_party`` this narrows already lives
# here, and splitting a control across two stores invites the two halves to
# disagree.
#
# Be precise about the blast radius: a grant is scoped to ONE named app as a
# DECISION, but the thing granted is not itself a bounded privilege — a trusted
# app's Python loads in-process in the gateway (``module_loader``), so it inherits
# the gateway's trust domain, including write access to the keystone files the
# Security handlers' doctrine keeps away from the agent. Adding per-app grants does not
# widen that (``apps_allow_third_party`` already reached it); it narrows WHO gets
# there. Every mutation is SEL-audited.


def _trusted_list(agent: object) -> list[str]:
    """Current ``agent.apps_trusted`` as a list of non-empty strings.

    Defensive against a hand-edited ``config.json`` (a non-list value, or a list
    holding ``{}``/``5``/``""``): junk is dropped rather than raising, so a
    malformed file yields a clean mutation instead of a 500.
    """
    raw = getattr(agent, "apps_trusted", [])
    if not isinstance(raw, list):
        return []
    return [a for a in raw if isinstance(a, str) and a]


def _trusted_list_raw(agent_raw: dict) -> list[str]:
    """``apps_trusted`` from the RAW base ``agent`` dict, junk dropped.

    The raw-dict twin of :func:`_trusted_list`. Writers use this one because they
    edit the base file: reading the merged model and writing the base would drop
    base entries whenever ``config.local.json`` replaces the list.
    """
    raw = agent_raw.get("apps_trusted", [])
    if not isinstance(raw, list):
        return []
    return [a for a in raw if isinstance(a, str) and a]


def _trusted_local_list_raw(agent_raw: dict) -> list[str]:
    """Explicit local grant markers from the raw base config, junk dropped."""
    raw = agent_raw.get("apps_trusted_local", [])
    if not isinstance(raw, list):
        return []
    return [a for a in raw if isinstance(a, str) and a]


def _trusted_repositories_raw(agent_raw: dict) -> dict[str, str]:
    """Credential-free repository bindings from the raw base config."""
    raw = agent_raw.get("apps_trusted_repositories", {})
    if not isinstance(raw, dict):
        return {}
    repositories: dict[str, str] = {}
    for name, repository in raw.items():
        if not isinstance(name, str) or not name or not isinstance(repository, str):
            continue
        # Never turn an unsupported legacy identity into a valid base-URL grant
        # as a side effect of mutating an unrelated app's settings.
        if _git_target_is_unsupported(repository):
            continue
        normalized = _normalize_git_target(repository)
        if normalized:
            repositories[name] = normalized
    return repositories


def build_trusted_apps_snapshot() -> dict:
    """Compute the snapshot returned by every trusted-apps endpoint.

    ``apps`` is the de-duplicated, sorted list of grants the gate ACTUALLY
    enforces; ``ineffective`` holds stored entries it ignores. The split matters
    because ``trusted_app_names`` (the enforcement reader) requires the app-name
    charset while a hand-edited ``config.json`` can hold anything: without the
    split, ``LD-App``, ``ld-app `` (trailing space), a fullwidth homoglyph, ``..``
    or ``*`` would render as granted while admitting nothing. That fails safe —
    it over-reports trust — but a security panel that shows a grant which does
    not exist is a lie in the other direction, and the user cannot tell why their
    app is still blocked.

    ``allowAll`` mirrors the blanket ``agent.apps_allow_third_party`` with the
    same strict identity check the gate uses, so a hand-typed ``"true"`` renders
    false here exactly as it denies there.

    Synchronous — reads ``config.json``. Async callers MUST offload it (see
    :func:`build_trusted_apps_snapshot_async`).
    """
    agent = KiroCrewConfig.load().agent
    stored = set(_trusted_list(agent))
    syntactically_effective = trusted_app_names()
    # A legacy name-only grant is not an execution grant for code known to
    # come from a repository. Classify it with the same resolver used by runtime
    # admission so the Security panel never displays a fail-closed grant as fully
    # trusted. The name stays in ``ineffective`` and can still be revoked or
    # replaced through the existing endpoints.
    raw_repositories = getattr(agent, "apps_trusted_repositories", {})
    if not isinstance(raw_repositories, dict):
        raw_repositories = {}
    raw_local = getattr(agent, "apps_trusted_local", [])
    local_bindings = set(raw_local) if isinstance(raw_local, list) else set()
    effective = set()
    for name in syntactically_effective:
        binding = raw_repositories.get(name, "")
        binding = binding.strip() if isinstance(binding, str) else ""
        repository: str | None = None
        # A repository grant is intentionally usable before the app is installed.
        # In that state there is no installed provenance to compare, so resolve the
        # same authoritative row install will consume. A rebind or catalog failure
        # moves the name to ``ineffective`` without reflecting either coordinate.
        if binding and get_app(name) is None:
            try:
                entry = get_registry_app(name)
                entry_url = _entry_git_url(entry) if entry else ""
                repository = (
                    ""
                    if _git_target_is_unsupported(entry_url)
                    else _normalize_git_target(entry_url)
                )
            except CatalogUnavailable:
                repository = ""
        if (
            _repository_grant_denied_for_binding(
                name,
                binding=binding,
                local_binding=name in local_bindings,
                repository=repository,
            )
            is None
        ):
            effective.add(name)
    return {
        "apps": sorted(stored & effective),
        "ineffective": sorted(stored - effective),
        "allowAll": getattr(agent, "apps_allow_third_party", False) is True,
    }


async def build_trusted_apps_snapshot_async() -> dict:
    """`build_trusted_apps_snapshot` off the event loop (config.json read)."""
    return await _run_off_loop(build_trusted_apps_snapshot)


async def _trusted_apps_response(extra: dict | None = None) -> web.Response:
    """Return the refreshed snapshot (200), optionally merged with *extra* fields."""
    snapshot = await build_trusted_apps_snapshot_async()
    if extra:
        snapshot.update(extra)
    return web.json_response(snapshot)


class TrustSettingOverlayOwned(Exception):
    """A trust setting lives in ``config.local.json``, so writing ``config.json`` is a no-op.

    ``KiroCrewConfig`` deep-merges the user-owned ``config.local.json`` overlay OVER
    ``config.json`` on every load, and ``save()`` deliberately STRIPS overlay-owned
    values from what it writes. So if ``apps_trusted`` or ``apps_allow_third_party``
    is set in the overlay, a mutation of ``config.json`` is doubly ineffective: the
    write is stripped on the way out and the overlay is re-applied on the way in.

    That makes the honest failure mode a refusal. The dangerous alternative is what
    this code did before: return 200 while a revoked grant keeps admitting the app's
    code on the next load, which is the third variant of "revocation that revokes
    nothing" this feature has had to close. Editing the overlay on the user's behalf
    is not an option either — that file is explicitly user-owned and never written by
    Kiro Crew — so the caller is told which key and which file to edit.
    """

    def __init__(self, keys: list[str]) -> None:
        self.keys = keys
        super().__init__(
            "these trust settings are set in config.local.json, which overrides "
            f"config.json: {', '.join(keys)}"
        )


_TRUST_SETTING_NAMES = (
    "apps_trusted",
    "apps_trusted_local",
    "apps_trusted_repositories",
    "apps_allow_third_party",
)


def _overlay_owned_trust_settings() -> list[str]:
    """Return the trust settings ``config.local.json`` currently owns (blocking I/O)."""
    path = config_local_path()
    if not path.is_file():
        return []
    try:
        raw = json.loads(read_config_text(path))
    except (OSError, UnicodeError, json.JSONDecodeError):
        # Unreadable overlay: the loader ignores it, so config.json IS effective.
        # Refusing here would block a legitimate revoke on an unrelated broken file.
        return []
    agent = raw.get("agent") if isinstance(raw, dict) else None
    if not isinstance(agent, dict):
        return []
    return [name for name in _TRUST_SETTING_NAMES if name in agent]


async def _preflight_agent_config_mutable() -> None:
    """Raise if an ``agent`` trust mutation could not succeed, WITHOUT writing.

    Used by the falling-edge allow-all path, which stops app code before it
    persists the flag: without this, a mutation destined to fail (corrupt
    ``config.json``, overlay-owned setting) would tear the user's apps down and
    then refuse, leaving them stopped for nothing. Raising the same two exceptions
    the real mutation raises keeps the handler's error branches identical.

    This is a pre-flight, not a guarantee — the real check still runs inside the
    config lock during the write. It only ensures the common, already-detectable
    failures are reported before anything is stopped.
    """

    def _check() -> None:
        path = config_path()
        if path.is_file():
            try:
                existing = json.loads(read_config_text(path))
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                raise ConfigCorruptError(f"{path} is unreadable: {exc}") from exc
            if not isinstance(existing, dict):
                raise ConfigCorruptError(f"{path} is not a JSON object")
        owned = _overlay_owned_trust_settings()
        if owned:
            raise TrustSettingOverlayOwned(owned)

    await asyncio.get_running_loop().run_in_executor(None, _check)


async def _mutate_agent_config(mutate) -> None:
    """Read-modify-write ``config.json``'s ``agent`` section off the event loop.

    ``mutate(agent_raw) -> None`` edits the RAW base ``agent`` dict in place.
    Runs under the SHARED config lock (the same lock every other config writer
    takes) so a concurrent agent/settings write cannot clobber the grant list,
    and the blocking read→write is offloaded to the default thread executor so a
    slow FS never stalls the gateway loop. The config cache is invalidated after
    the write, so the very next ``trusted_app_names()`` read sees the change —
    the grant enforces without a restart.

    **Why a targeted raw edit and not ``KiroCrewConfig.load()`` → ``save()``.**
    ``save()`` deliberately strips every value ``config.local.json`` also defines
    so overlay settings do not leak into the base file. Routing a trust mutation
    through it therefore rewrites the WHOLE base document minus all overlay-owned
    keys — so a user with base ``model: sonnet`` and overlay ``model: opus`` loses
    the base ``sonnet`` permanently the first time they grant an app trust. The
    effective config still reads ``opus`` from the overlay, so nothing looks
    wrong until the overlay is removed and the setting is simply gone. Editing
    only the key we own keeps the blast radius to that key. (The pre-existing
    overlay refusal below is a different guard, and still required: when the
    overlay owns a TRUST key, no base write can make the change effective.)

    Raises :class:`ConfigCorruptError` when ``config.json`` exists but does not
    parse as a JSON object. ``KiroCrewConfig.load()`` degrades a corrupt file to
    DEFAULTS, so a blind load→save here would write those defaults over the
    user's file and silently erase everything it holds — inline channel
    credentials, ``registries``, ``sandbox``/``jail``/``approval_mode``, the
    model. That is the same hazard ``_read_denied_strict`` guards for the
    denied-commands keystone file, and the reason its comment forbids the
    read-as-empty-and-write-back pattern. The pre-flight parse is inside the
    lock so a concurrent writer cannot slip a corrupt file in between the check
    and the load.
    """

    def _read_modify_write() -> None:
        path = config_path()

        def _apply(raw: dict) -> dict:
            # Runs inside ``update_config_locked``'s hold on the
            # ``<config>.json.lock`` sidecar, so neither the overlay check nor
            # the grant edit can be separated from the write by ANY other
            # writer -- including the CLI and other processes, which the
            # surrounding asyncio lock cannot reach.
            owned = _overlay_owned_trust_settings()
            if owned:
                raise TrustSettingOverlayOwned(owned)
            agent_raw = raw.get("agent")
            if not isinstance(agent_raw, dict):
                agent_raw = {}
                raw["agent"] = agent_raw
            mutate(agent_raw)
            return raw

        try:
            update_config_locked(path, mutate=_apply, stamp_meta=False)
        except ConfigReadError as exc:
            # Same refusal as before, reported through the handlers' own error
            # type so callers keep turning it into a coded 409. The locked read
            # fails closed by default (``on_corrupt="fail"``), so the existing
            # file is never replaced with defaults -- an absent file is still a
            # genuine empty starting point and proceeds.
            raise ConfigCorruptError(f"{path} is unreadable: {exc}") from exc
        # save() would have done this; a raw write must do it explicitly or the
        # grant does not take effect until a restart — and a REVOKE that does not
        # take effect is the exact failure this feature exists to prevent.
        _invalidate_config_cache()

    async with _get_config_lock():
        await asyncio.get_running_loop().run_in_executor(None, _read_modify_write)


async def api_trusted_apps_list(request: web.Request) -> web.Response:
    """GET /api/security/trusted-apps — grants + blanket flag (no audit; read)."""
    return await _trusted_apps_response()


async def api_trusted_app_grant(request: web.Request) -> web.Response:
    """POST /api/security/trusted-apps/{name} — grant one app execution trust.

    Idempotent: granting an already-granted app is a 200 with no duplicate entry.

    The name must be well-formed AND name a REAL app — one that is either already
    installed or listed in a registry the operator trusts. Both are required
    because the grant is consulted by NAME: an arbitrary name would sit in config
    and silently admit whatever app later claims it.

    Accepting a not-yet-installed registry name is what makes the feature usable
    at all. ``install_from_registry`` checks the execution gate BEFORE cloning
    (clone, build and ``onInstall`` are themselves third-party code), so an
    install-only precondition would deadlock: no grant without an install, no
    install without a grant, and the operator's only way through would be the
    blanket ``apps_allow_third_party`` this endpoint exists to avoid. A registry
    name is safe to accept because the index is curated or operator-configured,
    not caller-supplied.
    """
    op = "security.trusted_apps.grant"

    # Body-scope import, like the sibling gates in this package
    # (``connections.py``, ``mcp_apps.py``, ``files.py``): ``source_providers``
    # reaches back into sibling handler modules, so importing the helper at
    # module scope from here would close a cycle.
    from kiro_crew.dashboard.handlers._shared import require_owner_dashboard_request

    denied = await require_owner_dashboard_request(request, op)
    if denied is not None:
        return denied
    name = request.match_info["name"]

    if not APP_NAME_RE.fullmatch(name):
        _audit(request, operation=op, outcome="denied", resources=f"{name}=invalid_name")
        return web.json_response(
            {
                "error": "app name must match [a-z0-9][a-z0-9_-]*",
                "code": "invalid_app_name",
            },
            status=400,
        )

    # The repository is a consent proof, not authority: the server resolves the
    # current target independently below. An empty body keeps local installed
    # apps (which have no repository binding) compatible, while a malformed
    # proof is refused before any config mutation.
    body: object = {}
    if request.can_read_body:
        try:
            body = await request.json()
        except Exception:
            _audit(request, operation=op, outcome="denied", resources=f"{name}=invalid_json")
            return web.json_response(
                {
                    "error": "repository consent proof must be valid JSON",
                    "code": "invalid_repository_consent",
                },
                status=400,
            )
    if not isinstance(body, dict):
        body = {}
    consent_raw = body.get("repository")
    if consent_raw is not None and not isinstance(consent_raw, str):
        _audit(request, operation=op, outcome="denied", resources=f"{name}=bad_repository")
        return web.json_response(
            {
                "error": "repository consent proof must be a string",
                "code": "invalid_repository_consent",
            },
            status=400,
        )
    if _git_target_is_unsupported(consent_raw or ""):
        _audit(request, operation=op, outcome="denied", resources=f"{name}=bad_repository")
        return web.json_response(
            {
                "error": (
                    "repository consent proof contains an unsupported query or fragment "
                    "or an ambiguous Git transport identity"
                ),
                "code": "invalid_repository_consent",
            },
            status=400,
        )
    consent_repository = _normalize_git_target(consent_raw or "")

    # Validation and the write must be ONE critical section, held against the
    # same per-app lock every other lifecycle transition takes. Uninstall's
    # grant-removal precondition and this handler's existence check are both
    # read-then-act, so unserialized they interleave: the uninstall sees no grant
    # to drop and proceeds to tear the app down, and this grant lands *after* the
    # app is gone. That leaves a grant on a name nothing owns — and a name-keyed
    # grant is precisely what lets a LATER third-party app claiming that name run
    # code with no consent prompt, the same hazard the builtin check below exists
    # to prevent. Revoke already serializes this way (`api_trusted_app_revoke`),
    # so the lock order here — app lock, then the config lock inside
    # `_mutate_agent_config` — matches and introduces no new inversion.
    #
    # Safe to hold across these awaits: this handler is only ever entered from
    # the HTTP route, never from inside `install_app`, so it cannot re-enter a
    # lock its own caller holds.
    async with app_lifecycle_lock(name):
        # Offloaded: both read from disk (installed.json + app.json; the registry
        # file and its cached external-index snapshots).
        def _known_app_repository() -> tuple[bool, str]:
            installed = get_app(name)
            if installed is not None:
                try:
                    return resolve_installed_trust_repository(installed)
                except CatalogUnavailable:
                    # A legacy registry install cannot safely degrade to a
                    # name-only grant when its current source is unavailable.
                    return False, ""
            try:
                entry = get_registry_app(name)
            except CatalogUnavailable:
                # Resolution was refused because the catalog could not be consulted.
                # Treat as not-known: this gates an execution grant, so declining
                # while the source cannot be confirmed is the safe direction.
                return False, ""
            if entry is None:
                return False, ""
            entry_url = _entry_git_url(entry)
            if _git_target_is_unsupported(entry_url):
                return False, ""
            return True, _normalize_git_target(entry_url)

        # Whether the app is INSTALLED right now, kept separate from "known". A
        # registry-only name is grantable on purpose (the install-consent flow grants
        # before the clone), so its absence from disk is expected and must not be
        # mistaken for the app vanishing. Only a name that WAS installed and is gone
        # after the write is the race below.
        was_installed = await _run_off_loop(lambda: get_app(name) is not None)

        known, repository = await _run_off_loop(_known_app_repository)
        if not known:
            _audit(request, operation=op, outcome="denied", resources=f"{name}=unknown")
            return web.json_response(
                {
                    "error": f"app {name!r} is neither installed nor in a known registry",
                    "code": "app_not_installed",
                },
                status=404,
            )

        # Bind the click to what the modal actually showed. The registry may
        # legitimately publish both ``repo`` (display/legacy alias) and
        # ``gitUrl`` (the effective clone URL); only the latter wins through
        # ``_entry_git_url`` above. A stale or compromised client cannot replace
        # that server-side result — it can only prove it displayed the same one.
        # Compare even when one side is empty: an app changing between a local
        # install and a repository-backed source is also a changed consent scope.
        if consent_repository != repository:
            reason = (
                "missing_repository"
                if repository and not consent_repository
                else "repository_changed"
            )
            _audit(request, operation=op, outcome="denied", resources=f"{name}={reason}")
            return web.json_response(
                {
                    "error": (
                        "repository consent proof is required; refresh the app listing and review it"
                        if reason == "missing_repository"
                        else "app repository changed since the consent dialog was opened; refresh and review it"
                    ),
                    "code": "app_trust_repository_mismatch",
                },
                status=409,
            )

        # A builtin needs no grant (shipped package code is exempt at the gate), and
        # storing one is actively harmful: the entry stays in config after the
        # builtin stops owning the slot, so a LATER third-party app that claims the
        # same name inherits a grant nobody made for it. The existence check above
        # cannot catch this on its own — builtins have an installed.json too, so the
        # name passes as "known" and the grant persists inertly until the takeover
        # makes it live.
        if name in await _run_off_loop(builtin_app_names):
            _audit(request, operation=op, outcome="denied", resources=f"{name}=builtin")
            return web.json_response(
                {
                    "error": f"app {name!r} is a built-in and does not need a trust grant",
                    "code": "app_is_builtin",
                },
                status=409,
            )

        def _mutate(agent_raw: dict) -> None:
            # Reads the BASE list, not the merged view: this write lands in the
            # base file, so the merged value (which an overlay may replace
            # wholesale) is the wrong thing to append to.
            current = _trusted_list_raw(agent_raw)
            if name not in current:
                current.append(name)
            agent_raw["apps_trusted"] = current
            repositories = _trusted_repositories_raw(agent_raw)
            local_bindings = _trusted_local_list_raw(agent_raw)
            if repository:
                repositories[name] = repository
                local_bindings = [a for a in local_bindings if a != name]
            else:
                # Re-granting a local app under a formerly registry-owned name
                # must not retain a stale binding from that prior occupant.
                repositories.pop(name, None)
                if name not in local_bindings:
                    local_bindings.append(name)
            agent_raw["apps_trusted_local"] = local_bindings
            agent_raw["apps_trusted_repositories"] = repositories

        try:
            await _mutate_agent_config(_mutate)
        except ConfigCorruptError as exc:
            _audit(request, operation=op, outcome="denied", resources=f"{name}=config_corrupt")
            return web.json_response({"error": str(exc), "code": "config_corrupt"}, status=409)
        except TrustSettingOverlayOwned as exc:
            # 409, not 200: writing config.json here changes NOTHING while the
            # overlay owns the setting, and a success response would tell the
            # operator a grant was revoked while the app stays trusted.
            _audit(request, operation=op, outcome="denied", resources=f"{name}=overlay_owned")
            return web.json_response(
                {
                    "error": str(exc),
                    "code": "trust_setting_overlay_owned",
                    "overlaySettings": exc.keys,
                },
                status=409,
            )
    # Re-check AFTER the write. `app_lifecycle_lock(name)` serializes this handler
    # against other in-process lifecycle work, but `kirocrew app uninstall` runs in a
    # DIFFERENT PROCESS and no asyncio lock reaches it — so an app that passed the
    # existence check above can be gone by the time the grant lands. Grants are keyed
    # on the name alone, so that leaves a grant over a name no app occupies, and the
    # next app installed under it would run its own code with no consent prompt: the
    # same orphan the uninstall path refuses to create and the consent modal rolls
    # back. Scoped to `was_installed` so a deliberately-granted registry name that
    # was never on disk is untouched.
    #
    # This check alone does NOT close the race — it covers only the interleaving
    # where the delete finishes first. The opposite one (this check runs and still
    # sees the app, and only then does the other process delete it) is closed by its
    # PAIR: `uninstall_app` withdraws the grant a second time AFTER its `rmtree`.
    # See the ordering argument there; the two guards are complementary and neither
    # is sufficient on its own.
    if was_installed and not await _run_off_loop(lambda: get_app(name) is not None):
        logger.warning(
            "app %r disappeared while its trust grant was being written; removing it",
            name,
        )

        def _undo(agent_raw: dict) -> None:
            agent_raw["apps_trusted"] = [a for a in _trusted_list_raw(agent_raw) if a != name]
            repositories = _trusted_repositories_raw(agent_raw)
            repositories.pop(name, None)
            agent_raw["apps_trusted_local"] = [
                a for a in _trusted_local_list_raw(agent_raw) if a != name
            ]
            agent_raw["apps_trusted_repositories"] = repositories

        try:
            await _mutate_agent_config(_undo)
        except (ConfigCorruptError, TrustSettingOverlayOwned):
            # The rollback could not be written. Report rather than claim a grant
            # that is both live and orphaned — this needs an operator, and the
            # snapshot in the response shows the entry that is still there.
            logger.warning("could not roll back the orphaned grant for %r", name)
        _audit(request, operation=op, outcome="denied", resources=f"{name}=uninstalled_mid_grant")
        return web.json_response(
            {
                "error": (
                    f"{name!r} was uninstalled while the grant was being saved, so the "
                    "grant was withdrawn — a grant over a name no app occupies would "
                    "let a different app installed under it run code without asking"
                ),
                "code": "app_uninstalled_mid_grant",
            },
            status=409,
        )

    _audit(request, operation=op, outcome="ok", resources=name)
    return await _trusted_apps_response()


async def api_trusted_app_revoke(request: web.Request) -> web.Response:
    """DELETE /api/security/trusted-apps/{name} — revoke trust, stop the code.

    Idempotent: revoking a name that holds no grant is a 200 (the postcondition
    "this app is not trusted" already holds). Deliberately NOT name-validated —
    a hand-edited ``config.json`` can hold a malformed entry that the snapshot
    surfaces, and the user must be able to remove exactly what they see.

    Revocation is EFFECTIVE, not just declarative: the grant is dropped first
    (so nothing can re-enable in the window) and then the app's code is torn
    down — shutdown hooks, route deregistration and cron cleanup via
    ``on_app_disable``, the backend PROCESS via ``stop_app_backend``, resource
    deregistration, and finally the enabled flag. ``disable_app`` alone is a
    metadata write: the backend keeps running with its app secret, its routes
    stay proxied and its crons stay armed, so a metadata-only revoke would
    report success while third-party code kept executing.

    The teardown runs ONLY for a name that actually held a grant and is not a
    builtin. Revoke's un-validated name is for removing junk config entries, and
    a side effect that powerful must not fire on a name the caller never granted
    — otherwise the endpoint becomes a way to disable any installed app,
    including a first-party one, without holding any grant over it.
    """
    op = "security.trusted_apps.revoke"

    # Body-scope import, like the sibling gates in this package
    # (``connections.py``, ``mcp_apps.py``, ``files.py``): ``source_providers``
    # reaches back into sibling handler modules, so importing the helper at
    # module scope from here would close a cycle.
    from kiro_crew.dashboard.handlers._shared import require_owner_dashboard_request

    denied = await require_owner_dashboard_request(request, op)
    if denied is not None:
        return denied
    name = request.match_info["name"]

    # Teardown runs BEFORE the grant is dropped, and the whole thing sits under the
    # app's lifecycle lock. Dropping the grant first looked safer ("nothing can
    # re-enable in the window") but the lock is what actually closes that window,
    # and grant-first made a partial failure UNRECOVERABLE: the retry would see no
    # grant, skip the teardown, and the app would keep running with trust already
    # gone. Stopping the code first means a failed teardown leaves the grant intact,
    # so the client can simply call DELETE again.

    is_builtin = name in await _run_off_loop(builtin_app_names)

    async with app_lifecycle_lock(name):
        # Config mutability is checked BEFORE anything is stopped, for the same
        # reason the blanket falling edge checks it: teardown disables the app,
        # and a mutation destined to fail (corrupt `config.json`, an
        # overlay-owned trust key) would leave the app switched off while the
        # grant it was supposed to lose is still standing. The user asked to
        # revoke trust and would get "that change did not take effect" over an
        # app that had silently stopped working. Pre-flight, not a guarantee —
        # `_mutate_agent_config` still re-checks inside the config lock, which is
        # what makes the check race-free at the moment of the write.
        try:
            await _preflight_agent_config_mutable()
        except ConfigCorruptError as exc:
            _audit(request, operation=op, outcome="denied", resources=f"{name}=config_corrupt")
            return web.json_response({"error": str(exc), "code": "config_corrupt"}, status=409)
        except TrustSettingOverlayOwned as exc:
            _audit(request, operation=op, outcome="denied", resources=f"{name}=overlay_owned")
            return web.json_response(
                {
                    "error": str(exc),
                    "code": "trust_setting_overlay_owned",
                    "overlaySettings": exc.keys,
                },
                status=409,
            )

        snapshot = await build_trusted_apps_snapshot_async()
        stored = set(snapshot["apps"]) | set(snapshot["ineffective"])
        was_granted = name in stored

        disabled = False
        teardown_warnings: list[str] = []
        # A builtin is never torn down: shipped code is exempt at the gate, so no
        # grant governs it and revoking one must not switch it off.
        if was_granted and not is_builtin:
            record = await _run_off_loop(lambda: get_app(name))
            if record:
                # Teardown runs regardless of the PERSISTED ``enabled`` flag.
                #
                # That flag is metadata, NOT evidence about the runtime.
                # ``disable_app`` is a pure metadata write, and the CLI
                # (``kirocrew app disable``) calls it from a DIFFERENT PROCESS
                # that cannot reach the gateway's backend child — so
                # ``enabled: false`` while the app's code is still executing is
                # an ordinary state, not a corner case. Gating the teardown on
                # the flag therefore skipped it for exactly the apps whose
                # recorded state was least trustworthy, and revoke answered 200
                # while third-party code kept running.
                #
                # Running it unconditionally is safe rather than merely
                # thorough: every step is idempotent (deregistration of absent
                # symlinks/manifests is a no-op), and a backend that genuinely
                # is NOT running produces no failure, because the teardown
                # reports one only when it can OBSERVE the port still listening.
                # So for an app that really was off this is a sequence of
                # no-ops, and for one whose flag lied it is the whole point.
                was_enabled = bool(record.get("enabled"))
                result = await teardown_app_runtime(name, record, withdrawing_trust=True)
                for warning in result.warnings:
                    logger.info("revoke teardown of %r: %s", name, warning)
                # Returned, not only logged. A warning here is something the
                # operator has to be able to LEARN — the leading case is the app's
                # own on_shutdown hook failing, which means state it was buffering
                # may be gone. The revocation still succeeded (its code is stopped),
                # so this rides on the 200 rather than turning into a refusal.
                teardown_warnings = list(result.warnings)
                if not result.ok:
                    # The app's code is NOT fully stopped — most importantly its
                    # crons may still fire. Reporting success here is the exact
                    # defect this endpoint was rewritten to remove, so fail loudly
                    # and leave the grant in place for a retry.
                    for failure in result.failures:
                        logger.warning("revoke teardown of %r FAILED: %s", name, failure)
                    _audit(
                        request,
                        operation=op,
                        outcome="failed",
                        resources=f"{name} teardown_incomplete",
                    )
                    return web.json_response(
                        {
                            "error": (
                                f"trust for {name!r} was NOT revoked: its code could not be "
                                "fully stopped, so the grant is left in place — retry"
                            ),
                            "code": "teardown_incomplete",
                            "failures": result.failures,
                        },
                        status=409,
                    )
                # ``disabled`` answers "did WE switch it off", so it stays keyed
                # on the PRE-teardown flag. ``disable_app`` returns ok for an app
                # that was already off, so reporting that as a disable would tell
                # the operator we changed something we did not touch.
                if was_enabled:
                    disabled = bool(await _run_off_loop(lambda: disable_app(name).ok))

        def _mutate(agent_raw: dict) -> None:
            agent_raw["apps_trusted"] = [a for a in _trusted_list_raw(agent_raw) if a != name]
            repositories = _trusted_repositories_raw(agent_raw)
            repositories.pop(name, None)
            agent_raw["apps_trusted_local"] = [
                a for a in _trusted_local_list_raw(agent_raw) if a != name
            ]
            agent_raw["apps_trusted_repositories"] = repositories

        try:
            await _mutate_agent_config(_mutate)
        except ConfigCorruptError as exc:
            _audit(request, operation=op, outcome="denied", resources=f"{name}=config_corrupt")
            return web.json_response({"error": str(exc), "code": "config_corrupt"}, status=409)
        except TrustSettingOverlayOwned as exc:
            # 409, not 200: writing config.json here changes NOTHING while the
            # overlay owns the setting, and a success response would tell the
            # operator a grant was revoked while the app stays trusted.
            _audit(request, operation=op, outcome="denied", resources=f"{name}=overlay_owned")
            return web.json_response(
                {
                    "error": str(exc),
                    "code": "trust_setting_overlay_owned",
                    "overlaySettings": exc.keys,
                },
                status=409,
            )

    _audit(
        request,
        operation=op,
        outcome="ok",
        resources=f"{name} was_granted={was_granted} disabled={disabled}",
    )
    return await _trusted_apps_response({"disabled": disabled, "warnings": teardown_warnings})


async def api_trusted_apps_allow_all(request: web.Request) -> web.Response:
    """PUT /api/security/trusted-apps/allow-all — {value: bool} blanket flag.

    Only a JSON boolean is accepted; a truthy ``"true"``/``1`` is rejected rather
    than coerced, mirroring the strict identity check the execution gate applies
    (``third_party_execution_allowed``). Coercing here would persist a value the
    gate then reads as deny — a settings surface that lies about its own state.
    """
    op = "security.trusted_apps.allow_all"

    # Body-scope import, like the sibling gates in this package
    # (``connections.py``, ``mcp_apps.py``, ``files.py``): ``source_providers``
    # reaches back into sibling handler modules, so importing the helper at
    # module scope from here would close a cycle.
    from kiro_crew.dashboard.handlers._shared import require_owner_dashboard_request

    denied = await require_owner_dashboard_request(request, op)
    if denied is not None:
        return denied
    try:
        body = await request.json()
    except Exception:
        _audit(request, operation=op, outcome="denied", resources="invalid_json")
        return web.json_response({"error": "invalid JSON", "code": "invalid_value"}, status=400)

    # A valid-but-non-object body (e.g. `[]`) must yield a clean 400, not a 500.
    if not isinstance(body, dict):
        body = {}
    value = body.get("value")
    if not isinstance(value, bool):
        _audit(request, operation=op, outcome="denied", resources="bad_type")
        return web.json_response(
            {"error": "value must be a boolean", "code": "invalid_value"}, status=400
        )

    def _mutate(agent_raw: dict) -> None:
        agent_raw["apps_allow_third_party"] = value

    # Falling edge: STOP the code first, THEN persist `false`.
    #
    # The order matters because the flag is what authorises loading an app's Python
    # at all: `load_app_module` consults `app_execution_denied`, so the moment
    # `false` is on disk a third-party app holding no grant cannot be
    # loaded — and that includes its `on_shutdown` hook. Persisting first therefore
    # denied the very hook whose job is to flush state and release resources, so the
    # app was stopped without ever being told to clean up. Sweeping while trust
    # still stands lets the hook run, and the flag lands immediately afterwards.
    #
    # A pre-flight check comes first so a mutation that CANNOT succeed (corrupt
    # config, overlay-owned setting) does not stop the user's apps before failing.
    # The sweep's own failures do not abort the write: the flag going off is the
    # security-relevant half and must not be held hostage to a stuck teardown, so
    # they are reported as a 409 afterwards instead.
    stopped: list[str] = []
    still_running: list[str] = []
    sweep_error = ""
    if value is False:
        try:
            await _preflight_agent_config_mutable()
        except ConfigCorruptError as exc:
            _audit(request, operation=op, outcome="denied", resources="config_corrupt")
            return web.json_response({"error": str(exc), "code": "config_corrupt"}, status=409)
        except TrustSettingOverlayOwned as exc:
            _audit(request, operation=op, outcome="denied", resources="overlay_owned")
            return web.json_response(
                {
                    "error": str(exc),
                    "code": "trust_setting_overlay_owned",
                    "overlaySettings": exc.keys,
                },
                status=409,
            )
        try:
            stopped, still_running = await _stop_apps_running_on_blanket_trust()
        except Exception as exc:  # noqa: BLE001 - reported below, never swallowed
            # NOT a 500 and NOT a success. An unreadable app directory or a thrown
            # teardown means apps the flag was admitting may still be executing,
            # with their crons still scheduled. Reported as a coded partial failure
            # once the flag has landed.
            logger.warning("could not stop blanket-trusted apps", exc_info=True)
            sweep_error = str(exc) or exc.__class__.__name__

    try:
        await _mutate_agent_config(_mutate)
    except ConfigCorruptError as exc:
        _audit(request, operation=op, outcome="denied", resources="config_corrupt")
        return web.json_response({"error": str(exc), "code": "config_corrupt"}, status=409)
    except TrustSettingOverlayOwned as exc:
        # 409, not 200: writing config.json here changes NOTHING while the
        # overlay owns the setting, and a success response would tell the
        # operator a grant was revoked while the app stays trusted.
        _audit(request, operation=op, outcome="denied", resources="overlay_owned")
        return web.json_response(
            {
                "error": str(exc),
                "code": "trust_setting_overlay_owned",
                "overlaySettings": exc.keys,
            },
            status=409,
        )

    # Report BOTH lists. Withdrawing blanket trust while an app it was admitting
    # keeps running is exactly the "revocation that revokes nothing" shape this
    # feature already had to fix once; a response that only carried `stopped`
    # silently omitted the failures, so the operator could not tell that code they
    # just un-trusted is still executing. `outcome` reflects it too, so the audit
    # trail does not read as a clean success.
    if value is False:
        # SECOND sweep, after the flag is on disk, to close the enable race.
        #
        # The first sweep enumerates candidates and then tears them down, which takes
        # real time (it stops backend processes). An app enabled DURING that window is
        # never considered by it, and once `false` lands the gate stops admitting new
        # loads — so without this pass that app would keep executing indefinitely
        # under trust the operator believes they withdrew. Per-app
        # `app_lifecycle_lock` cannot prevent this: the race is against an app that
        # was not in the enumeration at all, so there was no name to lock.
        #
        # Serializing every lifecycle transition against this handler would be the
        # airtight fix, but there is no global lifecycle lock and adding one touches
        # every enable/disable path — out of proportion to this change. A second
        # sweep is bounded and provably closes the window: anything enabled in it is
        # enabled-and-ungranted, which is exactly what `_candidates` selects.
        #
        # Results merge into the SAME lists on purpose. An app caught here has had
        # its backend stopped, but its `on_shutdown` hook could not load (the flag is
        # already false), so its teardown reports a failure and it lands in
        # `still_running` — over-reporting rather than under-reporting, which is the
        # safe direction for a security control, and the 409 points the operator
        # straight at it. Retrying is idempotent and re-sweeps.
        try:
            late_stopped, late_still = await _stop_apps_running_on_blanket_trust(
                require_enabled=True
            )
        except Exception as exc:  # noqa: BLE001 - reported, never swallowed
            logger.warning("post-write blanket-trust sweep failed", exc_info=True)
            sweep_error = sweep_error or str(exc) or exc.__class__.__name__
        else:
            stopped = sorted(set(stopped) | set(late_stopped))
            still_running = sorted(set(still_running) | set(late_still))

    incomplete = bool(still_running or sweep_error)
    _audit(
        request,
        operation=op,
        outcome="partial" if incomplete else "ok",
        resources=(
            f"{value} stopped={len(stopped)} still_running={len(still_running)}"
            f"{' sweep_error' if sweep_error else ''}"
        ),
    )
    if incomplete:
        # 409 with the flag ALREADY changed, deliberately: the setting is the
        # security-relevant half and must not be rolled back, but the caller has to
        # be able to tell "trust withdrawn and nothing is running" from "trust
        # withdrawn and code it was admitting survived". A bare 200 with an extra
        # field could not be distinguished by a client that does not read the field,
        # and this is exactly the confusion the revoke path was already faulted for.
        # Retrying the same request is safe and idempotent — it re-runs the sweep.
        # 409 carrying the FRESH snapshot: the client must render the new state (the
        # flag really is off) while being told, in a machine-readable way, that
        # something it asked for did not happen.
        snapshot = await build_trusted_apps_snapshot_async()
        # Keys spelled out rather than `**snapshot`: a spread makes the body opaque
        # to the error-code contract scan, which cannot follow where `code` came
        # from and so cannot verify this response carries one.
        return web.json_response(
            {
                "apps": snapshot["apps"],
                "ineffective": snapshot["ineffective"],
                "allowAll": snapshot["allowAll"],
                "stopped": sorted(stopped),
                "stillRunning": sorted(still_running),
                "error": (
                    sweep_error
                    or "blanket trust is off, but these apps could not be stopped "
                    "and may still be executing: " + ", ".join(sorted(still_running))
                ),
                "code": "blanket_trust_sweep_incomplete",
            },
            status=409,
        )
    return await _trusted_apps_response(
        {"stopped": sorted(stopped), "stillRunning": sorted(still_running)}
    )


async def _stop_apps_running_on_blanket_trust(
    *, require_enabled: bool = False
) -> tuple[list[str], list[str]]:
    """Tear down third-party apps that hold no per-app grant.

    Called on the FALLING edge of ``agent.apps_allow_third_party``. An app in this
    set was executing solely because the blanket flag admitted it, so withdrawing
    the flag has to stop it — otherwise the setting is a label that changes nothing
    until the next gateway restart.

    ``require_enabled`` selects which of the two passes this is, and the default is
    the security-relevant one:

    * ``False`` — the pass that runs BEFORE the flag is persisted. It ignores the
      recorded ``enabled`` flag entirely, because that flag is metadata rather than
      evidence about the runtime (``disable_app`` only writes it, and the CLI writes
      it from a process that cannot stop the gateway's backend child). Trusting it
      here skipped the sweep for exactly the apps whose recorded state was least
      trustworthy.
    * ``True`` — the bounded SECOND pass that runs after the write, whose only job
      is catching an app enabled DURING the window. It must stay narrow: the flag
      is already false by then, so an app's ``on_shutdown`` hook cannot be
      loaded, and re-tearing-down an app the first pass already handled would run
      that hook in a state where the loader denies it.

    Apps WITH their own grant are left running: their permission is independent of
    the blanket flag, and stopping them would revoke a grant the user never touched.
    Builtins are never in scope — shipped code is exempt at the gate.

    Returns ``(stopped, still_running)``. Each app is torn down under its own
    lifecycle lock via the shared teardown, and one failure never aborts the rest —
    but a failure is REPORTED rather than swallowed, because an app that is still
    executing after the operator withdrew the trust admitting it is the single most
    important thing this response can say.
    """

    def _candidates() -> list[str]:
        # `require_enabled=False` (the falling-edge pass) deliberately does NOT
        # filter on `enabled`. The persisted flag is metadata, not evidence about
        # the runtime — `disable_app` only writes it, and the CLI writes it from a
        # process that cannot stop the gateway's backend child — so an app recorded
        # as disabled may still be executing. Filtering on it excluded precisely
        # the apps whose recorded state was least trustworthy from the sweep that
        # exists to stop them.
        #
        # Builtin exemption comes from `builtin_app_names()` ALONE, never from
        # `origin == "builtin"`: `origin` is a field of the app's own
        # `installed.json` record — writable by any app trusted to run code — so a
        # trusted app could stamp itself first-party and walk out of the sweep that
        # exists to stop it. `builtin_app_names()` cannot be forged: it requires a
        # SHIPPED `app.json` to declare the name, and its own contract is that
        # `installed.json` is consulted only to REMOVE trust, never to widen it.
        granted = set(build_trusted_apps_snapshot()["apps"])
        builtins = builtin_app_names()
        return [
            app["name"]
            for app in list_apps()
            if app.get("name")
            and (app.get("enabled") or not require_enabled)
            and app["name"] not in granted
            and app["name"] not in builtins
        ]

    stopped: list[str] = []
    still_running: list[str] = []
    for name in await _run_off_loop(_candidates):
        try:
            async with app_lifecycle_lock(name):
                record = await _run_off_loop(lambda n=name: get_app(n))
                if not record:
                    continue
                was_enabled = bool(record.get("enabled"))
                if require_enabled and not was_enabled:
                    continue
                swept = await teardown_app_runtime(name, record, withdrawing_trust=True)
                for note in (*swept.warnings, *swept.failures):
                    logger.warning("blanket-trust teardown of %r: %s", name, note)
                if not swept.ok:
                    still_running.append(name)
                    continue
                # `stopped` is shown to the operator as the apps this action
                # stopped, so an app that was already off does not belong in it —
                # its teardown was a no-op and claiming otherwise overstates what
                # the sweep did.
                if not was_enabled:
                    continue
                if await _run_off_loop(lambda n=name: disable_app(n).ok):
                    stopped.append(name)
                else:
                    still_running.append(name)
        except Exception:  # noqa: BLE001 - one app must not block the others
            logger.warning("could not stop blanket-trusted app %r", name, exc_info=True)
            still_running.append(name)
    return stopped, still_running


# Named by the path callers import it from: the facade that serves it.
TrustSettingOverlayOwned.__module__ = "kiro_crew.dashboard.handlers.security"
