"""Tool schemas for the builtin apps' agent tools.

Issue Radar investigations and crew records, the Ops Mission Control API allowlist,
the Design Tweak thread update and the pod tools.

Callers import these names from :mod:`kiro_crew.validation`, which serves them; this
module never imports that facade.
"""

from __future__ import annotations

import re
from typing import Any

from kiro_crew.constants import MAX_SHORT_STRING
from kiro_crew.validation_parts.field_engine import FieldSpec, ToolSchema, ValidationError
from kiro_crew.validation_parts.input_bounds import MAX_MEDIUM_STRING

# ── Tool Schemas (Issue Radar app) ──
#
# ``issue_radar_record_investigation`` is the agent's ONLY write path into an
# Issue Radar investigation record. The findings sub-object is flattened into
# per-field args on purpose: ``FieldSpec`` validates scalars and string lists,
# not nested dicts, so a single ``findings`` dict arg would reach the gateway
# unvalidated. The tool re-assembles the object from these fields.

_ISSUE_RADAR_PROVIDERS = frozenset({"github", "gitlab"})
_ISSUE_RADAR_ITEM_KINDS = frozenset({"issue", "pull"})
_ISSUE_RADAR_STATUSES = frozenset({"investigating", "resolved", "archived"})
# Mirrors ``issue_radar.backend.routes.MAX_ITEM_NUMBER``: bounds the number that
# becomes part of the record's FILENAME (``investigation-<n>.json``), so an
# absurd value cannot produce an ENAMETOOLONG write.
_ISSUE_RADAR_MAX_ITEM_NUMBER = 1_000_000_000

# ── Tool Schemas (Ops Mission Control app) ──
#
# ``ops_mission_control_api`` is the agent's ONLY credentialed path to the
# app's HTTP surface (same pattern as ``issue_radar_record_investigation``:
# the MCP server process holds the internal secret; the agent never sees a
# credential). The (method, path) allowlist below is the entire authorization
# story for the tool, so it is defined here — next to the schema that
# enforces it — and imported by both the tool handler and the tests. It
# deliberately covers only what the app's SOPs need and EXCLUDES the
# human-decision and configuration routes (``/incident/proposal/decide``,
# ``/incident/propose``, ``/proposals``, ``/providers*``, ``/settings``,
# ``/webhook``, bare ``/incident``): an agent that needs one of those is by
# definition off-SOP.

OPS_MISSION_CONTROL_ALLOWED_CALLS: frozenset[tuple[str, str]] = frozenset(
    {
        ("GET", "/state"),
        ("GET", "/signals"),
        ("GET", "/incidents"),
        ("GET", "/handover"),
        ("GET", "/rotation"),
        ("GET", "/ledger"),
        ("GET", "/ledger/contradictions"),
        ("POST", "/dispatch"),
        ("POST", "/incident/transition"),
        ("POST", "/incident/claim"),
        ("POST", "/incident/action"),
        ("POST", "/rotation/arm"),
        ("POST", "/ledger"),
        ("POST", "/ledger/hygiene"),
    }
)
_OMC_API_METHODS = frozenset({"GET", "POST"})
_OMC_API_PATHS = frozenset(p for _, p in OPS_MISSION_CONTROL_ALLOWED_CALLS)
# Query strings are value-position only: no '/', '?' or '#', so a query can
# never rewrite the path it is appended to. '%' admits URL-encoded values.
_OMC_QUERY_RE = re.compile(r"^[A-Za-z0-9_.=&%+,:-]*$")
# Bounds the JSON body an agent can push through the tool. Ledger entries are
# the largest legitimate payload; 32 KiB is ~4x the biggest one observed.
_OMC_MAX_BODY = 32_768


def _validate_omc_api(cleaned: dict[str, Any]) -> None:
    method = cleaned.get("method")
    path = cleaned.get("path")
    if (method, path) not in OPS_MISSION_CONTROL_ALLOWED_CALLS:
        raise ValidationError("path", f"{method} {path} is not part of the agent surface")
    if cleaned.get("query") and method != "GET":
        raise ValidationError("query", "query is only accepted on GET calls")
    if cleaned.get("body_json") and method == "GET":
        raise ValidationError("body_json", "GET calls take no body")


OPS_MISSION_CONTROL_API_SCHEMA = ToolSchema(
    tool_name="ops_mission_control_api",
    fields=[
        FieldSpec("method", str, required=True, allowed=_OMC_API_METHODS),
        FieldSpec("path", str, required=True, allowed=_OMC_API_PATHS),
        FieldSpec("query", str, max_len=512, pattern=_OMC_QUERY_RE, default=""),
        FieldSpec("body_json", str, max_len=_OMC_MAX_BODY, default=""),
    ],
    custom_validator=_validate_omc_api,
)

# ── Tool Schema (Design Tweak app) ──
#
# ``design_tweak_update_thread`` is the agent's ONLY credentialed path to the
# Design Tweak app's ``POST /thread`` route (same pattern as
# ``ops_mission_control_api`` and ``issue_radar_record_investigation``: the MCP
# server process holds the internal secret; the agent never sees a credential).
# The app hands the agent batched click-anchored comments to apply, but the
# agent cannot update the preview's thread bubbles — posting to ``/thread``
# needs the dashboard credential an agent session is deliberately denied — so
# it reported progress inline in chat and the dots never changed.
#
# Both ids are value-position only: the schema URL-quotes them with
# ``safe=''`` so neither can carry a '/', '?' or '#' that would rewrite the
# single ``/thread`` route, and the backend's own ``_ID_RE``
# (``^[A-Za-z0-9._-]+$``) is the authority that rejects a malformed id. The
# tool boundary therefore bounds only length, not grammar. ``status`` is
# restricted to the one FORWARD-progress value ``done`` — the agent may mark a
# comment it finished, but the tool exposes no destructive thread action
# (``/clear`` archives a request and removes its pins, and is NOT reachable
# here). The ``text or status`` rule is likewise the backend's
# (``_h_thread``): an empty call is refused there, so it is not restated here.
# The one thread status the tool may set: forward progress only. The backend's
# full set is ``new | sent | done`` (COMMENT_STATUSES); ``new``/``sent`` are
# the app's own lifecycle, not something an agent reports, so the scoped tool
# admits only ``done``.
_DESIGN_TWEAK_ALLOWED_STATUSES = frozenset({"done"})
# Bounds one progress note. The backend caps a thread at MAX_THREAD_ENTRIES
# short lines; 2 KiB is well above one legitimate note and keeps an unbounded
# string from being carried further.
_DESIGN_TWEAK_MAX_TEXT = 2_048


DESIGN_TWEAK_UPDATE_THREAD_SCHEMA = ToolSchema(
    tool_name="design_tweak_update_thread",
    fields=[
        FieldSpec(
            "request_id",
            str,
            required=True,
            max_len=200,
        ),
        FieldSpec("comment_id", str, max_len=200, default=""),
        FieldSpec("text", str, max_len=_DESIGN_TWEAK_MAX_TEXT, default=""),
        FieldSpec("status", str, allowed=_DESIGN_TWEAK_ALLOWED_STATUSES, default=""),
    ],
)

# Dev Fleet pod lifecycle (agent surface). A pod name is a git worktree basename,
# so it reaches `git worktree list` matching and a filesystem path before
# `rt.validate_name` -- the real authority -- ever sees it. Bounded here so an
# unbounded string is refused at the tool boundary rather than carried further.
_POD_WORKTREE_MAX_LEN = 200

POD_UP_SCHEMA = ToolSchema(
    tool_name="pod_up",
    fields=[FieldSpec("worktree", str, required=True, max_len=_POD_WORKTREE_MAX_LEN)],
)

POD_DOWN_SCHEMA = ToolSchema(
    tool_name="pod_down",
    fields=[FieldSpec("worktree", str, required=True, max_len=_POD_WORKTREE_MAX_LEN)],
)

POD_STATUS_SCHEMA = ToolSchema(
    tool_name="pod_status",
    fields=[FieldSpec("worktree", str, required=True, max_len=_POD_WORKTREE_MAX_LEN)],
)

# `pod_ls` takes nothing. It is registered anyway: a tool ABSENT from
# MCP_CORE_SCHEMAS has its arguments passed through raw, so an empty schema is
# what makes an unexpected argument an "Error:" string instead of unvalidated input.
POD_LS_SCHEMA = ToolSchema(tool_name="pod_ls", fields=[])

ISSUE_RADAR_RECORD_INVESTIGATION_SCHEMA = ToolSchema(
    tool_name="issue_radar_record_investigation",
    fields=[
        FieldSpec("owner", str, required=True, max_len=MAX_SHORT_STRING),
        FieldSpec("repo", str, required=True, max_len=MAX_SHORT_STRING),
        FieldSpec("number", int, required=True, min_val=1, max_val=_ISSUE_RADAR_MAX_ITEM_NUMBER),
        # provider/host/kind are REQUIRED, with no defaults, because together
        # with owner/repo they select the record's STORAGE NAMESPACE (see
        # ``store.provider_root``: public GitHub keeps the original
        # ``repos/{owner}/{repo}`` path, everything else lives under the
        # ``@providers`` subtree). Defaulting them to GitHub would let a caller
        # that simply omits the identity — a GitLab investigation, say — write
        # into the GitHub ledger and silently overwrite a same-slug GitHub
        # record, since a GitLab group can share a name with a GitHub owner.
        # The caller always has this information to hand (`recordIdentityJson`
        # in ``website/src/apps/issue-radar/lib/links.ts`` emits all three), so
        # requiring them costs nothing and removes the ambiguity.
        FieldSpec("provider", str, required=True, max_len=16, allowed=_ISSUE_RADAR_PROVIDERS),
        FieldSpec("host", str, required=True, max_len=253),
        FieldSpec("kind", str, required=True, max_len=8, allowed=_ISSUE_RADAR_ITEM_KINDS),
        FieldSpec("status", str, max_len=16, allowed=_ISSUE_RADAR_STATUSES, default="resolved"),
        FieldSpec("verdict", str, max_len=MAX_SHORT_STRING),
        FieldSpec("root_cause", str, max_len=MAX_MEDIUM_STRING),
        FieldSpec("next_action", str, max_len=MAX_MEDIUM_STRING),
        FieldSpec("summary", str, max_len=MAX_MEDIUM_STRING),
        FieldSpec(
            "suggested_labels",
            list,
            item_type=str,
            item_max_len=MAX_SHORT_STRING,
            max_items=20,
        ),
    ],
)

# ── Tool Schemas (Issue Radar crews) ──
#
# The crew ledger is an autonomous agent's ONLY memory across compaction, the
# per-turn ceiling and a gateway restart, so both tools are on the same
# internal-secret path as ``issue_radar_record_investigation`` and validated
# here, next to it.
#
# Neither schema carries owner/repo/crew_id. That is a security choice, not an
# omission: identity is resolved from the CALLING SESSION (see
# ``mcp_core._crew_identity``). If the model could name the crew, a crew could
# write into another crew's ledger — clobbering its ``next``, its worktree path
# or its claim comment id — and the store's "at most one item in an editing
# phase" invariant is per-crew, so a cross-crew write would also defeat that.
#
# The phase / event-kind vocabularies MIRROR
# ``issue_radar.backend.crew_store.PHASES`` and ``.EVENT_KINDS`` rather than
# importing them: ``validation`` is core and must not import an app package
# (apps load dynamically and may be absent). ``test_issue_radar_crew_mcp_tools``
# asserts the mirrors are exact, so drift fails a test instead of silently
# rejecting a legitimate phase at the tool boundary.
_ISSUE_RADAR_CREW_PHASES = frozenset(
    {
        "selected",
        "claimed",
        "investigating",
        "implementing",
        "awaiting-ci",
        "addressing-review",
        "awaiting-merge",
        "awaiting-reply",
        "resolved",
        "skipped",
        "yielded",
        "handed-back",
        "preempted",
    }
)
_ISSUE_RADAR_CREW_EVENT_KINDS = frozenset(
    {
        "claim",
        "investigate",
        "reply",
        "implement",
        "ci",
        "review",
        "conflict",
        "merge",
        "handback",
        "skip",
        "yield",
        # The one crew-level kind: the crew looked at the queue and took nothing.
        # It is the only kind valid with NO ``number``, and it is invalid WITH one
        # — a relation between two fields, so it is enforced on the write route
        # and in the store rather than here.
        "sweep",
    }
)
#: Mirrors ``crew_store.SKIP_SCOPES`` — the classification a crew attaches to a
#: pass in the repo-wide shared skip index. Advertised to the model as an enum so
#: it picks a real one; NOT enforced as ``allowed=`` on the field (see the
#: ``skip_scope`` spec below for why).
#:
#: ``needs-decision`` and ``needs-investigation`` are how a crew says the next step
#: belongs to a human. They are scopes on a PASS because a crew never holds an
#: issue waiting for one: it says what it needs on the issue, labels it, records the
#: pass and moves on.
_ISSUE_RADAR_CREW_SKIP_SCOPES = frozenset(
    {
        "architecture",
        "new-feature",
        "needs-design",
        "needs-decision",
        "needs-investigation",
        "duplicate",
        "already-fixed",
        "not-reproducible",
        "wrong-root-cause",
        "breaking-change",
        "gate-config",
        "other",
    }
)

#: Work-item fields the record tool may EMPTY through its ``clear`` list. Spelled
#: out so the tool schema advertises them as an enum; pinned against the entry
#: type's ``RADAR_CLEARABLE_FIELDS`` by test, so the two cannot drift.
_ISSUE_RADAR_CREW_CLEARABLE_FIELDS = frozenset(
    {
        "decision",
        "why",
        "next",
        "worktree",
        "branch",
        "base_sha",
        "pr_number",
        "claim_comment_id",
        "ci_state",
        "labels_applied",
        "outcome",
    }
)

# Abbreviated-or-full git object name. Bounds ``base_sha`` to something that can
# actually be handed to git on a resume; a resumed turn checks out from this
# value, so an arbitrary 5k string here is a resume that fails much later.
_GIT_SHA_RE = re.compile(r"^[0-9a-fA-F]{7,64}$")


def _validate_crew_record_couples_phase_to_an_event(args: dict[str, Any]) -> None:
    """Enforce the invariants that justify ONE write tool instead of two.

    * ``event`` and ``event_kind`` travel together — the store refuses an
      unknown kind, and an event with no kind cannot be filed on either surface
      it feeds (crew page + the public claim comment).
    * a ``phase`` write must carry its reason. Splitting upsert and append into
      two tools is what allows a phase to move with nothing logged; merging them
      only closes that if the event is actually mandatory on a phase change.
    """
    if args.get("event") and not args.get("event_kind"):
        raise ValidationError("event_kind", "required when 'event' is given")
    if args.get("event_kind") and not args.get("event"):
        raise ValidationError("event", "required when 'event_kind' is given")
    if args.get("phase") and not args.get("event"):
        raise ValidationError(
            "event",
            "required when 'phase' changes — record the reason with the phase "
            "(also pass 'event_kind')",
        )


ISSUE_RADAR_CREW_READ_SCHEMA = ToolSchema(
    tool_name="issue_radar_crew_read",
    fields=[
        # Deliberately empty: no argument can select WHICH crew is read (see the
        # block comment above). ``max_events`` is not exposed either — the
        # handler bounds the log itself so a long-lived crew cannot blow the
        # caller's context by asking for more.
    ],
)

ISSUE_RADAR_CREW_RECORD_SCHEMA = ToolSchema(
    tool_name="issue_radar_crew_record",
    fields=[
        # Bounds the number the work item is KEYED by: a JSON int on one
        # ``radar/recorded`` crew log entry and the string key the fold files the
        # item under. The bound guards a key rather than a path, and keeps the
        # same constant as the investigation record so a number a crew records is
        # one every other Issue Radar surface can also hold.
        #
        # NOT required. A crew that swept its queue and took nothing has no issue
        # to name, and requiring one here left it recording the cycle against an
        # issue it never acted on. The coupling that replaces the requirement —
        # a missing number is valid ONLY with the crew-level ``sweep`` kind, and
        # ``sweep`` is valid ONLY without one — is enforced on the write route and
        # in the store, because it is a relation between two fields and this
        # schema validates them one at a time. Keeping the bound here still
        # matters: when a number IS sent it is the item's key.
        FieldSpec("number", int, min_val=1, max_val=_ISSUE_RADAR_MAX_ITEM_NUMBER),
        FieldSpec("phase", str, max_len=32, allowed=_ISSUE_RADAR_CREW_PHASES),
        # Bounded but deliberately NOT ``allowed=``, unlike ``phase`` beside it.
        # An out-of-vocabulary phase has to be refused — it would corrupt the
        # phase state machine. A scope is only a filter label, and refusing one
        # would fail the whole write, which on a ``skipped`` write is the write
        # that puts the issue in the shared skip index. Weakening "a skip is
        # always indexed" to buy a tidier label is the wrong trade, so the store
        # coerces an unknown value to ``other`` (``crew_store.SKIP_SCOPES``) and
        # the pass is recorded either way. The vocabulary is still advertised as
        # an enum in the tool schema, so the model is told what to pick.
        FieldSpec("skip_scope", str, max_len=32),
        # ``outcome`` is a bounded free string, NOT an enum: the store keeps it
        # as free text (``crew_store.commit_work_progress``) and no vocabulary is
        # defined anywhere in the app, so an allowlist invented here would
        # reject a legitimate terminal outcome and lose it.
        FieldSpec("outcome", str, max_len=MAX_SHORT_STRING),
        FieldSpec("next", str, max_len=MAX_MEDIUM_STRING),
        FieldSpec("decision", str, max_len=MAX_MEDIUM_STRING),
        FieldSpec("why", str, max_len=MAX_MEDIUM_STRING),
        FieldSpec("tried_approach", str, max_len=MAX_MEDIUM_STRING),
        FieldSpec("tried_rejected_because", str, max_len=MAX_MEDIUM_STRING),
        # Local-only resume fields. These are the ONE place an absolute path
        # legitimately belongs, which is why the handler must not scrub them the
        # way it scrubs the public strings.
        FieldSpec("worktree", str, max_len=4096),
        FieldSpec("branch", str, max_len=255),
        FieldSpec("base_sha", str, max_len=64, pattern=_GIT_SHA_RE),
        FieldSpec("pr_number", int, min_val=1, max_val=_ISSUE_RADAR_MAX_ITEM_NUMBER),
        # ci_* are flat args assembled into the store's ``ci_state`` dict by the
        # handler. ``ci_state`` is the forge's own verdict word (success /
        # failure / pending / neutral / cancelled / timed_out, and GitLab's
        # differ again), so it is bounded but not enumerated here.
        FieldSpec("ci_state", str, max_len=32),
        FieldSpec("ci_passed", int, min_val=0, max_val=100_000),
        FieldSpec("ci_total", int, min_val=0, max_val=100_000),
        FieldSpec("ci_round", int, min_val=0, max_val=1_000),
        FieldSpec("ci_inherited_reds", int, min_val=0, max_val=100_000),
        # Forge comment ids are large (GitHub is past 3e9 and monotonic).
        FieldSpec("claim_comment_id", int, min_val=1, max_val=10**18),
        # No ``crew:``-prefix pattern here on purpose: this field RECORDS what
        # was applied so a hand-back knows what to remove. The prefix allowlist
        # belongs to the forge write route that applies a label; enforcing it at
        # this boundary would reject a truthful record and lose the removal list.
        FieldSpec(
            "labels_applied",
            list,
            item_type=str,
            item_max_len=MAX_SHORT_STRING,
            max_items=20,
        ),
        # Names of work-item fields this update empties. The route checks each name
        # against the store's clearable list and refuses an unknown one; this bound
        # only keeps the list from being a payload.
        FieldSpec("clear", list, item_type=str, item_max_len=32, max_items=16),
        # One public progress line. Short by design: it is rendered as a list
        # item inside the claim comment's <details> block, not as a report.
        FieldSpec("event", str, max_len=MAX_SHORT_STRING),
        FieldSpec("event_kind", str, max_len=16, allowed=_ISSUE_RADAR_CREW_EVENT_KINDS),
    ],
    custom_validator=_validate_crew_record_couples_phase_to_an_event,
)
