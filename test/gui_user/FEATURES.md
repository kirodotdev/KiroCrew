# GUI user-test feature inventory

The backlog for `.github/workflows/gui-user-test.yml`: every user-visible feature of the
dashboard, written as the scenario the lane would drive, so scenario batches can be cut
by feature and by priority instead of by whoever remembered a page. This file is RENDERED
from `features.json` by `features_catalog.py` -- edit the JSON, then run
`python test/gui_user/features_catalog.py --write`; `test_features_catalog.py` fails when
the two disagree. Generated from `docs/feature-map/README.md`, `website/src/pages/**`,
`website/src/surfaces/builtins.tsx`, the settings registry, the builtin apps and
`docs/system-specs/modules/**` at commit `2f71d47e6c06`, then deduped by route + flow. It is an
inventory, not a contract: a row says what a person can do and where, the scenario YAML
under `scenarios/` says how the lane checks it.

## How to read a row

- **Priority**: P0 = newly merged UI or a core daily path, first scenario batch; P1 = other
  smoke-tier flows; P2 = nightly-tier flows; P3 = not generated (see *Not generated*).
- **Runnable**: `smoke` = core path in about five model actions (PR + nightly); `nightly` =
  runs on the lane's target (Xvfb + Chromium, fake ACP backend, no login, no network);
  `native-only` = needs the Electron shell; `needs-secret` = needs a real external account
  or credential; `excluded` = not observable through pixels or would take the target down.
- **Seed**: the `KIROCREW_HOME` fixture the target boots from (`kirocrew gateway --seed`).
- **Steps**: estimated model actions; the scenario's `max_steps` should sit a little above.

## Counts

| | Total | smoke | nightly | native-only | needs-secret | excluded |
|---|---|---|---|---|---|---|
| **All features** | 268 | 42 | 165 | 12 | 27 | 22 |
| Chat sessions (`chat`) | 26 | 3 | 19 | 0 | 2 | 2 |
| Side panel tabs (`side-panel`) | 4 | 1 | 3 | 0 | 0 | 0 |
| Terminal panel (`terminal`) | 3 | 0 | 3 | 0 | 0 | 0 |
| Sessions sidebar & folders (`sidebar`) | 16 | 5 | 10 | 0 | 1 | 0 |
| Routing & redirects (`navigation`) | 10 | 0 | 10 | 0 | 0 | 0 |
| Top bar (`topbar`) | 3 | 0 | 3 | 0 | 0 | 0 |
| Search everywhere & command palette (`search`) | 1 | 1 | 0 | 0 | 0 | 0 |
| Crew Members (`members`) | 2 | 0 | 2 | 0 | 0 | 0 |
| Agent capabilities (crews, templates, skills, prompts, steering, hooks, workflows) (`capabilities`) | 14 | 2 | 12 | 0 | 0 | 0 |
| Connections (MCP servers & services) (`connections`) | 2 | 1 | 0 | 0 | 1 | 0 |
| Memory, lessons & usage (`memory`) | 6 | 1 | 4 | 0 | 1 | 0 |
| Knowledge library (`knowledge`) | 7 | 1 | 5 | 0 | 0 | 1 |
| Artifacts (`artifacts`) | 10 | 1 | 5 | 0 | 3 | 1 |
| File viewer & project files (`files`) | 1 | 1 | 0 | 0 | 0 | 0 |
| Browser panel (`browser-panel`) | 3 | 0 | 1 | 1 | 0 | 1 |
| Apps & App Store (`apps`) | 26 | 3 | 12 | 2 | 7 | 2 |
| Task Runner (`task-runner`) | 1 | 1 | 0 | 0 | 0 | 0 |
| Worlds (3D scenes) (`worlds`) | 1 | 0 | 1 | 0 | 0 | 0 |
| Dev Fleet (`dev-fleet`) | 1 | 0 | 1 | 0 | 0 | 0 |
| Schedule (cron jobs) (`schedule`) | 6 | 2 | 4 | 0 | 0 | 0 |
| Headless API surfaces (`api`) | 5 | 0 | 0 | 0 | 0 | 5 |
| Inbound webhooks (`webhooks`) | 2 | 0 | 2 | 0 | 0 | 0 |
| Chat channel integrations (`channels`) | 15 | 1 | 6 | 1 | 7 | 0 |
| Voice (`voice`) | 4 | 0 | 1 | 0 | 2 | 1 |
| Notifications (`notifications`) | 5 | 2 | 0 | 1 | 0 | 2 |
| Computer Use (`computer-use`) | 3 | 0 | 1 | 2 | 0 | 0 |
| Multi-instance shell (`instances`) | 2 | 1 | 0 | 0 | 1 | 0 |
| Remote instances & cloud launch (`remote-instances`) | 4 | 0 | 1 | 0 | 1 | 2 |
| Popouts & embeds (`popout`) | 6 | 0 | 4 | 2 | 0 | 0 |
| Authentication & sign-in (`auth`) | 3 | 2 | 0 | 0 | 0 | 1 |
| Onboarding (`onboarding`) | 4 | 0 | 4 | 0 | 0 | 0 |
| Settings (`settings`) | 47 | 12 | 28 | 3 | 0 | 4 |
| Themes (`themes`) | 3 | 0 | 2 | 0 | 1 | 0 |
| Security & governance (`security`) | 10 | 1 | 9 | 0 | 0 | 0 |
| Developer tools (`developer`) | 12 | 0 | 12 | 0 | 0 | 0 |

Priorities: P0 10 · P1 35 · P2 162 · P3 61. Deduped from 387 raw records.

## Chat sessions (`chat`)

| Priority | Id | User story | Start URL | Seed | Runnable | Steps |
|---|---|---|---|---|---|---|
| P0 | `chat-sessions` | As a dashboard user, I want a multi-slot agent chat with one slot per conversation and pinned rows on top, so that I can run and return to several conversations at once. | `/chat` | sessions-a-few | smoke | 4 |
| P0 | `chat-subagents` | As a user, I want to see background agent runs spawned from a session with their status and a rail badge, so that I can follow delegated work. | `/chat` | sessions-a-few | nightly | 4 |
| P1 | `chat-session-title` | As a user, I want auto-generated slot titles I can rename inline, so that sessions are recognisable in the sidebar. | `/chat` | sessions-a-few | smoke | 3 |
| P1 | `chat-turn-stats-footer` | As a user, I want each finished reply to show how long it took, so that I can gauge responsiveness. | `/chat` | minimal | smoke | 3 |
| P2 | `chat-collapse-message-input` | As a reader, I want to put the composer away and restore it from a labelled bar showing my draft's first line, so that a long reply gets the whole pane. | `/chat` | sessions-a-few | nightly | 4 |
| P2 | `chat-deep-link-slug` | As a returning user, I want a bookmarked /chat/<key> URL to open that session, so that I can resume work directly. | `/chat` | sessions-a-few | nightly | 3 |
| P2 | `chat-incognito-badge-and-history-exclusion` | As a privacy-conscious user, I want incognito/temporary sessions labelled and kept out of history and memory surfaces, so that private chats leave no trace. | `/chat` | incognito-mix | nightly | 5 |
| P2 | `chat-knowledge-picker-attach` | As a user, I want to pick knowledge items to include in a turn, so that the agent answers from my documents. | `/chat` | memory-populated | nightly | 4 |
| P2 | `chat-long-history-pagination` | As a user with long sessions, I want earlier messages to load on demand, so that big transcripts stay responsive. | `/chat` | sessions-long-history | nightly | 4 |
| P2 | `chat-mcp-tools-panel` | As a user, I want to see which MCP tools the session can call, so that I understand the agent's capabilities. | `/chat` | connections-two | nightly | 3 |
| P2 | `chat-pinned-messages` | As a user, I want to pin a message and see a per-session pins panel, so that key answers stay one click away. | `/chat` | sessions-a-few | nightly | 4 |
| P2 | `chat-pinned-prompt-banner` | As a reader of long replies, I want the turn's own prompt kept visible in a banner while I scroll, so that I never lose the question the answer belongs to. | `/settings/chat` | sessions-long-history | nightly | 5 |
| P2 | `chat-question-cards` | As a user, I want to answer an agent's multiple-choice question from a card in chat, so that decisions are one click. | `/chat` | sessions-a-few | nightly | 3 |
| P2 | `chat-regenerate-variants` | As a user, I want to re-run a turn and switch between kept answers, so that I can compare responses. | `/chat` | sessions-a-few | nightly | 4 |
| P2 | `chat-rewind` | As a user, I want to drop the transcript back to an earlier turn, so that I can redo the conversation from that point. | `/chat` | sessions-a-few | nightly | 3 |
| P2 | `chat-session-summary` | As a user, I want a rolling summary of the conversation in the right panel, so that I can catch up on a long session at a glance. | `/chat` | sessions-long-history | nightly | 3 |
| P2 | `chat-share-message-as-card` | As a user, I want to turn an assistant reply into a branded PNG card with a prefilled caption, so that I can post it to X or LinkedIn. | `/chat` | sessions-a-few | nightly | 4 |
| P2 | `chat-side-chat` | As a user, I want a scratch sub-conversation beside the main turn, opened from the panel, /side, or 'Ask about this' on selected text, so that I can ask a tangent without polluting the main transcript. | `/chat` | sessions-a-few | nightly | 4 |
| P2 | `chat-tool-approvals` | As a user, I want to approve or deny a tool call the agent proposes from an inline card, so that risky actions never run without me. | `/chat` | sessions-a-few | nightly | 4 |
| P2 | `chat-turn-minimap` | As a desktop user reading a long transcript, I want a proportional marker rail in the left gutter with hover previews and click/arrow-key jumps, so that I can navigate turns quickly. | `/chat` | sessions-long-history | nightly | 3 |
| P2 | `chat-worktrees` | As a developer, I want to create a git worktree for a follow-up session from a card, so that parallel work stays isolated. | `/chat` | rich | nightly | 3 |
| P2 | `side-chat-busy-send-steer-or-queue` | As a user, I want a second question during a streaming side turn to be steered in or shown as a queue card, so that nothing I typed is dropped. | `/chat` | rich | nightly | 7 |

## Side panel tabs (`side-panel`)

| Priority | Id | User story | Start URL | Seed | Runnable | Steps |
|---|---|---|---|---|---|---|
| P1 | `chat-activity-side-panel-toggle` | As a user, I want to open the Activity panel beside the transcript, so that I can watch tool calls and subagents while chatting. | `/chat` | sessions-a-few | smoke | 3 |
| P2 | `chat-app-contributed-panel-tabs` | As a user of an installed app, I want its declared side-panel tabs in the chat + menu and its sessionControls chip in the composer bar, so that app UI sits beside my conversation. | `/chat` | apps-installed | nightly | 4 |
| P2 | `side-panel-ctrl-g-opens-subagents` | As a user following a crew pipeline, I want Ctrl+G to jump to the Subagents tab, so that the kiro-cli hint 'Press ctrl+g to monitor progress' works. | `/chat` | rich | nightly | 2 |
| P2 | `side-panel-plus-menu-opens-on-demand-views` | As a user, I want to add on-demand views to the side panel, so that I can inspect logs, links or a session summary when I need them. | `/chat` | rich | nightly | 6 |

## Terminal panel (`terminal`)

| Priority | Id | User story | Start URL | Seed | Runnable | Steps |
|---|---|---|---|---|---|---|
| P2 | `chat-terminal-panel` | As a developer, I want a docked PTY and a per-chat terminal opening on the chat's working directory, so that I can run commands beside the conversation. | `/chat` | rich | nightly | 4 |
| P2 | `side-panel-terminal-tab` | As a developer, I want a terminal beside the chat, so that I can run commands in the session's project directory. | `/chat` | rich | nightly | 4 |
| P2 | `sidebar-terminal-row-toggles-panel` | As a user, I want a terminal docked in the dashboard, so that I can run commands next to the agent. | `/chat` | minimal | nightly | 2 |

## Sessions sidebar & folders (`sidebar`)

| Priority | Id | User story | Start URL | Seed | Runnable | Steps |
|---|---|---|---|---|---|---|
| P0 | `sidebar-folders-history-and-archive` | As a returning user, I want folders, pinned sessions and a History panel, so that I can find past work quickly. | `/chat` | rich | smoke | 3 |
| P1 | `chat-older-sessions` | As a user, I want a searchable history pane of closed sessions with per-session and bulk delete, so that I can revisit or prune past work. | `/chat` | sessions-long-history | smoke | 4 |
| P1 | `chat-sessions-page` | As a dashboard user, I want a bookmarkable session chooser with search, filter chips and recency groups, so that I can pick a session without one being auto-selected or auto-created. | `/sessions` | sessions-a-few | smoke | 3 |
| P1 | `sidebar-rail-collapse-expand` | As a user, I want to collapse the rail to icons, so that I have more room for content. | `/chat` | minimal | smoke | 2 |
| P1 | `sidebar-rail-order-and-active-state` | As a user, I want the rail to list Sessions, Schedule, Artifacts, then Apps/Discover/Library, then Agent Capabilities and Settings, so that navigation is predictable. | `/schedule` | minimal | smoke | 1 |
| P2 | `chat-fork-session` | As a user, I want to branch a new slot from an existing transcript, keeping incognito or temporary mode on the child, so that I can explore an alternative without losing the original. | `/chat` | incognito-mix | nightly | 3 |
| P2 | `chat-session-folders` | As a user with many sessions, I want to group session rows into folders and start mode-pinned ephemeral chats inside them, so that my sidebar stays organised. | `/chat` | sessions-a-few | nightly | 6 |
| P2 | `chat-session-tags` | As a user, I want to put coloured labels on sessions and filter by them, so that I can find related conversations quickly. | `/chat` | sessions-a-few | nightly | 5 |
| P2 | `sidebar-apps-drag-reorder` | As a user, I want to reorder my app shortcuts, so that my most-used apps are on top. | `/chat` | apps-installed | nightly | 3 |
| P2 | `sidebar-apps-overflow-toggle` | As a user with many apps, I want the Apps section to fold past six rows, so that the rail stays compact. | `/chat` | apps-installed | nightly | 3 |
| P2 | `sidebar-focus-mode-peek` | As a user, I want focus mode to hide the header and rail, so that the active surface fills the window. | `/chat` | minimal | nightly | 3 |
| P2 | `sidebar-mobile-drawer-swipe` | As a phone user, I want a drawer for navigation, so that the rail does not consume the screen. | `/chat` | minimal | nightly | 3 |
| P2 | `sidebar-report-issue-diagnostics` | As a user hitting a bug, I want to report it with logs attached, so that triage has evidence. | `/chat` | minimal | nightly | 2 |
| P2 | `sidebar-session-states-pinned-open-closed-archived` | As a user, I want each session state to appear where I expect (slots, History, archive), so that nothing looks lost. | `/sessions` | sessions-a-few | nightly | 4 |
| P2 | `sidebar-workspace-switcher-three-workspaces` | As a user with several projects, I want to switch workspace, so that memory and sessions scoped to it are what I see. | `/chat` | multi-workspace | nightly | 4 |

## Routing & redirects (`navigation`)

| Priority | Id | User story | Start URL | Seed | Runnable | Steps |
|---|---|---|---|---|---|---|
| P2 | `capabilities-pin-tab-to-rail` | As a daily user of one capability tab, I want to pin it as its own rail row (up to NAV_PINNED_LIMIT), so that it is one click away instead of two. | `/capabilities` | minimal | nightly | 3 |
| P2 | `redirect-agents` | As a user with an old bookmark, I want /agents and /mc-agents to land on Agent Capabilities, so that deep links keep working. | `/agents` | minimal | nightly | 1 |
| P2 | `redirect-artifacts-deploy` | As a user with an old bookmark, I want /artifacts/deploy to land on /deploy, so that deep links keep working. | `/artifacts/deploy` | minimal | nightly | 1 |
| P2 | `redirect-connections` | As a user with an old bookmark, I want /connections to land on the Connections tab, so that deep links keep working. | `/connections` | minimal | nightly | 1 |
| P2 | `redirect-instances` | As a user with an old bookmark, I want /instances to land on Settings Instances, so that deep links keep working. | `/instances` | minimal | nightly | 1 |
| P2 | `redirect-knowledge` | As a user with an old bookmark, I want /knowledge to land on the Knowledge tab, so that deep links keep working. | `/knowledge` | minimal | nightly | 1 |
| P2 | `redirect-overview` | As a user with an old bookmark, I want /overview to land on Settings Overview, so that deep links keep working. | `/overview` | minimal | nightly | 1 |
| P2 | `redirect-tasks` | As a user with an old bookmark, I want /tasks to land on Task Runner, so that deep links keep working. | `/tasks` | minimal | nightly | 1 |
| P2 | `redirect-unmatched` | As a user who mistypes a URL, I want anything unmatched to land on /chat, so that I am never stranded. | `/this-does-not-exist` | minimal | nightly | 1 |
| P2 | `settings-legacy-query-url-redirect` | As a user following an old bookmark, I want /settings?tab=channels&channel=slack to land on /settings/channels/slack, so that saved links keep working. | `/settings` | minimal | nightly | 3 |

## Top bar (`topbar`)

| Priority | Id | User story | Start URL | Seed | Runnable | Steps |
|---|---|---|---|---|---|---|
| P2 | `shell-update-overlay-and-pill` | As a user, I want clear update progress, so that I know when the gateway is restarting. | `/chat` | minimal | nightly | 3 |
| P2 | `topbar-feedback-pill-request-feature` | As a user, I want a one-click feature request, so that the agent files it for me. | `/chat` | minimal | nightly | 2 |
| P2 | `topbar-readout-capsule-metrics-usage` | As a user, I want CPU/mem/disk and credit usage in the header, so that I can watch resource pressure while working. | `/chat` | minimal | nightly | 4 |

## Search everywhere & command palette (`search`)

| Priority | Id | User story | Start URL | Seed | Runnable | Steps |
|---|---|---|---|---|---|---|
| P0 | `search-everywhere-palette` | As a user, I want a command palette that answers instantly from local commands, apps, pages and settings and lets me opt into searching sessions, so that I can reach anything from the keyboard without stalling streaming. | `/chat` | rich | smoke | 3 |

## Crew Members (`members`)

| Priority | Id | User story | Start URL | Seed | Runnable | Steps |
|---|---|---|---|---|---|---|
| P0 | `members-crew-members` | As a crew operator, I want a durable DM thread per member with a docked side panel (Crew summary, activity by day, worker sessions, auto-patrol status) and a filterable roster, so that I can supervise each crew in one place. | `/members` | rich | nightly | 6 |
| P2 | `sidebar-crew-members-create-menu-entry` | As a user, I want the Crew Members menu entry to open the page or the setting that enables it, so that I can find the feature either way. | `/chat` | minimal | nightly | 3 |

## Agent capabilities (crews, templates, skills, prompts, steering, hooks, workflows) (`capabilities`)

| Priority | Id | User story | Start URL | Seed | Runnable | Steps |
|---|---|---|---|---|---|---|
| P1 | `capabilities-crews` | As an operator, I want to define named crews binding an agent, model and workspace with a custom avatar, so that I can pick the right worker per task. | `/capabilities` | rich | smoke | 4 |
| P1 | `capabilities-crews-templates-skills-tabs` | As a user, I want to manage crews, agent templates, and skills in one panel, so that agent configuration is centralized. | `/capabilities` | skills-custom | smoke | 3 |
| P2 | `capabilities-agent-templates` | As an operator, I want to inspect the harness-level agent definitions crews bind to, so that I understand what a crew actually runs. | `/capabilities` | minimal | nightly | 2 |
| P2 | `capabilities-crew-appearance-library` | As an operator, I want a Library tab of appearance packs a crew can wear, with import and guarded delete, so that crews look distinct. | `/capabilities` | rich | nightly | 6 |
| P2 | `capabilities-hooks` | As an operator, I want event-triggered agent runs, so that routine reactions happen without me. | `/capabilities` | minimal | nightly | 4 |
| P2 | `capabilities-prompts` | As a user, I want reusable prompt entries from the registry, so that common requests are one pick away. | `/capabilities` | minimal | nightly | 2 |
| P2 | `capabilities-prompts-steering-hooks-workflows` | As a user, I want to edit prompts, steering files, hooks, and saved workflows, so that I can shape agent behavior and automation. | `/capabilities` | skills-custom | nightly | 4 |
| P2 | `capabilities-skills` | As an operator, I want to see installed skills, browse the public registry and review pending candidates, so that I can curate agent know-how. | `/capabilities` | skills-custom | nightly | 3 |
| P2 | `capabilities-steering` | As an operator, I want to manage always-injected steering documents, so that every agent turn follows house rules. | `/capabilities` | minimal | nightly | 4 |
| P2 | `capabilities-workflows` | As a user, I want saved dynamic-workflow definitions and their runs in one library, so that I can rerun orchestrations. | `/capabilities` | rich | nightly | 4 |
| P2 | `knowledge-steering-tab-inclusion-mode` | As an author, I want to see each steering document's inclusion mode (and typos resolved as always), so that I understand what loads into every session. | `/capabilities` | minimal | nightly | 5 |
| P2 | `settings-skills` | As a user, I want skill enablement and a context budget, so that agents load the right skills without blowing context. | `/settings/skills` | skills-custom | nightly | 3 |
| P2 | `skills-pending-candidate-approve-dismiss` | As a user, I want to review an auto-generated skill before it goes live, so that only skills I trust are injected. | `/capabilities` | skills-custom | nightly | 4 |
| P2 | `standalone-hooks` | As an operator, I want a full-page hook manager, so that I can manage event-triggered runs without the capabilities tabs. | `/hooks` | minimal | nightly | 3 |

## Connections (MCP servers & services) (`connections`)

| Priority | Id | User story | Start URL | Seed | Runnable | Steps |
|---|---|---|---|---|---|---|
| P1 | `capabilities-connections` | As an operator, I want to install, enable and scope MCP servers, so that agents get exactly the tools they need. | `/capabilities` | connections-two | smoke | 3 |

## Memory, lessons & usage (`memory`)

| Priority | Id | User story | Start URL | Seed | Runnable | Steps |
|---|---|---|---|---|---|---|
| P1 | `memory-browser` | As a user, I want to browse preferences, projects, history, lessons and the vector store, so that I know what the agent remembers about me. | `/settings/overview` | memory-populated | smoke | 3 |
| P2 | `memory-embeddings` | As a user, I want to enable the vector store and pick its model, so that semantic memory search works. | `/settings/overview` | memory-populated | nightly | 2 |
| P2 | `memory-episodic-search` | As a user, I want to search past episodic memories, so that I can recover a fact from an earlier session. | `/settings/overview` | memory-populated | nightly | 3 |
| P2 | `portability-export-import` | As a user moving machines, I want to export and import the whole memory/config bundle, so that nothing is lost. | `/settings/imports` | memory-populated | nightly | 3 |
| P2 | `usage-tokens-and-turns` | As a user, I want token and turn usage over time, so that I understand my consumption. | `/settings/overview` | memory-populated | nightly | 2 |

## Knowledge library (`knowledge`)

| Priority | Id | User story | Start URL | Seed | Runnable | Steps |
|---|---|---|---|---|---|---|
| P0 | `knowledge-add-folder-source-and-scan` | As a user, I want to register a local folder and see per-source counts after a scan, so that its markdown becomes searchable. | `/knowledge` | memory-populated | nightly | 7 |
| P1 | `capabilities-knowledge` | As a user, I want a document library with sources and items I can open, so that agents can search what my team knows. | `/knowledge` | memory-populated | smoke | 3 |
| P2 | `knowledge-graph-view` | As a user, I want a graph of knowledge relations, so that I can see how documents connect. | `/knowledge` | memory-populated | nightly | 3 |
| P2 | `knowledge-library-empty-state` | As a new user, I want the Knowledge tab to show no sources and how to add one, so that I know where to start. | `/knowledge` | empty | nightly | 2 |
| P2 | `knowledge-settings-embedding-status` | As a user, I want to see embedding progress and knowledge settings, so that I know search is ready. | `/knowledge` | memory-populated | nightly | 3 |
| P2 | `knowledge-upload-document` | As a user, I want to upload a PDF or markdown file, so that its content is searchable by the agent. | `/knowledge` | empty | nightly | 4 |

## Artifacts (`artifacts`)

| Priority | Id | User story | Start URL | Seed | Runnable | Steps |
|---|---|---|---|---|---|---|
| P1 | `artifacts-library` | As a user, I want saved widgets, HTML and documents organised in versioned folders with gallery and table views and a kind filter, so that I can find and reuse what agents produced. | `/artifacts` | artifacts-library | smoke | 3 |
| P2 | `artifacts-comments` | As a reviewer, I want threaded, anchored comments on an artifact, so that feedback lands on the exact passage. | `/artifacts/pagination-design` | artifacts-library | nightly | 4 |
| P2 | `artifacts-companion-chat` | As a user, I want a chat bound to the artifact beside its render, so that I can ask for changes without leaving the page. | `/artifacts/pagination-design` | artifacts-library | nightly | 4 |
| P2 | `artifacts-detail` | As a user, I want to view an artifact, switch between its versions and read its comment threads, so that I can review the agent's iterations. | `/artifacts/release-checklist` | artifacts-library | nightly | 3 |
| P2 | `artifacts-session-tab-honest-empty` | As a user, I want the chat side panel's Artifacts tab to list this session's artifacts only, so that library artifacts from elsewhere do not appear as mine. | `/chat` | artifacts-library | nightly | 3 |
| P2 | `artifacts-star-toggle-persists-view` | As a user, I want to pin artifacts and have the Starred/All choice remembered, so that my library opens where I left it. | `/artifacts` | artifacts-library | nightly | 4 |

## File viewer & project files (`files`)

| Priority | Id | User story | Start URL | Seed | Runnable | Steps |
|---|---|---|---|---|---|---|
| P1 | `chat-files-in-chat` | As a user, I want to browse, attach and upload workspace files from the chat, so that the agent sees the context I mean. | `/chat` | rich | smoke | 4 |

## Browser panel (`browser-panel`)

| Priority | Id | User story | Start URL | Seed | Runnable | Steps |
|---|---|---|---|---|---|---|
| P2 | `browser-panel-open-tab-empty-state` | As a developer working in a chat session, I want to open a Browser tab in the side panel and expand it, so that I can point it at a page I am serving and inspect it comfortably without leaving the chat. | `/chat` | rich | nightly | 4 |

## Apps & App Store (`apps`)

| Priority | Id | User story | Start URL | Seed | Runnable | Steps |
|---|---|---|---|---|---|---|
| P0 | `apps-discover` | As a user, I want to browse the App Store storefront, open an app's detail page and enable it, so that the app appears in my sidebar and I can extend the dashboard. | `/apps` | apps-installed | smoke | 4 |
| P1 | `app-command-bar-launch` | As a keyboard user, I want to press Cmd/Ctrl+K, type a command, and jump to an app or setting, so that I navigate without leaving the keyboard. | `/chat` | rich | smoke | 3 |
| P1 | `apps-library` | As a user, I want installed apps as launchpad tiles with rail pinning and a 'Show N disabled' toggle, so that I can launch and organise apps. | `/apps/library` | apps-installed | smoke | 3 |
| P2 | `app-auto-research-campaign` | As a researcher, I want to enter a broad question, expand it into sub-questions, and start a campaign, so that agents investigate while I am away and stream findings back. | `/auto-research` | apps-installed | nightly | 6 |
| P2 | `app-design-critique-screenshot` | As a designer, I want to drop in a screenshot and receive heuristic-backed findings, so that I get a critique before asking a colleague. | `/design-critique` | apps-installed | nightly | 5 |
| P2 | `app-design-tweak-annotate` | As a frontend developer, I want to preview my local web app, right-click elements in Edit mode, and send a batch of visual comments, so that the agent makes source-mapped edits. | `/design-tweak` | rich | nightly | 7 |
| P2 | `app-file-explorer-browse` | As a user, I want to open a directory on the gateway host and read a file in a tab, so that I can inspect files without leaving the dashboard. | `/file-explorer` | rich | nightly | 4 |
| P2 | `app-md-notebook-attach-edit` | As a writer, I want to attach an existing markdown folder as a vault and edit a note in place, so that my notes stay versioned and portable beside the agent. | `/md-notebook` | rich | nightly | 6 |
| P2 | `app-personal-shopper-start-advice` | As a shopper, I want to save my preferences and start a conversation with the advisor agent, so that I get store research without it ever buying anything. | `/personal-shopper` | rich | nightly | 4 |
| P2 | `app-project-scaffolder-create-folders` | As a developer with a monorepo, I want to scan a project directory and tick sub-projects, so that matching sidebar folders are created without duplicates. | `/project-scaffolder` | rich | nightly | 5 |
| P2 | `app-spec-builder-requirements` | As an engineer, I want to turn a feature idea into Requirements -> Design -> Tasks with an embedded agent and approve each stage, so that an execution session builds the approved plan. | `/spec-builder` | rich | nightly | 7 |
| P2 | `app-workflows-validate-run` | As an orchestrator, I want to validate a dynamic-workflow script and run it, so that I watch phases and agent events stream without arbitrary Python executing. | `/workflows` | apps-installed | nightly | 5 |
| P2 | `apps-detail` | As a user, I want one app's manifest, config, permissions and uninstall in one page, so that I can audit and manage it. | `/apps/library` | apps-installed | nightly | 3 |
| P2 | `apps-installed-app-page` | As a user, I want an app's own UI served inside the dashboard and builtin apps to claim their own top-level routes, so that I use them without leaving. | `/apps` | apps-installed | nightly | 2 |
| P2 | `apps-updates` | As a user, I want to see installed apps with a newer version and update them, so that I stay current. | `/apps/-/updates` | apps-installed | nightly | 2 |

## Task Runner (`task-runner`)

| Priority | Id | User story | Start URL | Seed | Runnable | Steps |
|---|---|---|---|---|---|---|
| P1 | `taskrunner-projects` | As a user, I want autonomous multi-step runs from a spec -- compose, refine, plan, run, then follow one run's steps, gates and approvals -- so that large tasks proceed without babysitting. | `/projects` | rich | smoke | 5 |

## Worlds (3D scenes) (`worlds`)

| Priority | Id | User story | Start URL | Seed | Runnable | Steps |
|---|---|---|---|---|---|---|
| P2 | `app-agent-worlds-scene` | As a user running several agents, I want an ambient 3D scene of my crew with switchable themes, so that I can see at a glance how busy Kiro Crew is. | `/worlds` | apps-installed | nightly | 3 |

## Dev Fleet (`dev-fleet`)

| Priority | Id | User story | Start URL | Seed | Runnable | Steps |
|---|---|---|---|---|---|---|
| P2 | `dev-fleet-page` | As a developer, I want to see worktrees and agent pods, so that I can coordinate parallel work. | `/dev-fleet` | multi-workspace | nightly | 4 |

## Schedule (cron jobs) (`schedule`)

| Priority | Id | User story | Start URL | Seed | Runnable | Steps |
|---|---|---|---|---|---|---|
| P0 | `schedule-cron-jobs` | As a user, I want to see my scheduled jobs with status and next run and create new cron jobs (agent turns, scripts, commands), so that routine work runs on time. | `/schedule` | crons-active | smoke | 4 |
| P1 | `schedule-list-calendar-executions-views` | As a user with cron jobs, I want list, calendar, and execution-history views, so that I can see when jobs run and what happened. | `/schedule` | crons-active | smoke | 3 |
| P2 | `schedule-cron-secret-grants` | As an owner, I want to approve, deny or revoke vault-secret env grants a script cron requested, so that scripts get secrets only with my consent. | `/schedule` | crons-active | nightly | 4 |
| P2 | `schedule-job-detail-drawer-logs` | As a user, I want to inspect a job's last result and logs, so that I can debug a failing schedule. | `/schedule` | crons-active | nightly | 3 |
| P2 | `schedule-monitor-loops` | As a user, I want same-session bounded monitors and nudge loops I can arm, inspect and stop from the composer popover, so that the agent babysits something for me. | `/chat` | crons-active | nightly | 4 |
| P2 | `schedule-template-update-signal` | As a user who created a job from a template, I want a dismissible hint when that template's prompt changes, so that I can refresh my job. | `/schedule` | crons-active | nightly | 3 |

## Inbound webhooks (`webhooks`)

| Priority | Id | User story | Start URL | Seed | Runnable | Steps |
|---|---|---|---|---|---|---|
| P2 | `settings-webhooks` | As an integrator, I want inbound webhook tokens and contexts, so that external systems can wake an agent. | `/settings/webhooks` | minimal | nightly | 4 |
| P2 | `standalone-webhooks` | As an integrator, I want inbound webhook tokens, contexts and run history on one page, so that I can debug callbacks. | `/webhooks` | minimal | nightly | 4 |

## Chat channel integrations (`channels`)

| Priority | Id | User story | Start URL | Seed | Runnable | Steps |
|---|---|---|---|---|---|---|
| P1 | `settings-channels` | As a team lead, I want to configure Slack, Discord, Telegram, WhatsApp, Teams and more, so that the crew reaches my team where they are. | `/settings/channels` | minimal | smoke | 2 |
| P2 | `app-channels-create-room` | As a lead, I want to create a channel from a team preset with role-assigned agents and post an @mention, so that several agents collaborate in one readable transcript. | `/channels` | apps-installed | nightly | 6 |
| P2 | `chat-channels` | As a team, I want group rooms with several agents in one thread, so that multiple crews can collaborate on a topic. | `/channels` | apps-installed | nightly | 5 |
| P2 | `settings-channels-bot-channel-allowlist-and-thresholds` | As an operator, I want to set Allowed user IDs and Soft context threshold for Telegram, so that only I can message the bot and compaction is suggested in time. | `/settings/channels/telegram` | minimal | nightly | 5 |
| P2 | `settings-channels-bot-channel-file-sessions-in-folder` | As an operator, I want channel sessions filed into a named sidebar folder, so that Discord or Telegram chats stay grouped. | `/settings/channels/discord` | minimal | nightly | 5 |
| P2 | `settings-channels-governance-denied-pane` | As a governed user, I want to see 'Off by admin' on a channel denied by policy, so that I understand why I cannot edit its config. | `/settings/channels/slack` | minimal | nightly | 3 |
| P2 | `settings-channels-slack-show-thinking-toggle` | As an operator, I want to toggle Show thinking for Slack, so that reasoning is or is not posted as a thread reply. | `/settings/channels/slack` | minimal | nightly | 3 |

## Voice (`voice`)

| Priority | Id | User story | Start URL | Seed | Runnable | Steps |
|---|---|---|---|---|---|---|
| P2 | `voice-read-aloud-failure-notice` | As a user whose host has no speech engine, I want Read aloud to show a clear failure notice pointing at text-to-speech settings, so that I know why nothing was spoken. | `/chat` | minimal | nightly | 5 |

## Notifications (`notifications`)

| Priority | Id | User story | Start URL | Seed | Runnable | Steps |
|---|---|---|---|---|---|---|
| P1 | `notifications-bell-feed` | As a user, I want a bell feed of agent-pushed notifications, so that I see what happened while I was away. | `/chat` | rich | smoke | 3 |
| P1 | `notifications-center-empty-state` | As a new user, I want the Notifications page to show an honest empty state, so that I understand nothing has happened yet. | `/notifications` | empty | smoke | 2 |

## Computer Use (`computer-use`)

| Priority | Id | User story | Start URL | Seed | Runnable | Steps |
|---|---|---|---|---|---|---|
| P2 | `settings-computer-use` | As a desktop user, I want to enable and scope native desktop automation, so that the agent can drive my apps safely. | `/settings/computer-use` | minimal | nightly | 2 |

## Multi-instance shell (`instances`)

| Priority | Id | User story | Start URL | Seed | Runnable | Steps |
|---|---|---|---|---|---|---|
| P1 | `sidebar-blank-instance` | As a first-time user, I want to see what the dashboard offers with no content, so that I know how to begin. | `/chat` | empty | smoke | 1 |

## Remote instances & cloud launch (`remote-instances`)

| Priority | Id | User story | Start URL | Seed | Runnable | Steps |
|---|---|---|---|---|---|---|
| P2 | `settings-instances` | As an operator, I want to register remote Kiro Crew instances and see provisioning lanes, so that I can switch crews from the header. | `/settings/instances` | multi-workspace | nightly | 3 |

## Popouts & embeds (`popout`)

| Priority | Id | User story | Start URL | Seed | Runnable | Steps |
|---|---|---|---|---|---|---|
| P2 | `embed-chat` | As a host surface, I want chat embedded without dashboard chrome, so that it fits inside another app. | `/embed/chat` | sessions-a-few | nightly | 2 |
| P2 | `embed-sessions` | As a host surface, I want the session list embedded, so that users can switch sessions inside my app. | `/embed/sessions` | sessions-a-few | nightly | 2 |
| P2 | `embed-settings` | As a host surface, I want reduced settings for an embedded host, so that users adjust essentials in place. | `/embed/settings` | minimal | nightly | 2 |
| P2 | `popout-artifact` | As a user, I want one artifact in its own window, so that I can view it full-size beside the chat. | `/artifacts/queue-badge` | artifacts-library | nightly | 2 |

## Authentication & sign-in (`auth`)

| Priority | Id | User story | Start URL | Seed | Runnable | Steps |
|---|---|---|---|---|---|---|
| P0 | `kas-auth-sign-in-card-signed-out` | As an operator, I want to see whether Kiro Crew holds a Kiro identity and how to sign in, so that I can fix a session that cannot start. | `/settings/overview` | minimal | smoke | 2 |
| P1 | `dashboard-token-login-gate` | As an operator, I want the dashboard to refuse unauthenticated visitors and accept my one-time token link, so that only I can drive the gateway. | `/` | minimal | smoke | 3 |

## Onboarding (`onboarding`)

| Priority | Id | User story | Start URL | Seed | Runnable | Steps |
|---|---|---|---|---|---|---|
| P2 | `onboarding-changelog-modal-after-update` | As a user, I want to see what's new after an update, so that I learn about new features. | `/chat` | minimal | nightly | 3 |
| P2 | `onboarding-first-run-wizard` | As a first-time user, I want a guided setup covering import review, privacy, and customization, so that the dashboard is set up and I understand the product before my first chat. | `/` | onboarding-fresh | nightly | 6 |
| P2 | `standalone-mobile-connect` | As a user, I want to pair my phone to this gateway via governed methods and a QR code, so that I can chat on the go. | `/chat` | minimal | nightly | 3 |
| P2 | `standalone-startup-feature-video` | As a returning user, I want one short clip introducing a new feature on the first launch after it ships, so that I discover what changed. | `/chat` | minimal | nightly | 2 |

## Settings (`settings`)

| Priority | Id | User story | Start URL | Seed | Runnable | Steps |
|---|---|---|---|---|---|---|
| P0 | `settings-search-header-jump-to-setting` | As a dashboard user, I want to type a setting name into the Settings header search and pick a result, so that I am taken to the right tab with the row highlighted. | `/settings` | minimal | smoke | 4 |
| P1 | `settings-about` | As a user, I want version, build info and a diagnostics bundle, so that I can report a problem accurately. | `/settings/about` | minimal | smoke | 2 |
| P1 | `settings-chat` | As a user, I want chat behaviour preferences including plain vs highlighted diffs, so that the transcript reads the way I like. | `/settings/chat` | minimal | smoke | 2 |
| P1 | `settings-chat-toggle-show-timestamps` | As a dashboard user, I want to toggle Show Timestamps in Settings > Chat, so that each message displays its time. | `/settings/chat` | sessions-a-few | smoke | 4 |
| P1 | `settings-developer-panel-dev-mode-toggle` | As a developer, I want to enable Developer Mode in Settings > Developer, so that the Developer page with logs and metrics appears in the sidebar. | `/settings/developer` | minimal | smoke | 3 |
| P1 | `settings-display` | As a user, I want theme, density, language and terminal font/shell options, so that the dashboard fits my eyes and habits. | `/settings/display` | minimal | smoke | 2 |
| P1 | `settings-display-theme-switch` | As a user, I want to pick a color theme and font, so that the dashboard matches my preference. | `/settings/display` | minimal | smoke | 3 |
| P1 | `settings-notifications-panel` | As a dashboard user, I want to open Settings > Notifications, so that I can configure sounds and background-chat alerts. | `/settings/notifications` | minimal | smoke | 2 |
| P1 | `settings-overview` | As a user, I want a health hero, stat cards and drill-ins to memory and usage, so that I see system state at a glance. | `/settings` | rich | smoke | 2 |
| P1 | `settings-privacy` | As a user, I want telemetry disclosure and an opt-out, so that I control what is collected. | `/settings/privacy` | minimal | smoke | 2 |
| P1 | `settings-shortcuts` | As a keyboard user, I want a shortcut reference with overrides, so that I can work faster. | `/settings/shortcuts` | minimal | smoke | 2 |
| P1 | `settings-tab-rail-navigation` | As a user, I want each Settings tab at /settings/<tab>, so that I can bookmark and deep-link settings. | `/settings` | minimal | smoke | 3 |
| P2 | `chat-default-memory-mode` | As a privacy-conscious user, I want to choose Persistent, Incognito or Temporary as the default for new dashboard chats, so that new sessions follow my retention preference without a per-chat click. | `/settings/chat` | incognito-mix | nightly | 4 |
| P2 | `notifications-channel-mute-and-priority-override` | As a user, I want per-channel mute and priority controls, so that noisy channels stay in history without badging while approvals stay critical. | `/settings/notifications` | minimal | nightly | 5 |
| P2 | `settings-about-report-a-problem` | As a user, I want to click Report a Problem in Settings > About, so that I can file an issue with diagnostics attached. | `/settings/about` | minimal | nightly | 3 |
| P2 | `settings-about-update-channel-and-notifications` | As a user, I want to choose stable or insider Update channel and toggle Notify when an update is available, so that I control how I receive updates. | `/settings/about` | minimal | nightly | 4 |
| P2 | `settings-browser-attach-token` | As a dashboard user, I want to paste an extension token in Settings > Browser, so that attaching to my own Chrome skips the per-attach prompt. | `/settings/browser` | minimal | nightly | 3 |
| P2 | `settings-browser-panel` | As a dashboard user, I want to open Settings > Browser and see whether playwright-cli is installed and toggle the built-in browser, so that the agent can browse. | `/settings/browser` | minimal | nightly | 3 |
| P2 | `settings-chat-content-width-buttongroup` | As a dashboard user, I want to switch Content Width in Settings > Chat, so that the chat column uses more or less of the screen. | `/settings/chat` | sessions-a-few | nightly | 3 |
| P2 | `settings-chat-select-default-model` | As a dashboard user, I want to pick a Default Model in Settings > Chat, so that new sessions start with that model. | `/settings/chat` | minimal | nightly | 4 |
| P2 | `settings-chat-select-response-verbosity` | As a dashboard user, I want to set Response Verbosity in Settings > Chat, so that agent replies are as terse as I prefer. | `/settings/chat` | minimal | nightly | 3 |
| P2 | `settings-chat-toggle-restore-sessions` | As a dashboard user, I want to toggle Restore Sessions and pick a Restore Window, so that recently active sessions reopen on startup. | `/settings/chat` | sessions-a-few | nightly | 4 |
| P2 | `settings-developer-feature-previews` | As an early adopter, I want to turn on a feature preview in Settings > Developer, so that unfinished surfaces appear in the dashboard. | `/settings/developer` | minimal | nightly | 4 |
| P2 | `settings-display-language-select` | As a non-English user, I want to change the dashboard Language in Settings > Display, so that the UI is shown in my language. | `/settings/display` | minimal | nightly | 3 |
| P2 | `settings-display-session-color-palette` | As a dashboard user, I want to set the session colour palette and defaults in Settings > Display, so that sidebar rows are colour-coded. | `/settings/display` | sessions-a-few | nightly | 5 |
| P2 | `settings-display-terminal-settings` | As a developer, I want to set the terminal shell, font, font size and command completion in Settings > Display, so that the built-in terminal matches my workflow. | `/settings/display` | minimal | nightly | 5 |
| P2 | `settings-display-zoom-level-stepper` | As a dashboard user, I want to step the Zoom Level in Settings > Display, so that the UI is larger or smaller. | `/settings/display` | minimal | nightly | 3 |
| P2 | `settings-imports-panel` | As a new user, I want to import existing agent setups, so that I do not start from scratch. | `/settings/imports` | minimal | nightly | 3 |
| P2 | `settings-notifications-category-sound-override` | As a dashboard user, I want to choose a different sound for Approval or Cron notifications, so that I can tell them apart by ear. | `/settings/notifications` | minimal | nightly | 4 |
| P2 | `settings-notifications-play-sound-toggle` | As a dashboard user, I want to toggle notification sounds and adjust Volume, so that alerts are audible at my preferred level. | `/settings/notifications` | minimal | nightly | 4 |
| P2 | `settings-notifications-sources-mute` | As a dashboard user, I want to mute a noisy notification source in Settings > Notifications > Sources, so that it stops badging and sounding. | `/settings/notifications` | crons-active | nightly | 4 |
| P2 | `settings-privacy-usage-heartbeat-toggle` | As a user, I want to opt in or out of the anonymous usage heartbeat, so that I decide whether a beacon is sent on launch. | `/settings/privacy` | minimal | nightly | 3 |
| P2 | `settings-releases` | As a user, I want a release channel, update check and changelog, so that I stay current on my terms. | `/settings/releases` | minimal | nightly | 3 |
| P2 | `settings-shortcuts-ctrl-for-chat-tabs` | As a Mac user, I want chat-tab switching bound to Ctrl+digit instead of Option+digit, so that it does not conflict with typing special characters. | `/settings/shortcuts` | sessions-a-few | nightly | 4 |
| P2 | `settings-shortcuts-search-everywhere-preset` | As a keyboard user, I want to pick double-Shift, Cmd/Ctrl+K or record a custom chord for Search Everywhere, so that the palette opens with my preferred keys. | `/settings/shortcuts` | minimal | nightly | 5 |
| P2 | `settings-voice-auto-speak-toggle` | As a dashboard user, I want to enable Auto-speak Responses, so that every assistant reply is read aloud. | `/settings/voice` | minimal | nightly | 3 |
| P2 | `settings-voice-panel` | As a dashboard user, I want to open Settings > Voice, so that I can configure spoken replies and dictation. | `/settings/voice` | minimal | nightly | 2 |
| P2 | `settings-voice-ptt-shortcut` | As a dashboard user, I want to set the dictation Shortcut key and how it works (hold/tap/both), so that I can start recording from the keyboard. | `/settings/voice` | minimal | nightly | 4 |
| P2 | `settings-voice-stt-enable-and-provider` | As a dashboard user, I want to enable dictation and choose Local or Transcribe in Settings > Voice, so that I can speak into the message box. | `/settings/voice` | minimal | nightly | 4 |
| P2 | `settings-voice-tts-provider-select` | As a dashboard user, I want to switch the TTS Provider in Settings > Voice, so that spoken replies use the engine I choose. | `/settings/voice` | minimal | nightly | 4 |

## Themes (`themes`)

| Priority | Id | User story | Start URL | Seed | Runnable | Steps |
|---|---|---|---|---|---|---|
| P2 | `themes-install-l0-pack-from-local-dir` | As a user, I want to install a theme pack from a folder on disk, so that it appears in the single Theme dropdown. | `/settings/display` | minimal | nightly | 6 |
| P2 | `themes-l2-persona-consent-modal` | As a user, I want to see exactly the persona text a theme will inject and consent to it, so that nothing surprising reaches the agent. | `/settings/display` | minimal | nightly | 8 |

## Security & governance (`security`)

| Priority | Id | User story | Start URL | Seed | Runnable | Steps |
|---|---|---|---|---|---|---|
| P1 | `settings-security` | As an owner, I want denied commands, sensitive paths and the approval posture in one place, so that I can tighten what agents may do. | `/settings/security` | minimal | smoke | 3 |
| P2 | `settings-secrets` | As a user, I want managed integration credentials and other vault entries, so that secrets live encrypted, not in config. | `/settings/secrets` | minimal | nightly | 4 |
| P2 | `settings-security-approval-yolo-duration` | As a user, I want to choose the auto-approve duration in Settings > Security > Approval, so that YOLO mode expires when I expect. | `/settings/security/approval` | minimal | nightly | 3 |
| P2 | `settings-security-docs-section` | As an operator, I want to open Settings > Security > Docs, so that I can find links to the security documentation. | `/settings/security/docs` | minimal | nightly | 2 |
| P2 | `settings-security-governance-section` | As a governed user, I want to see the active governance policy and its scopes (access, io, channels, modes), so that I know which controls an admin has set. | `/settings/security/governance` | minimal | nightly | 2 |
| P2 | `settings-security-layers-section` | As an operator, I want to read the enforced security layers list, so that I understand what protections apply. | `/settings/security/layers` | minimal | nightly | 2 |
| P2 | `settings-security-mobile-login-link` | As a user, I want to generate a one-time mobile login link from Settings > Security, so that I can sign in on my phone. | `/settings/security/approval` | minimal | nightly | 3 |
| P2 | `settings-security-rules-custom-deny` | As an operator, I want to add a regex to Your custom denies in Settings > Security > Rules and remove it again, so that the PreToolUse gate blocks commands I choose. | `/settings/security/rules` | minimal | nightly | 5 |
| P2 | `settings-security-tailnet` | As an operator, I want to expose the dashboard over my tailnet, so that I can reach it from other devices. | `/settings/security/tailnet` | minimal | nightly | 3 |
| P2 | `settings-security-trusted-apps-toggle` | As an operator, I want to decide whether third-party apps run their own code without asking, so that I balance convenience against safety. | `/settings/security/apps` | apps-installed | nightly | 3 |

## Developer tools (`developer`)

| Priority | Id | User story | Start URL | Seed | Runnable | Steps |
|---|---|---|---|---|---|---|
| P2 | `developer-agent-backend` | As a developer, I want to see which agent harness backend is live and whether Kiro prerequisites are met, so that I know what turns run on. | `/developer` | minimal | nightly | 2 |
| P2 | `developer-archive` | As a developer, I want a consolidated session archive browser, so that I can inspect compacted history. | `/developer` | sessions-long-history | nightly | 3 |
| P2 | `developer-config` | As a developer, I want raw Kiro Crew and agent config editors, so that I can fix a setting no panel exposes. | `/developer` | minimal | nightly | 3 |
| P2 | `developer-debug-tools` | As a developer, I want diagnostic overlays such as the chat scroll inspector, so that I can debug layout issues. | `/developer` | sessions-a-few | nightly | 3 |
| P2 | `developer-logs` | As a developer, I want a live gateway log stream with level control, so that I can debug in real time. | `/developer` | minimal | nightly | 3 |
| P2 | `developer-mcp-pool` | As a developer, I want MCP connection pool state and a probe, so that I can diagnose tool outages. | `/developer` | connections-two | nightly | 3 |
| P2 | `developer-memory-graph` | As a developer, I want an entity/relation visualiser over the memory store plus every memory layer and saved lesson, so that I can debug and audit what the agent remembers. | `/developer` | memory-populated | nightly | 3 |
| P2 | `developer-page-tabs` | As a developer, I want logs, system, telemetry, storage, MCP pool, memory graph, config, agent backend, debug tools, and archive in one place, so that I can diagnose the gateway. | `/developer` | rich | nightly | 5 |
| P2 | `developer-storage` | As a developer, I want a raw localStorage inspector, so that I can debug client preferences. | `/developer` | minimal | nightly | 2 |
| P2 | `developer-system` | As a developer, I want host runtime, services, sessions and performance views, so that I can spot resource problems and reclaim session storage. | `/developer` | sessions-long-history | nightly | 3 |
| P2 | `developer-telemetry` | As a developer, I want startup timings and context traces, so that I can find slow paths. | `/developer` | minimal | nightly | 2 |
| P2 | `standalone-logs` | As an operator, I want a full-page log viewer, so that I can watch the gateway without dashboard chrome distraction. | `/logs` | minimal | nightly | 2 |

## Not generated

Features the lane cannot drive on its target. Listed so the gap is a decision, not an omission.

| Tier | Id | Feature | Title | Why |
|---|---|---|---|---|
| native-only | `chat-browser-panel` | browser-panel | Browser panel | The panel is owned by the Electron native Chromium view and Annotate is IPC-only. Web-only degradation is limited to asserting the tab and its 'use playwright-cli' guidance / empty state render, which browser-panel-open… |
| native-only | `app-crew-companion-reminders` | apps | Crew Companion: set break interval and add a reminder | Requires the Electron desktop app and its companion process; the target has no Electron. Degradable to web-only only for the offline landing state and cached Memories. |
| native-only | `app-mochi-dashboard` | apps | Mochi: enable the desktop pet and view activity, watchlist, and plan | Pet window, click-through transparency, and panel windows need Electron, which the target lacks; the dashboard data page alone is degradable to web-only. |
| native-only | `settings-channels-imessage-configure` | channels | Configure iMessage channel (enable, allowed handles, service, DB path) | Requires the macOS Messages database; form rendering is degradable to web-only but the channel cannot start on Linux. |
| native-only | `standalone-crash-report-notice` | notifications | Crash report notice | Driven by Electron IPC (crash-reports:get/reveal) and the OS file manager; the target has no Electron shell, so the banner never appears and there is no web-only degradation. |
| native-only | `computer-use-live-view-pip` | computer-use | Live view PiP mirrors screenshots the agent already read | Depends on a macOS/Windows accessibility capture and a model that invokes computer_get_state; neither exists on the Linux fake-ACP target. |
| native-only | `settings-computer-use-enable-toggle` | computer-use | Enable computer use and tune screenshot/tree options | Persistence is web-testable (degradable) but the capability only functions with the desktop accessibility layer. |
| native-only | `popout-chat` | popout | Popout chat window | Opening an OS window is Electron behaviour, so native-only; degradable to a web-only check by loading /popout/chat/<slug> directly and asserting the chrome-less frame. Two popout records merged. |
| native-only | `popout-terminal-window` | popout | Pop the terminal out and re-dock it | Requires a second window and a PTY; Electron-first, so native-only. Degradable: load /popout/terminal directly and assert the frame. Moved the feature-map popout-terminal record from the chat bucket (docked terminal is … |
| native-only | `settings-chat-toggle-prevent-sleep` | settings | Toggle Prevent sleep while running | Toggle persistence is web-testable (degradable), but the actual sleep-inhibit effect needs Electron. |
| native-only | `settings-developer-run-local-gateway-toggle` | settings | Toggle Run a local gateway | Desktop-app-only section; not degradable to web because it is hidden without Electron. |
| native-only | `settings-notifications-background-chat-finished` | settings | Toggle Notify when a background chat finishes | Toggle persistence is web-testable (degradable), but OS notification delivery needs Electron and window focus state. |
| needs-secret | `chat-mcp-oauth-banner-sign-in` | chat | Complete MCP OAuth from the chat banner | Requires a real OAuth provider account. |
| needs-secret | `standalone-source-provider-review` | chat | Source-provider review | Fetching PR state requires a real source-provider token. |
| needs-secret | `sidebar-connect-your-phone-modal` | sidebar | Open the Connect your phone dialog | Kept needs-secret: the row only appears when /api/mobile-connect returns a renderable method, and every real method (tunnel/tailnet) needs an external account the no-network target lacks. Rendering the modal against a s… |
| needs-secret | `connections-services-gallery-oauth` | connections | Connect a third-party service from the Services gallery | Completing OAuth needs a real provider account; card rendering alone is nightly-testable. |
| needs-secret | `wakatime-activity-and-export` | memory | WakaTime coding activity and export | Stats come from the WakaTime API with a personal key; the empty-state alone is nightly-renderable. |
| needs-secret | `artifacts-deploy` | artifacts | Deploy | The deploy action needs an external AWS account and network, which the target has neither of. Only the config/scan page renders offline — that read-only render could be split out as a nightly check later. |
| needs-secret | `artifacts-publishing` | artifacts | Publishing | Publishing targets a real external provider; the empty-provider share menu is the only offline-renderable part. |
| needs-secret | `artifacts-remote` | artifacts | Remote artifacts | Browsing requires a live provider account and network; no fixture stands in for it. |
| needs-secret | `app-auto-improvement-run` | apps | Auto-Improvement: connect a GitHub repo, calibrate ruler, view findings | Primary flow depends on an authenticated gh CLI and a real GitHub repo to clone, measure, and draft PRs against; the empty setup panel alone could be checked nightly but is not the primary flow. |
| needs-secret | `app-aws-control-drive` | apps | AWS Control: view account health and browse the S3 cloud drive | Every meaningful surface (account health, drive listing, share links) requires live AWS credentials and an S3 bucket the user owns; the target has no network and no credentials. |
| needs-secret | `app-code-review-sage-review-pr` | apps | Code Review Sage: add a repo and review a pull request | Repository discovery, PR listing, and draft review staging all go through the authenticated gh CLI against GitHub. |
| needs-secret | `app-issue-radar-connect-triage` | apps | Issue Radar: connect a repository and triage issues | Repository connection, issue caching, and every write go through authenticated provider CLIs; only the welcome carousel is reachable without them. |
| needs-secret | `app-meetings-live-transcribe` | apps | Meetings: start a meeting and capture live transcript with notes | The primary flow needs a working STT backend (an optional heavy install the no-network target cannot fetch) and a calendar/audio source; only the empty meeting list and settings views render from fixtures, and that is n… |
| needs-secret | `app-ops-mission-control-board` | apps | Ops Mission Control: configure a provider and work an incident on the board | Primary flow needs a real monitoring provider credential; a signed generic webhook could degrade this to nightly but the board is empty without a configured provider. |
| needs-secret | `app-pptx-maker-studio` | apps | PPTX Maker: open the studio, browse the library, and start a deck chat | The primary flow (start a deck chat, watch preview) requires the engine download and MCP tools that need network, which the target does not have; the Decks/Library/Settings views render headless but are not the primary … |
| needs-secret | `chat-channel-mirroring` | channels | Channel mirroring | Linking requires a real messaging-provider token to list channel targets; the menu shell alone renders without one. |
| needs-secret | `settings-channel-connect-slack` | channels | Connect Slack in the Slack channel panel | Requires real bot tokens; form rendering is nightly-testable. |
| needs-secret | `settings-channels-feishu-wecom-configure` | channels | Configure Feishu / WeCom channels | Requires real platform app credentials to verify a connection. |
| needs-secret | `settings-channels-teams-configure` | channels | Configure Microsoft Teams channel (App ID, password, tenant, allowed users) | Requires real Azure Bot credentials to reach a connected state. |
| needs-secret | `settings-channels-webex-configure` | channels | Configure Webex channel (token, allowed emails, group spaces, thread reply) | Connection verification requires a real Webex bot token; form is renderable on nightly. |
| needs-secret | `settings-channels-weixin-configure` | channels | Configure WeChat (Weixin) channel | Connecting requires a real WeChat login/QR; UI-only form is nightly-degradable. |
| needs-secret | `settings-channels-whatsapp-configure` | channels | Configure WhatsApp channel (enable, who can message, how the agent joins in) | Real account pairing required for connection; conditional form rendering could be nightly. |
| needs-secret | `stt-dictation-aws-transcribe` | voice | Dictate through AWS Transcribe after granting consent | Billed AWS service with real credentials and network; also needs a microphone, which the target lacks. |
| needs-secret | `voice-polly-synthesis-with-aws-consent` | voice | Select Amazon Polly and pass the AWS consent gate before hearing a reply | A paid AWS service reached with real credentials over the network; the target has neither. |
| needs-secret | `shell-instances-tab-bar-switching` | instances | Switch between Local and remote instance panes | Needs a reachable remote instance over a tunnel. |
| needs-secret | `settings-instances-remote-crew` | remote-instances | Add a remote instance and switch panes via the header tab strip | Connecting needs a real peer and tunnel credentials; the form and empty tab strip are nightly-testable. |
| needs-secret | `themes-install-pack-from-github` | themes | Install a theme pack from a github.com URL | Needs outbound network to github.com (and a sandbox backend), unavailable on the target. |
| excluded | `chat-mcp-app-inline-render` | chat | An MCP App renders inline in chat as a sandboxed iframe with a live bridge | Not reachable on the fake backend/no-network target. |
| excluded | `chat-workflow-launch-and-completion-cards` | chat | workflow_run shows a launch card and later a completion card in chat | Depends on real MCP tool execution by a model, absent on the fake ACP target. |
| excluded | `knowledge-fetch-url-source` | knowledge | Add a URL source that is fetched and ingested | No external account is involved, the blocker is outbound network, which the target does not have, so the fetch fails by construction and the flow is unobservable here. |
| excluded | `artifacts-widget-auto-registration-from-chat` | artifacts | A chat-rendered mcwidget is auto-registered as an artifact | Not reachable on the fake backend; requires a model that emits widget markup. |
| excluded | `browser-panel-load-dev-server-preview` | browser-panel | Load a locally served dev-server URL into the Browser tab iframe | The target's managed browser policy (URLBlocklist * with only the gateway origin allowed) makes any dev-server iframe fail by construction, so the behaviour cannot be observed on this harness; it is web-capable elsewher… |
| excluded | `app-papyrus-create-compile` | apps | Papyrus: create a paper, edit LaTeX, and compile to PDF | Excluded: compiling to PDF needs a TeX toolchain the lane's runner image does not install, so the asserted end state is unreachable; a create-and-edit-only scenario without compile could be nightly. Paper creation and e… |
| excluded | `apps-migration` | apps | App migration | Excluded until a fixture ships an orphaned app: no tests_fixtures seed carries one, so the migration page (/apps/migrate/<name>) is unreachable on the target. Page renders from fixture state and mutates only fixture sta… |
| excluded | `schedule-agent-chat-egress` | api | Agent chat egress | No UI and requires a live provider; not pixel-testable. |
| excluded | `schedule-session-control` | api | Session control | No UI; side effects show up as ordinary sessions covered elsewhere. |
| excluded | `schedule-session-ledger` | api | Session ledger | No UI surface; not pixel-testable. |
| excluded | `schedule-work-ledger` | api | Work ledger | No UI surface today; not pixel-testable. |
| excluded | `standalone-openai-compatible-api` | api | OpenAI-compatible API | HTTP-only surface with no dashboard pixels. |
| excluded | `chat-voice-reply-and-dictation` | voice | Voice reply and dictation | The Electron shell would not add a microphone, an audio sink or a recogniser model to the Xvfb host, so no native lane on this target could observe the behaviour either. The mic button and Read-aloud menu entry renderin… |
| excluded | `notifications-desktop-alert-toast-opt-in` | notifications | Opt into Desktop alerts and receive an OS toast when a background session finishes | The outcome is an OS-level toast outside the browser viewport and gated on a permission prompt; not pixel-testable on Xvfb. The toggle itself is covered by settings-notifications. |
| excluded | `notifications-turn-complete-chime-audio` | notifications | Hear the chime when a turn completes | Audio output is not pixel-testable and leaves no visible record; Xvfb has no audio sink. The chime preset control itself is asserted in settings-notifications. |
| excluded | `chat-remote-bound-session` | remote-instances | Remote-bound session | This is not a missing credential but a missing second live Kiro Crew instance plus a network tunnel, which the Xvfb+fake-ACP, no-network target cannot provide. Only the flag-gated menu entry is renderable (cover that in… |
| excluded | `standalone-cloud-launch` | remote-instances | Cloud launch | Kept excluded rather than needs-secret: beyond real AWS credentials it provisions billable infrastructure and exposes Stop/Delete-by-tag lifecycle, so it must not run even with secrets. Only the lane selector and IAM po… |
| excluded | `kas-auth-chat-error-row-sign-in-link` | auth | An auth-required turn renders a 'Sign in to Kiro' error row deep-linking to the card | Excluded: the fake ACP backend cannot emit the AcpAuthRequired vocabulary (only [[ERROR]]), so the row and its deep link are unreachable on the target. Closing that harness gap (an auth-required sentinel) is the prerequ… |
| excluded | `settings-about-check-for-updates` | settings | Check for updates and install | Network-dependent and restarts the application; not pixel-testable. |
| excluded | `settings-browser-install-cli` | settings | Install playwright-cli and a browser engine from Settings | Network download and host mutation; not pixel-testable deterministically. |
| excluded | `settings-ui-prefs-backup` | settings | UI preference host backup | Cross-cutting mechanism with no UI of its own; verified through the settings rows it backs, not pixels. |
| excluded | `settings-voice-stt-model-download` | settings | Download a local STT model | Requires large external download and disk writes; not suitable for pixel tests. |

## Proposed new feature slugs

None outstanding: every area the readers proposed is a slug in the `FEATURES` registry in `scenarios.py` (mirrored by `FEATURE_TITLES` here), and its records are filed there.
