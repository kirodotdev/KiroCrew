# Feature Map

One table per area, mapping every user-facing dashboard feature to **how a user
reaches it** and **where its code lives**. It exists so that "which page and
which handler own this?" is a lookup rather than a search, for a human or an
agent landing cold on a feature.

It is a **navigation index, not a contract**. Behavior contracts live in
[`../system-specs/`](../system-specs/README.md); this file says where to go and
deliberately says nothing about how a feature works.

## Maintenance contract

**A pull request that ADDS or REMOVES a feature updates this map in the same
PR. A pull request that only changes how an existing feature behaves does
not.** That line is drawn where it is because a map re-reviewed on every edit
stops being read: the cost has to land on the change that actually invalidates
a row.

`scripts/check_feature_map.py` enforces the mechanical half. It reads the
`base..HEAD` diff and fails only when a file is **added or deleted** under
`website/src/pages/` or `src/kiro_crew/dashboard/handlers/`, or a `<Route>`
entry is added or removed in `website/src/App.tsx`, while this file is
untouched. Editing existing files never trips it. The job row is in
[../ci/ci-and-reviews.md](../ci/ci-and-reviews.md).

The judgment half is reviewer-owned: the blocking `feature-map-correctness`
rule in root `AUTOSDE.yaml` verifies each changed row against the code diff and
rejects unrelated or cosmetic map churn. The mechanical check cannot tell
whether a row's *content* is still true, only that a structural change went by
without anyone looking at the map. When the check fires and the honest answer
is "no feature changed", update the row the new file belongs to so its columns
remain truthful; a whitespace or cosmetic edit does not count.

## How to read this

- **Reach it** is the user's path, written as the URL plus the tab or control
  that gets there. `?tab=` and `?view=` are real query parameters the page
  parses, not shorthand.
- **Page** is relative to `website/src/`.
- **Handler** is relative to `src/kiro_crew/dashboard/`. Two areas live outside
  that root and are written in full.
- **Endpoints** are 2–4 representative routes, not the complete set. The full
  table is `src/kiro_crew/dashboard/routes/` plus the direct registrations in
  `dashboard/server.py`; `test/test_dashboard_route_table.py` pins its order.
- `TODO(verify)` marks a cell nobody has confirmed against code. Fix it or
  leave it; never replace it with a guess.

Sidebar entries come from `website/src/surfaces/builtins.tsx`, which is the
authoritative nav table (label, group, badge, preview gate). A surface marked
`hiddenFromNav` there has a working route but no rail row — it is reached from
somewhere else, noted per row below.

---

## Chat and sessions

The default destination. `/chat` is the rail's **Sessions** row; everything in
this area is reached from inside it unless stated otherwise.

| Feature | What it is | Reach it | Page | Handler | Endpoints |
|---|---|---|---|---|---|
| Sessions | Multi-slot agent chat, one slot per conversation | `/chat/:slug?` — rail **Sessions** | `pages/ChatPage.tsx`, `pages/ChatSidebar.tsx`, `pages/chat/TranscriptScrollShell.tsx` (internal split of ChatPage — the transcript scroller skeleton, no new user-facing feature), `pages/chat/hoverHold.ts` (internal split of ChatSidebar — seat arithmetic for the hovered-row hold, no new user-facing feature) | `chat_handlers.py`, `ws.py` | `POST /api/chat`, `GET,POST /api/chat/slots`, `GET /api/ws` |
| Remote-bound session | A local session whose turns execute on a connected peer crew and stream back over its tunnel — local sidebar row, transcript and history, remote execution | New-chat menu → **New chat on crew** → pick a connected crew | `pages/ChatPage.tsx`, `pages/ChatSidebar.tsx`, `components/RemoteCrewChip.tsx` | `chat_handlers.py`, `handlers_instances.py`, `handlers/core.py`, `remote_relay.py`, `remote_mirror.py` | `POST /api/chat/slots` (`instance_id`), `POST /api/chat?relay=1`, `GET /api/instances/{id}/capabilities`, `GET /api/version` |
| Session folders | User-defined folders grouping session rows | Sidebar folder header → drag a row | `pages/chat/FolderPanel.tsx` | `chat_folders.py` | `GET,POST /api/chat/folders`, `PATCH /api/chat/slots/{slot}/folder` |
| Session tags | Colored labels on sessions, filterable | Sidebar row context menu → Tags | `pages/chat/SessionFlyout.tsx` | `chat_tags.py` | `GET,POST /api/chat/tags`, `PUT /api/chat/slots/{slot}/tags` |
| Pinned messages | Pin a message; pins panel per session | Message hover → pin; header pin count | `pages/chat/PinnedMessagesPanel.tsx` | `chat_pins.py` | `GET,POST /api/chat/pins`, `DELETE /api/chat/pins/{id}` |
| Share message as card | Turn an assistant reply into a branded PNG card + prefilled caption for X/LinkedIn; governed by `capabilities.social_share` (a policy pin withdraws the entry) | Message hover → More actions → Share as image | `pages/chat/share/ShareMessageModal.tsx`, `pages/chat/share/ShareCard.tsx` (helpers: `pages/chat/share/shareSupport.ts`) | `dashboard/social_share.py` (governance probe; card itself is client-side) | `GET /api/dashboard/config` (`social_share_enabled`) |
| Fork a session | Branch a new slot from an existing transcript | Session row menu → Fork | `pages/ChatSidebar.tsx` | `chat_fork.py` | `POST /api/chat/slots/{slot}/fork` |
| Rewind | Drop the transcript back to an earlier turn | Message action → Rewind | `pages/chat/AssistantMessage.tsx` | `chat_rewind.py` | `POST /api/chat/slots/{slot}/rewind` |
| Regenerate / variants | Re-run a turn, keep and switch between answers | Message action → Regenerate | `pages/chat/AssistantMessage.tsx` | `chat_regenerate.py` | `POST /api/chat/slots/{slot}/regenerate`, `.../switch-variant`, `.../edit-resend` |
| Session title | Auto-generated and hand-editable slot titles | Header title → click to rename | `pages/ChatSidebar.tsx` | `chat_title.py` | `POST /api/chat/slots/{slot}/generate-title`, `PATCH .../title` |
| Session summary | Right-panel rolling summary of the conversation | Chat right panel → **Summary** tab | `pages/chat/SessionSummaryTab.tsx` | `chat_handlers.py` | `GET,POST /api/chat/slots/{slot}/summary` |
| Side chat | A scratch sub-conversation beside the main turn | Chat right panel → **Side** | `pages/chat/SideChat.tsx`, `pages/chat/SidePanel.tsx` | `handlers/side.py` | `POST /api/chat/slots/{slot}/side/open`, `.../side/turn`, `.../side/close` |
| Channel mirroring | Mirror a session into Slack / Discord / other channel | Header channel menu → Link channel | `pages/chat/ChatSettings.tsx` | `chat_mirror.py`, `chat_slack.py` | `POST /api/chat/slots/{slot}/mirror-link`, `.../slack-link`, `GET /api/chat/channel-targets` |
| Voice reply and dictation | TTS on replies; streaming mic transcription | Composer mic; Settings → Voice | `components/ChatInput.tsx` | `chat_voice.py`, `stt_stream.py` | `POST /api/voice/synthesize`, `GET /api/ws/stt`, `POST /api/stt/transcribe` |
| Tool approvals | Approve or deny a tool call the agent proposes | Inline card in the transcript | `components/ApprovalCard.tsx` | `handlers/sessions.py` | `GET /api/approvals`, `POST /api/approvals/{id}/{action}` |
| Question cards | Agent asks a multiple-choice question in chat | Inline card in the transcript | `components/PendingQuestionCard.tsx` | `handlers/ask_question.py` | `POST /api/ask-question`, `GET /api/ask-question/pending`, `POST /api/ask-question/{ask_id}/answer` |
| Files in chat | Browse, attach and upload workspace files | Chat left rail → files; composer attach | `pages/chat/FileBrowserRail.tsx`, `pages/chat/FilesHomePanel.tsx` | `handlers/files.py` | `GET /api/file-read`, `POST /api/upload`, `POST /api/upload/file` |
| Terminal panel | Docked PTY beside or below the chat | Header terminal toggle | `components/BottomTerminalPanel.tsx` | `handlers/terminal.py` | `POST /api/terminal/sessions`, `GET /api/ws/terminal/{session_id}` |
| Browser panel | Live in-panel browser the agent drives | Right panel → **Browser** | `components/WebPreviewPanel.tsx` | `handlers/messaging.py` | `GET,POST /api/browser/view`, `POST /api/browser/command`, `POST /api/browser/command-result` |
| Notifications | Bell feed of agent-pushed notifications | Topbar bell → `/notifications` | `pages/NotificationsPage.tsx` | `handlers/messaging.py`, `handlers/notifications_push.py` | `GET /api/notifications`, `POST /api/notifications/ack`, `POST /api/notifications/push` |
| Crew Members | One durable pinned DM thread per crew member; the detail drawer lists the worker sessions the member is driving (live `slots` frames filtered on `created_by`) | `/members` — rail row when the crew preview is on | `pages/members/MembersPage.tsx` | `handlers/members.py`, `slot_projection.py` (`created_by`) | `GET /api/members`, `POST /api/members/{slug}/thread`, `GET /api/members/{slug}/activity`, `GET /api/ws` (`slots`) |
| Channels | Group rooms with several agents in one thread | `/channels` (builtin app surface) | `pages/ChannelPage.tsx` | `handlers_channel.py` | `GET,POST /api/channels`, `POST /api/channels/{id}/messages`, `POST /api/channels/{id}/agents` |

The Notifications surface is registered `hiddenFromNav`: its route and badge
stay wired, but it is entered from the topbar bell rather than a rail row.

## Agent Capabilities

One destination, pinned to the bottom of the rail, hosting nine tabs. Every tab
is a `?tab=` value on `/capabilities` (`pages/CapabilitiesPage.tsx`).

| Tab | What it is | Reach it | Page | Handler | Endpoints |
|---|---|---|---|---|---|
| Crews | Named agent bindings — which agent, model and workspace; per-crew custom avatar (hand-picked ghost traits or an uploaded picture, edited in the crew editor's avatar builder) | `/capabilities?tab=crews` | `pages/KiroCrewAgentsPage.tsx`, `components/CrewAvatarBuilder.tsx`, `components/CrewAvatar.tsx` | `handlers/agents.py` | `GET /api/agent/config`, `GET,PUT /api/config/default-agent`, `POST /api/agents`, `PUT,DELETE /api/agents/{name}`, `GET,POST /api/agents/{name}/avatar` |
| Agent Templates | The harness-level agent definitions crews bind to | `?tab=templates` | `pages/AgentsPage.tsx` | `handlers/agents.py` | `GET /api/agent/config`, `GET /api/config/schema` |
| Connections | MCP servers: install, sign in, enable, scope tools | `?tab=mcp` | `pages/connections/ConnectionsPage.tsx` | `handlers/mcp.py`, `handlers/connections.py`, `handlers/mcp_discover.py` | `GET /api/mcp`, `GET /api/mcp/discover`, `POST /api/connections/mint`, `POST /api/mcp/custom` |
| Skills | Installed skills, the public registry, pending candidates | `?tab=skills` | `pages/overview/SkillsTab.tsx` | `handlers/prompts.py`, `handlers/discover.py`, `handlers/skill_budget.py` | `GET,POST /api/skills`, `GET /api/skills/-/discover`, `GET /api/skills/-/pending` |
| Knowledge | The document library: sources, sync, entities, graph | `?tab=knowledge` | `pages/knowledge/index.tsx` | `handlers/knowledge.py` | `GET /api/knowledge/items`, `POST /api/knowledge/sources`, `GET /api/knowledge/graph` |
| Prompts | Reusable prompt entries from the registry | `?tab=prompts` | `pages/overview/PromptsTab.tsx` | `handlers/prompts.py` | `GET /api/prompts`, `GET /api/prompts/{name}` |
| Steering | Always-injected steering documents | `?tab=steering` | `pages/overview/SteeringTab.tsx` | `handlers/steering.py` | `GET,POST /api/steering`, `PUT,DELETE /api/steering/{key}` |
| Hooks | Event-triggered agent runs | `?tab=hooks` | `pages/HooksPage.tsx` | `handlers/hooks.py` | `GET,POST /api/hooks`, `PUT,DELETE /api/hooks/{hook_id}`, `GET /api/kiro-hooks` |
| Workflows | Saved dynamic-workflow definitions and their runs | `?tab=workflows` | `pages/overview/WorkflowLibraryTab.tsx` | `handlers/workflows.py` | `POST /api/workflows/run`, `GET,POST /api/workflows/definitions`, `POST /api/workflows/author` |

`/agents`, `/mc-agents` and `/connections` are legacy paths that redirect here;
`/knowledge` redirects to `?tab=knowledge`. `/hooks` also stands alone as a
full page.

## Memory, lessons and usage

Not a rail destination. The user-facing memory browser is a drill-in under
Settings → Overview; the graph visualizer is a Developer internals view.

| Feature | What it is | Reach it | Page | Handler | Endpoints |
|---|---|---|---|---|---|
| Memory browser | Preferences, projects, history, lessons, vector store | `/settings/overview?view=memory` | `pages/overview/MemoryTab.tsx` | `handlers/memory.py`, `handlers/cron.py` | `GET,PUT /api/memory/preferences`, `GET /api/memory/semantic`, `GET,POST /api/lessons` |
| Episodic search | Search past episodic memories | Memory browser → search | `pages/overview/MemoryTab.tsx` | `handlers/memory.py` | `GET /api/memory/episodic/search`, `GET /api/memory/episodic`, `DELETE /api/memory/episodic/{id}` |
| Embeddings | Enable the vector store and pick its model | Memory browser → vector card | `pages/overview/VectorMemoryCard.tsx` | `handlers/memory.py` | `GET /api/memory/embedding-status`, `POST /api/memory/enable-embeddings`, `POST /api/memory/embedding-model` |
| Memory graph | Entity/relation visualizer over the memory store | `/developer?tab=memory` | `pages/overview/MemoryGraphTab.tsx` | `handlers/memory.py` | `GET /api/memory/graph`, `GET /api/memory/observability` |
| Usage | Token and turn usage over time | `/settings/overview?view=usage` | `pages/overview/UsageTab.tsx` | `handlers/usage.py`, `handlers/telemetry.py` | `GET /api/usage`, `GET /api/usage/kiro`, `GET /api/usage/turns` |
| Portability | Export and import the whole memory/config bundle | `/settings/imports` | `pages/overview/PortabilityTab.tsx` | `handlers/portability.py` | `GET /api/portability/export`, `POST /api/portability/import`, `POST /api/portability/preview` |

## Schedules and loops

| Feature | What it is | Reach it | Page | Handler | Endpoints |
|---|---|---|---|---|---|
| Schedule | Cron jobs: recurring agent turns, scripts, commands | `/schedule` — rail **Schedule**; also created inline from the crew editor's "What wakes this crew" section (`/capabilities?tab=crews`) | `pages/SchedulePage.tsx`, `components/CrewWakeSection.tsx` | `handlers/cron.py` | `GET,POST /api/crons`, `DELETE /api/crons/{job_id}`, `GET /api/crons/history` |
| Cron secret grants | Owner-approved vault-secret env grants for script crons: agent requests via `cron_secret_request`, the owner approves/denies/revokes on the job's Secrets panel | `/schedule` → job → **Secrets** | `pages/SchedulePage.tsx` (`JobSecretsPanel`) | `handlers/cron.py` | `PUT /api/crons/{job_id}/secrets` |
| Monitor loops | Same-session bounded monitors and legacy nudge loops watching an external thing | Agent/API for bounded monitors; Chat header → legacy loop popover | `components/AutoNudgePopover.tsx` | `handlers/autonudge.py` | `GET,POST /api/monitors`, `PATCH /api/monitors/{id}`, `GET /api/monitors/slot/{slot_key}`, `POST /api/monitors/{id}/stop`, `POST /api/monitors/{id}/restart`, `GET,POST /api/autonudge`, `PATCH,DELETE /api/autonudge/{loop_id}` |
| Session ledger | Durable per-session work state surviving compaction | Agent-written; no dashboard page | — | `handlers/session_ledger.py` | `GET /api/session-ledger`, `POST /api/session-ledger/record` |
| Session control | Create / stop / send-to a session from outside it | Agent and app callers, not a UI | — | `session_control.py` | `POST /api/session-control/create`, `.../stop`, `.../send`, `GET .../read` |

## Artifacts

| Feature | What it is | Reach it | Page | Handler | Endpoints |
|---|---|---|---|---|---|
| Artifact library | Saved widgets, HTML and documents, versioned | `/artifacts` — rail **Artifacts** | `pages/ArtifactsPage.tsx` | `handlers/artifacts.py` | `GET,POST /api/artifacts`, `GET /api/artifact-folders`, `PATCH /api/artifacts/{slug}/folder` |
| Artifact detail | View, edit, version history, companion chat | `/artifacts/:slug` | `pages/ArtifactDetailPage.tsx` | `handlers/artifacts.py` | `GET,PATCH /api/artifacts/{slug}`, `GET /api/artifacts/{slug}/versions`, `GET /api/artifacts/{slug}/events` |
| Artifact comments | Threaded, anchored comments on an artifact | Artifact detail → select text → comment | `pages/ArtifactDetailPage.tsx` | `handlers/artifacts.py` | `GET,POST /api/artifacts/{slug}/comments`, `PATCH,DELETE .../comments/{comment_id}` |
| Remote artifacts | Provider-hosted docs browsed and commented in place | `/artifacts/remote/:provider/:externalId` | `pages/RemoteArtifactDetailPage.tsx` | `handlers/artifacts.py` | `GET /api/remote-artifacts/{provider}/browse`, `GET .../{external_id}`, `GET .../comments` |
| Publishing | Push an artifact out to a configured provider | Artifact detail → share menu | `pages/ArtifactDetailPage.tsx` | `handlers/artifacts.py` | `GET /api/artifacts/publish-providers`, `PATCH /api/artifacts/{slug}/sharing`, `DELETE .../publish` |
| Deploy | Ship a webapp artifact to a public URL on the user's AWS | `/deploy` (`/artifacts/deploy` redirects) | `pages/ArtifactDeployPage.tsx` | `src/kiro_crew/deploy/handlers.py` | `GET,PUT /api/deploy/config`, `GET,POST /api/deploy/profiles`, `POST /api/deploy/deploy` |

## Apps

Third-party and builtin apps that add their own pages, crons and MCP tools.

| Feature | What it is | Reach it | Page | Handler | Endpoints |
|---|---|---|---|---|---|
| Discover | Browse the app registries and install | `/apps` — **Apps** section header link | `pages/apps/DiscoverPage.tsx` | `src/kiro_crew/apps/routes.py` | `GET /api/apps/registry`, `POST /api/apps/registry/install`, `GET /api/apps/registries` |
| Updates | Apps with a newer version available | `/apps/-/updates` | `pages/apps/DiscoverPage.tsx`, `pages/apps/UpdatesList.tsx` | `src/kiro_crew/apps/routes.py` | `GET /api/apps`, `POST /api/apps/{name}/update` |
| Library | Installed apps, launchpad tiles, rail pinning | `/apps/library` | `pages/apps/LibraryPage.tsx` | `src/kiro_crew/apps/routes.py` | `GET /api/apps`, `POST /api/apps/{name}/enable`, `POST /api/apps/{name}/disable` |
| App detail | One app's manifest, config, permissions, uninstall | `/apps/detail/:name` | `pages/AppDetailPage.tsx` | `src/kiro_crew/apps/routes.py` | `GET /api/apps/{name}`, `GET /api/apps/{name}/manifest`, `GET /api/apps/{name}/config` |
| Installed app page | An app's own UI, served by the app | `/apps/:name` | `pages/AppPage.tsx` | app-owned | `POST /api/apps/{name}/token`, `POST /api/apps/{name}/open` |
| App migration | Move an app's data after a packaging change | `/apps/migrate/:name` | `pages/MigrationPage.tsx` | `src/kiro_crew/apps/routes.py` | `DELETE /api/apps/{name}/migrate-cleanup` |
| Builtin app surfaces | Top-level routes builtin apps claim | `/<app>` via the `/:builtinApp` catch-all | `apps/builtinRegistry.ts` → per-app page | per-app `backend/routes.py` | `POST /api/apps/<app>/...` per app |

`apps/builtinRegistry.ts` is the path→component table for builtin surfaces
(22 entries: Worlds, Channels, Auto Improvement, Auto Research, AWS Control,
File Explorer, Code Review Sage, Workflows, Dev Fleet, Issue Radar, Meetings,
Papyrus, PPTX Maker, Ops Mission Control, Design Critique, Crew Companion,
Task Runner, MD Notebook, Mochi, Spec Builder, Personal Shopper, Design Tweak).
Adding a builtin surface means an entry there plus `ui.pages` in the manifest —
`App.tsx` needs no change, which is why the router-delta half of the freshness
check cannot see it and the pages-dir half can.

## Task runner and subagents

| Feature | What it is | Reach it | Page | Handler | Endpoints |
|---|---|---|---|---|---|
| Task Runner | Autonomous multi-step runs from a spec | `/projects` — **Apps** group rail row | `pages/ProjectsPage.tsx` | `handlers/taskrunner.py`, `handlers_project.py` | `GET,POST /api/projects`, `POST /api/taskrunner/plan`, `POST /api/taskrunner/from-chat` |
| Task detail | One run's steps, gates and approvals | `/projects` → a project row | `pages/ProjectDetailPage.tsx` | `handlers_project.py` | `GET /api/projects/{id}`, `GET /api/activities`, `GET,POST /api/comments` |
| Review fix (PR) | Applies selected Code Review Sage findings on a PR in a candidate worktree, group by group, with user-confirmed apply, commit and push | `/code-review-sage` → a PR's Sage review → the Review Fix panel; the run opens at `/projects` | `components/ReviewFixTaskPanel.tsx` | `handlers/review_fix.py` | `GET /api/taskrunner/{task_id}/review-fix`, `POST .../review-fix/actions` |
| Review fix (Local) | Fixes selected findings from a Local review directly in the repository — no candidate task or `/projects` run; progress and results report back in the Local view | `/code-review-sage` → **Local** → select findings → **Fix selected** | `apps/code-review-sage/views/LocalReviewView.tsx` | `apps/code-review-sage/backend/routes.py` (`_handle_local_fix`) | `POST /api/apps/code-review-sage/local/fix`, `POST /api/apps/code-review-sage/local/sessions/{run_id}/disposition` |
| Spec refinement | Interactive tightening of a task spec before running | Task Runner → refine | `pages/ProjectsPage.tsx` | `handlers/taskrunner.py` | `GET,POST /api/taskrunner/refine`, `POST /api/taskrunner/refine/answer` |
| Subagents | Background agent runs spawned from a session | Chat activity viewer; rail badge | `pages/chat/ActivityViewer.tsx` | `handlers/messaging.py` | `GET,POST /api/spawn`, `POST /api/spawn/stop-all`, `GET /api/spawn/{agent_id}`, `POST /api/spawn/{agent_id}/steer` |
| Worktrees | Create a git worktree for a follow-up session | Chat follow-up card → new worktree | `pages/ChatPage.tsx` | `handlers/worktree.py` | `POST /api/worktree/create` |

## Settings

`/settings/*` is a splat route; `pages/SettingsPage.tsx` parses the trailing
segments itself (`segment[0]` = tab, `segment[1]` = sub-nav). Every row below
is `/settings/<key>`. Panels live in `pages/settings/`.

| Tab | What it is | Panel | Handler | Endpoints |
|---|---|---|---|---|
| `overview` | Health hero, stat cards, memory and usage drill-ins | `OverviewPanel.tsx` → `pages/OverviewPage.tsx` | `handlers_system.py` | `GET /api/status`, `GET /api/system` |
| `imports` | Import config and history from another tool | `ImportPanel.tsx` | `handlers/onboarding_import.py`, `handlers/portability.py` | `GET /api/onboarding/import/scan`, `POST /api/onboarding/import/apply` |
| `chat` | Chat behavior preferences | `ChatPanel.tsx` | `handlers/core.py` | `GET,PUT,PATCH /api/config/kirocrew` |
| `display` | Theme, density, language, plain vs highlighted diffs | `DisplayPanel.tsx` | `handlers/themes.py`, `handlers/core.py` | `GET,POST /api/themes`, `GET,PUT /api/config/theme` |
| `voice` | TTS voice and dictation engine | `VoicePanel.tsx`, `SttSettings.tsx` | `chat_voice.py`, `handlers/core.py` | `GET,PUT /api/voice/config`, `GET /api/voice/voices`, `GET,PUT /api/config/stt`, `GET /api/stt/status`, `POST /api/stt/ffmpeg/download` |
| `notifications` | Which events notify, and on which channel | `NotificationsPanel.tsx` | `handlers/messaging.py` | `GET /api/notifications/channels`, `PUT /api/notifications/channels/settings` |
| `shortcuts` | Keyboard shortcut reference and overrides | `ShortcutsPanel.tsx` | `handlers/files.py` | `GET,PUT /api/dashboard/config` |
| `skills` | Skill enablement and context budget | `SkillsPanel.tsx` | `handlers/prompts.py`, `handlers/skill_budget.py` | `GET /api/skills`, `GET /api/skills/-/budget` |
| `channels` | Slack, Discord, Telegram, WhatsApp, Teams, and more | `ChannelsPanel.tsx` + one panel per provider | `handlers/messaging.py` | `GET,PUT /api/slack/config`, `GET,PUT /api/discord/config`, `GET /api/slack/manifest` |
| `browser` | Install playwright-cli, attach token, engine choice | `BrowserPanel.tsx` | `handlers/messaging.py` | `GET,POST /api/browser/install`, `PUT /api/browser/token`, `POST /api/browser/engine` |
| `computer-use` | Enable and scope native desktop automation | `ComputerUsePanel.tsx` | `handlers/computer_use.py` | `GET,PUT /api/computer-use/config`, `POST /api/computer-use/invoke` |
| `webhooks` | Inbound webhook tokens and contexts | `WebhooksPanel.tsx` | `handlers/hooks.py` | `GET /api/webhooks`, `POST /api/webhooks/tokens`, `POST /api/webhooks/test` |
| `instances` | Remote Kiro Crew instances to connect to | `RemoteCrewPanel.tsx`, `InstancesPanel.tsx` | `handlers_instances.py`, `handlers_cloud.py` | `GET,POST /api/instances`, `POST /api/instances/{id}/connect`, `GET /api/cloud/preflight` |
| `privacy` | Telemetry disclosure and opt-out | `PrivacyPanel.tsx` | `handlers/telemetry.py` | `GET /api/telemetry/collection`, `GET /api/telemetry/beacon` |
| `security` | Denied commands, sensitive paths, approval posture | `SecurityPanel.tsx`, `PostureDisclosure.tsx` | `handlers/security.py`, `handlers/tailnet.py` | `GET /api/security/denied-commands`, `PATCH .../builtins/{id}`, `POST .../user` |
| `secrets` | Stored credentials the agent may use | `SecretsPanel.tsx` | `handlers/secrets.py` | `GET,POST /api/secrets`, `DELETE /api/secrets/{name}` |
| `developer` | Pointer into the developer surfaces | `DeveloperPanel.tsx` | — | — |
| `releases` | Release channel, update check, changelog | `ReleasesPanel.tsx` | `handlers/updates.py` | `GET /api/update/check`, `POST /api/update`, `GET /api/changelog`, `GET /api/releases` |
| `about` | Version, build, diagnostics bundle | `AboutPanel.tsx`, `ReportProblemCard.tsx` | `handlers/diagnostics.py`, `handlers/feedback.py` | `POST /api/diagnostics/collect`, `GET /api/diagnostics/download/{filename}` |

Instances is deliberately not a rail row: it is set up here once and switched
from the header tab strip. Webhooks carries both a preview flag and
`hiddenFromNav`, so it surfaces as this Settings tab rather than a rail row.

## Developer

`/developer` (`pages/DeveloperPage.tsx`), eleven `?tab=` values. Internals views;
not where a user manages their own data.

| Tab | What it is | Page | Handler | Endpoints |
|---|---|---|---|---|
| `logs` | Live gateway log stream and level control | `pages/LogsPage.tsx` (`LogViewer`) | `handlers/updates.py` | `GET /api/logs`, `GET,POST /api/logs/level` |
| `system` | Host runtime, services, sessions, performance | `pages/SystemPage.tsx`, `pages/system/` | `handlers_system.py`, `handlers/session_storage.py` | `GET /api/system`, `GET /api/system/session-storage` |
| `telemetry` | Startup timings and context traces | `pages/TelemetryPanel.tsx` | `handlers/telemetry.py` | `GET /api/telemetry/startup`, `GET /api/telemetry/context-trace` |
| `storage` | Raw localStorage inspector | `pages/LocalStorageDebug.tsx` | — (client only) | — |
| `mcp-pool` | MCP connection pool state | `pages/settings/McpManagement.tsx` | `handlers/mcp.py` | `GET /api/mcp/active`, `GET /api/mcp/scopes`, `POST /api/mcp/probe` |
| `memory` | Memory graph visualizer | `pages/overview/MemoryGraphTab.tsx` | `handlers/memory.py` | `GET /api/memory/graph` |
| `config` | Raw Kiro Crew and agent config editors | `pages/overview/KiroCrewCfgTab.tsx`, `AgentCfgTab.tsx` | `handlers/core.py`, `handlers/agents.py` | `GET,PUT,PATCH /api/config/kirocrew`, `GET,PUT /api/agent/config` |
| `agent-backend` | Which agent harness backend is live | `pages/developer/AgentBackendTab.tsx` | `handlers/acp_backend_status.py`, `handlers/kiro_prerequisite.py` | `GET /api/acp-backends`, `GET /api/kiro-prerequisite` |
| `feature-previews` | Toggle unreleased surfaces on | `pages/developer/FeaturePreviewsTab.tsx` | — (client flags) | — |
| `debug-tools` | Diagnostic overlays, currently the chat scroll inspector | `pages/developer/DebugToolsTab.tsx` | — (client only) | — |
| `archive` | Consolidated session archive browser | `pages/SessionArchive.tsx` | `handlers/sessions.py` | `GET /api/session/archive`, `GET /api/session/archive/{name}` |

## Standalone operator surfaces

| Feature | What it is | Reach it | Page | Handler | Endpoints |
|---|---|---|---|---|---|
| Logs | Full-page log viewer | `/logs` | `pages/LogsPage.tsx` | `handlers/updates.py` | `GET /api/logs`, `GET /api/stream` |
| Hooks | Full-page hook manager | `/hooks` | `pages/HooksPage.tsx` | `handlers/hooks.py` | `GET,POST /api/hooks`, `GET /api/kiro-hooks` |
| Webhooks | Inbound webhook tokens, contexts, run history | `/webhooks` (preview-gated) | `pages/WebhooksPage.tsx` | `handlers/hooks.py` | `GET /api/webhooks`, `POST /api/webhooks/tokens`, `POST /api/hooks/agent` |
| Cloud launch | Provision a Kiro Crew EC2 instance in the user's account | Settings → Instances → Cloud | `pages/settings/InstancesPanel.tsx` | `handlers_cloud.py` | `GET /api/cloud/preflight`, `POST /api/cloud/launch`, `GET /api/cloud/iam-policy` |
| Mobile connect | Pair a phone to this gateway | Settings → Instances → mobile card | `pages/settings/MobileLoginCard.tsx` | `handlers/mobile_connect.py`, `handlers/tailnet_mobile.py` | `GET /api/mobile-connect/methods`, `POST /api/auth/mobile-link`, `POST /api/tailnet/mobile/qr` |
| Kiro sign-in gate | KAS-mode sign-in without kiro-cli: Google/GitHub via loopback (local) or device code (remote), Builder ID and company SSO via device code | Not yet mounted (pre-integration): the API and the `KasLoginGate` component exist, but no production route renders the gate until KAS mode is wired into the app shell | `components/KasLoginGate.tsx` | `handlers/kas_login.py` | `GET /api/kas-login`, `POST /api/kas-login/device`, `POST /api/kas-login/loopback`, `POST /api/kas-login/poll`, `POST /api/kas-login/cancel`, `POST /api/kas-login/logout` |
| Source-provider review | PR state, checks and review threads in the Changes panel | Chat right panel → **Changes** | `components/PullRequestPanel.tsx`, `components/CommentThreads.tsx` | `handlers/source_providers.py` | `POST /api/source/pull-request`, `.../checks`, `.../status`, `.../resolve` |
| OpenAI-compatible API | Chat-completions shim for external clients | External clients only | — | `openai_compat.py` | `POST /v1/chat/completions` |

## Popouts and embeds

Separate top-level route trees `App.tsx` selects before the dashboard chrome
renders, so nothing in them carries the sidebar.

| Route | What it is | Page |
|---|---|---|
| `/popout/chat/:slug?` | One session in its own OS window | `pages/PopoutFrame.tsx` |
| `/popout/artifact/:slug` | One artifact in its own window | `pages/ArtifactPopoutFrame.tsx` |
| `/popout/terminal` | The docked terminal, detached | `pages/TerminalPopoutFrame.tsx` |
| `/embed/chat/:slug?` | Chat embedded in a host surface | `pages/ChatPage.tsx` (`embedded`) |
| `/embed/sessions` | Session list embedded in a host surface | `pages/ChatPage.tsx` (`embedded`) |
| `/embed/settings` | Reduced settings for an embedded host | `pages/EmbedSettingsPage.tsx` |

## Redirects

Kept so old bookmarks and deep links still resolve. Adding a feature never
means adding a row here; retiring one usually does.

| From | To |
|---|---|
| `/knowledge` | `/capabilities?tab=knowledge` |
| `/agents`, `/mc-agents` | `/capabilities` |
| `/connections` | `/capabilities?tab=mcp` |
| `/overview` | `/settings/overview` |
| `/instances` | `/settings/instances` |
| `/artifacts/deploy` | `/deploy` |
| `/orchestrated/:slug?` | `/chat/:slug?` (orchestrator slot) |
| `/tasks` | Task Runner |
| anything unmatched | `/chat` |
