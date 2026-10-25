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
  ceiling the agent is fenced from — see the posture-only snapshot in
  ``handlers/security_governance_policy.py``), or user data. Sensitive-path entries are the *blocklist* itself,
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

import importlib as _importlib
import sys as _sys
from typing import TYPE_CHECKING

from kiro_crew import _facade_mirror

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
        # Gate-side: the .crewmate reader runs `needs_redaction` on bundle text
        # only to REFUSE a file whose rules, crons or carried files hold a
        # credential-shaped string. It returns no redacted text.
        "crewmate_bundle.py",
        # Selects which redaction the OTLP host audit rows go through in a
        # process with no platform context (`_policy_redactor` returns the
        # companion's), and calls none itself. The rows are written by the SEL,
        # which is the sink.
        "metrics/provider.py",
        # The redaction cards' owner-only route (allowed hosts). It applies no
        # redactor; the call-site scan matches its
        # handler names (`api_redaction_allow_host`, ...).
        "dashboard/handlers/redaction.py",
        # Gate-side: the link-preview route runs both redactors on the URL it was
        # asked to fetch only to REFUSE it (`400 blocked_url`) when either would
        # change it, so a zero-click fetch never carries what redaction removes.
        # Nothing it returns is redacted text, so it is a gate, not an egress sink.
        "dashboard/handlers/link_meta.py",
        # Validator only: a dashboard package's model field NAME is run through the
        # redactor and the package is REFUSED when the output differs, because a
        # field name is served in the package's `fields` list and written into the
        # page's data island, where only VALUES are scrubbed. The redacted form is
        # used solely to name the offending field in the refusal, never emitted as
        # content, so this classifies a name rather than being an output boundary.
        "artifact_store/dashboard_package.py",
        # Drives the backup redaction pass and reports what it did, but applies no
        # redactor itself: the outbound bytes are rewritten in `snapshot_redact.py`,
        # which is the registered sink. What matches the call-site scan here are the
        # orchestration and reporting names (`_redacted_upload_copy`,
        # `_report_redaction`, `_report_redacted_bundle`).
        "snapshot.py",
        # Defines the shared external-text redactor used by output boundaries and
        # creation guards. It emits nothing itself.
        "external_text.py",
        # Consumes that redactor as a yes/no predicate, not as a rewrite:
        # `is_dispatchable_member_name` asks whether a stored Crew Member name
        # WOULD need redaction and refuses the pin when it would. Nothing is
        # emitted here; the chat routes that act on the answer are the sinks.
        "members_parts/member_space.py",
        "members_parts/self_edit.py",
        # Gate-side, like `link_meta.py` above: the UI-guide catalog asks whether
        # the redactor WOULD change a proposed crewmate name/goal or MCP command,
        # args or url, and REFUSES the guide when it would. Nothing it returns is
        # redacted text.
        "guide_catalog.py",
        # The .crewmate writer: a part whose text the redactor would change is left
        # out of the file and named in the confirm view, never written redacted.
        "crewmate_bundle_export.py",
        # Gate-side the same way: a change card's reason and parameters are refused
        # when the redactor WOULD change them, and nothing it returns is redacted.
        "dashboard/change_cards_records.py",
        # Inbound / gate-side: redacts what comes IN or what a gate logs, not what
        # goes out to a human.
        "context.py",
        "context_assembly/session_start.py",
        "agent_parts/spec_build.py",
        # Capture-side, not egress: redacts a Slack thread's first message as it
        # is READ from Slack, before the text is kept for the model's fenced
        # thread-context block and for the transcript's display-only notice row.
        # It owns no output of its own. The model prompt is built by context.py
        # and the transcript is shown through the dashboard routes, which are the
        # surfaces that carry the text onward.
        "slack/thread_parent.py",
        # Capture-side as well: redacts thread replies as they are READ from
        # Slack, for the model's fenced thread-replies block. No output of its own.
        "slack/thread_replies.py",
        # Gate-side audit hygiene: the tool gate clips and redacts the tool labels
        # and refusal reason of each permission decision before writing them to
        # the SEL audit log. That is a local audit record, not an output bound for
        # a human or a third party.
        "permission_floor.py",
        # Transfer-side log hygiene: both redact a URL down to scheme+host before it
        # reaches a gateway log line ("Downloading %s from %s", a fetch failure, a
        # manifest that overran its byte bound). The url is operator- or
        # manifest-supplied and its path or query can carry a token, so the redaction
        # keeps that out of the log ring and /api/logs. Neither module writes to a
        # human-bound or third-party output, so neither is an egress boundary.
        #
        # `download_to` also RETURNS its failure string with the url cut to
        # scheme+host and the exception reduced to its type, never `{exc}`. The
        # callers own where that string goes: Papyrus's Tectonic install puts it in
        # its install state and PPTX Maker's engine install in its provision log,
        # and each app's own routes hand that to the dashboard. Those two app
        # modules call no redactor themselves, so they are not listed.
        "asset_downloader.py",
        "feature_videos_manifest.py",
        # Same transfer-side hygiene for the KAS engine installer: a fetch failure
        # names the manifest url with its path and query cut. The message reaches
        # the operator's own terminal (``kirocrew kas install``) and the gateway
        # log, neither a third-party boundary.
        "acp/kas_bundle.py",
        # Gate-side log hygiene: the update provider redacts an update command's
        # stderr before writing it to the gateway log. It is a boot-time
        # operational log line, not an output boundary bound for a human or a
        # third party — the redaction is defensive so a credential-bearing
        # installer error cannot leak into the log ring / /api/logs stream.
        "platform/update_provider.py",
        # The managed-venv shadow engine, same shape: it redacts a build child's
        # stderr IN FULL before cutting it into an error message. The operator
        # surface is the registered "Managed-venv update failures" sink
        # (platform/wheel_apply.py); redacting at capture is what keeps a
        # credential from being split by the cut and surviving in halves.
        "platform/wheel_engine.py",
        "platform/wheel_engine_build_child.py",
        # Same shape: redacts the unparseable LLM decomposition response before
        # writing the diagnostic ERROR line to the gateway log. Defensive log
        # hygiene so a response echoing a credential or exfiltration URL cannot
        # leak into the log ring / /api/logs stream; not an egress boundary.
        "task_planner.py",
        # Gate-side log hygiene, same shape as update_provider: redacts pip's
        # stderr tail (an app requirements.txt can name an index URL carrying
        # credentials) before the provisioning failure is written to the
        # gateway log and to the app backend's own log file. Defensive
        # scrubbing at the point of capture, not an output boundary.
        "apps/backend_runtime/provisioning.py",
        # Log hygiene, same shape: the loop-stall enrichment formats the loop
        # thread's innermost frame with the diag frame redactor before writing
        # it to the stall WARNING in the gateway log, so the frame's absolute
        # path carries no home directory into the log ring / /api/logs stream.
        # It is an operational log line, not an output boundary of its own.
        "dashboard/stall_enrichment.py",
        # Builds a crewmate's "waiting on you" list from its own thread, redacted the
        # way the sidebar preview is. It returns data and serves nothing: the list
        # leaves through `dashboard/crewmate_feed.py` into the crewmate's crew log,
        # and is shown only through the Dashboard page's data island.
        "dashboard/member_waiting.py",
        # Redacts a crewmate's goal and milestone text the same way before appending
        # it as a snapshot to that crewmate's own crew log. It serves nothing; the
        # snapshot is shown only through the Dashboard page's data island.
        "dashboard/crewmate_feed.py",
        # Gate-side log hygiene, same shape: the declared-name spec scan redacts
        # an agent name and the spec filenames it read from the user-writable
        # agents directory before naming them in the twin-specs WARNING in the
        # gateway log, so a credential-shaped name never enters the log ring /
        # /api/logs stream. The parsed spec it returns is untouched; the surfaces
        # that emit text derived from it are the registered sinks.
        "agent_discovery_parts/spec_reads.py",
        # Capture-side, not egress: the per-session MCP report scrubs a server
        # name and a failing server's startup error as it RECORDS them, so a
        # credential never enters the accumulator at all. Deliberately earlier
        # than the boundary — redacting first is what stops a credential being
        # split across this module's own length cap and surviving in halves. The
        # surfaces that SHOW the report are the registered sinks (the dashboard
        # slot snapshot and the live stream).
        "acp/mcp_session_report.py",
        # Same shape: the workflow previewer scrubs the script-authored strings it
        # RETAINS (an agent label, a prompt fallback label, a phase title) inside
        # `_bounded`, before its own 120-char title cap. Redacting first is the
        # whole point -- the response-level redactors match a credential by its
        # full shape, so cutting first can leave a prefix no later pass
        # recognises. It owns no output: the plan reaches a client only through
        # dashboard/handlers/workflows.py, the registered sink for this surface,
        # and that handler is the previewer's sole caller.
        "workflows/preview.py",
        # Audit-side log hygiene: log_decline scrubs the model-authored tool
        # title before writing the shared auto_approve_declined SEL row. The
        # audit log is a gate-side record, not an output bound for a human or
        # a third party — the surfaces that SHOW a refusal (the dashboard's
        # notice line) are the registered sinks.
        "name_grant.py",
        # Same shape as name_grant: audit_decision scrubs the caller-supplied
        # detail (a refusal reason, a delivery description) before the 200-char
        # clip and before writing the file_delivery_consent SEL row. A gate-side
        # audit record, not an output bound for a human or a third party; the
        # consent card and the tool's own error string are the surfaces that
        # show a refusal, and those are owned by their registered sinks.
        "file_delivery_consent.py",
        # Detector, not redactor: the decision seam runs redact_credentials
        # AND redact_exfiltration_urls over the state it is about to send to a
        # third-party judge, then DISCARDS both cleaned strings and refuses the
        # whole call on any warning. So the module sits on an egress path but
        # never emits redacted bytes -- a hit means nothing is sent at all, and
        # the row records which scanner refused. Registering it as a redaction
        # sink would make the panel claim this path is covered BY redaction,
        # when what covers it is refusal; a partially redacted payload is still
        # derived from a credential, and a judge's answer over redacted state
        # would be logged as if it had judged the real thing.
        "decisions/gate.py",
        # Capture-side, not egress: the opt-in frame recorder scrubs a raw ACP
        # frame as it WRITES it to a local file, so a credential never lands in
        # a recording the operator may later commit to the replay corpus. There
        # is no transport and no audience -- the file is on the operator's own
        # machine, written only while KIROCREW_ACP_RECORD_FRAMES names a
        # directory, and the scrub is a floor under the hand review the corpus
        # README requires rather than a boundary's own guarantee. The surfaces
        # that SHOW frame-derived text are the registered sinks.
        "acp/_frame_record.py",
        # Internal coordinator partitions behind the single registered subagent
        # output boundary, ``subagent_parts/run_output.py``.  They redact lifecycle
        # payloads before handing them to facade-owned event/completion callbacks,
        # but the split adds no new transport or audience and therefore no
        # additional posture row.
        "subagent_manager/admission/gate.py",
        "subagent_manager/admission/pump.py",
        "subagent_manager/continuation.py",
        "subagent_manager/monitoring.py",
        "subagent_manager/run.py",
        "subagent_manager/run_lifecycle.py",
        "subagent_manager/run_turn.py",
        "subagent_manager/run_yields.py",
        "subagent_manager/terminal.py",
        # The other owners composed onto the ``subagent.py`` namespace hold moved
        # redactor calls; they add no transport or audience either.
        "subagent_parts/admission_surface.py",
        "subagent_parts/agent_policy.py",
        "subagent_parts/continuation_surface.py",
        "subagent_parts/monitor_surface.py",
        "subagent_parts/terminal_surface.py",
        # Internal partitions behind the single registered
        # ``apps/builtins/aws_control/backend/backup.py`` boundary, which keeps every
        # outbound archive and label PUT. ``egress_text`` defines the redaction sequence
        # the engine applies to exported conversation rows, published and foreign labels
        # and the recorded nightly failure text; ``conversations`` applies it to each
        # exported conversation row before the row reaches the archive the facade
        # uploads; ``nightly`` applies it to that failure text, which the status route
        # serves (the call-site scan matches its read of the outbound-redaction switch,
        # ``_unattended_sessions_redaction_gap``); ``retention`` redacts only its
        # gate-side log lines and SEL audit text. The split adds no transport or audience
        # and therefore no posture row.
        "apps/builtins/aws_control/backend/backup_parts/conversations.py",
        "apps/builtins/aws_control/backend/backup_parts/egress_text.py",
        "apps/builtins/aws_control/backend/backup_parts/nightly.py",
        "apps/builtins/aws_control/backend/backup_parts/retention.py",
        # Internal partitions behind the single registered
        # ``apps/builtins/aws_control/backend/routes.py`` boundary, the AWS Control HTTP
        # surface whose routes serve every response. ``routes_parts/common.py`` holds
        # ``_safe_error``, the one chokepoint every outbound error string goes through,
        # and ``routes_parts/drive.py`` redacts a preview's head bytes before the preview
        # route returns them. The split adds no transport or audience and therefore no
        # posture row.
        "apps/builtins/aws_control/backend/routes_parts/common.py",
        "apps/builtins/aws_control/backend/routes_parts/drive.py",
        # Beside the registered browser_cli/install.py sink: install_runner.py defines its redactor;
        # the launcher and seams owners apply it to refusal and seam text. No new egress path.
        "browser_cli/install_launcher.py",
        "browser_cli/install_runner.py",
        "browser_cli/install_seams.py",
        # The shared recursive redactor helper itself — a pure scrubber, not an
        # egress boundary; the modules that CALL it (mochi routes/hooks) are the
        # registered sinks.
        "apps/builtins/mochi/redact.py",
        # Shared declared-temp warning-field formatter: applies a redactor the
        # caller injects, then repr-escapes the result. It emits nothing; the
        # probe and gateway logger modules that consume the returned string are
        # the output boundaries.
        "sandbox_workspace.py",
        # Shared model-fallback text builders (fallback_story_of /
        # annotate_model_fallback): scrub the chain-exhaustion story and the
        # fallback-served warning line ONCE, centrally, so every consumer gets
        # the same safe text. They own no output — the scrubbed text reaches a
        # human only through the delivering surfaces (the cron/heartbeat alert
        # paths composed into slack/gateway.py and the sub-agent completion path
        # in subagent_parts/run_output.py), which are the registered sinks.
        "llm_helpers_parts/model_fallback.py",
        "llm_helpers_parts/tool_approval.py",
        # Shared credential give-up line builder (credential_give_up_text):
        # scrubs the provider text ONCE, centrally, through redact_via_context, so
        # the dashboard row, the
        # channel post and the Slack reply that carry it all get the same safe
        # text. It owns no output; those delivering surfaces
        # (dashboard/chat_runner.py, channel.py, and for Slack
        # handler_runtime/delivery.py) are the registered sinks.
        "llm_helpers_parts/backend_errors.py",
        # Shared in-band deny-notice builder: redacts the provider-authored tool
        # title ONCE, centrally, before the notice is steered back into the
        # model's own turn (not to a person). It owns no egress — the steer goes
        # to the ACP subprocess through the provider, and the display rows a
        # human sees are appended by the dashboard chat turn and the native Slack turn,
        # the registered sinks for those surfaces (Slack's is handler_runtime/delivery.py).
        "deny_notice.py",
        # The app-facing seam: it OWNS no output. It hands the redaction pass to
        # an installed app so the app can scrub content at its own boundary, and
        # the write that follows happens in app code this repo does not inventory.
        # Registering it as a sink would count an egress path as covered on the
        # strength of a call the app may never make, which is the overstatement
        # the panel's count exists to avoid; the app's own boundary is the real
        # one and is outside this list either way. Not a companion-blind site
        # despite naming a baseline redactor: it takes the WARNINGS from the
        # baseline pass and finishes the text through `redact_via_context`, so a
        # loaded companion's patterns still apply to whatever the app publishes.
        "apps/scrub_sdk.py",
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
        # the account snapshot is BUILT, and the registry default that
        # resolve_consent_target falls back to. It owns no output — the snapshot
        # is served through routes.py, the registered sink for this app, and the
        # resolved profile/region reach the paid-service consent card through
        # dashboard/handlers/aws_consent.py; both read text this module already
        # scrubbed.
        "apps/builtins/aws_control/backend/accounts.py",
        # Same shape, one layer earlier: scrubs runner-supplied job payload as the
        # run record is BUILT and persisted. `_redact` covers `step`, `error` and
        # each progress line, and `_json_safe` recurses through nested structures
        # (dict KEYS as well as values) before `_persist` writes the record. It
        # owns no output -- the record reaches a human only through
        # apps/job_routes.py, the registered sink for this surface. The on-disk
        # file is not the egress boundary the panel counts.
        "apps/job_sdk.py",
        # The run-record owner the ``apps/job_sdk.py`` facade serves: it hosts ``_redact``,
        # the record's ingest scrub, under the facade's classification.
        "apps/job_sdk_records.py",
        # Partitions of the "App lifecycle output" sink beside the registered
        # apps/routes_uninstall.py.
        "apps/routes_enable.py",
        "apps/routes_lifecycle.py",
        "apps/routes_listing.py",
        "apps/routes_registry.py",
        # Same shape: hosts _redact_memory_field, the shared recursive scrubber
        # for memory fields. It owns no output of its own — the handler modules
        # that call it (memory.py and the cron.py owners) are the covered surfaces.
        "dashboard/handlers/_shared_scrub.py",
        # The shared helpers' memory-store owner scrubs a store's refusal text with it.
        "dashboard/handlers/_shared_memory_stores.py",
        # Applies the memory scrubber as a COMPARISON on the write path, never on
        # the way out: `_require_editable_record` refuses a whole-value edit whose
        # stored source does not survive the scrub unchanged, because the browser
        # drafts from the display form and writing that back would replace the
        # record's content with it. Nothing here is emitted -- the response this
        # guard protects is served by `dashboard/handlers/memory_edit.py`, the
        # registered sink for the records surface.
        "memory_edit.py",
        # Same shape: applies a redactor the CALLER injects, to scan the form a
        # platform will actually render (markup collapsed, ANSI stripped). It owns
        # no output of its own -- the registered sinks are the modules that call
        # it (slack/format.py, messaging/renderer.py).
        "messaging/display_safety.py",
        # Also a caller-injected redactor, applied to DECIDE rather than to emit:
        # the splitter grades the boundaries a length budget chose, so a key the
        # cut would sever is not handed to a reader across two messages, and it
        # redacts the text it hands back only when no budget cuts it safely. The
        # chunks go to the module that asked for them, which is the registered
        # sink (slack/format.py, messaging/renderer.py and the channel renderers).
        "messaging/split.py",
        # ``autonudge_service/store_loader.py``'s ``_load`` credential-scrubs a persisted
        # ``banner`` in memory and attempts to persist the masked value back, so a banner
        # written to the store out-of-band (a hand-edited file, or a direct
        # ``AutoNudgeService.add`` that skips the authorizer) is masked on the next
        # load rather than served raw — best-effort, since a failed re-persist
        # leaves the raw value on disk until the following load. It owns no output
        # of its own — the egress that serves the banner is the registered
        # ``dashboard/handlers/autonudge.py`` serializer and the gateway broadcast;
        # this is at-rest sanitisation at the trust boundary where the store is
        # read, not an egress pass.
        "autonudge_service/store_loader.py",
        "autonudge_authz.py",
        # Applies no redactor at all: the pipeline conductor's fleet probe BUILDS a
        # command fragment for a `BANNED` line out of what can hold no secret -- a
        # runner or launcher name drawn from a fixed vocabulary, and an option name
        # with its value dropped, the cap flag's digits included -- and withholds
        # every other token, counting it as `+<n>`. What matches the call-site scan
        # here is the name of that builder (`_redacted_command`), not a call to a
        # redactor: nothing is scrubbed, because nothing unrecognised is emitted in
        # the first place. Registering it as a sink would make the panel claim this
        # path is covered BY redaction, when what covers it is withholding -- the
        # same overstatement the decision-seam entry above refuses for refusal.
        "builtin_skills/pipeline-conductor/scripts/fleet_probe.py",
        # The loop stop record: store-sourced ids and caller reasons are scrubbed
        # before they are held in memory or written to the gateway log or the
        # stop file under ``logs/``. Local diagnostics, not an egress pass.
        "autonudge_stop_log.py",
        # Inbound structured-monitor target validation. A canonical provider URL
        # is rejected when its path contains credential-shaped text, before the
        # target reaches persistence, inspection, or a wake envelope.
        "monitoring/targets.py",
        # Gate-side log hygiene for a channel whose user identity IS a phone
        # number or an Apple Account email. ``redact_handle`` shortens a handle
        # before it reaches a gateway log line or a SEL ``caller`` field. None of
        # these modules writes message content to the user through this call.
        #
        # ``imessage/renderer.py`` is deliberately NOT in this list even though
        # it also calls ``redact_handle`` for a delivery-failure log line: it is
        # a real egress sink, registered as one in security_posture_sinks_delivery.py. Its redaction is NOT
        # the shared TurnDriver's and is not already counted there -- treating it as
        # such is the kind of wrong that suppresses a gate. The driver scans the
        # provider stream as literal bytes; the renderer then flattens the markup, which
        # stream as literal bytes; the renderer then flattens the markup, which
        # can reassemble a credential that scan could not see.
        "imessage/client.py",
        "imessage/transport.py",
        "imessage/transport_dispatch.py",
        # The WhatsApp turn lifecycle. What the scan matches here is the
        # post-answer redaction NOTICE pair -- ``count_redaction_tags`` over the
        # delivered chunks and ``redaction_notice`` to build the follow-up
        # sentence -- which tallies placeholders already written by a redaction
        # pass and rewrites no outbound byte itself. The pass that carries this
        # channel's guarantee is ``whatsapp/renderer.py``'s render pipeline
        # (``render_chunks`` / ``display_safe_text``), registered as the sink
        # in security_posture_sinks_delivery.py; its row names this module as the wire-writer those screened
        # forms ship through.
        "whatsapp/turn_renderer.py",
        # ``feishu/renderer.py`` is deliberately NOT in this list even though it
        # carries the same redaction-notice pair as the WhatsApp entry above: it
        # is a real egress sink -- its ``text()`` override screens the answer
        # body through ``redact_for_target`` at the send boundary -- and is
        # registered as one in security_posture_sinks_delivery.py. The notice pair rides along in a module the
        # sink registry already classifies.
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
        "acp/_dispatch_parts/adapter_signals.py",
        "acp/_dispatch_parts/permission_requests.py",
        "acp/_dispatch_parts/session_update_parser.py",
        "acp/_dispatch_parts/surfaced_text.py",
        "acp/_dispatch_parts/tool_result_events.py",
        # The client's owners: code that was acp/client.py's own, scrubbing the
        # same strings for the same records.
        "acp/client_lifecycle.py",
        "acp/client_mcp_array.py",
        "acp/client_model_selection.py",
        "acp/client_permissions.py",
        "acp/client_start.py",
        "acp/client_tool_events.py",
        "acp/client_transport.py",
        "acp/client_turn.py",
        "acp/client_turn_control.py",
        "acp/client_usage.py",
        "acp/transport_errors.py",
        # Split out of transport_errors to keep that file under the line cap; its
        # redact_text call scrubs a compaction-failure reason for the SAME
        # compaction notice, whose display sink is already registered, so it is
        # not an egress boundary of its own.
        "acp/compaction_errors.py",
        "acp/runtime_frame_accounting.py",
        # Redacts the tool title in the auto-rejected-permission WARNING (a
        # gate-side log line) and defers user-facing display to the routed
        # permission event, whose sinks are already registered.
        "acp/runtime_host_requests.py",
        "acp/runtime_liveness.py",
        "acp/runtime_session_registry.py",
        "acp/session_handle_compaction.py",
        "acp/session_handle_dispatch.py",
        "acp/session_handle_kas.py",
        "acp/session_handle_lifecycle.py",
        "acp/session_handle_model_selection.py",
        "acp/session_handle_native_children.py",
        "acp/session_handle_permissions.py",
        "acp/session_handle_turn.py",
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
        # a credential mid-match (a sliced fragment does not match the
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
        # - driver_publish.py's failed-push note (git's push stderr) goes to the run
        #   ledger, and its log line through redact_log_via_context; the ledger reaches
        #   the dashboard only through the backend routes.
        "apps/builtins/auto_improvement/backend/clone_setup.py",
        "apps/builtins/auto_improvement/profiles/github_repo/profile.py",
        "apps/builtins/auto_improvement/spine/driver_publish.py",
        "apps/builtins/auto_improvement/spine/gate.py",
        # Inbound: the crew worker's slot title is derived from an issue title,
        # which is untrusted text anyone who can open an issue wrote. It is
        # scrubbed before it becomes a slot title (and fails CLOSED to the slot
        # key if the redactors are unavailable), so this is inbound sanitisation
        # rather than an egress boundary — the slot title's user-visible surface
        # is already covered by the registered dashboard sinks.
        "apps/builtins/issue_radar/backend/crew_runtime_session.py",
        # Log/audit hygiene, not an egress boundary: strips ``user:password@`` from
        # an external registry's clone URL before it reaches the SEL credential-grant
        # record and the warning logs. The URL is index-supplied, so it can carry a
        # token; scrubbing it keeps the secret out of a persisted audit trail. It is
        # not an output bound for a human or a third party, and the value that DOES
        # reach a dashboard client (``GET /api/apps/registries``) is protected by
        # refusing a credential-bearing repo outright rather than by redacting it.
        # The same owners reduce git output from a credentialed transport to fixed
        # failure classes, which carry no text from that output; ``store_art.py``
        # consumes that reduction for the clone-failure record it keeps beside the
        # manifest cache (a constant class label, read only by the prewarm).
        "apps/registry_pipeline/checkout.py",
        "apps/registry_pipeline/git_targets.py",
        "apps/registry_pipeline/indexes.py",
        "apps/registry_pipeline/sources.py",
        "apps/registry_pipeline/store_art.py",
        # Internal persistence / indexing (the on-disk or in-memory copy), whose
        # user-visible surface is already covered by a registered sink.
        "dashboard/chat_folders_icons.py",
        "dashboard/chat_fork.py",
        # Inbound: the receiving half of the session transfer. An arrived bundle's
        # title, origin label and rows are scrubbed before they become a slot, its
        # transcript and its audit line, so this is inbound sanitisation rather than an
        # egress boundary; ``dashboard/session_transfer.py`` stays the registered
        # "Session transfer bundle" sink for what leaves the host.
        "dashboard/session_transfer_install.py",
        # The owners composed into the ``dashboard/chat_handlers.py`` facade: the same
        # redactor calls, moved verbatim, under the facade's existing non-egress
        # classification; the split adds no egress path.
        "dashboard/chat_api/agent_switch.py",
        "dashboard/chat_api/chat_send.py",
        "dashboard/chat_api/followup_card.py",
        "dashboard/chat_api/pending_context.py",
        "dashboard/chat_api/resume.py",
        "dashboard/chat_api/slot_create.py",
        "dashboard/chat_api/slot_detail.py",
        "dashboard/chat_api/slot_note.py",
        "dashboard/chat_api/slot_settings.py",
        # Owners composed into dashboard/session_control.py, their redactor calls
        # moved verbatim out of dashboard/session_control.py.
        # The refusal factory scrubs the target and reason it writes to the audit
        # record; the model switch and the create verb refuse a model name their
        # redaction would change; and the model a pending pick reports, and the
        # inherited model a create reply says it dropped, are redacted before they
        # go back to the calling agent. None is a transcript or file output path.
        "dashboard/session_control_parts/target_authorization.py",
        "dashboard/session_control_parts/model_switch.py",
        "dashboard/session_control_parts/session_create.py",
        # The runner's own stream loop scrubs a tool call's id before the id keys a row;
        # the rows reach the browser through the dashboard's sinks.
        "dashboard/chat_runner.py",
        # The owners composed into dashboard/chat_runner.py: the redactor calls of the runner's
        # helpers, _run_chat's closures and its phases, unchanged (deny rows, turn and spawn errors,
        # file-change snapshots, the MCP sign-in banner, fallback model ids, native cards, prompt
        # and skill mentions, the queue drain, admission and permission logs, stream redactors,
        # cross-surface legs, tool rows and results, directive tool-call ids, the
        # agent-switch name, the /compact summary and failure reason, the partial reply and stream
        # flushes, spec-hook notices, the stats footer). Their output reaches the runner's surfaces.
        "dashboard/chat_turn/acp_recovery.py",
        "dashboard/chat_turn/deny_chokepoints.py",
        "dashboard/chat_turn/eager_spawn.py",
        "dashboard/chat_turn/elicitation_prompt.py",
        "dashboard/chat_turn/file_changes.py",
        "dashboard/chat_turn/mcp_session.py",
        "dashboard/chat_turn/model_fallback.py",
        "dashboard/chat_turn/native_cards.py",
        "dashboard/chat_turn/permission_prompt.py",
        "dashboard/chat_turn/prompt_assembly.py",
        "dashboard/chat_turn/queue_drain.py",
        "dashboard/chat_turn/recipient.py",
        "dashboard/chat_turn/session_events.py",
        "dashboard/chat_turn/slash_commands.py",
        "dashboard/chat_turn/tool_approval.py",
        "dashboard/chat_turn/tool_results.py",
        "dashboard/chat_turn/tool_rows.py",
        "dashboard/chat_turn/turn_admission.py",
        "dashboard/chat_turn/turn_entry.py",
        "dashboard/chat_turn/turn_failures.py",
        "dashboard/chat_turn/turn_ledgers.py",
        "dashboard/chat_turn/turn_reply_seams.py",
        "dashboard/chat_turn/turn_spec_hooks.py",
        "dashboard/chat_turn/turn_stats.py",
        "dashboard/chat_nav.py",
        "dashboard/chat_persistence_parts/member_store.py",
        "dashboard/chat_persistence_parts/restore_drivers.py",
        "dashboard/chat_persistence_parts/slot_builders.py",
        # The row projection and the restore-time title check, composed into that
        # facade: the same internal persisted copy, no output of their own.
        "dashboard/slot_persistence/message_entries.py",
        "dashboard/slot_persistence/metadata_codec.py",
        "dashboard/chat_regenerate.py",
        "dashboard/chat_title.py",
        "dashboard/chat_utils.py",
        # The display-redaction owner served by the ``dashboard/chat_utils.py`` facade:
        # the same redactor calls, moved verbatim, under the facade's classification.
        "dashboard/chat_utils_display.py",
        # Slot-detail row JSON is an internal render projection, returned by
        # the existing dashboard handler sink. Its empty-string redaction key
        # shares that sink's policy context; it opens no separate output path.
        "dashboard/wire_render.py",
        # The display battery chat_utils composes (both shared redactors, in
        # order), moved beside it so the rules version can hash it. It returns
        # the redacted string to chat_utils and emits nothing of its own.
        "dashboard/display_battery.py",
        "dashboard/chat_voice.py",
        # The grep route and its content-search engines, composed into
        # dashboard/handlers/files.py: they redact each hit row, and that facade, a
        # registered sink module, serves the route that returns the rows.
        "dashboard/file_api/grep.py",
        # The outbox routes, the Slack and channel uploads with their admission gate,
        # and the file viewer's read, raw, write and diff routes, composed into
        # dashboard/handlers/files.py: they redact the file cards, upload gates and
        # viewer bodies that facade's registered sink rows already name, and open no
        # output path of their own.
        "dashboard/file_api/outbox.py",
        "dashboard/file_api/channel_upload.py",
        "dashboard/file_api/editor.py",
        # Owner-driven components extracted from dashboard/state.py.  They
        # redact while staging or dispatching through the facade, but they do
        # not introduce new logical egress paths: the same DashboardState
        # boundaries are represented by the owners' registered sink rows ("Dashboard
        # slot snapshot" -> state_parts/slot_transcript.py, "Session detach notice" ->
        # state_parts/session_events.py).  Classifying the implementation files separately
        # would make a behavior-preserving decomposition change the public posture items
        # and count even though no new value crosses a process or persistence boundary.
        "dashboard/interaction_coordinator.py",
        "dashboard/notification_coordinator.py",
        "dashboard/slot_projection.py",
        "dashboard/state_parts/dashboard_services.py",
        "dashboard/state_parts/notification_log.py",
        "dashboard/state_parts/slot_directory.py",
        "dashboard/state_parts/slot_turns.py",
        "dashboard/state_parts/slots_frame.py",
        "dashboard/state_parts/transcript_rows.py",
        "dashboard/websocket_hub.py",
        # Owners extracted from dashboard/handlers/source_providers.py, which keeps
        # the HTTP handlers. They redact provider data before it is cached or
        # returned through those handlers, but they add no logical egress path:
        # the same source-provider boundaries the handler entry covered, split by
        # responsibility, so the decomposition leaves the posture items unchanged.
        "dashboard/source_providers/adf.py",
        "dashboard/source_providers/cache.py",
        "dashboard/source_providers/chip_refresh.py",
        "dashboard/source_providers/review.py",
        "dashboard/source_providers/sanitize.py",
        # Pre-redacts a persisted subagent record's retained text at the point it
        # is READ, because that is also the point it is clamped to its field caps
        # and the two have a required order: a value cut at the cap first loses
        # the tail a credential pattern needs, so a downstream scanner cannot
        # match the fragment that survives. The outbound bytes are still redacted
        # at the registered sinks -- `dashboard/handlers/messaging.py` for the
        # REST listing, and the WS replay's own pre-redaction into `state.py` --
        # so this module hands records to consumers rather than writing to a
        # human, and is not itself an egress boundary.
        "subagent_persistence.py",
        # Pre-redacts follow-up items before handing to the WS egress in
        # state_parts/slots_push.py; its own return string is re-redacted by
        # chat_runner before broadcast. Not itself an egress boundary.
        "dashboard/session_directive_apply_slot.py",
        # The same consumer's other owners scrub a monitor notice row before
        # `append_and_surface` records it, and a stop reason before the loop keeps it.
        "dashboard/session_directive_apply_outcome.py",
        "dashboard/session_directive_apply_monitor.py",
        "dashboard/cron_inject.py",
        "dashboard/ws.py",
        # The gateway bootstrap's owner DM and workflow-result turn (owners that
        # dashboard/server.py composes).
        "dashboard/server_runtime/owner_notices.py",
        "dashboard/server_runtime/workflow_startup.py",
        "dashboard/handlers/sessions.py",
        "dashboard/handlers/sessions_parts/history_reads.py",
        "dashboard/handlers/sessions_parts/summaries.py",
        "dashboard/handlers/sessions_parts/tool_policy.py",
        "dashboard/handlers/sessions_parts/usage_scrape.py",
        # Roster last-message previews, and the drawer's reads of one crewmate: same
        # preview + redaction chain as handlers/sessions_parts/history_reads.py,
        # feeding the same dashboard HTTP surface the registered sink already covers.
        "dashboard/handlers/members_roster.py",
        "dashboard/handlers/members_drawer.py",
        # The crewmate recap lines `handlers/members_drawer.py`'s recap route returns:
        # a helper behind that same dashboard HTTP surface, not a boundary itself.
        "member_recap.py",
        "dashboard/handlers/artifacts_parts/artifact_comments.py",
        "dashboard/handlers/artifacts_parts/artifact_folders.py",
        "dashboard/handlers/artifacts_parts/create_update.py",
        "dashboard/handlers/artifacts_parts/dashboard_package_gate.py",
        "dashboard/handlers/artifacts_parts/library_reads.py",
        "dashboard/handlers/artifacts_parts/publish_sharing.py",
        "dashboard/handlers/artifacts_parts/record_serialization.py",
        "dashboard/handlers/artifacts_parts/remote_artifacts.py",
        "dashboard/handlers/artifacts_parts/route_plumbing.py",
        "dashboard/handlers/artifacts_parts/session_documents.py",
        "dashboard/handlers/core.py",
        "dashboard/handlers/core_config_view.py",
        "dashboard/handlers/core_stt_speech.py",
        "dashboard/handlers/cron_board.py",
        "dashboard/handlers/cron_jobs.py",
        "dashboard/handlers/cron_lessons.py",
        "dashboard/handlers/cron_runs.py",
        "dashboard/handlers/cron_script_source.py",
        "dashboard/handlers/cron_secret_grants.py",
        "dashboard/handlers/cron_shared.py",
        # Same class as the dashboard handlers around it: the five debug reads
        # redact every string they return -- a gateway description, a classified
        # refusal, a process roster -- but the egress boundary is the loopback
        # transport the result crosses to the kirocrew-debug proxy, not this module.
        "dashboard/handlers/debug.py",
        "dashboard/handlers/discover.py",
        "dashboard/handlers/hooks.py",
        "dashboard/handlers/hooks_scripts.py",
        "dashboard/handlers/hooks_webhooks.py",
        "dashboard/handlers/knowledge_agent_context.py",
        "dashboard/handlers/knowledge_bundles.py",
        "dashboard/handlers/knowledge_ingest.py",
        "dashboard/handlers/knowledge_sources.py",
        "dashboard/handlers/knowledge_sync.py",
        "dashboard/handlers/memory.py",
        "dashboard/handlers/memory_embeddings.py",
        "dashboard/handlers/memory_graph.py",
        "dashboard/handlers/optimizer.py",
        "dashboard/handlers/prompts_authoring.py",
        "dashboard/handlers/prompts_reading.py",
        "dashboard/handlers/taskrunner.py",
        "dashboard/handlers/terminal.py",
        "dashboard/handlers/themes.py",
        "dashboard/handlers/updates_apply.py",
        "dashboard/handlers/webapp_preview.py",
        "dashboard/handlers_project.py",
        # The spawn and Slack workspace owners composed into ``dashboard/handlers/messaging.py``:
        # they redact run text and Slack reads the facade's routes answer with, and that
        # facade stays the registered sink for those REST responses, so they are not egress
        # paths of their own. (The send owner posts to Slack and is a registered sink in security_posture_sinks_delivery.py.)
        "dashboard/messaging_api/run_control.py",
        "dashboard/messaging_api/run_views.py",
        "dashboard/messaging_api/slack_workspace.py",
        "dashboard/messaging_api/spawn.py",
        "knowledge/agent_fetch.py",
        "knowledge/agent_source.py",
        "knowledge/artifact_ingest.py",
        "knowledge/ingestion.py",
        "workflows/library.py",
        "mcp_core.py",
        # The same server's owner modules, same class: they redact an HTTP error
        # body, a history snippet, a crew error and the issue-radar crew's writes
        # before ``mcp_core`` returns or forwards them, and the egress boundary is
        # the transport the result crosses.
        "mcp_core_parts/crew.py",
        "mcp_core_parts/gateway.py",
        "mcp_core_parts/history.py",
        "mcp_core_parts/radar_crew.py",
        # Same class as mcp_core.py: this stdio server redacts every rendered tool
        # result -- a crew log page, a fold, a refusal -- before returning it, but
        # the egress boundary is the transport the result crosses, not this module.
        "mcp_crew_log.py",
        # Same class as mcp_core.py: this stdio server redacts every rendered tool
        # result -- a gateway description, a classified refusal, a folded series --
        # before returning it, but the egress boundary is the transport the result
        # crosses, not this module.
        "mcp_debug.py",
        "mcp_cron.py",
        "mcp_cron_list_render.py",
        "mcp_cron_vet.py",
        # Same class as mcp_core.py: the kirocrew-dashboard stdio server's owner
        # modules -- its reference resolvers and its tool bodies -- redact tool
        # RESULTS and agent-authored names before they are persisted or returned,
        # but the egress boundary itself is the transport the result crosses, not
        # these modules.
        "mcp_dashboard_folder_runs.py",
        "mcp_dashboard_references.py",
        "mcp_dashboard_session_runs.py",
        "mcp_dashboard_tag_runs.py",
        # Same class again: this stdio server redacts the ledger's own
        # worker-authored prose before returning it, but the egress boundary is
        # the transport the result crosses, not this module.
        "mcp_work.py",
        # Same class again, and worth stating precisely because the module's NAME
        # suggests otherwise. Two flows pass through the kirocrew-panel server and
        # only one of them touches a redactor:
        #
        #   agent -> dashboard: the `data` object and `title` are FORWARDED
        #     verbatim by `panel_publish` (`_post` at mcp_panel.py:189). This
        #     module applies no redactor to them. The scrub happens in
        #     `agent_panel.publish`, a registered sink in security_posture_sinks_delivery.py -- so the
        #     panel content IS counted in the posture report, under the module
        #     that actually performs it rather than the one that relays it.
        #   dashboard -> model: the gateway's refusal prose, which can quote
        #     caller-supplied material (an echoed template id), is scrubbed at
        #     mcp_panel.py:167 and :194 before being handed back as a tool result.
        #
        # It is the second flow that matches the call-site scan, and its consumer
        # is the MODEL over the stdio transport -- the boundary is that transport,
        # not this module, exactly as for the `mcp_dashboard_*.py` owners above.
        "mcp_panel.py",
        # Same class as `mcp_panel.py`: the guide shim relays gateway results and
        # redacts only the refusal prose it hands back to the model; the boundary
        # is the stdio transport, not this module.
        "mcp_guide.py",
        # Same class again: the mate shim relays gateway results and redacts
        # the refusal prose and the record it hands back to the model.
        "mcp_mate.py",
        "mcp_gateway/backend.py",
        # The backend's wire owner: it redacts the server-supplied identifiers and
        # tool-call error text the backend's gateway log warnings carry, and like
        # mcp_gateway/backend.py it is not an egress boundary.
        "mcp_gateway/backend_wire.py",
        # The kirocrew-core tool handlers, moved out of mcp_core.py into their
        # domain modules. Same classification as mcp_core.py above for the same
        # reason: a tool result's user-visible surface is a registered sink
        # downstream, and these redact before returning to it. The owner modules
        # of the control and spawn facades are classified with their domain.
        # `learn.py` and `spawn.py` are absent because they call no redactor (the
        # spawn reads that redact are in its `spawn_parts` owners) -- the allowlist
        # is checked for stale entries too.
        "mcp_tools/apps.py",
        "mcp_tools/artifacts.py",
        "mcp_tools/control.py",
        "mcp_tools/control_parts/monitor.py",
        "mcp_tools/knowledge.py",
        "mcp_tools/messaging.py",
        "mcp_tools/sessions.py",
        "mcp_tools/skills.py",
        "mcp_tools/spawn_parts/listing.py",
        "mcp_tools/spawn_parts/wave.py",
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
        "suggestions.py",
        "tips.py",
        "task_executor.py",
        "taskrunner_parts/run_steps.py",
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
        "cron_script_context.py",
        "cron_script_mcp.py",
        "cron_script_redaction.py",
        "eval/runner.py",
        # The owners composed into kiro_prerequisite.py that call a redactor, in the
        # same class as that facade.
        "kiro_prerequisite_probe.py",
        "kiro_prerequisite_status.py",
        "instances/ssh_tunnel_manager_child_text.py",
        "instances/ssh_tunnel_manager_remote_ops.py",
        "instances/token_mint.py",
        "instances/ssm_token_mint.py",
        "publish_sync.py",
        "cli_commands_agent.py",
        "cli_commands_cron.py",
        "cli_commands_eval.py",
        "cli_commands_learn.py",
        # Slack sub-surfaces whose posted output is covered by the two Slack rows.
        "slack/events.py",
        "slack/events_home_tab.py",
        "slack/events_slash.py",
        # Inbound attachment ingestion: redacts text extracted FROM a user's
        # own uploaded file before it enters the prompt. Inbound sanitisation,
        # not agent output on its way to a user.
        "messaging/attachments.py",
        "slack/interactions.py",
        "slack/interactions_approvals.py",
        "slack/interactions_modals.py",
        "slack/interactions_options.py",
        "slack/interactions_sessions.py",
        "slack/renderer.py",
        "slack/sessions_view.py",
        # The Slack handler facade and the owners composed into it: the facade defines
        # the canonical-order pair delivery.py's fallback egress uses (_display_redactor),
        # and the owners redact the answer stream, the approval prompts and tool titles,
        # the command and keyword replies, the turn's error text and the thread context;
        # handler_runtime/delivery.py is the registered "Slack messages" sink for them all.
        "slack/handler.py",
        "slack/handler_runtime/approvals.py",
        "slack/handler_runtime/commands.py",
        "slack/handler_runtime/dispatch.py",
        "slack/handler_runtime/inbound.py",
        "slack/handler_runtime/permission_ladder.py",
        "slack/handler_runtime/stream.py",
        "slack/handler_runtime/turn.py",
        "slack/handler_runtime/turn_context.py",
        # The owners composed into slack/gateway.py, in the same class as that
        # facade: they redact the install-repair, update and boot logs, the
        # AutoNudge notices, the nudge and heartbeat turns' posted replies, the
        # approval prompts, the cron runs' results and failure alerts, the
        # sub-agent completions they report, and the unattended results they
        # deliver. The registered "Slack cron / notification posts" sink is the
        # composed sub-agent injection owner, which holds that row's redaction;
        # these share its process and namespace, so the split adds no egress path
        # of its own.
        "slack/gateway_runtime/cron_delivery.py",
        "slack/gateway_runtime/cron_exec.py",
        "slack/gateway_runtime/cron_failure.py",
        "slack/gateway_runtime/cron_fire.py",
        "slack/gateway_runtime/heartbeat_turns.py",
        "slack/gateway_runtime/install_repair.py",
        "slack/gateway_runtime/nudge_channel_fire.py",
        "slack/gateway_runtime/nudge_wiring.py",
        "slack/gateway_runtime/result_egress.py",
        "slack/gateway_runtime/service_boot.py",
        "slack/gateway_runtime/subagent_report.py",
        "slack/gateway_runtime/subagent_wiring.py",
        "slack/gateway_runtime/turn_approvals.py",
        "slack/gateway_runtime/update_apply.py",
        "slack/gateway_runtime/update_schedule.py",
        # The owners composed into discord/transport_dispatch.py's class, beside the
        # registered "Discord direct send" sink (the !compact owner, which holds that
        # row's redaction): they redact the spawn-approval prompt and the transcript's
        # failure row, and share that sink's class and namespace, so the split adds no
        # egress path of its own.
        "discord/transport_dispatch_ledger.py",
        "discord/transport_dispatch_spawn_approval.py",
        # The owners of mcp_discovery.py beside its "MCP server metadata" sink: what
        # they redact is stored on the McpServerInfo record that sink serializes, so
        # the split adds no egress path of its own.
        "mcp_discovery_config.py",
        "mcp_discovery_probe.py",
        "mcp_discovery_probe_state.py",
        # Redaction of a LOG line or a diagnostic URL/token, not agent output on
        # its way to a user. These match the (deliberately broad) redactor regex
        # in the drift guard but are not egress paths.
        "cli_doctor.py",
        "cloud/connect.py",
        "cloud/login.py",
        "embeddings_download.py",
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
        "monitoring/pull_request.py",
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
        # user-facing surfaces that consume them are registered sinks in security_posture_sinks_*.py.
        "platform/context.py",
        "mcp_shared_calls.py",
        "mcp_shared_dispatch.py",
        "mcp_shared_policy.py",
        "dashboard/handlers/agents.py",
        # The agents handlers composed into dashboard/handlers/agents.py, in the
        # same class as that facade and the dashboard handlers around it: the
        # config and template-pane reads mask `oauth.clientSecret`, and the
        # capability routes and the roster row mask edition- and agent-written
        # text with `redact_external_text`, all answering the dashboard's HTTP
        # surface rather than adding an egress path of their own.
        "dashboard/agent_admin/agent_config.py",
        "dashboard/agent_admin/agent_detail.py",
        "dashboard/agent_admin/capabilities.py",
        "dashboard/agent_admin/roster.py",
        # The deploy routes and owners: a pre-publish scan, not an egress of agent output.
        "deploy/handlers.py",
        "deploy/handlers_guards.py",
        "deploy/handlers_sites.py",
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
        # A behavior-preserving decomposition of the Research Lab backend. These
        # modules feed the same app-owned HTTP/SSE surface as handlers.py; the
        # split adds no transport or audience and therefore no posture sink.
        "apps/builtins/auto_research/campaign/agent_mode.py",
        "apps/builtins/auto_research/campaign/exploration.py",
        "apps/builtins/auto_research/campaign/lifecycle.py",
        "apps/builtins/auto_research/campaign/publication.py",
        "apps/builtins/auto_research/campaign/storage.py",
        "apps/builtins/auto_research/campaign/untrusted.py",
        "apps/builtins/auto_research/campaign/workflow_mode.py",
        "apps/builtins/code_review_sage/sage_lib/learning.py",
        "apps/builtins/code_review_sage/sage_lib/pipeline.py",
        "apps/builtins/code_review_sage/sage_lib/report.py",
        "apps/builtins/code_review_sage/sage_lib/review_driver.py",
        # Split out of `review_driver` unchanged: it scrubs the worker-written
        # record's validation and persist errors before the driver surfaces them.
        # Same classification as `review_driver`.
        "apps/builtins/code_review_sage/sage_lib/worker_response.py",
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
        # `backend/routes_parts/publishing` scrubs a dismissed finding's reason before
        # the run record stores it. Same classification as `backend/routes` above.
        "apps/builtins/code_review_sage/backend/routes_parts/publishing.py",
        # Dev Fleet's redactor wrapper and the cohesive owners that apply it to
        # the app's own API/state/worktree surfaces, all carrying the same
        # non-core-egress classification. `gateway_routes` is the same surface
        # served from the gateway process (the live-target cutover), redacting
        # the target path and error text bound for its SEL record and JSON reply.
        "apps/builtins/dev_fleet/runtime.py",
        "apps/builtins/dev_fleet/http_api.py",
        "apps/builtins/dev_fleet/gateway_routes.py",
        "apps/builtins/dev_fleet/fleet_state.py",
        "apps/builtins/dev_fleet/repository.py",
        "apps/builtins/dev_fleet/live.py",
        "apps/builtins/dev_fleet/worktree_ops.py",
        "apps/builtins/dev_fleet/worktree_ops_pods.py",
        "apps/builtins/dev_fleet/worktree_ops_removal.py",
        # Same Dev Fleet surface: redacts a failed systemd/launchd command's
        # stderr at the point of capture, before its bounded tail becomes the
        # cutover failure reason the app's own routes render.
        "apps/builtins/dev_fleet/gateway_service.py",
        # Capture-side, not egress: the GitHub Issues provider scrubs `gh`'s
        # stderr as it captures a failed poll or action, before the bounded tail
        # becomes the error text. The surfaces that SHOW that text (the app's
        # routes and dispatch) are the registered sinks.
        "apps/builtins/ops_mission_control/backend/providers/github_issues.py",
        # Gate-side log hygiene, same shape as update_provider: the tailnet
        # probe and mise activation redact a failed CLI's stderr before its
        # bounded tail is written to the gateway debug log; an auth-key or
        # registry URL in that stderr must not reach the log ring / /api/logs.
        "dashboard/tailnet.py",
        "env.py",
        # Operator's own terminal during `kirocrew setup`: redacts npm's and
        # electron-builder's stderr (a registry URL can carry a token) before
        # the bounded tail is printed. Same classification as the cli_commands_* rows above.
        "cli_setup.py",
        # Issue Radar's route layer: its AI routes redact the model's own output
        # (summaries, label reasons, recommendation text) before caching or
        # returning it. These three modules hold those calls.
        "apps/builtins/issue_radar/backend/http_routes/ai.py",
        "apps/builtins/issue_radar/backend/http_routes/recommendations.py",
        "apps/builtins/issue_radar/backend/http_routes/tagging.py",
        "apps/builtins/meetings/backend/domain/session.py",
        # Live translation redacts the MODEL's answer before writing it to the
        # meeting's translations.json. The source line was already redacted at
        # dispatch, so this covers only what a model reintroduced, and the
        # user-visible surface is the app's own translations route.
        "apps/builtins/meetings/backend/domain/translate.py",
        "apps/builtins/meetings/backend/providers/calendar.py",
        "apps/builtins/meetings/backend/providers/tasks.py",
        # `_common.dispatch_line` is the transcript-ingress boundary every producer
        # (speech, the broadcast bar, an audio import) redacts through — inbound
        # scrubbing before the line reaches disk and the agents, same
        # classification as its siblings below.
        "apps/builtins/meetings/backend/routes/_common.py",
        "apps/builtins/meetings/backend/routes/agents.py",
        "apps/builtins/meetings/backend/routes/meeting_lifecycle.py",
        "apps/builtins/meetings/backend/routes/tasks.py",
        "apps/builtins/papyrus/backend/routes.py",
        "apps/builtins/pptx_maker/backend/decks.py",
        "apps/builtins/pptx_maker/backend/routes.py",
        # A behavior-preserving decomposition of the Spec Builder app backend.
        # These modules still feed the same app-owned HTTP/dashboard surface; the
        # split adds no transport or audience and therefore no posture sink.
        "apps/builtins/spec_builder/backend/handlers.py",
        "apps/builtins/spec_builder/backend/parsers.py",
        "apps/builtins/spec_builder/backend/repository.py",
        "apps/builtins/spec_builder/backend/repository_discovery.py",
        "apps/builtins/spec_builder/backend/repository_git.py",
        "apps/builtins/spec_builder/backend/repository_sentinel.py",
        "apps/builtins/spec_builder/backend/runtime.py",
        "apps/builtins/workflows/server.py",
        # Bundled dev-skill scripts: print CI/review findings to a developer
        # terminal (pr_status.py also to the PR Readiness job log), not an
        # agent-output egress path. `_review_contract.py` holds the redactor that
        # both entry points print through.
        "builtin_skills/kirocrew-dev/kirocrew-prepare-pr/scripts/_review_contract.py",
        "builtin_skills/kirocrew-dev/kirocrew-prepare-pr/scripts/pr_findings.py",
        "builtin_skills/kirocrew-dev/kirocrew-prepare-pr/scripts/pr_status.py",
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
        # ``.../backend/registry.py``, a registered sink in security_posture_sinks_delivery.py.
        "apps/builtins/ops_mission_control/backend/secrets.py",
        "apps/builtins/ops_mission_control/backend/providers/http.py",
        # Log-level defensive scrubbing: strips vault secrets and Bearer tokens
        # from log output before it reaches the log ring. Not an egress boundary
        # itself — the log ring reader (/api/logs) is the registered sink.
        "log_redaction.py",
        # CLI bootstrap: the ``install_log_redaction()`` call in
        # ``_setup_cli_logging`` matches the drift-guard's redaction regex.
        # The CLI is not an egress boundary — it's an installer, not a sink.
        "cli_logging.py",
        # Defensive scrub: hooks.py redacts a persisted last_error as it loads a
        # hook (ScriptHook.from_dict), and hook_runtime/script_hooks.py redacts a
        # hook's output before storing it in last_error (an internal model field).
        # The egress boundary is the dashboard API handler that serializes hooks
        # via to_dict() — already a registered sink.
        "hooks.py",
        "hook_runtime/script_hooks.py",
        # Helper, not a boundary: `redact_oauth_client_secrets` /
        # `restore_redacted_oauth_client_secrets` are pure functions over an agent
        # spec dict that mask (and, on the write-back, un-mask) a pre-registered
        # Connections client's `oauth.clientSecret`. Nothing leaves the process
        # here; the egress boundaries are the two dashboard reads that CALL the
        # masker -- `GET /api/agent/config` and `GET /api/agents/detail/{name}`,
        # in `dashboard/agent_admin/agent_config.py` and `agent_detail.py`.
        "mcp_utils.py",
        # Pure-type error-envelope constructor, not an egress boundary: the W01
        # connector control plane's `redacted_detail` / `operation_error` scrub an
        # error `detail` with `redact_and_truncate` as the typed `OperationError`
        # is BUILT, so a credential a provider reflected can never enter the
        # envelope unredacted. The module owns no output and crosses no transport
        # -- the surface that eventually RENDERS a connector error is the egress
        # boundary and is a registered sink there, not here.
        "connections/control_plane/errors.py",
        # Same class, one layer out: the Zoom vendor slice's `redact_zoom_secrets`
        # scrubs Zoom-SHAPE credentials (signed `/rec/` media URLs, bare token
        # fields the site-wide scanner does not recognize) as the error `detail`
        # is BUILT in `zoom_operation_error`, composed BEFORE the control plane's
        # `redacted_detail`. It owns no output and crosses no transport -- the
        # surface that eventually renders a Zoom connector error is the egress
        # boundary and is a registered sink there, not here.
        "connections/vendors/zoom/errors.py",
        # Not a redactor call at all: the keystone path helper
        # `credential_redaction_path()` merely NAMES the switch that governs the
        # credential pass (its docstring and identifier match the scan). Nothing
        # is redacted here and nothing leaves; the sink that honours the switch is
        # `dashboard/handlers/files.py`, already registered.
        "config/loader_locations.py",
        # Owner-gated GET/PUT for the credential-redaction switch. The identifiers
        # (`api_credential_redaction_*`, `redaction_switch.set_enabled`) match the
        # scan, but the handler redacts nothing: it reads and writes a boolean on
        # the keystone and audits the change. The output it produces is that
        # boolean, never agent-authored text, so it is not an egress boundary.
        "dashboard/handlers/credential_redaction.py",
        # Normalizer, not an egress: `normalize_spawn_label` redacts a spawn's
        # display label BEFORE cutting it to one bounded line, so the cut cannot
        # leave a key fragment that the egress redactors would miss.
        # The label leaves the process only through sinks registered in security_posture_sinks_*.py (the
        # sub-agent frames and snapshots, which redact it again).
        "spawn_label.py",
    }
)

# The rest of the module lives in the owners below, lowest layer first. A name an owner
# binds is read from it, and a patch through this module reaches every module holding
# the name (:mod:`kiro_crew._facade_mirror`). Owners never import this module. A new
# redaction sink row is appended to the end of the security_posture_sinks_*.py part whose
# surface it belongs to; a new non-egress module goes in the set above.
_OWNERS = (
    "kiro_crew.security_posture_sinks_gateway",
    "kiro_crew.security_posture_sinks_delivery",
    "kiro_crew.security_posture_controls",
)
for _owner in _OWNERS:
    _importlib.import_module(_owner)
#: Name the facade serves but does not bind -> the owner that answers a read of it.
_EXPORTS: dict[str, str] = {}
_EXPORTS.update(_facade_mirror.install(_sys.modules[__name__], _OWNERS, excluded=()).served)
_sys.modules[__name__].__class__ = _facade_mirror.MirroredModule
__all__ = sorted(n for n in set(globals()) | set(_EXPORTS) if not n.startswith("_"))
if TYPE_CHECKING:  # the names an owner serves, for type checkers and IDEs
    import kiro_crew.validation as _validation  # noqa: F401
    from kiro_crew import security  # noqa: F401
    from kiro_crew import sel as _sel_mod  # noqa: F401
    from kiro_crew.executors import governance_executor  # noqa: F401
    from kiro_crew.security_posture_controls import (  # noqa: F401
        _AUDIT_SURFACE_DETAIL,
        _CONTROLS,
        _CREDENTIAL_FAMILIES,
        _EXFIL_HEURISTICS,
        _REDACTION_SINKS,
        _SCHEMA_REGISTRY_NAMES,
        PostureControl,
        PostureItem,
        _audit_surface_items,
        _control_payload,
        _credential_family_items,
        _denied_command_items,
        _exfil_heuristic_items,
        _own_namespace_prefixes,
        _redaction_sink_items,
        _sensitive_path_items,
        _suspicious_pattern_items,
        _token_auth_items,
        _tool_schema_items,
        _write_protected_items,
        build_posture_snapshot,
        build_posture_snapshot_async,
        logger,
        posture_counts,
        posture_counts_async,
    )
    from kiro_crew.security_posture_sinks_delivery import _DELIVERY_SINKS  # noqa: F401
    from kiro_crew.security_posture_sinks_gateway import _GATEWAY_SINKS  # noqa: F401
