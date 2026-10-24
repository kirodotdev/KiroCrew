"""The redaction egress paths of the dashboard, chat sessions and the gateway's own work.

The first part, in registry order, of ``security_posture._REDACTION_SINKS``: each row names
an output boundary, the module that redacts it, and how. ``security_posture_controls`` joins
it with ``security_posture_sinks_delivery`` in this order, and the Settings -> Security
panel lists the rows in that order. The two parts are a contiguous cut of the registry at
its first Slack row, each named for the surface most of its rows cover, so a few rows sit on
the other side of the cut. Append a new row to the end of the part whose surface it belongs to.

Part of :mod:`kiro_crew.security_posture`, the import path callers, tests and docs use: it
reaches every name here, and a patch through it reaches this module too
(:mod:`kiro_crew._facade_mirror`).
"""

from __future__ import annotations

_GATEWAY_SINKS: tuple[tuple[str, str, str], ...] = (
    (
        "Interrupted work in a mate reply",
        "dashboard/responsive_chat.py",
        "The bounded interrupted-turn reference passed to the next model prompt. "
        "Structural markers are neutralized, then exfiltration URLs and "
        "credentials are redacted before the reference enters model context.",
    ),
    (
        "The settings diagnosis",
        "dashboard/change_cards_settings.py",
        "Current and default values of every setting `diagnose_settings` reports, "
        "on their way to an agent's model context. A credential-like key is "
        "reduced to whether it is set, and every remaining string passes the "
        "credential redactor, because a setting value can be a token the operator "
        "pasted into a free-text field.",
    ),
    (
        "Change-card and guide rows in a conversation",
        "dashboard/chat_cards.py",
        "The title and result text of each change card and guide row written "
        "into a chat transcript and rendered in the browser. The text comes from "
        "an agent's proposal or a gateway outcome, so each string passes the "
        "exfiltration-URL then the credential redactor before it is stored.",
    ),
    (
        "A return-triggered warm task brief",
        "dashboard/warm_task_brief.py",
        "The original request and model-generated task facts written into a chat "
        "transcript and rendered in the browser. Each string passes the "
        "exfiltration-URL then the credential redactor before it is stored.",
    ),
    (
        "Change-card details shown before Apply",
        "change_card_catalog_plans.py",
        "Values a change card shows in full so the user sees what Apply runs: an "
        "MCP server's launch line, environment values and request headers. Each "
        "value passes the credential redactor before it reaches the card.",
    ),
    (
        "Mate goals",
        "member_goals.py",
        "The goals, key results and their readings a mate may write "
        "about itself, on their way to the Goals tab, the mate's tools and its "
        "saved goal reports. Each string passes the "
        "exfiltration-URL then the credential redactor before it leaves the record.",
    ),
    (
        "The symptom probes",
        "diagnose_probes.py",
        "Evidence strings the read-only `diagnose_settings` probes return to "
        "an agent's model context: log lines, job errors and file excerpts read "
        "from this host. Each string passes the credential redactor before it "
        "leaves the probe.",
    ),
    (
        "A remote crew's answer to one chat turn",
        "dashboard/handlers_crew_turn.py",
        "Everything a headless remote crew sends back for a turn, on its way to the "
        "caller and into a transcript: the streamed SSE events, a non-streamed JSON "
        "body, and the detail of a refusal the crew itself returned. The crew is a "
        "model with tools, holding its own Kiro identity and control secret in its "
        "environment, so a reply can quote a credential either because the model "
        "repeated its own environment or because content it fetched told it to -- the "
        "same reason the peer proxy redacts, and this route is a second way out of "
        "the same machine. The stream is reassembled into whole SSE EVENTS before "
        "each is scrubbed, because the socket splits wherever it likes and a "
        "credential straddling two chunks is invisible to a per-chunk pass. An event "
        "too large to scrub, or one the scrubber refuses, ends the stream instead of "
        "being forwarded; a non-streamed body that cannot be scrubbed is refused "
        "rather than returned.",
    ),
    (
        "Thread, GIL and loop-stall diagnostics",
        "diag/threads.py",
        "Python frames, folded stacks and loop-stall dump text, on their way to an "
        "operator through the debug read routes. Three things make this an egress "
        "path rather than an internal read. A frame label carries the absolute "
        "path of the file it came from, so it carries the operator's home "
        "directory and therefore their username; the loop-stall dumps are written "
        "by `faulthandler` from C, which cannot redact as it writes, so read time "
        "is the ONLY point at which they can be scrubbed at all; and a sampled "
        "frame can name a literal. Each string passes the shared path-shortening "
        "then the exfiltration-URL and credential chain, and the shortening runs "
        "FIRST because it is what removes the home prefix that the credential "
        "scanners do not look for. A scanner that raises drops the field rather "
        "than emitting it unredacted.",
    ),
    (
        "Fold values filled into a mate's dashboard page",
        "dashboard/handlers/member_dashboard.py",
        "Every string a template's fold paths resolve to, on its way into the page's "
        "own data island. A fold value is whatever a conductor wrote into the crew "
        "log, so a `session_ledger_record(goal=...)` carrying a pasted private key is "
        "rendered verbatim by any template that binds that fold -- and a mapping's "
        "KEYS are agent-authored on the same terms, an artifact name being a free "
        "string. This is the one step that hands fold values to a page's own script, "
        "so the pass sits here rather than in the feed, which the conductor also "
        "reads. Recursive over the resolved value rather than a named list of fields, "
        "because which fold reaches a page is a decision each TEMPLATE makes. A "
        "snapshot is taken from what the page holds, so it inherits this pass. The "
        "same traversal drops `worker_session_key` and empties a `bind` event's "
        "text, which covers the fields known to be secrets and says nothing about "
        "prose that happens to contain one.",
    ),
    (
        "Conductor work items shown on the Crew page",
        "dashboard/handlers/work_ledger_board.py",
        "Every string in one conductor's work-item board on its way to a browser: "
        "the item titles, each worker's own `summary`, the conductor's `decision`, "
        "and every artifact key and value. All are written by an AGENT and nothing "
        "between that write and this read inspects them, so a worker that pasted a "
        "token into its own status line would otherwise have it rendered verbatim "
        "on the page -- the masking that removes `worker_session_key` covers the one "
        "field known to be a secret and says nothing about prose that happens to "
        "contain one. The pass is recursive over the whole payload rather than a "
        "named list of prose fields, so a field added to `WorkItem` later is covered "
        "by default; it runs through `platform.context.redact_via_context`, so both "
        "scanners run in the shared order and a host with a loaded companion applies "
        "that companion's patterns too. Deliberately fail-closed: a composition "
        "error surfaces as a 500 rather than an un-redacted board.",
    ),
    (
        "Chat titles and kept models in the Settings follow-default confirm",
        "dashboard/chat_follow_default.py",
        "The titles of the first five chats the Settings follow-default count names, "
        "and the name of each model those chats keep, on their way to the confirm "
        "dialog in the browser. A title is LLM-written or the first words a person "
        "typed, and a closed chat's title and model are read straight from its "
        "transcript line, so nothing upstream of this answer inspects them. Each "
        "passes the dashboard's display redaction, `_redact_for_display`; a title "
        "does so BEFORE it is capped at 200 characters, because a cut taken first can "
        "split a credential into a stub the patterns no longer match. The raw model "
        "still drives the clear. Only chats the count already names contribute, and "
        "the route is owner-only.",
    ),
    (
        "Tool-call risk questions sent to the decision judge",
        "decisions/points/tool_risk.py",
        "The tool name, its arguments and the message excerpt that one `tool.risk` "
        "question carries to the third-party judge. Unlike its sibling "
        "`decisions/gate.py` -- which SCANS the same request and refuses the whole "
        "call on a hit, emitting nothing -- this module scrubs, and the scrubbed "
        "bytes are what leaves the machine: a credential in a tool argument is "
        "ordinary (an `aws` command, a curl header), so refusing on it would mean "
        "the seam never looks at the calls most worth looking at. Each field passes "
        "the shared exfiltration-URL then credential chain and is clipped only "
        "AFTER that, because clipping first can halve a secret into a fragment "
        "neither redactor matches; a scanner that raises drops the field instead of "
        "sending it. The gate then scans the placeholder, so a request it clears "
        "carries no unredacted byte.",
    ),
    (
        "Mid-turn handling decisions",
        "decisions/points/message_steer.py",
        "The running turn's own request and its newest assistant text and tool "
        "activity, sent to the third-party judge that decides whether a message "
        "typed into that turn interrupts it or waits for the next one. Unlike the "
        "gate above it, this module EMITS its redacted bytes: the cleaned excerpt "
        "is what leaves the machine, because refusing on it would turn 'the reply "
        "quoted an env file' into 'the seam stopped deciding'. Both scanners run, "
        "in the order credential-then-URL, over each half in FULL, and the clip to "
        "the consented budget is taken AFTER them -- a cut placed first can halve a "
        "secret, and a half is a fragment neither pattern matches, which is the "
        "same order `tool_risk.scrubbed` states; a scanner that fails yields the empty "
        "string rather than the input, and the gate's own scan still refuses the "
        "whole request for a spelling this pass missed. Nothing else about the "
        "turn is sent -- private reasoning is excluded at the read.",
    ),
    (
        "Compaction scoring sent to the decision judge",
        "decisions/points/compaction_keep.py",
        "A whole slot transcript sent to the third-party judge that scores, in the "
        "SHADOW, which of a session's tool calls an automatic compaction should have "
        "kept: the conversation text and every tool-call INPUT in it. Tool OUTPUT "
        "never leaves the machine at any fitting stage -- each result is replaced by "
        "its own character count (`result_placeholder`), which is the widest single "
        "omission in this table and is what keeps the largest half of a transcript "
        "local. Like the two points above it this module EMITS its redacted bytes "
        "rather than refusing on a hit: a credential in a tool argument is ordinary, "
        "and refusing would mean the measurement never runs on the transcripts most "
        "worth measuring. Both scanners run, credential-then-URL, over each field in "
        "FULL, and the stage's clip is taken AFTER them -- a cut placed first can "
        "halve a secret into a fragment neither pattern matches; a scanner that "
        "fails yields the empty string rather than the input, and the gate's own "
        "scan still refuses the whole request for a spelling this pass missed. "
        "Routed through `platform.context.redact_via_context` rather than the bare "
        "baseline pass, so a loaded companion's own credential and cookie spellings "
        "apply: this state is a whole transcript, and a baseline-only scrub would "
        "send a companion-defined secret verbatim to a third party. A composition "
        "failure re-raises out of the shim and DROPS the field here, which is the "
        "same direction every other arm of this module fails in. The shipped "
        "exfiltration-URL pass runs on top, because the shim covers credentials. "
        "Authorized by a THIRD keystone scope (`consent.STATE_KEY_COMPACTION`), "
        "because neither the message excerpt the main switch records nor the "
        "single-call `tool_args` scope describes it.",
    ),
    (
        "Recalled-memory questions sent to the decision judge",
        "decisions/points/memory_recall.py",
        "A snippet of each memory vector similarity recalled for this turn, sent to "
        "the third-party judge that decides which of them are worth their place in "
        "the prompt. Like the three points above it this module EMITS its redacted "
        "bytes rather than refusing: a recalled episode is text the agent wrote down "
        "turns or days ago, so refusing on a single episode that happens to quote an "
        "env file would stop the point firing on that store at all. Both scanners "
        "run over each snippet in FULL, credential then URL, and the clip to 200 "
        "characters is taken AFTER them -- a cut placed first can halve a secret into "
        "a fragment neither pattern matches, the same order `tool_risk.scrubbed` "
        "states; a scanner that fails yields the empty string rather than the input, "
        "and the gate's own scan still refuses the whole request for a spelling this "
        "pass missed.",
    ),
    (
        "Member capability editor responses",
        "agent_capabilities.py",
        "Owner-facing capability rows, Parent-change previews and impact summaries. "
        "safe_view applies redact_via_context before serialization and masks credential "
        "map values while preserving their structure. A crewmate's in-place editor "
        "(`mate_capabilities`) returns through the same safe_view. Retained secret "
        "values and source-content digests remain server-side.",
    ),
    (
        "Crew log entries",
        "crew_log/emit_text.py",
        "Message bodies written to the append-only per-session crew log under "
        "`<home>/crew-log/sessions/` -- what the user typed and what the model "
        "answered. This sink is a FILE rather than "
        "a response, so what it writes outlives the process and is read back "
        "later by folds and the session panel; that makes it an output boundary "
        "with a longer reach than an egress response, not a lesser one. Every "
        "body passes the shared exfiltration-URL then credential chain in the "
        "emitter rather than at its call sites, so a new call site cannot forget, "
        "and a redaction that fails writes the empty string instead of the input.",
    ),
    (
        "Subagent wait broadcasts",
        "subagent_manager/admission/waits.py",
        "The wait REASON on the `subagent_waiting` frame, which reaches every dashboard "
        "browser, the SSE relay and every app holding a `subagent:*` scope. It is the "
        "same provider- and store-authored prose the task routes scrub and the sibling "
        "`subagent_done` event redacts field by field, so it passes the shared "
        "exfiltration-URL + credential chain before it leaves the process.",
    ),
    (
        "Task queue panel responses",
        "dashboard/handlers/tasks.py",
        "Store-authored prose served by the Tasks & capacity routes: each task "
        "event's data payload and each row's wait reason, both of which carry a "
        "tool's or a provider's own error text verbatim. Every string, including "
        "the ones nested in dicts and lists, passes through the shared "
        "exfiltration-URL + credential chain before serialization; row "
        "identifiers are left intact because the panel keys its rows by them.",
    ),
    (
        "Memory recovery responses",
        "dashboard/handlers/memory_admin.py",
        "Retired episode text and supersession references, plus backup and restore "
        "failure details served to the memory recovery panel. These fields pass "
        "through the shared credential + exfiltration-URL chain before egress.",
    ),
    (
        "Memory record editor responses",
        "dashboard/handlers/memory_edit.py",
        "Record detail, correction previews, and bulk operation results served to "
        "the memory editor. Nested fields pass through the shared credential + "
        "exfiltration-URL chain before reaching the browser.",
    ),
    (
        "Member memory recall and copy responses",
        "dashboard/handlers/memory_member.py",
        "Selected facts, experiences, corrections, and owner-selected copy results "
        "returned to the dashboard or the memory_recall tool. Nested fields pass "
        "through the shared credential + exfiltration-URL chain before serialization.",
    ),
    (
        "Managed-venv update failures",
        "platform/wheel_apply.py",
        "The failure text a managed-venv shadow update shows an operator -- "
        "`kirocrew update`'s print, the dashboard's failed update step, the "
        "approve route's audit record -- plus the fallback installer one-liner "
        "printed next to it. The engine's error quotes the URL it tried and the "
        "fallback command embeds the artifact CDN base -- with a token-bearing "
        "KIROCREW_CDN_BASE either would land credentials in terminal scrollback, "
        "the dashboard or the audit chain, so both strings run the context "
        "redactor (redact_via_context: credentials and exfiltration URLs, plus a "
        "loaded companion's patterns) in full before any cap.",
    ),
    (
        "CLI update output",
        "cli_server_update.py",
        "What `kirocrew update` prints around a wheel update: the release-feed "
        "URL it is checking and the plain-pip in-place upgrade hint (its command "
        "or failure note). Both embed the CDN base -- with a userinfo-bearing "
        "KIROCREW_CDN_BASE either would land credentials in terminal scrollback, "
        "so each displayed string runs only the shared credential redactor "
        "(no exfiltration-URL pass: the value is the operator's own CDN base) "
        "immediately before print; the fetch still uses the real URL.",
    ),
    (
        "Central policy fetch failures",
        "platform/policy_distribution_boot.py",
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
        "Project-scan warning reasons",
        "project_scan.py",
        "The per-file warning strings the folder scanner returns "
        "(`CandidateTree.warnings`), which ride the scan/scaffold API responses "
        "into the dashboard's preview surface. A warning reason embeds the "
        "exception text of a failed parse, and a parser quotes the offending "
        "source line back -- tree content the user merely pointed at, so a "
        "malformed workspace declaration holding a credential would otherwise "
        "be echoed verbatim. Redacted at construction (`_warning_reason`), so "
        "no unredacted copy of the reason ever exists for a later consumer (a "
        "log line, a persisted report) to leak.",
    ),
    (
        "AWS identity-probe failures",
        "aws_consent.py",
        "The stderr of a failed `aws sts get-caller-identity`, run to show the "
        "operator which account a paid AWS service would bill before they confirm "
        "it. The text reaches the Settings > Voice consent card "
        "(`identityDetail` over `GET /api/aws/consent`), which is the only "
        "surface that carries it. The AWS CLI quotes back what it was resolving, "
        "so a failure "
        "can carry a `credential_process` command line, an SSO start URL, or a "
        "role ARN, and an endpoint override can carry an inline-credential URL -- "
        "so the first stderr line goes through the shared credential + "
        "exfiltration-URL chain at the source, before it is put on an `Identity`.",
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
        "Browser install job failure detail",
        "browser_cli/install_job.py",
        "The `error_detail` of the gateway's browser install job, returned by "
        "`GET /api/browser/install` and in every 409 `install_already_running` "
        "body. Failed-step output is redacted by `install._step` at the source; "
        "exception and error fallbacks are not. `bounded_detail` re-redacts every "
        "carrier on the FULL text before the 2000-character cut, using only "
        "`redact_install_output` (the shared two-pass plus npm patterns), so a "
        "pre-redaction cut cannot split a credential past its matching anchor.",
    ),
    (
        "Browser panel launch failures",
        "browser_cli/launcher.py",
        "The CLI's own words when the Browser panel's address bar could not open a "
        "page (`playwright-cli -s=panel-… open|goto` failing), shown VERBATIM in the "
        "panel's failure card; the gateway log gets only the session name and the "
        "exit code, never the text. The daemon's stack quotes its "
        "environment and the URL the human typed, so the text can carry a "
        "query-string credential or a `*_TOKEN=` echo; `_error_text` distills the "
        "message and runs the shared credential + exfiltration-URL chain on the "
        "kept lines BEFORE the cap, so a truncated head cannot leak what the tail "
        "would have matched.",
    ),
    (
        "Azure DevOps comment bodies",
        "apps/builtins/issue_radar/backend/azure_client_writes.py",
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
        "Automatic session status cards",
        "dashboard/card_lifecycle.py",
        "Bounded recent transcript messages sent to the background model and its "
        "HTML/text response served by GET /api/chat/slots/{slot}/dashboard-card. "
        "Both boundaries run credential and exfiltration-URL redaction before "
        "model input or cached publication; the owner-only GET never generates content.",
    ),
    (
        "Cross-session turn delivery",
        "dashboard/chat_delivery.py",
        "The text a steer or a queued message carries into a turn, on the path "
        "`POST /api/chat` uses. Two boundaries in one call: the text is persisted "
        "into the slot's transcript and broadcast to every connected browser as a "
        "`steer_push` / `queue_push` card, so the scan happens here, before either "
        "boundary, for every origin except the session's own human, whose text is "
        "shown as typed like an ordinary send's row.",
    ),
    (
        "Queued-message drop notice",
        "dashboard/session_control_parts/delivery_containment.py",
        "The excerpt of a queued message a drain dropped, posted back to the channel "
        "it was addressed from (`notify_channel_recipient_dropped`): conversation "
        "text leaving for an external channel, scanned for credentials and "
        "exfiltration URLs on the way out.",
    ),
    (
        "Session control read",
        "dashboard/session_control_parts/session_status.py",
        "Another session's transcript tail and its cached intent summary, served by "
        "`GET /api/session-control/read` and `GET /api/session-control/summary` to "
        "the calling agent. Conversation "
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
        "Peer live-session list",
        "dashboard/handlers_instances_peer.py",
        "Rows returned by GET /api/instances/{id}/chat-slots, straight to the "
        "browser's Sessions list. A second, distinct Instances boundary from "
        "the federated search above: these are a connected peer's OPEN session "
        "titles, and a title is MODEL-AUTHORED text produced on the other machine. "
        "The local half of that same list has its title scrubbed by "
        "dashboard/slot_projection.py before it renders, so a peer row forwarded "
        "as-sent would be the one row in a merged list whose text never met a "
        "redactor. Allowlist-reshaped to the fields the sidebar reads, then every "
        "string run through the peer-text sink (scrub before clamp, so a "
        "credential cannot survive by sitting past the length limit).",
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
        "apps/builtins/md_notebook/server_http.py",
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
        "apps/builtins/auto_improvement/spine/driver_commit.py",
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
        "raw value — so a credential-shaped argument would be echoed straight back to the "
        "model and into the SEL record unless scrubbed. The exception type and JSON-RPC "
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
        # execute; the row detail below states that consequence to the operator.
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
        "gates whether a polled GET may PROMOTE watchers (without it a promote would need no "
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
        "and `WatcherState.as_dict` beside it serves `target`/`title`/`lastNote`/"
        "`verdictReason`/`fixing` — all model- or pull-request-derived, and the watcher "
        "ingests PR text as untrusted input by design, so unscanned a credential-shaped "
        "`target` would reach the browser verbatim. The session "
        "records go through it too, because `save_session` merges the caller's patch and the "
        "stored `title` is built from a finding's target. So do the route ERROR bodies: "
        "`commit.py` builds its `error` from git stderr — which quotes refs, paths and "
        "whatever a repository's own hooks printed — scrubbed at that source "
        "(redact-then-bound) so its character bound can never cut a credential mid-match. "
        "A refused commit is surfaced at the finding row, which makes that error a live path "
        'to the browser, so all five `result.get("error")` responses plus the '
        "PR-status and draft bodies are scanned. "
        "Covers the TERMINAL ERROR field on the same response too "
        '(`_fail`): `f"{type(exc).__name__}: {exc}"` is scanned like the feed beside it, '
        "because an exception message routinely quotes what failed — a git url, a "
        "subprocess argv, a path — so a run dying on an agent-influenced value would carry "
        "it to the browser. The exception TYPE is composed in after redaction so "
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
        "dashboard/chat_turn/stream_chunks.py",
        "Per-chunk StreamRedactor on the chat_chunk WebSocket stream — withholds a "
        "trailing credential-class run so a secret split across chunks cannot cross "
        "the wire.",
    ),
    (
        "Dashboard thinking stream",
        "dashboard/chat_turn/stream_chunks.py",
        "Separate StreamRedactor for the ephemeral chat_thinking stream.",
    ),
    (
        "Dashboard final message",
        "dashboard/chat_turn/segment_flush.py",
        "Full-text redaction pass over the assembled assistant turn before it is " "finalized.",
    ),
    (
        "Dashboard slot snapshot",
        "dashboard/state_parts/slot_transcript.py",
        "Every message, tool input, tool output, and title in the slot payload sent "
        "to the browser.",
    ),
    (
        "Session detach notice",
        "dashboard/state_parts/session_events.py",
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
        "history_append.py",
        "Redacted before persistence, so a leaked credential is not written to disk "
        "and replayed into later context.",
    ),
    (
        "Auto-skill update merge prompt",
        "history_consolidation_skills.py",
        "The existing live skill body is redacted before it enters the model prompt "
        "that merges a proposed update, and the model's merged output is scanned again.",
    ),
    (
        "Auto-skill pending candidate staging",
        "history_consolidation_skills.py",
        "LLM-authored descriptions, triggers, procedures, and generated scripts are "
        "redacted before SkillsLoader writes a pending create or update candidate.",
    ),
    (
        "Auto-skill direct publication",
        "history_consolidation_skills.py",
        "LLM-authored descriptions, triggers, and procedures are redacted before "
        "immediate live skill creation or direct refinement through SkillsLoader.",
    ),
    (
        "Side-panel stream",
        "dashboard/handlers/side.py",
        "StreamRedactor mirroring the main chat for the side-question stream.",
    ),
    (
        "Reply-thread stream and store",
        "dashboard/chat_threads.py",
        "Three boundaries of a reply thread on a mate chat message: the "
        "StreamRedactor on the mate's live reply (as the side panel's), the "
        "thread envelope (parent, surrounding chat and prior replies, which "
        "kiro-cli persists into its own session file), and every stored reply "
        "and quoted parent on its way out of the sidecar to the browser.",
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
)
