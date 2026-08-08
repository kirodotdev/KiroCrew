"""Security-posture detail registry — the data behind Settings → Security.

Every count in this module is DERIVED from the live control it describes — never
a hardcoded number that can drift out of sync with what it covers — and each
control also resolves its concrete ``items`` so the dashboard can expand a row
into the real list instead of asking the reader to trust a pill.

Design contract:

- **Derived where a live list exists.** A count is always ``len(items)``, so the
  pill and the expanded list can never disagree. Seven controls resolve their
  items straight from the enforcing object (``sensitive_home_dirs()``,
  ``write_protected_home_paths()``, ``BUILTIN_DENIED_RULES``,
  ``SUSPICIOUS_BASH_PATTERNS``, the MCP dispatch registries, ``exfil_query_min_len()``,
  ``sel.audit_sources()``) — for those, drift is structurally impossible.
- **Three controls are curated, and are guarded by inverse tests.**
  ``_REDACTION_SINKS``, ``_CREDENTIAL_FAMILIES``, and ``_EXFIL_HEURISTICS`` have no
  single live list to enumerate (a redaction sink is a *call site*, a credential
  family is a *regex alternative*, a heuristic is a *branch*). Deriving a count
  from ``len()`` of a hand-written tuple would just relocate the original
  stale-number bug into this module, so each is paired with an
  **omission-detecting** test: the redaction registry is checked against every
  redactor call site in the package (with an explicit
  ``NON_EGRESS_REDACTION_MODULES`` allowlist), and the family/heuristic lists are
  checked against the live scanners. An omission — a curated list silently
  missing an entry — is the failure mode here, and only a test that
  detects an omission
  catches it, and a `len()` assertion never will.
- **Posture, not secrets.** Every item here is either a *public* control
  definition (a path pattern the agent is blocked from, a redaction sink's
  module, a credential FAMILY name) or a derived count. This endpoint never
  emits credential material, policy/profile rule CONTENTS (the governance
  ceiling the agent is fenced from — see ``handlers/security.py``'s posture-only
  snapshot), or user data. Sensitive-path entries are the *blocklist* itself,
  which is already documented in ``docs/architecture/security-deep-dive.md`` and is not a
  secret: knowing ``~/.aws`` is blocked does not help reach it.
- **One registry, two readers.** The dashboard renders it; ``test_security_posture``
  pins the derivation so a control that grows without its detail entry fails CI.

Redaction sinks are *named* in ``_REDACTION_SINKS`` rather than counted straight
from call sites, because a raw ``grep`` count would count comments, internal
non-egress uses, and every refactor. But the list is not trusted on its own: the
drift guard walks every redactor call site in the package and requires each
module to be either a registered sink or an explicitly-reasoned entry in
``NON_EGRESS_REDACTION_MODULES``. So the count means "egress paths covered" AND
cannot silently omit one.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Callable

import kiro_crew.validation as _validation
from kiro_crew import security
from kiro_crew import sel as _sel_mod
from kiro_crew.executors import governance_executor

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PostureItem:
    """One concrete element behind a control's count.

    ``label`` is what the control covers (a path, a sink, a credential family);
    ``detail`` is the optional "how/where" shown as secondary text.
    """

    label: str
    detail: str = ""


@dataclass(frozen=True)
class PostureControl:
    """One expandable security control.

    ``items_fn`` is a callable (not a list) so the items are resolved at request
    time against the LIVE control — a governance profile reload or a user-added
    deny rule is reflected without a gateway restart.

    Deliberately has NO ``items`` field: an eagerly-captured list is the exact
    shape of the drift bug this module exists to prevent (a snapshot taken at
    import time and then quietly outliving the thing it described), and a mutable
    default would also make this "frozen" dataclass unhashable.
    """

    key: str
    label: str
    unit: str
    items_fn: Callable[[], list[PostureItem]]
    summary: str = ""
    source: str = ""


# ── Redaction egress paths ──
# The distinct boundaries where agent/tool output crosses to a human or an
# external service and therefore runs a redaction pass. Named here (rather than
# counted straight from grep) so the count means "egress paths covered" instead
# of "call sites that mention redact" — but see NON_EGRESS_REDACTION_MODULES
# below: a drift guard checks this list against EVERY redactor call site, so an
# egress path cannot be silently omitted. Each entry names the owning module.
# Where a sink runs only ONE of the two scanners, its detail text says so.
_REDACTION_SINKS: tuple[tuple[str, str, str], ...] = (
    (
        "Skill context budget",
        "dashboard/handlers/skill_budget.py",
        "Skill display names served by `GET /api/skills/-/budget`. An auto-skill's "
        "frontmatter `name` is written by the agent, so it is LLM-authored text that "
        "reaches the dashboard verbatim — the same output-boundary reason as the "
        "session-memory titles below.",
    ),
    (
        "Session & task memory panel",
        "dashboard/session_memory.py",
        "Chat titles served by `GET /api/sessions/memory`. Titles are generated from "
        "user content, and the resume path in `chat_handlers` assigns a "
        'client-supplied `body["title"]` to the slot with no scan of its own, so this '
        "serializer is the boundary that guarantees the scan — the same "
        "output-boundary reason as the sibling subagent-task text.",
    ),
    (
        "Mochi notify + pin egress",
        "apps/builtins/mochi/hooks.py",
        "Agent-authored notify text (perform_pet_action summary/chatMessage) crosses to "
        "the browser via the `mochi:notify` broadcast and the chat push; `redact_tree` "
        "scrubs credentials and exfiltration URLs before publish, the same "
        "output-boundary reason as the app plan/activity-log sinks.",
    ),
    (
        "Profile artifact",
        "perf_sampler.py",
        "Folded-stack profiles written by `kirocrew perf sample`. Frame labels are "
        "code identifiers and shortened paths, but the artifact exists to be sent "
        "to a maintainer, and py-spy's raw output embeds absolute paths from the "
        "target process — so it is an egress boundary, redacted on the way out.",
    ),
    (
        "md-notebook error middleware",
        "apps/builtins/md_notebook/server.py",
        "Every md-notebook API error body. Handlers drive git and filesystem work "
        "over caller-supplied vault URLs, ids and note paths, so an unmodeled "
        "exception carries absolute paths and git stderr; `_safe_error` scrubs "
        "credentials, exfiltration URLs and host paths before the JSON reaches the "
        "browser — the same output-boundary reason as the dashboard error sinks.",
    ),
    (
        "Issue Radar provider CLI stderr",
        "apps/builtins/issue_radar/backend/errors.py",
        "`gh`/`glab` stderr quoted into provider exception messages, which the "
        "issue-radar routes return in their error bodies and the frontend renders "
        "verbatim. `sanitize_cli_stderr` runs only the host-path pass plus a "
        "private-host filter, not the credential scanner: both CLIs take their "
        "token from the environment rather than argv and neither echoes it, so host "
        "topology is the disclosure risk here. The actionable phrasing (auth, "
        "not-found, 403, timeout) is deliberately preserved so the user can "
        "self-diagnose.",
    ),
    (
        "Auto-Improvement fallback tool audit",
        "apps/builtins/auto_improvement/spine/agent_runner.py",
        "Per-tool SEL events for the unattended subprocess agent carry the tool's TARGET "
        "HINT — a path, glob or shell command the model chose. On-disk SEL records are not "
        "redacted by the writer and the persisted HMAC chain signs the bytes as-written, so "
        "the hint is redacted here, before `log_tool_invocation`. Fail-closed: a redactor "
        "that cannot run emits a fixed placeholder rather than raw agent text.",
    ),
    (
        "Auto-Improvement commit messages",
        "apps/builtins/auto_improvement/spine/driver.py",
        "Agent-authored commit subjects/bodies redacted before they become PERMANENT git "
        "history — both the local keep commit and the direct-push commit. A pushed commit "
        "message cannot be edited without rewriting history, so this is a one-way egress "
        "boundary.",
    ),
    (
        "Auto-Improvement one-click commit",
        "apps/builtins/auto_improvement/backend/commit.py",
        "The operator-triggered commit path builds its message from the queued PR body "
        "(agent-authored prose) and redacts before committing — same "
        "cannot-be-unpublished reason as the driver's path.",
    ),
    (
        "Auto-Improvement MCP tool results",
        "apps/builtins/auto_improvement/backend/mcp_server.py",
        "Every `tools/call` result is serialized run evidence handed to an LLM — a finding's "
        "note/signature/hypothesis are the model's own prose. Redacted BEFORE truncation so "
        "the cut cannot split a credential into a fragment the scanner misses. FAIL-CLOSED: "
        "these six tools are conveniences, so withholding a result beats leaking. The ERROR "
        "paths are scanned too (`_redact_error`): tool ARGUMENTS reach exception text by "
        'design — `get_finding` raises "no finding with fingerprint <fp>" with the caller\'s '
        "raw value — so a credential-shaped argument was echoed straight back to the model "
        "and into the SEL record. Measured before fixing. The exception type and JSON-RPC "
        "error code are composed in after scrubbing, so the message stays actionable.",
    ),
    (
        # The watcher's residual risk, disclosed because accepting it is the OPERATOR's
        # decision and a silent limitation is the real defect. Measured: a nested process under
        # `mode="strict"` sees ~/.aws, ~/.config/gh and ~/.docker EMPTY on a populated host,
        # and ~/.ssh exposes only `known_hosts` (host-key verification needs it) while
        # id_rsa/*.key stay hidden — so CREDENTIALS are confined. NETWORK EGRESS is NOT: the
        # sandbox never enters a network namespace (no CLONE_NEWNET; its docstring explains
        # agentic commands need reachable networking), and while curl/wget/nc are denied,
        # `python helper.py` is allowed and can open a socket. The shell denylist cannot close
        # that — it gates the requested command, not what the command then does. Consequence:
        # point the PR watcher only at repositories whose PR comments you would be willing to
        # execute. Raised by the GPT review (twice); the credential half was already verified
        # under D-84, the egress half is new and correct.
        "Auto-Improvement PR-watcher egress boundary",
        "apps/builtins/auto_improvement/backend/pr_watchers.py",
        "The watcher reads UNTRUSTED text (pull-request comments, check logs) and runs with an "
        "auto-approved shell, because its job is to run the repository's own build/test/lint, "
        "rebase, and commit. Credential stores are hidden by the strict sandbox (verified), but "
        "network EGRESS is deliberately reachable and a nested interpreter can open a socket "
        "even though curl/wget/nc are denied. Two OPT-IN gates, both default OFF, both a "
        "one-time consent: `watcherAcceptEgressRisk` is a HARD precondition — `_make_runner` "
        "refuses to build any watcher runner without it, so a watcher cannot run at all until "
        "the operator acknowledges this egress boundary — and `watcherAutoStart` separately "
        "gates whether a polled GET may PROMOTE watchers (a promote used to happen with no "
        "operator action, leaving no consent moment). Treat setting either flag as agreeing to "
        "execute the pull request's comments.",
    ),
    (
        "Auto-Improvement run activity feed",
        "apps/builtins/auto_improvement/backend/runner.py",
        "The live activity ring buffer carries RAW model output — assistant text and the "
        "`command` of a bash tool call — and is served verbatim by GET /run into the "
        "dashboard — so this is an egress boundary, not an internal log. FAIL-CLOSED: an "
        "unscannable string becomes a fixed placeholder while the event structure, "
        "timestamps and other fields survive, so the operator still sees the run "
        "progressing without unscanned text reaching the browser. Recursive, because the "
        "agent event is nested. The WATCHER snapshot and chat-session records are scanned on "
        "the same grounds (`routes._redact_tree`): the watcher log ring is redacted on WRITE, "
        "but `WatcherState.as_dict` beside it served `target`/`title`/`lastNote`/"
        "`verdictReason`/`fixing` raw — all model- or pull-request-derived, and the watcher "
        "ingests PR text as untrusted input by design. Measured with a credential-shaped "
        "`target` (an access-key literal, not reproduced here — this disclosure is itself "
        "scanned): it reached the browser verbatim. The session "
        "records go through it too, because `save_session` merges the caller's patch and the "
        "stored `title` is built from a finding's target. So do the route ERROR bodies: "
        "`commit.py` builds its `error` from `(proc.stderr or '')[:160]` — raw git stderr, "
        "which quotes refs, paths and whatever a repository's own hooks printed. That was "
        "latent while nothing rendered it; surfacing a refused commit at the finding row made "
        'it a live path to the browser, so all five `result.get("error")` responses plus the '
        "PR-status and draft bodies are scanned. "
        "Covers the TERMINAL ERROR field on the same response too "
        '(`_fail`): `f"{type(exc).__name__}: {exc}"` was assigned raw while the feed '
        "beside it was scanned, and an exception message routinely quotes what failed — a "
        "git url, a subprocess argv, a path — so a run dying on an agent-influenced value "
        "carried it to the browser. The exception TYPE is composed in after redaction so "
        "the message stays actionable.",
    ),
    (
        "Auto-Improvement PR prose",
        "apps/builtins/auto_improvement/profiles/github_repo/pr_recipe.py",
        "The pull-request TITLE and BODY are agent-authored prose published by `gh pr "
        "create`, and a PR description cannot be un-published. Redacted in place because "
        "prose survives rewriting; the DIFF beside it is instead detected-and-refused by "
        "`spine/push_policy.py:scan_content_for_secrets`, since rewriting a patch would "
        "corrupt the fix the gate proved.",
    ),
    (
        "Auto-Improvement finding evidence",
        "apps/builtins/auto_improvement/backend/routes.py",
        "The candidate diff and drafted PR body are agent-authored text read back off "
        "disk and rendered in the operator's browser by GET /findings/{fp}. FAIL-CLOSED, "
        "unlike the watcher log below: the text stays on disk and re-readable, so "
        "withholding it beats serving it unscanned.",
    ),
    (
        "Auto-Improvement watcher log",
        "apps/builtins/auto_improvement/backend/pr_watchers.py",
        "Per-PR watcher log lines carry agent output and third-party CI text and are "
        "served to the dashboard via GET /watchers/{fp}/log with NO second scan, so this is "
        "the only pass between that text and the browser. FAIL-CLOSED: an unscannable "
        "line becomes a fixed placeholder, so the log keeps advancing without serving "
        "unscanned text.",
    ),
    (
        "Side-chat parent snapshot",
        "dashboard/side_context.py",
        "Parent user/assistant turns embedded in the side-chat prompt. The prompt "
        "leaves the dashboard's own storage and is persisted by kiro-cli into its "
        "session file, so this is an egress boundary rather than an internal read.",
    ),
    (
        "Dashboard live stream",
        "dashboard/chat_runner.py",
        "Per-chunk StreamRedactor on the chat_chunk WebSocket stream — withholds a "
        "trailing credential-class run so a secret split across chunks cannot cross "
        "the wire.",
    ),
    (
        "Dashboard thinking stream",
        "dashboard/chat_runner.py",
        "Separate StreamRedactor for the ephemeral chat_thinking stream.",
    ),
    (
        "Dashboard final message",
        "dashboard/chat_runner.py",
        "Full-text redaction pass over the assembled assistant turn before it is " "finalized.",
    ),
    (
        "Dashboard slot snapshot",
        "dashboard/state.py",
        "Every message, tool input, tool output, and title in the slot payload sent "
        "to the browser.",
    ),
    (
        "Channel session surfacing",
        "dashboard/channel_slots.py",
        "Titles and hydrated transcript of a Slack/Discord/Teams conversation as it is "
        "surfaced into a dashboard chat slot — the channel transport is a separate "
        "egress boundary, so its content is re-scanned before reaching the browser.",
    ),
    (
        "Session history (JSONL)",
        "history.py",
        "Redacted before persistence, so a leaked credential is not written to disk "
        "and replayed into later context.",
    ),
    (
        "Side-panel stream",
        "dashboard/handlers/side.py",
        "StreamRedactor mirroring the main chat for the side-question stream.",
    ),
    (
        "Steering file metadata",
        "dashboard/handlers/steering.py",
        "First-heading descriptions and display paths in the /api/steering listing "
        "and detail responses. Document CONTENT is deliberately NOT redacted: it "
        "populates the tab's editor and is written straight back on save, so "
        "redacting it would overwrite the user's own file with markers.",
    ),
    (
        "Telemetry spend ranking",
        "dashboard/handlers/telemetry.py",
        "Session titles attached to the conversations-by-spend rows of "
        "/api/telemetry/startup. `display_title` is model-authored, and this is a "
        "SECOND egress path for it alongside the slot snapshot — a title set "
        "through `api_chat_slot_resume` reaches the slot unredacted, so nothing "
        "upstream of this handler has scanned it.",
    ),
    (
        "OpenAI-compatible API",
        "dashboard/openai_compat.py",
        "Streamed and non-streamed completions served to third-party clients.",
    ),
    (
        "Slack messages",
        "slack/handler.py",
        "StreamRedactor on the live edit stream plus a full pass on the final posted " "message.",
    ),
    (
        "Ops Mission Control Slack board",
        "apps/builtins/ops_mission_control/backend/slack_out.py",
        "Incident titles, resources, and diagnoses mirrored to an ops channel. A "
        "separate egress boundary from slack/handler.py: this text originates in a "
        "third-party provider's alarm payload (not a model turn) and lands in a "
        "channel whose audience is usually wider than the dashboard's.",
    ),
    (
        "Slack cron / notification posts",
        "slack/gateway.py",
        "Job names, cron results, and error strings before they reach a channel.",
    ),
    (
        "Subagent results",
        "subagent.py",
        "Task text and returned results before injection into the parent context or "
        "the dashboard.",
    ),
    (
        "Voice reply (TTS)",
        "voice_reply.py",
        "Spoken text is redacted before synthesis so a credential is never read " "aloud.",
    ),
    (
        "SEL audit log",
        "sel.py",
        "String fields are redacted before an event is forwarded to a log "
        "integration; on-disk records are redacted by each caller before `log` "
        "(the writer signs bytes as-written, so it cannot redact them itself).",
    ),
    (
        "Task reports",
        "task_reporter.py",
        "Run names, titles, and bodies in generated task reports — "
        "exfiltration-URL scanning only, not the credential scanner.",
    ),
    (
        "Vector memory snippets",
        "vector_memory.py",
        "Search-result snippets before they are surfaced or re-embedded.",
    ),
    (
        "Workflow injections",
        "dashboard/workflow_inject.py",
        "Workflow progress summaries injected back into a session.",
    ),
    (
        "Onboarding import",
        "onboarding_import.py",
        "Imported foreign-agent history and config before it enters Kiro Crew.",
    ),
    (
        "Discord / Telegram / WeCom / Webex",
        "messaging/driver.py",
        "The channel-neutral TurnDriver's StreamRedactor — every non-Slack chat "
        "channel inherits redaction from this one egress.",
    ),
    (
        "Discord direct send",
        "discord/transport_dispatch.py",
        "The direct-send path that bypasses TurnDriver, redacted independently.",
    ),
    (
        "Discord session-resume replay",
        "discord/session_resume.py",
        "Session titles in the `!sessions` picker and the transcript replayed when a "
        "dashboard session is resumed — content authored in one surface is re-scanned "
        "before it crosses into Discord, which is a separate egress boundary. Also "
        "neutralizes `@` mentions so replayed text cannot ping a server.",
    ),
    (
        "Outbound channel messages",
        "dashboard/handlers/messaging.py",
        "Recursively redacts outbound message payloads before they leave the gateway.",
    ),
    (
        "Streaming speech-to-text",
        "dashboard/stt_stream.py",
        "Transcription partials and finals are redacted before they leave the process.",
    ),
    (
        "Agent channels",
        "channel.py",
        "Tool names and tool inputs shared between agents on a persistent channel.",
    ),
    (
        "App lifecycle output",
        "apps/routes.py",
        "Install/start/stop script output and warnings surfaced from an app.",
    ),
    (
        "App teardown output",
        "apps/teardown.py",
        "Output from an app's own onDisable script, scrubbed by the dual-pass "
        "redact() helper before it becomes a warning on the disable and "
        "trust-revocation responses.",
    ),
    (
        "App activity log",
        "apps/builtins/mochi/activity_log.py",
        "Agent-authored activity entries are redacted before persistence, for the "
        "same reason as the session JSONL: the file is served back over the app's "
        "activity route AND read into a later prompt, so an unredacted credential "
        "would be written to disk and then replayed. Redaction sits at the single "
        "write point rather than at each caller.",
    ),
    (
        "App plan endpoint",
        "apps/builtins/mochi/backend/routes.py",
        "The agent-authored plan queue (update_plan) is served to the dashboard "
        "over the app's /plan route; a credential or webhook URL an LLM wrote into "
        "a narrative/task field is recursively redacted here before json_response, "
        "the same output-boundary reason as the app activity log.",
    ),
    (
        "Slack render pipeline",
        "slack/format.py",
        "The Slack RENDERING boundary: text that is converted to mrkdwn goes "
        "through render_for_slack / render_one_for_slack, and a build gate "
        "(test_slack_render_pipeline.py) fails if any module calls "
        "to_slack_mrkdwn itself. Both helpers strip ANSI and run "
        "redact_via_context BEFORE conversion and again after, because neither "
        "ordering is safe alone -- the ANSI strip inside to_slack_mrkdwn "
        "reassembles a credential the escapes had broken up, while its 39k "
        "self-truncation cuts one into an unmatchable prefix. Text is pre-split "
        "below that ceiling so conversion never truncates, and OPTIONS choice "
        "labels are redacted before the Block Kit slices. SCOPE, stated exactly: "
        "the gate polices the CONVERSION primitive, not the Slack API calls -- a "
        "path that posts raw text without ever converting it is not covered here "
        "and relies on its own redaction.",
    ),
    (
        "Slack session mirror",
        "dashboard/chat_slack.py",
        "Thread titles and the conversation history seeded into a newly linked "
        "thread. Titles go through redact_and_truncate (redaction BEFORE "
        "truncation, so a truncation boundary cannot split and hide a "
        "credential); history is delegated to the shared Slack render pipeline "
        "above with redact_via_context injected as its redactor.",
    ),
    (
        "Configured-channel session mirror",
        "dashboard/chat_mirror.py",
        "Recent dashboard context posted while linking a configured non-Slack "
        "destination, via redact_via_context before transport dispatch, then "
        "chunked to the channel's own message limit rather than truncated.",
    ),
    (
        "Slack Block Kit views",
        "slack/blocks.py",
        "Message text rendered into Block Kit payloads (Home Tab, session views).",
    ),
    (
        "File cards broadcast to the browser",
        "dashboard/handlers/files.py",
        "The file-card JSON pushed over the chat WebSocket is redacted before "
        "broadcast. (This module's other redact() calls are upload GATES — they "
        "abort a send when redaction would alter the content — not egress.)",
    ),
    (
        "MCP app tool results",
        "dashboard/handlers/mcp_apps.py",
        "Recursively redacts every string leaf of an MCP app's tool result before "
        "it reaches the browser.",
    ),
    (
        "MCP app render payloads",
        "mcp_apps_render.py",
        "Recursively redacts string leaves of a rendered MCP app payload.",
    ),
    (
        "Auto-skill pending detail / promotion",
        "skills.py",
        "LLM-authored auto-skill candidate content (SKILL.md, scripts, and nested "
        ".meta.json values/keys) is redacted at the pending detail-read choke "
        "before it is returned by the dashboard skills API, and again in-place "
        "before an approved candidate is promoted to the live skills dir.",
    ),
    (
        "Ops provider evidence",
        "apps/builtins/ops_mission_control/backend/registry.py",
        "Third-party ops payloads (CloudWatch log lines, Datadog monitor context, "
        "and any companion-contributed EvidenceSource) are redacted at the single "
        "gather_evidence choke before they reach a model prompt, a transcript, or "
        "Slack — centrally, so an adapter author cannot leak a credential by "
        "forgetting to redact. Routed through redact_via_context, so a loaded "
        "companion's own credential patterns apply and a host that fails to compose "
        "one fails closed rather than silently falling back to public patterns.",
    ),
    (
        "Ops shared-ledger push",
        "apps/builtins/ops_mission_control/backend/ledger_sync.py",
        "The LAST gate before this app's one published artifact leaves the machine. "
        "`ledger.jsonl` is committed and pushed to the team's git remote, so a "
        "credential sitting in a legacy row — written by an older build, or by any "
        "path other than the redacting POST /ledger route — would be fetched by every "
        "teammate and require a history rewrite to recall. The pre-push scan takes the "
        "UNION of `security.get_credential_patterns()` and this app's own "
        "`secrets.redact_tokens`, because neither is a superset: the core patterns "
        "carry AKIA/ASIA and miss a prefixed Datadog application key, while "
        "redact_tokens knows the provider shapes and misses an AWS access key id. A "
        "flagged line REFUSES the push rather than redacting it in place — the ledger "
        "is the operator's own knowledge and silently rewriting it would destroy the "
        "lesson — and the refusal reports line NUMBERS only, never the matched text, "
        "since it is logged to SEL and the console.",
    ),
    (
        "Ops investigation brief",
        "apps/builtins/ops_mission_control/backend/dispatch.py",
        "The signal's own provider-controlled metadata — title, resource and provider "
        "URL — is redacted before it is rendered into the investigation brief, which "
        "goes into the agent's context and from there into the transcript and any "
        "session artifact. A signed webhook is accepted from anything able to POST "
        "JSON and a console link can carry a token in its query string, so this "
        "metadata is exactly as untrusted as the evidence bodies gather_evidence "
        "already covers — that sink was registered while the metadata printed beside "
        "it was not. Routed through redact_via_context for the same companion-seam "
        "reason. Fields this app assigns (source, severity, fired_at, fingerprint, "
        "operating_mode) are deliberately not redacted: masking one could only "
        "corrupt a value the agent needs to reason about.",
    ),
    (
        "Ops knowledge ledger",
        "apps/builtins/ops_mission_control/backend/routes.py",
        "The learned pattern/fix pair is redacted on the WRITE path (POST /ledger), "
        "before the content-addressed id is computed. This is the app's only artifact "
        "that leaves the machine: ledger_sync commits ledger.jsonl to a shared git "
        "remote, and a 'fix' field is the likeliest place for a pasted credential "
        "because a command line is what a fix looks like. Write-path rather than "
        "sync-path because the entry is on local disk and in the vector index long "
        "before any sync runs, and an operator who enables sync later would otherwise "
        "retroactively publish everything written before it. ledger_sync.push() adds a "
        "second, independent refusal for entries that predate this redactor.",
    ),
    (
        "Ops Mission Control desktop notifications",
        "apps/builtins/ops_mission_control/backend/notify_out.py",
        "Incident titles and provider failure reasons pushed onto the local "
        "notification bus. A separate egress from the Slack board even though the "
        "source text is the same third-party provider payload: this one lands in the "
        "OS notification centre and in the persisted notification JSONL. Runs the "
        "credential and exfiltration-URL scanners plus the app's own provider-token "
        "pass, matching the postmortem writer — core redaction alone leaves a "
        "provider api_key inside a URL intact.",
    ),
    (
        "Ops Mission Control incident postmortem",
        "apps/builtins/ops_mission_control/backend/store.py",
        "The per-incident Markdown artifact written when an incident closes "
        "(incidents/<id>.md). A distinct boundary from the two above because the "
        "file is a SHAREABLE local artifact — it exists so an operator can hand a "
        "colleague, or a ticket, the investigation record — so its provider titles "
        "and model-authored diagnosis are redacted at the write, not at a read.",
    ),
    (
        "Diagnostics support bundle",
        "diagnostics.py",
        "The redacted zip built by `kirocrew doctor --bundle` and Settings › About › "
        "Report a Problem, plus the pre-filled GitHub issue URL it returns. The most "
        "external boundary in this list: the artifact exists to be attached to a "
        "PUBLIC issue, and its members are raw gateway/kiro-cli logs and crash "
        "reports. Every text member and the user-typed note run the credential and "
        "exfiltration-URL scanners plus a sensitive-header pass before anything is "
        "written into the archive.",
    ),
    (
        "Tag definitions (HTTP + auto-tag)",
        "dashboard/chat_tags.py",
        "Tag names supplied by both the POST /api/chat/tags HTTP handler and the "
        "background auto-tag task are LLM-authored or project-derived and persist "
        "to tags.json, the dashboard sidebar, and Slack notifications. Both paths "
        "redact credentials and exfiltration URLs before creation/persistence.",
    ),
    (
        "Background auto-tag (project-derived names)",
        "dashboard/chat_auto_tag.py",
        "The background auto-tag task derives tag names from the slot's project "
        "path and persists them to tags.json and the dashboard sidebar via the "
        "shared tag-creation path. Names are passed through redact_credentials "
        "and redact_exfiltration_urls before resolution or persistence.",
    ),
)

# Modules that call a redactor but are NOT an output egress boundary, so they do
# not earn a `redaction_paths` row. Enumerated explicitly (rather than left
# implicit) because the drift guard in ``test_security_posture`` walks every
# redactor call site and requires each module to be either a registered sink or
# listed here — so a NEW egress path cannot be added without someone deciding
# which bucket it belongs in. That inverse check is the whole point: the failure
# mode is a silently omitted egress path, and only an omission-detecting test
# catches an omission.
NON_EGRESS_REDACTION_MODULES: frozenset[str] = frozenset(
    {
        # Inbound / gate-side: redacts what comes IN or what a gate logs, not what
        # goes out to a human.
        "context.py",
        "agent.py",
        # The shared recursive redactor helper itself — a pure scrubber, not an
        # egress boundary; the modules that CALL it (mochi routes/hooks) are the
        # registered sinks.
        "apps/builtins/mochi/redact.py",
        "autonudge_authz.py",
        "acp/_dispatch.py",
        "acp/client.py",
        "acp/session_handle.py",
        "platform/defaults.py",
        "platform/interfaces.py",
        # Comparison-only: applies the redactors to compute a match identity and
        # discards the result. The two files being merged can hold the same
        # message with and without redaction, so a raw comparison would keep both
        # copies — nothing redacted here is ever written or shown.
        "channel_transcript_migration.py",
        # DETECTOR, not a redactor: the pre-push content scan calls both scanners only
        # to COUNT findings and then refuses the push. It deliberately discards the
        # cleaned text — rewriting a code diff would corrupt the very fix the gate
        # proved — so it is not a redaction egress path. The push it guards is not an
        # output boundary this panel measures either: nothing reaches GitHub when the
        # scan hits, and the change degrades to the local queue instead. Lives in
        # push_policy because all three push paths share this one implementation.
        "apps/builtins/auto_improvement/spine/push_policy.py",
        # Internal persistence / indexing (the on-disk or in-memory copy), whose
        # user-visible surface is already covered by a registered sink.
        "dashboard/chat_folders.py",
        "dashboard/chat_fork.py",
        "dashboard/chat_handlers.py",
        "dashboard/chat_nav.py",
        "dashboard/chat_orchestrator.py",
        "dashboard/chat_persistence.py",
        "dashboard/chat_regenerate.py",
        "dashboard/chat_rewind.py",
        "dashboard/chat_title.py",
        "dashboard/chat_utils.py",
        "dashboard/chat_voice.py",
        # Pre-redacts follow-up items before handing to state.py's WS egress
        # (the registered sink); its own return string is re-redacted by
        # chat_runner before broadcast. Not itself an egress boundary.
        "dashboard/session_directive_apply.py",
        "dashboard/cron_inject.py",
        "dashboard/ws.py",
        "dashboard/server.py",
        "dashboard/handlers/sessions.py",
        "dashboard/handlers/artifacts.py",
        "dashboard/handlers/core.py",
        "dashboard/handlers/cron.py",
        "dashboard/handlers/discover.py",
        "dashboard/handlers/hooks.py",
        "dashboard/handlers/knowledge.py",
        "dashboard/handlers/mcp.py",
        "dashboard/handlers/memory.py",
        "dashboard/handlers/optimizer.py",
        "dashboard/handlers/prompts.py",
        "dashboard/handlers/source_providers.py",
        "dashboard/handlers/taskrunner.py",
        "dashboard/handlers/terminal.py",
        "dashboard/handlers/themes.py",
        "dashboard/handlers/updates.py",
        "dashboard/handlers/webapp_preview.py",
        "dashboard/handlers/workflows.py",
        "dashboard/handlers_project.py",
        "knowledge/agent_fetch.py",
        "knowledge/agent_source.py",
        "knowledge/artifact_ingest.py",
        "knowledge/ingestion.py",
        "mcp_core.py",
        "mcp_cron.py",
        "mcp_discovery.py",
        "mcp_gateway/backend.py",
        "workflows/agent_exec.py",
        "workflows/agent_pool.py",
        "workflows/runner.py",
        "workflows/store.py",
        "apps/event_bus.py",
        "sync_bridge.py",
        "suggestions.py",
        "tips.py",
        "task_executor.py",
        "taskrunner.py",
        "transcribe.py",
        "metrics/schema.py",
        # Egresses, but carries NO redactable content: the payload is a fixed
        # five-key allowlist built by beacon.payload() (random install id,
        # release, Python minor, distribution channel, first-run bit).
        # There is no free-form field and no caller-supplied pass-through, so
        # there is nothing for a redactor to scrub — the allowlist IS the
        # control. It matches the drift scanner only because its module
        # docstring explains why it does NOT route through metrics/schema.py's
        # redact() (that guardrail would replace the install id with
        # "[REDACTED]" and make DAU compute as 1).
        "beacon.py",
        # Egresses, and for the same reason carries NO redactable content: the
        # app-install receipt sends a fixed three-field set (a truncated HMAC, a
        # two-valued kind, the clamped release) plus the official catalog slug in
        # the path. There is no free-form field and no caller-supplied
        # pass-through, so the allowlist IS the control. It matches the drift
        # scanner only because its docstring explains why it does NOT route
        # through metrics/schema.py's redactor.
        "apps/install_receipt.py",
        "cron_script.py",
        "eval/runner.py",
        "kiro_prerequisite.py",
        "instances/ssh_tunnel_manager.py",
        "instances/token_mint.py",
        "instances/ssm_token_mint.py",
        "publish_sync.py",
        "cli_commands.py",
        # Slack sub-surfaces whose posted output is covered by the two Slack rows.
        "slack/events.py",
        # Inbound attachment ingestion: redacts text extracted FROM a user's
        # own uploaded file before it enters the prompt. Inbound sanitisation,
        # not agent output on its way to a user.
        "messaging/attachments.py",
        "slack/interactions.py",
        "slack/renderer.py",
        "slack/sessions_view.py",
        # Redaction of a LOG line or a diagnostic URL/token, not agent output on
        # its way to a user. These match the (deliberately broad) redactor regex
        # in the drift guard but are not egress paths.
        "browser/setup.py",
        "cli_doctor.py",
        "cloud/connect.py",
        "cloud/login.py",
        "embeddings.py",
        # NOTE: papyrus's tectonic.py is deliberately NOT here — see the sinks
        # list below. Its redacted URL does reach the dashboard, so filing it as
        # non-egress was wrong and would have let the drift guard miss a future
        # change that started returning `{exc}` verbatim.
        # Same shape again: pptx-maker's digest-pinned engine download redacts the
        # DOWNLOAD URL (userinfo + signed query) before logging it, so a
        # mirrored/presigned KIROCREW_PPTX_ENGINE_URL override cannot leak
        # credentials into a log. Nothing here reaches a user-facing surface — the
        # app's own egress path (model-authored deck names and brief previews)
        # redacts separately in pptx_maker/backend/decks.py.
        "apps/builtins/pptx_maker/backend/engine_source.py",
        # Uses the redactor as a PREDICATE, not a transform: `resolve_deck_dir`
        # compares `redact(deck_id) != deck_id` to decide whether to refuse the deck
        # outright. A deck id cannot be scrubbed on the way out — it is the directory
        # name, the `preview/<deckId>/...` URL segment and the handle every later
        # request sends back — so the only safe answer is not to serve that deck at
        # all. Nothing is emitted here; the app's egress path is decks.py/routes.py.
        "apps/builtins/pptx_maker/backend/paths.py",
        # Redacts INBOUND attacker-controllable provider metadata before it is
        # stored/displayed — a sanitizer on the way in, not an output boundary.
        "dashboard/handlers/mcp_discover.py",
        # Computer use: the redaction pass runs on third-party desktop content
        # (window titles, accessibility values) on its way INTO the model's
        # context, exactly like the MCP tool-result paths above. `policy.py` owns
        # the single pass, `render.py` ends every renderer with it, and `tools.py`
        # applies it to the error string. The user-visible surface for all three is
        # the dashboard/Slack transcript, which is a registered sink.
        "computer_use/policy.py",
        "computer_use/render.py",
        "computer_use/tools.py",
        # redact_via_context helpers: the CredentialPolicy seam and its callers.
        # Each redacts a value bound for an audit record or a log tail, and the
        # user-facing surfaces that consume them are registered sinks above.
        "platform/context.py",
        "mcp_shared.py",
        "dashboard/handlers/agents.py",
        # Pre-publish content scanning (a scan, not an egress of agent output).
        "deploy/handlers.py",
        "deploy/iam.py",
        "deploy/profiles.py",
        "deploy/scan.py",
        # Bundled app backends: each app's own surface, not core egress.
        "apps/builtins/auto_research/handlers.py",
        "apps/builtins/code_review_sage/sage_lib/learning.py",
        "apps/builtins/code_review_sage/sage_lib/pipeline.py",
        "apps/builtins/code_review_sage/sage_lib/report.py",
        "apps/builtins/code_review_sage/sage_lib/review_driver.py",
        # `store` DEFINES this app's redactor (`redact_text`) so every reader in the
        # app can scrub, not just the posting path; `discovery` calls it when reading
        # the worker-writable pinned-repo file before the sidebar renders it. Both
        # are the app's own surface, same classification as its siblings above.
        "apps/builtins/code_review_sage/sage_lib/store.py",
        "apps/builtins/code_review_sage/sage_lib/discovery.py",
        "apps/builtins/dev_fleet/server.py",
        "apps/builtins/issue_radar/backend/routes.py",
        "apps/builtins/meetings/backend/domain/session.py",
        "apps/builtins/meetings/backend/providers/calendar.py",
        "apps/builtins/meetings/backend/providers/tasks.py",
        "apps/builtins/meetings/backend/routes/agents.py",
        "apps/builtins/meetings/backend/routes/meeting_lifecycle.py",
        "apps/builtins/meetings/backend/routes/tasks.py",
        "apps/builtins/papyrus/backend/routes.py",
        # A real egress boundary, not a log-only redaction: `_download_to` returns
        # `f"download failed (...) from {redact_url(url)}"`, which lands in the
        # persisted job state, rides `GET /health`, and is rendered verbatim in the
        # dashboard's install banner. The redaction itself is host-only (so a
        # credentialed mirror override cannot leak), but it must be REGISTERED here
        # or the drift guard cannot notice a change that starts returning `{exc}`.
        "apps/builtins/papyrus/backend/tectonic.py",
        "apps/builtins/pptx_maker/backend/decks.py",
        "apps/builtins/pptx_maker/backend/routes.py",
        "apps/builtins/spec_builder/backend/routes.py",
        "apps/builtins/workflows/server.py",
        # Bundled dev-skill script: prints CI/review findings to a
        # developer terminal, not an agent-output egress path.
        "builtin_skills/kirocrew-dev/prepare-pr/scripts/pr_findings.py",
        # Ops Mission Control provider-token redactor. ``secrets.py`` DEFINES
        # ``redact_tokens`` (the PagerDuty/Datadog token shapes) rather than
        # crossing a boundary with it — the same self-referential case as
        # ``security.py`` itself. ``providers/http.py`` applies it to an
        # ``HttpError`` message so a 401 body echoing a token cannot reach a log;
        # that is a log/diagnostic scrub, not agent output on its way to a user.
        # The real egress choke for this app is ``gather_evidence`` in
        # ``.../backend/registry.py``, a registered sink above.
        "apps/builtins/ops_mission_control/backend/secrets.py",
        "apps/builtins/ops_mission_control/backend/providers/http.py",
    }
)


# ── Credential families ──
# The credential CLASSES the redaction regex matches. Family names only — never a
# pattern that could be inverted into a generator, and never live secrets.
_CREDENTIAL_FAMILIES: tuple[tuple[str, str], ...] = (
    (
        "AWS access keys",
        "AKIA / ASIA key IDs, plus labelled secret-access-key and session-token forms",
    ),
    (
        "Private keys",
        "PEM blocks (RSA / DSA / EC / OPENSSH), including encrypted and truncated bodies",
    ),
    ("Slack tokens", "xoxb / xoxp / xoxa / xoxs bot, user, and app tokens"),
    (
        "GitHub tokens",
        "ghp / gho / ghu / ghs / ghr classic tokens and github_pat fine-grained tokens",
    ),
    ("GitLab tokens", "glpat personal access tokens"),
    ("Stripe keys", "sk_live / rk_live / sk_test / rk_test secret and restricted keys"),
    ("SendGrid keys", "SG. prefixed API keys"),
    ("OpenAI keys", "sk-proj project keys"),
    ("Anthropic keys", "sk-ant API keys"),
    ("npm tokens", "npm_ access tokens"),
    ("PyPI tokens", "pypi- API tokens"),
    ("DigitalOcean tokens", "dop / doo / dor v1 personal, OAuth, and refresh tokens"),
    ("Google OAuth secrets", "GOCSPX- client secrets"),
    ("Telegram bot tokens", "numeric bot id joined to a URL-safe secret"),
    ("JWT / JWE tokens", "3-segment signed and 5-segment encrypted compact tokens"),
    # Phrased WITHOUT a literal "Authorization: Bearer <token>" sequence: that
    # exact shape is what the scanner matches, so spelling it out here would make
    # this very description self-redacting wherever the payload is itself scanned
    # (the SEL audit log, a Slack-relayed posture summary) — the row would render
    # as "[REDACTED: credential]". A pinned test asserts the whole payload survives
    # redact_credentials() unchanged; keep new descriptions clear of live shapes.
    ("HTTP bearer tokens", "Bearer-scheme HTTP auth headers, including JSON-serialized log dumps"),
    (
        "Database URIs",
        "postgres / mysql / mongodb / redis / amqp connection strings with an embedded password",
    ),
    (
        "Base64-encoded variants",
        "40+ char base64 chunks are decoded and re-scanned against every family above",
    ),
)


# ── URL exfiltration heuristics ──
_EXFIL_HEURISTICS: tuple[tuple[str, str], ...] = (
    (
        "Credential in path or query",
        "Unconditional floor — an AWS key, PEM header, or Slack token anywhere in the "
        "URL is exfiltration regardless of destination.",
    ),
    (
        "Base64-encoded credential",
        "Base64 chunks in the query are decoded and re-scanned, so an encoded secret "
        "cannot slip past the literal-marker floor.",
    ),
    (
        f"Long query string (>= {security.exfil_query_min_len()} chars)",
        "A query large enough to carry a stolen payload.",
    ),
    (
        "Credential-like query data",
        "Base64 blobs and key-shaped values in query parameters.",
    ),
    (
        "Heavy percent-encoding",
        "20+ consecutive percent-encoded characters — an obfuscated payload. Applies "
        "to every host, including exempted ones.",
    ),
)


def _own_namespace_prefixes() -> tuple[str, ...]:
    """Home-relative prefixes that belong to KiroCrew / kiro-cli itself.

    Derived from the crew data-home prefixes, plus the ``.kiro`` parent that
    holds kiro-cli's own state and the gateway's auth staging dir. Used only to
    label a row in the posture view — a misclassification is cosmetic, never a
    gate decision.
    """
    return tuple({p.split("/", 1)[0] for p in security.crew_home_prefixes()})


def _sensitive_path_items() -> list[PostureItem]:
    """Credential paths blocked at the hook layer, classified by owner."""
    items: list[PostureItem] = []
    own = _own_namespace_prefixes()
    for entry in security.sensitive_home_dirs():
        # Path-boundary match, so a sibling like `.kirocrew-notes` is not counted
        # as ours just because it shares a string prefix.
        first = entry.split("/", 1)[0]
        if first in own:
            detail = "Kiro Crew trust root — the agent can neither read nor write it"
        else:
            detail = "Third-party credential store"
        items.append(PostureItem(label=f"~/{entry}", detail=detail))
    return items


def _write_protected_items() -> list[PostureItem]:
    """Paths readable but not writable by agent tools."""
    return [
        PostureItem(
            label=f"~/{entry}",
            detail="Reads allowed; writes blocked so the agent cannot raise its own limits",
        )
        for entry in security.write_protected_home_paths()
    ]


def _denied_command_items() -> list[PostureItem]:
    """Built-in deny rules, described (not raw-regex) and grouped by category."""
    return [
        PostureItem(label=rule.description, detail=rule.category)
        for rule in security.BUILTIN_DENIED_RULES
    ]


def _suspicious_pattern_items() -> list[PostureItem]:
    return [PostureItem(label=pattern) for pattern in security.SUSPICIOUS_BASH_PATTERNS]


#: Every MCP tool-schema dispatch registry in ``validation``, by attribute name. A
#: tool is only validated if it is IN one of these, so this list defines what the
#: posture view can see. Keep it complete — ``test_security_posture`` reads the same
#: names, so an omission fails there rather than quietly shrinking the report.
_SCHEMA_REGISTRY_NAMES: tuple[str, ...] = (
    "MCP_CORE_SCHEMAS",
    "MCP_CRON_SCHEMAS",
    "MCP_COMPUTER_SCHEMAS",
)


def _tool_schema_items() -> list[PostureItem]:
    """Validated tool schemas, keyed by the tool name they gate.

    Derived from EVERY dispatch registry in ``validation`` — the dicts
    ``mcp_core``/``mcp_cron``/``computer_use.tools`` actually look a tool up in —
    plus the module-level ``*_SCHEMA`` objects that gate dashboard handlers rather
    than MCP tools.

    Deriving from the registries (not the ``*_SCHEMA`` naming convention) makes
    the registry, not the convention, the source of truth. Several registered MCP
    tools (e.g. ``cron_trigger``) are defined as inline or shared ``ToolSchema``
    objects with no module-level name of their own, so a convention-only walk
    validates them but cannot see them here — a tool registered that way would add
    zero to the count.

    ``_SCHEMA_REGISTRY_NAMES`` is enumerated rather than discovered, but the drift
    test derives its expectation from the same module attributes, so a NEW registry
    that is not listed here fails that test instead of silently under-reporting.
    """
    seen: dict[str, str] = {}
    for registry_name in _SCHEMA_REGISTRY_NAMES:
        registry = getattr(_validation, registry_name, None) or {}
        for tool_name in registry:
            seen.setdefault(tool_name, registry_name)
    for name in dir(_validation):
        if not (name.endswith("_SCHEMA") and name.isupper()):
            continue
        tool_name = getattr(getattr(_validation, name, None), "tool_name", "") or ""
        # A registry entry wins the attribution: it names where the schema is
        # actually enforced. Nameless/helper constants are skipped so the count
        # and the list always agree.
        if tool_name:
            seen.setdefault(tool_name, name)
    return [PostureItem(label=tool, detail=source) for tool, source in sorted(seen.items())]


def _redaction_sink_items() -> list[PostureItem]:
    return [
        PostureItem(label=label, detail=f"{module} — {detail}")
        for label, module, detail in _REDACTION_SINKS
    ]


def _credential_family_items() -> list[PostureItem]:
    return [PostureItem(label=label, detail=detail) for label, detail in _CREDENTIAL_FAMILIES]


def _exfil_heuristic_items() -> list[PostureItem]:
    return [PostureItem(label=label, detail=detail) for label, detail in _EXFIL_HEURISTICS]


# Human-readable gloss per SEL ``source`` value. The KEYS are not authoritative —
# ``sel._infer_source`` is — so a source added there without a gloss here still
# gets a row (labelled by its raw token) rather than being silently dropped, and a
# pinned test fails so the gloss gets written.
_AUDIT_SURFACE_DETAIL: dict[str, str] = {
    "slack": "Messages, approvals, and owner-authorization decisions",
    "dashboard": "Tool invocations, permission decisions, and authenticated API access",
    "cron": "Scheduled job fires and their tool calls",
    "subagent": "Spawned agent lifecycle, tool calls, and results",
    "taskrunner": "Autonomous multi-step task execution",
    "background": "Background maintenance work",
    "heartbeat": "Liveness / watchdog activity",
    "cli": "Terminal chat sessions",
    "discord": "Discord messages, approvals, and owner-authorization decisions",
    "telegram": "Telegram messages, approvals, and owner-authorization decisions",
    "wecom": "WeCom messages, approvals, and owner-authorization decisions",
    "weixin": "Weixin messages, approvals, and owner-authorization decisions",
    "webex": "Webex messages, approvals, and owner-authorization decisions",
    "teams": "Microsoft Teams messages, approvals, and owner-authorization decisions",
    "host": "In-process governance checks not driven by a user-facing surface",
    "unknown": "Events that carry no surface signal (classified rather than misattributed)",
}


def _audit_surface_items() -> list[PostureItem]:
    """Audited surfaces, DERIVED from ``sel._infer_source``'s vocabulary.

    ``_infer_source`` maps a session key to a surface, so its return vocabulary is
    the set of surfaces SEL can infer — deriving from it means adding one moves
    this count automatically.

    SCOPE (deliberate): a caller may pass an explicit ``source=`` that bypasses
    inference entirely (``channel``, ``token_auth``, ``migration``, …; ~70 such
    literals exist, many of which are event *kinds* rather than surfaces). Those
    are NOT enumerable as a clean "audited surfaces" list, so this control is
    scoped to the inferred vocabulary and its unit/summary say so — a floor, not a
    total. Claiming otherwise would be the very overstatement this module exists
    to remove.
    """
    return [
        PostureItem(
            label=source.replace("_", " ").capitalize(),
            detail=_AUDIT_SURFACE_DETAIL.get(source, "Audited SEL event source"),
        )
        for source in sorted(_sel_mod.audit_sources())
    ]


def _token_auth_items() -> list[PostureItem]:
    # circular import: token_auth lives under kiro_crew.dashboard, whose package
    # __init__ imports the server (which imports this module via handlers.core).
    # A top-level import here would close that cycle, so the two TTL constants are
    # read at call time — the documented circular-import exception, and it keeps
    # the advertised windows derived from the enforcing module rather than
    # restated as literals that could drift.
    from kiro_crew.dashboard.token_auth import (
        LINK_WINDOW_SECS,
        MAX_SESSION_TTL_SECS,
        proxied_pin_observed,
    )

    # Tri-state, deliberately, and derived from the LIVE bindings so it recovers
    # on its own. A pin that has collapsed onto a same-host proxy's loopback
    # address is NOT the control this row used to advertise, and "nothing is
    # pinned right now" is not evidence that pins are effective — rendering
    # either as the plain claim is the failure this row is being corrected for.
    _pinned = proxied_pin_observed()
    if _pinned is None:
        _pin_detail = (
            "A session is bound to the address that first used it. No session is "
            "currently pinned, so the effective scope is not known yet"
        )
    elif _pinned:
        _pin_detail = (
            "SHARED, not per-client: sessions are binding to a proxy's address rather than a "
            "client's — either a same-host tunnel (cloudflared / ngrok / tailscale serve) or a "
            "reverse proxy in front of this gateway — so every client reaching the dashboard "
            "through it satisfies the same pin. Reach the dashboard directly, or over a "
            "transport that preserves the client address, for the pin to identify one client"
        )
    else:
        _pin_detail = "A session is bound to the client address that first used it"

    return [
        PostureItem(
            label="HMAC-SHA256 signature",
            detail="Tokens are signed with a persisted secret; a forged or tampered token is rejected",
        ),
        PostureItem(
            label="IP pinning",
            detail=_pin_detail,
        ),
        PostureItem(
            label="Single-use link nonce",
            detail=f"A share link is consumed on first click, within a {LINK_WINDOW_SECS // 60}-minute window",
        ),
        PostureItem(
            label="Bounded session lifetime",
            detail=f"Session cookies expire after at most {MAX_SESSION_TTL_SECS // 3600} hours",
        ),
        PostureItem(
            label="Revocation generation",
            detail="Bumping the generation invalidates every outstanding token at once",
        ),
        PostureItem(
            label="App-token scoping",
            detail="An app token reaches only its own namespace plus its manifest-declared API paths (deny-by-default)",
        ),
    ]


# ── The registry ──
# Order is display order. Each entry's count is len(items_fn()).
_CONTROLS: tuple[PostureControl, ...] = (
    PostureControl(
        key="sensitive_paths",
        label="Sensitive path blocking",
        unit="credential paths",
        summary=(
            "Paths the agent cannot read or write. Enforced at the PreToolUse gate on the "
            "resolved target, so a symlink into a blocked directory is refused too."
        ),
        source="src/kiro_crew/security.py",
        items_fn=_sensitive_path_items,
    ),
    PostureControl(
        key="write_protected_paths",
        label="Write-protected paths",
        unit="config paths",
        summary=(
            "Readable but not writable by agent tools — config carrying resource ceilings "
            "and the data-home migration marker."
        ),
        source="src/kiro_crew/security.py",
        items_fn=_write_protected_items,
    ),
    PostureControl(
        key="denied_commands",
        label="Denied commands",
        unit="built-in rules",
        summary=(
            "Destructive and credential-exfiltrating shell operations blocked at the "
            "PreToolUse gate. Configurable below; policy-pinned rules cannot be turned off."
        ),
        source="src/kiro_crew/security.py",
        items_fn=_denied_command_items,
    ),
    PostureControl(
        key="suspicious_patterns",
        label="Suspicious bash patterns",
        unit="patterns",
        summary=(
            "Deletion, exfiltration, and pipe-to-interpreter shapes. Advisory: these "
            "are surfaced by the `kirocrew` history scan, NOT blocked at the "
            "PreToolUse gate — the gate enforces the narrower denied-command rules "
            "and exfiltration checks above."
        ),
        source="src/kiro_crew/security.py",
        items_fn=_suspicious_pattern_items,
    ),
    PostureControl(
        key="tool_schemas",
        label="MCP input validation",
        unit="tool schemas",
        summary=(
            "Every MCP tool call is checked against a typed schema: unicode "
            "normalization, length limits, enum allow-lists, and unknown-field rejection."
        ),
        source="src/kiro_crew/validation.py",
        items_fn=_tool_schema_items,
    ),
    PostureControl(
        key="redaction_paths",
        label="Output redaction",
        unit="output paths",
        summary=(
            "Every boundary where agent output reaches a human or an external "
            "service runs a redaction pass first. Most run both scanners; the few "
            "that run only one say so on their own row."
        ),
        source="src/kiro_crew/security.py",
        items_fn=_redaction_sink_items,
    ),
    PostureControl(
        key="credential_families",
        label="Credential patterns",
        unit="credential families",
        summary=(
            "Credential classes the redaction scanner recognizes, in plaintext and "
            "base64-encoded form."
        ),
        source="src/kiro_crew/security.py",
        items_fn=_credential_family_items,
    ),
    PostureControl(
        key="exfil_heuristics",
        label="URL exfiltration detection",
        unit="heuristics",
        summary=(
            "Domain-agnostic — flags the payload, not the destination. A URL matching "
            "any heuristic is replaced with a redaction marker."
        ),
        source="src/kiro_crew/security.py",
        items_fn=_exfil_heuristic_items,
    ),
    PostureControl(
        key="audit_surfaces",
        label="SEL audit logging",
        unit="session-key surfaces",
        summary=(
            "Append-only, HMAC-chained security event log; the chain is verifiable "
            "for tampering. Redaction is applied on the forward path, and on-disk "
            "records are redacted by each caller before `log`. Listed below are the "
            "surfaces SEL infers from a session key; a call site may also stamp a "
            "more specific source of its own, so this is a floor, not a total."
        ),
        source="src/kiro_crew/sel.py",
        items_fn=_audit_surface_items,
    ),
    PostureControl(
        key="token_auth",
        label="Dashboard token auth",
        unit="auth controls",
        summary="Layered controls on every authenticated dashboard request.",
        source="src/kiro_crew/dashboard/token_auth.py",
        items_fn=_token_auth_items,
    ),
)


def _control_payload(control: PostureControl) -> dict:
    """Serialize one control, degrading to a count-less entry on failure.

    A control whose ``items_fn`` raises must not take down the whole posture
    response — the panel should show the rest of the posture and mark this one
    unavailable, since this endpoint is purely informational.
    """
    try:
        items = control.items_fn()
    except Exception:
        logger.warning("Security posture: items unavailable for %r", control.key, exc_info=True)
        return {
            "key": control.key,
            "label": control.label,
            "unit": control.unit,
            "summary": control.summary,
            "source": control.source,
            "count": None,
            "items": [],
            "unavailable": True,
        }
    return {
        "key": control.key,
        "label": control.label,
        "unit": control.unit,
        "summary": control.summary,
        "source": control.source,
        "count": len(items),
        "items": [{"label": i.label, "detail": i.detail} for i in items],
        "unavailable": False,
    }


def build_posture_snapshot() -> dict:
    """Build the full security-posture detail payload.

    Blocking: ``_denied_command_items`` reads the built-in rule table (in-memory)
    but the caller may also fold in the denied-commands snapshot, which touches
    the filesystem — see ``build_posture_snapshot_async``.
    """
    controls = [_control_payload(c) for c in _CONTROLS]
    return {
        "controls": controls,
        "counts": {c["key"]: c["count"] for c in controls},
    }


def posture_counts() -> dict[str, int | None]:
    """Just the ``key → count`` map, without materializing the item lists.

    For a counts-only caller (``/api/security/stats``) building and serializing the
    full ~45 KB item payload to return three integers is pure waste. Still resolves
    every ``items_fn`` — the count IS ``len(items)``, which is the invariant that
    keeps a count honest — it simply does not carry the items back.
    """
    return {c["key"]: c["count"] for c in (_control_payload(c) for c in _CONTROLS)}


async def posture_counts_async() -> dict[str, int | None]:
    """``posture_counts`` off the event loop (see ``build_posture_snapshot_async``)."""
    return await asyncio.get_running_loop().run_in_executor(governance_executor(), posture_counts)


async def build_posture_snapshot_async() -> dict:
    """Build the snapshot off the event loop.

    ``_tool_schema_items`` walks the validation module and the denied-rule table
    walk is O(137); neither blocks on I/O today, but this is a read-only
    informational endpoint and keeping it off the loop costs nothing while making
    a future filesystem-backed control safe to add.

    Offloaded to the dedicated ``governance_executor`` (``mc-gov``) — NOT the
    shared default pool — for the same reason its sibling
    ``build_governance_policy_snapshot_async`` is: this GET is
    browser-triggerable, so the moment a control DOES read the filesystem (the
    case the paragraph above exists to keep safe), default-pool I/O would
    contend with the workers the event loop shares for DNS. Choosing the right
    pool now means adding that control is a one-line change, not a latent stall.
    """
    return await asyncio.get_running_loop().run_in_executor(
        governance_executor(), build_posture_snapshot
    )
