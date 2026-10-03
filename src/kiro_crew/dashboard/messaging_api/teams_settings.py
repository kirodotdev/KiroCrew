"""The Microsoft Teams settings API: ``GET``/``PUT /api/teams/config``."""

from __future__ import annotations

import asyncio
import json
import os
from typing import TYPE_CHECKING

from aiohttp import web

if TYPE_CHECKING:
    from kiro_crew.dashboard.handlers.messaging import (
        _TEAMS_CREDENTIAL_REJECT_STATUSES,
        _TOKEN_VERIFY_TIMEOUT,
        DashboardState,
        _clean_id_list,
        _CredentialTupleChanged,
        _hot_apply_after_write,
        _LockedSectionWrite,
        _sel,
        _threshold_pct_rejection,
        _ThresholdPairInverted,
        _write_env_off_loop,
        channel_restart_required,
        clean_session_folder,
        ensure_channel_folder,
        is_direct_local_request,
        live,
        read_config_text,
        run_to_completion,
        stored_folder_name,
    )


async def api_teams_config_get(request: web.Request) -> web.Response:
    """GET /api/teams/config — read Teams channel status + config summary."""
    from kiro_crew.config.loader import (  # noqa: F811
        CRED_MICROSOFT_APP_ID,
        CRED_MICROSOFT_APP_PASSWORD,
        CRED_MICROSOFT_APP_TENANT_ID,
        KiroCrewConfig,
    )

    # Deferred for the same reason as in api_teams_activity: importing the client
    # probes for the optional PyJWT, which must not happen on the boot path.
    from kiro_crew.teams.client import HAS_JWT  # noqa: F811

    cfg = KiroCrewConfig.load()
    creds = cfg.load_credentials()
    tc = cfg.teams
    # Credential resolution mirrors the boot path (slack/gateway.py) exactly: the
    # env credential wins over config.json for all three values, so the panel
    # reports the identity the channel will actually run as.
    app_id = creds.get(CRED_MICROSOFT_APP_ID, "") or tc.app_id
    app_password = creds.get(CRED_MICROSOFT_APP_PASSWORD, "") or tc.app_password
    tenant_id = creds.get(CRED_MICROSOFT_APP_TENANT_ID, "") or tc.tenant_id
    state: DashboardState = request.app["state"]
    return web.json_response(
        {
            # True only once the outbound app credentials validated this
            # session (kept truthful by TeamsClient.on_state_change).
            "connected": bool(getattr(state, "teams_connected", False)),
            "connect_error": str(getattr(state, "teams_connect_error", ""))[:120],
            "configured": bool(app_id and app_password and tc.enabled and tc.allowed_emails),
            "read_only": not is_direct_local_request(request),
            "app_id_set": bool(app_id),
            # Presence only, deliberately with no masked preview: an Azure client
            # secret carries no vendor prefix, so the shared prefix-preserving
            # mask (_mask_secret keeps everything before the first "-") would
            # reveal real secret bytes rather than a type marker.
            "app_password_set": bool(app_password),
            # PyJWT ships in the optional `kirocrew[teams]` extra and the channel
            # REFUSES to start without it (teams/gateway.py), because inbound JWT
            # validation is impossible. Reported so an operator sees the reason
            # instead of a channel that silently never starts.
            "jwt_available": bool(HAS_JWT),
            "enabled": tc.enabled,
            # Non-secret: blank means a multi-tenant bot, a value means
            # single-tenant. The operator needs it to tell those apart.
            "tenant_id": tenant_id,
            "allowed_emails": list(tc.allowed_emails),
            "soft_threshold_pct": int(tc.soft_threshold_pct),
            "hard_threshold_pct": int(tc.hard_threshold_pct),
            "session_folder": tc.session_folder,
        }
    )


def _is_valid_teams_principal(v: str) -> bool:
    """Accept an allow-list entry that is either an email/UPN or an AAD object
    id. Both are non-empty, whitespace-free, and length-bounded; keeping the
    check shape-only (no regex) mirrors the Webex email helper and lets object
    ids (GUIDs) through, since Teams activities key on those."""
    if not v or len(v) > 254:
        return False
    return not any(ch.isspace() for ch in v)


async def _validate_teams_app_credentials(
    app_id: str, app_password: str, tenant_id: str
) -> str | None:
    """Check Azure Bot app credentials against Azure AD before they are stored.

    The client-credentials token exchange is the same call ``TeamsClient.connect``
    makes to prime its outbound token, so it is both the cheapest credential check
    available and exactly the one the channel performs at boot. Returns ``None``
    when Azure issues a token, or a short error code (``invalid_client``,
    ``unauthorized_client``, …) when it refuses the credentials. Azure-side
    trouble (5xx/429) and network failures propagate to the caller, which treats
    them as "unverifiable" rather than invalid — saves must not be blocked by
    being offline. Mirrors ``_validate_webex_token`` / ``_validate_discord_token``.

    Only the OAuth ``error`` code is surfaced, never ``error_description``: the
    description carries tenant ids, app ids, and a correlation id, none of which
    belong in a dashboard error string.
    """
    import aiohttp  # noqa: F811

    from kiro_crew.teams.client import (  # noqa: F811
        _TOKEN_SCOPE,
        _TOKEN_URL_TMPL,
        TEAMS_MULTITENANT_AUTHORITY,
    )

    # A blank tenant_id means a multi-tenant bot, whose token is issued by the Bot
    # Framework authority rather than a directory tenant. The default comes from
    # teams.client so the pre-store check and the running channel cannot disagree
    # about which authority a blank tenant means.
    authority = tenant_id.strip() or TEAMS_MULTITENANT_AUTHORITY
    timeout = aiohttp.ClientTimeout(total=_TOKEN_VERIFY_TIMEOUT)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(
            _TOKEN_URL_TMPL.format(tenant=authority),
            data={
                "grant_type": "client_credentials",
                "client_id": app_id,
                "client_secret": app_password,
                "scope": _TOKEN_SCOPE,
            },
        ) as resp:
            if 200 <= resp.status < 300:
                return None
            if resp.status in _TEAMS_CREDENTIAL_REJECT_STATUSES:
                desc = ""
                try:
                    data = await resp.json(content_type=None)
                    if isinstance(data, dict):
                        desc = str(data.get("error", "") or "")
                except Exception:
                    pass
                return (desc or f"HTTP {resp.status}")[:60]
            raise RuntimeError(f"teams credential verify http {resp.status}")


async def api_teams_config_save(request: web.Request) -> web.Response:
    """PUT /api/teams/config — persist the Teams secret (.env) + config (json).

    The app password (secret) is written ONLY to config_dir/.env
    (``MICROSOFT_APP_PASSWORD``, 0600); non-secret config (enabled, app_id,
    tenant_id, allowed_emails, thresholds) lives in config.json under the "teams"
    key. Remote sessions are read-only. Every field except ``session_folder`` is
    read at gateway startup, so an actual change returns ``restart_required``.
    """
    # A save is a config.json + credential transaction: a cancelled request
    # (client gone, gateway shutting down) must not abandon it between its
    # phases -- see ``run_to_completion``.
    return await run_to_completion(_teams_config_save(request))


async def _teams_config_save(request: web.Request) -> web.Response:
    """Body of the Teams save; runs to completion once started."""
    from kiro_crew.config.loader import (  # noqa: F811
        CRED_MICROSOFT_APP_ID,
        CRED_MICROSOFT_APP_PASSWORD,
        CRED_MICROSOFT_APP_TENANT_ID,
        ConfigReadError,
        _threshold_pct,
        config_path,
        read_env_file_credential,
    )

    caller = request.get("user", "dashboard")

    def _audit_denial(msg: str) -> None:
        _sel().log_api_access(
            caller=caller,
            operation="teams.config.update",
            outcome="denied",
            source="dashboard",
            error=msg,
        )

    def _deny(msg: str, status: int = 400) -> web.Response:
        _audit_denial(msg)
        return web.json_response({"error": msg}, status=status)

    def _reject(code: str, msg: str) -> web.Response:
        """Reject with a machine-readable ``code``; ``msg`` is advisory prose.

        A sibling of ``_deny`` rather than an extra parameter on it: the status is
        a literal here so the error-code contract gate can see the response, and
        the dashboard renders ``error`` verbatim into a localized UI, so prose
        alone would be untranslatable by construction (RFC 9457 3.1.3).
        """
        _audit_denial(msg)
        return web.json_response({"error": msg, "code": code}, status=400)

    # Remote sessions are read-only: a remote/tunneled session cannot alter
    # channel access or plant the Azure Bot secret.
    if not is_direct_local_request(request):
        return _deny("read-only from remote sessions (local machine only)", status=403)

    try:
        body = await request.json()
    except Exception:
        return _deny("invalid JSON")
    if not isinstance(body, dict):
        return _deny("body must be an object")

    # ── Phase 1: validate + stage (no partial writes). The secret goes to .env
    # only — never config.json — so the agent-readable config never holds it.
    env_updates: dict[str, str | None] = {}
    clear_flag = body.get("app_password_clear")
    if clear_flag is not None and not isinstance(clear_flag, bool):
        return _deny("app_password_clear must be a boolean")
    if clear_flag is True:
        env_updates[CRED_MICROSOFT_APP_PASSWORD] = None
    else:
        raw = body.get("app_password")
        if isinstance(raw, str):
            secret = raw.strip()
            if secret.startswith(f"{CRED_MICROSOFT_APP_PASSWORD}="):
                secret = secret[len(CRED_MICROSOFT_APP_PASSWORD) + 1 :].strip()
            if secret:
                if any(ch.isspace() for ch in secret):
                    return _deny("app_password must not contain whitespace")
                env_updates[CRED_MICROSOFT_APP_PASSWORD] = secret

    staged: dict[str, object] = {}
    if "enabled" in body:
        val = body.get("enabled")
        if not isinstance(val, bool):
            return _deny("enabled must be a boolean")
        staged["enabled"] = val
    for str_key in ("app_id", "tenant_id"):
        if str_key in body:
            val = body.get(str_key)
            if not isinstance(val, str):
                return _deny(f"{str_key} must be a string")
            v = val.strip()
            if any(ch.isspace() for ch in v):
                return _deny(f"{str_key} must not contain whitespace")
            staged[str_key] = v
    if "allowed_emails" in body:
        try:
            new_ids = _clean_id_list(
                body.get("allowed_emails"), _is_valid_teams_principal, "principal"
            )
        except ValueError as exc:
            return _deny(str(exc))
        staged["allowed_emails"] = new_ids

    # Context-window nudge thresholds, on the shared validator.
    for pct_key in ("soft_threshold_pct", "hard_threshold_pct"):
        bad_pct = _threshold_pct_rejection(body, pct_key)
        if bad_pct is not None:
            return _reject(*bad_pct)
        if pct_key in body:
            staged[pct_key] = int(body[pct_key])

    if "session_folder" in body:
        try:
            staged["session_folder"] = clean_session_folder(body.get("session_folder"))
        except ValueError as exc:
            return _deny(str(exc))

    # ── Phase 1.5: verify the app credentials against Azure AD before storing.
    # Runs whenever any part of the credential triple changes: a mistyped App ID
    # or tenant is as fatal as a bad secret, and all three are checked by the one
    # token exchange. Rejection fails the save here, where the operator can act on
    # it, and writes nothing. A network failure is NOT a rejection — the save
    # proceeds with a warning so being offline never blocks config (mirrors the
    # Webex and Discord saves).
    #
    # The fallbacks come from a config snapshot taken OUTSIDE the config lock and
    # are advisory only: they decide what to verify, never what to write. The
    # authoritative read-modify-write is still Phase 2 under the lock. Resolution
    # order mirrors the boot path (slack/gateway.py) — the env credential wins —
    # so the check exercises the credentials the channel will actually use.
    verify_warning = ""
    #: The exact (app_id, password, tenant) Azure accepted, or None when nothing was
    #: verified. Re-confirmed under the config lock before anything is written.
    verified_triple: tuple[str, str, str] | None = None
    credential_touched = (
        CRED_MICROSOFT_APP_PASSWORD in env_updates or "app_id" in staged or "tenant_id" in staged
    )
    if credential_touched:
        # Read credentials directly from .env (+ os.environ override) rather
        # than via KiroCrewConfig.load(), which triggers _load_resolved() ->
        # cfg.save(), an unconditional migration write-back that purges
        # teams.app_password from config.json as a side-effect.  That
        # write-back races Phase 2's write-order invariant (SET: .env first,
        # then config purge) by clearing the legacy credential before the .env
        # write has succeeded.  read_env_file_credential touches only the .env
        # file and never writes config.json.
        _p15_cfg = config_path()
        _raw_teams_15: dict = {}
        try:
            # Offload read_text + json.loads to a thread so a slow filesystem
            # cannot stall the async event loop.
            def _read_config_15() -> dict:
                data = json.loads(read_config_text(_p15_cfg)) if _p15_cfg.exists() else {}
                if not isinstance(data, dict):
                    raise ValueError("config.json is not a JSON object")
                return data

            _rd15 = await asyncio.to_thread(_read_config_15)
            # Guard against a malformed config.json where "teams" is not a dict
            # (e.g. someone hand-edited it to a list).  .get() on a list raises
            # AttributeError; the isinstance check degrades gracefully.
            _t15 = _rd15.get("teams")
            _raw_teams_15 = _t15 if isinstance(_t15, dict) else {}
        except Exception:
            pass
        _c_pw = await asyncio.to_thread(read_env_file_credential, CRED_MICROSOFT_APP_PASSWORD)
        _c_app_id = await asyncio.to_thread(read_env_file_credential, CRED_MICROSOFT_APP_ID)
        _c_tenant = await asyncio.to_thread(read_env_file_credential, CRED_MICROSOFT_APP_TENANT_ID)
        # ENV-first, matching load_credentials() semantics: os.environ overrides
        # the .env file.  A pending in-flight update still wins as
        # the outermost layer (see env_updates.get() below).
        _c_pw = os.environ.get(CRED_MICROSOFT_APP_PASSWORD, "") or _c_pw
        _c_app_id = os.environ.get(CRED_MICROSOFT_APP_ID, "") or _c_app_id
        _c_tenant = os.environ.get(CRED_MICROSOFT_APP_TENANT_ID, "") or _c_tenant
        # ENV-first: env_updates wins, then .env/os.environ (_c_pw).  When the
        # password lives ONLY in legacy config.json (not in .env or os.environ,
        # so _c_pw is empty), fall back to the raw legacy value so verification
        # and Phase-2's purge-write see the existing credential and preserve it.
        _c_pw_effective = _c_pw or _raw_teams_15.get("app_password", "")
        eff_password = env_updates.get(CRED_MICROSOFT_APP_PASSWORD, _c_pw_effective)
        eff_app_id = _c_app_id or str(staged.get("app_id", _raw_teams_15.get("app_id", "")))
        eff_tenant = _c_tenant or str(staged.get("tenant_id", _raw_teams_15.get("tenant_id", "")))
        # Nothing to verify while half the pair is missing (e.g. the operator is
        # clearing the secret, or is filling the form in two saves).
        if eff_app_id and eff_password:
            try:
                teams_err = await _validate_teams_app_credentials(
                    eff_app_id, eff_password, eff_tenant
                )
            except Exception:
                verify_warning = (
                    "Azure was unreachable, so the credentials were saved " "without verification."
                )
            else:
                if teams_err:
                    return _reject(
                        "credentials_rejected",
                        f"credentials rejected by Azure ({teams_err})",
                    )
                verified_triple = (eff_app_id, eff_password, eff_tenant)

    # ── Phase 2: commit under the repo-wide config lock (read fresh, merge only
    # the teams section, write atomic) so a concurrent save is never clobbered.
    from kiro_crew.dashboard.handlers.agents import _get_config_lock  # noqa: F811

    applied: list[str] = []
    async with _get_config_lock():
        path = config_path()
        try:
            data = json.loads(read_config_text(path)) if path.exists() else {}
            if not isinstance(data, dict):
                raise ValueError("config.json is not a JSON object")
        except Exception:
            return _deny("config.json is corrupt", status=500)
        if not isinstance(data.get("teams"), dict):
            data["teams"] = {}
        teams_cfg = data["teams"]

        _f_app_id: str | None = ""
        _f_tenant: str | None = ""
        now_password: str | None = ""
        if verified_triple is not None:
            # Re-derive the effective triple under the lock and refuse if what is about
            # to be STORED is not what Azure accepted. Optimistic, deliberately: the
            # verification is a network round trip and the config lock serializes EVERY
            # writer in the process, so holding it across that call would stall unrelated
            # saves and a hung endpoint would wedge them until the timeout. Verifying
            # outside and confirming inside costs one extra .env read and keeps the
            # invariant that nothing unverified is stored.
            #
            # The race it closes: two concurrent saves, one changing the app id and one
            # the secret. Each verifies a triple containing the OTHER's old value and
            # passes; the serialized commits then merge into a stored triple neither one
            # checked, and the channel is dead at the next restart with a green "Saved."
            # Read credentials directly from .env (same rationale as Phase 1.5:
            # avoid KiroCrewConfig.load() which would trigger migration write-back).
            _f_pw = await asyncio.to_thread(read_env_file_credential, CRED_MICROSOFT_APP_PASSWORD)
            _f_app_id = await asyncio.to_thread(read_env_file_credential, CRED_MICROSOFT_APP_ID)
            _f_tenant = await asyncio.to_thread(
                read_env_file_credential, CRED_MICROSOFT_APP_TENANT_ID
            )
            # ENV-first, matching load_credentials() semantics.
            _f_pw = os.environ.get(CRED_MICROSOFT_APP_PASSWORD, "") or _f_pw
            _f_app_id = os.environ.get(CRED_MICROSOFT_APP_ID, "") or _f_app_id
            _f_tenant = os.environ.get(CRED_MICROSOFT_APP_TENANT_ID, "") or _f_tenant
            now_password = env_updates.get(
                CRED_MICROSOFT_APP_PASSWORD,
                _f_pw or str(teams_cfg.get("app_password", "")),
            )
            now_app_id = _f_app_id or str(staged.get("app_id", teams_cfg.get("app_id", "")))
            now_tenant = _f_tenant or str(staged.get("tenant_id", teams_cfg.get("tenant_id", "")))
            if (now_app_id, now_password, now_tenant) != verified_triple:
                return _reject(
                    "config_changed",
                    "the Teams credentials changed while these were being verified; "
                    "nothing was saved — reload and try again",
                )
            # The same comparison runs once more INSIDE the sidecar lock, against
            # the merged section (``_finalize_teams`` below): the snapshot this
            # block read cannot see an ``app_id`` / ``tenant_id`` another PROCESS
            # lands before the write, and the .env-first fallback here would let
            # that value be stored under the verified password.

        # Threshold ordering is checked against the EFFECTIVE pair (the staged
        # value, else what is stored, else the shipped default), because a request
        # may send only one half. Still commit-last: nothing is written yet.
        # Stored values go through the loader's own coercion so a hand-edited
        # config.json is read here exactly as the next load will read it.
        eff_soft = _threshold_pct(
            staged.get("soft_threshold_pct", teams_cfg.get("soft_threshold_pct")), 80
        )
        eff_hard = _threshold_pct(
            staged.get("hard_threshold_pct", teams_cfg.get("hard_threshold_pct")), 95
        )
        if eff_hard < eff_soft:
            # The loader would silently pull soft down to hard
            # (_normalize_threshold_pair), so an inverted pair is not a crash but
            # is never what the operator meant: the soft nudge would be
            # unreachable. Refuse it here instead of storing a value that reads
            # back different.
            return _reject(
                "threshold_pct_inverted",
                "hard_threshold_pct must be >= soft_threshold_pct",
            )

        changes: dict[str, object] = {}
        if "enabled" in staged and staged["enabled"] != bool(teams_cfg.get("enabled", False)):
            changes["enabled"] = staged["enabled"]
        for str_key in ("app_id", "tenant_id"):
            if str_key in staged and staged[str_key] != teams_cfg.get(str_key, ""):
                changes[str_key] = staged[str_key]
        for pct_key, pct_default in (("soft_threshold_pct", 80), ("hard_threshold_pct", 95)):
            if pct_key in staged and staged[pct_key] != _threshold_pct(
                teams_cfg.get(pct_key), pct_default
            ):
                changes[pct_key] = staged[pct_key]
        if "allowed_emails" in staged and staged["allowed_emails"] != teams_cfg.get(
            "allowed_emails", []
        ):
            changes["allowed_emails"] = staged["allowed_emails"]
        if "session_folder" in staged and staged["session_folder"] != str(
            teams_cfg.get("session_folder", "") or ""
        ):
            changes["session_folder"] = staged["session_folder"]
        applied = list(changes.keys())
        # The secret is env-only; if a legacy plaintext app_password ever landed
        # in config.json, purge it when the credential is safely held elsewhere:
        # either it is being written to .env in this same save, or it already
        # exists in .env / os.environ (so purging the config copy is safe).
        # Do NOT purge when the password lives ONLY in legacy config.json (no .env
        # entry, no env_update) — that would erase the sole credential copy and
        # produce a dead pair at the next restart.
        # _c_pw is only populated inside ``if credential_touched`` (Phase 1.5).
        # For metadata-only saves (credential_touched=False) fall back to a
        # synchronous os.environ check — load_credentials() seeds os.environ from
        # .env at startup, so the key is present when .env holds the credential.
        _pw_in_env_or_environ = locals().get("_c_pw") or os.environ.get(
            CRED_MICROSOFT_APP_PASSWORD, ""
        )
        _pw_safe_in_env = bool(
            CRED_MICROSOFT_APP_PASSWORD in env_updates  # being written this save
            or _pw_in_env_or_environ  # already in .env / os.environ
        )
        if teams_cfg.get("app_password") and _pw_safe_in_env:
            applied.append("app_password_purged")
        # Blanked against the document the write lands on (see
        # ``_LockedSectionWrite``): a legacy copy a concurrent writer landed after
        # the snapshot is purged too, whenever the credential is safely in .env.
        blank_keys = ("app_password",) if _pw_safe_in_env else ()

        # Through ``update_config_locked``: it holds the advisory lock on the
        # sidecar ``<path>.lock`` across the whole read-modify-write, so a writer
        # in ANOTHER PROCESS cannot land between our read and our write, and the
        # changes are merged into the file as re-read inside that lock. The same
        # object undoes exactly those keys if the .env write below fails.
        def _finalize_teams(section: dict) -> None:
            # Both rules re-decided against the document the write lands on: a
            # concurrent writer may have moved a counterpart threshold or the
            # app id / tenant since the snapshot was taken, and a pair that is
            # inverted -- or a credential tuple Azure never saw -- only in the
            # merged result would otherwise be stored.
            if verified_triple is not None:
                fresh_app_id = _f_app_id or str(section.get("app_id", ""))
                fresh_tenant = _f_tenant or str(section.get("tenant_id", ""))
                if (fresh_app_id, now_password, fresh_tenant) != verified_triple:
                    raise _CredentialTupleChanged()
            if _threshold_pct(section.get("hard_threshold_pct"), 95) < _threshold_pct(
                section.get("soft_threshold_pct"), 80
            ):
                raise _ThresholdPairInverted()

        _cfg_write = _LockedSectionWrite(
            path, "teams", changes, blank_keys=blank_keys, finalize=_finalize_teams
        )
        # Hold the live-config watcher for the whole config+credential transaction:
        # the two files commit separately and a failed .env write rolls the config
        # back, so nothing between here and the end of the block may be applied to
        # the running gateway. The release wakes the watcher on the committed state.
        with live.hold():
            if changes or blank_keys:
                teams_cfg.update(changes)
                # Off-loop: file IO, and it may wait on another holder of the lock.
                try:
                    await _cfg_write.commit()
                except ConfigReadError:
                    return _deny("config.json is corrupt", status=500)
                except _ThresholdPairInverted:
                    return _reject(
                        "threshold_pct_inverted",
                        "hard_threshold_pct must be >= soft_threshold_pct",
                    )
                except _CredentialTupleChanged:
                    return _reject(
                        "config_changed",
                        "the Teams credentials changed while these were being verified; "
                        "nothing was saved — reload and try again",
                    )

            # Create the configured session folder now, on this user-initiated save,
            # so the reconcile path never has to write the folder store. Best-effort:
            # a failure leaves conversations unfiled until the next save.
            _folder_name = stored_folder_name(teams_cfg.get("session_folder"))
            if _folder_name:
                _state = request.app.get("state")
                if _state is not None:
                    await ensure_channel_folder(
                        _state,
                        "teams",
                        _folder_name,
                        relabel="session_folder" in changes,
                    )
            if env_updates:
                # Off-loop: the .env write is blocking file IO (lock, temp write,
                # owner-only lockdown, replace) that would stall the gateway loop
                # if run inline.
                #
                # Cancellation guard: _write_env_off_loop shields + drains its
                # worker, so a CancelledError from it means the .env write has
                # already finished (either succeeded or failed). Roll config back
                # ONLY when the write actually failed; if it succeeded, the pair
                # is consistent and rolling back would create a mismatch.
                _env_write_task: asyncio.Task[None] = asyncio.ensure_future(
                    _write_env_off_loop(env_updates)
                )
                try:
                    await asyncio.shield(_env_write_task)
                except asyncio.CancelledError:
                    # Drain to completion WITHOUT propagating, so we can inspect the
                    # outcome and roll back before re-raising (a second shield() would
                    # re-raise CancelledError before the rollback ran).
                    await asyncio.gather(_env_write_task, return_exceptions=True)
                    _env_exc = (
                        _env_write_task.exception() if not _env_write_task.cancelled() else None
                    )
                    if _env_exc is not None:
                        # .env write failed — roll config back for consistency.
                        if changes or blank_keys:
                            await _cfg_write.rollback("Teams")
                    raise
                except BaseException:
                    # Genuine .env write failure — roll the config metadata back so
                    # a failed write cannot leave the NEW metadata paired with the
                    # OLD credential on disk.
                    if changes or blank_keys:
                        await _cfg_write.rollback("Teams")
                    raise
                for key, new_val in env_updates.items():
                    if new_val is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = new_val

    _sel().log_api_access(
        caller=caller,
        operation="teams.config.update",
        outcome="ok",
        source="dashboard",
        resources=",".join(applied + list(env_updates.keys())),
    )
    # Answer only once the watcher has applied the write: a narrowed allow-list
    # is in force before the caller sees "saved", not one poll interval later.
    await _hot_apply_after_write()
    return web.json_response(
        {
            "ok": True,
            "restart_required": channel_restart_required("teams", applied, env_updates=env_updates),
            "verify_warning": verify_warning,
        }
    )
