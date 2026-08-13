"""The app-scoped tools reachable only through this server's credential tools: what they advertise and what they do.

``schemas()`` returns the ADVERTISEMENT half of each tool -- its name, the
model-facing description, and the JSON Schema a call is validated against.
``HANDLERS`` maps each of those names to the function that runs it. Both halves
of a tool live here so its contract and its behavior are read together, and
``test_mcp_tool_registry`` fails if one arrives without the other.

Handlers reach this server's shared plumbing as attributes of ``mcp_core`` --
``mcp_core._post``, the identity resolvers, the governance vets. That is
deliberate rather than untidy: an attribute lookup resolves at CALL time, so a
test that rebinds one on the module still intercepts the handler. Importing
those names directly here would bind them at import time and silently escape
every existing patch site.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from kiro_crew import mcp_core
from kiro_crew.platform import redact_via_context as redact
from kiro_crew.validation import (
    _ISSUE_RADAR_CREW_EVENT_KINDS,
    _ISSUE_RADAR_CREW_PHASES,
    _ISSUE_RADAR_CREW_SKIP_SCOPES,
    OPS_MISSION_CONTROL_ALLOWED_CALLS,
    sanitize_json_values,
)


def schemas() -> list[dict[str, Any]]:
    """Descriptors for the apps tools."""
    return [
        {
            "name": "issue_radar_record_investigation",
            "description": (
                "Record your conclusion on an Issue Radar investigation, so the "
                "verdict and summary appear on the issue's card instead of living "
                "only in this chat. Call this when you finish investigating an "
                "issue or PR opened via Issue Radar's Investigate button — the "
                "seed prompt names the owner, repo and number to pass back. Do "
                "NOT call it from a Review session: that prompt asks for a draft "
                "and forbids recording anything. "
                "This is the ONLY way to persist findings: a raw HTTP PUT to the "
                "same endpoint has no credential and is refused with 403. Local "
                "triage state only — nothing is written to GitHub or GitLab. "
                "Merges into any existing record, so a partial update is fine."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "owner": {"type": "string", "description": "Repo owner / group"},
                    "repo": {"type": "string", "description": "Repo / project name"},
                    "number": {"type": "integer", "description": "Issue or PR number"},
                    "provider": {
                        "type": "string",
                        "enum": ["github", "gitlab"],
                        "description": "Forge the repo lives on (default github)",
                    },
                    "host": {
                        "type": "string",
                        "description": "Forge host, e.g. github.com or a self-hosted GitLab",
                    },
                    "kind": {
                        "type": "string",
                        "enum": ["issue", "pull"],
                        "description": (
                            "Which sequence the number belongs to. Matters on GitLab, "
                            "where issue #5 and merge request !5 are unrelated items"
                        ),
                    },
                    "status": {
                        "type": "string",
                        "enum": ["investigating", "resolved", "archived"],
                        "description": "Record status (default resolved)",
                    },
                    "verdict": {
                        "type": "string",
                        "description": "bug | feature | question | duplicate | needs-info",
                    },
                    "root_cause": {
                        "type": "string",
                        "description": "Root cause or the code area involved",
                    },
                    "suggested_labels": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Labels you recommend for this item",
                    },
                    "next_action": {"type": "string", "description": "Recommended next action"},
                    "summary": {"type": "string", "description": "One-paragraph summary"},
                },
                "required": ["owner", "repo", "number"],
            },
        },
        {
            "name": "ops_mission_control_api",
            "description": (
                "Call the Ops Mission Control app's HTTP API with the gateway's "
                "own credential. This is the ONLY way an agent session reaches "
                "that API: raw HTTP has no credential and is refused with 403, "
                "and no CLI or environment variable provides one. Use it for "
                "every call an ops-mission-control SOP asks for — reading "
                "state/signals/incidents/rotation/ledger, claiming and "
                "transitioning incidents, arming the rotation, posting ledger "
                "entries. Paths are rooted at the app base (pass '/state', not "
                "the full URL) and only the SOP surface is reachable: "
                "provider configuration, settings, webhook ingest and the "
                "human proposal-decision routes are deliberately not callable "
                "from here."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "method": {
                        "type": "string",
                        "enum": ["GET", "POST"],
                        "description": "HTTP method",
                    },
                    "path": {
                        "type": "string",
                        # Derived from the frozen allowlist so the advertised
                        # schema cannot drift from what the handler admits.
                        "enum": sorted({p for _, p in OPS_MISSION_CONTROL_ALLOWED_CALLS}),
                        "description": (
                            "API path relative to /api/apps/ops-mission-control. "
                            "GET: /state /signals /incidents /handover /rotation "
                            "/ledger /ledger/contradictions. POST: /dispatch "
                            "/incident/transition /incident/claim /incident/action "
                            "/rotation/arm /ledger /ledger/hygiene."
                        ),
                    },
                    "query": {
                        "type": "string",
                        "description": (
                            "Optional query string without the leading '?', GET only "
                            "— e.g. 'status=investigating' or 'id=INV-42' on /incidents"
                        ),
                    },
                    "body_json": {
                        "type": "string",
                        "description": (
                            "JSON object for POST bodies, serialized as a string — "
                            "e.g. '{\"id\": \"INV-42\", \"status\": \"resolved\"}' for "
                            "/incident/transition"
                        ),
                    },
                },
                "required": ["method", "path"],
            },
        },
        {
            "name": "issue_radar_crew_read",
            "description": (
                "Read your Issue Radar crew's ledger: the crew record, the "
                "repo's protocol settings, and every work item that is not "
                "finished — each with its phase, its `next` step, what was "
                "already tried and rejected, its worktree, branch, PR and last "
                "CI reading. Takes no arguments: the crew is resolved from this "
                "session, so you cannot read another crew's ledger. "
                "It also returns `skipped_numbers` and `recent_skips` — the "
                "SHARED skip index for this repository, written by every crew "
                "on it, not just you. CHECK an issue against "
                "`skipped_numbers` BEFORE you investigate it: a number in that "
                "list has already been passed on and re-investigating it is "
                "wasted work that every crew would repeat. `recent_skips` says "
                "why the recent passes happened. "
                "Your per-turn nudge already carries a snapshot, so call this "
                "for the two cases a snapshot cannot cover: a turn long enough "
                "that the snapshot has gone stale, and a resume after "
                "compaction or a gateway restart where you must re-establish "
                "what you were doing before writing anything. "
                "This is the ONLY read path: a raw HTTP GET to the same "
                "endpoint has no credential and is refused with 403."
            ),
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "issue_radar_crew_record",
            "description": (
                "Record one step of Issue Radar crew work: it updates the work "
                "item AND appends one progress line, in a single call. There is "
                "deliberately no separate 'append event' tool — a phase must "
                "never move without a logged reason — so `event` and "
                "`event_kind` are REQUIRED whenever you pass `phase`. "
                "The ledger is your memory, not your report: write what a cold "
                "resume needs (`next` as an intent — 'add the Windows branch to "
                "_safe_chmod, the test already fails' — plus worktree, branch, "
                "base_sha, and any approach you tried and rejected). Fields you "
                "omit are left as an earlier write stored them, so a partial "
                "update is fine and is never a way to erase state. "
                "Setting `phase` to `skipped` also writes this repository's "
                "SHARED skip index, so every other crew sees the pass and none "
                "of them re-investigates the issue — pass `skip_scope` to say "
                "what kind of pass it was (architecture, new-feature, "
                "needs-design, needs-decision, needs-investigation, duplicate, "
                "already-fixed, not-reproducible, wrong-root-cause, "
                "breaking-change, gate-config, other) and put "
                "the real explanation in `why`, which is what the next crew "
                "reads. "
                "The crew and repo come from this session, not from arguments. "
                "WARNING — `event` and `why` BECOME PUBLIC: they are rendered into "
                "your claim comment on the forge as well as on your crew page. "
                "Never "
                "put an absolute path, a host name or anything else about the "
                "machine you run on in them; worktree paths belong in "
                "`worktree`, which stays local. "
                "This is the ONLY write path: a raw HTTP PUT to the same "
                "endpoint has no credential and is refused with 403."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "number": {
                        "type": "integer",
                        "description": "Issue number this step belongs to",
                    },
                    "phase": {
                        "type": "string",
                        "enum": sorted(_ISSUE_RADAR_CREW_PHASES),
                        "description": (
                            "Work-item phase. Requires `event` + `event_kind`. "
                            "Only one item may be in `implementing` or "
                            "`addressing-review` at a time — a second is refused"
                        ),
                    },
                    "skip_scope": {
                        "type": "string",
                        "enum": sorted(_ISSUE_RADAR_CREW_SKIP_SCOPES),
                        "description": (
                            "Only with `phase: skipped`. What kind of pass this "
                            "is, for the repo-wide shared skip index. Optional — "
                            "an omitted or unrecognised value is recorded as "
                            "`other`, and the pass is indexed either way"
                        ),
                    },
                    "outcome": {
                        "type": "string",
                        "description": "Why it ended. Set only in a terminal phase",
                    },
                    "next": {
                        "type": "string",
                        "description": (
                            "The resumable intent — the concrete next step, not a status word"
                        ),
                    },
                    "decision": {
                        "type": "string",
                        "description": "What you decided to do",
                    },
                    "why": {"type": "string", "description": "On what grounds"},
                    "tried_approach": {
                        "type": "string",
                        "description": (
                            "An approach you tried and rejected — appended, so a "
                            "resumed turn does not re-walk it"
                        ),
                    },
                    "tried_rejected_because": {
                        "type": "string",
                        "description": "Why that approach was rejected",
                    },
                    "worktree": {
                        "type": "string",
                        "description": "Absolute worktree path (local only, never made public)",
                    },
                    "branch": {"type": "string", "description": "Working branch name"},
                    "base_sha": {
                        "type": "string",
                        "description": "Base commit the branch was cut from",
                    },
                    "pr_number": {"type": "integer", "description": "PR / merge request number"},
                    "ci_state": {
                        "type": "string",
                        "description": "Latest CI verdict, e.g. success / failure / pending",
                    },
                    "ci_passed": {"type": "integer", "description": "Checks passing"},
                    "ci_total": {"type": "integer", "description": "Checks total"},
                    "ci_round": {"type": "integer", "description": "Which CI round this is"},
                    "ci_inherited_reds": {
                        "type": "integer",
                        "description": (
                            "Failures already red on the base — record these so you "
                            "do not rebase at the base branch's own breakage"
                        ),
                    },
                    "claim_comment_id": {
                        "type": "integer",
                        "description": "Id of your claim comment, so it can be edited in place",
                    },
                    "labels_applied": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Labels you applied, so a hand-back removes exactly those",
                    },
                    "event": {
                        "type": "string",
                        "description": (
                            "PUBLIC one-line progress note, e.g. 'CI round 3 — 41/47 "
                            "green, 6 inherited from main'. No paths, no host names"
                        ),
                    },
                    "event_kind": {
                        "type": "string",
                        "enum": sorted(_ISSUE_RADAR_CREW_EVENT_KINDS),
                        "description": "Which kind of step this line records",
                    },
                },
                "required": ["number"],
            },
        },
    ]


def _crew_session_key() -> tuple[str, str]:
    """Strictly-resolved session key for the crew tools, or an error to return.

    Returns ``(key, "")`` on success and ``("", message)`` when identity is not
    directly attributable. Both crew tools send THIS key rather than resolving
    their own, so the identity that passed the gate is the identity on the wire.
    """
    _crew_sk = mcp_core._resolve_session_key_strict()
    if not _crew_sk:
        return "", (
            "Error: this tool needs a directly-identified dashboard session. "
            "A subagent resolves to its parent's session, which would read and "
            "write the parent crew's ledger. Run this from the crew's own "
            "session."
        )
    return _crew_sk, ""


def issue_radar_record_investigation(name: str, args: dict[str, Any]) -> str:
    _ir_findings: dict[str, Any] = {
        key: redact(args[key])
        for key in ("verdict", "root_cause", "next_action", "summary")
        if args.get(key)
    }
    _ir_labels = [redact(s) for s in (args.get("suggested_labels") or []) if s]
    if _ir_labels:
        _ir_findings["suggested_labels"] = _ir_labels
    # provider/host/kind are sent EXPLICITLY, never left to the server
    # default: the record is keyed on them, so a GitLab item recorded as
    # public GitHub would write into — and could overwrite — a same-slug
    # GitHub repo's record. Same reason the seed prompt echoes them.
    _ir_body: dict[str, Any] = {
        "owner": args["owner"],
        "repo": args["repo"],
        "number": args["number"],
        "provider": args.get("provider") or "github",
        "host": args.get("host") or "github.com",
        "kind": args.get("kind") or "issue",
        "status": args.get("status") or "resolved",
    }
    if _ir_findings:
        _ir_body["findings"] = _ir_findings
    _ir_resp = mcp_core._put("/api/apps/issue-radar/investigation", _ir_body)
    if _ir_resp.get("error"):
        return f"Error: {_ir_resp['error']}"
    _ir_saved = (_ir_resp.get("investigation") or {}).get("findings") or {}
    _ir_ref = (
        f"{_ir_body['owner']}/{_ir_body['repo']}"
        f"{'!' if _ir_body['kind'] == 'pull' and _ir_body['provider'] == 'gitlab' else '#'}"
        f"{_ir_body['number']}"
    )
    if not _ir_saved:
        return (
            f"Recorded status `{_ir_body['status']}` for {_ir_ref} "
            "(no findings supplied — the card will show the status badge only)."
        )
    _ir_verdict = _ir_saved.get("verdict") or "(no verdict)"
    return (
        f"Recorded investigation for {_ir_ref}: status `{_ir_body['status']}`, "
        f"verdict `{_ir_verdict}`. It now shows on the item's Issue Radar card."
    )


def ops_mission_control_api(name: str, args: dict[str, Any]) -> str:
    _omc_method = args["method"]
    _omc_path = args["path"]
    if (_omc_method, _omc_path) not in OPS_MISSION_CONTROL_ALLOWED_CALLS:
        return (
            f"Error: {_omc_method} {_omc_path} is not part of the "
            "ops-mission-control agent surface."
        )
    _omc_body: dict[str, Any] = {}
    _omc_body_raw = args.get("body_json") or ""
    if _omc_body_raw:
        try:
            _omc_parsed = json.loads(_omc_body_raw)
        except (json.JSONDecodeError, ValueError):
            return "Error: body_json is not valid JSON."
        if not isinstance(_omc_parsed, dict):
            return "Error: body_json must encode a JSON object."
        # Schema sanitization saw body_json as one opaque string, where a
        # ``\u200b`` escape is plain ASCII — the hidden character only
        # materializes on decode. Walk the decoded structure so smuggled
        # invisibles cannot split a credential past downstream redaction.
        # Then redact on the way IN as well: ledger entries persist and
        # sync to a configured remote, so a credential quoted into a body
        # field must be scrubbed BEFORE ``_post``, not only on the
        # response path.
        _omc_body = mcp_core._redact_json_strings(sanitize_json_values(_omc_parsed))
    _omc_query = args.get("query") or ""
    _omc_url = "/api/apps/ops-mission-control" + _omc_path
    if _omc_query:
        _omc_url += "?" + _omc_query
    _omc_resp = mcp_core._get(_omc_url) if _omc_method == "GET" else mcp_core._post(_omc_url, _omc_body)
    # Serialize compactly and redact on the way OUT: signals, incident
    # titles and ledger entries carry text from external monitoring
    # systems and prior LLM turns, so a credential or exfil URL quoted
    # into one would otherwise flow straight into this agent's context.
    # Redact BEFORE truncating: slicing first could cut a credential in
    # half at the cap so the redaction pattern no longer matches, leaking
    # the surviving fragment.
    _omc_text = redact(json.dumps(_omc_resp, ensure_ascii=False, default=str))
    _omc_cap = 60_000
    if len(_omc_text) > _omc_cap:
        _omc_text = (
            _omc_text[:_omc_cap]
            + f"\n… truncated ({len(_omc_text)} chars total). Narrow the "
            "call (e.g. query filters) to see the rest."
        )
    return _omc_text


def issue_radar_crew_read(name: str, args: dict[str, Any]) -> str:
    _crew_sk, _crew_err = _crew_session_key()
    if _crew_err:
        return _crew_err
    _cr_payload = mcp_core._get(mcp_core._CREW_READ_PATH, session_key=_crew_sk)
    if _cr_payload.get("error"):
        return f"Error: {_cr_payload['error']}"
    if mcp_core._crew_identity(_cr_payload) is None:
        return (
            "Error: this session is not bound to an Issue Radar crew, so there "
            "is no ledger to read. Only a crew's own session can use this tool."
        )
    _cr_view = mcp_core._crew_ledger_view(_cr_payload)
    # Redact the OUTPUT too: the ledger holds LLM prose written from
    # untrusted issue text, and a resume re-reads it into context. Paths in
    # `worktree` survive this pass (it removes credentials and exfil URLs,
    # not paths) — which is required, since the resume needs them.
    return redact(json.dumps(_cr_view, indent=2, ensure_ascii=False))


def issue_radar_crew_record(name: str, args: dict[str, Any]) -> str:
    _crew_sk, _crew_err = _crew_session_key()
    if _crew_err:
        return _crew_err
    _cw_payload = mcp_core._get(mcp_core._CREW_READ_PATH, session_key=_crew_sk)
    if _cw_payload.get("error"):
        return f"Error: {_cw_payload['error']}"
    _cw_identity = mcp_core._crew_identity(_cw_payload)
    if _cw_identity is None:
        return (
            "Error: this session is not bound to an Issue Radar crew, so there "
            "is no ledger to write. Only a crew's own session can use this tool."
        )
    _cw_owner, _cw_repo, _cw_crew_id = _cw_identity

    _cw_body: dict[str, Any] = {
        "owner": _cw_owner,
        "repo": _cw_repo,
        "crew_id": _cw_crew_id,
        "number": args["number"],
    }
    # Local-only resume fields, passed through verbatim. NOT scrubbed: an
    # absolute worktree path is the point of the field, and it is never
    # rendered into a comment (crew_store keeps these local).
    for _cw_key in ("worktree", "branch", "base_sha"):
        if args.get(_cw_key):
            _cw_body[_cw_key] = args[_cw_key]
    # `phase` is an allowlisted enum value (validation rejects anything
    # else), so it is passed verbatim — redacting a closed vocabulary would
    # only obscure where the real sanitizing happens.
    if args.get("phase"):
        _cw_body["phase"] = args["phase"]
    # Classification for the repo-wide shared skip index. Forwarded raw: the
    # store coerces an unrecognised value to `other` rather than refusing, so
    # a mislabelled pass is still an indexed pass (see crew_store.SKIP_SCOPES).
    if args.get("skip_scope"):
        _cw_body["skip_scope"] = args["skip_scope"]
    # Prose that is rendered on the crew page. Redacted for the same reason
    # the investigation tool redacts its findings — it is LLM prose about an
    # untrusted issue body, stored verbatim and re-displayed on every visit.
    for _cw_key in ("outcome", "next", "decision", "why"):
        if args.get(_cw_key):
            _cw_body[_cw_key] = redact(args[_cw_key])
    if args.get("tried_approach"):
        _cw_body["tried_approach"] = redact(args["tried_approach"])
        if args.get("tried_rejected_because"):
            _cw_body["tried_rejected_because"] = redact(args["tried_rejected_because"])
    if args.get("pr_number"):
        _cw_body["pr_number"] = args["pr_number"]
    if args.get("claim_comment_id"):
        _cw_body["claim_comment_id"] = args["claim_comment_id"]
    # Presence, not truthiness: `labels_applied: []` is the crew SAYING it now
    # holds no labels, which is what it reports after removing its last one.
    # Gating on the list being non-empty made that indistinguishable from not
    # mentioning labels at all, so the store kept the previous set and the
    # crew's record claimed labels it had just taken off the issue.
    if "labels_applied" in args:
        _cw_body["labels_applied"] = [
            redact(s) for s in (args.get("labels_applied") or []) if s
        ]
    # The flat ci_* args are re-assembled into the store's `ci_state` dict
    # (crew_store merges it key-by-key). `ci_state` the ARG is the forge's
    # verdict word and becomes the dict's `state`; an int reading of 0 is
    # meaningful (0/47 green, 0 inherited reds) so these are dropped on
    # "not supplied", not on falsiness.
    _cw_ci: dict[str, Any] = {}
    if args.get("ci_state"):
        _cw_ci["state"] = args["ci_state"]
    for _cw_arg, _cw_field in (
        ("ci_passed", "passed"),
        ("ci_total", "total"),
        ("ci_round", "round"),
        ("ci_inherited_reds", "inherited_reds"),
    ):
        if args.get(_cw_arg) is not None:
            _cw_ci[_cw_field] = args[_cw_arg]
    if _cw_ci:
        _cw_body["ci_state"] = _cw_ci
    # The progress line: rendered inside the <details> block of the claim
    # comment on the forge, so it is the strictest string in this payload.
    if args.get("event"):
        _cw_body["event"] = mcp_core._crew_public_text(args["event"])
        _cw_body["event_kind"] = args["event_kind"]

    _cw_resp = mcp_core._put(mcp_core._CREW_WORK_PATH, _cw_body, session_key=_crew_sk)
    if _cw_resp.get("error"):
        return f"Error: {_cw_resp['error']}"
    _cw_raw_item = _cw_resp.get("item")
    _cw_item: dict[str, Any] = _cw_raw_item if isinstance(_cw_raw_item, dict) else {}
    _cw_ref = f"{_cw_owner}/{_cw_repo}#{args['number']}"
    _cw_phase = _cw_item.get("phase") or _cw_body.get("phase") or "(phase unchanged)"
    _cw_lines = [f"Recorded {_cw_ref}: phase `{_cw_phase}`."]
    if _cw_body.get("event"):
        # Echo the stored line, not the argument — if a sanitizer pass
        # changed it, the crew must see what actually became public.
        _cw_raw_event = _cw_resp.get("event")
        _cw_ev: dict[str, Any] = _cw_raw_event if isinstance(_cw_raw_event, dict) else {}
        _cw_stored_event = _cw_ev.get("text") or _cw_body["event"]
        _cw_lines.append(f"Logged ({_cw_body['event_kind']}): {_cw_stored_event}")
    if _cw_item.get("next"):
        _cw_lines.append(f"Next: {_cw_item['next']}")
    # Confirm the SHARED index write. The brief promises that recording
    # `phase: skipped` is itself what tells every other crew in the repo not to
    # re-investigate this issue, so the crew has to see that it landed —
    # otherwise the one guarantee that stops the fleet looping is invisible to
    # the only party that can act on it. Echoing the STORED scope also shows a
    # coercion: an unrecognised scope is filed as `other`, and a crew that
    # believed it recorded `architecture` should see what was really kept.
    _cw_raw_skip = _cw_resp.get("skip")
    if isinstance(_cw_raw_skip, dict) and _cw_raw_skip.get("number") is not None:
        _cw_lines.append(
            f"Shared skip index: #{_cw_raw_skip['number']} recorded as "
            f"`{_cw_raw_skip.get('scope') or 'other'}` — no other crew in this "
            "repository will investigate it now."
        )
    return redact("\n".join(_cw_lines))


HANDLERS: dict[str, Callable[[str, dict[str, Any]], str]] = {
    "issue_radar_record_investigation": issue_radar_record_investigation,
    "ops_mission_control_api": ops_mission_control_api,
    "issue_radar_crew_read": issue_radar_crew_read,
    "issue_radar_crew_record": issue_radar_crew_record,
}
