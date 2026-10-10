"""``GET/PUT /api/config/default-agent``: the alias a template choice resolves to, the enrollment of a user-level installed template, and the locked default write."""

from __future__ import annotations

import asyncio
import dataclasses
from typing import TYPE_CHECKING

from aiohttp import web

if TYPE_CHECKING:
    from kiro_crew.dashboard.handlers.agents import (
        SCOPE_GLOBAL,
        AgentInfo,
        ConfigReadError,
        KiroCrewAgentConfig,
        KiroCrewConfig,
        _foreign_private_copy_owner,
        _ForeignPrivateCopy,
        _lineage_unverifiable_error,
        _name_would_be_masked,
        _refresh_session_defaults,
        _require_owner,
        _sel,
        _StaleBinding,
        _UnverifiableLineage,
        agents_spec_lock,
        coerce_dict_section,
        discovery_executor,
        dispatch_kiro_agent,
        kiro_agents_dir_path,
        list_agents,
        logger,
        read_bounded_json,
        run_config_write,
        teams_mod,
        update_config_locked,
    )


class _AmbiguousDefaultTarget(Exception):
    """Several aliases bind the chosen template and none is the current default."""

    def __init__(self, aliases: list[str]):
        super().__init__(aliases)
        self.aliases = aliases


def _binds_template(bound: object, template: str) -> bool:
    """Whether a crewmate row's ``kiro_agent`` runs *template*.

    A binding that recorded the FILE name resolves the same template, so it is
    matched through :func:`dispatch_kiro_agent` too. One predicate for the
    pre-lock choice (:func:`_alias_binding_template`) and the locked re-check in
    :func:`api_default_agent`, so a binder the first sees is one the second
    sees.
    """
    return isinstance(bound, str) and (bound == template or dispatch_kiro_agent(bound) == template)


def _alias_binding_template(bindings: dict[str, str], default: str, template: str) -> str | None:
    """The alias to make the default when *template* is one some alias already runs.

    ``bindings`` maps each alias to its ``kiro_agent``, matched by
    :func:`_binds_template`. The current default wins when it is one of the
    binders (the picker then asked for what already holds); a single other
    binder is chosen; more than one raises ``_AmbiguousDefaultTarget``, because
    each alias carries its own memory store and workspace and nothing in the
    request names which of them the owner meant. ``None`` when no alias binds it.
    """
    binders = [alias for alias, bound in bindings.items() if _binds_template(bound, template)]
    if not binders:
        return None
    if default in binders:
        return default
    if len(binders) == 1:
        return binders[0]
    raise _AmbiguousDefaultTarget(binders)


class _AppRegisteredTemplate(Exception):
    """The template is an app's materialized agent, which the app's lifecycle owns."""


def _is_app_registered(info: AgentInfo) -> bool:
    """True when *info* is an app's materialized agent (``<app>--<agent>.json``).

    The same shape :func:`_namespaced_agent_file_exists` globs for, read off the
    discovery row instead of the directory. ``apps.bridges._register_agents``
    writes that file and ``_deregister_agents`` unlinks it when the app is
    disabled or uninstalled, so nothing in ``config.json`` may depend on it.
    """
    return info.filename.endswith(f"--{info.name}.json")


async def _installed_template_alias(
    name: str,
) -> tuple[KiroCrewAgentConfig, str] | None:
    """The alias record and filename for installed template *name*, or ``None``.

    Only a USER-LEVEL template the picker offers qualifies: the default is
    global, so a project agent (reachable from one checkout only) stays refused,
    and so does a background-only managed spec, matched on the owned file as the
    catalog matches it. A credential- or URL-shaped name is refused as the sync
    loop refuses it. The scan runs off the loop, like every other list_agents
    call.

    An APP's agent raises :class:`_AppRegisteredTemplate` instead: the owner's
    enrolled row is exempt from every prune, so it would outlive the spec the
    app removes on disable, and the default would then open no chat. Package
    (AIM) and user-authored agents remain eligible; this function makes no
    claim about what an external package lifecycle does to their files.

    Lineage is NOT decided here: ``AgentInfo.private_to`` is display data that an
    unreadable sidecar degrades to ``""``. The locked write re-reads it strictly
    through :func:`_foreign_private_copy_owner`, as every binding writer does.
    """
    # Deferred: the catalog module imports this one.
    from kiro_crew.dashboard.handlers.agent_catalog import _is_background_only

    if _name_would_be_masked(name):
        return None
    try:
        found = await asyncio.get_running_loop().run_in_executor(
            discovery_executor(), lambda: list(list_agents())
        )
    except Exception:
        logger.warning("default agent: installed-agent scan failed", exc_info=True)
        return None
    for info in found:
        if info.name == name and info.scope == SCOPE_GLOBAL and not _is_background_only(info):
            if _is_app_registered(info):
                raise _AppRegisteredTemplate(name)
            # Stamped ``kirocrew`` (the mark every non-sync writer leaves), not the
            # spec's discovery source: the owner chose this crewmate, so neither
            # the startup prune of generated sync rows nor the sync's prune of
            # package rows may treat it as one they wrote.
            return (
                KiroCrewAgentConfig(
                    kiro_agent=name, description=info.description, source="kirocrew"
                ),
                info.filename,
            )
    return None


async def api_default_agent(request: web.Request) -> web.Response:
    """GET/PUT /api/config/default-agent — read or set the default agent."""
    import kiro_crew.dashboard.handlers as _h  # noqa: F811

    if request.method == "PUT":
        denied = await _require_owner(request, "default_agent.write")
        if denied is not None:
            return denied
        body, body_err = await read_bounded_json(request, max_bytes=None)
        if body_err is not None:
            return body_err
        assert body is not None  # read_bounded_json returns (dict, None) on success
        name = body.get("agent", "")
        # Reject non-strings before any use: a JSON list/object here would make
        # the membership check below raise (unhashable) into a 500, and a
        # non-string must never reach the config write either.
        if not isinstance(name, str):
            return web.json_response(
                {"error": "agent must be a string", "code": "invalid_agent_type"}, status=400
            )
        # Only a config alias may become the default: the default is resolved
        # from cfg.agents on every dispatch, so persisting any other name (a
        # project-scope discovery, an app agent, a typo) writes a default that
        # silently resolves to something else. Guarded server-side so EVERY
        # caller is covered, not just whichever picker currently hides the
        # action — project-scope rows carry scope="project" in /api/agents
        # precisely so UIs can disable this, but the config file is the last
        # line of defense.
        try:
            # Config load is stat/read/validation filesystem work; off-loop so
            # slow storage cannot freeze chat and the liveness heartbeat.
            cfg: KiroCrewConfig | None = await asyncio.to_thread(KiroCrewConfig.load)
        except Exception:
            cfg = None
        known = set(cfg.agents.keys()) if cfg is not None else set()
        # Fail CLOSED: an unreadable config yields an empty `known`, and that is
        # precisely when validation is impossible — a non-empty name must be
        # rejected, not waved through. A valid config always has at least one
        # agent (load() guarantees default_agent exists in agents), so an empty
        # set never rejects a legitimate alias.
        # An installed template (one the user made, or one AIM / an app put in
        # ~/.kiro/agents) is offered by the chat picker's "Set as default" row
        # but is not an alias. When an alias already runs that template, THAT
        # alias becomes the default: a second row for the same template would
        # split one agent across two memory stores (the ``kirocrew`` template is
        # bound by ``default`` on every install). Otherwise choosing it IS the
        # request to enroll it: the alias is added with default bindings -- the
        # record the sync route writes, so the default runs exactly what picking
        # the template runs -- in the SAME locked write that sets the default.
        # An app's agent is refused instead (see _installed_template_alias).
        # Only when the config is readable (`cfg` loaded): fail-closed stays.
        enroll: KiroCrewAgentConfig | None = None
        enroll_filename: str | None = None
        target = name
        if name and name not in known and cfg is not None:
            try:
                bound = _alias_binding_template(
                    {alias: a.kiro_agent for alias, a in cfg.agents.items()},
                    cfg.default_agent,
                    name,
                )
            except _AmbiguousDefaultTarget as exc:
                return web.json_response(
                    {
                        "error": f"agents {sorted(exc.aliases)} all run {name!r}; "
                        "choose one of them",
                        "code": "default_agent_ambiguous",
                        "agents": sorted(exc.aliases),
                    },
                    status=409,
                )
            if bound is not None:
                target = bound
            else:
                try:
                    enrolled_template = await _installed_template_alias(name)
                    if enrolled_template is not None:
                        enroll, enroll_filename = enrolled_template
                except _AppRegisteredTemplate:
                    return web.json_response(
                        {
                            "error": f"agent {name!r} is installed by an app, which removes it "
                            "when the app is disabled; it cannot be the default agent",
                            "code": "app_registered_template",
                        },
                        status=409,
                    )
        if name and target not in known and enroll is None:
            return web.json_response(
                {
                    "error": f"agent {name!r} is not a configured agent alias",
                    "code": "default_agent_not_alias",
                },
                status=400,
            )
        path = _h.config_path()

        # This read-modify-write must hold the SAME in-process lock every other
        # ``config.json`` RMW in the dashboard takes (agent create/update/delete,
        # capability install/uninstall, the agent-config PUT). The event loop does
        # not serialize it for free: the PUT's own RMW runs in a WORKER
        # THREAD, holding this lock across the offload, so an unlocked read here
        # can capture a baseline the worker is about to republish — and the last
        # atomic rename silently reverts the other side's unrelated settings.
        #
        # That lock is not sufficient on its own, though: it is an asyncio lock,
        # so it serializes only same-loop callers. The read-modify-write itself
        # goes through ``update_config_locked``, which holds the
        # ``<config>.json.lock`` sidecar across its own read and write and so
        # also serializes against the CLI, worker threads and other processes.
        #
        # ``run_config_write`` is the one async entry point that holds BOTH --
        # its own docstring says so -- and it is what every other converted
        # dashboard writer uses. It takes the loop-side lock, dispatches the
        # synchronous read-modify-write to a worker thread so an unbounded
        # advisory-flock wait never stalls the gateway, and SHIELDS that worker
        # in a drain loop so the lock cannot be released with a write still in
        # flight. Composing those three by hand here would be a third copy of a
        # helper that already exists, free to drift from it.
        enrolled = False

        def _set_default(data: dict) -> dict:
            nonlocal enrolled
            if enroll is not None or target != name:
                # The request named a TEMPLATE, and `target` is the alias that
                # runs it: one chosen from the pre-lock read, or one enrolled
                # here under the template's own name. Both decisions are
                # re-derived INSIDE the critical section, like the locked
                # rebind's checks, because the rows can change in the window:
                # the chosen alias rebound or deleted, or an alias created under
                # the very name being enrolled -- which `POST /api/agents`
                # permits bound to ANY template. Setting the default to such a
                # row would silently run that other template, and a repeat of
                # the request would not notice (the name is then an alias).
                agents = coerce_dict_section(data, "agents")
                row = agents.get(target)
                if row is not None or enroll is None:
                    if not (isinstance(row, dict) and _binds_template(row.get("kiro_agent"), name)):
                        raise _StaleBinding()
                else:
                    # A crew bound to this template since the pre-lock read
                    # makes the enrollment a second row for it. Matched as the
                    # pre-lock read matched, so a file-name binding made in the
                    # window counts too.
                    if any(
                        isinstance(other, dict) and _binds_template(other.get("kiro_agent"), target)
                        for other in agents.values()
                    ):
                        raise _StaleBinding()
                    assert enroll_filename is not None
                    agents_dir = kiro_agents_dir_path()
                    with agents_spec_lock(agents_dir):
                        # Template deletion and spec writers serialize through
                        # this lock. Re-scan the exact discovery row here, after
                        # the config re-check and immediately before enrollment,
                        # so a removed or renamed spec cannot become durable.
                        try:
                            current = list_agents(agents_dir=agents_dir)
                        except Exception as exc:
                            raise _StaleBinding() from exc
                        if not any(
                            info.scope == SCOPE_GLOBAL
                            and info.name == name
                            and info.filename == enroll_filename
                            for info in current
                        ):
                            raise _StaleBinding()
                        # STRICT lineage read, as every binding writer does it:
                        # the scan's ``private_to`` is display data an unreadable
                        # sidecar degrades to "", and the spawn gate validates
                        # governance, not ownership. No crew holds this name, so
                        # every owner is foreign -- including the deleted owner
                        # of an orphaned copy, which the sync loop likewise
                        # refuses to resurrect.
                        if owner := _foreign_private_copy_owner("", target):
                            raise _ForeignPrivateCopy(owner)
                        # Every path that registers a name purges a stale team
                        # membership first, inside the lock (see the sync loop).
                        teams_mod.release_for_create(target)
                        agents[target] = dataclasses.asdict(enroll)
                        enrolled = True
            data["default_agent"] = target
            return data

        try:
            await run_config_write(
                update_config_locked, path, mutate=_set_default, stamp_meta=False
            )
        except teams_mod.TeamsUnavailable:
            logger.warning("Refusing to set default agent: team state unavailable", exc_info=True)
            return web.json_response(
                {"error": "team state unavailable; try again", "code": "teams_unavailable"},
                status=409,
            )
        except _StaleBinding:
            return web.json_response(
                {
                    "error": f"the crews running {name!r} changed underneath this request; "
                    "reload and retry.",
                    "code": "stale_binding",
                },
                status=409,
            )
        except _ForeignPrivateCopy as exc:
            return web.json_response(
                {
                    "error": f"Template {name!r} is crew '{exc.owner}'s private copy; "
                    "it cannot be the default.",
                    "code": "foreign_private_copy",
                },
                status=409,
            )
        except _UnverifiableLineage as exc:
            return web.json_response(
                {
                    "error": _lineage_unverifiable_error(name, exc),
                    "code": "lineage_unverifiable",
                },
                status=409,
            )
        except ConfigReadError:
            # Fail closed: writing back a {} baseline would drop every other
            # setting. Nothing durable ran, so this 500 is exact.
            logger.exception("Refusing to set default agent: config unreadable")
            return web.json_response(
                {"error": "failed to read config file", "code": "config_unreadable"},
                status=500,
            )
        if enrolled:
            # A crew registration, so the same two follow-ups as the create
            # route: the factory's captured config does not know the crew, and
            # the write installs the tool grants ``_require_owner`` names.
            await _refresh_session_defaults(request, target)
            _sel().log_api_access(
                caller=request.get("user", "dashboard"),
                operation="agent.create",
                outcome="success",
                source="dashboard",
                resources=target,
            )
        return web.json_response({"ok": True, "default_agent": target})
    cfg = KiroCrewConfig.load()
    return web.json_response({"default_agent": cfg.default_agent})
