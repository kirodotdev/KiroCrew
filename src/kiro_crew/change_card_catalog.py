"""The change-card catalog: what an agent may propose, and what each proposal does.

A change card is an agent's proposal of ONE registered kind of change. The agent
names the kind and fills in its parameters; it never supplies a route, a request
body, markup or a risk level. Everything the browser later sends, and everything
the card shows under "what changes", is derived HERE from the parameters and the
gateway's own read of the current state:

* :func:`validate_params` normalizes and bounds the parameters, refusing unknown
  keys, so a card can only carry the fields its kind declares.
* :func:`build_preview` turns parameters plus the current-state snapshot into the
  card's title, product-written ``changes`` rows, the computed ``risk`` and the
  ordered ``apply`` request plan against the EXISTING settings-page routes.
* :func:`build_undo` derives the undo plan after apply, from the state before
  the change and the identities the real routes returned (a created schedule's
  id, a created crewmate's key). Undo restores the state before the change; when
  that is impossible (an overwritten secret) the card says so instead.

Risk is computed from the diff, never from the kind the agent picked: a plain
``setting.change`` that turns on auto-approval is ``widen`` exactly like a trust
card. ``code_exec`` marks a change that runs third-party code on this machine.

A plan step is ``{method, path, body}`` with ``body`` canonical JSON (or ``None``).
A field the gateway cannot know at proposal time is named in the step's ``fill``
list and carries a ``{{...}}`` placeholder in ``body``: ``source: "user"`` is a
value only the person types (a secret's value, never stored anywhere), and
``source: "step"`` is a field a previous step's real response returned, which
the gateway checks against what it recorded. A ``repeat`` step is a read the
browser polls (an OAuth approval) and completes only on the state the route
itself reports.

Pure: no I/O. The dashboard layer (:mod:`kiro_crew.dashboard.change_cards`) reads
the state and calls in here.
"""

from __future__ import annotations

import importlib as _importlib
import sys as _sys
import urllib.parse
from typing import TYPE_CHECKING, Any

from kiro_crew import _facade_mirror, guide_catalog
from kiro_crew import member_goals as _goals
from kiro_crew.change_card_catalog_kinds import (
    CHECK_IN_FIELDS,
    CREWMATE_CREATE_GOAL_STEP,
    CREWMATE_CREATE_SCHEDULE_STEP,
    CREWMATE_CREATING_KINDS,
    KIND_CONNECTION_CONNECT,
    KIND_CREWMATE_CAPABILITIES,
    KIND_CREWMATE_CHECK_INS,
    KIND_CREWMATE_CREATE,
    KIND_CREWMATE_DASHBOARD_PAGE,
    KIND_CREWMATE_GOAL,
    KIND_CREWMATE_IMPORT,
    KIND_CREWMATE_KEY_RESULTS,
    KIND_CREWMATE_UPDATE,
    KIND_DENIED_COMMAND,
    KIND_FOLDER_STEERING,
    KIND_MCP_ADD_CUSTOM,
    KIND_MCP_INSTALL,
    KIND_MCP_TOGGLE,
    KIND_SCHEDULE_CREATE,
    KIND_SECRET_SAVE,
    KIND_SETTING_CHANGE,
    KIND_TEMPLATE_UPDATE,
    LIST_OP_ADD,
    LIST_OP_REMOVE,
    OP_APPLY,
)
from kiro_crew.change_card_catalog_plans import (
    DASHBOARD_KEYS,
    DASHBOARD_LIST_OP_KEYS,
    IMPORT_STEP_CRON,
    IMPORT_STEP_FILES,
    IMPORT_STEP_GOAL,
    IMPORT_STEP_RULES,
    LEGACY_CREWMATE_CREATE_SCHEDULE_STEP,
    UNDO_NO_IDENTITY_CHECK,
    _capability_plan,
    _check_ins_path,
    _create_schedule_index,
    _crewmate_put_body,
    _folder_steering_step,
    _goal_path,
    _key_results_path,
    _q,
    _scrub_kr_records,
    _scrub_kr_shape,
    _short,
    _toggle_step,
    capability_inverse,
    import_step_roles,
    import_undo,
    list_op_step,
    offers_no_undo,
    setting_write_step,
    step,
    string_items,
)
from kiro_crew.config.sections import MEMBER_APPROVAL_MODE_UNSET_EFFECTIVE

# ── evidence, undo, results ──


def extract_evidence(
    kind: str, op: str, step_index: int, data: dict[str, Any] | None
) -> dict[str, Any]:
    """The few identity fields a step's real 2xx response carried. Never values."""
    if not isinstance(data, dict):
        return {}

    def pick(*keys: str) -> dict[str, Any]:
        return {k: data[k] for k in keys if isinstance(data.get(k), (str, int, bool))}

    if kind == KIND_SCHEDULE_CREATE or (
        # Step 1 is a legacy card's schedule; the goal write it is now answers
        # no ``id``, so the pick stays empty there.
        kind == KIND_CREWMATE_CREATE
        and step_index in (LEGACY_CREWMATE_CREATE_SCHEDULE_STEP, CREWMATE_CREATE_SCHEDULE_STEP)
    ):
        return pick("id")
    if kind in CREWMATE_CREATING_KINDS and step_index == 0:
        return pick("member_id", "name")
    if kind == KIND_CREWMATE_IMPORT and op == OP_APPLY:
        # A cron step's job id and the fingerprint of the job as it made it; the
        # import's Undo reads them only at a cron step.
        return pick("id", "revision")
    if kind == KIND_CREWMATE_GOAL and op == OP_APPLY:
        # The goal the write added or changed, so the Undo of an added goal can
        # remove exactly that one.
        return pick("goal_id")
    if kind == KIND_CREWMATE_KEY_RESULTS and op == OP_APPLY:
        # The ids the write left, so the Undo refuses a set that reads the same
        # but was recreated since (new ids, new readings).
        krs = data.get("key_results")
        if isinstance(krs, list) and all(
            isinstance(k, dict) and isinstance(k.get("id"), str) for k in krs
        ):
            # And the goals each one serves after the write, so the Undo also
            # refuses a newer card that changed only the links.
            return {
                "ids": [k["id"] for k in krs],
                "links": {k["id"]: sorted(k.get("goal_ids") or []) for k in krs},
            }
        return {}
    if kind == KIND_CREWMATE_CAPABILITIES and step_index == 0:
        return pick("preview_token")
    if kind == KIND_CREWMATE_CAPABILITIES:
        return pick("revision")
    if kind == KIND_MCP_INSTALL:
        return pick("name")
    if kind == KIND_MCP_ADD_CUSTOM:
        added = data.get("added")
        return (
            {"added": [a for a in added if isinstance(a, str)]} if isinstance(added, list) else {}
        )
    if kind == KIND_CONNECTION_CONNECT:
        return pick("state")
    if kind == KIND_SECRET_SAVE:
        return pick("name", "revision")
    if kind == KIND_DENIED_COMMAND and op == OP_APPLY:
        return {}
    return {}


def poll_outcome(kind: str, evidence: dict[str, Any]) -> str | None:
    """For a ``repeat`` step: ``"done"``, ``"failed"`` or ``None`` (keep polling)."""
    if kind == KIND_CONNECTION_CONNECT:
        state = evidence.get("state")
        if state == "granted":
            return "done"
        if state in ("failed", "expired", "idle"):
            return "failed"
    return None


def _apply_step_is_write(step: dict[str, Any]) -> bool:
    """Whether an apply plan step writes, the same split as :data:`MEMORY_ROUTES`.

    A ``GET`` reads (the OAuth mint poll); a capability ``preview`` POST computes
    an impact and writes nothing. Every other hooked step mutates. Used so the
    preview that fronts a ``crewmate.capabilities`` apply is not miscounted as an
    applied change when its PUT never lands.
    """
    if str(step.get("method", "")).upper() == "GET":
        return False
    path = str(step.get("path", "")).partition("?")[0]
    return not path.endswith("/capabilities/preview")


def applied_write_count(apply_plan: list[dict[str, Any]], succeeded: int) -> int:
    """How many of the first *succeeded* apply steps actually wrote.

    ``succeeded`` is ``len(evidence)`` — one entry per step that returned 2xx, so
    the evidence list and the plan stay index-aligned. This collapses the read
    steps (preview, poll) out of the count passed to :func:`build_undo` as
    ``applied_steps`` WITHOUT reindexing evidence, so ``ev(i)`` still names the
    i-th step's response. A preview that succeeded while its PUT failed is zero
    writes applied and so no Undo.
    """
    return sum(1 for s in apply_plan[: max(0, succeeded)] if _apply_step_is_write(s))


def build_undo(
    kind: str,
    params: dict[str, Any],
    before: dict[str, Any],
    evidence: list[dict[str, Any]],
    applied_steps: int,
    context: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]] | None, str | None]:
    """``(plan, None)`` restoring the state before the change, or ``(None, reason)``.

    *applied_steps* counts successful writes, excluding previews and polls, so
    a partial apply undoes only what took effect.
    """
    return _build_undo(kind, params, before, evidence, applied_steps, context)


def _build_undo(
    kind: str,
    params: dict[str, Any],
    before: dict[str, Any],
    evidence: list[dict[str, Any]],
    applied_steps: int,
    context: dict[str, Any] | None,
) -> tuple[list[dict[str, Any]] | None, str | None]:
    p = params
    ev = lambda i: evidence[i] if i < len(evidence) else {}  # noqa: E731
    if applied_steps <= 0:
        return None, "nothing_applied"
    if offers_no_undo(kind, p):
        return None, UNDO_NO_IDENTITY_CHECK
    if kind == KIND_SETTING_CHANGE and "op" in p:
        # The reverse one-item delta, through the same route the apply used.
        reverse = LIST_OP_REMOVE if p["op"] == LIST_OP_ADD else LIST_OP_ADD
        return [list_op_step(before, reverse, p["item"])], None
    if kind == KIND_SETTING_CHANGE:
        return [setting_write_step(p, before, before.get("value"))], None
    if kind == KIND_SCHEDULE_CREATE:
        job_id = ev(0).get("id")
        if not job_id:
            return None, "identity_unknown"
        return [step("DELETE", f"/api/crons/{_q(str(job_id))}")], None
    if kind == KIND_CREWMATE_IMPORT:
        return import_undo(p, evidence, applied_steps), None
    if kind in CREWMATE_CREATING_KINDS:
        plan = []
        at = _create_schedule_index(evidence)
        job_id = ev(at).get("id")
        if applied_steps > at and job_id:
            plan.append(step("DELETE", f"/api/crons/{_q(str(job_id))}"))
        name = ev(0).get("name") or p["name"]
        plan.append(step("DELETE", f"/api/agents/{_q(str(name))}"))
        return plan, None
    if kind == KIND_CREWMATE_UPDATE:
        fields = before.get("fields") or {}
        old = {k: fields.get(k) if fields.get(k) is not None else "" for k in p["fields"]}
        if "approval_mode" in old:
            # The route accepts only a real mode, never "" (a pinned mode must
            # not fall back to the default by accident), so an approval mode
            # that was unset before the card is restored as the mode the thread
            # was running in, which is the same permission the user had.
            old["approval_mode"] = old["approval_mode"] or MEMBER_APPROVAL_MODE_UNSET_EFFECTIVE
        # The undo's expected mode is the one the card WROTE: a profile save
        # that landed since is a newer choice, and the route's compare-and-set
        # refuses to overwrite it (409 approval_mode_conflict).
        return [
            step("PUT", f"/api/agents/{_q(p['name'])}", _crewmate_put_body(old, p["fields"]))
        ], None
    if kind == KIND_CREWMATE_GOAL:
        # Back to the goals before the card, and only while the goal still reads
        # as the card wrote it: the route's compare-and-set refuses otherwise.
        slug = before.get("slug")
        if not slug:
            return None, "identity_unknown"
        gid = p.get("goal_id")
        if gid is None:
            # The card added a goal: its Undo removes that one (an empty text).
            gid = ev(0).get("goal_id")
            if not isinstance(gid, str):
                return None, "identity_unknown"
            old_text = ""
        else:
            old_text = next(
                (
                    str(g.get("text") or "")
                    for g in before.get("goals") or []
                    if isinstance(g, dict) and g.get("id") == gid
                ),
                "",
            )
        return [
            step(
                "PUT",
                _goal_path(str(slug)),
                {
                    "member": p["member"],
                    "goal_id": gid,
                    # Scrubbed like the key results' Undo: the plan is public.
                    "long_term": _goals.scrub_text(old_text),
                    "expected": p["long_term"],
                },
            )
        ], None
    if kind == KIND_CREWMATE_KEY_RESULTS:
        slug = before.get("slug")
        if not slug:
            return None, "identity_unknown"
        body: dict[str, Any] = {
            "member": p["member"],
            "items": _scrub_kr_shape(list(before.get("key_results") or [])),
            # The pre-change records by id, scrubbed: the plan is public.
            # The route keeps every id still in the set as it is now and
            # puts back only the ones the card removed.
            "restore": _scrub_kr_records(list(before.get("key_results_records") or [])),
            "expected": p["items"],
        }
        ids = ev(0).get("ids")
        if isinstance(ids, list):
            # The ids the card's write left: a set recreated since with the
            # same wording carries new ids and is refused, not overwritten.
            body["expected_ids"] = ids
        links = ev(0).get("links")
        if isinstance(links, dict):
            body["expected_links"] = links
        return [step("PUT", _key_results_path(str(slug)), body)], None
    if kind == KIND_CREWMATE_CHECK_INS:
        # Each field the card changed goes back to what it was before.
        slug = before.get("slug")
        if not slug:
            return None, "identity_unknown"
        then = before.get("schedules") or {}
        back = {k: then[k] for k in CHECK_IN_FIELDS if k in p and k in then and p[k] != then[k]}
        if not back:
            return None, "identity_unknown"
        return [
            step(
                "PUT",
                _check_ins_path(str(slug)),
                {"member": p["member"], **back, "expected": {k: p[k] for k in back}},
            )
        ], None
    if kind == KIND_CREWMATE_CAPABILITIES:
        # Step 0 is a preview (not a write), step 1 is the PUT. ``applied_steps``
        # counts writes (see :func:`applied_write_count`), so a PUT that never
        # landed is ``applied_steps == 0`` and already returned "nothing_applied"
        # above. The inverse binds to the revision the PUT returned (step 1's
        # evidence), never a fresh read's — a write another tab landed after ours
        # would otherwise become the baseline and Undo would overwrite it.
        ops = capability_inverse(
            p["draft"], before.get("rows") or [], severed=before.get("mode") == "mate"
        )
        revision = ev(1).get("revision")
        if ops is None:
            return None, "redacted_server_definition"
        if not revision:
            return None, "revision_unknown"
        return _capability_plan(p["member"], revision, {"operations": ops}), None
    if kind == KIND_TEMPLATE_UPDATE:
        fields = before.get("fields") or {}
        old = {}
        for k in p["fields"]:
            value = fields.get(k)
            if value is None:
                value = [] if k != "model" else ""
            old[k] = value
        return [step("PATCH", f"/api/agents/detail/{_q(p['template'])}", old)], None
    if kind == KIND_MCP_TOGGLE:
        return [_toggle_step(p, bool(before.get("enabled")))], None
    if kind == KIND_FOLDER_STEERING:
        # Offered only while the folder holds what the card stored
        # (after_shows_the_write); the card hook arms that list for the route.
        prior_dirs = string_items(before.get("steering_dirs"))
        return [_folder_steering_step(p["folder"], prior_dirs)], None
    if kind == KIND_SECRET_SAVE:
        if before.get("exists"):
            return None, "overwrites_existing"
        # Deletes only the entry this card's save wrote: a value saved over it
        # since (another tab) has another revision and is left alone.
        revision = ev(0).get("revision")
        if not isinstance(revision, str) or not revision:
            return None, "identity_unknown"
        path = f"/api/secrets/{_q(p['name'])}?" + urllib.parse.urlencode({"if_revision": revision})
        return [step("DELETE", path)], None
    # KIND_DENIED_COMMAND toggle (an add offers no undo, see offers_no_undo)
    return [
        step(
            "PATCH",
            f"/api/security/denied-commands/user/{_q(p['id'])}",
            {"enabled": bool(before.get("enabled"))},
        )
    ], None


def undo_label(kind: str) -> str:
    """What the undo button actually does, for kinds where "Undo" would overstate it."""
    if kind == KIND_SECRET_SAVE:
        return "delete_secret"
    return "undo"


def result_summary(
    kind: str,
    params: dict[str, Any],
    status: str,
    title: str,
    *,
    failed_step: int | None = None,
) -> str:
    """One line for the collapsed card and the agent's results block.

    *failed_step* is the apply step a ``partial`` card stopped at, when known.
    """
    if kind == KIND_CREWMATE_DASHBOARD_PAGE and status in ("applied", "cancelled", "expired"):
        name = params.get("member")
        if status == "applied":
            return f"Yes: “{name}” is building its Dashboard page"
        return f"Not now: “{name}” keeps its current Dashboard page"
    if status == "applied":
        return f"Done: {title}"
    if status == "partial":
        if kind in CREWMATE_CREATING_KINDS:
            name = params.get("name")
            if kind == KIND_CREWMATE_IMPORT and failed_step:
                # The import's steps depend on what the file holds: name the
                # part that failed, not the create plan's fixed step 1.
                roles = import_step_roles(params)
                part = {
                    IMPORT_STEP_GOAL: "its goal",
                    IMPORT_STEP_RULES: "its rules",
                    IMPORT_STEP_FILES: "its skills, scripts and picture",
                    IMPORT_STEP_CRON: "its schedules",
                }.get(roles[failed_step] if failed_step < len(roles) else "", "every part")
                return (
                    f"Created mate “{name}” without {part} or anything after it. "
                    "Undo removes the mate and what was saved"
                )
            if failed_step == CREWMATE_CREATE_GOAL_STEP:
                # A failed step stops the plan, so a schedule after it never ran.
                rest = " or its schedule" if params.get("schedule") else ""
                return f"Created mate “{name}”, but its goal{rest} was not saved"
            if failed_step == CREWMATE_CREATE_SCHEDULE_STEP:
                return f"Created mate “{name}”, but its schedule was not saved"
        return f"Partly applied: {title}"
    if status == "undone":
        return f"Restored: {title}"
    if status == "failed":
        return f"Failed: {title}"
    if status == "cancelled":
        return f"Cancelled: {title}"
    if status == "expired":
        return f"Expired: {title}"
    return title


# ── manual-change memory events ──


def describe_manual_change(
    route: tuple[str, str],
    match_info: dict[str, str],
    body: Any,
    before: Any = None,
) -> str | None:
    """A concise memory line for an owner mutation, or ``None`` to record nothing.

    Names only: never a secret value, an env value, a token or an MCP spec.
    """
    method, template = route
    b = body if isinstance(body, dict) else {}
    name = match_info.get("name") or match_info.get("job_id") or match_info.get("id") or ""

    def s(value: Any, limit: int = 60) -> str:
        return _short(value, limit)

    if route == ("PATCH", "/api/config/kirocrew"):
        path = b.get("path")
        if not isinstance(path, str):
            return None
        segments = set(guide_catalog._SEGMENT_SPLIT.split(path.lower()))
        if segments & {"token", "secret", "password", "key", "credential", "credentials"}:
            return f"changed config {s(path)}"
        return f"changed config {s(path)} {s(before)}→{s(b.get('value'))}"
    if route == ("PUT", "/api/members/{slug}/goals/key-results"):
        raw_items = b.get("items")
        count = len(raw_items) if isinstance(raw_items, list) else 0
        return f"set {count} key results for mate {s(b.get('member'))}"
    if route == ("PUT", "/api/members/{slug}/goals/long-term"):
        return f"set the goal of mate {s(b.get('member'))} to {s(b.get('long_term'))}"
    if route == ("PUT", "/api/members/{slug}/goals/schedules"):
        fields = ", ".join(s(k) for k in CHECK_IN_FIELDS if k in b)
        return f"set the check-ins of mate {s(b.get('member'))} ({fields or 'none'})"
    if route == ("PUT", "/api/dashboard/config"):
        prior = before if isinstance(before, dict) else {}
        parts = []
        for k, v in sorted(b.items()):
            if k in DASHBOARD_KEYS:
                parts.append(f"{s(k)} {s(prior.get(k))}→{s(v)}")
            elif k in DASHBOARD_LIST_OP_KEYS:
                parts.append(f"{s(k)} {s(v)}")
            else:
                parts.append(s(k))
        return f"changed dashboard setting {', '.join(parts[:6])}" if parts else None
    if route == ("POST", "/api/crons"):
        when = b.get("cron") or b.get("every") or ("once" if b.get("at") else "")
        return f"created schedule “{s(b.get('name'))}” ({s(when)})"
    if route == ("PATCH", "/api/crons/{job_id}"):
        return f"updated schedule {s(name)} ({', '.join(sorted(str(k) for k in b)[:6])})"
    if route == ("DELETE", "/api/crons/{job_id}"):
        return f"deleted schedule {s(name)}"
    if route == ("POST", "/api/agents"):
        return f"created mate “{s(b.get('name'))}”"
    if route == ("PUT", "/api/agents/{name}"):
        return f"updated mate {s(name)} ({', '.join(sorted(str(k) for k in b)[:6])})"
    if route == ("DELETE", "/api/agents/{name}"):
        shown = before if isinstance(before, str) and before else name
        return f"deleted mate “{s(shown)}”"
    if route == ("PUT", "/api/agents/{name}/capabilities"):
        raw_ops = b.get("operations")
        ops: list[Any] = raw_ops if isinstance(raw_ops, list) else []
        ids = [
            f"{o.get('action')} {o.get('section')}:{o.get('id')}"
            for o in ops
            if isinstance(o, dict)
        ]
        return f"changed capabilities of {s(name)}: {s('; '.join(ids[:6]), 200)}"
    if route == ("PATCH", "/api/agents/detail/{name}"):
        return f"changed template {s(name)} ({', '.join(sorted(str(k) for k in b)[:6])})"
    if route == ("POST", "/api/mcp/discover/install"):
        return f"installed MCP server {s(b.get('id'))} from {s(b.get('provider'))}"
    if route == ("POST", "/api/mcp/custom"):
        servers = b.get("servers")
        servers = servers if isinstance(servers, dict) else {}
        return f"added custom MCP server(s) {s(', '.join(str(k) for k in servers))}"
    if route == ("POST", "/api/mcp/remove"):
        return f"removed MCP server {s(b.get('name'))}"
    if route == ("POST", "/api/mcp/apply"):
        changes = [c for c in (b.get("changes") or []) if isinstance(c, dict)]
        names = ", ".join(s(c.get("name")) for c in changes[:6])
        if changes and all(c.get("uninstall") is True for c in changes):
            return f"ran MCP uninstall for {names}"
        return f"changed MCP server scopes {names}" if names else None
    if route == ("POST", "/api/mcp/toggle"):
        return (
            f"{'enabled' if b.get('enabled', True) else 'disabled'} MCP server {s(b.get('name'))}"
        )
    if route == ("POST", "/api/mcp/toggle-tool"):
        verb = "enabled" if b.get("enabled", True) else "disabled"
        return f"{verb} MCP tool {s(b.get('server'))}/{s(b.get('tool'))}"
    if route == ("POST", "/api/connections/mint"):
        return f"started connecting {s(b.get('slug'))}"
    if route == ("POST", "/api/connections/disconnect"):
        return f"disconnected {s(b.get('slug'))}"
    if route == ("POST", "/api/secrets"):
        return f"saved secret {s(b.get('name'))}"
    if route == ("DELETE", "/api/secrets/{name}"):
        return f"deleted secret {s(name)}"
    if route == ("POST", "/api/security/trusted-apps/{name}"):
        return f"trusted app {s(name)}"
    if route == ("DELETE", "/api/security/trusted-apps/{name}"):
        return f"revoked trust for app {s(name)}"
    if route == ("POST", "/api/security/denied-commands/user"):
        return f"added denied-command rule {s(b.get('pattern'))}"
    if route == ("PATCH", "/api/security/denied-commands/user/{id}"):
        verb = "enabled" if b.get("enabled") else "disabled"
        return f"{verb} denied-command rule {s(name)}"
    if route == ("DELETE", "/api/security/denied-commands/user/{id}"):
        return f"deleted denied-command rule {s(name)}"
    del method, template
    return None


#: Every existing route a plan may reference, by ``(method, route template)``.
#: The gateway's card hook runs on exactly these. ``MEMORY_ROUTES`` is the subset
#: that mutates and so becomes a memory event; the other two are reads a plan
#: needs (a capability preview, an OAuth poll).
HOOKED_ROUTES: frozenset[tuple[str, str]] = frozenset(
    {
        ("PATCH", "/api/config/kirocrew"),
        ("PUT", "/api/dashboard/config"),
        ("POST", "/api/crons"),
        ("PATCH", "/api/crons/{job_id}"),
        ("DELETE", "/api/crons/{job_id}"),
        ("POST", "/api/agents"),
        ("PUT", "/api/agents/{name}"),
        ("DELETE", "/api/agents/{name}"),
        ("POST", "/api/agents/{name}/capabilities/preview"),
        ("PUT", "/api/agents/{name}/capabilities"),
        ("PATCH", "/api/agents/detail/{name}"),
        ("PUT", "/api/members/{slug}/goals/long-term"),
        ("PUT", "/api/members/{slug}/goals/key-results"),
        ("PUT", "/api/members/{slug}/goals/schedules"),
        ("PUT", "/api/members/{slug}/rules"),
        ("POST", "/api/members/{slug}/import-files"),
        ("DELETE", "/api/members/{slug}/import-files"),
        ("POST", "/api/members/{slug}/dashboard/page-offer"),
        ("POST", "/api/mcp/discover/install"),
        ("POST", "/api/mcp/custom"),
        ("POST", "/api/mcp/remove"),
        ("POST", "/api/mcp/apply"),
        ("POST", "/api/mcp/toggle"),
        ("POST", "/api/mcp/toggle-tool"),
        ("POST", "/api/connections/mint"),
        ("GET", "/api/connections/mint"),
        ("POST", "/api/connections/disconnect"),
        ("POST", "/api/secrets"),
        ("DELETE", "/api/secrets/{name}"),
        ("POST", "/api/security/trusted-apps/{name}"),
        ("DELETE", "/api/security/trusted-apps/{name}"),
        ("POST", "/api/security/denied-commands/user"),
        ("PATCH", "/api/security/denied-commands/user/{id}"),
        ("DELETE", "/api/security/denied-commands/user/{id}"),
        ("PATCH", "/api/chat/folders/{id}"),
    }
)
#: The folder PATCH is hooked only so a ``folder.steering`` card can be applied
#: through it; it is sidebar organization, not a setting, so it writes no
#: memory line. The rules PUT and the import-files routes are hooked so a
#: ``crewmate.import`` card can write a new crewmate's rules and files; the
#: rules are the owner's private boundary and the files are code, so neither
#: is described in Global memory.
MEMORY_ROUTES: frozenset[tuple[str, str]] = HOOKED_ROUTES - {
    ("POST", "/api/agents/{name}/capabilities/preview"),
    ("GET", "/api/connections/mint"),
    ("PATCH", "/api/chat/folders/{id}"),
    ("POST", "/api/members/{slug}/dashboard/page-offer"),
    ("PUT", "/api/members/{slug}/rules"),
    ("POST", "/api/members/{slug}/import-files"),
    ("DELETE", "/api/members/{slug}/import-files"),
}


if TYPE_CHECKING:  # every owner-defined name in _EXPORTS, for type checkers and IDEs
    from kiro_crew.change_card_catalog_kinds import (  # noqa: F401
        _APP_NAME_RE,
        _AT_FIELD,
        _AT_RE,
        _BOOL,
        _CANNOT_WIDEN_SETTINGS,
        _CODE_EXEC_SETTINGS,
        _CONFIG_PATH_RE,
        _CREWMATE_DESCRIPTION_MAX,
        _CREWMATE_GOAL_MAX,
        _CREWMATE_NAME_MAX,
        _CRON_EXPR_RE,
        _CRON_FIELDS,
        _ID_RE,
        _IMPORT_MAX_CRONS,
        _IMPORT_MAX_SKILLS,
        _IMPORT_PART_RE,
        _IMPORT_SCRIPT_RE,
        _IMPORT_SKILL_SOURCE_RE,
        _IMPORT_TEMPLATE_RE,
        _KR_TEXT_MAX,
        _KR_VALUE_MAX,
        _LIST_ITEM_MAX,
        _LIST_OPS,
        _LONG_TERM_GOAL_MAX,
        _MAX_CAPABILITY_OPS,
        _MAX_CUSTOM_SERVERS,
        _MAX_LIST_ITEMS,
        _MCP_NAME_RE,
        _NAME_MAX,
        _PARAMS_MAX_BYTES,
        _PATTERN_MAX,
        _PLACEHOLDER,
        _SETTING_ID_RE,
        _SETTING_ORDER,
        _SHA256_RE,
        _SLUG_RE,
        _STR,
        _TEMPLATE_LIST_FIELDS,
        _TEMPLATE_NAME_RE,
        _TEXT_MAX,
        APP_ONLY_KINDS,
        CREWMATE_FORMAT_VERSIONS,
        DEFAULT_CREWMATE_TEMPLATE,
        FOLDER_STEERING_DIR_MAX,
        KIND_SCHEDULE_UPDATE,
        KIND_TRUST_APP,
        KINDS,
        NOT_IMPORTED_CODES,
        OP_UNDO,
        REASON_MAX_CHARS,
        RISK_CODE_EXEC,
        RISK_NORMAL,
        RISK_TIGHTEN,
        RISK_WIDEN,
        SELF_ONLY_KINDS,
        CardCatalogError,
        KindDef,
        _check_mcp_spec_shapes,
        _clean_at,
        _clean_bool,
        _clean_cron,
        _clean_str,
        _fail,
        _goal_text,
        _import_parts,
        _member_name,
        _obj,
        _only,
        _str_list,
        _template_clear,
        check_editable,
        list_kinds,
        valid_mcp_name,
        validate_params,
    )
    from kiro_crew.change_card_catalog_plans import (  # noqa: F401
        _BOOL_VALUES,
        _WEEKDAY_NAMES,
        CHECK_IN_LABELS,
        DASHBOARD_LIST_KEYS,
        DASHBOARD_LIST_SETTINGS,
        DASHBOARD_SETTINGS,
        IMPORT_STEP_CREATE,
        LIST_SETTING_WORDING,
        PATH_FILL_VALUE,
        STORE_DASHBOARD,
        STORE_KIROCREW,
        _check_in_value,
        _create_schedule_step,
        _cron_body,
        _display,
        _impact_rows,
        _import_cron_step,
        _import_files_step,
        _import_member_fills,
        _import_message_crons,
        _import_preview,
        _import_rules_step,
        _kr_line,
        _launched_servers,
        _list_diff,
        _order_risk,
        _plain,
        _server_spec_rows,
        _setting_risk,
        _setting_risk_key,
        _step_fill,
        _step_path_fill,
        _user_fill,
        _widening_row,
        _with_fills,
        build_preview,
        import_files_path,
        is_path_fill,
        legacy_create_apply,
        list_after,
        placeholder,
        value_allowed,
    )

if TYPE_CHECKING:  # names it serves from outside the family, from their homes
    from kiro_crew import mate_capability_catalog as _mate_catalog  # noqa: F401
    from kiro_crew.config.sections import (  # noqa: F401
        DEFAULT_MODEL,
        MEMBER_APPROVAL_MODES,
        effective_pin_scope,
        normalize_agent_model,
    )

# This module is the facade of the change-card catalog: every name is reached through it
# as ``kiro_crew.change_card_catalog.<name>``. A read is served by the owner that binds
# the name, and a write -- ``monkeypatch.setattr``, ``mock.patch`` -- reaches every owner
# holding it (:mod:`kiro_crew._facade_mirror`). Owners never import this module. The
# evidence, undo, result and manual-change code above stays here. New work goes to the
# owner of its responsibility:
#
#   ``change_card_catalog_kinds`` -- the kinds, their parameter schemas and validation
#   ``change_card_catalog_plans`` -- the plan steps, setting tables and build_preview
#: Owner modules, lowest layer first: the first holder of a name answers its reads.
_OWNERS = (
    "kiro_crew.change_card_catalog_kinds",
    "kiro_crew.change_card_catalog_plans",
)
for _owner in _OWNERS:
    _importlib.import_module(_owner)
#: Name the facade serves but does not bind -> the owner that answers a read of it.
_EXPORTS: dict[str, str] = {}
_EXPORTS.update(_facade_mirror.install(_sys.modules[__name__], _OWNERS, excluded=()).served)
_sys.modules[__name__].__class__ = _facade_mirror.MirroredModule
#: What a star import binds: every public name of the unsplit module's namespace.
__all__ = sorted(n for n in set(globals()) | set(_EXPORTS) if not n.startswith("_"))
