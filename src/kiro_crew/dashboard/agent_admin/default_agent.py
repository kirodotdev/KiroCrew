"""``GET/PUT /api/config/default-agent``: the default template for new sessions (``agent.default_agent``) and the roster's default crewmate alias, each a locked config write."""

from __future__ import annotations

import asyncio
import contextlib
import os
from pathlib import Path
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
        _name_would_be_masked,
        _overlay_kiro_agent,
        _require_owner,
        _sel,
        _StaleBinding,
        _UnverifiableLineage,
        agents_spec_lock,
        coerce_dict_section,
        default_template,
        discovery_executor,
        kiro_agents_dir_path,
        list_agents,
        logger,
        read_bounded_json,
        run_config_write,
        update_config_locked,
    )


class _AppRegisteredTemplate(Exception):
    """The template is an app's materialized agent, which the app's lifecycle owns."""


class _BackgroundOnlyTemplate(Exception):
    """The template is a managed background-only spec, which no chat runs."""


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

    A background-only managed spec raises :class:`_BackgroundOnlyTemplate`: it
    IS installed, so "not installed" would send the user looking for a file that
    is there; the refusal names the real reason.

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
        if info.name == name and info.scope == SCOPE_GLOBAL:
            if _is_background_only(info):
                raise _BackgroundOnlyTemplate(name)
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
        # The namespace the picker chose the name in, as the create and switch
        # routes take it. A crewmate and a template may share a name, and the two
        # are different defaults (see the template branch below).
        agent_kind = body.get("agent_kind", "")
        if agent_kind not in ("", "member", "template"):
            return web.json_response(
                {"error": "invalid agent kind", "code": "invalid_agent_kind"}, status=400
            )
        try:
            # Config load is stat/read/validation filesystem work; off-loop so
            # slow storage cannot freeze chat and the liveness heartbeat.
            cfg: KiroCrewConfig | None = await asyncio.to_thread(KiroCrewConfig.load)
        except Exception:
            cfg = None
        known = set(cfg.agents.keys()) if cfg is not None else set()
        # Two defaults live behind this route, told apart by the namespace the
        # picker chose the name in (`agent_kind`, as the create and switch routes
        # take it):
        #
        # * The default for NEW SESSIONS is a template. A session created without
        #   a crewmate runs `agent.default_agent` (`default_template`), never a
        #   crewmate record, so a template pick -- or any name that is no crewmate
        #   alias -- is written THERE and enrolls nothing.
        # * The roster's default crewmate (`default_agent`: the badge, the
        #   undeletable row) is a crewmate alias. Fail CLOSED on an unreadable
        #   config: `known` is empty exactly when the alias cannot be verified,
        #   and a non-string or unknown name must never reach the write.
        if (
            name
            and cfg is not None
            and (agent_kind == "template" or (not agent_kind and name not in known))
        ):
            return await _set_default_template(request, cfg, name)
        if name and cfg is None:
            # Both defaults need the config: the alias check reads the roster and
            # the template write is a locked read-modify-write of the same file.
            # Refused as what it is, so the picker does not tell the user the
            # template "is not a crewmate alias" when the file could not be read.
            return web.json_response(
                {"error": "config could not be read", "code": "config_unreadable"},
                status=503,
            )
        if name and name not in known:
            return web.json_response(
                {
                    "error": f"agent {name!r} is not a configured agent alias",
                    "code": "default_agent_not_alias",
                },
                status=400,
            )
        path = _h.config_path()

        def _set_default(data: dict) -> dict:
            data["default_agent"] = name
            return data

        try:
            await run_config_write(
                update_config_locked, path, mutate=_set_default, stamp_meta=False
            )
        except ConfigReadError:
            logger.exception("Refusing to set default agent: config unreadable")
            return web.json_response(
                {"error": "failed to read config file", "code": "config_unreadable"},
                status=500,
            )
        return web.json_response({"ok": True, "default_agent": name})
    cfg = KiroCrewConfig.load()
    # Two defaults, two fields. `default_template` is what an agent-less session,
    # cron or slot RUNS, so the surfaces that label one (the Schedule page's
    # agent column, the Worlds agent rail) read it here rather than the alias.
    return web.json_response(
        {"default_agent": cfg.default_agent, "default_template": default_template(cfg)}
    )


def _spec_lock_or_unlocked_read(agents_dir: Path) -> contextlib.ExitStack:
    """:func:`agents_spec_lock` held in an exit stack, or an empty stack when the
    lock FILE cannot be opened.

    The spec lock serializes the recheck with the spec WRITERS. A lock file that
    cannot be opened (a read-only ``~/.kiro/agents``, a sandboxed mount) is the
    refusal every writer meets at the same ``os.open``, so nothing can change the
    directory under a read; the open is probed HERE, before the lock, so that is
    the only case that degrades. A lock that opens but cannot be ACQUIRED (a
    holder past the bounded-wait ceiling) propagates: a stuck holder is a writer
    that may still change the directory, and the caller must refuse rather than
    read beside it. A plain function (not a generator-based context manager) so
    :func:`agent_admin.compose` rebinds it onto the handlers' globals.
    """
    held = contextlib.ExitStack()
    try:
        probe = os.open(agents_dir / ".kirocrew-agents.lock", os.O_CREAT | os.O_RDWR, 0o600)
    except OSError:
        return held
    os.close(probe)
    held.enter_context(agents_spec_lock(agents_dir))
    return held


async def _set_default_template(
    request: web.Request, cfg: KiroCrewConfig, name: str
) -> web.Response:
    """Make installed template *name* what a session created without a crewmate runs.

    Writes ``agent.default_agent`` (read back through :func:`default_template`)
    and touches no crewmate record. Refused for a name no user-level template
    declares, for an app's agent (its install lifecycle removes the file), for
    another crew's private copy, when the template vanished between the probe
    and the locked write, and when ``config.local.json`` pins the field, since
    the base write would then change nothing a new session reads.
    """
    import kiro_crew.dashboard.handlers as _h  # noqa: F811

    # `config.local.json` is merged OVER the base at load, so while it pins this
    # field a base write changes nothing a new session reads. Refused BEFORE
    # the write: a write followed by a 409 would land a default that takes
    # effect the day the pin is removed, with no record of it.
    overlaid = await asyncio.to_thread(_overlay_kiro_agent)
    if overlaid and overlaid != name:
        return web.json_response(
            {
                "error": "config.local.json pins agent.default_agent; edit it there",
                "code": "default_template_overlaid",
            },
            status=409,
        )
    try:
        installed = await _installed_template_alias(name)
    except _AppRegisteredTemplate:
        return web.json_response(
            {
                "error": f"agent {name!r} is installed by an app, which removes it "
                "when the app is disabled; it cannot be the default for new sessions",
                "code": "app_registered_template",
            },
            status=409,
        )
    except _BackgroundOnlyTemplate:
        return web.json_response(
            {
                "error": f"agent {name!r} is a background-only agent that no chat "
                "runs; it cannot be the default for new sessions",
                "code": "default_template_background_only",
            },
            status=409,
        )
    if installed is None:
        return web.json_response(
            {
                "error": f"{name!r} is not an installed custom agent, so new sessions "
                "cannot start from it",
                "code": "default_template_not_installed",
            },
            status=400,
        )

    _installed_config, installed_filename = installed

    def _set_template(data: dict) -> dict:
        # Re-checked INSIDE the config lock, under the agents-spec lock: the
        # template probed above can be deleted or renamed in the window, and a
        # default naming a file that is gone refuses every new session. The same
        # owned file must still declare the name (a different file declaring it
        # is a different template).
        agents_dir = kiro_agents_dir_path()

        def _recheck() -> None:
            try:
                current = list_agents(agents_dir=agents_dir)
            except Exception as exc:
                raise _StaleBinding() from exc
            if not any(
                info.scope == SCOPE_GLOBAL
                and info.name == name
                and info.filename == installed_filename
                for info in current
            ):
                raise _StaleBinding()
            # A crew's private copy is that crew's definition: its publish/reset
            # cleanup deletes the file, which must never be what every new
            # session runs. Strict, as every binding writer reads it.
            if owner := _foreign_private_copy_owner("", name):
                raise _ForeignPrivateCopy(owner)

        # This write only READS the directory; the lock serializes it with the
        # spec writers. A lock file that cannot be opened (a read-only
        # ``~/.kiro/agents``, a sandboxed mount) is the same refusal every
        # writer meets at the same ``os.open``, so nothing can change the
        # directory under this read: re-check without the lock rather than
        # refuse a config write the directory's state does not forbid.
        with _spec_lock_or_unlocked_read(agents_dir):
            _recheck()
        section = coerce_dict_section(data, "agent")
        section["default_agent"] = name
        return data

    try:
        await run_config_write(
            update_config_locked, _h.config_path(), mutate=_set_template, stamp_meta=False
        )
    except _StaleBinding:
        return web.json_response(
            {
                "error": f"the template {name!r} changed underneath this request; "
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
    except _UnverifiableLineage:
        return web.json_response(
            {
                "error": f"Cannot verify whether {name!r} is a private copy; retry.",
                "code": "lineage_unverifiable",
            },
            status=409,
        )
    except ConfigReadError:
        logger.exception("Refusing to set the default template: config unreadable")
        return web.json_response(
            {"error": "failed to read config file", "code": "config_unreadable"}, status=500
        )
    _sel().log_api_access(
        caller=request.get("user", "dashboard"),
        operation="default_template.write",
        outcome="success",
        source="dashboard",
        resources=name,
    )
    return web.json_response({"ok": True, "default_agent": cfg.default_agent})
