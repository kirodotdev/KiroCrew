"""Writes to this instance's configuration: provider config and secrets, ``/settings``, and
which of the app's crons ``/rotation/arm`` leaves armed.

Everything here decides what the agent may do, which of its crons run, or where this app's
credentials and output go. So a request is validated in full before anything is written,
secrets are write-only, and a store that refuses a write answers a coded 503 (a coded 500 for
corruption) rather than a bare 500 that leaves the operator guessing whether it landed.
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any, Callable
from urllib.parse import urlsplit

from aiohttp import web

from kiro_crew.apps.builtins.ops_mission_control.backend import (
    notify_out,
    policy_store,
    rotation,
    slack_out,
)
from kiro_crew.apps.builtins.ops_mission_control.backend.http_routes._shared import (
    AuditWriter,
    RegistryLookup,
    _json_body,
    _NotABool,
    _require_bool,
    _store_read_refusal,
    logger,
)
from kiro_crew.apps.builtins.ops_mission_control.backend.models import MODE_ORDER
from kiro_crew.apps.builtins.ops_mission_control.backend.providers import set_top_level
from kiro_crew.cron import CronStoreUnreadable
from kiro_crew.dashboard.handlers._shared import require_owner_dashboard_request

#: Cap on a secret value. Real provider tokens are well under this; a larger body
#: is a misuse (or an attempt to bloat the keystone file) and is refused.
_MAX_SECRET_LEN = 512


#: Cap on the shared-ledger git remote URL. An ssh/https remote is short; a longer
#: value is a paste accident, not a repo.
_MAX_REMOTE_LEN = 512


#: Cap on an opaque provider-side identifier (a PagerDuty user id). Generous, because the
#: vendor owns the format; this only refuses something that is obviously not an id.
_MAX_PROVIDER_ID_LEN = 128


#: Branch names we will hand to ``git``. Deliberately narrow: letters, digits, and
#: ``._/-``, not starting with ``-`` (which would read as an option). The value is
#: already passed as its own argv entry, never interpolated into a shell string, so
#: this is about failing clearly rather than about injection.
_SAFE_BRANCH_RE = re.compile(r"[A-Za-z0-9._][A-Za-z0-9._/-]{0,98}")


#: GitHub's own login shape: alphanumerics and single hyphens, 1-39 chars. This value is
#: compared against names in the shared `rotation.yaml` to decide whether this instance is on
#: shift and whether it is the ledger leader, so a shape guard here keeps a junk value from
#: silently never matching (which would read as "always off shift") rather than being refused.
_SAFE_LOGIN_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9]|-(?=[A-Za-z0-9])){0,38}")


def _url_has_userinfo(remote: str) -> bool:
    """True when an http(s) ``remote`` carries ANY userinfo (``scheme://anything@host``).

    **Any userinfo at all, not just a password.** The first version of this checked
    ``parts.password`` only, reasoning that a bare ``user@`` is a username rather than a
    secret. That was wrong, and wrong in the shape that matters most: GitHub's own documented
    token remote puts the PAT in the USERNAME position —
    ``https://ghp_xxx@github.com/org/repo.git`` — with no password at all. So the single
    likeliest way an operator pastes a token was the one shape explicitly allowed through, and
    a test asserted that as correct. Found in review.

    On http(s) there is no case where userinfo is worth storing in a world-readable file: a
    real username is supplied by a credential helper, and anything else is a secret. Refuse
    both halves.

    Still narrow where narrowness is right — two legitimate shapes contain an ``@``:

    - **scp-style**: ``git@github.com:org/repo.git`` — no ``://``, so no userinfo component
      exists. The single most common remote form there is; flagging it would break the
      recommended setup.
    - **ssh:// with a user**: ``ssh://git@github.com/org/repo.git`` — userinfo, but SSH
      authenticates by key, so the username is not a secret.

    Parsed with ``urlsplit`` rather than matched by hand: the authority ends at the first
    ``/``, ``?`` or ``#``, so a path-embedded ``@`` (``https://host/a@b``) is not misread as
    userinfo.
    """
    if "://" not in remote:
        return False  # scp-style; no userinfo component exists
    try:
        parts = urlsplit(remote)
    except ValueError:  # pragma: no cover — malformed enough that git will reject it too
        return True  # unparseable: refuse rather than guess
    if parts.scheme.lower() not in {"http", "https"}:
        return False  # ssh/git authenticate by key; a bare username is not a secret
    # `username`/`password` come from the AUTHORITY only, so a path-embedded `@` is not
    # mistaken for userinfo.
    return bool(parts.username or parts.password)


async def _handle_put_provider_config(
    request: web.Request,
    *,
    get_registry: RegistryLookup,
    merge_provider_config: Callable[[str, dict[str, Any]], dict[str, Any]],
    _audit: AuditWriter,
) -> web.StreamResponse:
    """Update one provider's NON-SECRET config (enable flag, region, ids, …).

    Two guards, both load-bearing because this file is served unauthenticated:

    1. Only keys the adapter declares in ``config_fields`` are accepted — an
       unknown key cannot become a place to stash data.
    2. Any key matching the adapter's ``secret_fields`` is REFUSED. A settings
       form that accidentally posted a token here would otherwise write it into a
       world-readable-over-the-port file; secrets must go to the keystone route.
    """
    provider_id = request.match_info.get("provider_id", "").strip()
    body = await _json_body(request)
    if body is None:
        return web.json_response(
            {"error": "request body must be a JSON object", "code": "body_not_object"}, status=400
        )

    known = {p.id: p for p in get_registry().catalog()}
    info = known.get(provider_id)
    if info is None:
        return web.json_response(
            {"error": "unknown provider", "code": "unknown_provider"}, status=404
        )

    allowed = set(info.config_fields)
    secret_names = set(info.secret_fields)
    updates: dict[str, Any] = {}
    for key, value in body.items():
        name = str(key)
        if name in secret_names:
            _audit(
                "provider_config_put",
                f"{provider_id}.{name}",
                "rejected",
                error="secret field submitted to the non-secret config route",
            )
            return web.json_response(
                {
                    "error": (
                        f"{name!r} is a secret field — use "
                        f"PUT /providers/{provider_id}/secret so it lands in the "
                        f"protected store, not the unauthenticated config file"
                    ),
                    "code": "secret_field_on_config_route",
                },
                status=400,
            )
        if name not in allowed:
            return web.json_response(
                {
                    "error": f"provider {provider_id!r} has no config field {name!r}",
                    "code": "unknown_config_field",
                },
                status=400,
            )
        # Coerce to the JSON-safe scalars the adapters read.
        updates[name] = value if isinstance(value, (bool, int, float, list)) else str(value)

    if not updates:
        return web.json_response(
            {"error": "no config fields supplied", "code": "no_recognized_fields"}, status=400
        )

    # `merge_provider_config` refuses rather than publishing over a read it could not make,
    # so this call can raise both failures. Routed through the shared mapper rather than
    # answering here: hand-rolling a second response pair beside the helper built for
    # exactly this was flagged in review (First Principles), and it was right -- two sites
    # constructing the same two responses is how their statuses drift apart.
    try:
        saved = await asyncio.to_thread(merge_provider_config, provider_id, updates)
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("ops-mission-control: provider config write refused (%s)", provider_id)
        _audit("provider_config_put", f"{provider_id}:{sorted(updates)}", "failure")
        return _store_read_refusal(exc, code="app_config")
    _audit("provider_config_put", f"{provider_id}:{sorted(updates)}", "success")
    return web.json_response({"ok": True, "provider": provider_id, "config": saved})


async def _settings_write_or_refuse(
    fn: Any,
    *args: Any,
    code: str,
    applied: dict[str, Any],
    _audit: AuditWriter,
    **kwargs: Any,
) -> web.Response | None:
    """Run one settings write off-loop. ``None`` on success, else the 503 to return.

    The keystone policy store refuses rather than publishing over a read it could not
    make, so any write reaching it can raise ``OSError``. Letting one escape gives
    aiohttp's default 500 -- a plain-text body with no ``code``, which the repo requires
    on an error response. On this route the ambiguity is the security-relevant part:
    ``mode`` and ``autonomy_rules`` ARE the authorization ceiling, so "did my change
    land?" is exactly the question the operator cannot be left guessing about. 503 rather
    than 500 because the condition is transient and retrying is correct.

    Accepts ``**kwargs`` so the one write here that takes keyword arguments
    (``policy_store.set_ceiling``, which must stay a single call to commit ``mode`` and
    ``autonomy_rules`` under one lock) goes through this same path rather than being
    spelled out a second time beside it.

    NOT every store behind this handler has that guarantee yet. ``set_top_level`` writes
    the app config, whose read still collapses a failure to ``{}`` -- so on a transient
    ``config.json`` read failure it truncates the file and returns 200 without this
    helper ever seeing an ``OSError``. The strict read that closes it is in the companion
    PR for ``providers/__init__.py``; the ``app_config_unwritable`` code here covers only
    the write-side failure that store can already raise today. Do not read this helper as
    proof of coverage it does not have.

    The refusal reports ``applied`` to the AUDIT LOG rather than in the response body.
    Phase 2 is a SEQUENCE of writes, so an earlier one may already have committed and a
    partial ceiling apply is a security state worth recording -- but the dashboard's own
    ``req`` helper reads only ``error`` from a non-2xx body and discards the rest, so a
    field there would have had no reader. The audit line has one, and it mirrors the
    ``settings_put`` line the success path already writes.
    """
    try:
        await asyncio.to_thread(fn, *args, **kwargs)
    except json.JSONDecodeError as exc:
        # `set_top_level` reaches the app config, whose update reader now refuses on a
        # malformed document -- so this helper, which caught only `OSError`, let the bare
        # 500 back in for exactly the store the docstring above said the companion PR would
        # make strict. That companion PR is this one, so the hole opened here the moment the
        # reader landed. Found in review (First Principles).
        #
        # The response comes from the shared mapper rather than being built here, so this
        # helper and the four route-level sites cannot drift on either status.
        logger.warning("ops-mission-control: settings write refused, document corrupt (%s)", code)
        _audit("settings_put", f"refused after {sorted(applied)}", "failure")
        # The caller's code names the WRITE being refused (`policy_store_unwritable`), which
        # is right for the `OSError` arm below and merged behaviour there. For corruption the
        # suffix would double up into `..._unwritable_corrupt`, so the base noun is taken and
        # the mapper names the condition: `policy_store_corrupt`.
        base = code[: -len("_unwritable")] if code.endswith("_unwritable") else code
        return _store_read_refusal(exc, code=base)
    except OSError as exc:
        logger.warning("ops-mission-control: settings write refused (%s)", code)
        _audit("settings_put", f"refused after {sorted(applied)}", "failure")
        return web.json_response({"ok": False, "error": str(exc), "code": code}, status=503)
    return None


async def _handle_put_settings(request: web.Request, *, _audit: AuditWriter) -> web.StreamResponse:
    """Update app-level settings: autonomy mode, primary flag, cycle tuning.

    ``mode`` is the autonomy ceiling, so an unrecognized value is refused rather
    than silently falling back — a typo must not quietly change what the agent is
    allowed to do.

    EVERY FIELD IS VALIDATED BEFORE ANY FIELD IS WRITTEN. The handler is two phases with
    nothing interleaved: phase 1 parses and validates into locals and can only ``return 400``;
    phase 2 performs the writes and cannot fail validation. This took three rounds to get
    right, and the shape of the mistake repeated each time, so it is worth stating plainly:

    - Round one: ``mode`` was written before the rules were validated, so ``mode=act`` plus one
      malformed rule wrote the mode, returned 400, and left the instance in ``act`` —
      activating whatever grants were already stored, from a request the operator was told had
      FAILED.
    - Round two: validating both halves of that PAIR first made the pair atomic but not the
      REQUEST — ``mode=act`` plus an over-long ``ledger_sync_remote`` still persisted ``act``
      and then 400'd. So the ceiling writes moved to the end.
    - Round three (this one): moving only the ceiling was still the wrong scope. Every other
      field had the same defect — ``{"primary_instance": false, "ledger_sync_branch": "--bad"}``
      returned 400 having already flipped leadership, which changes which instance passes the
      ``not_primary`` gate on ``POST /ledger/hygiene``. "Which field is dangerous to
      half-apply?" is the wrong question to keep re-answering; a rejected request must change
      NOTHING. Found in review each time.
    """
    body = await _json_body(request)
    if body is None:
        return web.json_response(
            {"error": "request body must be a JSON object", "code": "body_not_object"}, status=400
        )

    # ---- PHASE 1: validate everything. Only `return 400` below this line, never a write. ----

    mode: str | None = None
    if "mode" in body:
        mode = str(body["mode"]).strip()
        if mode not in MODE_ORDER:
            return web.json_response(
                {"error": f"mode must be one of {sorted(MODE_ORDER)}", "code": "invalid_mode"},
                status=400,
            )

    rules: list[dict[str, Any]] | None = None
    if "autonomy_rules" in body:
        # A rule that fails validation is REFUSED, not stored-and-ignored: `load_rules` drops
        # unparseable entries, so writing them would show the operator a saved grant that
        # silently never matches.
        ok, code, rules = await asyncio.to_thread(rotation.validate_rules, body["autonomy_rules"])
        if not ok:
            return web.json_response(
                {
                    "error": (
                        "an autonomy rule was rejected: a rule must name a source plus at "
                        "least one of resource_glob or label_match, and an act-rule may not "
                        "be a blanket grant"
                    ),
                    "code": code,
                },
                status=400,
            )

    # The rotation identity and the strict-gating flag are the OTHER two inputs to the same
    # authorization decision as `mode`/`autonomy_rules`, and they live on the same fenced floor.
    login: str | None = None
    if "schedule_github_login" in body:
        login = str(body["schedule_github_login"]).strip()
        if login and not _SAFE_LOGIN_RE.fullmatch(login):
            return web.json_response(
                {
                    "error": (
                        "schedule_github_login must be a GitHub login: letters, digits and "
                        "single hyphens, up to 39 characters"
                    ),
                    "code": "invalid_github_login",
                },
                status=400,
            )

    strict: bool | None = None
    if "schedule_strict_gating" in body:
        try:
            strict = _require_bool(body, "schedule_strict_gating")
        except _NotABool:
            return web.json_response(
                {
                    "error": "schedule_strict_gating must be true or false (a JSON boolean)",
                    "code": "invalid_field_type",
                },
                status=400,
            )

    # PagerDuty's rotation identity, fenced for the same reason and written here for the same
    # reason. Shape-checked only for length: PagerDuty ids are opaque (`PXXXXXX` today), so a
    # tighter pattern would be this app asserting a vendor format it does not own.
    pd_user: str | None = None
    if "pagerduty_user_id" in body:
        pd_user = str(body["pagerduty_user_id"]).strip()
        if len(pd_user) > _MAX_PROVIDER_ID_LEN:
            return web.json_response(
                {"error": "pagerduty_user_id is too long", "code": "value_too_long"}, status=400
            )

    # incident.io's rotation identity, on the same fence for the same reason. Length-checked
    # only: its ids are opaque ULIDs, and pinning that shape here would assert a vendor
    # format this app does not own.
    inc_user: str | None = None
    if "incidentio_user_id" in body:
        inc_user = str(body["incidentio_user_id"]).strip()
        if len(inc_user) > _MAX_PROVIDER_ID_LEN:
            return web.json_response(
                {"error": "incidentio_user_id is too long", "code": "value_too_long"}, status=400
            )

    primary: bool | None = None
    if "primary_instance" in body:
        try:
            primary = _require_bool(body, "primary_instance")
        except _NotABool:
            return web.json_response(
                {
                    "error": "primary_instance must be true or false (a JSON boolean)",
                    "code": "invalid_field_type",
                },
                status=400,
            )

    slack_enabled: bool | None = None
    if "slack_enabled" in body:
        try:
            slack_enabled = _require_bool(body, "slack_enabled")
        except _NotABool:
            return web.json_response(
                {
                    "error": "slack_enabled must be true or false (a JSON boolean)",
                    "code": "invalid_field_type",
                },
                status=400,
            )

    slack_channel: str | None = None
    if "slack_channel" in body:
        slack_channel = str(body["slack_channel"]).strip()

    notify_enabled: bool | None = None
    if "notify_enabled" in body:
        try:
            notify_enabled = _require_bool(body, "notify_enabled")
        except _NotABool:
            return web.json_response(
                {
                    "error": "notify_enabled must be true or false (a JSON boolean)",
                    "code": "invalid_field_type",
                },
                status=400,
            )

    # Shared-ledger git sync: the team's memory-exchange repo. A remote URL and a
    # branch name are not credentials (auth is the operator's own git/ssh/gh
    # config), so they belong in plain app config like the Slack channel above.
    #
    # Settable here rather than only by hand-editing ``data/config.json``:
    # ``ledger_sync.set_settings`` needs a caller, or the app's headline team
    # feature has no way in and an operator looking for "where do I point this
    # at my team repo?" finds nothing.
    wants_sync = (
        "ledger_sync_remote" in body
        or "ledger_sync_branch" in body
        or "ledger_sync_enabled" in body
    )
    remote_url = str(body["ledger_sync_remote"]).strip() if "ledger_sync_remote" in body else None
    branch_name = str(body["ledger_sync_branch"]).strip() if "ledger_sync_branch" in body else None
    try:
        sync_enabled = _require_bool(body, "ledger_sync_enabled")
    except _NotABool:
        return web.json_response(
            {
                "error": "ledger_sync_enabled must be true or false (a JSON boolean)",
                "code": "invalid_field_type",
            },
            status=400,
        )
    if remote_url is not None and len(remote_url) > _MAX_REMOTE_LEN:
        return web.json_response(
            {"error": "ledger_sync_remote is too long", "code": "value_too_long"}, status=400
        )
    # REFUSE a credential-bearing remote instead of storing it.
    #
    # `data/config.json` is served over `/api/apps/<name>/config` WITHOUT session auth,
    # and `redact_tokens` has no pattern for a PAT embedded in a URL — so
    # `https://user:ghp_xxx@github.com/org/repo.git` pasted here was persisted verbatim
    # into a world-readable file and echoed into SEL output. The frontend's
    # `displayRemote()` strips userinfo for DISPLAY, and its own docstring said outright
    # that the value is still stored and "this function changes nothing about that" — a
    # documented hole rather than a fixed one. Review blocked on it, correctly.
    #
    # Refusing rather than silently stripping: the token the operator pasted is now
    # compromised-by-paste either way, and a remote quietly rewritten to an
    # unauthenticated URL would fail to push later with no hint why. Tell them, so they
    # can rotate it and use a credential helper or SSH.
    if remote_url and _url_has_userinfo(remote_url):
        return web.json_response(
            {
                "error": (
                    "the remote URL contains an embedded username/password — remove it "
                    "and let git supply credentials (a credential helper, or an SSH "
                    "remote). Anything stored here is served unauthenticated, so treat "
                    "a token you already pasted as compromised and rotate it."
                ),
                "code": "remote_has_credentials",
            },
            status=400,
        )
    if branch_name and not _SAFE_BRANCH_RE.fullmatch(branch_name):
        # A branch name reaches a ``git`` argv. It is already passed as its own
        # argument and never interpolated into a shell string, so this is about
        # refusing option-like or whitespace-bearing values up front rather than
        # letting them surface later as a confusing sync failure.
        return web.json_response(
            {"error": "ledger_sync_branch is not a valid ref", "code": "invalid_branch_ref"},
            status=400,
        )

    numerics: dict[str, int] = {}
    for numeric_key in (
        "max_claims_per_cycle",
        "stale_after_secs",
        # Sits beside ``stale_after_secs`` because it is the same knob for the other
        # sweepable class: how long an unanswered ``needs_human`` incident may hold its
        # signal before the sweep releases it. Unset means "derive from
        # ``stale_after_secs``" (see ``store.sweep_stale``).
        "needs_human_stale_after_secs",
    ):
        if numeric_key not in body:
            continue
        try:
            numeric_value = int(body[numeric_key])
        except (TypeError, ValueError):
            return web.json_response(
                {"error": f"{numeric_key} must be an integer", "code": "invalid_field_type"},
                status=400,
            )
        if numeric_value <= 0:
            return web.json_response(
                {"error": f"{numeric_key} must be positive", "code": "value_out_of_range"},
                status=400,
            )
        numerics[numeric_key] = numeric_value

    recognized = (
        mode is not None
        or rules is not None
        or login is not None
        or strict is not None
        or primary is not None
        or slack_enabled is not None
        or slack_channel is not None
        or notify_enabled is not None
        or pd_user is not None
        or inc_user is not None
        or wants_sync
        or bool(numerics)
    )
    if not recognized:
        return web.json_response(
            {"error": "no recognized settings supplied", "code": "no_recognized_fields"}, status=400
        )

    # ---- PHASE 2: write. Everything above validated, so nothing here can 400. ----

    applied: dict[str, Any] = {}

    # Slack output. A channel ID is not a credential, so it belongs here rather
    # than in the secret store — and this app stores no Slack token at all, it
    # reuses Kiro Crew's own client (see slack_out for why).
    #
    # These three go through the refusal helper even though they do not name
    # `policy_store` here: `slack_out.set_settings` and `ledger_sync.set_settings` reach
    # `policy_store.put` INTERNALLY (their keys are operator-only, so they must), and
    # `notify_out.set_settings` reaches `set_top_level`. Guarding only the call sites that
    # spell `policy_store` left these three to answer a refused keystone write with a
    # plain 500 — the destination keys are exactly the ones an agent must not be able to
    # redirect, so they are the last place to leave the operator guessing whether their
    # change landed. Found in review (GPT 5.6).
    if slack_enabled is not None:
        refused = await _settings_write_or_refuse(
            slack_out.set_settings,
            code="policy_store_unwritable",
            applied=applied,
            _audit=_audit,
            enabled=slack_enabled,
        )
        if refused is not None:
            return refused
        applied["slack_enabled"] = slack_enabled

    if slack_channel is not None:
        refused = await _settings_write_or_refuse(
            slack_out.set_settings,
            code="policy_store_unwritable",
            applied=applied,
            _audit=_audit,
            channel_id=slack_channel,
        )
        if refused is not None:
            return refused
        applied["slack_channel"] = slack_channel

    # Local desktop notifications. Nothing to configure beyond on/off — there is no
    # destination and no credential, which is the whole point of this channel.
    if notify_enabled is not None:
        refused = await _settings_write_or_refuse(
            notify_out.set_settings,
            code="app_config_unwritable",
            applied=applied,
            _audit=_audit,
            enabled=notify_enabled,
        )
        if refused is not None:
            return refused
        applied["notify_enabled"] = notify_enabled

    if wants_sync:
        # Deferred import, matching the hygiene handler (``ledger.py``): ``ledger_sync`` pulls in
        # the git/sandbox machinery, and this module is imported at gateway start.
        from kiro_crew.apps.builtins.ops_mission_control.backend import ledger_sync

        refused = await _settings_write_or_refuse(
            ledger_sync.set_settings,
            code="policy_store_unwritable",
            applied=applied,
            _audit=_audit,
            enabled=sync_enabled,
            remote_url=remote_url,
            branch_name=branch_name,
        )
        if refused is not None:
            return refused
        for sync_key, sync_value in (
            ("ledger_sync_remote", remote_url),
            ("ledger_sync_branch", branch_name),
            ("ledger_sync_enabled", sync_enabled),
        ):
            if sync_value is not None:
                applied[sync_key] = sync_value

    for numeric_key, numeric_value in numerics.items():
        refused = await _settings_write_or_refuse(
            set_top_level,
            numeric_key,
            numeric_value,
            code="app_config_unwritable",
            applied=applied,
            _audit=_audit,
        )
        if refused is not None:
            return refused
        applied[numeric_key] = numeric_value

    # The authorization inputs go to the keystone store, not `set_top_level` (which writes
    # the agent-writable config.json): they ARE the security ceiling, and this authenticated PUT
    # is their sole writer. See `policy_store`.
    if primary is not None:
        refused = await _settings_write_or_refuse(
            policy_store.put,
            policy_store.PRIMARY_KEY,
            primary,
            code="policy_store_unwritable",
            applied=applied,
            _audit=_audit,
        )
        if refused is not None:
            return refused
        applied["primary_instance"] = primary
    # ONE call for both halves, so they commit under ONE lock acquisition. `mode` and
    # `autonomy_rules` are a single authorization decision (`effective = min(app_mode,
    # rule_mode)`), and two separately-locked writes let a CONCURRENT settings PUT interleave
    # between them — request A's `act` landing with request B's broader rules, authorizing a
    # write neither operator asked for. The two-phase validate-then-write discipline above
    # cannot close that window, because the interleaving comes from another request rather
    # than from ordering inside this one. Found in review (GPT 5.6).
    if mode is not None or rules is not None:
        refused = await _settings_write_or_refuse(
            policy_store.set_ceiling,
            code="policy_store_unwritable",
            applied=applied,
            _audit=_audit,
            mode=mode,
            rules=rules,
        )
        if refused is not None:
            return refused
        if mode is not None:
            applied["mode"] = mode
        if rules is not None:
            applied["autonomy_rules"] = rules
    if login is not None:
        from kiro_crew.apps.builtins.ops_mission_control.backend.providers import schedule_file

        refused = await _settings_write_or_refuse(
            policy_store.put,
            policy_store.SCHEDULE_LOGIN_KEY,
            login,
            code="policy_store_unwritable",
            applied=applied,
            _audit=_audit,
        )
        if refused is not None:
            return refused
        # A changed identity invalidates the cached `gh` answer, which is only a fallback for
        # an unset login — leaving it would keep answering with the previous operator.
        await asyncio.to_thread(schedule_file.reset_login_cache)
        applied["schedule_github_login"] = login
    if strict is not None:
        refused = await _settings_write_or_refuse(
            policy_store.put,
            policy_store.SCHEDULE_STRICT_KEY,
            strict,
            code="policy_store_unwritable",
            applied=applied,
            _audit=_audit,
        )
        if refused is not None:
            return refused
        applied["schedule_strict_gating"] = strict
    if pd_user is not None:
        refused = await _settings_write_or_refuse(
            policy_store.put,
            policy_store.PAGERDUTY_USER_KEY,
            pd_user,
            code="policy_store_unwritable",
            applied=applied,
            _audit=_audit,
        )
        if refused is not None:
            return refused
        applied["pagerduty_user_id"] = pd_user
    if inc_user is not None:
        refused = await _settings_write_or_refuse(
            policy_store.put,
            policy_store.INCIDENTIO_USER_KEY,
            inc_user,
            code="policy_store_unwritable",
            applied=applied,
            _audit=_audit,
        )
        if refused is not None:
            return refused
        applied["incidentio_user_id"] = inc_user

    _audit("settings_put", f"{sorted(applied)}", "success")
    return web.json_response({"ok": True, "applied": applied})


async def _handle_put_secret(
    request: web.Request,
    *,
    get_registry: RegistryLookup,
    put_secret: Callable[[str, str, str], None],
) -> web.StreamResponse:
    """Store a provider secret. Write-only: the value is never readable back."""
    owner_denied = await require_owner_dashboard_request(request, "ops_mission_control.put_secret")
    if owner_denied is not None:
        return owner_denied
    provider_id = request.match_info.get("provider_id", "").strip()
    body = await _json_body(request)
    if body is None:
        return web.json_response(
            {"error": "request body must be a JSON object", "code": "body_not_object"}, status=400
        )
    field_name = str(body.get("field", "")).strip()
    value = str(body.get("value", ""))
    if not provider_id or not field_name:
        return web.json_response(
            {"error": "provider_id and field are required", "code": "missing_required_field"},
            status=400,
        )
    if not value:
        return web.json_response(
            {"error": "value must not be empty", "code": "missing_required_field"}, status=400
        )
    if len(value) > _MAX_SECRET_LEN:
        return web.json_response(
            {"error": "value is too long", "code": "value_too_long"}, status=400
        )

    known = {p.id: p for p in get_registry().catalog()}
    info = known.get(provider_id)
    if info is None:
        return web.json_response(
            {"error": "unknown provider", "code": "unknown_provider"}, status=404
        )
    if field_name not in info.secret_fields:
        # Reject unknown field names so the keystone file cannot be used as
        # arbitrary agent-inaccessible storage.
        return web.json_response(
            {
                "error": f"provider {provider_id!r} has no secret field {field_name!r}",
                "code": "unknown_secret_field",
            },
            status=400,
        )

    try:
        await asyncio.to_thread(put_secret, provider_id, field_name, value)
    except json.JSONDecodeError as exc:
        # The store's update reader refuses a corrupt document rather than
        # replacing it, so this handler can see that refusal. Same shared mapper
        # as the settings writes, for the same
        # reason: translating it at some handlers and not others is worse than not
        # translating it anywhere. 500-with-code rather than the OSError arm's 503:
        # corruption does not clear on retry, it needs a person to repair the file.
        logger.warning("ops-mission-control: secret save refused, store corrupt")
        return _store_read_refusal(exc, code="secret_store")
    except OSError as exc:
        # Reported, not raised — the same shape ``_handle_rotation_arm`` uses for a
        # refusing cron store, and for the same reason its comment gives: escaping
        # here becomes aiohttp's default 500, a plain-text body with no ``code`` for
        # the UI to branch on. On THIS route the ambiguity is the security-relevant
        # part: the operator cannot tell whether the credential they just typed is
        # now stored. 503 rather than 500 because the condition is transient and
        # retrying is the correct client behaviour.
        logger.warning("ops-mission-control: secret save refused, store unwritable")
        return web.json_response(
            {"ok": False, "error": str(exc), "code": "secret_store_unwritable"}, status=503
        )
    return web.json_response({"ok": True, "provider": provider_id, "field": field_name})


async def _handle_delete_secret(
    request: web.Request, *, delete_secret: Callable[[str], bool]
) -> web.StreamResponse:
    owner_denied = await require_owner_dashboard_request(
        request, "ops_mission_control.delete_secret"
    )
    if owner_denied is not None:
        return owner_denied
    provider_id = request.match_info.get("provider_id", "").strip()
    if not provider_id:
        return web.json_response(
            {"error": "provider_id is required", "code": "missing_required_field"}, status=400
        )
    try:
        removed = await asyncio.to_thread(delete_secret, provider_id)
    except json.JSONDecodeError as exc:
        # The revocation route needs the corruption refusal MORE than the save
        # route: on the old lenient read a corrupt store reported the provider
        # absent, so the operator was told the token was already gone while it sat
        # readable in the corrupt bytes -- and a rewrite here would then destroy
        # the only copy. A coded refusal leaves them correctly believing the token
        # is still live and the file worth repairing.
        logger.warning("ops-mission-control: secret revocation refused, store corrupt")
        return _store_read_refusal(exc, code="secret_store")
    except OSError as exc:
        # The revocation route needs this MORE than the save route. Its whole purpose
        # is to let an operator stop trusting a credential, and the failure this
        # replaces is the one where they were told the token was already gone. A
        # coded refusal is the only answer that leaves them correctly believing the
        # token is still live.
        logger.warning("ops-mission-control: secret revocation refused, store unwritable")
        return web.json_response(
            {"ok": False, "error": str(exc), "code": "secret_store_unwritable"}, status=503
        )
    return web.json_response({"ok": True, "removed": removed})


async def _handle_rotation_arm(
    request: web.Request, *, get_registry: RegistryLookup
) -> web.StreamResponse:
    """Arm/disarm this app's crons to match the tier map — server-side, not agent-driven.

    The whole point is that the agent does not decide WHICH crons to pause. It POSTs here;
    ``rotation.apply_tiers`` computes the tier map and refuses to pause an always-tier job
    unconditionally. See that function for why prose in the SOP was not sufficient.
    """
    state = request.app.get("state")
    cron_service = getattr(state, "crons", None)
    if cron_service is None:
        return web.json_response(
            {
                "ok": False,
                "error": "cron service unavailable",
                "code": "cron_service_unavailable",
            },
            status=503,
        )
    shift = await get_registry().resolve_shift()
    try:
        return web.json_response(await rotation.apply_tiers(shift, cron_service))
    except CronStoreUnreadable as exc:
        # A store refusing writes fails the arm. Reported, not raised: escaping
        # here becomes a 500, and reported rather than skipped-per-job because a
        # partial arm returned as `ok: True` is exactly the quiet-versus-broken
        # conflation the server-side tier logic exists to prevent -- a gated
        # instance that believes it armed is indistinguishable from one that did.
        logger.warning("ops-mission-control: rotation arm refused, cron store unreadable")
        return web.json_response(
            {"ok": False, "error": str(exc), "code": "cron_store_unreadable", "changed": []},
            status=503,
        )
