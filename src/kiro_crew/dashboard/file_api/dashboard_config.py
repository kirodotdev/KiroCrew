"""``GET``/``PUT /api/dashboard/config``: the dashboard settings document."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from aiohttp import web

if TYPE_CHECKING:
    from kiro_crew.dashboard.handlers.files import (
        LINK_PATTERN_PATTERN_MAX_LEN,
        LINK_PATTERN_URL_MAX_LEN,
        LINK_PATTERNS_MAX,
        MODEL_PICKER_HIDDEN_MODEL_ID_RE,
        MODEL_PICKER_HIDDEN_MODELS_MAX,
        VALID_MEMORY_MODES,
        _body_err_code,
        _sel,
        link_pattern_url_ok,
        logger,
        read_bounded_json,
    )


async def api_dashboard_config(request: web.Request) -> web.Response:
    """GET/PUT /api/dashboard/config — read or write dashboard settings."""
    from kiro_crew.config.loader import KiroCrewConfig  # noqa: F811

    # Owner gate for PUT: reject non-owner writes before paying the config-load
    # I/O cost. The check is cheap (in-memory predicate + optional off-thread
    # SEL audit on denial) compared to the KiroCrewConfig.load() thread hop
    # below, so non-owner PUT requests are rejected immediately.
    if request.method == "PUT":
        from kiro_crew.dashboard.handlers._shared import require_owner_dashboard_request

        owner_denied = await require_owner_dashboard_request(request, "dashboard_config.write")
        if owner_denied is not None:
            return owner_denied

    # Offloaded: KiroCrewConfig.load() stats, reads, parses, and validates config
    # files. The client polls this endpoint on an interval to pick up externally
    # edited dashboard.gitlab_hosts, so a slow or network-backed config directory
    # would otherwise stall the sole event loop on every poll.
    try:
        cfg = await asyncio.to_thread(KiroCrewConfig.load)
    except asyncio.CancelledError:
        # A cancellation at this await (client disconnect mid-poll, gateway
        # shutdown) would otherwise unwind the handler before either the
        # read-success or the write-success/failure audit below, leaving an
        # authorized config access attempt entirely absent from the
        # tamper-evident SEL chain. Pair the landed request with an explicit
        # failure event, then re-raise so cancellation still propagates.
        _sel().log_tool_invocation(
            session_key="dashboard",
            tool_name=(
                "dashboard_config_write" if request.method == "PUT" else "dashboard_config_read"
            ),
            outcome="failure",
            error="request_cancelled",
        )
        raise
    if request.method == "PUT":
        # Default cap: the body is a fixed set of dashboard toggles and numbers.
        body, body_err = await read_bounded_json(request)
        if body_err is not None:
            _sel().log_tool_invocation(
                session_key="dashboard",
                tool_name="dashboard_config_write",
                outcome="failure",
                error=_body_err_code(body_err),
            )
            return body_err
        assert body is not None  # read_bounded_json returns (dict, None) on success
        _allowed = {
            "restore_sessions",
            "restore_window_minutes",
            "merge_queued_messages",
            "default_memory_mode",
            "widget_density",
            "use_builtin_browser",
            "verbosity",
            "quick_send",
            "session_grid",
            "tail_fork_enabled",
            "link_previews",
            "link_patterns",
            "mcp_app_panel",
            "auto_open_git_panel",
            "folder_suggestions_enabled",
            "session_card_source_links",
            "model_picker_hidden_models_add",
            "model_picker_hidden_models_remove",
        }
        # One-release backward-compat shim for removed key; delete after all clients update.
        deprecated_ignored_keys = {"tail_fork_head_handling"}
        # Read-only keys the GET exposes: both settings surfaces save with
        # `mutate({ ...dashCfg, ...patch })`, so every GET field comes back in the
        # PUT body. Drop them here instead of listing them in _allowed -- they
        # stay unwritable, but a round-tripped read-only field must not 400 an
        # unrelated toggle save.
        read_only_ignored_keys = {
            "gitlab_hosts",
            "jira_hosts",
            "social_share_enabled",
            "decisions_enabled",
            "model_picker_hidden_models",
            "model_picker_configured",
        }
        body = {
            k: v
            for k, v in body.items()
            if k not in deprecated_ignored_keys and k not in read_only_ignored_keys
        }
        unknown = set(body.keys()) - _allowed
        if unknown:
            _sel().log_tool_invocation(
                session_key="dashboard", tool_name="dashboard_config_write", outcome="failure"
            )
            return web.json_response({"error": f"Unknown fields: {unknown}"}, status=400)
        updates: dict[str, object] = {}
        hidden_model_add: list[str] | None = None
        hidden_model_remove: list[str] | None = None

        def _validated_hidden_model_list(
            field: str,
        ) -> tuple[list[str] | None, web.Response | None]:
            # mypy checks this owner before the handlers module, defers the
            # function, and loses the outer narrowing of ``body`` here.
            val = body[field]  # type: ignore[index]
            if not isinstance(val, list) or len(val) > MODEL_PICKER_HIDDEN_MODELS_MAX:
                _sel().log_tool_invocation(
                    session_key="dashboard", tool_name="dashboard_config_write", outcome="failure"
                )
                return None, web.json_response(
                    {
                        "error": (
                            f"{field} must be an array of at most "
                            f"{MODEL_PICKER_HIDDEN_MODELS_MAX} model IDs"
                        ),
                        "code": "invalid_model_picker_hidden_models",
                    },
                    status=400,
                )
            hidden_models: list[str] = []
            seen_models: set[str] = set()
            for raw_model in val:
                if not isinstance(raw_model, str):
                    _sel().log_tool_invocation(
                        session_key="dashboard",
                        tool_name="dashboard_config_write",
                        outcome="failure",
                    )
                    return None, web.json_response(
                        {
                            "error": f"{field} entries must be strings",
                            "code": "invalid_model_picker_hidden_models",
                        },
                        status=400,
                    )
                model = raw_model.strip()
                if not model or model == "auto":
                    continue
                if not MODEL_PICKER_HIDDEN_MODEL_ID_RE.fullmatch(model):
                    _sel().log_tool_invocation(
                        session_key="dashboard",
                        tool_name="dashboard_config_write",
                        outcome="failure",
                    )
                    return None, web.json_response(
                        {
                            "error": f"{field} contains an invalid model ID",
                            "code": "invalid_model_picker_hidden_models",
                        },
                        status=400,
                    )
                if model not in seen_models:
                    seen_models.add(model)
                    hidden_models.append(model)
            return hidden_models, None

        if "restore_sessions" in body:
            val = body["restore_sessions"]
            if not isinstance(val, bool):
                _sel().log_tool_invocation(
                    session_key="dashboard", tool_name="dashboard_config_write", outcome="failure"
                )
                return web.json_response(
                    {"error": "restore_sessions must be a boolean"}, status=400
                )
            updates["restore_sessions"] = val
        try:
            if "restore_window_minutes" in body:
                updates["restore_window_minutes"] = max(
                    0, min(1440, int(body["restore_window_minutes"]))
                )
        except (TypeError, ValueError):
            _sel().log_tool_invocation(
                session_key="dashboard", tool_name="dashboard_config_write", outcome="failure"
            )
            return web.json_response(
                {"error": "restore_window_minutes must be an integer"}, status=400
            )
        if "merge_queued_messages" in body:
            val = body["merge_queued_messages"]
            if not isinstance(val, bool):
                _sel().log_tool_invocation(
                    session_key="dashboard", tool_name="dashboard_config_write", outcome="failure"
                )
                return web.json_response(
                    {"error": "merge_queued_messages must be a boolean"}, status=400
                )
            updates["merge_queued_messages"] = val
        if "default_memory_mode" in body:
            val = body["default_memory_mode"]
            if val not in VALID_MEMORY_MODES:
                _sel().log_tool_invocation(
                    session_key="dashboard",
                    tool_name="dashboard_config_write",
                    outcome="failure",
                )
                return web.json_response(
                    {
                        "error": "default_memory_mode must be 'persistent', "
                        "'incognito' or 'temporary'",
                        "code": "invalid_default_memory_mode",
                    },
                    status=400,
                )
            updates["default_memory_mode"] = val
        if "widget_density" in body:
            val = body["widget_density"]
            if val not in ("more", "less"):
                _sel().log_tool_invocation(
                    session_key="dashboard", tool_name="dashboard_config_write", outcome="failure"
                )
                return web.json_response(
                    {"error": "widget_density must be 'more' or 'less'"}, status=400
                )
            updates["widget_density"] = val
        if "link_patterns" in body:
            val = body["link_patterns"]
            cleaned: list[dict[str, str]] = []
            # Reject (not silently drop) malformed entries: this path serves the
            # settings editor, and a dropped rule with a 200 would read as saved.
            # Regex VALIDITY is not checked -- patterns are compiled by the
            # browser in the JavaScript dialect, which Python cannot arbitrate.
            ok = isinstance(val, list) and len(val) <= LINK_PATTERNS_MAX
            if ok:
                seen_patterns: set[str] = set()
                for entry in val:
                    pattern = entry.get("pattern") if isinstance(entry, dict) else None
                    url = entry.get("url") if isinstance(entry, dict) else None
                    if not isinstance(pattern, str) or not isinstance(url, str):
                        ok = False
                        break
                    # Pattern text is stored EXACTLY as authored -- whitespace
                    # in a regex is load-bearing, so strip() only decides
                    # blankness (mirrors the load coercer). URL edge-trim is
                    # safe: the template is expanded, never matched.
                    url = url.strip()
                    if not pattern.strip() or len(pattern) > LINK_PATTERN_PATTERN_MAX_LEN:
                        ok = False
                        break
                    # http(s) only: these templates become anchors in every
                    # transcript, so javascript:/file: must not reach disk. The
                    # shared validator also applies the renderer's
                    # origin-stability rule, so a rule that saves is a rule
                    # that linkifies.
                    if len(url) > LINK_PATTERN_URL_MAX_LEN or not link_pattern_url_ok(url):
                        ok = False
                        break
                    # Duplicate patterns must be rejected here, not deduped:
                    # the load-time coercer keeps only the first of a pair, so
                    # accepting both would persist rules that GET then omits —
                    # and the editor's next whole-list save would silently
                    # delete the survivor's twin from disk.
                    if pattern in seen_patterns:
                        ok = False
                        break
                    seen_patterns.add(pattern)
                    cleaned.append({"pattern": pattern, "url": url})
            if not ok:
                _sel().log_tool_invocation(
                    session_key="dashboard", tool_name="dashboard_config_write", outcome="failure"
                )
                return web.json_response(
                    {
                        "error": (
                            f"link_patterns must be a list of at most {LINK_PATTERNS_MAX}"
                            " {pattern, url} objects with distinct non-empty patterns and"
                            " an absolute http(s) url template containing {match}"
                        ),
                        "code": "invalid_link_patterns",
                    },
                    status=400,
                )
            updates["link_patterns"] = cleaned
        # Apply ONLY when it is the sole submitted setting. The Browser panel
        # sends it alone; the Chat settings panel PUTs the whole config object
        # from its own (possibly stale) cache, and applying it on that path would
        # let a Chat-panel save silently revert a toggle another client changed
        # (lost update).
        if body.keys() == {"use_builtin_browser"}:
            val = body["use_builtin_browser"]
            if not isinstance(val, bool):
                _sel().log_tool_invocation(
                    session_key="dashboard", tool_name="dashboard_config_write", outcome="failure"
                )
                return web.json_response(
                    {
                        "error": "use_builtin_browser must be a boolean",
                        "code": "invalid_use_builtin_browser",
                    },
                    status=400,
                )
            updates["use_builtin_browser"] = val
        if "verbosity" in body:
            # An older client may still send the retired ``ultra``; store
            # what it now means instead of rejecting the write. Spelled out
            # here (mirrors config.sections.LEGACY_VERBOSITY_ALIASES) because a
            # file_api owner may not import project modules at runtime.
            val = body["verbosity"]
            if val == "ultra":
                val = "answer_only"
            if val not in ("default", "concise", "answer_only"):
                _sel().log_tool_invocation(
                    session_key="dashboard", tool_name="dashboard_config_write", outcome="failure"
                )
                return web.json_response(
                    {"error": "verbosity must be 'default', 'concise' or 'answer_only'"},
                    status=400,
                )
            updates["verbosity"] = val
        if "tail_fork_enabled" in body:
            val = body["tail_fork_enabled"]
            if not isinstance(val, bool):
                _sel().log_tool_invocation(
                    session_key="dashboard", tool_name="dashboard_config_write", outcome="failure"
                )
                return web.json_response(
                    {"error": "tail_fork_enabled must be a boolean"}, status=400
                )
            updates["tail_fork_enabled"] = val
        if "folder_suggestions_enabled" in body:
            val = body["folder_suggestions_enabled"]
            if not isinstance(val, bool):
                _sel().log_tool_invocation(
                    session_key="dashboard", tool_name="dashboard_config_write", outcome="failure"
                )
                return web.json_response(
                    {
                        "error": "folder_suggestions_enabled must be a boolean",
                        "code": "invalid_folder_suggestions_enabled",
                    },
                    status=400,
                )
            updates["folder_suggestions_enabled"] = val
        if "link_previews" in body:
            val = body["link_previews"]
            if not isinstance(val, bool):
                _sel().log_tool_invocation(
                    session_key="dashboard", tool_name="dashboard_config_write", outcome="failure"
                )
                return web.json_response(
                    {
                        "error": "link_previews must be a boolean",
                        "code": "invalid_link_previews",
                    },
                    status=400,
                )
            updates["link_previews"] = val
        if "quick_send" in body:
            val = body["quick_send"]
            if not isinstance(val, bool):
                _sel().log_tool_invocation(
                    session_key="dashboard", tool_name="dashboard_config_write", outcome="failure"
                )
                return web.json_response({"error": "quick_send must be a boolean"}, status=400)
            updates["quick_send"] = val
        if "session_grid" in body:
            val = body["session_grid"]
            if not isinstance(val, bool):
                _sel().log_tool_invocation(
                    session_key="dashboard", tool_name="dashboard_config_write", outcome="failure"
                )
                return web.json_response({"error": "session_grid must be a boolean"}, status=400)
            updates["session_grid"] = val
        if "mcp_app_panel" in body:
            val = body["mcp_app_panel"]
            if not isinstance(val, bool):
                _sel().log_tool_invocation(
                    session_key="dashboard", tool_name="dashboard_config_write", outcome="failure"
                )
                return web.json_response(
                    {
                        "error": "mcp_app_panel must be a boolean",
                        "code": "invalid_mcp_app_panel",
                    },
                    status=400,
                )
            updates["mcp_app_panel"] = val
        if "auto_open_git_panel" in body:
            val = body["auto_open_git_panel"]
            if not isinstance(val, bool):
                _sel().log_tool_invocation(
                    session_key="dashboard", tool_name="dashboard_config_write", outcome="failure"
                )
                return web.json_response(
                    {
                        "error": "auto_open_git_panel must be a boolean",
                        "code": "invalid_auto_open_git_panel",
                    },
                    status=400,
                )
            updates["auto_open_git_panel"] = val
        if "session_card_source_links" in body:
            val = body["session_card_source_links"]
            if not isinstance(val, bool):
                _sel().log_tool_invocation(
                    session_key="dashboard", tool_name="dashboard_config_write", outcome="failure"
                )
                return web.json_response(
                    {
                        "error": "session_card_source_links must be a boolean",
                        "code": "invalid_session_card_source_links",
                    },
                    status=400,
                )
            updates["session_card_source_links"] = val
        if "model_picker_hidden_models_add" in body:
            hidden_model_add, error_response = _validated_hidden_model_list(
                "model_picker_hidden_models_add"
            )
            if error_response is not None:
                return error_response
            updates["model_picker_configured"] = True
        if "model_picker_hidden_models_remove" in body:
            hidden_model_remove, error_response = _validated_hidden_model_list(
                "model_picker_hidden_models_remove"
            )
            if error_response is not None:
                return error_response
            updates["model_picker_configured"] = True
        # Serialize the read-modify-write under BOTH config locks so no concurrent
        # writer -- in-process OR another process -- can clobber it:
        #  * update_config_locked holds the cross-process advisory file lock
        #    (<config>.lock) for the whole read-modify-write, so a concurrent
        #    `kirocrew config set` (which takes that same file lock) cannot land
        #    between our read and write and be silently discarded.
        #  * wrapping it in _get_config_lock() (the repo-wide, loop-bound asyncio
        #    lock) serializes it against the legacy in-process writers that still
        #    save under that asyncio lock alone.
        # Both run OFF-THREAD so the event loop is never blocked. Only the
        # dashboard.<field> keys this request validated are written, leaving every
        # other config section on disk untouched. GET stays lock-free.
        from kiro_crew.config.loader import update_config_locked  # noqa: F811
        from kiro_crew.dashboard.handlers.agents import (  # lazy: import cycle
            _get_config_lock,
        )

        def _apply_dashboard_updates(data: dict) -> dict:
            # `dashboard` is normally a dict; tolerate a missing or malformed
            # (non-dict, e.g. a hand-edited/corrupt `[]`) section by replacing it
            # with a fresh dict rather than raising TypeError mid-write. The prior
            # non-dict value carried no valid dashboard settings, so this recovers
            # the section instead of losing data, and leaves other config keys
            # untouched.
            section = data.get("dashboard")
            if not isinstance(section, dict):
                section = data["dashboard"] = {}
            if hidden_model_add is not None or hidden_model_remove is not None:
                current = section.get("model_picker_hidden_models")
                current_models = current if isinstance(current, list) else []
                remove = set(hidden_model_remove or [])
                merged_models: list[str] = []
                seen_models: set[str] = set()
                for raw_model in current_models:
                    if not isinstance(raw_model, str):
                        continue
                    model = raw_model.strip()
                    if not model or model == "auto" or model in remove or model in seen_models:
                        continue
                    seen_models.add(model)
                    merged_models.append(model)
                for model in hidden_model_add or []:
                    if model not in seen_models:
                        seen_models.add(model)
                        merged_models.append(model)
                section["model_picker_hidden_models"] = merged_models
            for _field, _value in updates.items():
                section[_field] = _value
            return data

        try:
            async with _get_config_lock():
                await asyncio.to_thread(
                    lambda: update_config_locked(mutate=_apply_dashboard_updates)
                )
        except asyncio.CancelledError:
            # Cancellation (client disconnect / gateway shutdown) during the
            # off-thread write does NOT hit the `except Exception` below
            # (CancelledError is a BaseException), and the worker may still land
            # the write -- so the authorized attempt would vanish from the SEL
            # chain. Log a failure outcome, then re-raise so cancellation still
            # propagates. Mirrors the load guard above; both satisfy the
            # backend-security-controls audit contract.
            _sel().log_tool_invocation(
                session_key="dashboard",
                tool_name="dashboard_config_write",
                outcome="failure",
                error="request_cancelled",
            )
            raise
        except Exception:
            # Any other failure to land the write -- e.g. a corrupt on-disk config
            # makes update_config_locked's fail-closed read raise ConfigReadError
            # (not an OSError, so nothing else catches it) -- must still leave a
            # tamper-evident SEL entry rather than escaping as an unlogged 500.
            _sel().log_tool_invocation(
                session_key="dashboard", tool_name="dashboard_config_write", outcome="failure"
            )
            logger.exception("dashboard config write failed")
            return web.json_response(
                {
                    "error": "failed to save dashboard config",
                    "code": "dashboard_config_write_failed",
                },
                status=500,
            )
        _sel().log_tool_invocation(
            session_key="dashboard", tool_name="dashboard_config_write", outcome="success"
        )
        chips_written = updates.get("session_card_source_links")
        if isinstance(chips_written, bool):
            # Publish the new value NOW instead of leaving it to the next
            # allowlist refresh. That refresh is on a 30s TTL, so without this
            # the sidebar keeps rendering chips for up to half a minute after an
            # explicit click -- the switch acknowledges itself instantly and
            # nothing appears to happen, which reads as broken. This handler
            # already knows the value, so polling for it is the wrong shape.
            #
            # The push is the other half: the publisher bumps the shared
            # generation, but the owner websocket only compares that generation
            # once per TTL round, so a push here is what re-serializes the slots
            # with the new answer.
            #
            # The value is read OUTSIDE the try on purpose: only the publish and
            # the push may fail silently, so a body that never carried this key
            # cannot reach the publisher at all -- and a test can tell the two
            # apart instead of a swallowed KeyError standing in for the guard.
            try:
                from kiro_crew.dashboard.handlers.source_providers import (  # lazy: import cycle
                    publish_session_card_chips_now,
                )

                await publish_session_card_chips_now(chips_written)
                state = request.app.get("state")
                if state is not None:
                    state.push_slots_update()
            except Exception:
                # Best-effort: the write itself succeeded, and the next refresh
                # round picks the value up within one TTL. Failing the request
                # here would report a saved setting as unsaved.
                logger.debug("chip-switch snapshot publish failed", exc_info=True)
        return web.json_response({"ok": True})
    _sel().log_tool_invocation(
        session_key="dashboard", tool_name="dashboard_config_read", outcome="success"
    )
    # Governance-derived, not a config value: the dashboard draws the "Share as
    # image" entry only when this is true, and it has no other way to know — the
    # share card has no server-side action to refuse, so this read IS the
    # enforcement point. Resolved off-thread (profile resolution may read from
    # disk); every decision is SEL-audited by the probe itself.
    from kiro_crew.dashboard import social_share
    from kiro_crew.decisions.capability import denied_sides

    social_share_denied = await asyncio.to_thread(social_share.is_share_denied)
    # Same shape, same reason: the Decisions feature-preview card is drawn only when
    # the ceiling permits the seam, and this endpoint is the only place the dashboard
    # can learn that. Presentation, not the control -- the consent PUT and the gate's
    # own consent read are the two chokepoints (``decisions/capability.py``). The card
    # is drawn while EITHER side permits: a fleet that allows hosted Jev but withdraws
    # local models (``capabilities.decisions_local``) still needs the card. A pinned
    # ``capabilities.decisions`` deny covers the local side too, so it hides the card.
    # Both rows evaluated in one hop, each audited once, never short-circuited.
    hosted_denied, local_denied = await asyncio.to_thread(denied_sides)
    decisions_denied = hosted_denied and local_denied
    return web.json_response(
        {
            "restore_sessions": cfg.dashboard.restore_sessions,
            "restore_window_minutes": cfg.dashboard.restore_window_minutes,
            "merge_queued_messages": cfg.dashboard.merge_queued_messages,
            "default_memory_mode": cfg.dashboard.default_memory_mode,
            "widget_density": cfg.dashboard.widget_density,
            "use_builtin_browser": cfg.dashboard.use_builtin_browser,
            "verbosity": cfg.dashboard.verbosity,
            "quick_send": cfg.dashboard.quick_send,
            "session_grid": cfg.dashboard.session_grid,
            "mcp_app_panel": cfg.dashboard.mcp_app_panel,
            "auto_open_git_panel": cfg.dashboard.auto_open_git_panel,
            "session_card_source_links": cfg.dashboard.session_card_source_links,
            "tail_fork_enabled": cfg.dashboard.tail_fork_enabled,
            "link_previews": cfg.dashboard.link_previews,
            "folder_suggestions_enabled": cfg.dashboard.folder_suggestions_enabled,
            "model_picker_hidden_models": list(cfg.dashboard.model_picker_hidden_models),
            "model_picker_configured": cfg.dashboard.model_picker_configured,
            # Read-only here (absent from the PUT allowlist above): authorizing a
            # self-managed GitLab instance is a config-file decision, not a
            # dashboard toggle. The client uses it only to decide which pasted
            # links become source tabs; the provider handler re-checks every URL.
            "gitlab_hosts": list(cfg.dashboard.gitlab_hosts),
            # Same discipline for Jira: Atlassian Cloud (*.atlassian.net) is
            # auto-recognized; self-hosted instances need explicit allowlisting.
            "jira_hosts": list(cfg.dashboard.jira_hosts),
            # Read-only: the `capabilities.social_share` governance answer. False
            # withdraws the "Share as image" menu entry; there is no toggle behind
            # it, so nothing here is writable.
            "social_share_enabled": not social_share_denied,
            # Read-only: the `capabilities.decisions` governance answer. False hides
            # the Decisions (Jev) feature-preview card; the owner's own switch is
            # the keystone behind `/api/decisions/consent`, never a field here.
            "decisions_enabled": not decisions_denied,
            # Read-write (unlike the host allowlists above): a rule only changes
            # how this dashboard RENDERS text -- it grants no fetch and no CLI
            # any authority -- so the settings editor may manage it.
            "link_patterns": [
                {"pattern": rule.pattern, "url": rule.url} for rule in cfg.dashboard.link_patterns
            ],
        }
    )
