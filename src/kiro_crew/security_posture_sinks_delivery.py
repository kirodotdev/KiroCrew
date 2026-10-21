"""The redaction egress paths of messaging channels, file and MCP panels, and apps.

The second part, in registry order, of ``security_posture._REDACTION_SINKS``: each row
names an output boundary, the module that redacts it, and how. ``security_posture_controls``
joins it after ``security_posture_sinks_gateway``. The two parts are a contiguous cut of the
registry at its first Slack row, each named for the surface most of its rows cover, so a few
rows sit on the other side of the cut. Append a new row to the end of the part whose surface it
belongs to.

Part of :mod:`kiro_crew.security_posture`, the import path callers, tests and docs use: it
reaches every name here, and a patch through it reaches this module too
(:mod:`kiro_crew._facade_mirror`).
"""

from __future__ import annotations

_DELIVERY_SINKS: tuple[tuple[str, str, str], ...] = (
    (
        "Slack messages",
        "slack/handler_runtime/delivery.py",
        "StreamRedactor on the live edit stream plus a full pass on the final posted " "message.",
    ),
    (
        "Ops Mission Control Slack board",
        "apps/builtins/ops_mission_control/backend/slack_out.py",
        "Incident titles, resources, and diagnoses mirrored to an ops channel. A "
        "separate egress boundary from the Slack messages sink: this text originates in a "
        "third-party provider's alarm payload (not a model turn) and lands in a "
        "channel whose audience is usually wider than the dashboard's.",
    ),
    (
        "Slack cron / notification posts",
        "slack/gateway_runtime/subagent_injection.py",
        "Job names, cron results, and error strings before they reach a channel.",
    ),
    (
        "Subagent results",
        "subagent_parts/run_output.py",
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
        "Onboarding import",
        "onboarding_scan.py",
        "Text imported from a foreign agent's config (instructions, memories, skills, "
        "settings and the other import categories) before it enters Kiro Crew.",
    ),
    (
        "Discord / Telegram / WeCom / Webex",
        "messaging/driver.py",
        "The channel-neutral TurnDriver's StreamRedactor — every non-Slack chat "
        "channel inherits redaction from this one egress.",
    ),
    (
        "Hook replies and memory refusals (shared channel pipeline)",
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
        "classification and the egress one is the load-bearing half. Member-memory "
        "refusals also remove local paths before truncation and channel delivery.",
    ),
    (
        "Outbound raster payloads",
        "messaging/outbound_files.py",
        "Exact raster bytes go through platform.binary_content_is_flagged -- the "
        "one binary-content scan every file-delivery gate shares -- before upload, "
        "so this leg and those gates give one answer to one question.",
    ),
    (
        "Slack member-memory refusal",
        "slack/transport_dispatch.py",
        "Member-memory errors bypass the streamed response. Credentials, "
        "exfiltration URLs and local paths are removed before the bounded "
        "refusal is sent to the channel.",
    ),
    (
        "Discord direct send",
        "discord/transport_dispatch_commands.py",
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
        "telegram/renderer_display.py",
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
        "Outbound channel messages — send_message post leg",
        "dashboard/messaging_api/proactive_send.py",
        "The Slack post of a proactive send and the in-place edit: the text and Block Kit "
        "blocks are display-redacted before they are posted to Slack.",
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
        "apps/routes_uninstall.py",
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
        "Feishu answer body",
        "feishu/renderer.py",
        "The answer body Feishu renders as markdown. `text()` is what `on_done` "
        "sends, and Feishu is a buffered zero-widget channel: its body reaches "
        "`render_options_as_text` -> `apply_options_cap`, which display-redacts the "
        "numbered OPTIONS choices but returns the body itself unscrubbed, so the "
        "only pass the body had was the driver's channel-neutral LITERAL stream "
        "scan. A credential split by emphasis (`AKIA**REST`) or a link therefore "
        "survives that scan and is reassembled when Feishu renders the markdown. "
        "The body is screened at the send boundary through `Renderer.redact_for_"
        "target` -- the `redact_for_display` pass over the credential + "
        "exfiltration-URL chain, on the display form that actually ships. `text()` "
        "is where the scrub lands because `text()` is what `on_done` sends; history "
        "is persisted separately by `messaging.dispatch` from the driver's own "
        "accumulated text, so this pass covers the shipped bytes without touching "
        "the transcript.",
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
        "Unattended channel egress",
        "messaging/egress.py",
        "Text pushed to a non-Slack channel without a renderer: the dashboard reply "
        "mirror, subagent and cron results, and the compaction notice. Control tags "
        "are stripped, the text is redacted in display form through "
        "redact_via_context, and each transport chunk is seam-checked with the same "
        "redactor before send.",
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
        "broadcast. (Apart from the owner's file read and diff on the next row, "
        "this module's other redact() calls are upload GATES — they abort a send "
        "when redaction would alter the content — not egress.)",
    ),
    (
        "File viewer read and diff for the owner",
        "dashboard/handlers/files.py",
        "The file body returned by the dashboard's file read (api_file_read) — "
        "the chat side panel's Files tab, and the Library and Artifacts session-"
        "document previews and re-reads that use the same route — and the "
        "original and diff bodies returned by the matching file diff "
        "(api_file_diff), which the panel compares that buffer against. Both "
        "scanners run for every caller EXCEPT the dashboard owner with the "
        "credential-redaction switch turned off (Settings → Security → "
        "Credential redaction in file views): then these two responses run the exfil-URL "
        "pass alone, in the owner's own dashboard only, with one verdict per "
        "request so neither side is raw while the other is masked. The switch "
        "defaults to on, "
        "is stored on the keystone floor the agent cannot write, and every flip "
        "is audited.",
    ),
    (
        "File watch frames",
        "dashboard/file_api/transfer.py",
        "The file-watch SSE frames (api_file_watch) are redacted before they are "
        "pushed. The file download and the Range stream serve the file's own "
        "bytes and use redact() as a GATE: a file whose text the redactor would "
        "change is refused, not served masked.",
    ),
    (
        "Office document preview",
        "dashboard/file_api/office_preview.py",
        "The text, blocks and slides the Office preview (api_file_office_preview) "
        "returns are redacted before they are capped.",
    ),
    (
        "Spreadsheet preview",
        "dashboard/file_api/sheet.py",
        "The cell values and formula text the spreadsheet preview "
        "(api_file_sheet) returns are redacted.",
    ),
    (
        "Project branch label",
        "dashboard/file_api/project_dirs.py",
        "The branch name, short HEAD and project path the branch label "
        "(api_project_git) returns are redacted; the path goes through "
        "_redact_project_path, which the Git panel and the file tree share.",
    ),
    (
        "Git panel status and log",
        "dashboard/file_api/git_panel.py",
        "The branch, file paths, commit messages and authors the Git panel's "
        "status and log (api_project_git_status, api_project_git_log) return are "
        "redacted; file paths go through redact_path_segments.",
    ),
    (
        "Project file tree",
        "dashboard/file_api/project_tree.py",
        "The root the Files tab's tree listing (api_project_tree) returns goes "
        "through _redact_project_path, and every listed path is redacted segment "
        "by segment through redact_path_segments.",
    ),
    (
        "Slide-deck render screen and conversion diagnostic",
        "dashboard/handlers/office_slides.py",
        "The .pptx/.ppt slide renderer screens the deck's rendered text through "
        "the credential redactor then the exfiltration-URL redactor BEFORE it "
        "rasterizes any page (`_screen_rendered_text`): a slide PNG is a picture "
        "of that text and cannot be redacted after the fact, so a deck whose "
        "visible text either scanner would change is not rendered at all -- the "
        "manifest answers `content_redacted` and the panel falls back to the "
        "text outline, which the file-preview route redacts per slide. The same "
        "credential-then-URL chain also scrubs the conversion child's stderr "
        "tail before it is logged or returned as `detail`. Both scanners run "
        "over the full text; a scanner that raises drops the field rather than "
        "emitting it, and the render is refused rather than served unredacted.",
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
        "dashboard/handlers/mcp_probe.py",
        "Cached MCP probe results returned by the dashboard HTTP API to the browser. "
        "Configured header values and reflected credentials in probe errors receive "
        "redaction before they cross that boundary.",
    ),
    (
        "MCP apply errors",
        "dashboard/handlers/mcp.py",
        "Uninstall and agent-rebuild errors the apply route returns: URL, then credential.",
    ),
    (
        "MCP server metadata",
        "mcp_discovery_server_info.py",
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
        "skills_review.py",
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
        "already covers, so it is redacted the same way. Routed through redact_via_context for the same companion-seam "
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
        "dashboard/chat_tags_vocabulary.py",
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
        "Peer crew text",
        "dashboard/peer_redaction.py",
        "Every string a connected peer crew sends that this dashboard renders, "
        "broadcasts or persists: proxied replies (JSON documents and SSE events), "
        "capability labels and session-list fields. The peer is a SEPARATE "
        "machine with its own agents, environment and secrets, so its text can "
        "quote a credential or an exfiltration URL that no local redaction pass "
        "has ever seen. The peer redacts its own copy with this same chain, but a "
        "hub that trusted that would inherit whatever an older or misconfigured "
        "peer failed to scrub -- so every such string runs the credential + "
        "exfiltration-URL chain here, through the one sink.",
    ),
    (
        "Host-side UI preference backup",
        "ui_prefs.py",
        "Every value read back from `~/.kiro/crew/ui-prefs.json` before it is "
        "served by `GET /api/ui-prefs` and written into the renderer's "
        "localStorage. The values are opaque strings the server never parses, "
        "and the file sits in the agent-writable data home, so a credential or "
        "exfiltration URL smuggled inside an innocent key would otherwise reach "
        "the dashboard verbatim. A value the credential + exfiltration-URL chain "
        "would alter is DROPPED rather than served redacted: a redacted "
        "preference is not one the client can use, and the sync loop would "
        "write the sentinel back over the file.",
    ),
    (
        "WakaTime coding-activity heartbeats",
        "wakatime/heartbeats.py",
        "The project label sent as the heartbeat entity and project fields when "
        "WakaTime send-heartbeats is enabled. The label is the agent/user-selected "
        "project directory basename, so a directory whose name is itself "
        "credential- or URL-shaped would otherwise be POSTed verbatim to WakaTime. "
        "The basename passes through the shared credential + exfiltration-URL chain "
        "in `_entity_for_project` before it can leave for the external API.",
    ),
    (
        "Crew webview panels",
        "agent_panel.py",
        "Everything a crew publishes into its drawer webview: every string in the "
        "`data` object -- KEYS as well as values, because the template renders a "
        "key as a heading -- plus the panel title. A panel is assembled unattended "
        "from issue bodies, review comments and command output, so it is untrusted "
        "text on a path that ends at the operator's dashboard, both in the native "
        "docked summary and inside the composed sandbox document. Each string is "
        "Unicode-sanitized BEFORE the chain, because a redactor matches the "
        "characters it is handed: an invisible character inside a token hides it "
        "from both patterns and is then dropped downstream, which would store the "
        "joined-up secret with redaction skipped. The chain runs at "
        "PUBLISH rather than at render, so a credential never enters `panel.json` at "
        "all: it cannot be read back by a later reader, cannot survive in halves "
        "across the record's byte ceiling, and is not left on disk for the next "
        "reader that forgets to scrub.",
    ),
    (
        "Member event-log projection broadcast",
        "eventlog/service.py",
        "Folded member projection views pushed to the browser over the "
        "member-projection WS on every change. A projection carries "
        "agent-authored free-text (an activity record's `project`, message "
        "previews) that would otherwise cross to the dashboard verbatim, the "
        "same class the sibling `/activity` HTTP read redacts. "
        "`_redact_projection_value` runs the shared exfiltration-URL then "
        "credential chain over the view before broadcast, plus the "
        "URL-secret-parameter layer the sibling roster egress runs, so a short "
        "credential carried as a URL query parameter cannot slip the two "
        "heuristics; it is applied at the "
        "network boundary rather than the fold so the stored projection keeps "
        "its raw value for server-side folds while nothing leaves unredacted.",
    ),
    (
        "Process-tree diagnostics",
        "diag/procs.py",
        "The command line, working directory and four environment values of every "
        "process in the Kiro Crew family, rendered into the read-only process view "
        "that answers the dashboard and the debug MCP tool. Every one of those "
        "fields belongs to the process being inspected rather than to this code, so "
        "each passes the shared credential redactor BEFORE it is clipped to its "
        "retention cap -- the same order the sinks above state, and for the same "
        "reason: a cut taken first can halve a secret into a fragment neither "
        "pattern matches, which ships the surviving prefix in clear. The "
        "environment map is those four named keys and no others, and is omitted "
        "entirely unless a caller asks for it.",
    ),
    (
        "MCP launch approval display records",
        "mcp_gateway/launch_approval.py",
        "Command and argument display values persisted in the sealed approval store "
        "and returned by dashboard status. Both shared redactors run before storage, "
        "so raw launch values do not enter durable operator-facing state.",
    ),
    (
        "Mate-written dashboard field values",
        "dashboard_agentic.py",
        "Every string a mate writes into its own dashboard's agentic fields, on "
        "its way to the rendered page. The scrub runs on the way IN, so what the "
        "registry stores is already redacted and no later reader has to remember to "
        "scrub it. Both shared redactors run, exfiltration URLs first, and the walk "
        "is recursive over arrays and objects rather than a named list of fields, "
        "because a template declares its own agentic fields and a value's strings sit "
        "inside containers as often as they stand alone. An object's KEY is scrubbed "
        "beside its value, since a page draws a key as a label in the same characters "
        "it draws the value. The type and size questions are asked again about the "
        "scrubbed result, because a value that fit before the scrub can be over the "
        "cap after it.",
    ),
    (
        "Field values pushed to a mate's dashboard page",
        "dashboard/handlers/member_dashboard_fields.py",
        "Every subscribed field's value on its way to the owner's socket and into the "
        "page's data island. This is the ONE place such a value is produced -- the "
        "initial read and every later patch both come out of `_values` -- which is what "
        "makes one scrub here cover both rather than each sender remembering. The "
        "redactor is handed in by the route rather than chosen here, so the module "
        "applies the gateway's own redactor and cannot substitute a weaker one.",
    ),
)
