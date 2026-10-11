"""The app-scoped tools reachable only through this server's credential tools: what they advertise and what they do.

``schemas()`` returns the DECLARATION half of each tool -- its name, the
model-facing description, and the JSON Schema a call is validated against.
``HANDLERS`` maps each of those names to the function that runs it. Both halves
of a tool live here so its contract and its behavior are read together, and
``test_mcp_tool_registry`` fails if one arrives without the other.

``advertised()`` is what ``tools/list`` actually emits: the declaration minus
the tools of every app that is switched off. Each tool here belongs to one
built-in app (``TOOL_APPS``) whose gateway routes refuse a call while the app is
disabled, so advertising the tool regardless tells the model about a capability
that can only refuse. The listing and the call read the SAME
``installed.json`` through the same module; ``advertised()`` states the one
state in which the two readers differ.

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
from urllib.parse import quote

from kiro_crew import mcp_core
from kiro_crew.apps import manager as app_manager
from kiro_crew.apps.manager import app_enabled_state
from kiro_crew.apps.manifest import agent_route_matches, parse_agent_route
from kiro_crew.constants import APP_REQUEST_HEADER, APP_REQUEST_HEADER_VALUE
from kiro_crew.platform import redact_via_context as redact
from kiro_crew.validation import (
    _ISSUE_RADAR_CREW_CLEARABLE_FIELDS,
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
                "Pass provider, host and kind with owner, repo and number: "
                "together they pick which record is written. Within one "
                "investigation run a write merges into the existing record, so a "
                "partial update is fine; the first findings written after the "
                "item's session changed (e.g. Start over) replace the previous "
                "run's findings."
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
                        "description": "Forge the repo lives on",
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
                "required": ["owner", "repo", "number", "provider", "host", "kind"],
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
                            'e.g. \'{"id": "INV-42", "status": "resolved"}\' for '
                            "/incident/transition"
                        ),
                    },
                },
                "required": ["method", "path"],
            },
        },
        {
            "name": "design_tweak_update_thread",
            "description": (
                "Report progress on one Design Tweak comment thread, so its dot "
                "in the live preview updates as you apply the batched edits. "
                "This is the ONLY way an agent session can update a thread: the "
                "raw `POST /thread` the skill once documented needs a dashboard "
                "credential the session does not hold (the credential-exfil "
                "safety floor blocks minting one), so it 403s and the dots never "
                "change. The MCP server process holds the credential and you "
                "never see one. Pass `request_id` (the request's id) and usually "
                "`comment_id` (the `cid` of the specific comment you are working) "
                "so the note lands on that comment's bubble; omit `comment_id` "
                "only for a note that spans the whole batch. Post a short `text` "
                "note when you start a comment and at each meaningful step; when "
                "that comment is finished, send `status='done'` so its dot turns "
                "green. `status` accepts only `done` — the tool reports forward "
                "progress and exposes no destructive thread action (it cannot "
                "clear or dismiss a request). `text` or `status` is required. "
                "Never write the request file yourself; progress is reported "
                "ONLY through this tool."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "request_id": {
                        "type": "string",
                        "description": (
                            "The request's id (the `id` the request file is "
                            "keyed by). Matches `^[A-Za-z0-9._-]+$`."
                        ),
                    },
                    "comment_id": {
                        "type": "string",
                        "description": (
                            "The `cid` of the specific comment to report against, "
                            "so the note lands on that comment's bubble. Omit for "
                            "a request-level note that spans the whole batch "
                            "(e.g. 'rebuilding, one moment'). Matches "
                            "`^[A-Za-z0-9._-]+$`."
                        ),
                    },
                    "text": {
                        "type": "string",
                        "description": (
                            "A single short progress line, e.g. 'Editing "
                            "styles.css — uppercasing .section-title'. Required "
                            "unless `status` is given."
                        ),
                    },
                    "status": {
                        "type": "string",
                        "enum": ["done"],
                        "description": (
                            "Set `done` to mark the comment finished so its dot "
                            "turns green. Only `done` is accepted. Required "
                            "unless `text` is given."
                        ),
                    },
                },
                "required": ["request_id"],
            },
        },
        {
            "name": "pod_up",
            "description": (
                "Boot a Kiro Crew POD -- a complete preview gateway for one git "
                "worktree, on its own port with its own data directory -- and get "
                "back the {base_url, token} handle for driving it. On a host whose "
                "sandbox denies this session the systemd user bus, this is the only "
                "way to start a pod: a pod is a systemd --user unit, and "
                "`kirocrew pod up` in your shell then fails with `Permission "
                "denied`. Check with `systemctl --user is-system-running` if you "
                "want to know which case you are in; either way this tool works, "
                "because the gateway holds the host bus and does the systemd part "
                "for you. Use it to test an unfinished change end to end without "
                "touching the live gateway. Blocking: it returns once the pod "
                "answers its own health check, a few seconds for a worktree that "
                "is already built. A worktree with no built dist is REFUSED with "
                "the CLI's own remedy in the message -- provisioning (venv + SPA "
                "build) is minutes of work and is not reachable from here; run it "
                "from the Dev Fleet page or `kirocrew pod provision <wt>`. "
                "Requires the Dev Fleet app to be enabled. The returned token is a "
                "2h credential scoped to that pod alone; it is not the live "
                "gateway's, and no secret of the host is exposed by it. "
                "Release the pod with `pod_down` when done -- a pod holds a port "
                "and memory until it is stopped."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "worktree": {
                        "type": "string",
                        "description": (
                            "The worktree to bring up, named the way `pod_ls` and "
                            "the Dev Fleet page name it -- its directory basename, "
                            "e.g. 'kc-wt-10732'. NOT a path and NOT a branch"
                        ),
                    },
                },
                "required": ["worktree"],
            },
        },
        {
            "name": "pod_down",
            "description": (
                "Stop a pod and reclaim its isolated data directory. Call this when "
                "you are finished with a pod you started: until then it holds its "
                "port, its memory and its unit. Same reason as `pod_up` for why "
                "this is a tool and not a shell command -- the systemd user bus is "
                "not reachable from this session. This DELETES that pod's data "
                "directory, which is by design (a pod is disposable) but is not "
                "undoable: anything you want to keep must be copied out of the pod "
                "first. It does not touch the worktree, its branch, or your commits."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "worktree": {
                        "type": "string",
                        "description": "The worktree whose pod should be stopped",
                    },
                },
                "required": ["worktree"],
            },
        },
        {
            "name": "pod_status",
            "description": (
                "Whether one worktree's pod is up, on which port, and what its "
                "health probe answers. Use it to check a pod you started is still "
                "serving before driving it, and to tell 'my request failed' apart "
                "from 'the pod is not running'. A `health` of 200/401/403 means "
                "this pod is up; 0 means nothing answers on that port; -2 means "
                "another process answers on that port, so this pod is not up."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "worktree": {
                        "type": "string",
                        "description": "The worktree whose pod state you want",
                    },
                },
                "required": ["worktree"],
            },
        },
        {
            "name": "pod_ls",
            "description": (
                "Every pod currently ACTIVE on this host, with its port and health. "
                "Takes no arguments. Read this before starting a pod: it is what "
                "tells you a pod you need is already up (start nothing), or that "
                "someone else's run owns the port yours would derive. The list is "
                "deliberately not filtered to one repository -- a filtered list "
                "would hide exactly the pod that explains a refusal."
            ),
            "inputSchema": {"type": "object", "properties": {}},
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
                "To record that you checked the queue and took NOTHING, send "
                "`event_kind: sweep` with NO `number` — never invent an issue "
                "number for a cycle you did no work in. That writes one "
                "crew-level line and no work item, so it also takes none of the "
                "work-item fields. "
                "WARNING — `event` BECOMES PUBLIC: it is rendered into your claim "
                "comment on the forge as well as on your crew page. `why` is not "
                "posted to the forge, but it shows on your crew page and, for a "
                "skip, in the skip index every crew on this repo reads. Never "
                "put an absolute path, a host name or anything else about the "
                "machine you run on in either; worktree paths belong in "
                "`worktree`, which stays local. "
                "This is the ONLY write path: a raw HTTP PUT to the same "
                "endpoint has no credential and is refused with 403."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "number": {
                        "type": "integer",
                        "description": (
                            "Issue number this step belongs to. Omit it ONLY with "
                            "`event_kind: sweep`, to record that you checked the "
                            "queue and took nothing — never invent a number for that"
                        ),
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
                    "clear": {
                        "type": "array",
                        "items": {
                            "type": "string",
                            "enum": sorted(_ISSUE_RADAR_CREW_CLEARABLE_FIELDS),
                        },
                        "description": (
                            "Fields to EMPTY, by name — the only way to erase one, since "
                            "an omitted field is left as stored. `pr_number` once the "
                            "pull request is closed, `claim_comment_id` once the claim "
                            "comment is gone, `next` when there is no next step. A field "
                            "both cleared and set in the same call keeps the set value"
                        ),
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
                # Nothing is unconditionally required. `number` was, which left a
                # crew that swept an empty queue no way to record the cycle without
                # inventing an issue number; the backend now pairs a missing number
                # with the crew-level `sweep` kind and refuses every other
                # combination, so the coupling is enforced where the write happens
                # rather than by a schema that cannot express "one or the other".
                "required": [],
            },
        },
        {
            "name": "app_request",
            "description": (
                "Call an installed app's declared agent route: one of the "
                '`"METHOD /path"` entries in the app\'s `agentRoutes` manifest '
                "field. Apps document their agent routes, and what each one "
                "expects, in the skills they ship, so read the app's skill first. "
                "The gateway refuses an undeclared route, and this tool refuses "
                "a disabled app. The app is told which session is calling, so "
                "an unidentified caller is refused. A refusal before the gateway is "
                "safe to retry; a transport failure after a mutation has an unknown "
                "outcome and must be read back with GET instead of resent. Otherwise "
                "the tool returns the gateway's or app's answer. This is the ONLY way "
                "to reach an app's routes: raw HTTP has no credential and is refused "
                "with 403."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "app": {
                        "type": "string",
                        "description": "The installed app's name, e.g. 'slack-poller'",
                    },
                    "method": {
                        "type": "string",
                        "enum": ["DELETE", "GET", "PATCH", "POST", "PUT"],
                        "description": "HTTP method of the declared route",
                    },
                    "path": {
                        "type": "string",
                        "description": (
                            "Route path relative to the app, with parameters filled "
                            "in, e.g. '/subscriptions/42'. No query string"
                        ),
                    },
                    "body": {
                        "type": "object",
                        "description": "JSON body for POST, PUT, PATCH or DELETE",
                    },
                },
                "required": ["app", "method", "path"],
            },
        },
    ]


#: Which built-in app each tool reaches, by the app's installed name (the directory
#: under ``apps/`` whose ``installed.json`` the app's gateway routes gate on). Spelled
#: as literals rather than imported from the apps: each app's ``APP_NAME`` lives in
#: a backend module that drags that app's whole import graph into this server
#: process. ``test_mcp_apps_tool_gate`` pins every entry to the real constant.
TOOL_APPS: dict[str, str] = {
    "issue_radar_record_investigation": "issue-radar",
    "issue_radar_crew_read": "issue-radar",
    "issue_radar_crew_record": "issue-radar",
    "ops_mission_control_api": "ops-mission-control",
    "design_tweak_update_thread": "design-tweak",
    "pod_up": "dev-fleet",
    "pod_down": "dev-fleet",
    "pod_status": "dev-fleet",
    "pod_ls": "dev-fleet",
}


def advertised() -> list[dict[str, Any]]:
    """The descriptors ``tools/list`` emits now: ``schemas()`` minus disabled apps' tools.

    The listing and the call read ONE file through ONE module -- the app's
    ``installed.json`` via ``kiro_crew.apps.manager`` -- with two readers. Every
    route these tools reach refuses the call with HTTP 403 while the binary
    ``is_app_enabled`` does not prove the app enabled (Dev Fleet's routes and the
    app reverse proxy Design Tweak sits behind name the refusal
    ``app_not_enabled``, Ops Mission Control ``app_disabled``, Issue Radar carries
    no code). This reads the tri-state ``app_enabled_state`` and hides only on a
    readable ``False`` -- not installed, or installed and switched off. So a
    hidden tool is always one whose call would be refused, while a listed tool is
    not always one whose call would succeed: an unreadable record lists tools the
    route refuses. Hiding is a product decision, not a security one -- the route
    stays the enforcement point, as it does for the computer-use tools -- and it
    exists because kiro-cli caches ``tools/list`` once per session, so a disabled
    app's tools would otherwise spend context in every request of every session
    that can never use them.

    Fail-OPEN on a read fault, as the per-session policy filter does: ``None``
    (corrupt JSON, a dangling link, a directory in the file's place) lists the
    tools, and so does a read that raises. The same once-per-session cache that
    motivates hiding makes hiding on a transient fault permanent for the
    session's whole life, while a listed tool whose call the gateway refuses is
    not a hole; an exception escaping this build, by contrast, would withdraw
    EVERY tool of the pooled server for every session that shares it.

    Only the FULL descriptor build reaches this function. The names-only build
    (``mcp_tools.build_tool_names``, which in-process discovery reads on the
    gateway's event loop) takes ``schemas()`` directly -- the same rule that
    keeps ``spawn.schemas`` and ``control.schemas`` from their live reads on that
    path -- so no ``installed.json`` is read there and a disabled app's tools
    still appear by name. The process that serves ``tools/list`` to a model is
    ``mcp_shared.run_mcp_stdio_loop``, which takes the full build, where the gate
    IS applied.
    """
    hidden: set[str] = set()
    for app in sorted(set(TOOL_APPS.values())):
        try:
            state = app_enabled_state(app)
        except Exception:
            # Never let a metadata read break the tool advertisement: the route
            # still refuses the call, while an escaping exception withdraws every tool.
            continue
        if state is False:
            hidden.update(tool for tool, owner in TOOL_APPS.items() if owner == app)
    return [spec for spec in schemas() if spec["name"] not in hidden]


def _crew_session_key() -> tuple[str, str]:
    """Strictly-resolved session key for the crew tools, or an error to return.

    Returns ``(key, "")`` on success and ``("", message)`` when identity is not
    directly attributable. Both crew tools send THIS key rather than resolving
    their own, so the identity that passed the gate is the identity on the wire.
    """
    _crew_sk, _crew_err = mcp_core.require_strict_session_key(
        "Error: this tool needs a directly-identified dashboard session. "
        "A subagent resolves to its parent's session, which would read and "
        "write the parent crew's ledger. Run this from the crew's own "
        "session."
    )
    if not _crew_sk:
        return "", _crew_err
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
        except ValueError:
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
    _omc_resp = (
        mcp_core._get(_omc_url) if _omc_method == "GET" else mcp_core._post(_omc_url, _omc_body)
    )
    # Serialize compactly and redact on the way OUT: signals, incident
    # titles and ledger entries carry text from external monitoring
    # systems and prior LLM turns, so a credential or exfil URL quoted
    # into one would otherwise flow straight into this agent's context.
    # Redact BEFORE truncating: slicing first could cut a credential in
    # half at the cap so the redaction pattern would not match, leaking
    # the surviving fragment.
    _omc_text = redact(json.dumps(_omc_resp, ensure_ascii=False, default=str))
    _omc_cap = 60_000
    if len(_omc_text) > _omc_cap:
        _omc_text = (
            _omc_text[:_omc_cap] + f"\n… truncated ({len(_omc_text)} chars total). Narrow the "
            "call (e.g. query filters) to see the rest."
        )
    return _omc_text


# The one Design Tweak route the agent reaches, under the gateway's dashboard
# app reverse proxy (``/apps/{name}/api/{path}`` → the app's own backend
# process; NOT ``/api/apps/...``, which carries only the gateway's own app
# management routes). The gateway admits exactly this path to internal-secret
# callers (``_MIXED_INTERNAL_API_PATHS`` in dashboard/server.py); every other
# app route stays dashboard-only.
_DESIGN_TWEAK_THREAD_URL = "/apps/design-tweak/api/thread"


def design_tweak_update_thread(name: str, args: dict[str, Any]) -> str:
    """Append progress to one Design Tweak comment thread via the app proxy.

    The schema (``DESIGN_TWEAK_UPDATE_THREAD_SCHEMA``) bounds the field types,
    lengths, and the ``status`` value set; the backend's ``_h_thread`` is the
    single authority for the id grammar and the ``text or status`` rule (this
    handler does not restate either). The handler needs no caller identity, so
    it does NOT touch the session resolver.
    """
    _dt_request_id = args["request_id"]
    _dt_comment_id = args.get("comment_id") or ""
    _dt_text = args.get("text") or ""
    _dt_status = args.get("status") or ""

    # The agent-authored note is passed through unchanged: the backend's
    # ``_redact_incoming_thread_text`` runs on EVERY ``/thread`` write (the only
    # route this tool posts to), so a credential or exfil URL in it is scrubbed
    # before it persists. Redacting here too would be a second copy of that one
    # enforcement point. The response IS redacted below, since that path echoes
    # app state the backend's inbound scrub never touched.
    _dt_body: dict[str, Any] = {"role": "agent"}
    if _dt_text:
        _dt_body["text"] = _dt_text
    if _dt_status:
        _dt_body["status"] = _dt_status

    # ids are value-position only, but URL-quote them anyway so a '.' or '-'
    # edge case cannot alter the query.
    _dt_url = f"{_DESIGN_TWEAK_THREAD_URL}?id={quote(_dt_request_id, safe='')}"
    if _dt_comment_id:
        _dt_url += f"&cid={quote(_dt_comment_id, safe='')}"

    _dt_resp = mcp_core._post(_dt_url, _dt_body)
    # Redact on the way OUT: the response echoes the thread, whose entries carry
    # text from prior turns and the app's own state.
    _dt_text_out = redact(json.dumps(_dt_resp, ensure_ascii=False, default=str))
    _dt_cap = 20_000
    if len(_dt_text_out) > _dt_cap:
        _dt_text_out = _dt_text_out[:_dt_cap] + f"\n… truncated ({len(_dt_text_out)} chars total)."
    return _dt_text_out


_POD_API_BASE = "/api/apps/dev-fleet/pod"

# Client-side ceilings, each one step above the ceiling the gateway route enforces
# on its own subprocess (180s for a pod boot). Ordered that way on purpose: the
# server's bound is the one that produces a DIAGNOSIS ("systemd refused", "no built
# dist"), so it must be the one that fires. A tighter client timeout would replace
# every real failure with a bare "no response".
_POD_UP_TIMEOUT_S = 210.0
_POD_DOWN_TIMEOUT_S = 60.0
# A read still spawns `python -m kiro_crew pod ...` in the gateway, so it pays a
# cold interpreter start -- more than the 10s a pure telemetry read is given.
_POD_READ_TIMEOUT_S = 45.0


def _pod_failure(payload: dict[str, Any], verb: str) -> str | None:
    """The error line for a refused pod call, or None when it succeeded.

    Two failure shapes arrive here and both must read as errors: a transport-level
    ``{"error": ...}`` from the gateway helper, and the route's own
    ``{"ok": false, "error": ..., "code": ...}``. Reporting only the first would
    let a refused pod boot read as a success with no handle in it.
    """
    if payload.get("error") and not payload.get("ok"):
        _code = payload.get("code")
        _suffix = f" [{_code}]" if _code else ""
        return f"Error: {verb} failed{_suffix}: {redact(str(payload['error']))}"
    if not payload.get("ok"):
        return f"Error: {verb} returned no result: {redact(json.dumps(payload, default=str))}"
    return None


def pod_up(name: str, args: dict[str, Any]) -> str:
    _pu_worktree = args["worktree"]
    _pu_resp = mcp_core._post(
        f"{_POD_API_BASE}/up", {"worktree": _pu_worktree}, timeout=_POD_UP_TIMEOUT_S
    )
    _pu_err = _pod_failure(_pu_resp, f"pod up {_pu_worktree!r}")
    if _pu_err:
        return _pu_err
    # The token is the DELIVERABLE, so it is the one string here that must survive
    # verbatim -- redacting it would return a handle that cannot be used. Scope is
    # what makes that safe: it is a 2h credential for this pod's own gateway, not
    # the live one. Everything else in the payload is echoed as the route sent it.
    _pu_port = _pu_resp.get("port")
    _pu_base = _pu_resp.get("base_url") or (f"http://127.0.0.1:{_pu_port}" if _pu_port else "")
    _pu_token = _pu_resp.get("token") or ""
    _pu_lines = [
        f"Pod `{_pu_worktree}` is up on port {_pu_port}.",
        f"base_url: {_pu_base}",
    ]
    if _pu_token:
        _pu_lines.append(f"token: {_pu_token}  (ttl {_pu_resp.get('ttl') or '2h'})")
        _pu_lines.append(f"open: {_pu_base}/?token={_pu_token}")
    else:
        # `pod up --json` emits an empty token when it could not PROVE it owns the
        # port, and withholding the credential is the correct outcome there. Say so:
        # a missing token read as an oversight sends the agent hunting for a bug.
        _pu_lines.append(
            "token: (withheld -- the CLI could not prove this port belongs to the "
            "pod it just started; check `pod_status` before trusting the port)"
        )
    _pu_lines.append(f"Stop it with pod_down when you are done: {_pu_worktree}")
    return "\n".join(_pu_lines)


def pod_down(name: str, args: dict[str, Any]) -> str:
    _pd_worktree = args["worktree"]
    _pd_resp = mcp_core._post(
        f"{_POD_API_BASE}/down", {"worktree": _pd_worktree}, timeout=_POD_DOWN_TIMEOUT_S
    )
    _pd_err = _pod_failure(_pd_resp, f"pod down {_pd_worktree!r}")
    if _pd_err:
        return _pd_err
    return f"Pod `{_pd_worktree}` is stopped and its data directory reclaimed."


def pod_status(name: str, args: dict[str, Any]) -> str:
    _ps_worktree = args["worktree"]
    _ps_resp = mcp_core._get(
        f"{_POD_API_BASE}/status?worktree={quote(_ps_worktree, safe='')}",
        timeout=_POD_READ_TIMEOUT_S,
    )
    _ps_err = _pod_failure(_ps_resp, f"pod status {_ps_worktree!r}")
    if _ps_err:
        return _ps_err
    return redact(
        f"Pod `{_ps_resp.get('name') or _ps_worktree}`: "
        f"{_ps_resp.get('status') or 'unknown'} "
        f"port={_ps_resp.get('port')} health={_ps_resp.get('health')}"
    )


def pod_ls(name: str, args: dict[str, Any]) -> str:
    _pl_resp = mcp_core._get(f"{_POD_API_BASE}/list", timeout=_POD_READ_TIMEOUT_S)
    _pl_err = _pod_failure(_pl_resp, "pod ls")
    if _pl_err:
        return _pl_err
    _pl_pods = _pl_resp.get("pods") or []
    if not _pl_pods:
        return "No pods are active on this host."
    _pl_lines = [f"{len(_pl_pods)} pod(s) active:"]
    for _pl_row in _pl_pods:
        if not isinstance(_pl_row, dict):
            continue
        _pl_lines.append(
            f"  {_pl_row.get('name')}  port={_pl_row.get('port')} "
            f"health={_pl_row.get('health')}"
        )
    return redact("\n".join(_pl_lines))


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
    }
    # Presence, not truthiness. Omitting `number` is a MEANING — "this step belongs
    # to no issue" — so it is forwarded as an absence rather than as a zero, which
    # the route would read as a malformed issue number. The route pairs the absence
    # with the crew-level `sweep` kind and refuses any other combination.
    if "number" in args:
        _cw_body["number"] = args["number"]
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
        _cw_body["labels_applied"] = [redact(s) for s in (args.get("labels_applied") or []) if s]
    # Names, forwarded as names: the route turns each into an explicit null in the
    # work-item patch, which is how a field is emptied. Every field above is gated
    # on truthiness, so this list is the ONLY way a clear reaches the ledger.
    if "clear" in args:
        _cw_body["clear"] = [s for s in (args.get("clear") or []) if isinstance(s, str) and s]
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
    # A crew-level line has no issue to name and no item to report a phase for, so
    # it is summarised by the crew it belongs to. Reusing the numbered wording here
    # would print a bare `#` and claim a phase the write never touched.
    if "number" in args:
        _cw_ref = f"{_cw_owner}/{_cw_repo}#{args['number']}"
        _cw_phase = _cw_item.get("phase") or _cw_body.get("phase") or "(phase unchanged)"
        _cw_lines = [f"Recorded {_cw_ref}: phase `{_cw_phase}`."]
    else:
        # Whether a line was actually written matters to the caller: a coalesced
        # checkpoint is still acknowledged, but a crew that believed each idle
        # cycle added a line would misread its own log's length as its cycle count.
        if _cw_resp.get("coalesced"):
            _cw_lines = [
                f"Crew-level step for {_cw_owner}/{_cw_repo} folded into the open "
                "idle stretch (its ledger line already says the queue was empty, "
                "so no duplicate was added -- the existing line's timestamp marks "
                "when the stretch began)."
            ]
        else:
            _cw_lines = [
                f"Recorded a crew-level step for {_cw_owner}/{_cw_repo} "
                "(no issue -- no work item was written)."
            ]
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


#: One number, two bounds. First it caps the RAW BYTES ``mcp_core._send`` reads
#: from the wire before decoding (``max_response_bytes``), so an app cannot flood
#: this process with a body it never finishes reading. Then it caps the
#: CHARACTERS of the redacted text returned to the model, so a cut cannot split a
#: credential the redactor would have matched. The units differ, the figure is
#: shared because neither bound needs to be tighter than the other.
_APP_REQUEST_RESPONSE_CAP = 60_000
_APP_REQUEST_HEADERS = {APP_REQUEST_HEADER: APP_REQUEST_HEADER_VALUE}


def _declares_agent_route(app: str, method: str, path: str) -> bool:
    """Whether installed *app* declares *method* *path* for agent calls."""
    manifest = app_manager.get_app_manifest(app)
    if manifest is None or not isinstance(manifest.agentRoutes, list):
        return False
    for parsed, _reason in map(parse_agent_route, manifest.agentRoutes):
        if parsed is not None and parsed[0] == method and agent_route_matches(parsed[1], path):
            return True
    return False


def app_request(name: str, args: dict[str, Any]) -> str:
    _ar_sk, _ar_err = mcp_core.require_strict_session_key(
        "Error: app_request needs a directly-identified session. The app is told "
        "which session is calling, and a subagent resolves to its parent's session, "
        "which would act as the parent."
    )
    if not _ar_sk:
        return _redact_and_cap_app_response(_ar_err)
    _ar_chan_deny = mcp_core._deny_channel_agent_messaging(_ar_sk, "app_request")
    if _ar_chan_deny:
        return _redact_and_cap_app_response(_ar_chan_deny)
    _ar_app = args["app"]
    _ar_method = args["method"]
    _ar_path = args["path"]
    if redact(_ar_path) != _ar_path:
        return _redact_and_cap_app_response(
            f"Error: app_request refused {_ar_method} {_ar_path} because the path contains "
            "credential material. The request did not reach the app or gateway."
        )
    if not app_manager.is_app_enabled(_ar_app):
        return _redact_and_cap_app_response(
            f"Error: app_request refused {_ar_method} {_ar_path} before the request reached "
            "the gateway, so it is safe to retry after correcting the refusal. "
            f"{_ar_app!r} is not an enabled installed app."
        )
    # Keep the manifest check as a cheap defense in depth. The static dashboard
    # allowlists cannot be imported here without reversing the mcp_tools ->
    # dashboard dependency, so token_auth authoritatively refuses marked
    # app_request traffic on every static internal path.
    if not _declares_agent_route(_ar_app, _ar_method, _ar_path):
        return _redact_and_cap_app_response(
            f"Error: app_request refused {_ar_method} {_ar_path} before the request reached "
            "the gateway, so it is safe to retry after correcting the refusal. The route "
            f"is not declared by app {_ar_app!r} in its manifest `agentRoutes`."
        )
    _ar_url = f"/api/apps/{quote(_ar_app)}{_ar_path}"
    # Redacted on the way IN: the body reaches app code that may persist or forward
    # it, so a credential quoted into it is scrubbed before it leaves this process.
    _ar_body = mcp_core._redact_json_strings(sanitize_json_values(args.get("body") or {}))
    if _ar_method == "GET":
        _ar_resp = mcp_core._get(
            _ar_url,
            session_key=_ar_sk,
            max_response_bytes=_APP_REQUEST_RESPONSE_CAP,
            extra_headers=_APP_REQUEST_HEADERS,
        )
    elif _ar_method == "POST":
        _ar_resp = mcp_core._post(
            _ar_url,
            _ar_body,
            session_key=_ar_sk,
            max_response_bytes=_APP_REQUEST_RESPONSE_CAP,
            extra_headers=_APP_REQUEST_HEADERS,
        )
    elif _ar_method == "PUT":
        _ar_resp = mcp_core._put(
            _ar_url,
            _ar_body,
            session_key=_ar_sk,
            mark_transport_error=True,
            max_response_bytes=_APP_REQUEST_RESPONSE_CAP,
            extra_headers=_APP_REQUEST_HEADERS,
        )
    elif _ar_method == "PATCH":
        _ar_resp = mcp_core._patch(
            _ar_url,
            _ar_body,
            session_key=_ar_sk,
            mark_transport_error=True,
            max_response_bytes=_APP_REQUEST_RESPONSE_CAP,
            extra_headers=_APP_REQUEST_HEADERS,
        )
    else:
        _ar_resp = mcp_core._delete(
            _ar_url,
            _ar_body or None,
            session_key=_ar_sk,
            mark_transport_error=True,
            max_response_bytes=_APP_REQUEST_RESPONSE_CAP,
            extra_headers=_APP_REQUEST_HEADERS,
        )
    # Redact each complete result before its cap. This covers path and app names as
    # well as app-controlled response text, and prevents a cut from splitting a
    # credential that the redactor would otherwise match.
    if isinstance(_ar_resp, dict) and _ar_resp.get("error"):
        _ar_err_text = str(_ar_resp["error"])
        if _ar_resp.get("refused"):
            return _redact_and_cap_app_response(
                f"Error: {_ar_method} {_ar_path} was refused before reaching the gateway, "
                f"so it is safe to retry: {_ar_err_text}"
            )
        if _ar_resp.get("transport_error"):
            return _redact_and_cap_app_response(
                f"Error: outcome unknown: the {_ar_method} {_ar_path} request may have been "
                "applied; read state back with a GET instead of resending. "
                f"Transport failure: {_ar_err_text}"
            )
        return _redact_and_cap_app_response(
            f"Error: {_ar_app} answered {_ar_method} {_ar_path} with: {_ar_err_text}"
        )
    _ar_text = json.dumps(_ar_resp, ensure_ascii=False, default=str)
    return _redact_and_cap_app_response(f"OK {_ar_method} {_ar_path}\n{_ar_text}")


def _redact_and_cap_app_response(text: str) -> str:
    """Redact a complete app_request result, then apply its character cap."""
    return _cap_app_response(redact(text))


def _cap_app_response(text: str) -> str:
    """Truncate an already-redacted app_request result to the response cap."""
    if len(text) <= _APP_REQUEST_RESPONSE_CAP:
        return text
    return text[:_APP_REQUEST_RESPONSE_CAP] + f"\n… truncated ({len(text)} chars total)."


HANDLERS: dict[str, Callable[[str, dict[str, Any]], str]] = {
    "issue_radar_record_investigation": issue_radar_record_investigation,
    "ops_mission_control_api": ops_mission_control_api,
    "design_tweak_update_thread": design_tweak_update_thread,
    "pod_up": pod_up,
    "pod_down": pod_down,
    "pod_status": pod_status,
    "pod_ls": pod_ls,
    "issue_radar_crew_read": issue_radar_crew_read,
    "issue_radar_crew_record": issue_radar_crew_record,
    "app_request": app_request,
}
