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
        "CLI wheel-update failures",
        "cli_server.py",
        "The failure text `kirocrew update` prints when a managed-venv shadow "
        "update fails, plus the fallback installer one-liner printed next to it. "
        "The engine's error quotes the URL it tried and the fallback command "
        "embeds the artifact CDN base -- with a token-bearing "
        "KIROCREW_CDN_BASE either would land credentials in terminal "
        "scrollback and shell history, so both strings run the shared "
        "credential + exfiltration-URL chain immediately before print.",
    ),
    (
        "Central policy fetch failures",
        "platform/policy_distribution.py",
        "The failure text of a central governance-policy fetch, which reaches TWO "
        "surfaces: `kirocrew policy fetch` on stdout and a `logger` line (durable, "
        "and served to the dashboard by `GET /api/logs`). The bytes are not ours -- a "
        "malformed document reaches the message through a parser error, and `json`'s "
        "errors quote the offending text, so an endpoint that echoes back the "
        "request's `Authorization` header (its own request reflected, a proxy error "
        "page) would carry that credential to both surfaces. Every detail goes "
        "through `_sanitize_detail`, which applies the shared credential + "
        "exfiltration-URL chain and additionally elides the operator-configured "
        "source URL and its bare hostname.",
    ),
    (
        "AutoNudge monitor responses",
        "dashboard/handlers/autonudge.py",
        "Structured monitor state served by the AutoNudge dashboard endpoints. "
        "Provider observations are arbitrary nested JSON and can quote credentials "
        "or exfiltration URLs, so every string key and value in the monitor mapping "
        "passes through the platform-aware redaction chain before the response leaves "
        "the backend.",
    ),
    (
        "AWS identity-probe failures",
        "aws_consent.py",
        "The stderr of a failed `aws sts get-caller-identity`, run to show the "
        "operator which account a paid AWS service would bill before they confirm "
        "it. The text reaches TWO surfaces: the Settings > Voice consent card "
        "(`identityDetail` over `GET /api/aws/consent`) and `kirocrew aws-consent "
        "show` on stdout. The CLI quotes back what it was resolving, so a failure "
        "can carry a `credential_process` command line, an SSO start URL, or a "
        "role ARN, and an endpoint override can carry an inline-credential URL -- "
        "so the first stderr line goes through the shared credential + "
        "exfiltration-URL chain at the source, before it is put on an `Identity`.",
    ),
    (
        "Event backfill report samples",
        "events/backfill.py",
        "One serialized sample event per kind in the backfill validator's "
        "report, printed to CLI stdout. Store fields folded into samples "
        "(subagent task previews, cron names) can carry tokens the operator "
        "pasted into a task, so each sample passes the shared two-pass "
        "redaction before leaving the process.",
    ),
    (
        "Browser CLI install failures",
        "browser_cli/install.py",
        "The stderr of a failed `npm install -g @playwright/cli` / browser download, "
        "which reaches TWO surfaces: a `logger.warning` line (durable, and pasted "
        "into bug reports via `kirocrew logs`) and the Settings > Browser error card. "
        "npm quotes the command's own environment back on failure, so the text can "
        "carry a registry `_authToken`, an inline-credential proxy URL, or a "
        "`*_TOKEN=` echo -- shapes the shared credential family does NOT match, so "
        "this sink adds its own npm patterns on top of the shared two-pass and "
        "redacts at the source rather than at either boundary.",
    ),
    (
        "Azure DevOps comment bodies",
        "apps/builtins/issue_radar/backend/azure_client.py",
        "The text Issue Radar posts as a work-item or pull-request comment on Azure "
        "DevOps. A comment body is frequently model-authored -- a crew's reply, or an "
        "AI summary the user accepted -- and publishing it is irreversible: it lands "
        "somewhere public and permanent on the customer's own organization, so a "
        "credential or an exfiltration URL cannot be walked back. `_comment_text` "
        "therefore runs the two-pass chain at the client, immediately before the body "
        "reaches `az devops invoke`, rather than trusting each caller to have "
        "redacted; the crew path already redacts and loses nothing, because both "
        "passes are idempotent.",
    ),
    (
        "Off-host backup uploads (opt-in)",
        "snapshot_redact.py",
        "The copy of a backup bundle that leaves the machine for object storage. The "
        "destination is not this module's business: the AWS Control app owns the bucket, "
        "its hardening, the consent grant and the transport, and refuses to send at all "
        "if that posture is missing or unreadable. What lives here is the one thing a "
        "hardened destination does not do -- rewriting the bytes that leave -- because a "
        "private bucket still holds whatever was put in it, and the operator's own "
        "account is not the only place a restored bundle can end up. An operator opts IN "
        "to rewriting the outbound copy through the credential + exfiltration-URL chain; "
        "it is OFF by default because substituting a tag changes length, which "
        "invalidates any format that depends on byte offsets. When it is on, databases "
        "are rewritten value-by-value through SQL rather than over their bytes, and every "
        "outbound database is rebuilt so no old value survives in page slack. A pass that "
        "cannot complete refuses the send rather than falling through to an unredacted "
        "one. The LOCAL archive is never redacted either way: it does not cross the "
        "boundary, and rewriting it would damage the only copy that restores complete.",
    ),
    (
        "Off-host backup pushes (the boundary itself)",
        "apps/builtins/aws_control/backend/backup.py",
        "Where a bundle actually leaves the machine. This module runs no scanner itself: "
        "the only redaction it performs is the call into `snapshot.prepare_redacted_copy`, "
        "which runs the full credential + exfiltration-URL chain in `snapshot_redact.py` "
        "(the row above). It is named here anyway because the call ORDER is the control -- "
        "redaction runs before the upload is authorized and before any bytes reach the "
        "transport, so a redaction that raises stops the push instead of letting it "
        "proceed unredacted, and this module pushes whatever the seam returns: the "
        "redacted copy when the operator opted in, the original when they did not. "
        "Reversing those two steps would leave the guarantee intact on paper while "
        "sending the secrets anyway, which is why the boundary gets its own row rather "
        "than being left implicit in the module that computes the copy.",
    ),
    (
        "Session intent summaries",
        "session_summary.py",
        "Intent-summary payloads persisted to the `.intents` sidecar and served by "
        "`GET /api/chat/slots/{slot}/summary`. The payload is model output derived "
        "from transcript text, so a secret or beacon URL pasted into the chat can be "
        "reproduced inside it; `normalize_payload` runs the whole nested payload "
        "through the credential + exfiltration-URL chain before the write, because "
        "the sidecar is durable and read straight back to the panel.",
    ),
    (
        "Cross-session turn delivery",
        "dashboard/chat_delivery.py",
        "The text a steer or a queued message carries into a turn, on the path "
        "`POST /api/chat` uses. Two boundaries in one call: the text is persisted "
        "into the slot's transcript and broadcast to every connected browser as a "
        "`steer_push` / `queue_push` card, so the scan happens here, before either "
        "boundary.",
    ),
    (
        "Session control read",
        "dashboard/session_control.py",
        "Another session's transcript tail, served by "
        "`GET /api/session-control/read` to the calling agent. Conversation "
        "content read off a live slot, so it can carry a credential a tool "
        "printed — the same output-boundary reason as the session-storage inventory "
        "below, with the reader being an LLM rather than the browser.",
    ),
    (
        "Session storage inventory",
        "dashboard/handlers/session_storage.py",
        "A session's title and its first message, served by "
        "`GET /api/system/session-storage/sessions` and its per-row detail. Both are "
        "conversation content read straight off a transcript, so either can carry a "
        "key someone pasted into a chat — the same output-boundary reason as the "
        "session-memory titles below.",
    ),
    (
        "Workflow management responses",
        "dashboard/handlers/workflows.py",
        "Authored workflow source, reusable definitions, revision lineage, and run output "
        "served to the Agent Capabilities panel or returned through the workflow MCP tools. "
        "These values can contain model-authored text or tool results, so every nested key and "
        "value passes through the credential + exfiltration-URL chain before egress.",
    ),
    (
        "Auto-nudge loop inventory",
        "dashboard/handlers/autonudge.py",
        "A nudge loop's own text fields -- its `message` and `stopped_reason` -- "
        "served by `GET /api/autonudge` and its per-loop detail, and echoed back on the "
        "`POST` and `PATCH` responses. `message` is the instruction re-delivered to the "
        "model every cycle, and three producers reach `svc.add` without passing through the "
        "arming authorizer (the goal loop, auto-research, and issue-radar, the last "
        "composing its text from external issue bodies), while a hand-edited "
        "`autonudge.json` bypasses that authorizer too -- so the stored value can be text "
        "nothing has scanned. `_serialize` therefore runs the credential + "
        "exfiltration-URL chain over every string it returns except the `id` and `slot_key` "
        "the client uses to address the loop, which must survive verbatim or the row cannot "
        "be acted on.",
    ),
    (
        "Chat pin previews",
        "dashboard/chat_pins.py",
        "Message previews submitted to POST /api/chat/pins are persisted to "
        "chat_pins.json and re-rendered by the pinned-messages panel, so the "
        "preview is an output boundary; credentials and exfiltration URLs are "
        "redacted before storage and on every response path (list and "
        "idempotent duplicate-create) via _redacted_pin.",
    ),
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
        "Session transfer bundle",
        "dashboard/session_transfer.py",
        "Transcript content copied to another Kiro Crew instance over an Instances "
        "tunnel. The bundle LEAVES this host, so it is an output boundary: a "
        "transcript written before the redactors existed (or carried in from a "
        "channel) can still hold a raw credential on disk, and relying on the "
        "receiving instance to scrub it would send the secret across the boundary "
        "first. This covers **Layer A only** — the display transcript, plus the "
        "title and origin label. The Layer B kiro-cli context is deliberately "
        "forwarded BYTE-EXACT and is NOT scrubbed: its thinking blocks carry a "
        "provider signature over their own content, so any rewrite invalidates the "
        "conversation and the peer's next turn is rejected (measured: a leaf-string "
        "pass altered a signature in 41% of one developer machine's 704 sessions). "
        "Redacting that artifact and transplanting it are mutually exclusive. What "
        "bounds the exposure is the destination rather than the payload — a send "
        "goes to the OPERATOR'S OWN peer instance over a tunnel they authenticated, "
        "and the peer stores it 0600 — so Layer B never leaves the operator's own "
        "trust boundary. Inbound Layer B is validated structurally (parse-only, "
        "never rewritten) and refused whole if any record does not parse.",
    ),
    (
        "Federated session search",
        "dashboard/handlers_instances.py",
        "Rows returned by GET /api/instances/search-sessions, straight to the "
        "browser. Two distinct inputs make it an output boundary of its own: "
        "PEER rows are untrusted remote JSON (allowlist-reshaped, then title/"
        "snippet re-redacted locally — the peer claims to have scrubbed, this "
        "hub does not take its word for it), and LOCAL rows come from "
        "conversation_log.search_sessions directly, bypassing the "
        "/api/sessions/search handler where the local redaction normally runs, "
        "so the same title/snippet scrub is applied here.",
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
        "`commit.py` builds its `error` from git stderr — which quotes refs, paths and "
        "whatever a repository's own hooks printed — scrubbed at that source "
        "(redact-then-bound) so its character bound can never cut a credential mid-match. "
        "That was "
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
        "Session detach notice",
        "dashboard/state.py",
        "The notice sent to a channel whose session-resume binding was cleared — a "
        "separate egress boundary from the browser payload, and its session title is "
        "user-controlled, so the rendered notice is re-scanned through the shared "
        "display_safe sink before it reaches the transport.",
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
        "Auto-skill update merge prompt",
        "history_consolidation.py",
        "The existing live skill body is redacted before it enters the model prompt "
        "that merges a proposed update, and the model's merged output is scanned again.",
    ),
    (
        "Auto-skill pending candidate staging",
        "history_consolidation.py",
        "LLM-authored descriptions, triggers, procedures, and generated scripts are "
        "redacted before SkillsLoader writes a pending create or update candidate.",
    ),
    (
        "Auto-skill direct publication",
        "history_consolidation.py",
        "LLM-authored descriptions, triggers, and procedures are redacted before "
        "immediate live skill creation or direct refinement through SkillsLoader.",
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
        "Job names, cron results, and error strings before they reach a channel. Also "
        "the auto-nudge loop's `message` before the `autonudge_state` broadcast reaches "
        "every connected dashboard client -- a distinct egress from the channel posts, "
        "and one a producer that bypasses the arm-time authorizer can reach.",
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
        "Structured monitor wakes",
        "monitoring/controller.py",
        "Provider-controlled canonical facts and operator-authored wake instructions "
        "before they are injected into an agent session. The complete bounded envelope "
        "passes the exfiltration-URL and credential scanners before dispatch.",
    ),
    (
        "Crew Mode delivery",
        "crew_chat.py",
        "Every crew-slot post (`_post`): forwarded subagent summaries/errors, "
        "decision-agent questions, and topic-meta renders — all LLM-authored — "
        "written to the transcript, broadcast over WS, and persisted to the "
        "conversation log.",
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
        "Hook auto-replies (shared channel pipeline)",
        "messaging/dispatch.py",
        "A user-defined `on_message` hook can answer a turn instead of the model, "
        "which SHORT-CIRCUITS the turn and so never reaches the redactor in "
        "messaging/driver.py that every other reply on this pipeline inherits. "
        "The hook's text is arbitrary (it is user code, and it may quote the "
        "inbound message or a command's output back), so it goes through the "
        "shared credential + exfiltration-URL chain here, at the one point the "
        "reply leaves for a channel. This module also carries a NON-egress site, "
        "the session-directive consumer's confirmation log line, which scrubs the "
        "same LLM-derived text before it reaches the gateway log; it is named here "
        "rather than allowlisted separately because a module gets one "
        "classification and the egress one is the load-bearing half.",
    ),
    (
        "Outbound raster payloads",
        "messaging/outbound_files.py",
        "Exact raster bytes pass both credential and exfiltration-URL scanners " "before upload.",
    ),
    (
        "Discord direct send",
        "discord/transport_dispatch.py",
        "The direct-send path that bypasses TurnDriver, redacted independently.",
    ),
    (
        "Discord transformed image text",
        "discord/renderer.py",
        "Image-markup removal can join previously separated text into a credential; "
        "the transformed body is re-scanned before Discord receives it.",
    ),
    (
        "Teams rendered message text",
        "teams/renderer.py",
        "Every LLM-authored string this channel sends, re-scanned in the form Teams "
        "will RENDER rather than the bytes handed over: the answer body, an "
        "`[OPTIONS:]` chip label, the tool-progress bubble, and an approval card's "
        "tool title and purpose. Teams markdown-renders all of them, so markup the "
        "platform removes can rejoin a credential the TurnDriver's byte-level stream "
        "scan saw as broken -- `AKIA**...**` is two fragments to the scanner and one "
        "key on screen. Card text matters as much as message text here, because a "
        "card's TextBlock is rendered the same way.",
    ),
    (
        "Teams attachment name",
        "teams/attachments.py",
        "The display name on an inline image, whose two sources are BOTH "
        "LLM-authored: the markdown alt text and, when that is empty, the "
        "basename of the model-written path. Extraction has already cut the path "
        "out of the answer body, so for an empty caption this name is the only "
        "surviving sink -- and the sanitizer preserves `[A-Za-z0-9._-]`, every "
        "character an `AKIA...` key id or a `ghp_...` token needs. Both the source "
        "and the finished name are scanned, because the length cap can slice a "
        "token down to a prefix the scanner no longer matches.",
    ),
    (
        "Telegram rendered widget text",
        "telegram/renderer.py",
        "The two LLM-authored strings this channel renders outside the answer "
        "body: an approval prompt's tool title (sent as markdown) and every "
        "`[OPTIONS:]` inline-keyboard label. Both are rendered, so markup the "
        "platform removes can rejoin a credential the TurnDriver's byte-level "
        "stream scan saw as broken. Scanned at the one keyboard builder both "
        "callers pass through.",
    ),
    (
        "Discord attachment description",
        "discord/client.py",
        "The accessible description on an uploaded image, from its markdown alt "
        "text. Extraction unescapes that text before the wire, reassembling a "
        "backslash-escaped credential the TurnDriver's stream scan never saw "
        "contiguously -- it is re-scanned here, on the form that actually leaves.",
    ),
    (
        "Telegram failure reason",
        "telegram/transport_dispatch.py",
        "The bounded failure reason a permanent AcpError surfaces in the chat "
        "reply instead of the generic retry text. The message is backend error "
        "text rather than stream output, so it bypasses the shared TurnDriver "
        "redaction and is scanned (credentials, exfiltration URLs, local "
        "paths) at this egress before the renderer posts it.",
    ),
    (
        "Telegram rendered-form seal",
        "telegram/renderer.py",
        "Every text this channel posts, scanned against the form Telegram RENDERS "
        "rather than the bytes sent. The TurnDriver's stream scan runs before this "
        "renderer introduces any markup, so it cannot see a credential the markup "
        "then reassembles: `AKIA**...**` matches nothing at the byte level, and "
        "`_md_to_telegram_html` turns it into `AKIA<b>...</b>`, which the client "
        "displays as an intact key. A link and a zero-width character between the "
        "halves do the same. Both live-frame and seal run "
        "`display_safety.redact_for_display` ahead of any tag being introduced, "
        "covering the HTML, Rich-Message and plaintext branches at once; the "
        "reasoning blockquote and the restored upload markup run it too.",
    ),
    (
        "Recent-sessions read audit",
        "messaging/sessions_view.py",
        "The exception text recorded when the collector fails to read the sessions "
        "directory, which goes into a SEL audit record every surface's session list "
        "shares. The message is filesystem error text and can quote a path or an "
        "environment value, so it is redacted before truncation — truncating first "
        "would let a cut split a credential pattern out of the matcher's reach.",
    ),
    (
        "LLM-generated session title",
        "messaging/auto_title.py",
        "The 3-6 word title a background turn proposes for a session. The model "
        "writes it FROM the conversation, so it can quote a credential or a beacon "
        "URL the user pasted, and the title then lands in three places at once — "
        "the conversation log, the channel's own thread/chat title, and the "
        "dashboard sidebar. Redacted here, before the cap, because the cap is what "
        "would otherwise split a credential pattern out of the matcher's reach; and "
        "here rather than at each caller, because a title reaching one surface "
        "unredacted is the whole failure.",
    ),
    (
        "Channel keyword-command replies",
        "messaging/commands.py",
        "The reply text of the path-independent chat commands — a cron job's name, "
        "schedule and message, a subagent's task, a task-runner spec path and a "
        "start failure. Each is free-form text a user typed or the agent proposed, "
        "and every reply is posted to a channel AND persisted to the conversation "
        "log, so the credential + exfiltration-URL pair runs before the string "
        "leaves the module rather than at each channel's own boundary.",
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
        "Shared options-overflow sink",
        "messaging/renderer.py",
        "The one place LLM-authored [OPTIONS:] choices that do not fit a channel's "
        "interactive widget are written into the message BODY (format_overflow, "
        "reached from apply_options_cap on slack/telegram/discord). That crosses a "
        "boundary the widget path does not: a widget label is plain text, while the "
        "body is markdown-parsed, so a key split by a code span, emphasis or a "
        "Discord spoiler is broken to every byte-level scan -- including the "
        "TurnDriver's streaming redactor -- and whole on screen once the platform "
        "drops the delimiters. Choices are therefore redacted here in DISPLAY form "
        "(messaging/display_safety.py) BEFORE mention syntax is defanged, since the "
        "ZWSP insertion is itself a post-scan transformation. Enforced at this sink "
        "rather than per renderer for the same reason the max_buttons cap lives in "
        "shared code: a channel cannot forget what it does not call.",
    ),
    (
        "Webex persisted transcript rows",
        "webex/transport_dispatch.py",
        "The turn this channel writes to the conversation log, which is then served "
        "back to dashboard readers. The user side of that row can carry store-sourced "
        "text no ingress scan ever saw -- an auto-nudge goal composed from a "
        "hand-edited store, delivered here as a synthetic inbound whose single `text` "
        "field is both the prompt and the row. It is therefore redacted AT THE SINK, "
        "inside `_persist_turn`, which runs after the turn has completed: the prompt "
        "the model received is already consumed and cannot be rewritten, so the "
        "instruction survives verbatim while the persisted copy is scrubbed. The "
        "sibling channels that own a persister carry the same call at the same "
        "boundary, for two different reasons: `discord/transport_dispatch.py` can "
        "receive that synthetic inbound, while `telegram/transport_dispatch.py` cannot "
        "-- `binding_key_for` honours only `slack:`/`discord:`/`webex:`, so no nudge is "
        "ever armed on a telegram session and its scrub covers ordinary inbound user "
        "text on the same persisted-row egress.",
    ),
    (
        "Feishu persisted transcript rows",
        "feishu/transport_dispatch.py",
        "Same persisted-row egress as the Discord/Telegram/Webex persisters: the user side "
        "of the turn this channel writes to the conversation log is served back to dashboard "
        "readers. No nudge synthetic inbound reaches it -- `binding_key_for` honours only "
        "`slack:`/`discord:`/`webex:` -- so the scrub covers ordinary inbound user text, "
        "which is the same reader-facing sink and therefore the same rule.",
    ),
    (
        "Teams persisted transcript rows",
        "teams/transport_dispatch.py",
        "Same persisted-row egress as the sibling persisters: the user side of the logged "
        "turn is served back to dashboard readers, so it is scrubbed at the sink. No nudge "
        "is armed on a teams session, so this covers ordinary inbound user text.",
    ),
    (
        "WeCom persisted transcript rows",
        "wecom/transport_dispatch.py",
        "Same persisted-row egress as the sibling persisters: the user side of the logged "
        "turn is served back to dashboard readers, so it is scrubbed at the sink. No nudge "
        "is armed on a wecom session, so this covers ordinary inbound user text.",
    ),
    (
        "Weixin persisted transcript rows",
        "weixin/transport_dispatch.py",
        "Same persisted-row egress as the sibling persisters: the user side of the logged "
        "turn is served back to dashboard readers, so it is scrubbed at the sink. No nudge "
        "is armed on a weixin session, so this covers ordinary inbound user text.",
    ),
    (
        "WhatsApp persisted transcript rows",
        "whatsapp/transport_dispatch.py",
        "Same persisted-row egress as the sibling persisters: the user side of the logged "
        "turn is served back to dashboard readers, so it is scrubbed at the sink. No nudge "
        "is armed on a whatsapp session, so this covers ordinary inbound user text.",
    ),
    (
        "iMessage persisted transcript rows",
        "imessage/transport_dispatch.py",
        "Same persisted-row egress as the sibling persisters: the user side of the logged "
        "turn is served back to dashboard readers, so it is scrubbed at the sink. This "
        "module also calls `redact_handle` for gate-side log hygiene, which is why its "
        "siblings `imessage/client.py` and `imessage/transport.py` are non-egress; adding "
        "the transcript scrub here made THIS one a content sink, so it is registered "
        "rather than allowlisted. No nudge is armed on an imessage session, so the scrub "
        "covers ordinary inbound user text.",
    ),
    (
        "Slack persisted transcript rows",
        "slack/transport_dispatch.py",
        "The inbound user row this channel writes to the conversation log, served back to "
        "dashboard readers. Scrubbed for the PERSISTED copy only: the same `text` still "
        "reaches `build_message`, so the model prompt stays verbatim. This module's OTHER "
        "user rows do not call a redactor here -- they go through "
        "`save_conversation_turn`, which scrubs on behalf of all twelve of its Slack "
        "callers, so this entry covers the direct append and that helper covers the rest. "
        "The two assistant appends need no call: `_redact_at_write_boundary` already "
        "scrubs every role other than `user`, and that exemption is the gap being closed.",
    ),
    (
        "Webex delivery",
        "webex/renderer.py",
        "The Webex DELIVERY boundary. Webex renders markdown, so it reassembles a "
        "credential the driver's literal-byte scan of the provider stream could "
        "not match contiguously (`AKIA**IOSF**ODNN7EXAMPLE`, a link target broken "
        "by emphasis) -- and this channel adds two egress paths of its own that "
        "carry the same text: the numbered `[OPTIONS:]` fallback posted when an "
        "Adaptive Card send fails, and the local-path references restored when an "
        "upload fails. The final answer is therefore re-scanned through "
        "redact_for_display (messaging/display_safety.py) on the DISPLAYED form, "
        "with the same redactor pair TurnDriver streams through. Deliberately "
        "without the mention defang the shared `display_safe` adds: that "
        "neutralizes `@everyone`-style broadcast grammars, Webex has none, and "
        "this channel's allow-list is email addresses -- so defanging would mangle "
        "every address the agent legitimately prints.",
    ),
    (
        "iMessage delivery",
        "imessage/renderer.py",
        "The iMessage DELIVERY boundary. This channel is the one that collapses "
        "markdown itself -- `to_plaintext` in `delivery_text` -- because the "
        "surface renders no markup, so the driver's literal-byte scan of the "
        "provider stream runs BEFORE the transformation that can reassemble a "
        "credential it could not match (`**AKIA**IOSFODNN7EXAMPLE`, a link "
        "target broken by emphasis). `delivery_text` therefore re-scans the "
        "flattened form through redact_for_display (messaging/display_safety.py) "
        "with the same redactor pair TurnDriver streams through, which is the "
        "only form that actually ships. Elsewhere in this package "
        "`redact_handle` appears solely in log lines, which is why the sibling "
        "modules are listed as non-egress and this one is not.",
    ),
    (
        "WeCom reasoning blocks",
        "wecom/renderer.py",
        "The reasoning WeCom renders inside its native `<think></think>` block. "
        "`TurnDriver` redacts each thinking chunk, but with a plain per-chunk pass "
        "rather than the rolling `StreamRedactor` it uses for the answer -- so a "
        "credential split across two chunks passes both halves and is reconstituted "
        "by the renderer when it joins them for the frame. The assembled string "
        "therefore goes through the shared credential + exfiltration-URL chain at "
        "the send boundary, which is the only form that ships and also covers a "
        "credential that was never split. The ANSWER text needs no pass here: it "
        "reaches this renderer already through the driver's rolling redactor.",
    ),
    (
        "Slack attachment titles and filenames",
        "slack/files.py",
        "The two upload sinks that are NOT the message body: an attachment's "
        "title comes from LLM-authored alt text, and its filename from a local "
        "path the model chose. Both reach Slack as their own fields, so neither "
        "is covered by the body's render pipeline, and a filename is re-scanned "
        "AFTER sanitizing because collapsing the unsafe characters can rejoin a "
        "credential the original had broken up.",
    ),
    (
        "Typed-reply approval prompt",
        "messaging/approval.py",
        "The numbered tool-approval prompt for a channel with no interactive "
        "widget (max_buttons=0). Both fields it interpolates are AGENT-AUTHORED: "
        "the model chooses the tool name and writes the purpose, so either can "
        "carry a credential or an exfiltration URL, and the prompt puts them "
        "straight into a chat message. The screen runs in build_approval_prompt "
        "rather than at each channel's sink because that is why the helper is "
        "shared: a channel adopting the ladder inherits the guarantee instead of "
        "re-deriving it, and a caller that forgets it leaks on a security prompt. "
        "A channel that screens again at its own sink is merely redundant.",
    ),
    (
        "WhatsApp render pipeline",
        "whatsapp/renderer.py",
        "The WhatsApp RENDERING boundary. This is the only markup-CONSUMING "
        "channel whose own converter rewrites the delimiters: `to_whatsapp_text` "
        "turns `AKIA**I**OSFODNN7EXAMPLE` into `AKIA*I*OSFODNN7EXAMPLE`, which "
        "matches no credential pattern as written while the reader's client "
        "strips the markers and shows an intact key. It strips ANSI and then runs "
        "redact_for_display (messaging/display_safety.py) over the "
        "redact_exfiltration_urls + redact_credentials pair, and the pass that "
        "carries the guarantee is the one AFTER the conversion: the module also "
        "reduces `<thinking>` blocks, pipe tables and mermaid fences, each of "
        "which deletes a span and so joins whatever sat on either side of it, "
        "which a scan of the authored form cannot see. A second pass runs before "
        "the conversion as a belt. `render_chunks` runs the whole pipeline before "
        "the splitter, so a credential cannot be cut into an unmatchable prefix. "
        "`display_safe_text` is the same screen without the conversion, for the "
        "already-dialect sinks (a file-rejection note, an image caption, the "
        "approval prompt) that `whatsapp/turn_renderer.py` puts on the wire.",
    ),
    (
        "Weixin steer receipts",
        "weixin/turn_renderer.py",
        "The in-answer steer chip (`> \u21aa\ufe0f {summary}`). A steer arrives "
        "separately from the provider stream, so it never passes `TurnDriver`'s "
        "rolling redactor, and it is interpolated into the message BODY -- which "
        "the platform markdown-parses, so a credential split by a code span or "
        "emphasis is whole on screen while a byte-level scan saw it broken. The "
        "summary therefore goes through the shared DISPLAY-form redactor at the "
        "point of interpolation: `Renderer.redact_for_target`, which is only the "
        "`redact_for_display` pass -- it re-scans the markdown-rendered form with "
        "the credential + exfiltration-URL chain, and deliberately does not repeat "
        "the driver's rolling stream scan, which a one-shot summary has no split "
        "chunks to need. The ANSWER text needs no pass here at all: it reaches this "
        "renderer already redacted by the driver.",
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
        "MCP custom server specs",
        "dashboard/handlers/mcp_custom.py",
        "Editable MCP server specs returned by the dashboard HTTP API to the browser. "
        "Configured header values receive credential redaction only before they cross "
        "that boundary.",
    ),
    (
        "MCP probe results",
        "dashboard/handlers/mcp.py",
        "Cached MCP probe results returned by the dashboard HTTP API to the browser. "
        "Configured header values and reflected credentials in probe errors receive "
        "redaction before they cross that boundary.",
    ),
    (
        "MCP server metadata",
        "mcp_discovery.py",
        "McpServerInfo.to_dict() is the serialization boundary for every dashboard "
        "MCP listing; header values and reflected credentials in probe errors are "
        "redacted there before the payload leaves the backend.",
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
        "Connections L1 smoke report",
        "connections/l1_smoke.py",
        "The authorized-grant sweep's JSON verdict report: written to disk by "
        "`_persist_report`, echoed to stdout by `_echo`, and uploaded by the "
        "nightly lane as a build artifact — so it reaches CI logs and every "
        "reader of the repository, not just the operator who ran it. The "
        "credential-bearing part is the provider's own error text, scrubbed by "
        "`redact_mcp_error` as it enters a verdict row (both site-wide scanners "
        "plus the configured header-value pass), at the write rather than at a "
        "read, because the report outlives the process that made it.",
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
    (
        "Session-pulse survey feedback (Aperture egress)",
        "dashboard/handlers/feedback.py",
        "The free-text `feedback` field submitted via POST /api/feedback/submit is "
        "forwarded to Aperture, a third-party AWS service, so it is a genuine "
        "external egress boundary — a user typing a credential or exfiltration URL "
        "while describing their experience would otherwise leave the host "
        "unredacted. `_customer_responses` runs it through redact_exfiltration_urls "
        "then redact_credentials before it is included in the outbound payload. "
        "`email` is run through that SAME pass (redact_exfiltration_urls then "
        "redact_credentials) because a user could paste a credential into it; its "
        "`pii: True` marker is a separate Aperture disclosure flag, not a "
        "substitute for redaction. `rating` (a fixed frontend enum) is not run "
        "through this pass.",
    ),
    (
        "Auto Triage Pipeline dashboard strings",
        "apps/builtins/issue_radar/backend/pipeline_fold.py",
        "Every string this read-only fold hands to its routes -- issue titles, "
        "assignee and author logins, labels, event names, slot keys -- funnels "
        "through one `_printable` helper before serialization, and the routes render "
        "it in the dashboard. The titles and labels are NOT our text: they come from "
        "the forge, where any user can open an issue and write anything in the "
        "title, so this is a dashboard-bound sink for attacker-controlled text. "
        "`_printable` runs the shared credential + exfiltration-URL chain FIRST, "
        "then neutralizes control and bidirectional-override characters, and "
        "truncates LAST -- redaction has to precede truncation, because cutting "
        "first can split a credential so only its tail is left to match and the head "
        "survives into the output.",
    ),
    (
        "AWS Control error responses",
        "apps/builtins/aws_control/backend/routes.py",
        "Error text returned by the aws-control builtin's HTTP surface. The app "
        "shells out to the AWS CLI (via the deploy engine chokepoint), and a "
        "failed call's stderr can quote back a `credential_process` command "
        "line, an SSO URL, a role ARN, or an endpoint override carrying inline "
        "credentials. Every outbound error string (the `aws_call_failed` "
        "bodies, the bill card's `fetchError`, backup's `remoteError`, and the "
        "library `not_pushable` message) funnels through one `_safe_error` "
        "chokepoint that applies the deploy handlers' credential + "
        "exfiltration-URL chain before the text reaches the dashboard.",
    ),
    (
        "Job runtime error responses",
        "apps/job_routes.py",
        "Error text returned by the shared `_jobs/*` HTTP surface that every app's "
        "job runtime is served through. The SDK scrubs what a RUNNER produces, but "
        "the refusal this surface raises itself quotes caller-supplied material -- "
        "a job kind name, a dedupe key, or a path carried out of the SDK's own "
        "exception -- so the `job_start_failed` body funnels through one `_safe` "
        "chokepoint that applies the credential + exfiltration-URL chain before the "
        "text reaches the dashboard.",
    ),
    (
        "Relayed remote-crew turn output",
        "dashboard/remote_relay.py",
        "Every string in a turn relayed from a bound remote crew: the assistant "
        "text, and each mirrored frame's tool inputs and outputs. The peer is a "
        "SEPARATE machine with its own agents, environment and secrets, so its "
        "reply can quote a credential or an exfiltration URL that no local "
        "redaction pass has ever seen. The peer redacts its own copy with this "
        "same chain, but a hub that trusted that would inherit whatever an older "
        "or misconfigured peer failed to scrub -- so the boundary where the "
        "peer's bytes become this dashboard's transcript re-applies the "
        "credential + exfiltration-URL chain itself, before any broadcast.",
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
        # Drives the backup redaction pass and reports what it did, but applies no
        # redactor itself: the outbound bytes are rewritten in `snapshot_redact.py`,
        # which is the registered sink. What matches the call-site scan here are the
        # orchestration and reporting names (`_redacted_upload_copy`,
        # `_report_redaction`, `_report_redacted_bundle`).
        "snapshot.py",
        # Inbound / gate-side: redacts what comes IN or what a gate logs, not what
        # goes out to a human.
        "context.py",
        "agent.py",
        # Gate-side log hygiene: the update provider redacts an update command's
        # stderr before writing it to the gateway log. It is a boot-time
        # operational log line, not an output boundary bound for a human or a
        # third party — the redaction is defensive so a credential-bearing
        # installer error cannot leak into the log ring / /api/logs stream.
        "platform/update_provider.py",
        # Same shape: redacts the unparseable LLM decomposition response before
        # writing the diagnostic ERROR line to the gateway log. Defensive log
        # hygiene so a response echoing a credential or exfiltration URL cannot
        # leak into the log ring / /api/logs stream; not an egress boundary.
        "task_planner.py",
        # Capture-side, not egress: the per-session MCP report scrubs a server
        # name and a failing server's startup error as it RECORDS them, so a
        # credential never enters the accumulator at all. Deliberately earlier
        # than the boundary — redacting first is what stops a credential being
        # split across this module's own length cap and surviving in halves. The
        # surfaces that SHOW the report are the registered sinks (the dashboard
        # slot snapshot and the live stream).
        "acp/mcp_session_report.py",
        # Audit-side log hygiene: log_decline scrubs the model-authored tool
        # title before writing the shared auto_approve_declined SEL row. The
        # audit log is a gate-side record, not an output bound for a human or
        # a third party — the surfaces that SHOW a refusal (the dashboard's
        # notice line) are the registered sinks.
        "name_grant.py",
        # Internal coordinator partitions behind the single registered
        # ``subagent.py`` output boundary.  They redact lifecycle payloads before
        # handing them to facade-owned event/completion callbacks, but the split
        # adds no new transport or audience and therefore no additional posture
        # row.
        "subagent_manager/admission.py",
        "subagent_manager/continuation.py",
        "subagent_manager/monitoring.py",
        "subagent_manager/run.py",
        "subagent_manager/terminal.py",
        # The shared recursive redactor helper itself — a pure scrubber, not an
        # egress boundary; the modules that CALL it (mochi routes/hooks) are the
        # registered sinks.
        "apps/builtins/mochi/redact.py",
        # Shared model-fallback text builders (fallback_story_of /
        # annotate_model_fallback): scrub the chain-exhaustion story and the
        # fallback-served warning line ONCE, centrally, so every consumer gets
        # the same safe text. They own no output — the scrubbed text reaches a
        # human only through the delivering surfaces (the cron/heartbeat alert
        # paths in slack/gateway.py and the sub-agent completion path in
        # subagent.py), which are the registered sinks.
        "llm_helpers.py",
        # Redacts artifact names/metadata at the point they are STAGED (the
        # pushable list and the S3 meta sidecar), before any response exists.
        # It owns no output of its own — every HTTP response carrying that data
        # leaves through routes.py, the registered sink for this app.
        "apps/builtins/aws_control/backend/library.py",
        # Same shape: scrubs externally-authored S3 object keys/folder names
        # inside the listing helper. It owns no output — the listing reaches
        # the dashboard only through routes.py, the registered sink.
        "apps/builtins/aws_control/backend/storage.py",
        # Same shape: scrubs profile names and STS-derived account metadata as
        # the account snapshot is BUILT. It owns no output — the snapshot is
        # served only through routes.py, the registered sink for this app.
        "apps/builtins/aws_control/backend/accounts.py",
        # Same shape, one layer earlier: scrubs runner-supplied job payload as the
        # run record is BUILT and persisted. `_redact` covers `step`, `error` and
        # each progress line, and `_json_safe` recurses through nested structures
        # (dict KEYS as well as values) before `_persist` writes the record. It
        # owns no output -- the record reaches a human only through
        # apps/job_routes.py, the registered sink for this surface. The on-disk
        # file is not the egress boundary the panel counts.
        "apps/job_sdk.py",
        # Same shape: hosts _redact_memory_field, the shared recursive scrubber
        # for memory fields. It owns no output of its own — the handler modules
        # that call it (memory.py, cron.py) are the covered surfaces.
        "dashboard/handlers/_shared.py",
        # Same shape: applies a redactor the CALLER injects, to scan the form a
        # platform will actually render (markup collapsed, ANSI stripped). It owns
        # no output of its own -- the registered sinks are the modules that call
        # it (slack/format.py, messaging/renderer.py).
        "messaging/display_safety.py",
        # ``_load`` credential-scrubs a persisted ``banner`` and ``message`` in
        # memory and attempts to persist the masked value back, so a value
        # written to the store out-of-band (a hand-edited file, or a direct
        # ``AutoNudgeService.add`` that skips the authorizer) is masked on the next
        # load rather than served raw — best-effort, since a failed re-persist
        # leaves the raw value on disk until the following load. It owns no output
        # of its own — the egress that serves the banner is the registered
        # ``dashboard/handlers/autonudge.py`` serializer and the gateway broadcast;
        # this is at-rest sanitisation at the trust boundary where the store is
        # read, not an egress pass.
        "autonudge.py",
        # The other inbound edge: ``autonudge_authz`` scrubs a ``message`` arriving over
        # the API, and owns no output of its own.
        "autonudge_authz.py",
        # Gate-side log hygiene for a channel whose user identity IS a phone
        # number or an Apple Account email. ``redact_handle`` shortens a handle
        # before it reaches a gateway log line or a SEL ``caller`` field. None of
        # these modules writes message content to the user through this call.
        #
        # ``imessage/renderer.py`` is deliberately NOT in this list even though
        # it also calls ``redact_handle`` for a delivery-failure log line: it is
        # a real egress sink and is registered as one above. An earlier version
        # of this note claimed the renderer's redaction "is the shared
        # TurnDriver's and is already counted there" -- that was wrong, and it is
        # the kind of wrong that suppresses a gate. The driver scans the provider
        # stream as literal bytes; the renderer then flattens the markup, which
        # can reassemble a credential that scan could not see.
        "imessage/client.py",
        "imessage/transport.py",
        # The tool-permission prompt and its SEL record. Neither crosses a
        # machine boundary: the prompt is written to the operator's OWN terminal
        # in their own process, and the audit line goes to the local SEL log. The
        # scrubbing there is targeted hygiene — a credential the model echoed
        # into a tool title must not be copied into the audit record, and
        # `log_tool_invocation` does not redact for its callers — not an egress
        # pass. Local paths are deliberately NOT redacted in the prompt, because
        # seeing the real path is part of what the operator is consenting to.
        "cli_chat.py",
        "acp/_dispatch.py",
        "acp/client.py",
        # Redacts the tool title in the auto-rejected-permission WARNING (a
        # gate-side log line) and defers user-facing display to the routed
        # permission event, whose sinks are already registered.
        "acp/runtime.py",
        "acp/session_handle.py",
        "platform/defaults.py",
        "platform/interfaces.py",
        # Inbound sanitization: the browser MCP tool redacts UNTRUSTED native-panel
        # content (a page's text/console output) before it returns into the agent's
        # context. It scrubs what comes IN from an untrusted web page, not an output
        # bound for a third party -- so it is defensive input hygiene, not an egress
        # sink.
        "mcp_tools/browser.py",
        # Comparison-only: applies the redactors to compute a match identity and
        # discards the result. The two files being merged can hold the same
        # message with and without redaction, so a raw comparison would keep both
        # copies — nothing redacted here is ever written or shown.
        "channel_transcript_migration.py",
        # Comparison-only, same shape: the steer settler redacts BOTH the pending
        # text and the backend's echo purely to compute a match identity. The ACP
        # layer already redacted the echo on the way in, so comparing it against
        # raw pending text never matched and the consumed steer got requeued and
        # run twice. Nothing redacted here is written or shown — the ledger and
        # the transcript keep the original text.
        "dashboard/steer_settle.py",
        # DETECTOR, not a redactor: the pre-push content scan calls both scanners only
        # to COUNT findings and then refuses the push. It deliberately discards the
        # cleaned text — rewriting a code diff would corrupt the very fix the gate
        # proved — so it is not a redaction egress path. The push it guards is not an
        # output boundary this panel measures either: nothing reaches GitHub when the
        # scan hits, and the change degrades to the local queue instead. Lives in
        # push_policy because all three push paths share this one implementation.
        "apps/builtins/auto_improvement/spine/push_policy.py",
        # Source-side pre-pass, same shape as the aws_control scrubbers: pip's
        # stderr tail is scrubbed with redact_and_truncate at the point the
        # install-failure payload is BUILT, so the 200-char bound can never cut
        # a credential mid-match (a sliced fragment no longer matches the
        # credential regex, and the route's own redaction pass cannot catch
        # it). It owns no output — the payload reaches the dashboard only
        # through ``_handle_deps_install`` in routes.py, the registered sink
        # for this app.
        "apps/builtins/auto_improvement/backend/deps.py",
        # Same source-side pre-pass shape, for the app's git/gh subprocess stderr:
        # git echoes the remote's userinfo URL on an auth failure, and the fixed
        # character bound applied to each error string can cut a credential
        # mid-match, so the companion-aware redact_via_context runs where the
        # string is BUILT, before the bound.
        # None of these owns an output boundary:
        # - gate.py's scrubbed RuntimeError becomes the gate verdict detail, which
        #   travels to the ledger and the candidate detail — surfaces owned by the
        #   spine driver and the backend routes, this app's registered sinks.
        # - clone_setup.py's error strings reach the dashboard only through the
        #   backend routes (the registered sink for this app).
        # - profile.py's GateResult.detail follows the same ledger/candidate path as
        #   gate.py's verdicts.
        "apps/builtins/auto_improvement/backend/clone_setup.py",
        "apps/builtins/auto_improvement/profiles/github_repo/profile.py",
        "apps/builtins/auto_improvement/spine/gate.py",
        # Inbound: the crew worker's slot title is derived from an issue title,
        # which is untrusted text anyone who can open an issue wrote. It is
        # scrubbed before it becomes a slot title (and fails CLOSED to the slot
        # key if the redactors are unavailable), so this is inbound sanitisation
        # rather than an egress boundary — the slot title's user-visible surface
        # is already covered by the registered dashboard sinks.
        "apps/builtins/issue_radar/backend/crew_runtime.py",
        # Log/audit hygiene, not an egress boundary: strips ``user:password@`` from
        # an external registry's clone URL before it reaches the SEL credential-grant
        # record and the warning logs. The URL is index-supplied, so it can carry a
        # token; scrubbing it keeps the secret out of a persisted audit trail. It is
        # not an output bound for a human or a third party, and the value that DOES
        # reach a dashboard client (``GET /api/apps/registries``) is protected by
        # refusing a credential-bearing repo outright rather than by redacting it.
        "apps/registry.py",
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
        # Owner-driven components extracted from dashboard/state.py.  They
        # redact while staging or dispatching through the facade, but they do
        # not introduce new logical egress paths: the same DashboardState
        # boundaries were already represented by the facade's registered sink
        # rows.  Classifying the implementation files separately would make a
        # behavior-preserving decomposition change the public posture items and
        # count even though no new value crosses a process or persistence
        # boundary.
        "dashboard/interaction_coordinator.py",
        "dashboard/notification_coordinator.py",
        "dashboard/slot_projection.py",
        "dashboard/websocket_hub.py",
        # Pre-redacts follow-up items before handing to state.py's WS egress
        # (the registered sink); its own return string is re-redacted by
        # chat_runner before broadcast. Not itself an egress boundary.
        "dashboard/session_directive_apply.py",
        "dashboard/cron_inject.py",
        "dashboard/ws.py",
        "dashboard/server.py",
        "dashboard/handlers/sessions.py",
        # Roster last-message previews: same preview + redaction chain as
        # handlers/sessions.py, feeding the same dashboard HTTP surface the
        # registered sink already covers.
        "dashboard/handlers/members.py",
        "dashboard/handlers/artifacts.py",
        "dashboard/handlers/core.py",
        "dashboard/handlers/cron.py",
        "dashboard/handlers/discover.py",
        "dashboard/handlers/hooks.py",
        "dashboard/handlers/knowledge.py",
        "dashboard/handlers/memory.py",
        "dashboard/handlers/optimizer.py",
        "dashboard/handlers/prompts.py",
        "dashboard/handlers/source_providers.py",
        "dashboard/handlers/taskrunner.py",
        "dashboard/handlers/terminal.py",
        "dashboard/handlers/themes.py",
        "dashboard/handlers/updates.py",
        "dashboard/handlers/webapp_preview.py",
        "dashboard/handlers_project.py",
        "knowledge/agent_fetch.py",
        "knowledge/agent_source.py",
        "knowledge/artifact_ingest.py",
        "knowledge/ingestion.py",
        "workflows/library.py",
        "mcp_core.py",
        "mcp_cron.py",
        # Same class as mcp_core.py: an MCP stdio server redacts tool RESULTS and
        # agent-authored names before they are persisted or returned, but the
        # egress boundary itself is the transport the result crosses, not this
        # module.
        "mcp_dashboard.py",
        "mcp_gateway/backend.py",
        # The kirocrew-core tool handlers, moved out of mcp_core.py into their
        # domain modules. Same classification as mcp_core.py above for the same
        # reason: a tool result's user-visible surface is a registered sink
        # downstream, and these redact before returning to it. `learn.py` is
        # absent because it calls no redactor -- the allowlist is checked for
        # stale entries too.
        "mcp_tools/apps.py",
        "mcp_tools/artifacts.py",
        "mcp_tools/control.py",
        "mcp_tools/knowledge.py",
        "mcp_tools/messaging.py",
        "mcp_tools/sessions.py",
        "mcp_tools/skills.py",
        "mcp_tools/spawn.py",
        "mcp_tools/workflows.py",
        "workflows/agent_exec.py",
        "workflows/agent_pool.py",
        "workflows/runner.py",
        "workflows/store.py",
        "apps/event_bus.py",
        # Redacts agent progress text INBOUND, before it is persisted into the
        # app's own queue JSON (`/thread`). Because the stored copy is already
        # scrubbed, every later read of it — the panel's own `/queue`, and the
        # thread rendered beside a pin — serves clean data, so there is no
        # separate egress boundary to register. Three modules of this backend
        # match the call-site scan and the boundary between them adds no new
        # transport or audience: `server.py` owns the redactor pair itself and
        # the app's whole security-policy surface, `request_state.py` applies it
        # while BUILDING the panel-facing request and comment shapes, and
        # `http_api.py` runs the ingest pass and re-applies the same floor on
        # the read path it serves.
        "apps/builtins/design_tweak/backend/http_api.py",
        "apps/builtins/design_tweak/backend/request_state.py",
        "apps/builtins/design_tweak/backend/server.py",
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
        # Inbound provider sanitization: check identities are stripped of URLs and
        # credentials before they enter canonical monitor state. The controller's
        # later agent-session injection is the registered output boundary.
        "monitoring/github_pull_request.py",
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
        # Redacts CLI stderr as an AWSError message is BUILT, before any caller
        # exists to render it. It has to happen here rather than at the boundary:
        # this is also where that text is trimmed to a length cap, and trimming
        # first can cut a credential in half, after which no downstream redactor
        # can recognise the fragment. The surfaces that render the message
        # (deploy/handlers.py, the app route modules) are registered sinks and
        # redact again; these passes are idempotent.
        "deploy/engine.py",
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
        # `followup` scrubs every turn of a review's stored question history at
        # its read boundary: the reviewer can repeat a credential it read in the
        # diff, a tool title carries the arguments it was called with, and it can
        # write that file itself. `backend/routes` scrubs the reviewed pull
        # request's title before it becomes a chat session's name. Same
        # classification as their siblings — the app's own surface, rendered by
        # this app's panel, not a core egress path.
        "apps/builtins/code_review_sage/sage_lib/followup.py",
        "apps/builtins/code_review_sage/backend/routes.py",
        # Dev Fleet's redactor wrapper and the cohesive owners that apply it to
        # the app's own API/state/worktree surfaces. These were previously all
        # housed in server.py and retain the same non-core-egress classification.
        "apps/builtins/dev_fleet/runtime.py",
        "apps/builtins/dev_fleet/http_api.py",
        "apps/builtins/dev_fleet/fleet_state.py",
        "apps/builtins/dev_fleet/repository.py",
        "apps/builtins/dev_fleet/live.py",
        "apps/builtins/dev_fleet/worktree_ops.py",
        "apps/builtins/issue_radar/backend/routes.py",
        "apps/builtins/meetings/backend/domain/session.py",
        # Live translation redacts the MODEL's answer before writing it to the
        # meeting's translations.json. The source line was already redacted at
        # dispatch, so this covers only what a model reintroduced, and the
        # user-visible surface is the app's own translations route.
        "apps/builtins/meetings/backend/domain/translate.py",
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
        # A behavior-preserving decomposition of the Spec Builder app backend.
        # These modules still feed the same app-owned HTTP/dashboard surface; the
        # split adds no transport or audience and therefore no posture sink.
        "apps/builtins/spec_builder/backend/handlers.py",
        "apps/builtins/spec_builder/backend/parsers.py",
        "apps/builtins/spec_builder/backend/repository.py",
        "apps/builtins/spec_builder/backend/runtime.py",
        "apps/builtins/workflows/server.py",
        # Bundled dev-skill script: prints CI/review findings to a
        # developer terminal, not an agent-output egress path.
        "builtin_skills/kirocrew-dev/prepare-pr/scripts/pr_findings.py",
        # Same shape, one step further: a bundled dev-skill AUTHORING script that
        # an author runs from a shell to narrate a demo film. It scrubs the
        # author's own script text before handing it to the author's own cloud
        # speech account -- defensive hygiene on a line that would otherwise be
        # spoken aloud in a published video. The gateway neither imports nor runs
        # it, so it is not a path this product carries agent output over, and
        # counting it would inflate "egress paths covered" with a surface the
        # product does not have.
        "apps/builtins/dev_fleet/skills/feature-demo-recording/references/narrate.py",
        # Same shape, the other authored input: the compositor scrubs `brand.json`
        # prose before rendering it onto a slide, so a credential in a brand file is
        # not published in the film. Also not a path this product carries agent
        # output over -- the gateway neither imports nor runs it.
        "apps/builtins/dev_fleet/skills/feature-demo-recording/references/compose.py",
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
        # Log-level defensive scrubbing: strips vault secrets and Bearer tokens
        # from log output before it reaches the log ring. Not an egress boundary
        # itself — the log ring reader (/api/logs) is the registered sink.
        "log_redaction.py",
        # CLI bootstrap: the ``install_log_redaction()`` call in
        # ``_setup_cli_logging`` matches the drift-guard's redaction regex.
        # The CLI is not an egress boundary — it's an installer, not a sink.
        "cli.py",
        # Defensive scrub: hooks.py redacts stderr before storing in last_error
        # (an internal model field). The egress boundary is the dashboard API
        # handler that serializes hooks via to_dict() — already a registered sink.
        "hooks.py",
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
            detail=(
                "Reads allowed; the agent's file-edit tool cannot modify it when the "
                "call declares the ACP edit kind. Shell writes are not covered by "
                "this control"
            ),
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
    "MCP_DASHBOARD_SCHEMAS",
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
    "whatsapp": "WhatsApp messages, approvals, and owner-authorization decisions",
    "feishu": "Feishu messages, approvals, and owner-authorization decisions",
    "webex": "Webex messages, approvals, and owner-authorization decisions",
    "teams": "Microsoft Teams messages, approvals, and owner-authorization decisions",
    "imessage": "iMessage messages, approvals, and owner-authorization decisions",
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
        _pin_detail = (
            "Per-client: a session is bound to the client address — or the "
            "daemon-verified tailnet identity — that first used it"
        )

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
            "PreToolUse gate. Configurable below; policy-pinned rules cannot be turned "
            "off. The count is the SHIPPED catalogue -- the set actually enforced is "
            "this minus any rule disabled below, so it can be smaller."
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
            "MCP tool calls with a registered schema are checked for unicode "
            "normalization, length limits, enum allow-lists, and unknown-field "
            "rejection. Coverage is per tool, not universal: computer-use refuses an "
            "unregistered tool outright, while the core, cron and dashboard "
            "dispatchers pass one through unvalidated unless its own handler "
            "validates."
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
