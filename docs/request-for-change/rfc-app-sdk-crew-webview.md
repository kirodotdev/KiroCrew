---
title: App SDK for the Crew Webview — app templates, live data, a lifecycle feed, lane publishing, and app skills for every crew
status: draft
author: iamwhatever
created: 2026-09-27
last-audited: 2026-09-28
audited-at: dceaca3a95
doc-pr:
implementation-prs: []
tracking-issues: []
supersedes: []
superseded-by: []
---
# RFC: App SDK for the Crew Webview

- Status: draft. Nothing in this document is built. Each of the five capabilities below lands as its own implementation PR after the design is accepted, and each PR carries its own App Kit doc update.
- Author: iamwhatever
- Created: 2026-09-27
- Related: `rfc-app-session-controls.md` (the precedent for a manifest field that lands with its reader), `rfc-everything-is-an-app.md` (the declared-but-unread manifest field trap this document avoids), `rfc-app-sandbox-isolation.md` (the app trust model), `docs/app-kit/manifest-reference.md` and `docs/app-kit/api-reference.md` (the contracts the implementation PRs extend)

## Summary

The Crew Webview is the drawer on the Crew page where a crew publishes JSON data into a human-authored HTML template (`src/kiro_crew/agent_panel.py`). An App Store app can make that drawer much more useful, and today it can only do so by writing a file into the operator's template directory. This RFC turns what one real app needed into five generic App SDK capabilities: four for the Crew Webview, and one for the skill the app ships beside its template.

| # | Capability | Manifest / SDK surface | Replaces |
|---|---|---|---|
| 1 | App-contributed templates | `contributes.crewWebviewTemplates[]`, template ids `<app>.<id>` | an `on_startup` hook writing into `panel-templates/` |
| 2 | Live data push | a `postMessage` handshake between host and template | a frame remount on every publish |
| 3 | Lifecycle feed | `permissions.lifecycle`, `ctx.lifecycle.subscribe()`, frame lifecycle messages | the crew spending model tokens to report its own state |
| 4 | Lane publishing | `panel_publish` from any session bound to the crew, stored as a lane | the `no_dashboard_slot` refusal |
| 5 | App skills for every crew | enabled apps' skills join every crew member agent's skill scope | a skill only the built-in `kirocrew` agent sees |

The sandbox (`allow-scripts` only) does not change, and the widget CSP does not change. An app template gets a narrower CSP of its own, without the two script CDNs (§3.4). Every capability that runs or feeds app code sits behind an owner decision: the app execution trust grant for templates (§3.4), install-time consent for any lifecycle scope wider than the app's own sessions, backend or frame (§5.3, §5.4), and a per-crew switch for app skills (§7).

## 1. Motivating consumer

[iamwhatever/xiaoya-crew-app](https://github.com/iamwhatever/xiaoya-crew-app) gives every crew a small companion in its webview that shows what the crew is doing (thinking, working, worried about a red check, proud when done). It ships a template, a fallback template and a skill, and works with zero core change. Each of its workarounds maps to one capability here:

- It installs its template by having `on_startup` write `xiaoya.html`, carrying an ownership marker on the first line, into `agent_panel.override_templates_dir()` (`xiaoya/hooks.py` `install`). It needs an execution trust grant only to run that hook. Capability 1 removes the hook.
- Its `on_shutdown` swaps in `fallback.html` instead of deleting the file, because a stored template id with no file makes every drawer read of that crew answer 503 (`xiaoya/hooks.py` `on_shutdown`). It cannot tell a disable from an uninstall or a gateway stop. Capability 1 defines the withdrawn state in core.
- Its skill tells the crew to call `panel_publish` whenever its state or mood changes (`xiaoya/skills/xiaoya-react/SKILL.md`). Each reaction costs model tokens, and each publish remounts the frame, so the companion's animation restarts. Capabilities 2 and 3 fix both.
- A crew that fans work out to subagents cannot show their progress, because those sessions cannot publish. Capability 4 fixes that.
- Its skill reaches a crew running the built-in `kirocrew` agent and does not reach a crew running a custom agent such as `kirocrew-worker`, so that crew has a companion template and no instructions for feeding it (§2.5). Capability 5 fixes that.

The design is generic. Nothing below names Xiaoya, a pet, or a mood.

## 2. Current behaviour

Verified on origin/main at the commit named in `audited-at`.

### 2.1 Templates

- A template is resolved from two roots, operator override first: `override_templates_dir()` (`<data home>/panel-templates/`) then `shipped_templates_dir()` (`src/kiro_crew/agent_panel_templates/`, which holds `default.html` and `kirocrew-pipeline-conductor.html`). See `resolve_template`.
- A template id must match `TEMPLATE_ID_RE` (lowercase, digits, single dashes, no dots). `template_for_crew` gives a crew the template whose id equals its slugified name, else `default`. `available_templates` lists both roots and backs the `panel_templates` MCP tool (`api_agent_panel_templates` in `dashboard/handlers/agent_panel.py`).
- The module docstring states the trust split: the template is "HTML a human wrote and reviewed", and only the data comes from the crew. `panel-templates` is sealed read-only for a sandboxed shell (`sandbox._CREW_READONLY_LEAVES`), write-protected for agent file tools (`security/paths.py`, the `panel-templates` entry), and refused when it is a symlink (`_real_dir_under_data_home`, `sandbox._CREW_NO_ALIAS_LEAVES`).
- Composition happens on READ: `render_record` calls `resolve_template` for the stored id on every drawer open, and `compose` fills `DATA_MARKER` with an inert `application/json` island (`kirocrew-panel-data`). A stored id that does not resolve raises `PanelError("unknown_template")`, which `_read_and_compose` turns into a 503 `panel_render_failed` from `api_member_panel`.
- App Kit has no template contribution. `Contributes` in `apps/manifest.py` holds `commands`, `sessionControls`, `panelTabs` (chat side-panel tabs, unrelated to the Crew Webview) and `fileMenuItems`.

### 2.2 Frame updates

- `api_agent_panel_publish` ends with `state.broadcast_ws("panel_published", {"slug": slug})`. The frame is slug-only so the ownership digest never reaches a client.
- `useWebSocket.ts` handles `panel_published` by invalidating the `['member-panel', slug]` query. `CrewWebview.tsx` refetches, the composed `html` changes, `buildSrcdoc` produces a new document, and `useSandboxDoc` mints a new single-use `/sandbox-doc/` URL. The iframe's `src` changes, so the frame reloads and all template state (timers, animation, scroll) is lost.
- The frame runs with `CREW_WEBVIEW_SANDBOX = "allow-scripts"` and the CSP from `cspFor` in `lib/widgetSrcdoc.ts`, which `buildSrcdoc` injects: `default-src 'none'`, `connect-src 'none'`, `form-action 'none'`, `img-src data: blob:`, and `script-src 'unsafe-inline'` plus the vendored Tailwind runtime and two CDNs (`https://cdn.jsdelivr.net`, `https://cdnjs.cloudflare.com`). A script element pointed at either CDN is an outbound GET whose URL the frame controls, so the frame has one network reach beyond navigation. That CSP also does not block the frame navigating itself; the navigation guard in `components/McpAppFrame.tsx` says so and counts `load` events for that reason.
- There is no host-to-template message channel on this surface. Widget frames post to the parent (`mc-widget-height`, `mc-widget-action` in `widgetSrcdoc.ts`); nothing posts into a crew webview.

### 2.3 Lifecycle events

- `apps/event_bus.py` `EventBus` is publish-only: an app broadcasts its own events, checked against `permissions.events`, under the fixed WS type `app_event`. An app backend cannot subscribe to anything.
- The WS scope gate for app tokens (`dashboard/ws_event_scope.py`) already classifies turn lifecycle frames as slot-scoped (`_SLOT_SCOPED_EVENTS`: `chat_status`, `chat_done`, `tool_call`, `tool_result`, `approval`, `approval_resolved`, and the subagent frames), with visibility decided by `_slot_visible` / `_subagent_visible` over the `slots:*` and `subagent:*` declarations. These frames carry full content (message text, tool payloads).
- Mochi's pet state machine understands `tool_call`, `approval_required`, `approval_granted`, `approval_rejected` and more (`apps/builtins/mochi/pet_state_machine.py`), but the machine lives in the gateway and gets nothing from it. Its panel page reconstructs the events from dashboard WS frames and POSTs each one back to its own backend (`reportPetEvent` in `apps/mochi/panel/panelBridge.ts`, whose comment says "no seam publishes chat lifecycle to an app"). This only works while that page is open. The crew companion app does the same from `apps/crew-companion/sessionWatch.ts`.
- `approval_resolved` is not always slot-keyed: `interaction_coordinator.py` adds `slot` only when the session key is non-empty and not `state`. Mochi's hooks document the consequence (the `note_chat_lifecycle` docstring in `apps/builtins/mochi/hooks.py`).
- `hooks.py` `HOOK_EVENTS` (`AgentSpawn`, `UserPromptSubmit`, `PreToolUse`, `PostToolUse`, `Stop`) are user-authored script hooks run per event. They are not an app surface and are out of scope.

### 2.4 Who may publish

- `_resolve_publishing_crew` in `dashboard/handlers/agent_panel.py` requires the internal secret, `agent.crew_panel` on, a non-app caller (`_deny_app_caller`), a recognised non-restricted session, and then a dashboard slot. A session with no slot is refused `no_dashboard_slot`. The comment there names the case: a subagent inheriting its parent's member selection resolves fine, and is refused only by that check.
- The crew is read from the live allocation (`SessionAllocationService.get_agent_selection` in `session_allocation.py`, which returns `("member", capability_member)`), never from the request body.
- The MCP side refuses first: `mcp_panel._strict_session_key` routes through `mcp_core.require_strict_session_key`, and its refusal text says subagents inherit no session identity of their own.
- Ownership is the exact crew name: `publish` stores `crew_key(crew)` (SHA-256 of the exact name) and, inside the record lock, refuses `crew_slug_collision` when a live different owner holds the slug. `api_member_panel` re-checks the digest on read.
- A crew member's settings live in `KiroCrewAgentConfig` (`config/sections.py`, one entry per crew under `agents`), which holds, among others, `kiro_agent`, `workspace`, `memory_store`, `model` and `reasoning_effort`. It has no template field. That section lives in `config.json`, which the agent file-edit gate write-protects but the OS sandbox leaves writable to a shell, so it cannot hold an owner-only decision. Owner-only controls such as `computer_use.json` live instead in `_CREW_SECRET_LEAVES` (`security/paths.py`, read- and write-protected on the tool path) and in `sandbox._CREW_READONLY_LEAVES` (read-only for the shell), with the dashboard handler as the sole writer.

### 2.5 App skills

- `_register_skills` in `apps/bridges.py`, called from `register_app`, installs an app's skills by symlinking each declared skill directory into the crew skills root twice: namespaced as `skills/<app>/<skill>` and flat as `skills/<skill>` (the flat link is skipped for reserved names). `deregister_app` removes them.
- Whether a session sees those skills is decided by `_skills_injection_plan` in `context.py`, the single seam shared by session-start injection and post-compaction re-injection. It injects the catalog for the default `kirocrew` agent, and for a custom agent only when `agent_skill_globs` (`agent_discovery.py`) finds `skill://` mappings in that agent's spec, restricted to those globs.
- So a custom agent with no mapping gets no skill catalog at all, and a mapped one gets only what its spec names. An app's spec cannot name the data-home path its skills land in; the `app_skills_dir` docstring in `apps/bridges.py` records this and hands the path to apps that render their agent prompt at runtime. An app that ships a skill for crews in general has no way to reach a crew whose agent is custom.

## 3. Capability 1 — app-contributed templates

### 3.1 Manifest

```json
"contributes": {
  "crewWebviewTemplates": [
    {"id": "companion", "path": "templates/companion.html", "title": "Companion", "lifecycle": true}
  ]
}
```

- `id` matches `TEMPLATE_ID_RE`. `path` is relative to the app root and validated by the same helpers every other manifest path uses (`_is_rooted_path`, `_has_dotdot_segment`, `_path_escapes_app_root`). It must end in `.html`, stay under a per-file byte cap (proposed 512 KiB, well under the 4 MiB the sandbox document channel accepts), and contain `DATA_MARKER`. All three are install-time validation errors, so the author sees them before a crew does.
- `title` is display text for the template picker. `lifecycle` opts the template into capability 3's frame messages and defaults to `false`.
- At most 8 entries per app, unique ids, with the same malformed-input flags `Contributes` already carries for `commands` (`bad_*`, `dropped_*`) so a typo is an error rather than a silent drop.
- The field is named `crewWebviewTemplates`, not `panelTemplates`, because `contributes.panelTabs` already names a different surface.

### 3.2 Ids and resolution

- An app template's id is qualified: `<app>.<id>`, e.g. `companion-app.companion`. `TEMPLATE_ID_RE` forbids dots, so a qualified id can never equal a shipped or operator id, and no bare id can reach an app root.
- `resolve_template` gains one branch: a qualified id resolves only through the gateway's registry of enabled apps' templates. Bare ids resolve exactly as today (operator, then shipped).
- `available_templates` adds the qualified ids of enabled apps for the owner's member settings picker. The agent-facing `panel_templates` tool keeps listing bare ids only, matching what `panel_publish` accepts from it.
- A crew remembers a default template in its member settings: `panel_template` (empty by default), which may hold a bare or a qualified id. It is an owner-only control, so it is not stored in `config.json`. It lives in a new gateway-owned record, `crew_member_owner_settings.json`, one entry per crew keyed on the exact crew name, added to `_CREW_SECRET_LEAVES` beside `computer_use.json` (§2.4) for the tool path, and to `sandbox._CREW_READONLY_LEAVES` for the shell, mirroring `computer_use.json` in both lists; `_CREW_SECRET_LEAVES` alone is consulted only by the tool gate, so a shell `open()` would otherwise still write it. The dashboard member settings handler is its only writer; no agent, shell or app can set it.
- `panel_publish` without `template` resolves, in order: the crew's `panel_template` when set, then `template_for_crew` exactly as today (the template whose id equals the crew's slug, else `default`). An explicit `template` argument still wins, but it accepts bare ids only: the agent writes that argument, and a qualified id would let the agent, not the owner, put third-party code into the drawer. A qualified id in the argument is refused `app_template_owner_only`.
- `template_for_crew` stays bare-only. An app never becomes a crew's default by naming a template after the crew; only the owner's `panel_template` reaches an app template.
- A `panel_template` naming an app template that is withdrawn or changed falls back as in §3.5, and the member settings page shows the same notice beside the field.
- One pattern names a qualified id (`<app name>.<TEMPLATE_ID_RE>`), and every site that checks a stored or owner-set template id accepts exactly a bare or a qualified id: `publish`'s own guard (which refuses `bad_template_id` on a dot today), `resolve_template`, `available_templates`, `read`, and the crew-log fold. A qualified id outgrows every 64-character template-id clamp on those paths (the crew-log fold's `PANEL_TEMPLATE_LIMIT` among them), so P1 raises them together. The agent-facing `template` argument keeps its cap, because it accepts bare ids only.

### 3.3 Serving from the bundle

- The bytes are read from the app's own resource root (`bridges._app_resource_root`, which resolves a builtin to its package and a third-party app to its installed directory). Nothing is written to `panel-templates/`.
- The registry is built at enable and cleared at disable and uninstall, beside the existing skill and MCP registration in `bridges.register_app` / `bridges.deregister_app`.

### 3.4 Trust gate

An app template is third-party code that runs in the sandboxed frame. The frame cannot fetch, but it can navigate itself and it can load a script from the two CDNs in `script-src` (§2.2), so a template can carry out anything it can read: the crew's published data, and with capability 3 its lifecycle kinds. Two controls follow:

- A template resolves only while its app is enabled and `apps/execution.app_execution_denied` answers `None` for that app. Builtins pass; a third-party app needs the same execution trust grant (`agent.apps_trusted` / `agent.apps_trusted_local`, `agent.apps_trusted_repositories`, or `agent.apps_allow_third_party`) its hooks would need. A sandboxed template counts as app code for admission, because it can navigate its frame and carry out the data it reads. One gate, so a hook and a template cannot drift to different defaults, which is the stated purpose of `execution.py`.
- An app template's document is built with its own CSP: the widget policy minus the two CDNs, so `script-src` holds only `'unsafe-inline'` and the vendored Tailwind runtime. This is a second `cspFor` caller for app templates only; widgets, operator templates and shipped templates keep today's policy. Navigation stays open, so the trust grant is still the control that decides whether third-party code sees the data at all.
- `agent.crew_panel` still governs the whole surface.
- The drawer's "Contained" bar names the source of an app template ("Template from the <app> app"), so the operator can tell a reviewed operator template from a third-party one.

### 3.5 Disable, uninstall, and the stale panel

A crew's stored record keeps its qualified template id when the app goes away, and composition on read would then fail. Instead:

- `resolve_template` raises a coded `app_template_withdrawn`.
- `render_record` catches exactly that code and composes the shipped `default` template with the same stored data. `default.html` renders any data object, and the data was scrubbed at publish, so nothing new is exposed.
- `api_member_panel` adds `notice: {"code": "app_template_withdrawn", "app": "<app>"}` to the response, and the drawer shows one line: the app is off, so the plain view is shown.
- Re-enabling the app restores the full template on the next read with no republish, because composition is on read.

This is the behaviour the motivating app hand-builds with `fallback.html`, done once in core for every app and every exit path.

## 4. Capability 2 — live data without a reload

### 4.1 Handshake

Opt-in by the template, so `default.html`, the conductor template and every operator template keep today's behaviour untouched.

1. The template, after reading its island at load, posts `{"type": "kirocrew-panel:ready", "v": 1}` to `parent`.
2. `CrewWebview` accepts it only when `event.source === iframe.contentWindow` and the frame has not navigated. The navigation guard is the one `McpAppFrame.tsx` already uses: the first `load` is ours, and any later `load` or the pre-navigation signal marks the channel dead until a fresh mint. The pre-navigation signal comes from a bootstrap `mcpAppSrcdoc.ts` injects and `buildSrcdoc` does not, so P2 adds the same bootstrap to the crew webview document; without it the channel would have only the load-count backstop and the pre-`load` window that bootstrap exists to close.
3. On the next `panel_published` refetch, the host posts `{"type": "kirocrew-panel:data", "v": 1, "data": …, "lanes": …}` to `contentWindow` and does not remint only when all three hold: the document on screen sent `kirocrew-panel:ready`, the navigation guard has not marked its channel dead, and the new `panel.template` and `panel.template_digest` equal those of the document on screen. In every other case it remints as today, so a template that never opts in, one that has not finished the handshake, and a dead channel all get a fresh document on every publish.
4. The template updates its DOM from the message with the same text-only rendering it uses for the island.

### 4.2 What does not change

- `CREW_WEBVIEW_SANDBOX` stays `allow-scripts`, and `cspFor` is untouched. `postMessage` is not governed by `connect-src`.
- The target origin is `'*'`, which an opaque-origin frame requires; confidentiality rests on posting only to this iframe's own `contentWindow` behind the navigation guard, the same argument `McpAppFrame.tsx` records.
- The message carries exactly the two islands' content: `data` is the `panel.data` the route already returns beside `html`, from the same record snapshot (`_read_and_compose`), and `lanes` is what the `kirocrew-panel-lanes` island (§6.2) carries. No other record field reaches the frame.

### 4.3 Server change

`api_member_panel` adds `template_digest` (SHA-256 of the resolved template HTML) to `panel`, computed from the same read. The host needs it to know that only data changed; comparing composed HTML would make the drawer a parser of template markup, which that route's docstring rules out.

## 5. Capability 3 — lifecycle feed

### 5.1 One projector at the broadcast seam

Every lifecycle frame already passes through `DashboardState.broadcast_ws`, and the WS gate already classifies those types. The feed is a projector registered at that seam which maps a frame to a minimal event or to nothing. One seam instead of new calls at each emitter, so a new emitter cannot be missed.

| Kind | From frame | Fields beyond the common ones |
|---|---|---|
| `turn.started` | the first `chat_status` of a turn | none |
| `turn.ended` | `chat_done` | `continuing` (bool, from `chat_done_payload`) |
| `tool.called` | the first `tool_call` for a `tool_call_id` | `tool` (name only), `tool_call_id` |
| `tool.finished` | `tool_result` | `tool_call_id`, `tool` (joined from its `tool.called`; the frame carries only the id and the output) |
| `approval.required` | `approval` | `approval_id`, `tool` |
| `approval.resolved` | `approval_resolved` | `approval_id`, `approved`, `expired` |

Both source frames repeat within one step: `chat_orchestrator.py` re-broadcasts `chat_status` every `_SA_STATUS_EVERY_SECS` during a subagent wait, and `chat_runner.py` sends `tool_call` again on `EVENT_TOOL_CALL_UPDATE` for the same `tool_call_id`. The projector emits each `turn.started` and `tool.called` once per turn and per call.

Common fields: `kind`, `ts`, `slot`, `origin` (the slot's origin class). The projector maps only the slot frames in the table; the `subagent_batch_*` frames are not mapped, so a subagent's steps reach the feed only as its parent slot's turn and tool events. An `approval_resolved` frame without `slot` (§2.3) belongs to an empty or `state` session, so there is no slot to fill; the projector drops it, and no app subscriber receives a slotless event. The owner still sees it on the dashboard as today.

Never included: message text, thinking, tool arguments, tool output, approval purpose text, file paths, crew names. `tool` passes through the same `redact_credentials` and `redact_exfiltration_urls` pair `EventBus._redact_value` applies. Crew names are withheld because apps are isolated from member surfaces (`_deny_app_caller`, `_OWNER_ONLY_EVENTS`); crew scoping happens only in §5.4, inside the host.

### 5.2 Backend subscription

```python
async def on_startup(ctx):
    ctx.lifecycle.subscribe(handle)   # present only when permissions.lifecycle is declared

async def handle(event: dict) -> None:
    ...
```

- `ctx.lifecycle` is `None` unless the manifest declares `permissions.lifecycle`, the same way `ctx.events`, `ctx.spawn` and `ctx.job` are populated only on declaration (`apps/context.py` `AppContext`).
- Delivery never blocks the broadcast: each app gets one bounded queue (proposed 256 events) drained by one task. On overflow the oldest events are dropped and the next delivered event carries `dropped: <count>`.
- The subscription ends with the app: the drain task is cancelled on disable and shutdown by the lifecycle dispatcher that runs `on_shutdown` (`apps/lifecycle.py` `LifecycleDispatcher`).
- Subscribe and unsubscribe are SEL-audited per app; per-event delivery is not, matching the dedup posture of `ws_event_scope._audit_decision`.

### 5.3 Scope

`permissions.lifecycle` is a list drawn from two kinds of scope. The slot scopes are the strings the WS gate already defines: `slots:own` (the default), `slots:user`, `slots:app:<name>` (with that app's `exposeToApps` consent), and `slots:all`; they govern the backend feed. The one other value is `crew:frame` (§5.4); it governs only the frame feed, and it can appear beside any slot scope without widening it. The `subagent:*` family is not accepted here, because the projector maps no subagent frames (§5.1). For the slot scopes the decision is made by the same `_slot_visible`, so an app sees exactly the sessions over the feed that it would see over its WS token.

On the WS token those scopes are self-declared; `ws_event_scope.py` calls them a structuring mechanism plus an audit trail, not a barrier. The lifecycle feed does not inherit that posture for anything wide:

- `slots:own` needs no consent: it covers only sessions the app itself started.
- Every wider scope (`slots:user`, `slots:app:<name>`, and `slots:all`) needs the owner's consent at install or update time. The install dialog lists the declared lifecycle scopes in plain words ("sees when any of your chats start, stop, call a tool or ask for approval"), and the grant is recorded gateway-side per app, in a file the app and agents cannot write, SEL-audited.
- A declared scope the owner did not grant delivers nothing and the subscription reports it in `ctx.lifecycle.scopes`. An update that widens the declared scopes asks again; it never inherits the earlier grant.

A Mochi-style companion declares `slots:user` and asks for it at install.

### 5.4 Lifecycle into the crew's own frame

This is the zero-token path for a webview companion. The frame feed carries one crew's lifecycle into app code, which is wider than `slots:own`, so it takes the same consent as a backend scope. A template may declare `"lifecycle": true` (§3.1) only when its manifest also declares the scope `crew:frame` in `permissions.lifecycle`; the install dialog discloses it with the §5.3 scopes ("sees when a crew that uses this template starts, stops, calls a tool or asks for approval"), and the template picker shows the flag beside `title`. `crew:frame` is not a slot scope and never reaches `_slot_visible`: `CrewWebview` checks the app's recorded install-time grant for `crew:frame` before forwarding anything. For a template whose `crew:frame` grant the owner gave and that has completed the §4.1 handshake:

- The gateway sends the dashboard owner a new frame `crew_lifecycle` `{member, kind, ts, tool?}` for sessions whose allocation is bound to a crew, where `member` is the crew's exact name from that allocation. It is classified in `_OWNER_ONLY_EVENTS`, so no app token receives it, and the owner already sees crew names. It is keyed on the exact name, not the slug, because `members.slug_for_name` is not guaranteed unique and `crew_slug_collision` only refuses a second writer once a record exists.
- `CrewWebview` forwards events only when `member` equals the exact crew name the drawer was opened for, as `{"type": "kirocrew-panel:lifecycle", "v": 1, "kind": …, "ts": …, "tool": …}`, over the channel from §4.
- The frame learns what kind of step its own crew is on, and nothing about any other crew.

The companion then animates from the feed, and the crew publishes only when it has something worth saying.

## 6. Capability 4 — lane publishing from non-slot sessions

### 6.1 Who may publish

The rule becomes: the crew's own dashboard thread writes the panel's main document, and every other session bound to that crew writes a lane.

- Everything in `_resolve_publishing_crew` before the slot check stays: internal secret, `agent.crew_panel`, `_deny_app_caller`, `_recognize_session`, restricted-session refusal.
- A session without a slot is accepted when `get_agent_selection` returns `("member", <crew>)`. That value is gateway-held allocation state, inherited at spawn, and never read from the caller, so a session can only reach the crew it was spawned under. A session whose selection is a template is still refused `no_crew`.
- The MCP side needs a strict identity for such a session. The refusal text in `mcp_panel._strict_session_key` says `spawn_run` children carry none today. A gateway-issued identity for spawned children is a primitive other surfaces will reuse, and how it is minted, carried, and revoked is its own design, so it is out of this document: it goes to a separate RFC, and lane publishing (P4) waits for that RFC to be accepted and built. Nothing in P1–P3 or P5 depends on it.

### 6.2 Lanes

- The record gains `lanes`, keyed by a lane id the gateway derives from the caller's session key (a short digest), never from the body. Each lane holds `{label, data, published_at}`, where `label` is a short gateway-made name (`Lane <n>`, numbered by arrival). It is never the session's title, because a session title can be the first user message, and lanes reach app templates.
- A lane publish replaces only its own lane, inside the same record lock and after the same exact-crew `crew_key` check `publish` already performs. A lane can never write the main `data`, and the thread can never be overwritten by a child.
- Caps: lanes have a per-lane size, a lane count, and a total budget that keeps the main `data` plus every lane under the existing `_MAX_RECORD_BYTES` ceiling; the numbers are the implementation PR's. A lane publish that would pass the count or the budget evicts the oldest lanes by `published_at` until it fits, so a lane publish never makes the crew's own record write fail with `record_too_large`.
- Lifetime: lanes are kept until the crew's next main-panel publish, which drops all of them. A fan-out's final lanes therefore stay on screen after its children finish, until the crew's own thread says something new.
- `compose` adds a second inert island, `kirocrew-panel-lanes`, only when lanes exist. A template that does not read it is unaffected; `default.html` gains a plain list of lanes under its main content.
- The `panel_published` frame is unchanged, and §4's data message carries `lanes`.

## 7. Capability 5 — app skills reach every crew member

An enabled app's skills reach every crew member's agent, built-in or custom, unless the owner turns that off for a crew.

- The default `kirocrew` agent is unchanged: it already sees the whole catalog, app skills included, and keeps `only=None`.
- For a custom agent, `_skills_injection_plan` in `context.py` returns a restriction today, and both consumers spend it as one (`only=skill_globs or None`, and `discovery_only=not lazy_skills and not skill_globs`). Under the default `skills.lazy_load=True`, `discovery_only` is `False` either way, so for an unmapped custom agent the app globs only decide which skills the lazy index lists. P5 adds one glob per skill an enabled app registered to that restriction: the resolved real path of that skill's directory inside the app bundle, as `_register_skills` in `apps/bridges.py` links it. `_matches_any` compares real filesystem paths, so the glob is absolute, and because both the namespaced and the flat link resolve to that same bundle path, it matches whichever link catalog deduplication keeps: a mapped custom agent gets its own mapping plus the app globs, and an unmapped custom agent, which gets no skills today, gets the app globs only, never the whole catalog. The plan's first return value turns true for an unmapped custom agent exactly when the app globs are non-empty.
- One seam for injection, so session-start injection and post-compaction re-injection cannot disagree, which is that function's stated purpose. `skill_search` resolves its scope through a second resolver, `session_skill_globs` in `agent_discovery.py`, so P5 adds the same app globs there too; the two share one helper that lists them, so they cannot drift.
- Owner control is per crew: `app_skills` (default `true` for new crews; see Upgrade below for existing custom-agent crews) is stored in the same owner-only `crew_member_owner_settings.json` record as `panel_template` (§3.2), set from the member settings page beside it. `false` keeps a crew's agent at its own mapping.
- Upgrade: a crew that exists when P5 lands and runs a custom agent starts with `app_skills: false`, so nothing new reaches its context without the owner. The member settings page shows a one-time opt-in card on each such crew ("Let this crew use skills from your enabled apps"), and the CHANGELOG entry for P5 names it. Crews created after P5, and crews on the default agent, which already see app skills, start with `true`.
- The default is on because the owner already made the per-app decision by enabling the app, and a skill is text the agent loads on demand; the per-crew switch covers an owner who enables a companion for one crew and wants another crew's agent kept narrow.

## 8. Phase plan

Each phase is one implementation PR, merged after this RFC is `accepted`, and updates in the same PR both its App Kit doc and the system spec that owns the code it changes.

| Phase | Capability | Main code | Docs updated | Done when |
|---|---|---|---|---|
| P1 | App templates, qualified ids, execution gate, narrower CSP, withdrawn fallback, member default template | `apps/manifest.py`, `apps/bridges.py`, `agent_panel.py`, `handlers/agent_panel.py`, `crew_log/entry_types.py` (`PANEL_TEMPLATE_LIMIT`), `sandbox.py` (owner-settings leaf), `security/paths.py` (`crew_member_owner_settings.json` in `_CREW_SECRET_LEAVES`), the member settings handler, `lib/widgetSrcdoc.ts`, `CrewWebview.tsx` notice | `manifest-reference.md` (`contributes.crewWebviewTemplates`), `docs/system-specs/modules/app-kit-platform.md`, `docs/system-specs/modules/security.md` | a crew with `panel_template` set to an enabled, trusted app template renders it; disabling the app renders `default` with the notice |
| P2 | Live data handshake | `CrewWebview.tsx`, `lib/widgetSrcdoc.ts` (pre-navigation bootstrap), `handlers/agent_panel.py` (`template_digest`) | `api-reference.md` (Crew Webview template protocol) | an opted-in template keeps its state across publishes; `default.html` still remints on every publish |
| P3 | Lifecycle feed and frame lifecycle | broadcast projector, `apps/context.py`, `apps/lifecycle.py`, `ws_event_scope.py`, `CrewWebview.tsx` | `manifest-reference.md` (`permissions.lifecycle`, template `lifecycle` flag), `api-reference.md` (`ctx.lifecycle`, lifecycle message), `docs/system-specs/modules/app-kit-platform.md` | one `turn.started` per turn through a subagent wait; an ungranted scope delivers nothing |
| P4 | Lane publishing (needs the spawned-child identity RFC) | `handlers/agent_panel.py`, `agent_panel.py`, `mcp_panel.py`, `default.html` | `api-reference.md` (lanes island and lane message fields) | a child's publish lands in its own lane; a 17th lane or a full budget evicts the oldest; the next main publish drops all lanes |
| P5 | App skills for every crew | `context.py` (`_skills_injection_plan`), `agent_discovery.py` (`session_skill_globs`), `apps/bridges.py` (registered-skill path list), `app_skills` in `crew_member_owner_settings.json` | `manifest-reference.md` (`skills`: who sees them), `src/kiro_crew/docs/agent-spec-fields.md` (skill injection table), `docs/system-specs/modules/memory-skills-hooks.md` | an unmapped custom agent sees an enabled app's skill and nothing else; `app_skills: false` removes it; the PR description names the default-on for new crews for reviewers |

P1 is first because P2 and P3's frame half need a template worth updating live. P3's backend half is independent of P1 and P2. P4 waits for the separate spawned-child identity RFC and is otherwise independent of the rest on the server; it uses P2 for live lane updates. P5 is independent of all of them and can land first. The motivating app migrates after P3: it drops its hooks and its fallback file, and its skill keeps only the one short line the crew chooses to say.

## 9. Non-goals

- No change to the frame sandbox, the widget CSP, or the `/sandbox-doc/` mint.
- No app writes into any crew's panel data. An app contributes layout and receives lifecycle kinds; data still comes only from the crew.
- No change to operator or shipped templates, or to how `template_for_crew` picks a default when a crew has no `panel_template`.
- No change to what app frontend pages already receive over the dashboard WebSocket.

## 10. Security considerations

- Egress from an app template: navigation and nothing else (§3.4). The narrower CSP removes the CDN script route; the execution trust grant decides whether third-party code runs at all.
- What app code can learn: the crew's scrubbed published data (templates), and text-free lifecycle kinds only under a scope the owner granted at install (§5.3, §5.4). No message text, tool arguments or output reach any app surface.
- Who can pick an app template or turn app skills on for a crew: the owner, through `panel_template` and `app_skills` in member settings. Both live in `crew_member_owner_settings.json` on the keystone floor (`_CREW_SECRET_LEAVES`), not in agent-writable `config.json`, and the dashboard handler is the only writer.
- The template trust gate reuses `app_execution_denied`, whose inputs (`agent.apps_trusted` and its siblings) sit in `config.json` today. That placement is shared with app hooks and is not widened here; moving the app trust grant to the keystone floor is a separate change that would cover hooks and templates together.
- Who can publish a lane: only a session whose gateway-held allocation names the crew, after the same exact-name ownership check as a main publish (§6).

## 11. Open questions

- App bundle files are not integrity-pinned after install today, for hooks, skills and MCP servers alike, and app templates inherit that posture here. Pinning bundle files would be a platform change covering every app surface, not a template-only one.
