"""The current-state readers: what a change card previews, verifies and undoes.

:func:`read_state` and :func:`read_context` are the gateway's own read of what a card
replaces, per kind (settings, schedules, crewmates, goals, capabilities, templates, MCP
servers, connections, secrets, folders), returning names, flags and settings values
only, never a secret value, an env value or a token. Beside them: the comparable
snapshot a card records and checks before an apply or an undo, whether the write an
apply made shows in the state read after it, and :func:`build`, which turns a proposal
into a card. A new kind's state read goes here.

Served through :mod:`kiro_crew.dashboard.change_cards`, which callers import and tests
patch; its mirror (:mod:`kiro_crew._facade_mirror`) carries such a patch to this module,
and this module never imports it.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import hmac
import json
import os
import re
import time
import urllib.parse
from collections.abc import Iterator
from typing import Any

from kiro_crew import change_card_catalog as catalog
from kiro_crew.dashboard.change_cards_records import (
    CardError,
    _jsonable,
    canonical,
    check_param_text,
)
from kiro_crew.dashboard.change_cards_settings import (
    check_target_params,
    read_setting_value,
    resolve_setting,
)


def template_auto_approved_servers(template: str) -> list[str]:
    """The user-installed MCP servers *template* runs without asking, sorted.

    A new crewmate is bound to the template, so it inherits each bare
    ``@server`` grant in the template's ``allowedTools`` (already narrowed by the
    governance ceiling when the spec was written). Kiro Crew's own managed
    servers are left out: they are the product's own tools, not a connection
    the user installed. Unreadable specs report none.
    """
    from kiro_crew.config.paths import kiro_agents_dir

    try:
        spec = json.loads((kiro_agents_dir() / f"{template}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    allowed = spec.get("allowedTools") if isinstance(spec, dict) else None
    servers = {
        ref[1:]
        for ref in allowed or ()
        if isinstance(ref, str) and ref.startswith("@") and "/" not in ref and len(ref) > 1
    }
    return sorted(s for s in servers if not s.startswith("kirocrew"))


def _crewmate_key(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def _cron_job_fields(job: Any) -> dict[str, Any]:
    schedule = getattr(job, "schedule", None)
    return {
        "name": getattr(job, "name", None),
        "message": getattr(job, "message", None),
        "cron_expr": getattr(schedule, "cron_expr", None),
        "timezone": getattr(job, "timezone", "") or "",
    }


#: A schedule's configuration a person sets or edits -- never what running it
#: updates (last result, counters, retry stamps). A change to any of these
#: after a card created the schedule makes that card's Undo stale.
_CRON_CONFIG_FIELDS = (
    "name",
    "message",
    "schedule",
    "channel",
    "thread_ts",
    "user_paused",
    "delete_after_run",
    "context_enabled",
    "agent_id",
    "member_id",
    "memory_store",
    "approval_mode",
    "silent",
    "skip_dates",
    "timezone",
    "persistent_session",
    "minimal_context",
    "hide_in_chat",
    "folder_id",
    "model",
    "chat_folder_id",
    "env",
    "timeout_secs",
    "strict_schedule",
    "script",
    "command",
    "timeout",
    "secret_env",
)


def _cron_job_revision(job: Any) -> str:
    return _revision_digest({k: getattr(job, k, None) for k in _CRON_CONFIG_FIELDS})


def cron_job_revision(job: Any) -> str:
    """Public fingerprint of a cron job's schedule config, matching the digest
    :func:`read_state` records for a ``schedule.create`` card's ``after``.

    The card-Undo compare-and-delete (``api_cron_delete``) passes this to the
    cron store so the live job is fingerprinted the same way the card snapshot
    was; identical digests mean the schedule still holds what the card made.
    """
    return _cron_job_revision(job)


def next_run_at(cron_expr: str, timezone: str | None) -> float | None:
    from kiro_crew.cron_service.model import CronJob, CronSchedule
    from kiro_crew.cron_service.schedule import compute_next_run_ts

    job = CronJob(
        id="card-preview",
        name="preview",
        message="",
        schedule=CronSchedule(kind="cron", cron_expr=cron_expr),
    )
    job.timezone = timezone or ""
    try:
        return compute_next_run_ts(job)
    except Exception:
        return None


def one_shot_context(at: str, timezone: str) -> dict[str, Any]:
    """Resolve a one-shot card's local ``at`` to the instant ``POST /api/crons`` takes.

    The zone is the card's own ``timezone``, or the CONFIGURED one when the card
    names none -- the same default ``parse_time_string`` reads a wall clock in,
    so the card and the route agree on the instant. A time already gone is
    refused here, before a card is shown, exactly as the route would refuse it
    (``at_in_past``). ``once`` and ``run_at_local`` are display facts the card
    record carries so the dashboard can say "Runs once" with the local time.
    """
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from kiro_crew.cron_service.schedule import (
        _wall_clock_resolution_error,
        get_local_tz,
        is_valid_timezone,
    )

    tz = timezone or get_local_tz()[0]
    if not is_valid_timezone(tz):
        raise catalog.CardCatalogError("invalid_timezone", f"unknown timezone '{tz[:64]}'")
    zone = ZoneInfo(tz)
    local = datetime.fromisoformat(at).replace(tzinfo=zone)
    skipped = _wall_clock_resolution_error(local, zone, tz)
    if skipped is not None:
        raise catalog.CardCatalogError("at_not_in_zone", skipped.removeprefix("Error: "))
    at_ts = local.timestamp()
    if at_ts <= time.time():
        raise catalog.CardCatalogError("at_in_past", "that time has already passed")
    return {
        "next_run_at": at_ts,
        "timezone": tz,
        "once": True,
        "run_at_local": at,
        "at_ts": at_ts,
    }


def _mcp_global_entry(name: str) -> tuple[bool, dict[str, Any] | None]:
    from kiro_crew.dashboard.handlers import mcp as mcp_handlers

    try:
        data = json.loads(mcp_handlers._GLOBAL_MCP_JSON.read_text(encoding="utf-8"))
    except FileNotFoundError:
        data = {}
    except (OSError, ValueError):
        data = {}
    servers = data.get("mcpServers") if isinstance(data, dict) else None
    servers = servers if isinstance(servers, dict) else {}
    try:
        key = mcp_handlers._config_key_for(servers, name)
    except Exception:
        key = None
    if key is not None and isinstance(servers.get(key), dict):
        return True, servers[key]
    from kiro_crew.mcp_discovery import list_servers

    known = {s.name for s in list_servers()}
    return (name in known), None


def _server_reach(name: str) -> dict[str, str]:
    """What MCP server *name* reaches, for a card row: a URL's host or a command's name."""
    _exists, entry = _mcp_global_entry(name)
    entry = entry or {}
    if isinstance(entry.get("url"), str):
        host = urllib.parse.urlsplit(entry["url"]).hostname or ""
        return {"name": name, "host": host} if host else {"name": name}
    if isinstance(entry.get("command"), str) and entry["command"].strip():
        return {"name": name, "command": os.path.basename(entry["command"].strip())}
    return {"name": name}


def _mcp_exists(name: str) -> bool:
    from kiro_crew.dashboard.handlers.mcp import _find_server_spec_anywhere

    return _find_server_spec_anywhere(name) is not None


#: Recorded for a scope whose file exists but cannot be read: an unknown is
#: never taken as "not there".
_MCP_SCOPE_UNREADABLE = "unreadable"


def _mcp_value_key() -> bytes:
    from kiro_crew.config.loader import config_dir
    from kiro_crew.secrets import SecretVault

    return SecretVault(config_dir()).derive_subkey("change-card-mcp-fingerprint")


def _mcp_scopes(name: str) -> dict[str, str]:
    """Each scope an uninstall would delete *name* from, with a fingerprint of
    the definition there.

    ``env`` and ``headers`` values enter only as keyed digests under a vault
    subkey, so an edited value changes the fingerprint while the stored
    fingerprint cannot be tested against a guessed credential. A scope without
    the name is absent; one that cannot be read is :data:`_MCP_SCOPE_UNREADABLE`.
    """
    from kiro_crew.dashboard.handlers.mcp import _config_entry_for, _uninstall_scope_files

    key: bytes | None = None
    out: dict[str, str] = {}
    for label, path in _uninstall_scope_files():
        if label == "agent" or label.endswith("Agent"):
            # A rendered agent file is Kiro Crew's own merge output, rewritten
            # on every rebuild; the uninstall strips it with the scopes above.
            continue
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            continue
        except (OSError, UnicodeDecodeError):
            out[label] = _MCP_SCOPE_UNREADABLE
            continue
        try:
            data = json.loads(raw) if raw.strip() else {}
        except ValueError:
            out[label] = _MCP_SCOPE_UNREADABLE
            continue
        servers = data.get("mcpServers", {}) if isinstance(data, dict) else {}
        entry = _config_entry_for(servers, name)
        if not isinstance(entry, dict):
            continue
        shape: dict[str, Any] = {}
        for k, v in entry.items():
            if k in ("env", "headers") and isinstance(v, dict):
                key = key if key is not None else _mcp_value_key()
                shape[k] = {
                    str(ek): hmac.new(
                        key, canonical(ev).encode("utf-8"), hashlib.sha256
                    ).hexdigest()
                    for ek, ev in v.items()
                }
            else:
                shape[k] = v
        out[label] = _revision_digest(shape)
    return out


def _mcp_undo_names(rec: dict[str, Any]) -> list[str]:
    names: list[str] = []
    for s in (rec.get("plan") or {}).get("undo") or []:
        for change in (s.get("body") or {}).get("changes") or []:
            if isinstance(change, dict) and isinstance(change.get("name"), str):
                names.append(change["name"])
    return names


def after_shows_the_write(
    kind: str,
    params: dict[str, Any],
    after: Any,
    evidence: list[dict[str, Any]] | None = None,
) -> bool:
    """Whether the post-apply read still holds what this card wrote.

    Undo's baseline is the value the card WROTE, never whatever the read found:
    a save made in another tab between the write and the read would otherwise
    become the baseline, and Undo would then pass its check and erase it. For
    the kinds that update values in place, the fields this card set must read
    back as it set them; a create is identified by its own evidence instead.
    Applies only to what Undo writes back: a field the card did not touch is
    never restored, so another edit there is left alone.
    """
    if not isinstance(after, dict):
        return False
    p = params
    ev = evidence or []

    def same(a: Any, b: Any) -> bool:
        return canonical(_jsonable(a)) == canonical(_jsonable(b))

    if kind == catalog.KIND_SETTING_CHANGE:
        value = after.get("value")
        if "value" in p:
            return same(value, p["value"])
        items = value if isinstance(value, list) else []
        present = p.get("item") in items
        return present if p.get("op") == catalog.LIST_OP_ADD else not present
    if kind in (
        catalog.KIND_SCHEDULE_UPDATE,
        catalog.KIND_CREWMATE_UPDATE,
        catalog.KIND_TEMPLATE_UPDATE,
    ):
        fields = after.get("fields")
        if not isinstance(fields, dict):
            return False
        return all(same(fields.get(k), v) for k, v in (p.get("fields") or {}).items())
    if kind in catalog.CREWMATE_CREATING_KINDS:
        # Undo deletes the crewmate this card made. The only safe baseline is the
        # IMMUTABLE member_id the card's own create returned (step 0's evidence):
        # a name is reusable, so an owner who deleted and recreated the same name
        # between apply and Undo leaves a different crewmate wearing it. Offer the
        # Undo only while the live member_id still equals the one we created; a
        # replacement reads a different id (or none) and the Undo is withheld so
        # it cannot delete the replacement and its crew log.
        created = ev[0].get("member_id") if ev else None
        if not isinstance(created, str) or not created:
            return False
        if not (bool(after.get("exists")) and after.get("member_id") == created):
            return False
        return kind != catalog.KIND_CREWMATE_IMPORT or _import_after_is_written(p, after, ev)
    if kind == catalog.KIND_CREWMATE_CHECK_INS:
        sched = after.get("schedules")
        return isinstance(sched, dict) and all(
            sched.get(k) == v for k, v in p.items() if k != "member"
        )
    if kind in (catalog.KIND_CREWMATE_GOAL, catalog.KIND_CREWMATE_KEY_RESULTS):
        # A card can carry the scrubbed form of text the record keeps raw
        # (``member_goals.keep_unscrubbed``), so the write reads back in its
        # scrubbed form: compare both sides scrubbed.
        from kiro_crew import member_goals

        if kind == catalog.KIND_CREWMATE_GOAL:
            gid = p.get("goal_id") or (ev[0].get("goal_id") if ev else None)
            got = next(
                (
                    g.get("text")
                    for g in after.get("goals") or []
                    if isinstance(g, dict) and g.get("id") == gid
                ),
                None,
            )
            want = p.get("long_term")
            if not isinstance(got, str):
                return False
            return got == want or member_goals.scrub_text(got) == member_goals.scrub_text(want)
        got_items, want_items = after.get("key_results"), p.get("items")
        records = after.get(KR_RECORDS_KEY)
        if isinstance(records, list) and isinstance(want_items, list):
            # A key result the card linked to goals must read back with those links.
            for rec_item, want_item in zip(records, want_items):
                if (
                    isinstance(want_item, dict)
                    and "goal_ids" in want_item
                    and (
                        not isinstance(rec_item, dict)
                        or rec_item.get("goal_ids") != want_item["goal_ids"]
                    )
                ):
                    return False
        if member_goals.key_result_shape(got_items) == member_goals.key_result_shape(want_items):
            return True

        def scrubbed(items: Any) -> list[dict[str, str]]:
            return [
                {k: member_goals.scrub_text(v) for k, v in i.items()}
                for i in member_goals.key_result_shape(items)
            ]

        return isinstance(got_items, list) and scrubbed(got_items) == scrubbed(want_items)
    if kind == catalog.KIND_CREWMATE_CAPABILITIES:
        # The write landed at the revision the PUT returned (step 1's evidence);
        # Undo inverts that revision. If the post-apply read is at a different
        # revision, another tab wrote after us and inverting would erase it, so
        # there is no Undo.
        written = ev[1].get("revision") if len(ev) > 1 else None
        return bool(written) and after.get("revision") == written
    if kind == catalog.KIND_TEMPLATE_MCP_SERVER:
        # Undo removes the record this card wrote: the same card id and entry.
        written = ev[0].get("card_id") if ev else None
        return (
            bool(written)
            and after.get("present") is True
            and after.get("card_id") == written
            and same(after.get("entry"), p.get("entry"))
        )
    if kind == catalog.KIND_MCP_TOGGLE:
        return after.get("enabled") is bool(p.get("enabled"))
    if kind == catalog.KIND_TRUST_APP:
        return after.get("trusted") is True
    if kind == catalog.KIND_DENIED_COMMAND and p.get("action") == "toggle":
        return after.get("enabled") is bool(p.get("enabled"))
    if kind == catalog.KIND_FOLDER_STEERING:
        # The card carries each directory as the route stores it
        # (resolve_params), so the read-back matches unless another save landed.
        return same(after.get("steering_dirs"), p.get("steering_dirs"))
    return True


#: Where a ``crewmate.key_results`` snapshot keeps the full records its Undo restores.
KR_RECORDS_KEY = "key_results_records"


def comparable_state(kind: str, state: Any) -> Any:
    """*state* as the conflict checks compare it.

    A ``crewmate.key_results`` snapshot is compared on the ``{text, target}``
    shape, ids and goal links of its records (:data:`KR_RECORDS_KEY`), never
    their readings: a fresh reading between preview, apply and Undo is not a
    conflict, but new ids or changed goal links are.
    """
    if kind == catalog.KIND_CREWMATE_KEY_RESULTS and isinstance(state, dict):
        out = {k: v for k, v in state.items() if k != KR_RECORDS_KEY}
        records = state.get(KR_RECORDS_KEY)
        if isinstance(records, list):
            out["ids"] = [r.get("id") for r in records if isinstance(r, dict)]
            # Which goals each one serves is part of what a card changes.
            out["links"] = [sorted(r.get("goal_ids") or []) for r in records if isinstance(r, dict)]
        return out
    return state


def undo_snapshot_changed(kind: str, current: Any, after: Any) -> bool:
    """Whether the state an Undo is about to reverse differs from what the card made.

    For an MCP add or install, the comparison is per server and per scope: a
    scope already emptied (an earlier Undo got that far) is done; a scope still
    holding the server must hold what the card left there, and an unreadable or
    newly added scope is a change.
    """
    if kind not in (catalog.KIND_MCP_INSTALL, catalog.KIND_MCP_ADD_CUSTOM):
        return canonical(comparable_state(kind, current)) != canonical(
            comparable_state(kind, after)
        )
    cur, then = current or {}, after or {}

    def changed(now: dict[str, str], was: dict[str, str]) -> bool:
        return any(
            digest == _MCP_SCOPE_UNREADABLE or was.get(label) != digest
            for label, digest in now.items()
        )

    if kind == catalog.KIND_MCP_INSTALL:
        return changed(cur.get("scopes") or {}, then.get("scopes") or {})
    now_all, then_all = cur.get("scopes") or {}, then.get("scopes") or {}
    return any(changed(now_all.get(n) or {}, then_all.get(n) or {}) for n in now_all)


#: The spec fields a person edits after a crewmate exists (goal, prompt,
#: capabilities). A change to any of them makes an earlier card's Undo stale.
_CREWMATE_SPEC_FIELDS = (
    "description",
    "prompt",
    "tools",
    "allowedTools",
    "mcpServers",
    "resources",
    "model",
)


def _revision_digest(value: Any) -> str:
    return hashlib.sha256(canonical(_jsonable(value)).encode("utf-8")).hexdigest()[:16]


def _crewmate_revision(agent: Any) -> str:
    """A fingerprint of a crewmate's config entry and its spec's editable fields."""
    from kiro_crew.dashboard.handlers.agents import _agent_detail_candidates

    specs = _agent_detail_candidates(getattr(agent, "kiro_agent", "") or "")
    spec = specs[0][1] if len(specs) == 1 and isinstance(specs[0][1], dict) else {}
    return _revision_digest(
        {"config": agent, "spec": {k: spec.get(k) for k in _CREWMATE_SPEC_FIELDS}}
    )


def _secret_revision(vault: Any, name: str) -> str:
    """A fingerprint of the stored entry: it changes when the value is replaced.

    The entry is ciphertext under a fresh nonce, so its digest says nothing
    about the value itself.
    """
    from kiro_crew.secrets.vault import entry_revision

    entry = vault._load_entries().get(name)  # noqa: SLF001 - read-only fingerprint
    return entry_revision(entry) if isinstance(entry, dict) else ""


def _template_state_with_skills(params: dict[str, Any], state: Any) -> dict[str, Any]:
    """A template's snapshot when the card changes its skills.

    ``skills`` is not a stored spec field: the template editor projects it from
    the spec's ``resources``, so the snapshot reads it the same way (the prior
    mapping is what Undo restores) and fingerprints ``resources`` so any later
    skill edit makes the card stale.
    """
    from kiro_crew.dashboard.handlers._shared import agent_skill_views
    from kiro_crew.dashboard.handlers.agents import _agent_detail_candidates

    candidates = _agent_detail_candidates(params["template"])
    if len(candidates) != 1:
        return {"exists": False, "ambiguous": len(candidates) > 1}
    path, spec = candidates[0]
    spec = spec if isinstance(spec, dict) else {}
    fields = {k: _jsonable(spec.get(k)) for k in params["fields"]}
    fields["skills"] = agent_skill_views(spec, path, state)[0]
    return {
        "exists": True,
        "fields": fields,
        "resources": _revision_digest(spec.get("resources")),
    }


def _crewmate_identity(member: str) -> dict[str, Any]:
    """``{exists, slug, member_id}`` of *member*, the identity a page-offer card compares."""
    from kiro_crew import members as members_mod
    from kiro_crew.config.loader import KiroCrewConfig

    cfg = KiroCrewConfig.load()
    agent = cfg.agents.get(member)
    if agent is None:
        return {"exists": False}
    return {
        "exists": True,
        "slug": members_mod.member_slug(member, cfg),
        "member_id": getattr(agent, "member_id", "") or "",
    }


def _page_offer_snapshot(member: str) -> dict[str, Any]:
    """The crewmate's identity and the page its offer would replace.

    The page is part of what a yes is compared against, so a template the person
    adopts after the card was raised refuses the yes instead of being replaced.
    """
    ident = _crewmate_identity(member)
    if not ident.get("exists") or not ident.get("slug"):
        return ident
    from kiro_crew import dashboard_agentic

    return {**ident, "page": dashboard_agentic.page_offer_target(str(ident["slug"]))}


def _page_offer_context(member: str, before: dict[str, Any]) -> dict[str, Any]:
    """What a page-offer card shows, refusing one the crewmate may not raise.

    Refused when the crewmate has no goal and confirmed milestones yet (``not_ready``),
    or is not on a template page (``not_on_a_template``: nothing on its tab, where it
    writes its page with no card, or a page of its own). Otherwise the goal the page
    is built around and the template it would replace. Asked-once is checked where
    the card is raised (``member_self._page_offer_claim``), not here: a re-preview of
    the card that wrote the record must not be refused by it.
    """
    from kiro_crew import dashboard_agentic, member_goals
    from kiro_crew.dashboard import goal_reminder

    slug = str(before.get("slug") or "")
    if not slug:
        return {"label": _crewmate_label(member)}
    try:
        rec = member_goals.read_goals(slug, member)
    except member_goals.GoalsUnreadable:
        raise catalog.CardCatalogError(
            "goals_unreadable", "this crewmate's goal record cannot be read"
        ) from None
    if not goal_reminder.knows_its_page(rec):
        raise catalog.CardCatalogError(
            "not_ready", "offer a Dashboard page once you have a goal and confirmed milestones"
        )
    target = dashboard_agentic.page_offer_target(slug)
    if target is None or not target.get("replaces"):
        raise catalog.CardCatalogError(
            "not_on_a_template",
            "this offer is for a crewmate on a template page; with nothing on your tab, "
            "write your page directly, and with a page of your own, you already have one",
        )
    goals = rec.get("goals") or []
    return {
        "label": _crewmate_label(member),
        "goal": member_goals.scrub_text(goals[0]["text"]) if goals else "",
        "replaces": target.get("replaces", ""),
    }


def _goal_state(member: str, kind: str) -> dict[str, Any]:
    """The crewmate's slug and the part of its goal a goal card replaces.

    A ``crewmate.goal`` card compares the goals (ids and raw text); a
    ``crewmate.key_results`` card the ``{text, target}`` of each key result (a
    fresh reading between preview and apply is not a conflict).

    The member id is part of the compared snapshot: a crewmate deleted and made
    again under the same name between preview and apply reads as a change.
    """
    from kiro_crew import member_goals
    from kiro_crew import members as members_mod
    from kiro_crew.config.loader import KiroCrewConfig

    cfg = KiroCrewConfig.load()
    agent = cfg.agents.get(member)
    if agent is None:
        return {"exists": False}
    slug = members_mod.member_slug(member, cfg)
    try:
        rec = member_goals.read_goals(slug, member)
    except member_goals.GoalsUnreadable:
        raise catalog.CardCatalogError(
            "goals_unreadable", "this mate's goal record cannot be read"
        ) from None
    out: dict[str, Any] = {
        "exists": True,
        "slug": slug,
        "member_id": getattr(agent, "member_id", "") or "",
    }
    if kind == catalog.KIND_CREWMATE_GOAL:
        # Raw: a card Undo restores this text (REQ_CARD_RAW_RESTORE); the plan
        # and the change line carry it scrubbed.
        out["goals"] = [{"id": g["id"], "text": g["text"]} for g in rec["goals"]]
    elif kind == catalog.KIND_CREWMATE_CHECK_INS:
        out["has_goal"] = member_goals.has_goal(rec)
        out["schedules"] = {k: rec["schedules"].get(k) for k in catalog.CHECK_IN_FIELDS}
    else:
        out["has_goal"] = member_goals.has_goal(rec)
        # Only the ids: which goals exist and their numbers. A goal's wording
        # changing between preview and apply is not this card's conflict.
        out["goals"] = [{"id": g["id"]} for g in rec["goals"]]
        out["key_results"] = member_goals.key_result_shape(rec["key_results"])
        # The whole records, ids and readings included, for the card's Undo to
        # put back; only their ids are compared (:func:`comparable_state`).
        out[KR_RECORDS_KEY] = [dict(k) for k in rec["key_results"]]
    return out


def _sync_read_state(
    kind: str, params: dict[str, Any], evidence: list[dict[str, Any]]
) -> dict[str, Any]:
    p = params
    ev0 = evidence[0] if evidence else {}
    if kind == catalog.KIND_SETTING_CHANGE:
        target = resolve_setting(p)
        return {
            "store": target["store"],
            "key": target["key"],
            "label": target["label"],
            "value": read_setting_value(target),
        }
    if kind in (*catalog.CREWMATE_CREATING_KINDS, catalog.KIND_CREWMATE_UPDATE):
        from kiro_crew.config.loader import KiroCrewConfig

        agents = KiroCrewConfig.load().agents
        if kind in catalog.CREWMATE_CREATING_KINDS:
            key = ev0.get("name") if isinstance(ev0.get("name"), str) else None
            if key:
                agent = agents.get(key)
                return {
                    "exists": agent is not None,
                    "member_id": getattr(agent, "member_id", "") if agent else "",
                    "revision": _crewmate_revision(agent) if agent else "",
                }
            taken = p["name"] in agents or _crewmate_key(p["name"]) in agents
            taken = taken or any(
                getattr(a, "display_name", "") == p["name"] for a in agents.values()
            )
            inherited = template_auto_approved_servers(catalog.DEFAULT_CREWMATE_TEMPLATE)
            out = {"exists": bool(taken), "inherited_auto_approve": inherited}
            if kind == catalog.KIND_CREWMATE_IMPORT:
                out["inherited_reach"] = [_server_reach(name) for name in inherited]
            return out
        agent = agents.get(p["name"])
        if agent is None:
            return {"exists": False}
        return {
            "exists": True,
            # The immutable identity: a crewmate deleted and re-made under the
            # same name reads as a change, so the old card's Undo is refused.
            "member_id": getattr(agent, "member_id", "") or "",
            "fields": {k: _jsonable(getattr(agent, k, None)) for k in p["fields"]},
        }
    if kind in (
        catalog.KIND_CREWMATE_GOAL,
        catalog.KIND_CREWMATE_KEY_RESULTS,
        catalog.KIND_CREWMATE_CHECK_INS,
    ):
        return _goal_state(p["member"], kind)
    if kind == catalog.KIND_CREWMATE_DASHBOARD_PAGE:
        return _page_offer_snapshot(p["member"])
    if kind == catalog.KIND_TEMPLATE_UPDATE:
        from kiro_crew.dashboard.handlers.agents import _agent_detail_candidates

        candidates = _agent_detail_candidates(p["template"])
        if len(candidates) != 1:
            return {"exists": False, "ambiguous": len(candidates) > 1}
        spec = candidates[0][1] if isinstance(candidates[0][1], dict) else {}
        return {"exists": True, "fields": {k: _jsonable(spec.get(k)) for k in p["fields"]}}
    if kind == catalog.KIND_TEMPLATE_MCP_SERVER:
        return _template_server_state(p)
    if kind == catalog.KIND_MCP_INSTALL:
        from kiro_crew.dashboard.handlers.mcp_discover import _derive_install_name

        name = ev0.get("name") or _derive_install_name(p["id"])
        exists = bool(name) and _mcp_exists(name)
        return {"name": name, "exists": exists, "scopes": _mcp_scopes(name) if name else {}}
    if kind == catalog.KIND_MCP_ADD_CUSTOM:
        return {
            "exists": {name: _mcp_exists(name) for name in p["servers"]},
            "scopes": {name: _mcp_scopes(name) for name in p["servers"]},
        }
    if kind == catalog.KIND_MCP_TOGGLE:
        server = p.get("server") or p["name"]
        exists, entry = _mcp_global_entry(server)
        entry = entry or {}
        if "tool" in p:
            raw_disabled = entry.get("disabledTools")
            disabled = raw_disabled if isinstance(raw_disabled, list) else []
            return {"exists": exists, "enabled": p["tool"] not in disabled}
        return {"exists": exists, "enabled": not bool(entry.get("disabled"))}
    if kind == catalog.KIND_SECRET_SAVE:
        from kiro_crew.config.loader import config_dir
        from kiro_crew.secrets import SecretVault

        vault = SecretVault(config_dir())
        if p["name"] not in set(vault.list_names()):
            return {"exists": False}
        return {"exists": True, "revision": _secret_revision(vault, p["name"])}
    if kind == catalog.KIND_TRUST_APP:
        from kiro_crew.dashboard.handlers.security import build_trusted_apps_snapshot

        snap = build_trusted_apps_snapshot()
        stored = set(snap.get("apps") or []) | set(snap.get("ineffective") or [])
        return {"trusted": p["name"] in stored}
    if kind == catalog.KIND_DENIED_COMMAND:
        from kiro_crew.dashboard.handlers.security import build_denied_commands_snapshot

        rules = build_denied_commands_snapshot().get("user_added") or []
        if p["action"] == "add":
            hit = next((r for r in rules if r.get("pattern") == p["pattern"]), None)
            return {"exists": hit is not None, **({"id": hit["id"]} if hit else {})}
        hit = next((r for r in rules if r.get("id") == p["id"]), None)
        if hit is None:
            return {"exists": False}
        return {"exists": True, "enabled": bool(hit.get("enabled")), "pattern": hit.get("pattern")}
    raise catalog.CardCatalogError("unknown_kind", kind)


def _template_server_state(p: dict[str, Any]) -> dict[str, Any]:
    """A ``template.mcp_server`` snapshot: is the server recorded, under which card.

    ``template_has`` is the template's own spec declaring that name already, so
    the card is refused rather than added behind a server of the same name.
    """
    from kiro_crew import template_mcp_additions as additions
    from kiro_crew.dashboard.handlers.template_server_request import template_declares

    added = additions.read_additions(p["template"]).get(p["server"])
    out: dict[str, Any] = {
        "present": added is not None,
        "template_has": template_declares(p["template"], p["server"]),
    }
    if added is not None:
        out["card_id"] = added.card_id
        out["entry"] = _jsonable(dict(added.entry))
    return out


def _capability_service(app: Any) -> Any:
    from kiro_crew.agent_capabilities import CapabilityService
    from kiro_crew.dashboard.handlers import agent_capabilities as handler

    service = app.get(handler._SERVICE) if app is not None else None
    if service is None:
        service = CapabilityService(handler._catalog, handler._connections)
        if app is not None:
            try:
                app[handler._SERVICE] = service
            except Exception:  # a frozen app keeps its own; a fresh one serves this read
                pass
    return service


#: Capability sections a card may edit, in the order the view lists them.
_CARD_CAPABILITY_SECTIONS = ("tools", "allowedTools", "autoApprove", "mcpServers", "skills")


def member_capabilities_view(app: Any, member: str) -> dict[str, Any]:
    """A member's capability rows in exactly the shape ``crewmate.capabilities`` drafts take.

    Ids only, never an MCP transport (its env can carry credentials). Explains the
    two approval grammars, since mixing them is the common mistake: a built-in
    tool (``fs_read``) is approved through ``allowedTools`` by its bare name, an
    MCP tool through ``autoApprove`` as ``@<server>/<tool>``.
    """
    from kiro_crew.agent_capabilities import CapabilityError

    try:
        view = _capability_service(app).get(member)
    except CapabilityError as exc:
        raise catalog.CardCatalogError(exc.code, f"capabilities unavailable: {exc.code}") from None
    rows = []
    for row in view.get("rows") or []:
        section = row.get("section")
        if section not in _CARD_CAPABILITY_SECTIONS:
            continue
        rid = str(row.get("id") or "")
        locked = bool(row.get("managed")) or (
            section == "autoApprove" and rid.startswith("@kirocrew-computer/")
        )
        rows.append(
            {
                "section": section,
                "id": rid,
                "label": row.get("label") or rid,
                "state": row.get("state"),
                "present": bool(row.get("present")),
                "editable": not locked,
            }
        )
    servers = sorted({r["id"] for r in rows if r["section"] == "mcpServers" and r["present"]})
    template = view.get("template") or {}
    return {
        "member": member,
        "template": template.get("name") or "",
        "revision": view.get("revision"),
        "enroll_required": view.get("mode") != "inherited",
        "rows": rows,
        "mcp_servers": servers,
        "connections": [c.get("id") for c in view.get("connections") or [] if c.get("id")],
        "skills": [s.get("id") for s in view.get("skills") or [] if s.get("id")],
        "how_to": {
            "builtin_tool_approval": {"section": "allowedTools", "id": "<tool name, e.g. fs_read>"},
            "mcp_tool_approval": {"section": "autoApprove", "id": "@<mcp server>/<tool>"},
            "set": {"action": "set", "value": True},
            "enroll": 'add "enroll": true to the draft when enroll_required is true',
        },
    }


def _valid_refs(view: dict[str, Any]) -> list[str]:
    return [f"{r['section']}:{r['id']}" for r in view["rows"]]


def capability_ref_hint(app: Any, params: dict[str, Any]) -> str:
    """For a refused draft: the closest valid ``section:id`` refs for each operation."""
    import difflib

    try:
        view = member_capabilities_view(app, params["member"])
    except Exception:
        return ""
    valid = _valid_refs(view)
    builtins = {
        r["id"] for r in view["rows"] if r["section"] == "tools" and not r["id"].startswith("@")
    }
    hints = []
    for op in params["draft"].get("operations") or []:
        section, rid = str(op.get("section")), str(op.get("id"))
        if f"{section}:{rid}" in valid:
            continue
        if section == "autoApprove" and not rid.startswith("@") and rid in builtins | {"*"}:
            hints.append(f"{rid} is a built-in tool: use section allowedTools id {rid}")
            continue
        close = difflib.get_close_matches(f"{section}:{rid}", valid, n=3, cutoff=0.3)
        close += [v for v in valid if v.endswith(":" + rid) and v not in close][:2]
        if close:
            hints.append(f"{section}:{rid} -> closest valid: {', '.join(close)}")
    if not hints:
        return ""
    return "; " + "; ".join(hints) + " (see get_member_capabilities)"


def _capability_state(app: Any, params: dict[str, Any]) -> dict[str, Any]:
    from kiro_crew.agent_capabilities import CapabilityError

    try:
        view = _capability_service(app).get(params["member"])
    except CapabilityError as exc:
        raise catalog.CardCatalogError(exc.code, f"capabilities unavailable: {exc.code}") from None
    touched = {(op.get("section"), op.get("id")) for op in params["draft"]["operations"]}
    rows = [
        {k: row.get(k) for k in ("section", "id", "state", "value", "present")}
        for row in view.get("rows") or []
        if (row.get("section"), row.get("id")) in touched
    ]
    return {"revision": view.get("revision"), "mode": view.get("mode"), "rows": rows}


async def resumed_undo_is_stale(rec: dict[str, Any], index: int, *, state: Any, app: Any) -> bool:
    """Whether what a resumed Undo's step *index* would remove changed since apply.

    The step-0 check compares the whole post-apply snapshot, which stops
    matches once earlier Undo steps removed their part. A resumed step compares
    only what is still there to remove.
    """
    kind, params, after = rec["kind"], rec["params"], rec.get("after") or {}
    if kind in catalog.CREWMATE_CREATING_KINDS:
        current = await read_state(kind, params, rec["evidence"][:1], state=state, app=app)
        keys: tuple[str, ...] = ("exists", "member_id", "revision")
        undo = (rec.get("plan") or {}).get("undo") or []
        rules_at = next(
            (i for i, s in enumerate(undo) if str(s.get("path", "")).endswith("/rules")), -1
        )
        if kind == catalog.KIND_CREWMATE_IMPORT and "rules" in after and index <= rules_at:
            # Until the Undo's own rules step runs, rules saved since are the
            # owner's; after it, the Undo cleared them itself.
            current = await read_state(kind, params, rec["evidence"], state=state, app=app)
            keys += ("rules",)
        return canonical({k: current.get(k) for k in keys}) != canonical(
            {k: after.get(k) for k in keys}
        )
    if kind == catalog.KIND_CREWMATE_CAPABILITIES:
        # Step 1 applies the preview step 0 just took; its token and revision
        # are what the capability route checks, so nothing else to compare.
        return False
    # Every other kind undoes in one step, so a resumed step is never admitted.
    return True


async def undo_survivors(
    rec: dict[str, Any], *, state: Any, app: Any, response: Any = None
) -> list[str]:
    """What a finished Undo was meant to remove but is still there.

    MCP servers are re-read from their scopes. A Disconnect reports its own
    survivors: credential artifacts it could not unlink (``grantSurviving``).
    """
    kind = rec["kind"]
    if kind == catalog.KIND_CONNECTION_CONNECT:
        surviving = response.get("grantSurviving") if isinstance(response, dict) else None
        return [str(s) for s in surviving] if isinstance(surviving, list) else []
    if kind not in (catalog.KIND_MCP_INSTALL, catalog.KIND_MCP_ADD_CUSTOM):
        return []
    names = _mcp_undo_names(rec)
    return [n for n in names if await asyncio.to_thread(_mcp_scopes, n)]


def _connection_grant_fingerprint(slug: str) -> list[int] | None:
    """``[mtime_ns, size]`` of the provider's token artifact; stats only, never opened."""
    from kiro_crew import mcp_grant
    from kiro_crew.connections.registry import get_visible_providers

    provider = next((p for p in get_visible_providers() if p.get("slug") == slug), None)
    if provider is None:
        return None
    stamp = mcp_grant.grant_fingerprint(str(provider["mcp_url"]))
    return list(stamp) if stamp is not None else None


async def read_state(
    kind: str, params: dict[str, Any], evidence: list[dict[str, Any]], *, state: Any, app: Any
) -> dict[str, Any]:
    """The gateway's read of what *params* replaces (or, with *evidence*, what it made)."""
    if kind in (catalog.KIND_SCHEDULE_CREATE, catalog.KIND_SCHEDULE_UPDATE):
        job_id = params.get("id") if kind == catalog.KIND_SCHEDULE_UPDATE else None
        if kind == catalog.KIND_SCHEDULE_CREATE:
            job_id = (evidence[0] if evidence else {}).get("id")
            if not job_id:
                return {}
        job = await state.crons.get_job_async(str(job_id))
        if job is None:
            return {"exists": False}
        return {
            "exists": True,
            "name": job.name,
            "fields": _cron_job_fields(job),
            "revision": _cron_job_revision(job),
        }
    if kind == catalog.KIND_CONNECTION_CONNECT:
        from kiro_crew.connections.status import collect_connection_statuses

        statuses = await collect_connection_statuses()
        row = next((s for s in statuses if s.get("slug") == params["slug"]), None)
        return {
            "known": row is not None,
            "granted": bool(row and row.get("grantPresent")),
            # Presence says "a grant", the stat fingerprint says "THIS grant": a
            # re-authorization rewrites the token artifact, so an Undo recorded
            # against the earlier grant refuses instead of revoking the new one.
            "grant": await asyncio.to_thread(_connection_grant_fingerprint, params["slug"]),
        }
    if kind == catalog.KIND_CREWMATE_CAPABILITIES:
        return await asyncio.to_thread(_capability_state, app, params)
    if kind == catalog.KIND_FOLDER_STEERING:
        folder = await _folder(state, params["folder"])
        if folder is None:
            return {"exists": False}
        return {"exists": True, "steering_dirs": catalog.string_items(folder.get("steering_dirs"))}
    if kind in catalog.WORKFLOW_KINDS:
        return await _workflow_state(kind, params, state)
    if kind == catalog.KIND_TEMPLATE_UPDATE and "skills" in (params.get("fields") or {}):
        return await asyncio.to_thread(_template_state_with_skills, params, state)
    # A legacy-shaped create card made its schedule at step 1, the current one at
    # step 2: the evidence says which (the same index its Undo deletes).
    if kind == catalog.KIND_CREWMATE_IMPORT and evidence:
        return await _import_state(params, evidence, state=state)
    schedule_step = catalog._create_schedule_index(evidence)
    if kind == catalog.KIND_CREWMATE_CREATE and len(evidence) > schedule_step:
        base = await asyncio.to_thread(_sync_read_state, kind, params, evidence[:1])
        job = await state.crons.get_job_async(str(evidence[schedule_step].get("id")))
        schedule = _cron_job_revision(job) if job is not None else None
        return {**base, "schedule_exists": job is not None, "schedule": schedule}
    return await asyncio.to_thread(_sync_read_state, kind, params, evidence)


async def _import_state(
    params: dict[str, Any], evidence: list[dict[str, Any]], *, state: Any
) -> dict[str, Any]:
    """An applied import's crewmate, plus each cron it made and its rules, as fingerprints.

    The Undo deletes a cron only while it still reads as the import left it, and
    clears the rules only while they are still the imported text: an edit the
    owner saved since makes the snapshot differ, so the Undo is refused. The
    carried files are checked file by file by their own route's Undo.
    """
    base = await asyncio.to_thread(
        _sync_read_state, catalog.KIND_CREWMATE_IMPORT, params, evidence[:1]
    )
    roles = catalog.import_step_roles(params)
    crons: dict[str, Any] = {}
    for index, role in enumerate(roles[: len(evidence)]):
        job_id = evidence[index].get("id")
        if role == catalog.IMPORT_STEP_CRON and isinstance(job_id, str) and job_id:
            job = await state.crons.get_job_async(job_id)
            crons[job_id] = _cron_job_revision(job) if job is not None else None
    out = {**base, "crons": crons}
    if catalog.IMPORT_STEP_RULES in roles[: len(evidence)]:
        out["rules"] = await asyncio.to_thread(_import_rules_digest, evidence[0])
    return out


def _import_after_is_written(
    params: dict[str, Any], after: dict[str, Any], evidence: list[dict[str, Any]]
) -> bool:
    """Whether the post-apply read of an import holds what each step committed.

    Each cron step's response carried the fingerprint of the job it made, and the
    rules step wrote the card's own text. A cron or the rules the owner saved
    while later steps ran would otherwise become the Undo's baseline, and the
    Undo would then delete that edit.
    """
    roles = catalog.import_step_roles(params)
    seen = after.get("crons")
    crons: dict[str, Any] = seen if isinstance(seen, dict) else {}
    for index, role in enumerate(roles[: len(evidence)]):
        if role == catalog.IMPORT_STEP_CRON:
            job_id, written = evidence[index].get("id"), evidence[index].get("revision")
            if not isinstance(written, str) or crons.get(str(job_id)) != written:
                return False
        elif role == catalog.IMPORT_STEP_RULES:
            text = str(params.get("rules") or "").strip()
            if after.get("rules") != hashlib.sha256(text.encode("utf-8")).hexdigest():
                return False
    return True


def _import_rules_digest(ev0: dict[str, Any]) -> str | None:
    from kiro_crew import members as members_mod

    slug, name = ev0.get("member_id"), ev0.get("name")
    if not isinstance(slug, str) or not slug or not isinstance(name, str):
        return None
    try:
        text = members_mod.read_member_rules(slug, name)
    except Exception:
        return None
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


async def _folder(state: Any, folder_id: str) -> dict[str, Any] | None:
    """A copy of the sidebar folder *folder_id*, or ``None``."""

    def _find(folders: list[dict[str, Any]]) -> dict[str, Any] | None:
        hit = next((f for f in folders if f.get("id") == folder_id), None)
        return copy.deepcopy(hit) if hit is not None else None

    found: dict[str, Any] | None = await state.read_folders(_find)
    return found


async def _mcp_detail(params: dict[str, Any]) -> dict[str, Any]:
    """Best-effort display facts for an install; empty when the registry is unreachable."""
    try:
        from kiro_crew.dashboard.handlers.mcp_discover import _get_registry

        registry = await asyncio.to_thread(_get_registry)
        provider = registry.get(params["provider"])
        if provider is None or not provider.is_available():
            return {}
        detail = await asyncio.wait_for(provider.fetch_detail(params["id"]), timeout=8)
    except Exception:
        return {}
    if detail is None:
        raise catalog.CardCatalogError("not_found", f"no MCP server '{params['id']}'")
    out: dict[str, Any] = {}
    plan = getattr(detail, "install_plan", None)
    spec = getattr(plan, "spec", None) if plan is not None else None
    if isinstance(spec, dict):
        if spec.get("url"):
            out["command"] = str(spec["url"])
        elif spec.get("command"):
            out["command"] = " ".join(
                [str(spec["command"])] + [str(a) for a in spec.get("args") or []]
            )
    for key in ("version", "repo_url"):
        value = getattr(detail, key, None)
        if isinstance(value, str) and value:
            out[key] = value[:200]
    return out


def _crewmate_label(key: str) -> str:
    """The name the user knows a crewmate by: its display name, else its key.

    A card raised in the crewmate's own chat is read under that display name, so
    a title or scope line naming the key would name a crewmate the user has never
    seen. Display-only, so it rides in the context rather than the compared
    snapshot: a rename between propose and apply is not a conflict.
    """
    from kiro_crew.config.loader import KiroCrewConfig

    agent = KiroCrewConfig.load().agents.get(key)
    label = getattr(agent, "display_name", "") if agent is not None else ""
    return label.strip() if isinstance(label, str) and label.strip() else key


async def read_context(
    kind: str, params: dict[str, Any], before: dict[str, Any], *, state: Any, app: Any
) -> dict[str, Any]:
    """Display-only facts and up-front validation a preview needs."""
    p = params
    if kind == catalog.KIND_CREWMATE_UPDATE:
        return {"label": await asyncio.to_thread(_crewmate_label, p["name"])}
    if kind == catalog.KIND_CREWMATE_DASHBOARD_PAGE:
        return await asyncio.to_thread(_page_offer_context, p["member"], before)
    if kind in (
        catalog.KIND_CREWMATE_GOAL,
        catalog.KIND_CREWMATE_KEY_RESULTS,
        catalog.KIND_CREWMATE_CHECK_INS,
    ):
        return {"label": await asyncio.to_thread(_crewmate_label, p["member"])}
    if kind == catalog.KIND_SETTING_CHANGE:
        target = await asyncio.to_thread(resolve_setting, p)
        check_target_params(target, p)
        return {}
    if kind == catalog.KIND_SCHEDULE_CREATE and "at" in p:
        return one_shot_context(p["at"], p.get("timezone") or "")
    if kind in (
        catalog.KIND_SCHEDULE_CREATE,
        catalog.KIND_SCHEDULE_UPDATE,
        catalog.KIND_CREWMATE_CREATE,
    ):
        source = p
        if kind == catalog.KIND_SCHEDULE_UPDATE:
            fields = dict(before.get("fields") or {})
            fields.update(p["fields"])
            source = fields
        if kind == catalog.KIND_CREWMATE_CREATE:
            source = p.get("schedule") or {}
        expr = source.get("cron_expr")
        if not expr:
            return {}
        from kiro_crew.cron_service.schedule import get_local_tz, is_valid_timezone

        tz = source.get("timezone") or ""
        if tz and not is_valid_timezone(tz):
            raise catalog.CardCatalogError("invalid_timezone", f"unknown timezone '{tz[:64]}'")
        when = next_run_at(expr, tz)
        if when is None:
            raise catalog.CardCatalogError("invalid_cron", "that cron expression never runs")
        # No zone on the card: the scheduler runs it in the configured one, so
        # that is the zone the card names and the next run is shown in.
        return {"next_run_at": when, "timezone": tz or get_local_tz()[0]}
    if kind == catalog.KIND_CREWMATE_CAPABILITIES:
        from kiro_crew.agent_capabilities import CapabilityError

        body = {"revision": before.get("revision"), **p["draft"]}

        def _preview() -> dict[str, Any]:
            return _capability_service(app).preview(p["member"], body)

        try:
            result = await asyncio.to_thread(_preview)
        except CapabilityError as exc:
            hint = await asyncio.to_thread(capability_ref_hint, app, p)
            raise catalog.CardCatalogError(
                exc.code, f"capabilities refused: {exc.code}{hint}"
            ) from None
        impact = result.get("impact") or []
        approvals = [
            f"{op['section']}:{op['id']}"
            for op in p["draft"]["operations"]
            if op.get("action") == "set" and op.get("section") in ("allowedTools", "autoApprove")
        ]
        withheld = [
            ref
            for ref in approvals
            if not any(f"{i.get('section')}:{i.get('id')}" == ref for i in impact)
            and any(
                f"{r.get('section')}:{r.get('id')}" == ref
                # Sanitized to a tombstone (forked members) or kept as an override
                # the ceiling filtered out of the spec (the Assistant singleton).
                and (r.get("state") == "removed" or not r.get("present"))
                for r in result.get("rows") or []
            )
        ]
        if withheld and not impact:
            raise catalog.CardCatalogError(
                "approval_withheld_by_policy",
                "the governance ceiling does not allow auto-approving "
                + ", ".join(withheld)
                + "; the tool keeps asking each time, and no card can change that",
            )
        from kiro_crew.dashboard.handlers.member_capability_catalog import gateway_matches

        return {
            "impact": impact,
            "label": await asyncio.to_thread(_crewmate_label, p["member"]),
            "gateway": sorted(await asyncio.to_thread(gateway_matches, p)),
        }
    if kind == catalog.KIND_TEMPLATE_UPDATE:
        from kiro_crew.config.loader import KiroCrewConfig

        cfg = await asyncio.to_thread(KiroCrewConfig.load)
        members = sorted(
            crew for crew, c in cfg.agents.items() if getattr(c, "kiro_agent", "") == p["template"]
        )
        return {"members": members}
    if kind == catalog.KIND_TEMPLATE_MCP_SERVER:
        from kiro_crew.dashboard.handlers.template_server_request import (
            addition_reach_view,
            existing_grants,
            live_apply,
            server_tools,
        )

        return {
            "reach": await asyncio.to_thread(addition_reach_view, state, p["template"]),
            "live": live_apply(state),
            "tools": await asyncio.to_thread(server_tools, p["server"]),
            "grants": await asyncio.to_thread(existing_grants, p["template"], p["server"]),
        }
    if kind == catalog.KIND_MCP_INSTALL:
        return {"detail": await _mcp_detail(p)}
    if kind == catalog.KIND_FOLDER_STEERING:
        # Display only: the folder's name, so a rename is not a conflict.
        folder = await _folder(state, p["folder"])
        return {"label": str((folder or {}).get("name") or p["folder"])}
    if kind in catalog.WORKFLOW_KINDS:
        return await _workflow_context(kind, p, before, state)
    return {}


def _workflow_service(state: Any) -> Any:
    svc = getattr(state, "workflow_service", None)
    if svc is None:
        raise catalog.CardCatalogError("workflows_unavailable", "workflows are not available")
    return svc


async def _workflow_state(kind: str, params: dict[str, Any], state: Any) -> dict[str, Any]:
    """What a workflow card replaces: nothing for a save, the saved revision for an update.

    A save creates a definition with its own new id, so there is nothing it could
    overwrite; only the slash command it asks for must still be free. An update is pinned to the revision read here: Apply sends it as
    ``expected_revision``, and any save in between is a change since the preview.
    """
    svc = _workflow_service(state)
    if kind == catalog.KIND_WORKFLOW_SAVE:
        # The command the card shows, asked for or derived from the name, and
        # whether it is free, read again before Apply, so a save that takes it in
        # between is a change since the preview.
        slug = catalog.workflow_save_slug(params)
        taken = await asyncio.to_thread(svc.get_definition, slug)
        return {"slug": slug, "slug_taken": taken is not None}
    definition = await asyncio.to_thread(svc.get_definition, params["workflow"])
    if not isinstance(definition, dict):
        return {"exists": False}
    return {
        "exists": True,
        "id": str(definition.get("id") or ""),
        "slug": str(definition.get("slug") or ""),
        "name": str(definition.get("name") or ""),
        "description": str(definition.get("description") or ""),
        "format": str(definition.get("format") or "python"),
        "revision": definition.get("revision"),
        "content_hash": str(definition.get("content_hash") or ""),
    }


#: Largest script diff a ``workflow.update`` card carries, as JSON-encoded text like
#: ``WORKFLOW_SOURCE_MAX``; it is kept twice (the row and the card's context). A
#: larger diff is refused, never cut: a cut diff would hide the changed lines past
#: the cut, and a removed line is shown nowhere else on the card.
WORKFLOW_DIFF_MAX = 6_000

#: The exact hunk header ``difflib.unified_diff`` writes with ``lineterm=""``.
_HUNK_HEADER_RE = re.compile(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
#: How a hunk's separator row starts. Every other diff line starts with ``+``,
#: ``-`` or a space, so a row starting with this is never a script line.
WORKFLOW_DIFF_SEPARATOR = "\u22ef"


def _hunk_separator(header: re.Match[str]) -> str:
    """The separator row standing for one ``@@ -a,b +c,d @@`` header.

    It names the new revision's lines the hunk spans (``⋯ lines c-(c+d-1) ⋯``),
    the same numbers the card's numbered script block shows, so two far-apart
    hunks never draw as one block. A hunk that only removes lines spans none of
    them and names the new line it removes after (``⋯ after line c ⋯``).
    """
    _old_start, _old_len, new_start, new_len = (
        int(g) if g is not None else 1 for g in header.groups()
    )
    sep = WORKFLOW_DIFF_SEPARATOR
    if not new_len:
        return f"{sep} after line {new_start} {sep}"
    return f"{sep} lines {new_start}-{new_start + new_len - 1} {sep}"


def _unified_range(start: int, stop: int) -> str:
    """One side of a hunk header, spelled as ``difflib.unified_diff`` spells it."""
    length = stop - start
    if length == 1:
        return str(start + 1)
    return f"{start + 1 if length else start},{length}"


def _unified_hunks(old: list[str], new: list[str]) -> Iterator[str]:
    """The hunk lines of a unified diff of *old* to *new*, with no file headers.

    ``difflib.unified_diff``'s output past its two file headers, except that lines
    are matched with ``autojunk`` off. With it on, any line that makes up more than
    1% of a script of 200 lines or more (a blank line, a closing bracket) is never
    matched, so one changed line can diff as the whole script rewritten and the
    card refuse a small change as too large to show.
    """
    import difflib

    matcher = difflib.SequenceMatcher(None, old, new, autojunk=False)
    for group in matcher.get_grouped_opcodes(3):
        first, last = group[0], group[-1]
        old_range = _unified_range(first[1], last[2])
        new_range = _unified_range(first[3], last[4])
        yield f"@@ -{old_range} +{new_range} @@"
        for tag, i1, i2, j1, j2 in group:
            if tag == "equal":
                yield from (" " + line for line in old[i1:i2])
                continue
            if tag in ("replace", "delete"):
                yield from ("-" + line for line in old[i1:i2])
            if tag in ("replace", "insert"):
                yield from ("+" + line for line in new[j1:j2])


def _workflow_diff(old: str, new: str) -> str:
    """Every changed line from the saved script *old* to *new*, or a refusal.

    A unified diff (:func:`_unified_hunks`, no file headers) with each ``@@``
    hunk header replaced by a separator row naming the lines the hunk spans.
    Hunk headers are matched by their exact shape, never by prefix, because a
    source line such as ``-- x`` or a markdown ``---`` rule is written as
    ``--- x`` / ``----`` once marked as removed, and every changed line must
    reach the card. A removed line comes from a revision saved earlier, which
    never passed the card's character checks, so its hidden characters are
    spelled out (``catalog.escape_hidden_characters``) before it is shown.
    """
    # Split at ``\n`` only: ``splitlines`` also breaks at U+2028, U+2029 and
    # U+0085, which Python's compiler reads as ordinary characters, so a line it
    # runs as a comment could diff as unchanged code.
    shown: list[str] = []
    for line in _unified_hunks(old.split("\n"), new.split("\n")):
        header = _HUNK_HEADER_RE.fullmatch(line)
        if header is not None:
            shown.append(_hunk_separator(header))
        elif line.startswith("-"):
            shown.append(catalog.escape_hidden_characters(line))
        else:
            shown.append(line)
    diff = "\n".join(shown)
    # Counted as the card store counts it: the escaped text.
    if len(json.dumps(diff)) - 2 > WORKFLOW_DIFF_MAX:
        raise catalog.CardCatalogError(
            "diff_too_large",
            f"the change to the script is too large to show on one card (its diff is "
            f"over {WORKFLOW_DIFF_MAX} bytes once encoded) and a card never hides a "
            "changed line; propose a smaller change, one card per step, or ask the "
            "owner to edit the workflow in Customize -> Workflows",
        )
    return diff


async def _workflow_context(
    kind: str, params: dict[str, Any], before: dict[str, Any], state: Any
) -> dict[str, Any]:
    """Refuse a script the library would refuse, and for an update, the diff it shows.

    The script is validated before the card exists, so the person is never asked to
    approve something the save route answers 400 to.
    """
    from kiro_crew.workflows.validate import validate

    result = await asyncio.to_thread(validate, params["source"])
    if not result.ok:
        errors = "; ".join(str(e) for e in result.errors)
        raise catalog.CardCatalogError(
            "invalid_workflow", f"the script is not valid: {errors[:400]}"
        )
    if kind != catalog.KIND_WORKFLOW_UPDATE or not before.get("exists"):
        return {}
    definition = await asyncio.to_thread(
        _workflow_service(state).get_definition, str(before.get("id") or "")
    )
    revision = before.get("revision")
    if not isinstance(definition, dict) or definition.get("revision") != revision:
        # The diff must be against the revision Apply pins as ``expected_revision``;
        # a save landing between the two reads would otherwise show a diff against
        # a script the card does not replace.
        raise catalog.CardCatalogError(
            "workflow_changed", "the saved workflow changed while the card was built; ask again"
        )
    old = str(definition.get("source") or "")
    if old == params["source"]:
        return {}
    return {
        # Matching without autojunk can take seconds on a script of thousands of
        # identical lines, so the diff runs off the event loop.
        "diff": await asyncio.to_thread(_workflow_diff, old, params["source"]),
        "source_changed": True,
    }


async def resolve_params(kind: str, params: dict[str, Any]) -> dict[str, Any]:
    """*params* with any value the target route would canonicalize, canonicalized.

    A ``folder.steering`` card carries each directory as the folder route will
    store it (``_validate_steering_dirs``: ``~`` expanded, links resolved), so
    the list the person approves is the list the gateway reads, and Undo's
    read-back matches what the card wrote.
    """
    if kind != catalog.KIND_FOLDER_STEERING:
        return params
    from kiro_crew.dashboard.chat_folders import _validate_steering_dirs

    resolved, err = await asyncio.to_thread(_validate_steering_dirs, params["steering_dirs"])
    if err:
        raise catalog.CardCatalogError("invalid_param", err)
    # Re-validated as resolved, so a list the stored card could not be rebuilt
    # from (on restart) is refused here instead of the card being dropped later.
    return catalog.validate_params(kind, {**params, "steering_dirs": resolved})


async def build(
    kind: str, params: dict[str, Any], *, state: Any, app: Any
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """``(preview, before, context)`` for a validated proposal."""
    before = await read_state(kind, params, [], state=state, app=app)
    context = await read_context(kind, params, before, state=state, app=app)
    preview = catalog.build_preview(kind, params, before, context)
    check_preview_text(preview)
    return preview, before, context


def check_preview_text(preview: dict[str, Any]) -> None:
    """Refuse a card whose preview shows a stored value an output redactor would change.

    ``check_param_text`` covers what the agent wrote; this covers what the card
    copied from the live record (a schedule's current message, as ``before``).
    The preview is published to the agent and the page as is, and an undo plan
    writes those same values back, so a credential there is refused rather than
    shown or rewritten.
    """
    try:
        check_param_text(preview, _path="current")
    except CardError as exc:
        raise CardError(
            400,
            "unsafe_current_value",
            "the current value holds text that cannot be shown on a card; "
            "change it on its own page",
        ) from exc
