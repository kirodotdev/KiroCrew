# Changelog

All notable changes to KiroCrew are documented in this file.

## [Unreleased]

### Features

- **The agent now looks at its own front-end changes.** A new `web-verify` builtin skill turns the integrated Browser panel into a verification step rather than a viewing surface: after a UI edit the agent navigates the loopback URL of the dev server or pod it started, screenshots the surface it changed, reads the frame to judge it, and embeds it in chat — so a clipped label, a wrapped row, or a page stuck behind a first-run gate is caught before a reviewer finds it. Three capture backends are called out explicitly so a missing one never reads as "verification impossible": the Playwright MCP `browser_*` tools (frames stream into the Browser panel), the [`agent-browser`](https://github.com/vercel-labs/agent-browser) CLI (annotated frames, baseline pixel diff, axe-core audit — screenshots land on disk, not the panel), and scripted Playwright via `pod-e2e`; the agent names which one it used. The system prompt gets **smaller**, not larger: its blanket "DO NOT use `browser_take_screenshot`" rule — which made front-end self-verification a policy violation — is deleted rather than qualified, and the three enumerated view-only exceptions collapse into one sentence granting view-only browsing and naming the skills. Which backend to use, how many frames to take, and how to caption them live in the skill, where they can be revised without touching every agent's prompt. `web-browse` and `web-preview` route self-verification to `web-verify`, and `prepare-pr` sources a PR's mandatory UI screenshots from the same capture, so the evidence a reviewer sees is the evidence the agent used. The guard stays honest: with no browser backend installed the agent says plainly that rendering was not verified and points at `kirocrew browse setup` or `npm install -g agent-browser`, rather than claiming a look it never took.

- **Official Docker image** — The gateway now ships as a multi-arch container at `ghcr.io/kirodotdev/kirocrew` (`stable` / `insider` / `nightly` channel tags plus immutable version tags, linux/amd64 + linux/arm64), built from the exact same wheel pip users install and carrying SLSA build provenance. Registry access remains private for now and requires GHCR authentication with package access. One `docker run` with a single volume gives an always-on headless gateway — dashboard, Slack/Discord/Telegram/WeCom/Webex bots, crons — with kiro-cli preinstalled; `docker exec` in for the one-time `kiro-cli login` and to mint dashboard links. A new `KIROCREW_BIND` env override (validated, fail-narrow) lets the gateway bind beyond loopback inside the container's network namespace so published ports actually work; token auth, CSRF, and Host validation are unchanged and every request still needs a dashboard token. Orchestrator liveness/readiness probes (`/api/health`, `/api/live`, `/api/ready`) are now reachable when addressed by container/pod IP: they bypass Host validation, and in exchange the build-identity fields are additionally gated on a served Host header — a DNS-rebound loopback request learns only `{"ok": true}`. See `docs/DOCKER.md`.

- **WeCom channel settings panel** — Settings gains a WeCom tab so the WeCom (企业微信) channel can be set up and modified from the dashboard instead of hand-editing `.env` and `config.json`: paste the Bot ID and Secret from the WeCom admin console (stored in `.env`, masked previews, independent replace/clear per credential), manage the userid allow-list (display names in `config.json` are preserved), and optionally flip **Allow all organization members** — an explicit opt-in that lets everyone in your WeCom org tenant DM the bot without listing each userid (an empty allow-list still denies everyone; messages without a userid are always dropped). The status badge reports whether the channel actually started this session, with the failure reason when it didn't.

- **Data home moved under ~/.kiro/** — KiroCrew now stores its data in ~/.kiro/crew (was ~/.kirocrew), aligning with other Kiro apps. Existing installs migrate automatically on first launch: data is copied to the new location, overwriting any conflicting file already there, and the old ~/.kirocrew is then deleted. Re-downloadable bulk content (embedding models, caches) is excluded from the copy — the new home regenerates it — so the migration stays fast, and a Python virtual environment sitting in the old home (`venv`/`.venv`/`venvs`) is left in place rather than moved, since relocating one breaks the interpreter. There is no rollback copy; back up ~/.kirocrew yourself before upgrading if you need one. Set KIROCREW_HOME to override.

### Fixes

- The Sidebar PR/MR panel and Issue Radar now accept the `gh`/`glab` you already have installed, instead of demanding a root-owned copy. A stock `brew install gh` could never satisfy the old rule (`/opt/homebrew` is owned by you and `bin/gh` is a symlink into `Cellar/`), so users with a working, signed-in CLI were told to `sudo cp` it into `/usr/local/libexec/kirocrew/` and re-copy it after every upgrade. Resolution now follows the rule you would expect — if `gh` works in your terminal, it works here: the well-known install dirs are searched first, then the gateway's own `PATH`, and symlinked layouts resolve normally, so Homebrew, Linuxbrew, asdf/mise and `~/.local/bin` installs are all found. Still refused: a CLI owned by another unprivileged account, a world-writable one, or one inside your project checkout or agent workspace, the one place the agent itself can write; a gateway running as root is refused outright as before. Provider children keep receiving only a minimal provider-scoped environment (no AWS/Slack/gateway secrets, no inherited `PATH`) and every spawn stays SEL-audited. Set `KIROCREW_PROVIDER_BIN_STRICT=1` on a shared or multi-tenant host to restore the previous root-owned-only policy. Dev Fleet's `git`/`gh` resolution gained the Homebrew and Linuxbrew prefixes for the same reason and now pins the resolved target rather than the bin-dir symlink, and `KIROCREW_GH_BIN` / `KIROCREW_GLAB_BIN` / `KIROCREW_ISSUE_RADAR_GH` still take an explicit absolute path.
- **Data-home migration no longer destroys a KiroCrew install whose virtual environment lives inside the data home.** The wheel installer (`curl … cli.sh`) used to create its managed venv at `~/.kirocrew/venv`, so the one-time move to `~/.kiro/crew` treated the running interpreter as user data: it copied the venv (useless — a venv is not relocatable, so every console script at the new location pointed at an absolute path that was about to vanish) and then deleted `~/.kirocrew` outright, removing the live interpreter and its `site-packages` mid-run. The upgrade died with `ModuleNotFoundError: No module named 'kiro_crew.config.validation'`, and every later `kirocrew` invocation hit a dangling `~/.local/bin/kirocrew` symlink or a `bad interpreter` error — no working CLI, and no obvious way back short of a reinstall. The migration is now a data move only: `venv`, `.venv`, and `venvs` directories at the top of the old home are neither copied nor deleted, and the old directory survives to hold them. As a further backstop, if the running interpreter sits anywhere else inside the old home the migration declines to run rather than delete it. `kirocrew doctor` reports a retained venv as expected rather than as leftover debris to delete.
- The data-home migration no longer stalls permanently when the old home contains a regular *file* named `venv`, `models`, or `cache`. The copy step skipped those names outright while verification only pruned same-named directories, so such a file was counted as a failed copy and the migration aborted and retried forever. Only directories are skipped now; a file with one of those names migrates as ordinary data.
- **Installed apps no longer break after the data-home move.** An app with a `requirements.txt` keeps its own virtual environment at `apps/<name>/.venv`, and the migration was copying it — producing a broken environment at the new location (the copy skips symlinks, so `bin/python` never arrived, and the console-script paths pointed into the old home that was then deleted). Because an app only rebuilds its environment when the directory is *absent*, the broken copy was never repaired and dependency installation failed on every start. That one managed environment is now left out of the copy entirely, so each app rebuilds a working one on next start.
- **Your own virtual environments in the old data home are never destroyed by the move.** Only the app environment KiroCrew manages itself (`apps/<name>/.venv`) can be silently dropped, because only that one gets rebuilt. If any *other* virtual environment is found under `~/.kirocrew` — say a `tools/myenv` you created — the migration now defers instead: it cannot copy one intact (virtual environments are not relocatable), and completing the move would delete the original. KiroCrew keeps using the old location and prints which paths to relocate; move them and restart to finish the one-time migration. Detection is by the `pyvenv.cfg` marker rather than by directory name, so a folder merely *called* `.venv` that holds real data is treated as ordinary data and migrates normally.
- The wheel installer now creates its managed virtual environment beside the data home (`~/.kiro/crew-venv`) instead of inside it (`~/.kiro/crew/venv`), so no whole-data-home operation can reach the interpreter again. Re-running the installer moves an existing in-home venv over automatically — the old one is removed only after the new one is verified working. Set `KIROCREW_VENV` to choose a different location; `pipx` installs (preferred when available) were never affected.
- Data-home migration no longer fails when the new `~/.kiro/crew` folder already holds read-only files. Git-backed content under the data home (app-source checkouts store their packfiles read-only) would make the copy raise a permission error and abort the whole migration, leaving the app stuck on the old `~/.kirocrew` location while a half-populated `~/.kiro/crew` sat alongside it — the exact "my old chats are in one folder but new sessions write to the other" split-brain some users hit. The migration now overwrites read-only destination files (your `~/.kirocrew` data still wins on a conflict) and completes.
- Data-home migration no longer strands users who already have a `~/.kiro/crew` folder. If that folder pre-exists with different contents (e.g. created by another Kiro tool or a `KIROCREW_HOME` experiment), the migration now force-copies your `~/.kirocrew` data over it — your `~/.kirocrew` data always wins on a conflict — and completes the move, instead of silently keeping you on the old `~/.kirocrew` location forever. A gateway actively using `~/.kiro/crew` is never disturbed. Note: whatever was previously at the pre-existing `~/.kiro/crew` is overwritten with no backup — if you need to keep it, copy it elsewhere before upgrading.
- If an earlier build had already created a `~/.kirocrew.archived` or `~/.kiro/crew.pre-migration/<timestamp>` rollback copy, KiroCrew now removes it automatically on the next start. Those paths held real credentials but were no longer protected from agent file access once the rollback machinery that created them was removed, so an existing copy is deleted rather than left exposed.

### Changes

- **App Kit `ctx.cron` mutation API stays synchronous** — The cron store's event-loop-safety refactor briefly flipped the app-facing `CronSDK` methods (`add_job`/`remove_job`/`update_job`/`remove_all`) to `async def`. Because these are the published App Kit surface handed to apps via `ctx.cron`, an existing third-party app calling `ctx.cron.add_job(...)` synchronously would have received an un-awaited coroutine — the cron would silently never be created (a `RuntimeWarning` only, no error at the call site). The methods are now **synchronous again, preserving the original contract**, with separately-named `*_async` siblings for callers already on the gateway event loop. The sync methods never run on the loop: invoked from a genuinely loop-less context (CLI, MCP process, worker thread — what apps overwhelmingly use) they run inline as before, but invoked from a *running* event loop they now raise `CronSyncOnLoopError` naming the `*_async` sibling to await instead. **This is a breaking change for the narrow case of an app calling a synchronous `ctx.cron.*` mutator from an on-loop `on_startup` hook or route handler**; such a call previously appeared to work while parking the gateway loop for the bounded cron-store lock window (up to 10s), stalling chat, timers, and heartbeats for every session on the instance. Offloading to a worker thread does not fix that — the caller must still block on the worker's result — and running inline is rejected outright by the loop-safety guard below, so there is no correct synchronous on-loop behavior to preserve. Migration is a one-line change: `ctx.cron.add_job(...)` → `await ctx.cron.add_job_async(...)` (identical arguments and return value). The error is raised before any mutation, so a refused call never half-applies. `remove_all` (invoked on app disable/uninstall) now removes every owned job in ONE atomic transaction and surfaces a `CronStoreBusy` cleanup failure instead of masking it as a successful `0`. The owned set is now SELECTED inside that same locked transaction — against the freshly-reloaded on-disk store, not a cache-only `list_jobs()` id snapshot taken before the lock — so a job the app created in another process since the gateway's last cache refresh is still seen and removed. Selecting from a stale cache snapshot could otherwise miss such a job, report a successful cleanup, and let uninstall delete the app while leaving an ENABLED orphan cron with no owning app. **`POST /api/apps/{name}/uninstall` now treats cron cleanup as a hard precondition**: a contended store (after a short bounded retry) aborts the uninstall with a retryable `409` and leaves the app installed, instead of logging the failure and proceeding to delete the app. Previously — and this is what the propagation exposed — continuing past a failed cleanup deleted the app directory and its cron manifest while the app's jobs stayed persisted and ENABLED, leaving permanent orphans that kept firing their command/script/agent payload with no owning app left to clean them up. The `disable` path deliberately still reports-and-continues (it is reversible: the app and its manifest remain, so the cleanup is retryable). A new machine-enforced loop-safety guard (`KIROCREW_STRICT_LOOP_SAFETY`) catches any future code path that takes the cron store lock on the event loop.

- **Local telemetry adds opt-in bounded retention and OTLP packaging** — Existing telemetry history is preserved on upgrade: `telemetry.retention_days` and `telemetry.max_total_mb` both default to `0`, so no shard is deleted unless an operator explicitly configures a positive age window or opportunistic size budget. Before the first destructive sweep in each exporter process, KiroCrew emits a path-free warning and defers deletion for one 300-second prune interval. Optional OTLP/HTTP support is installable with `pip install "kirocrew[otlp]"`; network egress still requires an explicit `telemetry.otlp_endpoint`.
- **Remote health payload is intentionally narrower** — `/api/health` and `/api/live` remain anonymous public probes on non-loopback binds, but remote and forwarded callers now receive exactly `{"ok": true}` instead of `app` and `version` build identity. Direct-loopback callers retain identity for the desktop production/nightly shared-port guard. An in-repository consumer audit found that guard is the only `app`/`version` reader and it fetches `http://localhost:<port>/api/health`, so it is unaffected. External proxied consumers that read those fields must migrate before upgrading; the anonymous probe paths now expose only stable service-presence/lifecycle markers.
- **Slack challenge-and-redirect REMOVED** — Inbound Slack messages are now processed inline and reach the agent directly (gated by the user allowlist and Enterprise Grid origin check), instead of being intercepted and turned into a presigned dashboard-session link for every message. The challenge-and-redirect flow was an internal-only security posture and is not needed for external/open-source usage. The `send_channel_challenge()` helper and the `_CHALLENGE_REDIRECT_ENABLED` gate were removed; the explicit `/kirocrew dashboard` link command is unchanged.
- **Default dashboard port is now 5476** (was 8765) — `5476` spells "KIRO" on a phone keypad (K=5, I=4, R=7, O=6) and is far less commonly grabbed than the `8765` descending-sequence port. The gateway, CLI, dashboard, Electron app, and frontend dev proxy now default to `5476`. If you relied on the old port, set `KIROCREW_PORT=8765` (or pass `--port 8765`) to keep it, and update any bookmarks, SSH tunnels (`ssh -NL 5476:localhost:5476 <host>`), and `dashboard.url` config entries. The auth cookie name follows the port (`mc_token_5476`), so existing sessions on the old port are re-issued automatically on first load.
- **Default dashboard port is now 8765** (was 7777) — The gateway, CLI, dashboard, and frontend dev proxy now default to port `8765`, avoiding clashes with other tools that commonly grab 7777. If you relied on the old port, set `KIROCREW_PORT=7777` (or pass `--port 7777`) to keep it, and update any bookmarks, SSH tunnels (`ssh -NL 8765:localhost:8765 <host>`), and `dashboard.url` config entries. The auth cookie name follows the port, so existing sessions on the old port are re-issued automatically on first load.

## [2.6.0] — 2026-05-31

287 commits across 2 packages (182 KiroCrew + 105 KiroCrewWebsite), 99 contributors since v2.5.0.

### Features

- **Standalone Provider — Full Parity + Interactive Permissions** — KiroCrew now runs the standalone provider as a first-class backend through a unified ACP (Agent Client Protocol) adapter built on `claude-agent-acp`, replacing the old subprocess provider. Every tool decision routes through the same four-tier interactive permission protocol kiro-cli uses (approve / trust-reads / trust / yolo), and you get reasoning-effort control, a curated model picker defaulting to Opus 4.8, the 1M-context unlock, cross-provider session continuity, live context-usage tracking, and tooling to mirror your kiro agents/skills/MCP config into the standalone provider. The dashboard ships a kiro→provider migration panel and a master-detail skill browser. (Bolin Chen)
- **Artifact Library + Unified File Viewer** — LLM-generated widgets (charts, dashboards, HTML tools) now persist as named, versioned artifacts with a browsable library instead of vanishing with chat scrollback. Bookmark any inline widget, reopen any past version full-screen, and ask the agent to iterate on it by name in a later session. The file side panel and the Artifacts library share one live-pointer model so file-backed artifacts read and write the same on-disk path, with deliberate versioning via an explicit Snapshot button, selection-to-comment popovers, and an activity timeline that deep-links to chat sessions. (Nick Bowers)
- **Side Chat (`/side`)** — A non-blocking `/side` slash command opens a multi-turn side conversation against a frozen snapshot of the parent chat's context, surfaced as a tab in the Activity panel. The sidecar is fully isolated — its messages never enter the main conversation log, vector memory, or learn store, and tools are hard-rejected — so you can ask clarifying questions without polluting the primary agent's state. (Stan Tian)
- **Native Browsing — Playwright MCP with Enterprise Auth** — The custom browser module is replaced by Playwright MCP tools plus an auth shim that auto-injects enterprise SSO cookies, giving the agent full interactive browsing of internal sites. Toggle between Chrome Extension mode (attach to your real browser) and headless mode, with a token-saving proxy. (Bolin Chen)
- **Code Reviewer — Now a Built-In App** — The Code Reviewer graduates from external app to a first-class built-in with a Python backend, SQLite store, async git operations, AI-review SSE streaming, and a directory picker. Ships a unified diff viewer, inline multi-line comments, nine syntax themes, live streaming as fixes are requested, and an IntelliJ-style Git panel (commit graph, squash, push). Just enable it from the app list — no separate install. (Robert Zhang)
- **Cron Execution History + Zero-Token Cron** — Scheduled jobs now record a persistent execution history surfaced in a dedicated Executions view and a per-job Logs tab, capturing status, timing, and live elapsed time. Two new LLM-free execution modes — a Python script mode with MCP tool access and Skip/Done/Report control flow, and a shell command mode — run deterministically in the sandbox at zero token cost. (Adam Doussan, Luke Ely)
- **Time-Limited Safety Override** — Replaces the permanent "disable all safety controls" (YOLO) mode with a time-limited override that auto-expires (24h max at startup, 6h from the dashboard, 30min from Slack) and requires re-authorization, with a 5-minute renewal grace window. Every activate/renew/expire event is audit-logged, fleet status exposes whether an override is active and when it expires, and users get expiry warnings via dashboard and Slack DM. Closes a major safety gap where controls could be turned off indefinitely. (Bolin Chen)
- **Knowledge Library — Local Folder Sources + Built-In Surface** — Add any local folder as a knowledge source: recursively scanned, auto-ingested, and re-scanned on a ~5-minute interval with per-file progress, pause/resume, and crash recovery. Each ingested file gets an AI-generated topic and theme tags. The Knowledge Library moves from an optional App Store item to an always-on built-in navigation surface, and local knowledge search gains embedding-backed semantic endpoints, a search-for-context capability, and persistent metadata. (Joe Guo)
- **Edit and Rewind Past Messages** — Hover any past user message in a dashboard chat, edit it inline, and press Enter to rewind the conversation to that point and re-run the agent. The fork-and-swap keeps the slot's identity (title, folder, color, position) unchanged so the rewind is invisible. (Nick Bowers)
- **File-Change Chips and Monaco Diff Viewer** — Chips below each assistant message show the files modified during that turn, opening a side-by-side Monaco diff viewer on click. The activity Files tab is redesigned with file-type-colored tiles, an inline file browser, and clear separation of agent-touched files from user-opened history. (Krish Dhasmana)
- **3-Tier Trust Escalation on Approval Cards** — The binary approve/deny tool prompt becomes a Kiro-CLI-style trust granularity picker: approve once, trust the exact command, trust the base command, or trust all tools for the session. Trust is session-scoped; hook deny-lists and sensitive-path checks still take precedence, and choices are captured in audit logs. (Nikhil Menon)
- **Real-Time Tool Status, Results & Inline Detail Panels** — Tool execution status and output stream into the chat UI as soon as each tool completes instead of waiting for the next JSONL flush, and tool calls expand inline to show purpose, input, and output, persisted on the message so they survive reloads. (Krish Dhasmana)
- **Redesigned Approval Workflow** — A pending approval stays visible while scrolling: when the inline approval pill scrolls off screen, a mirrored pill keeps it actionable. (Krish Dhasmana)
- **Interactive Question Cards** — Intercepts the provider's `AskUserQuestion` tool and renders an interactive card with clickable options in the dashboard. (Vishal Sreekrishnan)
- **Mobile Dashboard Access via Enterprise Tunnels** — Automatic tunnel management lets the dashboard be reached from postured mobile devices through an enterprise tunnel service, spinning up on demand. (Gabe Sanchez)
- **Slack Challenge-and-Redirect Enforcement** — Every Slack message that would reach the agent must first be verified against a dashboard session, denying requests by default if verification fails. (Gabe Sanchez)
- **`config.local.json` Overlay** — A local config overlay so user-customized settings survive upgrades instead of being overwritten by shipped defaults. (Shreyas Bhise)
- **Binary File Uploads** — `file_send` and the Slack upload path accept binary files (PDFs, images, audio, video) via MIME allowlist, with an optional channel parameter to upload directly to a tracked Slack channel. (Luca Chang)
- **Inline Audio/Video Players** — Media shared into chat renders as native inline players instead of plain download links. (David Hickox)
- **Plan from Here** — A toolbar button forks the current conversation into a new autopilot (orchestrator) session, carrying the full chat history. (Joe Guo)
- **Channel Clear-Context** — A Clear Context button on the channel header (all agents) and a per-agent clear button, behind a confirm dialog, reset LLM sessions while preserving channel configuration. (Connor LoPresti)
- **Cron Dashboard Chat Threading** — A scheduled job can thread its results into a persistent, bidirectional dashboard chat slot linked to the cron's session. (James Joseph)
- **On-Demand Cron Triggering** — Fire a scheduled job immediately via `kirocrew cron trigger` (CLI) and the `cron_trigger` MCP tool. (Imran Baig)
- **Schedule Timezone Picker** — The Calendar view respects each job's stored timezone instead of misinterpreting cron times as UTC, with a timezone picker. (Ethan Levine)
- **Jira Integration in the Task Aggregator** — Jira joins Asana and internal issue trackers as a task source, with onboarding cards, site-ID/project-key config, and status/assignee/tag filtering. (Reece Bailey, Chetan Chaku, Emmanuella Dasilva-Domingos)
- **Secretary Keyword Hooks + Auto-Reply** — Keyword-triggered workflow hooks dispatch actions (notify, spawn-session, auto-reply, emoji reactions) when configured keywords appear in watched Slack messages, with a settings-panel editor and an enable/disable toggle. (Thomas Lane, Chetan Chaku, Uday Prakash, Vishal Sreekrishnan)
- **Slack Home Tab Sessions View + Plan Usage** — A Sessions section lists recent dashboard chats and runs with one-click Resume, plus a plan-usage status line showing credits, spend, and reset date. (David Hickox, Ethan Levine)
- **Inline MCP OAuth Banner + Multi-Provider MCP Dashboard** — MCP server auth surfaces inline in chat with an Authorize link, and the MCP Integrations page shows per-scope presence (KiroCrew, Kiro global, Claude global) for every server. (Zezhen Xu, Nick Bowers)
- **Skill Directory Browser** — A three-pane master-detail layout (skill list, file tree, content viewer) with path-traversal and symlink-escape defenses, reporting which installed agents load each skill. (Zezhen Xu)
- **Federated External App Registries** — Point KiroCrew at org-owned git repositories of app definitions, managed from a Registry Manager card in the App Store. App-declared cron jobs auto-register into the scheduler on install. (Tyger Hugh, Ray Xu, Rohit Jose)
- **Start Chat from TaskKeeper / Task Aggregator Tasks** — A Chat/Work button on task rows opens a new session pre-filled with the task's context (title, details, due date, links). (Takahiro Ishii, Vasanth Subramanian)
- **TaskKeeper Duplicate Grouping & Merging** — TaskKeeper visually groups similar inbox mentions and lets you merge duplicates with an LLM-drafted merged title. (Joe Pontone)
- **Quick Send** — A setting to send a suggested-reply option with a single click; Shift+Click switches to multi-select. (Maninder Singh)
- **Keyboard Shortcuts to Cycle Agent & Approval Mode** — Alt+Shift+A/Z cycle through installed agents; Alt+Shift+D/C cycle approval mode. (Wilson Wu)
- **Duplicate Chat Slot, Drag-Reorder Folders & Per-Folder Default Agent** — Hover-Duplicate forks a session into a new slot; root folders drag-reorder with a grip handle; each folder can define a default agent for new sessions. (Wilson Wu, Jingchao Cao, Naoya Ishikawa)
- **Per-Slot Message Cache** — Switching between previously-visited chats restores instantly from a Redux cache with no scroll jump. (Luke Ely)
- **JSONL File Viewer** — Renders `.jsonl` files as per-line collapsible trees with infinite-scroll pagination. (Pramod Dudhi)
- **IntelliJ Plugin Embed Mode** — An additive `/embed/*` route set renders a stripped-down chat UI (multi-tab strip, nav bridge, minimal settings) for IDE embedding. (Krish Dhasmana)
- **Run in Terminal Button** — Shell code blocks gain a Run in Terminal button gated by a two-click warning for dangerous patterns. (Shreyas Bhise)
- **Selection Toolbar for Comment & Copy** — An explicit selection toolbar in the markdown panel replaces the implicit move-mouse-away comment popover. (Dayong Li)
- **`/interrupt` Endpoint** — Stop the current turn while keeping the message queue intact so a queued message is picked up immediately. (Naoya Ishikawa)
- **Configurable Subagent Completion Truncation** — Control which end of a subagent's transcript survives in the completion event (head, tail, or both). (Greg Rebholz)
- **Per-Subagent Attribution in Tool Hooks** — `PreToolUse`/`PostToolUse` hooks optionally carry `subagent_id`, `parent_session_key`, and `agent_role`. (Arpit Vyas)
- **External IAM-Authenticated Embeddings (SigV4)** — The embedding endpoint can point at an IAM-authenticated API Gateway in front of Ollama, removing the SSH reverse-tunnel dependency. (Toby Wong)
- **More Skill Auto-Extraction Triggers** — Auto-skill extraction fires on session idle expiry, explicit Slack session end, and via a new `consolidate` CLI for manual backfill. (Shayan Yaseen)
- **Native arm64 Python for Apple Silicon** — The `osx_arm64` bundle uses system arm64 Python instead of the x86_64 build overlay, running natively without Rosetta. (Apoorv Srivastava)
- **Remote Desktop Sync Script** — A standalone `sync-to-remote.sh` copies a KiroCrew install to a remote desktop with custom SSH port and `--dry-run`. (Huan He)
- **Service-Aware `kirocrew restart`** — Restarts the installed systemd/launchd service when present, otherwise cleanly bounces the foreground gateway. (Nick Bowers)
- **LLM-Powered Link Summaries** — Bare URLs in navigation are summarized by the model in a single batched request, removing an N+1 bottleneck. (Nansong Yi)
- **Seeded Home Fixtures & Test Harness** — Two ready-to-use populated KiroCrew home fixtures (minimal + rich) plus a `spawn_feature_gateway` helper for integration tests and evals. (Simon Meyffret)

### Security

- **Time-Limited Safety Override** — replaces permanent YOLO with an expiring, audit-logged safety override so auto-approve can never be left on indefinitely. (Bolin Chen)
- **Isolated Config Directory for the Spawned Standalone Provider** — points the spawned `claude-agent-acp` subprocess at a KiroCrew-owned `CLAUDE_CONFIG_DIR` (settings file written `0600`), strips inherited `permissions.allow/ask` so every tool routes through the host deny gate, and guards against overwriting the operator's real `~/.claude`. (Bolin Chen)
- **Per-Segment Shell Deny Evaluation** — deny patterns are evaluated per shell segment, closing a command-chaining/substitution bypass. (Chetan Chaku)
- **Slack Challenge-and-Redirect** — direct Slack requests must be verified against a dashboard session, denying by default on failure. (Gabe Sanchez)
- **Token-Free Slack Pins/Reactions Proxy** — gateway routes let local callers pin and react without ever holding the Slack bot token. (Akim Akimov)
- **Block Prompt Injection via Comment Newlines** — the comment-formatting escaper neutralizes raw newlines that could inject a fake system prompt. (Arpit Vyas)
- **Enterprise Grid Workspace Allowlisting** — per-message origin checks use an audited in-memory allowlist of validated child-workspace IDs; the config loader retains both `E`- and `T`-prefix IDs. (Ken Harrison)
- **Scoped CSP for Enterprise Tunnels** — enterprise tunnel and app WebSocket connections are allowlisted in CSP `connect-src` without broadening the policy. (Justin Bess)
- **Internal-Secret Path for `/api/chat`** — local integrations (Zoom watcher, cron scripts) calling from localhost with an internal secret no longer get 403. (Luca Bruera)

### Fixes

- claude-agent-acp hardening: unblock `/compact`, honor `autocompact_pct`, resolve node binary explicitly, meaningful tool-pill labels, fix standalone-provider slots failing to start (Hugo Costa, Patrick Gao)
- Stop duplicate user message re-injected into fresh-slot history (Raymond Chen)
- Recover memory embeddings when Ollama starts late; embedding-dimension passthrough for custom models (Arpit Vyas)
- Reset session on mid-session project change; fork/rewind validate chained history; sync dashboard metadata to remote (Simon Meyffret)
- Channel agents inherit global model; reset agent-to-agent exchange budget on human input (Connor LoPresti)
- Stale-turn detection no longer disabled by passive events; restore autopilot routing for multi-select plan options; quieter sandbox startup logging on macOS 26+ (Joe Guo)
- Org-wide Slack installs route outbound calls to the right workspace (Angelo Yang)
- Thread context reconstructed when bot is @mentioned mid-thread (Maksym Yachnyi)
- IME composition Enter no longer sends prematurely (Nansong Yi)
- Voice playback fixes for neural voices and CSP; macOS dashboard metrics under LaunchAgent (Naoya Ishikawa)
- App-installed skills are now discoverable (Greg Chapman)
- Subagent auto-approval fallback when parent session is gone (Zeiad Zaf)
- Repair invalid hook keys in kiro-cli agent config (Bolin Chen)
- Restore Code Review tools in builder-mcp config (Kevin Goldberg)
- Cron editor parses named day-of-week values; transient parse error on Schedule page over a tunnel (Dinesh Mathan, Sam Oldak)
- Prune uninstalled managed agents during sync (Yehui Zhang)
- Mobile dropdown stays open while scrolling (Helena Stafford)
- Markdown widget rendering inside inline code (Nick Bowers)
- Session cleanup loop survives unexpected exceptions (Chris Paton)
- `kirocrew doctor` detects Docker-based Ollama (Shreyas Bhise)

### Testing & Quality

- Seed fixtures (minimal + rich) and `spawn_feature_gateway` test harness for reproducible gateway integration tests (Simon Meyffret)
- jscpd duplication gate + Vitest cobertura coverage with Coverlay CI integration
- Flaky-test cleanup across `shutdown_event`, cron `skip_dates`, and per-session error paths

### Contributors (99)

Adam Doussan, Akim Akimov, Albert Achtenberg, Alec Douglas, Alexander Blom, Angelo Yang, Apoorv Srivastava, Arpit Vyas, Beau Bright, Bharath Janyavula, Bhavana Chinthalapally, Bolin Chen, Chen Tong, Chetan Chaku, Chris Paton, Chris Wundram, Connor LoPresti, David Hickox, Dayong Li, Di Wu, Dinesh Jayapalan, Dinesh Mathan, Emmanuella Dasilva-Domingos, Eric Muessel, Ethan Levine, Felipe Barajas, Gabe Sanchez, Greg Chapman, Greg Rebholz, Helena Stafford, Huan He, Hugo Costa, Imran Baig, Jake Zhao, James Joseph, Jingchao Cao, Joe Guo, Joe Pontone, Justin Bess, Kaiwei Luo, Ken Harrison, Kevin Goldberg, Kiavash Samadi, Krish Dhasmana, Krunal Patel, Luca Bruera, Luca Chang, Luke Ely, Madhur Bajaj, Maksym Yachnyi, Maninder Singh, Matt Pierringer, Matthew Muncy, Matthieu Dufour, Milos Chaloupka, Nansong Yi, Naoya Ishikawa, Nathan Beals, Nick Bowers, Nick Gonzales, Nick Papadopoulos, Nikhil Menon, Nischal Kumar, Patrick Gao, Pramod Dudhi, Projjol Banerji, Qifeng Huang, Rabinarayan Patra, Raghav Bhardwaj, Raj Puram, Ray Xu, Raymond Chen, Reece Bailey, Robert Zhang, Rochak Gupta, Rohit Jose, Sam Oldak, Sean Iamartino, Sergey Chebotarev, Shayan Yaseen, Shreyas Bhise, Shuli He, Simon Meyffret, Stan Tian, Stif Spear Subba, Swapnil Gaikwad, Sypher Su, Takahiro Ishii, Thomas Lane, Toby Wong, Tyger Hugh, Uday Prakash, Vasanth Subramanian, Vishal Sreekrishnan, Wilson Wu, Yagna Gurjala, Yehui Zhang, Zeiad Zaf, Zezhen Xu

## [2.5.0] — 2026-05-19

111 commits across 2 packages (68 KiroCrew + 43 KiroCrewWebsite), 57 contributors since v2.4.1.

### Features

- **Widget Event Bridge** — interactive `data-action` elements on mcwidgets. Click triggers `[UI] action: {payload}` message to agent. Named form inputs auto-collected into `formData`. Enables generative UI patterns. (Bharath Janyavula)
- **App SDK Gateway Hooks** — apps register `on_message`, `on_tool_call`, `on_session_start` handlers to intercept/transform agent behavior. Full event bus with typed payloads. (Ray Xu)
- **Session Tags & Trello Sidebar** — tag-based session organization with folders, columns, and drag-drop. Persistent across restarts. (Akim Akimov)
- **Steering Files** — auto-load workspace `.kiro/steering` files into kiro-cli sessions. Project-specific instructions injected as context automatically. (Zhuoyu Li)
- **Piper TTS** — local neural text-to-speech as offline alternative to AWS Polly. `auto_reply_to_voice` toggle for symmetric voice-in/voice-out. Provider abstraction with OS-level sandbox. (Matt McLeod)
- **LLM Usage Tracking** — daily-sharded JSONL token persistence with stacked bar chart. Cascading provider/model filters, per-day cross-tab filtering. (Hoang Phan)
- **Composable Gateway CLI** — `--port auto`, `--json-ready`, `--approval`, `--test-mode` flags for testing harness and CI. (Simon Meyffret)
- **`kirocrew service` Command** — `install/uninstall/status` for system-level systemd (Linux) and launchd (macOS). (Roberto Matarrita Arce)
- **Chat Embedding for Apps** — `useChatSession` hook, `ChatPanel`, `ChatEmbed`, `ChatMessageList`. Apps embed KiroCrew chat in their UI. 63 tests. (Anirudh Narayanan)
- **Knowledge Library DOCX** — ingest Word documents with heading-aware chunking. Hybrid BM25+vector search via `/kb` endpoints. Frontend KnowledgePicker. (Joe Guo)
- **Export/Import Config** — one-click zip export/import for settings and memory. Security exclusions for secrets/keys. 37 tests. (Bolin Chen)
- **Reasoning Effort Selector** — per-slot dropdown (Default/Low/Medium/High/Max) plumbed to the standalone provider's `--effort` flag. (Hoang Phan)
- **Fix with AI** — button on failed app installs opens pre-filled AI chat with error log and setup instructions. (Ray Xu)
- **Builtin App Auto-Discovery** — frontend auto-discovers builtin app routes. `dependencies.aim` resolved on enable. (Ray Xu)
- **Navigation Panel** — context-aware link labels for quick access to referenced resources. (Nansong Yi)
- **Drag-Reorder Apps** — reorder Apps section in sidebar via drag-and-drop. (Kishore Baskar)
- **Shareable Session URLs** — deep-link to specific messages within sessions. (Nikhil Menon)
- **Sidebar Context Menu** — ⋮ more button with Pin, Close, Toggle Read/Unread. (Ezzat Qupty)
- **Resizable Thread Panel** — drag-resize channel thread sidebar, width persisted. (Arpan Banerjee)
- **One-Click Slack App** — `kirocrew manifest --url` generates pre-filled Slack app creation URL. (Yifan Liu)
- **Cron Jitter** — random delay (0-20min hourly, 0-2h daily) to spread load. `strict_schedule` opt-out. Per-job `timeout_secs`. (Joe Guo, Axel Vidales)
- **Subagent Timeout Config** — `subagent_timeout_secs` in agent config, default 1800s. (Luis Gabriel Lima)
- **Session CWD** — `slot.project` wired as cwd to kiro-cli process. Pool reuse when cwd matches. (Hoang Phan)
- **Auto-Compaction Notice** — visual notice in chat when context is auto-compacted. (Sean Iamartino)
- **TaskKeeper Triage Quality** — split scan response, skip_reason field, better context fetching, link extraction. (Joe Pontone, Brent Naylor)
- **Electron Multi-Tab** — connect to multiple gateways from single Electron window. WebContentsView refactor. (Saran Kota, Mihir Dhamankar)
- **Electron Remote Tunnel** — auto-discover kirocrew binary over SSH. (Leo Zhadanovsky)
- **Mobile Swipe Gesture** — swipe to open/close mobile chat sidebar. (Vishal Sreekrishnan)
- **Token Auth Bypass for Apps** — `/apps/{name}/ui/*` GET/HEAD bypass auth. (Shubhranshu Kumar)
- **Agent SOP Discovery** — `rglob` for nested `agent-sops/<agent>/*.sop.md` in managed agent packages. (August Vilakia)

### Security

- **XSS Sanitization + CSP** — `rehypeSanitize` strips dangerous elements. Content-Security-Policy middleware. Fixes a high-severity XSS finding. (Bolin Chen)
- **CC Sandbox Mode** — 108 deny patterns for credential exfiltration, destructive ops, reverse shells. (Joe Guo)
- **Git Push Deny Unification** — scoped exception for stash, anchored regex. (Kan Zhu)
- **Process Leak Fix** — reduce per-session MCP footprint, silence 404 retry storm, kill escaped child processes. (Akim Akimov, Bharath Janyavula)
- **Kill-Regex Anchor** — prevent regex injection in kill-kirocrew pattern. (Simon Meyffret)
- **CSP Widget Fix** — blob: URL rendering + CDN allowlist for widget iframe scripts. (Bolin Chen)

### Bug Fixes

- sandbox cross-fs tmpfs bind + env propagation (Akim Akimov)
- OPTIONS parent message edit-in-place (Ethan Levine)
- .venv/bin/kirocrew exec when present (Ahmed Hassanin)
- prevent duplicate kirocrew-lite config (Raghu Burukunte)
- secretary bot_id derivation from user.isBot (Ethan Levine)
- dismissed messages resurfacing in dormant channels (Eric Muessel)
- standalone provider eager reconnect on process death (Patrick Gao)
- standalone provider augmented_path for the agent CLI (Vitor Durante)
- MCP toggle 404 for servers in other scopes (Helena Stafford)
- MCP sync tools/allowedTools after install/uninstall (August Vilakia)
- pysqlite3 import for AL2 FTS5 compatibility (Rony Jacob John)
- subagent completion events as user messages (Hoang Phan)
- cron timezone uses config instead of system UTC (Milos Chaloupka)
- Ollama model name hardcoded constant (Patrick Gao)
- Slack home tab reads from vector store (Mujahed Syed)
- unrecognized bang commands caught (Raghav Bhardwaj)
- null team field in Enterprise Grid payloads (Rohit Ingle)
- streaming-diff React removeChild crash (Jeff Neuberger)
- widget utf-8 charset + openInNewTab hardening (Shuolei Jin)
- Packaged-install bin path in Electron find-bin (Vitor Durante)
- file attachment leak across chat sessions (Tony Hardie)
- install.sh builds KiroCrewWebsite (Warren Bui)
- managed-agent CLI PATH augmentation (Filip Godina)
- STT missing deps + pin openssl (Helena Stafford)
- validate tracking_channels config format (Rohan Kumar)
- setup timezone retry loop (Chad Bailey)
- stop_stream finalizes with clean text (Marvellous Adedapo)
- unread markers persist across refresh (Ezzat Qupty)
- shared MCP servers in rebuild_agent_config (Landon Coe)
- channel-agent sessions exempt from idle expiry (Arpan Banerjee)
- system metrics pill toggle-on-click (Kishore Baskar)
- sidebar dark-mode button chrome (Teodor-Gabriel Oprescu)
- follow-up option buttons wrap (Emma Zhou)
- vscode:// URLs through sanitizer (Nick Gonzales)

### Testing & Quality

- 143 tests for GatewayOrchestrator (Simon Meyffret)
- HMAC integrity + 4 KB scan edge-case tests (Simon Meyffret)
- 46 tests for process-leak follow-up (Akim Akimov)
- ACP client coverage 48% → 81% (Simon Meyffret)
- Security-critical modules 80%+ coverage (Madhur Bajaj)
- pytest-timeout, xdist worksteal, 5-min builds (Patrick Gao)
- jscpd duplication check gate (Simon Meyffret)
- vitest cobertura for Coverlay (Simon Meyffret)
- Agent test isolation from local MCP config (Rony Jacob John)

### Documentation

- README rewritten 770 → 158 lines, feature lists moved to docs/features.md (Bolin Chen)
- AGENTS.md updated for both packages (Bolin Chen)
- Deprecate !dashboard in favor of /kirocrew dashboard (Sam Oldak)
- Persistent SSH tunnel setup for macOS LaunchAgent (Sai Chaitanya Manchikatla)
- model collaborator reminder in setup wizard (Yohanes Setiawan)

### Contributors (57)

Ahmed Hassanin, Akim Akimov, Anirudh Narayanan, Arpan Banerjee, August Vilakia, Axel Vidales, Bharath Janyavula, Brent Naylor, Brian Thomas, Chad Bailey, Emma Zhou, Eric Muessel, Ethan Levine, Ezzat Qupty, Filip Godina, Helena Stafford, Hoang Phan, Jeff Neuberger, Joe Guo, Joe Pontone, Kan Zhu, Kishore Baskar, Landon Coe, Leo Zhadanovsky, Luis Gabriel Lima, Madhur Bajaj, Marvellous Adedapo, Matt McLeod, Mihir Dhamankar, Milos Chaloupka, Mujahed Syed, Nansong Yi, Nick Gonzales, Nikhil Menon, Patrick Gao, Raghav Bhardwaj, Raghu Burukunte, Ray Xu, Roberto Matarrita Arce, Rohan Kumar, Rohit Ingle, Rony Jacob John, Sai Chaitanya Manchikatla, Sam Oldak, Saran Kota, Sean Iamartino, Shubhranshu Kumar, Shuolei Jin, Simon Meyffret, Teodor-Gabriel Oprescu, Tony Hardie, Vitor Durante, Vishal Sreekrishnan, Warren Bui, Yifan Liu, Yohanes Setiawan, Zhuoyu Li

## [2.4.1] — 2026-05-12

Hotfix release.

### Features

- **Knowledge Library Lazy Pool** — knowledge LLM pool starts on first use instead of gateway boot. Grill skill adds confirmation gate before executing. (Joe Guo)
- **Knowledge Agent Config** — auto-generates kirocrew-knowledge agent config for Knowledge Library queries. (Joe Guo)
- **Chat Input During Compaction** — input stays enabled during context compaction so messages queue instead of being lost. (Simon Meyffret)

### Bug Fixes

- fix: remove high-risk scopes from default Slack manifest (Eric Hays)
- fix(session): exempt channel-agent sessions from idle expiry (Arpan Banerjee)
- fix(dashboard): correct kwargs in api_reveal_path log_tool_invocation (a KiroCrew contributor)
- fix: suppress repeated sandbox warning on macOS 26+ (Joe Guo)
- fix: mock platform.mac_ver in sandbox probe tests for macOS 26+ compat (Bolin Chen)
- Revert parseBlocks inline-code fix (caused widget rendering regression) (Bolin Chen)
- fix(shortcuts): skip Alt+Arrow chat swap when focus is in text input — preserves macOS Option+Arrow word-jump and Option+Shift+Arrow word-select (Bolin Chen)

### Refactoring

- refactor: remove code_package, sharepoint, and url connectors from Knowledge Library (Joe Guo)

## [2.4.0] — 2026-05-12

184 commits across 2 packages (102 KiroCrew + 82 KiroCrewWebsite), 59 contributors since v2.3.3.

### Features

- **Knowledge Library** — full-stack knowledge management with ingestion, graph retrieval, and auto-watch. Backend: SQLite store with FTS5 full-text search, chunker, extractor, LLMPool for summarization, graph-based retrieval with Reciprocal Rank Fusion. Connectors for local files, code packages (git), wiki/doc sources, and URLs. Frontend: grouped-by-source list, D3 force-directed graph, sources tab, optimistic mutations. 120 tests. (Joe Guo)

- **Task Aggregator** — autonomous task management app aggregating internal issue trackers and Asana into a unified dashboard. 4-step onboarding wizard, stat cards (Executing/Blocked/Waiting/Completed), agent assignment for autonomous processing, MCP server exposing 7 tools, GraphQL task-tracker client with enterprise SSO auth, sequential multi-agent cron execution. (Bhavana Chinthalapally)

- **Grill Skill** — built-in structured pre-task questioning protocol. Decision-tree interview walks each branch one question at a time, checks memory before asking (skips already-decided questions), saves every answer via `learn_add(scope="workspace")`, provides recommended answers, auto-stops when plan is clear. Triggers on "think this through", "poke holes", "grill me", etc. (Joe Guo)

- **Multi-Provider MCP Management** — unified MCP server management across kiro-cli, the standalone provider, and KiroCrew. Per-scope dashboard badges showing provider membership, batched `POST /api/mcp/apply` endpoint with preservation rule, always-render standalone-provider agent artifacts, `_is_valid_mcp_name` security hardening (charset allowlist, 128-char cap, path traversal rejection). (Nick Bowers)

- **Python-Controlled Autopilot Stage Loop** — replaces LLM-controlled stage advancement with deterministic `_stage_loop()`. Fixes 5 recurring regressions (stages in single turn, Go All stalling, non-deterministic boundaries). Per-stage result capture to disk, subagent wait polling (2s interval, 5min max), compacted prior results (30% head / 70% tail). (Joe Guo)

- **Warm Pool Health-Check Loop** — proactive 30s sweep of pooled kiro-cli processes, discards dead/expired providers, triggers replenishment. Fixes orphan sweep killing healthy pool processes by including pool PIDs in active set. (Hoang Phan)

- **Prompt Optimizer** — native pre-send prompt rewriting via Cmd+Shift+Enter or sparkle button. Dedicated session (no semaphore contention), 30s timeout, context-aware (last ~10 messages), security redaction. User reviews before sending. (Yohanes Setiawan)

- **Notification Sounds** — Web Audio API notification system with 4 preset tones (chime, ding, blip, pop). Master toggle + volume slider, per-category overrides (cron, approval, hook, heartbeat, subagent, taskrunner), SSE `mc-notification` CustomEvent dispatcher, 300ms debounce. Settings in Dashboard → Notifications. (Teodor-Gabriel Oprescu)

- **Quote-Reply** — select text in assistant messages → floating toolbar with Quote and Copy. Clicking Quote inserts blockquote into input with Safari-download-style spring animation (FlyingQuote). Supports multiple stacked quotes. (Zezhen Xu)

- **In-Session Message Search** — VS Code-style Cmd+F / Ctrl+F search bar. Per-occurrence matching across all messages (including virtualized), DOM TreeWalker highlighting, case-sensitive toggle, Enter/Shift+Enter navigation, MutationObserver for async code blocks. (Nikhil Menon)

- **Table of Contents Drawer** — right-side drawer for markdown file viewer. Extracts ATX/setext headings (skipping fenced code), slugified id attributes on h1-h3, index-based scroll navigation. (Zhuoyu Li)

- **Collapse Large Pastes** — pastes ≥3 lines or ≥200 chars collapse to `[ Paste #N · M lines ]` tokens. Atomic keyboard handling, click-to-expand, framer-motion spring animation on chips in sent messages, content-addressed localStorage persistence (200-entry cap), Edit & Resend expansion. (Krish Dhasmana)

- **Sidebar Context Menu** — right-click dropdown replacing direct rename-on-right-click. Menu items: Rename and Mark as Unread. Viewport edge clamping, Escape-to-close, ARIA roles. (Ezzat Qupty)

- **Unread-Only Filter** — sidebar toggle filtering to unread sessions only. Live count badge (capped 99+), auto-drain when inbox reaches zero, localStorage persistence, dynamic aria-label with count. (Teodor-Gabriel Oprescu)

- **Bulk Session Cleanup** — `POST /api/chat/slots/cleanup` endpoint archives stale sessions by configurable inactivity threshold. Skips active slot and pinned sessions. Frontend "Clean Up" button with dry-run preview. (Nikhil Menon)

- **CodeReview Read/Write Actions** — exposes `CodeReviewReadActions` and `CodeReviewWriteActions` via `--include-tool-tags default,code-review`. Prompt-per-call (not auto-approved). Also fixes `_inject_skill_paths` corrupting flag-with-value args. (Kevin Goldberg)

- **Node.js Backend Support** — adds Node.js as app backend type with nvm/system node resolution. Adopts healthy existing instances on port conflict, preserves app_secret during updates. (Ray Xu)

- **Jump to Previous User Message** — ArrowUp button at top-centre of chat scrolls to nearest user message above viewport with 72px offset. Repeated clicks chain through history. (Simon Meyffret)

- **--no-open Flag** — `kirocrew gateway --no-open` and `dashboard.auto_open_browser` config suppress automatic browser open. Used by Electron app to avoid redundant tab. (Bobby Earl)

- **Subagent CWD** — `cwd` parameter on `spawn_run` launches subagents in a caller-specified directory. Gated by `subagent_cwd_allowed_roots` config. (Simon Meyffret)

- **Subagent Orphan Recovery** — folder-per-agent persistence at `~/.kirocrew/subagents/{id}/` with state.json, result.txt, tombstone.json. On gateway restart: reconciles orphaned processes, delivers results, tombstones failures. Reaper prunes stale tombstones after 7 days. (Hugo Costa)

- **Task Runner Pause/Resume** — pause and resume running tasks without losing progress. Crash recovery transitions running→paused on restart. `force_approval` gates block even in YOLO mode. (Matt Pierringer)

- **Code-Approvers Tier Routing** — tier-based reviewer routing via a code-approvers config. T1 (1 core), T2 (2 core for security/harness), T3 (both owners for frozen modules). Drift validator test fails build on pattern mismatch. (Simon Meyffret)

- **Managed Tool Policy** — `managedToolPolicy` config for per-agent MCP tool filtering. Allows restricting which tools an agent can access. (Ray Xu)

- **Heartbeat Dashboard Delivery** — `prompt:dashboard:<slot>` delivery mode injects heartbeat results into specific dashboard chat slots. Slack suppressed for incomplete tasks (HEARTBEAT_KEEP). (Simon Meyffret)

- **AutoNudge Stop Sentinel** — per-slot `{{STOP_FILE}}` template in nudge messages. Auto-defaults stop sentinel path per slot. (Akim Akimov)

- **Managed Agent Bidirectional Sync** — agent-granular sync across kiro and CC. Uninstalls skills alongside agents/plugins to prevent reinstall on rebuild. (Nick Bowers)

- **Auto-Discover Hooks** — `~/.kiro/hooks/*.sh` auto-imported at agent boot. Parses `# event:` / `# matcher:` headers, enforces caps (10 per-event, 20 total), security validation. (Simon Meyffret)

- **Script Hooks for All Code Paths** — hooks now fire for chat, cron, subagent, and task runner execution paths (not just chat). (Jack Bandon)

- **Contextual Prompt Suggestions** — background LLM generates contextual follow-up suggestions after each response. (Zezhen Xu)

- **Per-phase reaction suppression** — set any value in `slack.reactions` to `null` to suppress just that phase. (Ethan Levine)

- **TaskKeeper /tk Command** — `/tk` slash command for quick-notes, writes to `~/.taskkeeper/quick-notes.json`. (Joe Pontone)

- **Seed Fixtures** — `kirocrew gateway --seed <fixture> [--seed-replace]` for reproducible testing environments. (Simon Meyffret)

- **File Search API** — per-project in-memory file index with fuzzy scoring for `@`-file references. (Hoang Phan)

- **Widget Prompt Template** — configurable `{{WIDGET_BLOCK}}` density for dashboard-only widget instructions. (Joe Guo)

- **Document Parser** — stdlib-only .docx/.pdf/.pptx extraction for Slack file attachments. (Bolin Chen)

- **Auto Skill Creation (Hermes loop)** — KiroCrew can now synthesize reusable skills from your conversations. After a multi-step session (≥5 tool calls by default), the existing idle consolidator asks the background LLM whether the procedure was worth saving, and if so writes a `~/.kirocrew/skills/auto/<slug>/SKILL.md` with triggers, step-by-step procedure, and provenance. On reuse, if the agent finds a better procedure, the skill updates itself. **Disabled by default** — opt in via `kirocrew config set skills.auto_create_from_sessions true`. Auto-generated skills live under the `auto/` namespace so they never collide with hand-authored ones. All writes are gated by sensitive-session detection, output redaction (credentials + exfiltration URLs), similarity-based dedup, and SEL audit logging. (Shayan Yaseen)

- **Inline Interactive Widgets** — `<mcwidget>` tags in assistant responses render as sandboxed iframes with Tailwind CSS, auto-resize (100-800px), expand/minimize, open-in-new-tab. Ocean-of-dots loading animation during streaming. Theme-aware via CSS variables. (Zezhen Xu, Simon Meyffret)

- **Render User Messages with Markdown** — user messages now render with full markdown formatting (bold, links, code, lists) instead of plain text. (Maninder Singh)

- **Stage Progress Indicator** — `chat_status` WebSocket event drives a visual stage progress bar during autopilot execution. (Joe Guo)

- **Unread Chat Count Badge** — Chat nav tab shows live unread count badge. (Nick Papadopoulos)

- **Edit Button for Options** — prefill selected `[OPTIONS]` choices back into chat input for modification before sending. (Teodor-Gabriel Oprescu)

- **Reveal in Sidebar** — header button scrolls sidebar to highlight the active session. (Roman Ivanov)

- **Prettier Code Blocks** — improved code block styling with replaced inline followUp options and general cleanup. (Krish Dhasmana)

### Bug Fixes

- 🔒 fix: block chmod/chown on system paths (/usr/, /etc/, /sbin/, /boot/, /lib/, /lib64/) in deny patterns — prevents remote-desktop bricking (Joe Guo)
- fix: use bracket access on sqlite3.Row in sync_source handler (Joe Guo)
- fix(dashboard): persist cron agent on PATCH — UI sends agent field (Sean Iamartino)
- fix(mimir): register mimir routes at startup to avoid frozen router error (Bhavana Chinthalapally)
- fix(acp): add streaming staleness timeout + remove mode identity announcements (Joe Guo)
- fix: deduplicate agent config — make src/kiro_crew/config/ single source of truth (Joe Guo)
- fix(agent-sync): uninstall skills too so sync does not reinstall package (Nick Bowers)
- fix: stop syncing kirocrew MCP servers to global mcp.json (Joe Guo)
- fix(pool): reset pool state on provider switch so warm pool refills (Hoang Phan)
- fix(pool): persist CWD in session_map for accurate resume (Hoang Phan)
- fix(mcp): support Content-Length framing in MCP stdio transport (Michael Viscardi)
- fix(mcp): walk ancestor PIDs to resolve session key in warm pool (Hoang Phan)
- fix(dashboard): route subagent injection through _run_chat for full streaming (Luke Ely)
- fix(dashboard): auto-revive slot for session=origin cron injection (Gavin Tse)
- fix(dashboard): trust loopback origins regardless of port (Sam Oldak)
- fix(dashboard): pass timezone from cron create/update API to job storage (Dallin Kooyman)
- fix(dashboard): fork inherits folder_id so new slot lands in parent's folder (Simon Meyffret)
- fix(dashboard): resolve symlinked _DIST_DIR in pwa_file guard (Simon Meyffret)
- fix(dashboard): stop currency symbols from triggering KaTeX math parsing (frontend, Krish Dhasmana)
- fix(heartbeat): suppress Slack delivery for incomplete tasks (HEARTBEAT_KEEP) (Chen Tong)
- fix(orchestrator): raise plan threshold to 4+ stages (Joe Guo)
- fix(taskkeeper): persist CandidateStore so pending survive restart (Eric Zhang)
- fix(autonudge): auto-default per-slot stop sentinel + {{STOP_FILE}} template (Arpan Banerjee)
- fix(chat): drain stale _pending chunks when SSE reader disconnects (Luca Bruera)
- fix(secretary): reply in-thread for channel/group messages (Geet Sawhney)
- fix: single authoritative config writer (rebuild_agent_config) (Nick Bowers)
- fix: catch ConnectionResetError/BrokenPipeError in ACP client with retry logic (Selena Wang)
- fix: use config_dir() for all config paths in CLI (Addison Tustin)
- fix: stabilize flaky tests in CI (Joe Guo)
- fix: sandbox probe false-positive on macOS 26+ — early-exit on darwin ≥ 26 where sandbox-exec is broken for third-party binaries (Joe Guo)

### Refactoring

- refactor: split chat.py (5,405 lines) into 12 focused modules (Joe Guo)
- refactor: split cli.py and chat.py into focused modules (Joe Guo)
- refactor: split session.py into session_pid.py and session_map.py (Joe Guo)

### Contributors (59)

Alec Douglas (Alec Douglas), Albert Huang (Albert Huang), Akim Akimov (Akim Akimov), Ariana Morgan (Ariana Morgan), Arpan Banerjee (Arpan Banerjee), Ben Grubin (Ben Grubin), Bhavana Chinthalapally (Bhavana Chinthalapally), Bobby Earl (Bobby Earl), Bolin Chen (Bolin Chen), Chen Tong (Chen Tong), Chenying Han (Chenying Han), Dallin Kooyman (Dallin Kooyman), David Fayerman (David Fayerman), Dinesh Mathan (Dinesh Mathan), Eric Zhang (Eric Zhang), Ethan Levine (Ethan Levine), Ezzat Qupty (Ezzat Qupty), Gavin Tse (Gavin Tse), Geet Sawhney (Geet Sawhney), Andrew Golightly (Andrew Golightly), Hoang Phan (Hoang Phan), Hugo Costa (Hugo Costa), Jack Bandon (Jack Bandon), Jimmy Kilpatrick (Jimmy Kilpatrick), Joe Guo (Joe Guo), Joe Pontone (Joe Pontone), Kevin Goldberg (Kevin Goldberg), Kotaro Inoue (Kotaro Inoue), Krish Dhasmana (Krish Dhasmana), Krunal Patel (Krunal Patel), Luca Bruera (Luca Bruera), Luke Ely (Luke Ely), Maninder Singh (Maninder Singh), Manuel Chavez (Manuel Chavez), Mark Lord (Mark Lord), Matt Pierringer (Matt Pierringer), Michael Viscardi (Michael Viscardi), Milos Chaloupka (Milos Chaloupka), Nick Bowers (Nick Bowers), Nick Papadopoulos (Nick Papadopoulos), Nikhil Menon (Nikhil Menon), Patrick Gao (Patrick Gao), Ray Xu (Ray Xu), Roman Ivanov (Roman Ivanov), Sam Oldak (Sam Oldak), Sean Iamartino (Sean Iamartino), Selena Wang (Selena Wang), Shayan Yaseen (Shayan Yaseen), Shihao Wang (Shihao Wang), Simon Meyffret (Simon Meyffret), Sivan Cooperman (Sivan Cooperman), Teodor-Gabriel Oprescu (Teodor-Gabriel Oprescu), Tony Hardie (Tony Hardie), Addison Tustin (Addison Tustin), Tyger Hugh (Tyger Hugh), Vasanth Subramanian (Vasanth Subramanian), Vitor Durante (Vitor Durante), Yohanes Setiawan (Yohanes Setiawan), Zezhen Xu (Zezhen Xu), Zhuoyu Li (Zhuoyu Li)

## [2.3.0] — 2026-05-02

324 commits across 2 packages, 91 contributors since v2.2.0.
Frontend split into dedicated KiroCrewWebsite package.

### Features

- **Packaged Install** — one-command install with native platform-specific bundles for common Linux distros and macOS (x86_64 + aarch64). No repo clone needed. Includes auto-prerequisite installation (kiro-cli and the managed agent package manager), `kirocrew doctor` stale-path checks, and migration from one-line install. (Joe Guo, Bolin Chen)

- **TaskKeeper** — personal task management app in the dashboard. Scan Slack mentions and Outlook emails for actionable items, triage with LLM-powered confidence scoring, accept/skip/merge candidates, and sync bidirectionally with Microsoft To-Do. Includes auto-scan polling, duplicate detection, and bulk operations. (Joe Pontone)

- **Cooperative Stop** — Stop button sends ACP `session/cancel` first and falls back to hard kill only after a configurable budget (default 10s). Preserves kiro-cli session state in the common case — stop latency drops from ~11s to <1s. Pulsing Stop button, inline StopEventCard in transcript, Slack ephemeral with Kill Now button. Double-tap forces immediate kill. Cancelled turn context re-injected on next prompt. (Ben Grubin)

- **AutoNudge** — reactive same-session self-nudge service. A dashboard chat slot keeps working toward a goal by re-injecting a configured nudge message whenever it goes idle. Unlike cron, runs in the same slot with warm memory and tools. Survives browser disconnect and gateway restart. Composer toolbar icon with popover for start/save/stop. (Akim Akimov)

- **Streaming STT** — live speech-to-text via AWS Transcribe Streaming. Words appear in the chat input as you speak instead of waiting for recording to finish. First-word latency reduced from 7560ms to 761ms. Concurrency cap (3) and duration cap (300s) prevent cost runaway. (Simon Meyffret)

- **Tool Tracking v2** — inline tool pills show live execution state (spinner/shield/checkmark/rejected). Approval banner renders inside the chat input bar with hover-to-preview and click-to-pin. Simplified tool names show agent's purpose instead of raw commands. Batch rejection auto-rejects remaining tools when one is denied. (Krish Dhasmana)

- **Board / Kanban** — 4-lane session status view (Needs Approval / Your Turn / Working / Idle) with inline option buttons, approval actions, stall detection, and close-session. See all active sessions at a glance without clicking into each chat. (Tyger Hugh)

- **Fork Session** — fork a conversation into a new tab pre-loaded with full history. Supports forking at a specific message index and passing an initial prompt. GitFork button in assistant message actions. App-isolation enforced. (Simon Meyffret)

- **Edit & Resend** — edit the last user message in-place and get a fresh response. Pencil button below last user message, inline textarea with Enter to send. Truncates history to the edited message. (Jiahao Guo)

- **Regenerate Replies** — click ↻ to regenerate any assistant reply, browse between all versions with ◀ ▶ arrows. Up to 20 variants per message with dedup. Variant history persists across page reloads and gateway restarts. (Jiahao Guo)

- **App SDK Ecosystem** — three-axis app classification (origin/resources/lifecycle), import map system for shared React modules, HMAC-SHA256 signed gateway reverse proxy for app backends, dependency ledger with reference-counted uninstall preview. (Ray Xu)

- **Warm Session Pool** — pre-spawn kiro-cli processes at gateway startup so new sessions claim an already-initialized process. Eliminates 3-10s cold-start latency on first message. Configurable pool_size, pool_agent, pool_ttl. Liveness drain loop discards dead providers. (Hoang Phan)

- **Portable Snapshot & Restore** — `kirocrew snapshot` creates .tar.gz archives of all state (memory, crons, config, skills). `kirocrew restore` with auto-detect replace vs merge mode, selective component restore, and dry-run preview. Symlink/path traversal rejection. (Patrick Gao)

- **Multi-Session Eval Harness** — `kirocrew eval` benchmarks cross-session memory with full memory loop (ConversationLog → Consolidator → MemoryStore → ContextBuilder). 5 built-in scenarios including a 54-turn software-engineering workflow. LLM judge scoring. (Zifeng Xia)

- **Stateless Cron Sessions** — `persistent_session: false` flag on `cron_add` opens a fresh session per run instead of accumulating context indefinitely. Prevents polling crons from OOM-killing the gateway after days of use. Reaper correctly targets ephemeral session keys. (Raymond Chen)

- **Session Deep-Link URLs** — permanent bookmarkable URLs with human-readable slugs (`/chat/my-session?sid=key`). Survives page refresh, works across browser tabs independently. Legacy `?slot=` still supported. (Thiago Andrade)

- **Mobile Responsive Layout** — collapsible hamburger nav drawer, touch-friendly dropdowns, session toggle button, responsive chat/notifications/secretary pages. Usable on phones via enterprise tunnels. sendOnEnter wired for desktop/mobile. (Pedro Barrios)

- **MCP Integrations Redesign** — card grid with iOS App Store-style expand-to-modal animation, tools accordion with animated expand/collapse, Update All MCPs button, skeleton loading. Backend switched from text parsing to JSON output. (Zezhen Xu)

- **Inline Comment Line Numbers** — comments in the file viewer now carry source-file coordinates (line, column) plus ~20-char context snippets. Eliminates ambiguity for short or repeated anchors. Custom rehype sourcepos plugin. (Simon Meyffret)

- **Kiro Usage Tab** — Overview > Kiro Usage shows billing card (plan, credits, cost), period summaries (today/week/month), 30-day averages, and scrollable daily history table. Parses ~/.kiro/sessions/cli/*.jsonl. (Erik Schweiss)

- **Data Classification Warning** — deployed across 13 user touchpoints (README, docs, terminal, installer, Slack) per security leadership request. New Security panel in Settings with live security posture (6 status rows) and 12 defense-in-depth features. (Bolin Chen)

- **iOS-Style Queue Stack** — queued messages display as stacked cards above the chat input instead of inline banners. Framer Motion spring animations, expand/collapse, dedicated queue_push/queue_pop WebSocket events. (Zezhen Xu)

- **Memory Context Budget 3x** — hard cap increased from 18k to 55k tokens. Lessons cap 3.3k→12k (highest-signal data), semantic memory 0.5k→4k, disk retention 90→365 days. Fixes silent truncation for power users with 70+ corrections. (Bolin Chen)

- **User-Defined kiro_hooks** — `agent.kiro_hooks` in config.json lets users define kiro-cli hooks (preToolUse, postToolUse) that persist across `kirocrew update`. Bundled hooks always run first. Validated with allowlist regex, path checks, and limits. (Mikhail Kuznetsov)

- **Sortable Column Headers** — all dashboard data tables now have clickable column headers for sorting. (Nishant Srivastava)

- **Built-in CLI Terminal** — terminal panel in the dashboard for running commands without leaving the browser. Go All auto-run retrigger fix included. (Joe Guo)

- **Electron Spellcheck** — native context menu with spelling suggestions, Cut/Copy/Paste, Look Up on macOS. (Chris McMillon)

- **Math Rendering** — LaTeX/KaTeX support in markdown chat messages. (Filippo Galli)

- **Lumon Industries Theme** — dark/light variants with theme-conditional branding. (Matthew Barnum)

- **Secretary Emoji Reactions** — react to Slack messages with emoji from Secretary inbox. Quick-access row and searchable grid. (Uday Prakash)

- **send_message Thread Replies** — `thread_ts` and `reply_broadcast` parameters on the send_message MCP tool. Enables posting as threaded replies for CR monitors, oncall acks, and cron pollers. (Caillin Bathern)

- **Fullscreen File Preview** — maximize button in file viewer opens a full-viewport portal overlay with all renderers preserved. (Zhuoyu Li)

- **Copy User Messages** — copy button on user message bubbles. (Yashwanth Korla)

- **Memory Usage Indicator** — pill in the header bar showing current memory consumption. (Yashwanth Korla)

- **SSO Hours Display** — traffic light colors (green/yellow/red) with floored hours instead of static text. (Roman Ivanov)

- **Warm Pool Config UI** — Pool Size, Pool Agent, Pool TTL fields in Settings with agent dropdown. (Hoang Phan, Bolin Chen)

- **Cancel Queued Messages** — X button on queued message cards with backend queue IDs. (Erik Schweiss)

- **Persist Chat Drafts** — drafts survive tab close, refresh, and browser crashes via localStorage with 300ms debounce. (Simon Meyffret)

- **Persist Draft Comments** — inline comments in the file viewer survive panel close via localStorage with 20-file LRU cap. (Simon Meyffret)

- **Session Content Search Ranking** — weighted scoring with title boost and length normalization instead of recency-only ordering. (Simon Meyffret)

- **Sidebar Redesign** — history row parity with session rows, session color side bars, folder indent borders, animated folder collapse, drag-drop improvements. (Zezhen Xu, Krish Dhasmana)

- **Plan Node Editing** — persistent unsaved edits with visual indicators in orchestrator plans. (David Fayerman)

- **Configurable Session Close** — toggle to skip confirmation dialog when closing sessions. (Swapnil Dixit)

- **Kiro-CLI Hooks Display** — read-only view of kiro-cli agent hooks in dashboard Hooks page with source badges. (Mikhail Kuznetsov)

- **Home Tab Capabilities** — Slack Home Tab now shows uptime, MCP integrations count, and installed skills. (siddartb)

### Improvements

- **Frontend Package Split** — React/TypeScript SPA extracted to dedicated KiroCrewWebsite package. KiroCrew resolves dist/ at build time. (Joe Guo)
- **handlers.py Split** — 7,646-line monolith split into 14 focused modules. Surfaced 12 pre-existing deadlock bugs. 50+ automated code-review fixes across subprocess safety, encoding, concurrency locks, and security validation. (Joe Guo)
- **Session Rebuild Fidelity** — role filtering excludes tool display titles (57% of budget), compression cap raised to 100 messages, per-message cap 1.5K→8K on fallback path. (Nick Bowers)
- **Subagent Timeouts** — task 20→30min, delivery 5→20min, injection 2→5min. Turn limit 30→100 default, 200 max. (Bolin Chen)
- **Config Persistence** — `to_dict()` now includes all dataclass fields (secretary, taskrunner, orchestrator, skills, timezone were silently dropped). (Bolin Chen)
- **Cron Retry** — Slack notification delivery retries up to 3 times with linear backoff on transient API errors. (Swapnil Gaikwad)
- **MCP Priority** — `~/.kirocrew/mcp.json` takes priority over `~/.kiro/settings/mcp.json`, fixing restrictive --include-tools leaking from kiro mcp.json. (Bolin Chen)
- **Managed Skill Dedup** — `_sync_aim_skill_paths_to_global()` replaces instead of union-merging, reducing --skill-paths from 50 to 8. MCP servers synced on Apply & Restart. (Bolin Chen)
- **Systemd Restart Limits** — `StartLimitBurst=5` + `StartLimitIntervalSec=300` prevents infinite crash loops (42,617 restarts in 5 days observed). (Bolin Chen)

### Security

- **Multi-User Slack Access Disabled** — identity assumption vulnerability where allowed users acted under the owner's system identity. `is_allowed_user()` now delegates to `is_owner()`. (Joe Guo)
- **Data Classification Warning** — 13 touchpoints warn users not to enter Critical/Restricted data on remote desktops/laptops per an internal data-handling standard. (Bolin Chen)
- **Subagent Memory Guard** — refuses spawns below 4GB available memory, preventing health-check kills from `spawn_run` bursts (~1.1GB per agent). Three remote-desktop crashes in 6 days were traced to this — memory dropped to 11.9% available, then the hypervisor terminated the instance 19 minutes later (before kernel OOM could intervene). Configurable via `spawn_min_memory_gb` (default 4.0, 0 disables). Fails open on macOS/containers. (Abhishek Mitra)
- **Subagent Task Redaction** — dual redaction (credentials + exfiltration URLs) applied once at top of spawn(), before truncation in SEL metadata, and on all SubagentInfo paths. (Abhishek Mitra)
- **PID Recycling Guard** — validates process start time before killpg in both subagent and cron reapers to prevent killing recycled PIDs. (Patrick Gao)

### Bug Fixes

- Fix warm pool sending wrong session key (`_warm_pool`) for ALL MCP tool API calls — broke trust, memory scoping, cron association, and audit logs for every warm pool user (Joe Guo)
- Fix `learn_add` returning "unknown session" in long-lived Slack threads — JSONL fallback recovers evicted slots with path-traversal defense (Sypher Su)
- Fix full chat history lost across gateway restarts — tab_id chaining, read_messages_chained(), removed broken frontend pagination (Bolin Chen)
- Fix kiro-cli tool-interrupt marker causing 2h agent hang — detect exact marker in 3 read paths, synthesize EVENT_COMPLETE (Simon Meyffret)
- Fix polling crons OOM-killing gateway after days — stateless sessions prevent unbounded context accumulation (Raymond Chen)
- **Lossless Session Resume** — process death (crash, idle kill, OOM) no longer deletes the session_map entry that holds the resume_sid. Split `remove()` (soft — preserves map for `session/load` resume) from `destroy()` (hard — permanent deletion). kiro-cli session files persist on disk indefinitely, so the resume_sid is valid and usable. Previously every process death forced lossy JSONL reconstruction even for short conversations. (Nick Bowers)
- Fix orphaned kiro-cli processes accumulating indefinitely — periodic 5-min PID sweep with tagged format for multi-instance safety (Patrick Gao)
- Fix secretary slack-mcp orphan leak on 30-min recycle — 30 orphans in 15h (~3.4GB). Process-group teardown with SIGKILL escalation (Shuya Sawa)
- Fix multi-byte UTF-8 chars (em dashes, smart quotes) causing kiro-cli Rust panic at byte boundary — replace with ASCII equivalents (Selena Wang)
- Fix `is_alive()` 600s stale-activity threshold falsely declaring healthy processes dead — use `is_process_alive()` for session liveness (Bolin Chen)
- Fix consolidation offset advancing on LLM failure — permanently skipped 30 messages with no retry (Bolin Chen)
- Fix subagent injection recovery guard never resetting after success — blocked all future recovery attempts (Bolin Chen)
- Fix auto-run (Go All) pausing after every stage — `_stage_instruction()` now checks `_auto_run` flag (Bolin Chen)
- Fix approval dialog reappearing after tab switch — `_mark_permission_resolved()` persists resolved state (Bolin Chen)
- Fix `config-permanent` YOLO overwritten by interactive TTL — `_yolo_from_config` flag prevents downgrade (Bolin Chen)
- Fix trust button click having no effect when pending approval already resolved — derive session_key from thread_ts (Bolin Chen)
- Fix cron timezone display showing UTC instead of job timezone in Slack Home tab (Bolin Chen)
- Fix channel agent dropdown storing filename stem instead of JSON name field — custom agents silently fell back to default (Bolin Chen)
- Fix `kirocrew token/status/logout/stop` hitting wrong port when `dashboard.url` configured — resolve from config/env/flag (Bolin Chen)
- Fix S3 presigned URLs blocked by exfiltration URL filtering (Bolin Chen)
- Fix non-image file uploads silently dropped after KiroCrewWebsite package split — restored a contributor's fix from 224e5f2 (Bolin Chen)
- Fix `sendOnEnter` setting not wired to ChatInput keydown handler (Zezhen Xu)
- Fix native notifications showing "N new notification(s)" instead of real content (Vitor Durante)
- Fix `KiroCrewConfig.to_dict()` silently dropping secretary, taskrunner, orchestrator, skills, and timezone sections on save (Bolin Chen)
- Fix `kirocrew` launcher resolving to stale workspace binary after packaged-install migration (Joe Guo)
- Fix MCP probe failure on remote desktops from deploy-tool envroot binaries — check .envroot before accepting (Joe Guo)
- Fix `is_process_alive()` stale-activity threshold causing conversation continuity loss (Bolin Chen)
- Fix bare number-dot responses rendered as "1." by markdown ordered list parser (Bolin Chen)
- Fix targeted `pip install <missing>` instead of full `pip install -e .` for missing deps — seconds vs minutes (Bolin Chen)
- Fix Docker Ollama container detection on AL2 to avoid native binary GLIBC crash (Bolin Chen)
- Fix AL2 installer conda missing channel + corrupt directory check (Bolin Chen)
- Fix pysqlite3-binary restricted to x86_64 Linux only — no aarch64 wheels exist (Bolin Chen)
- Fix schedule page day-of-week off-by-one display bug (Bolin Chen)
- Fix cron DOW range parsing in fmtCron and parseJobDefaults — expand ranges, wrap-around, normalize (Zezhen Xu)
- Fix list bullet/number marker color — add muted marker styling (Rohan Kapadia)
- Fix list padding preventing marker clipping in message bubbles (Rohan Kapadia)

### Refactors

- Split `dashboard/handlers.py` (7,646 lines) into 14 focused modules with backward-compatible re-exports (Joe Guo)
- Extract KiroCrewWebsite as a dedicated package for frontend (Joe Guo)
- Delete ~2.9MB tracked dead files (orphaned tests, screenshots, irrelevant skills) (Bolin Chen)

### Docs

- Comprehensive runtime docs update for 227 commits — 18 stale files deleted, 8 docs updated (Bolin Chen)
- Packaging publishing guide with cross-platform architecture and manual osx workflow (Joe Guo)
- Mobile dashboard access setup guide (enterprise tunnels) (Pedro Barrios)
- Snapshot and restore user documentation (Patrick Gao)
- remote-desktop-setup expanded with enterprise SSO and packaging bootstrap from scratch (Bolin Chen)
- Install docs updated for cross-platform bundles and migration PATH cleanup (Bolin Chen)

### Contributors (91)

Abhishek Mitra (Abhishek Mitra), Akim Akimov (Akim Akimov), Alec Douglas (Alec Douglas), Amit Chowdhary (Amit Chowdhary), Artem Pliasunov (Artem Pliasunov), Arvind Srinath Kumar (Arvind Srinath Kumar), Ayan Das (Ayan Das), Aziz Saifuddin (Aziz Saifuddin), Ben Grubin (Ben Grubin), Bolin Chen (Bolin Chen), Caillin Bathern (Caillin Bathern), Casey Huggins (Casey Huggins), Chanon Sinitskul (Chanon Sinitskul), Chen Tong (Chen Tong), Chen Yang Lho (Chen Yang Lho), Chris McMillon (Chris McMillon), Chris Raley (Chris Raley), Connor Marr (Connor Marr), Daisy Dazhen (Daisy Dazhen), David Fayerman (David Fayerman), David Ney Abarca (David Ney Abarca), Edward Riede (Edward Riede), Emmanuel Okonkwo (Emmanuel Okonkwo), Eric Hays (Eric Hays), Erik Schweiss (Erik Schweiss), Fei Ma (Fei Ma), Filippo Galli (Filippo Galli), Grant Gollier (Grant Gollier), Hoang Phan (Hoang Phan), Hugo Costa (Hugo Costa), James Joseph (James Joseph), Jaya Prakash Reddy Gade (Jaya Prakash Reddy Gade), Jiahao Guo (Jiahao Guo), Jin Cheng (Jin Cheng), Jingjin Wei (Jingjin Wei), Joe Guo (Joe Guo), Joe Pontone (Joe Pontone), John Law (John Law), Juan Segura (Juan Segura), Kishore Baskar (Kishore Baskar), Kotaro Inoue (Kotaro Inoue), Krish Dhasmana (Krish Dhasmana), Lanxiao Bai (Lanxiao Bai), Lester Lee (Lester Lee), Luke Jung (Luke Jung), Maninder Singh (Maninder Singh), Matthew Barnum (Matthew Barnum), Mike Mayer (Mike Mayer), Mikhail Kuznetsov (Mikhail Kuznetsov), Minglong Pan (Minglong Pan), Mustafa Onur Aydin (Mustafa Onur Aydin), Naoya Ishikawa (Naoya Ishikawa), Nick Bowers (Nick Bowers), Nick Papadopoulos (Nick Papadopoulos), Nishant Srivastava (Nishant Srivastava), Patrick Gao (Patrick Gao), Paul McKissock (Paul McKissock), Pedro Barrios (Pedro Barrios), Ray Xu (Ray Xu), Raymond Chen (Raymond Chen), Rittik Gautam (Rittik Gautam), Rohan Kapadia (Rohan Kapadia), Roman Ivanov (Roman Ivanov), Satheesh Prabhakaran (Satheesh Prabhakaran), Selena Wang (Selena Wang), Shameem PK (Shameem PK), Shihao Wang (Shihao Wang), Shuya Sawa (Shuya Sawa), Siddartha B V (siddartb), Simon Meyffret (Simon Meyffret), Stan Tian (Stan Tian), Sudhamsu Manne (Sudhamsu Manne), Sujoy Datta (Sujoy Datta), Swapnil Dixit (Swapnil Dixit), Swapnil Gaikwad (Swapnil Gaikwad), Sypher Su (Sypher Su), Teodor-Gabriel Oprescu (Teodor-Gabriel Oprescu), Thiago Andrade (Thiago Andrade), Tianxiang Xu (Tianxiang Xu), Tyger Hugh (Tyger Hugh), Uday Prakash (Uday Prakash), Vamil Gandhi (Vamil Gandhi), Vitor Durante (Vitor Durante), William Randall (William Randall), Xu Deng (Xu Deng), Yashwanth Korla (Yashwanth Korla), Yu Cheng (Yu Cheng), Zezhen Xu (Zezhen Xu), Zhaolong Zhang (Zhaolong Zhang), Zhengfei Ji (Zhengfei Ji), Zhuoyu Li (Zhuoyu Li), Zifeng Xia (Zifeng Xia)

## [2.2.0] — 2026-04-21

172 commits, 271 files changed, 63 contributors since v2.1.0.

### Features

- **Secretary Service** — background Slack inbox manager that classifies messages as needs-reply, FYI, or noise. On-demand draft generation, keyword and name-mention alerts, edit-diff style learning, and self-healing reconnection. Full dashboard page with investigate and dismiss-all. (Lanxiao Bai)
- **Message Queue** — messages arriving while a session is busy are queued (shown with ⏳) and processed in order when the agent is free. Cancel queued messages or clear the queue with `!stop`. No more lost messages during long tool calls. (Sam Cuthbertson)
- **Linked Thread Sync** — bidirectional dashboard↔Slack message mirroring. Link a Slack thread to a dashboard slot; messages and agent responses stream to both surfaces in real-time. Includes link persistence across restarts. (Ravi Teja Kondisetty)
- **SSO WebSocket Terminal** — PTY-based terminal for enterprise SSO authentication with RSA-OAEP encrypted input. Private key never leaves server, heartbeat and idle timeout for secure session management. (Erik Schweiss)
- **Project Folder Grouping** — organize chat sessions into folders with drag-drop, LLM-generated emoji icons, and server-persisted pinning. (Ravi Teja Kondisetty)
- **File Picker & Workspace Picker** — `@filename` in chat input triggers fuzzy file search scoped to active project. Directory browser and workspace picker for switching context. (Hoang Phan)
- **Session Archive Viewer** — rotated/compacted session lines archived to `sessions/archive/` with 7-day retention. Dashboard viewer under Developer page. Atomic exclusive-create writes. (Jiahao Guo)
- **HEARTBEAT_KEEP Sentinel** — agent can include `HEARTBEAT_KEEP` in response to retain incomplete heartbeat tasks for the next cycle. Previously all tasks were removed after processing. (Curtis Demerah)
- **Subagent Timeout Redesign** — two-level timeout architecture prevents subagents from hanging indefinitely. Automatic prompt-busy recovery with retry and backoff. On exhaustion, results are saved to disk so no work is lost. (Joe Guo)
- **Security Governance Integration** — a governance MCP server registered as a managed server with its search tool auto-approved. New `security-assistance` skill requires a governance search before responding to security-sensitive requests. (Zach He)
- **Incognito Mode** — ephemeral sessions that block `learn_add` and memory consolidation. No persistent traces. (Tianxiang Xu)
- **Session Content Search** — search history by content (CR IDs, error messages, file paths) rather than title alone. Exposed via `/api/sessions/search`. (Simon Meyffret)
- **`kirocrew stop` Command** — stop a running gateway via SIGTERM with port-based PID lookup and process verification. (Patrick Gao)
- **`!compact` Slack Command** — trigger in-place context compaction from Slack. Shows ♻️ reaction, streams `/compact`, reports result with timing. (Nihal Singh)
- **OPTIONS Multi-Select** — upgraded from single-choice buttons to checkboxes with Send button. Supports up to 10 options. (Ravi Teja Kondisetty)
- **Configurable Autocompact** — `session.autocompact_pct` (default 90%, valid 5–90) replaces hardcoded threshold. (Hugo Costa)
- **DAU Metrics** — CloudWatch RUM `session_start` event with hashed user identity (SHA-256 + per-install salt), system info fields, and 8-panel dashboard. (Bolin Chen)
- **Collapsible Tool Calls** — completed agent turns collapse tool calls by default. Configurable via settings. (Bolin Chen, Zezhen Xu)
- **Subagent Progress Bar** — compact expandable indicator showing running agents and current tools. (Shailesh Agrawal)
- **Session Colors** — per-session color coding with 4 palette generators and accessibility-aware contrast. (Jimmy Kilpatrick)
- **Font Size Setting** — independent 100–250% font size control in Display settings. (Joe Guo)
- **High Contrast Theme** — dark + light variants, bringing total to 14 themes. (Maxwell Schroder)
- **Context Window Utilization** — timing footer in Slack shows current context usage percentage. (Nihal Singh)
- **Cron Thread Replies** — `thread_ts` parameter in `cron_add`/`cron_update` lets cron jobs reply in-thread instead of top-level. (Sam Cuthbertson)
- **`next_run_ts` in Cron** — `cron_list` and `/api/crons` now include next scheduled run time. (siddartb)
- **Thread/DM Resume Choice** — session resume shows buttons to choose between thread reply and DM. (Shashwat Srivastava)
- **Claude Opus 4.7** — added to `model_tokens` with 1M context window. (Patrick Gao)
- **Inline Tool Call Lines** — tool calls render inline with collapsible turns. StrictMode WebSocket fix. (Zezhen Xu)
- **Invocation Tracking** — `userPromptSubmit` hook reports per-message invocation counts to an AI capabilities dashboard. (Bolin Chen)
- **Electron Remote Tunnel** — Electron app fetches auth tokens automatically via SSH for headless CDE setups. Configurable remote host settings. (Chris McMillon)
- **LLM Retitle Button** — sparkles icon replaces pencil for LLM-generated session titles. (Jaden Yuros)
- **Remember Selected Session** — switching between Chat and Autopilot preserves your active session. (Maninder Singh)
- **Copy Feedback Animation** — CodeBlock component shows visual confirmation on copy. (Christopher Huk)
- **Minimal Installer** — `minimal_install.sh` for environments with existing tooling (Python, Node, git already available). Skips platform tool installation. (Adam Doussan)
- **Agent Selector in Channels** — dropdown to pick which kiro agent to use when adding an agent to a channel. Reuses existing AgentSelector component. (Nitan Singh)
- **Home Tab Version Badge** — Slack App Home Tab shows running KiroCrew version and update-available notice for Slack-primary users. (siddartb)
- **Session-Targeted Cron** — `send_message` with `session="origin"` injects cron results directly into the dashboard session that created the job. (Luke Ely)
- **Kiro & IntelliJ Themes** — two new built-in themes added to the color system. (Krish Dhasmana)

### Improvements

- **Orphaned Session Fix** — `set_active_dashboard_slots()` immediately reaps dashboard sessions whose slot no longer exists. Fixes zombie sessions consuming ~400MB each. (Barrett Karson)
- **Stale PID/Dir Cleanup** — clean orphaned `session_pid_*.txt` files and empty session workspace dirs at startup. Fixes draft loss on tab switch. (Joe Guo)
- **Credential Deny Pattern Removal** — narrowed then removed broad `*credential*` patterns from `BUILTIN_DENY_PATTERNS`. OS sandbox handles credential file access. Fixes false positives on package names. (Paxton Tomooka, Bolin Chen)
- **Auto-Approve Subagent Tools** — new `auto_approve_subagent_tools` config flag (deny-by-default). `spawn_run` auto-approved at handler level when `auto_approve_subagent_spawn` is true. (Emmanuel Okonkwo)
- **Cron Timezone Evaluation** — `cron_expr` now evaluated in job timezone instead of always UTC. (Parwinder Singh)
- **URL Redaction Skip** — internal corporate domains skip URL length redaction. Extracted `_is_safe_domain()` helper. (Nansong Yi, Bolin Chen)
- **ACP Auto-Retry** — automatic retry on cron process death with lazy MCP binary resolution. Bounded `agent.py` parent walk at `pyvenv.cfg`. (Himanish Kaul)
- **Snowball Stemming** — FTS5 keyword scoring uses Snowball stemmer for better recall. `pysqlite3-binary` on Linux for FTS5/UPSERT compat. (Mohammed Madni Vaid)
- **Parallel Test Suite** — pytest-xdist with `-n auto` (48 workers). ~29% speedup with fake clock and Hypothesis profiles. (Alec Douglas, Patrick Gao)
- **Bounded Restart Shutdown** — 5s `asyncio.wait_for` on `provider.shutdown()` during Apply & Restart prevents leaks. (Mark Lord)
- **Project Scope Refactor** — replaced per-session workspace binding with project-scoped directory. (Hoang Phan)
- **MCP Server Sync** — merge env/command/args for existing local MCP servers in `sync_to_agent_config`. (Nick Bowers)
- **spawn_status Disk Read** — reads full result from disk instead of truncated memory copy. (Ben Grubin)
- **Frontend Rebuild on Update** — `kirocrew update` and auto-update now rebuild frontend. Propagate build failures in setup.py. (Lachlan Lindsay, Alec Douglas)
- **set_mode for All Agents** — `session/set_mode` called for all agents, not just default. (Joe Guo)
- **Auto-Approve Subagent Spawn** — trusted dashboard sessions auto-approve `spawn_run` without interactive dialog. (Jiahao Guo, Emmanuel Okonkwo)
- **Subagent Activity Panel** — `subagent_done` event includes result payload so Activity panel shows final output even if streaming chunks were missed. (Shailesh Agrawal, Bolin Chen)
- **Approval Card Scrollable** — command preview in approval cards scrollable instead of clipped. (Yohanes Setiawan)
- **Overflow Menu Actions** — handle Slack overflow menu `selected_option` nesting correctly. (Ben Grubin)
- **macOS Orphan Cleanup** — replaced `ps` with `libproc` for Python 3.10+ on macOS 26. (Toby Wong)
- **Lucide Icons** — replaced UI emojis with lucide-react icons throughout dashboard. (Alec Douglas)
- **Modern Chat UI** — Claude-style title row, input bar redesign, overlay session drawer, unified topbar, pinned-only sidebar with smooth animations. (Krish Dhasmana, Zezhen Xu, Lanxiao Bai)

### Security

- **SSO Terminal** — RSA-OAEP encryption, deny-by-default auth middleware, input size validation, and decrypt error feedback. (Erik Schweiss)
- **Scoped Channel Trust** — SEL audit on trust changes, approval button UX improvements. (Satheesh Prabhakaran)
- **Mixed Internal Paths** — non-loopback MCP/browser paths validate `X-Internal-Secret` header before cookie auth. (Bolin Chen)
- **Security Governance** — mandatory governance doc search before security-sensitive responses. (Zach He)
- **Build-Tool Snapshot Push Block** — build-tool workspace snapshot-push commands added to deny lists. (Alex Yelle)
- **Electron Hardening** — path traversal fix, hostname validation, race condition fix, Referer stripping via webRequest API. (Chris McMillon)

### Bug Fixes

- Fix session-expired banner persisting on remote desktops (6be6c09) (Johnny Xue)
- Fix framer-motion layout animations on chat sidebar session rows (2f79d0d) (Zezhen Xu)
- Suppress error replies to trusted bot messages preventing echo loops (53e28fc) (Minglong Pan)
- Fix variable shadowing bug in `api_agent_config` (4e131a2) (Kevin Zuern)
- Fix Ollama resource leak — assign `_ollama_manager` before `ensure_running` (41009e7) (Bolin Chen)
- Fix `send_message` 502 on Slack failure + eid hint in action context (c44f927) (Ben Grubin)
- Fix voice input UX — show recording/transcribing status (9712d31) (Shashwat Srivastava)
- Fix deferred `scrollBottom` after `appendMessage` preventing invisible user messages (2e9c566) (Shashwat Srivastava)
- Fix npm E401 auth errors in Electron and TUI installs (81d361c) (Joe Guo)
- Fix SQLite connection leak in MemoryStore FTS methods (5948e22) (Jiahao Guo)
- Fix table formatting preservation in Slack streaming mode (174e781) (Viren Khatri)
- Fix graceful `pysqlite3` fallback to stdlib `sqlite3` (fe03a4d) (Maninder Singh)
- Fix cancel parent prompt before releasing semaphore in subagent injection (e0fc51b) (Patrick Gao)
- Remove Docker dependency from Whisper STT and Ollama embeddings (d993f86) (Hugo Costa)
- Fix `embedding_model` not loaded from config.json in KiroCrewConfig (19c201a) (Patrick Gao)
- Fix thread parent context — block extraction, ch_ctx gate, compressed guard (943e68d) (James Joseph)
- Fix auth token not included in gateway browser auto-open URL (943ae1c) (Simon Meyffret)
- Fix session history sort by modified time (d4f86f3) (Joe Guo)
- Fix YOLO mode mutating per-slot trust state (2205b55) (Simon Meyffret)
- Fix `_ensure_ssl_certs()` not running from cli.py entry-point (971debe) (Vishal Sreekrishnan)
- Fix dashboard URL printed on same line as other output (c0941e8) (Rohan Kapadia)
- Fix honour `agent.yolo` config on dashboard startup (98e2e17) (Simon Meyffret)
- Fix approval screen persisting after approving (45ee69c) (Mohammed Madni Vaid)
- Fix custom agent prompts not loaded in Slack — pass agent to `build_message` (7c47d1e) (Chen Yang Lho)
- Fix input draft loss on tab switch — reorder sessionStorage rehydration (40c9c7b) (Joe Guo)
- Fix `arcc` and `uv` orphan processes escaping cleanup — add to kill allowlist (5aa9a2a) (Satheesh Prabhakaran)
- Fix `.venv/` blocking auto-update — add to gitignore (c38f415) (Rohan Kapadia)
- Fix `~/.kirocrew/` not existing before log handler init (85ebec7) (Yehui Zhang)
- Fix Slack Home Tab hardcoded `/kirocrew` — use configured command name (d85b047) (Namra Saheba)
- Fix lazy `_config_lock` init for Python 3.10 compat (c30b8e5) (Joe Guo)

### Documentation

- Comprehensive spec update for all beta-braveheart features (Bolin Chen)
- Updated remote desktop setup: recommend m7a.4xlarge, AL2023 over AL2 (Joe Guo)
- Added SLACK_SETUP.md references to README (Hugo Wen)

### ⚠️ Breaking Changes

- `auto_approve_subagent_tools` defaults to `False` — tool calls inside subagents now require explicit approval unless configured. Previously inherited parent session trust implicitly.

### Contributors (63)

Adam Doussan (Adam Doussan), Alec Douglas (Alec Douglas), Alex Yelle (Alex Yelle), Artem Krivonos (Artem Krivonos), Barrett Karson (Barrett Karson), Ben Grubin (Ben Grubin), Bolin Chen (Bolin Chen), Chen Yang Lho (Chen Yang Lho), Chris McMillon (Chris McMillon), Christopher Huk (Christopher Huk), Curtis Demerah (Curtis Demerah), David Fayerman (David Fayerman), Doruk (Doruk), Emma Zhou (Emma Zhou), Emmanuel Okonkwo (Emmanuel Okonkwo), Erik Schweiss (Erik Schweiss), Himanish Kaul (Himanish Kaul), Hoang Phan (Hoang Phan), Hugo Costa (Hugo Costa), Hugo Wen (Hugo Wen), Jacob Morgan (Jacob Morgan), Jaden Yuros (Jaden Yuros), James Joseph (James Joseph), Jiahao Guo (Jiahao Guo), Jimmy Kilpatrick (Jimmy Kilpatrick), Joe Guo (Joe Guo), Johnny Xue (Johnny Xue), Kevin Zuern (Kevin Zuern), Krish Dhasmana (Krish Dhasmana), Lachlan Lindsay (Lachlan Lindsay), Lanxiao Bai (Lanxiao Bai), Luke Ely (Luke Ely), Maninder Singh (Maninder Singh), Mark Lord (Mark Lord), Matt Cohen (Matt Cohen), Maxwell Schroder (Maxwell Schroder), Minglong Pan (Minglong Pan), Mohammed Madni Vaid (Mohammed Madni Vaid), Namra Saheba (Namra Saheba), Nansong Yi (Nansong Yi), Nick Bowers (Nick Bowers), Nihal Singh (Nihal Singh), Nitan Singh (Nitan Singh), Parwinder Singh (Parwinder Singh), Patrick Gao (Patrick Gao), Paxton Tomooka (Paxton Tomooka), Ravi Teja Kondisetty (Ravi Teja Kondisetty), Rohan Kapadia (Rohan Kapadia), Sam Cuthbertson (Sam Cuthbertson), Satheesh Prabhakaran (Satheesh Prabhakaran), Shailesh Agrawal (Shailesh Agrawal), Shashwat Srivastava (Shashwat Srivastava), Siddartha B V (siddartb), Simon Meyffret (Simon Meyffret), Tianxiang Xu (Tianxiang Xu), Toby Wong (Toby Wong), Tony Hardie (Tony Hardie), Viren Khatri (Viren Khatri), Vishal Sreekrishnan (Vishal Sreekrishnan), Yehui Zhang (Yehui Zhang), Yohanes Setiawan (Yohanes Setiawan), Zach He (Zach He), Zezhen Xu (Zezhen Xu)

## [2.1.0] — 2026-04-13

170 commits, 298 files changed, 59 contributors since v2.0.0.

### Features

- **Multi-Agent Orchestration** — conductor delegates tasks across named agents with isolated context and plan memory. Useful for complex workflows where a code agent and a review agent collaborate on the same task. (Joe Guo)
- **Persistent Agent Channels** — dedicated multi-agent collaboration spaces with their own history. Spin up a channel with 3 agents working on a design doc while you chat separately. (Lanxiao Bai)
- **Dashboard ↔ Slack Handoff** — link a dashboard session to a Slack thread for bidirectional sync. Start debugging in the dashboard, then hand off to Slack so your phone gets updates. `sessions` command lists recent sessions with resume buttons. (Nate Eklund)
- **Slash Command System** — `/kirocrew dashboard`, `/kirocrew sessions`, `/kirocrew channels`, `/kirocrew users`. Manage allowlists, channels, and agents without memorizing bang commands. Old `!` commands still work with deprecation warnings. (Ezzat Qupty)
- **Dashboard UI Overhaul** — 6 new pages: Settings (General/Chat/Display), Developer (log viewer), Capabilities (MCP tools), Schedule (week grid), OrchestratedChat, Channels. Configure everything from the browser instead of editing JSON. (Zezhen Xu, Dan Dagayev)
- **Monaco Editor** — code blocks in chat render with Monaco syntax highlighting. Diff blocks show colored +/- lines. Review code changes without leaving the chat. (Mustafa Onur Aydin, Finn Haddon)
- **AMOLED Theme** — pure-black theme for OLED screens. Saves battery on mobile and reduces eye strain in dark rooms. JSON syntax tokens added across all 11 themes. (Ishan Mishra)
- **Prompts & Agent-SOP** — browse and manage prompt templates from Overview > Prompts tab. Reuse proven prompts across sessions instead of retyping them. (Marc Shelton)
- **AWS Transcribe Streaming** — alternative STT provider for voice input. Use when whisper is too slow or unavailable. Configure `stt.provider: "transcribe"` with region and language. (Simon Meyffret)
- **Targeted send_message** — MCP tool can DM a specific user or post to a specific channel. Useful for cron jobs that alert different people based on what they find. (Vamil Gandhi)
- **Inline Action Buttons** — Block Kit buttons and selects in Slack route back to the LLM session. Agent can present choices and act on your click without another message. (Ben Grubin)
- **Configurable Reactions** — customize phase emojis (`slack.reactions`) or disable them entirely (`reactions_enabled: false`). Reduce noise in busy channels. (Sudhamsu Manne)
- **Open Channels** — `slack.open_channels` bypasses allowlist for specified channels. Add your team channel so everyone can use the bot without individual approval. (Adi Sridharan)
- **Bot Identity** — `agent.bot_name` lets the bot introduce itself by a custom name. Useful when running multiple KiroCrew instances with different personas. (Tao Jiang)
- **Cron Enhancements** — `skip_dates` excludes holidays, `timezone` evaluates dates locally, `--no-crons` flag for multi-instance setups. Schedule page shows a week grid of upcoming jobs. (Hugo Costa, Zezhen Xu, Simon Meyffret)
- **Inline Image Preview** — drag-drop images show a preview strip before sending. Verify you're sharing the right screenshot before the agent sees it. (Luke Ely)
- **Fish Shell Support** — `install.sh` auto-detects fish and configures PATH. No more manual `set -gx` after install. (David Ramos)
- **mise Runtime Management** — `install.sh --mise` uses mise for Python 3.12 and Node 16 instead of system package managers. Keeps your global environment clean. (Kyle Helmick)
- **Packaged Bundle Distribution** — one-command bundled install path. For teammates who don't want to clone the repo. (Joe Guo)
- **CloudWatch RUM** — browser analytics for dashboard usage patterns. See which pages people actually use. 100% session sampling. (Zezhen Xu)
- **Agent Identity Injection** — `[CURRENT AGENT]` and `[RUNTIME]` tags in LLM context. Agent knows whether it's running in Slack, dashboard, or cron and adapts behavior. (Chen Qiu)
- **Thread Parent Context** — cron reply sessions auto-inject parent thread context. Cron jobs that reply to existing threads understand what was discussed before. (James Joseph)
- **Subagent Improvements** — agent name inheritance from parent, enriched timeout errors with turn/tool context, approval cascade fix. Easier debugging when subagents fail. (Nitan Singh, Patrick Gao, Aryaman Pathania)
- **Session Status Indicator** — per-slot status (idle/streaming/tool_running) with independent ApprovalBar. See at a glance whether the agent is thinking, running a tool, or waiting. (Joe Guo, Himakireeti Konda)
- **CSRF Protection** — allowed origin derived from `dashboard.url`. Port-specific cookies prevent auth collision when running multiple instances. (Cole Whitley, Stan Tian)
- **Skill Matching** — negative triggers via `!` prefix (e.g. `!test` excludes when "test" appears). Prevents wrong skills from loading. (Yongbo Xiao)

### Fixes

- WS dead client cleanup — check `ws.closed` before broadcast, remove dead clients via `_remove_ws()` (Bolin Chen)
- Plan memory rotation — cap at 500 lines, plan_lessons 30s TTL cache (Bolin Chen)
- Process tree kill on session reset and subagent reap (Akim Akimov)
- Textarea layout preserved when files change (Luke Ely)
- Helpful error for private package registry 401 during pip install (Wenli Yan)
- Electron token prompt for remote gateway connections (Luis Gabriel Lima)
- Confirm dialog restored for destructive history session delete (Marvellous Adedapo)
- Internal path auth fall-through for browser-facing routes (Eric Zhang)
- History ranked by `updated_at` instead of creation time (Joe Guo)
- Lesson injection uses recency ordering instead of random hash (Stan Tian)
- MCP probe timeouts reduced to prevent gateway startup hangs (Nagarajesh Lakshmanan)
- Merge all `mcp.json` files instead of returning on first hit (Nick Bowers)
- Cron dedup fix for repeated Slack alerts (Simon Meyffret)
- Firefox compatibility — replace `crypto.randomUUID()` (Rohan Khanderia)
- Slot.task None guard before cancel() (Eduardo Vencovsky)
- Subagent approval bypass via Slack callback (Aryaman Pathania)
- MagicMock leaking into session_pid filenames (Joe Guo)
- Autopilot recursive auto-run stack and stage timeout (Joe Guo)

### Refactors

- Remove Dream memory consolidation system — caused memory regressions (Bolin Chen)
- Eliminate code duplication, onboard jscpd (Aryaman Pathania)
- Replace embedding-based skill matching with negative keywords (Yongbo Xiao)
- Restructure orchestrator docs — rename to Autopilot (Joe Guo)
- Makefile for single-command build cycle (Bolin Chen)

### Docs

- Comprehensive documentation update for all post-v2.0.0 features (Bolin Chen)
- Memory architecture, security deep dive, team communication guides (Zezhen Xu)
- Voice input/output setup, session-Slack linking design (Zezhen Xu, Joe Guo)
- Autopilot design and lifecycle (Dan Dagayev)
- Delete stale docs: DEVELOPMENT.md, TASK_PLAN_MODE.md, verification-2026-03-03.md

### Contributors

Joe Guo (Joe Guo), Dan Dagayev (Dan Dagayev), Bolin Chen (Bolin Chen), Ben Grubin (Ben Grubin), Patrick Gao (Patrick Gao), Zezhen Xu (Zezhen Xu), Nick Papadopoulos (Nick Papadopoulos), Simon Meyffret (Simon Meyffret), Yongbo Xiao (Yongbo Xiao), Toby Wong (Toby Wong), Luke Ely (Luke Ely), Eric Zhang (Eric Zhang), Vamil Gandhi (Vamil Gandhi), James Joseph (James Joseph), Yuliang Qiao (Yuliang Qiao), Stan Tian (Stan Tian), Rohan Khanderia (Rohan Khanderia), Hugo Costa (Hugo Costa), Himakireeti Konda (Himakireeti Konda), Finn Haddon (Finn Haddon), Chen Qiu (Chen Qiu), Aryaman Pathania (Aryaman Pathania), Sudhamsu Manne (Sudhamsu Manne), Lanxiao Bai (Lanxiao Bai), Eduardo Vencovsky (Eduardo Vencovsky), David Ramos (David Ramos), Marc Shelton (Marc Shelton), Matthew Barnum (Matthew Barnum), Mariam Alaidi (Mariam Alaidi), Adi Sridharan (Adi Sridharan), Siming Deng (Siming Deng), Ezzat Qupty (Ezzat Qupty), Graham Roberts (Graham Roberts), Ishan Mishra (Ishan Mishra), Kellen Jia (Kellen Jia), Kyle Helmick (Kyle Helmick), Luis Gabriel Lima (Luis Gabriel Lima), Lipeng Yang (Lipeng Yang), Mustafa Onur Aydin (Mustafa Onur Aydin), Nagarajesh Lakshmanan (Nagarajesh Lakshmanan), Nate Eklund (Nate Eklund), Nathan Burns (Nathan Burns), Nick Bowers (Nick Bowers), Anthony Dominianni (Anthony Dominianni), Phillip Gong (Phillip Gong), Nitan Singh (Nitan Singh), Bhargav Mistry (Bhargav Mistry), Aswin Damodar (Aswin Damodar), Beau Bright (Beau Bright), Cole Whitley (Cole Whitley), David Fayerman (David Fayerman), David Qian (David Qian), Akim Akimov (Akim Akimov), Yuta Tsuji (Yuta Tsuji), Tianxiang Xu (Tianxiang Xu), Wei Wei (Wei Wei), Wenli Yan (Wenli Yan), Tao Jiang (Tao Jiang), Marvellous Adedapo (Marvellous Adedapo)

## [2.0.0] — 2026-04-06

93 CRs merged, 158 commits, 37 contributors since v1.2.2.

### Features

- **Activity Viewer** — real-time tool call cards, subagent progress bars, collapsible tool groups with inline approval UI (Matthew Barnum, Himakireeti Konda)
- **Trust Reads mode** — 📖 auto-approve read-only bash commands while prompting for writes (a KiroCrew contributor)
- **Voice streaming** — real-time TTS via AWS Polly with sentence-boundary streaming, auto-speak, interrupt (Ezzat Qupty)
- **Terminal UI (TUI)** — full-featured terminal interface built with Ink + React + TypeScript (Yao Bian)
- **File send MCP tool** — agent outbox with Slack upload + dashboard download, 50MB cap, content scanning (a KiroCrew contributor)
- **Cross-platform file upload** — drag-drop, clipboard paste, file picker in dashboard chat (Graham Roberts)
- **Custom themes** — create/edit/delete color themes with visual picker and paste-JSON mode (a KiroCrew contributor)
- **Model selector** — switch LLM models mid-session from session bar, welcome screen, or agent config (Matthew Barnum)
- **Cron inline editing** — PATCH endpoint, Run Now, Open in Chat, human-readable schedules with timezone (a KiroCrew contributor, a KiroCrew contributor)
- **LLM-compressed thread history** — resumed sessions get head/tail sandwich compression of prior conversation (Aswin Damodar)
- **Concurrent dashboard tokens** — up to 5 valid tokens with `kirocrew logout` for revocation (a KiroCrew contributor)
- **Pin/favorite sessions** — pin sessions to top of sidebar with localStorage persistence (Ezzat Qupty)
- **Subagent turn limit** — configurable `subagent_max_turns` with per-spawn override (Stan Tian)
- **spawn_status MCP tool** — retrieve full untruncated subagent output (Ben Grubin)
- **Block Kit support** — `send_message` accepts `blocks` for rich Slack messages with deep-walk sanitization (Ben Grubin)
- **!stop command** — force-halt agent mid-stream in Slack, bypasses per-session semaphore (Rohan Khanderia)
- **Approval routing** — route approval cards to correct chat slot with concurrent support (Himakireeti Konda)
- **Trust/YOLO propagation** — subagent sessions inherit parent's approval policy (a KiroCrew contributor)
- **DevSpaces auto-detection** — auto-configure CORS and proxy URL (Rikiya Tsukidate)
- **Workspace CRUD** — create/update/delete workspaces via HTTP API, CLI, and frontend (Zezhen Xu)
- **Enforce denied commands scope** — `"all"` or `"kirocrew"` to skip non-kirocrew agents (Bolin Chen)
- **Editable Config Summary** — dashboard settings directly editable with inline feedback (Bolin Chen)
- **Security governance integration** — setup/update/auto-update flows for governance tools (Bolin Chen)
- **Log filter & tail** — dashboard Logs page with real-time filter and tail toggle (Vaibhav Bhatia)
- **Live markdown preview** — file watching with inline comments and batch submit (Rohan Khanderia)
- **Diff "View full file"** — click 📄 on diff blocks to open full file in side panel (a KiroCrew contributor)
- **Phase-aware Slack reactions** — granular emoji status with debounce and stall watchdog (Vamil Gandhi)
- **Slack thread metadata injection** — mid-thread @mentions get full thread context (a KiroCrew contributor)
- **API server in --slack-only** — minimal API server for MCP tools when dashboard disabled (Parimal Deshmukh)
- **Slack manifest auto-populate** — `kirocrew manifest` CLI command (a KiroCrew contributor)
- **CLI usage examples** — practical examples in `--help` output (a KiroCrew contributor)
- **SSO auth status** — certificate validity in Slack status and Home tab (Pedro Barrios)
- **~~Auto-dream all sessions~~** — removed in dream system cleanup
- **Restore window infinite** — `restore_window_minutes=0` restores all sessions (Arturo Acuaviva)

### Security

- **Loopback auth bypass removed** — closes port-forward bypass (security-review finding). File-based IPC secret, X-Internal-Secret header, nonce-based tokens, SEL audit (Bolin Chen)
- **11 security-review findings addressed** — Mermaid strict sandbox, 42 suspicious bash patterns, tiered YOLO auto-expiry (Slack 30min/dashboard 6h/config permanent), SEL forward callback redaction, chmod 600, observe-mode gating (Bolin Chen)
- **Credential redaction on assembled output** — runs on final head+compressed+tail string (Aswin Damodar)
- **Custom theme injection hardening** — CSS injection prevention with security tests (a KiroCrew contributor)
- **LessonStore path hardening** — validate against is_sensitive_path(), SEL audit (a KiroCrew contributor)
- **Atomic file writes** — tempfile.mkstemp across 9 call sites (a KiroCrew contributor)
- **Subagent result redaction** — redact task text before truncation (Bolin Chen)

### Bug Fixes

- **Cron stall** — non-blocking execution, semaphore _acquired flag in 5 components, timer always re-arms, ACP zombie detection, 13 new tests (Bolin Chen)
- **Cron timer restore** — re-arm after gateway restart (a KiroCrew contributor)
- **Cron timer cap** — 30s poll interval for externally-added jobs (James Joseph)
- **Cron+subagent race** — _cron_injecting counter prevents premature session reset (a KiroCrew contributor)
- **Cron subagent routing** — route replies to correct Slack thread (James Joseph)
- **~~Dream agent stability~~** — removed in dream system cleanup
- **Auto-title deadlock** — try/finally on semaphore release (a KiroCrew contributor)
- **Subagent Activity Viewer** — status bug, approval state in Redux, reconnect recovery (Matthew Barnum)
- **Subagent reaper** — force-kill zombies past 20min timeout (Jingjin Wei)
- **TaskRunner heartbeat** — detect dead processes every 30s, reset sessions between retries (Joe Guo)
- **TaskRunner context compaction** — mid-stream compaction at 90%, project re-execution (Joe Guo)
- **MCP server graceful stop** — close stdin for wrapper processes (a KiroCrew contributor)
- **learn_add timeout** — store embeddings instead of O(N) recompute, 38s→0.12s (a KiroCrew contributor)
- **Null prompt handling** — coerce null/missing to empty string (a KiroCrew contributor, Patrick Gao)
- **Closing code fence split** — fix unclosed blocks when LLM streams fence+text (a KiroCrew contributor)
- **Kiro agent name resolution** — resolve before ACP session creation (Ben Grubin)
- **IME composition Enter** — prevent CJK candidate from triggering rename (Shihao Wang)
- **Ollama model name** — correct to qwen3-embedding:0.6b (a KiroCrew contributor)
- **Chat input at high zoom** — zoom-aware viewport units (Joe Guo)
- **Dashboard 5 UI bugs** — Config PATCH 401, model dropdown TUI regression, layout fixes (Bolin Chen)
- **Whitespace in Slack streaming** — move .strip() to final message only (a KiroCrew contributor)
- **Memory blob serialization** — strip BLOB fields from API JSON (Bolin Chen)
- **Slot key normalization** — fix subagent routing on session resume (a KiroCrew contributor)

### Contributors (37)

Adam Duncan, Anthony Orozco, Arturo Acuaviva, Aswin Damodar, Beau Taylor-Ladd, Ben Grubin, Bolin Chen, Chengxi Li, David Fayerman, Eduardo Vencovsky, Ezzat Qupty, Goutham Manjunatha, Graham Roberts, Himakireeti Konda, Huan He, Hugo Costa, James Joseph, Jiacheng Wang, Jiayi Zhang, Jingjin Wei, Joe Guo, Joel Blumenthal, Joel Studevant, John Li, Kejian Wang, Lili Liu, Lin Zhu, Lysander Hernandez, Marcus Mann, Matthew Barnum, Matthew Nguyen, Mert Hizli, Nick Papadopoulos, Nitin Kanigicharla, Nolan Clayton, Parimal Deshmukh, Patrick Gao, Pedro Barrios, Rohan Khanderia, Rohit Mehra, Sajal Narang, Shihao Wang, Stan Tian, Tian Zhang, Toby Wong, Tom Sanchez, Tsukky, Vaibhav Bhatia, Vamil Gandhi, William Bowditch, Yao Bian, Yueyang Mi, Zach Akin-Amland, Zezhen Xu

## [1.2.3] — 2026-04-02

### ⚠️ Breaking Changes

- **Loopback auth bypass removed** — `127.0.0.1` / `::1` requests now require a valid token. Port forwarders (`socat`, `ssh -R`) can no longer bypass authentication.
  - **Dashboard users:** Run `kirocrew token` to get a click-and-go URL. The startup banner also prints one automatically.
  - **Electron app:** Authenticates automatically via `~/.kirocrew/.local_secret`. No action needed.
  - **Scripts/automation calling `/api/spawn`, `/api/lessons`, etc.:** Add `X-Internal-Secret` header with the contents of `~/.kirocrew/.local_secret`. Loopback is still required.
  - **SSH tunnel users:** Use the token URL from `kirocrew token` or the startup banner. The 403 page also has a paste-and-go input field.

- **Nonce-based token invalidation** — Only the most recently issued token is valid. If you generate a new token, all previous tokens (and their browser sessions) are invalidated. Re-run `kirocrew token` to get a fresh one.

- **Single-use token consumption removed** — Tokens are now reusable across multiple browsers/tabs/apps until a new token is generated or TTL expires (20h default, up from 6h).

### Security

- Removed blanket loopback bypass in `token_auth_middleware` — closes port-forward auth bypass (security-review finding: 14 exposed dashboards, 5 from this exemption)
- File-based per-session IPC secret (`~/.kirocrew/.local_secret`, `chmod 600`, atomic `os.open` + `fchmod`)
- `X-Internal-Secret` header required for internal API paths (`/api/spawn`, `/api/lessons`, `/api/taskrunner`, `/api/send-message`, `/api/hooks/agent`)
- `/api/token/local` endpoint for Electron/local apps (loopback + file-based secret + `hmac.compare_digest`)
- Constant-time secret comparison throughout (`hmac.compare_digest`)
- `X-Auth-Required: true` header on auth 403s (disambiguates from CSRF 403s)
- `encodeURIComponent` on token values in redirects
- Frontend uses DOM API instead of `innerHTML` (XSS prevention)
- Guard empty `KIROCREW_PROJECT_DIR` (prevents CWD script execution)
- SEL audit logging for all token issuance and auth decisions

### Features

- `kirocrew token` CLI command — prints a dashboard URL with auth token (default 20h TTL, configurable via `--ttl`)
- Startup banner includes click-and-go token URL
- 403 page with token URL input field and dark mode support
- Electron token-prompt with dynamic port from `main.js`
- Session-expired banner with inline token input in dashboard frontend
- `ensure-node.sh` — cross-platform Node 16+ install via mise/nvm (macOS, AL2, AL2023)
- `kirocrew doctor` now checks node version, not just existence

### Fixes

- Re-source mise/nvm after `ensure-node.sh` so newly-installed node lands on PATH
- Post-install node verification in `setup.sh`
- Move all in-method imports to top level in `cli.py`
- Variable name collisions in `_doctor()` (`result` → `node_ver_result`, `py_result`, `kiro_result`)

### Contributors

Bolin Chen (Bolin Chen)

## [1.2.2] — 2026-03-31

### Features

- **Multi-agent orchestration** — Configure multiple named agents with independent kiro-cli backends, workspaces, and memory stores. Switch agents per chat slot from the dashboard or Slack. CLI: `kirocrew agent list|create|update|delete`. HTTP CRUD at `/api/agents`.
- **Inline tool cards** — Tool call results now render inline between text segments, matching the agent's actual execution order. Previously all text merged into one block with tools appended below.
- **Managed agent auto-sync** — managed-agent-installed agents are automatically discovered and persisted into `config.json` as first-class entries with customizable workspace, memory store, and description. Color-coded source badges (managed/kirocrew/builtin) in the frontend.

### Fixes

- Slack Enterprise Grid workspace validation — gateway verifies workspace belongs to the configured Enterprise Grid at startup, blocking connections to personal or external workspaces
- Enterprise Grid DM team_id: copy outer payload `team_id` into event when inner event lacks it
- Missing whitespace between text segments across tool call boundaries
- Slack delivery errors no longer mark cron jobs as failed — delivery failures are logged separately
- Augment PATH for kiro-cli subprocess with MCP binary directories so managed-agent-installed servers work under systemd
- Agent switch handler resolves by config key or kiro_agent name
- Empty agent_name guard prevents silent binding to unintended agent
- `useAgents` hook race condition: skip initial fetch when syncing, add cancelled flag

### Refactors

- Extract shared `useAgents` hook — eliminates duplicated agent fetch logic across ChatPage, CronTab, WelcomeView
- Unified `KiroCrewAgent` TypeScript interface across all pages
- New `SourceBadge` component for consistent agent origin display
- `_config_lock` (asyncio.Lock) around agent CRUD to prevent config.json races

### Contributors

Bolin Chen (Bolin Chen), Joe Guo (Joe Guo), Zezhen Xu (Zezhen Xu), Yuta Tsuji (Yuta Tsuji), Piyush Galphat (Piyush Galphat), Eduardo Vencovsky (Eduardo Vencovsky), David Qian (David Qian)

## [1.2.1] — 2026-03-31

### Features

- **WCAG AA color contrast for all 22 themes** — Added `--accent-fg`, `--ok-fg`, `--warn-fg`, `--danger-fg`, `--info-fg`, `--aim-fg` CSS variables to every theme. All hardcoded `text-white` replaced with theme-aware `-fg` classes so badges, buttons, and status indicators meet WCAG AA contrast ratios in every theme including light variants.

### Fixes

- ACP crash loop recovery: atomic JSON writes for agent config files prevent kiro-cli from reading truncated JSON on startup
- Subagent progress: turns, last_tool, and elapsed now visible in API and dashboard
- Surface kiro-cli stderr on ACP process death for better crash diagnostics
- Retry `ensure_ready` on `AcpError` (not just timeout) for automatic recovery
- Chat draft persistence: fix IIFE so sessionStorage drafts actually load on init
- Smart scroll: only auto-scroll when user is near bottom, reset on slot switch and send
- Preserve queued messages in API response so frontend renders queued banner after tab switch
- `gc.collect()` after provider shutdown to prevent "Event loop is closed" RuntimeError on chat exit
- ffmpeg built from source (LGPLv2.1) instead of binary download per Open Source guidance
- `git stash --include-untracked` in CLI and auto-update so untracked files don't block git pull
- Deny `kirocrew restart/update/kill` in deniedCommands so LLM cannot restart the gateway
- Auto-update saves chat slots before closing sessions
- `--muted-fg` CSS variable added to fix DEBUG log badge contrast in solarized-light and other themes

### Refactors

- Remove 245 lines dead code: `drain_all_kirocrew_processes()`, `get_mcp_servers()`, `_is_valid_mcp_command()`, unused install.sh variables
- DOM construction instead of innerHTML for subagent progress cards (XSS safety)

### Contributors

Bolin Chen (Bolin Chen), Joe Guo (Joe Guo), Yuta Tsuji (Yuta Tsuji)

## [1.2.0] — 2026-03-30

### Features

- **🎙️ Voice memo transcription** — Send a voice memo in Slack and KiroCrew transcribes it via openai-whisper, then responds to the text. Supports native whisper or Docker fallback for AL2. Turbo model (~1.6 GB) runs 8× faster than large. Dashboard STT settings in Overview > Slack tab with one-click install.

- **~~🧠 Dream: memory consolidation~~** — removed (caused memory regressions)

- **📎 File, image & voice handling in Slack** — Drop images (png/jpeg/gif/webp) and they're sent to ACP vision. Drop text/code files and they're injected inline (50KB cap). Voice memos are auto-transcribed. All with credential redaction and audit logging.

- **🏠 Slack Home Tab** — Open KiroCrew's profile in Slack to see a live Block Kit dashboard: gateway status, active cron jobs, running sessions, recent lessons, and a quick link to the web dashboard.

- **🤖 Multi-node mesh communication** — Configure `slack.trusted_bot_ids` to let two KiroCrew instances (e.g. Mac laptop + remote desktop) talk to each other via a shared Slack channel. Self-echo guard prevents loops; cross-bot loop prevention delegated to agent-layer envelope protocol.

- **📢 Per-channel activation & `!ta` command** — KiroCrew can now observe group channels. Use `!ta` in any thread to summon the agent for that thread only. Observe mode persists per-channel so the bot stays quiet unless explicitly invoked.

- **🔇 Silent cron mode & `send_message` MCP tool** — Cron jobs can run silently (`silent: true`) — the agent executes but doesn't auto-post results. Instead, it decides when to notify you via the new `send_message` MCP tool. Perfect for monitoring jobs that only alert on anomalies.

- **⏳ Proactive push: webhooks + wait tool** — Two mechanisms for autonomous workflows. `wait` pauses 60–1800s within a live session (submit change → wait → check automated review → fix → repeat). `POST /api/hooks/agent` accepts external triggers (CI alerts, email) with Bearer auth, ephemeral sessions, and context from `hooks.json`.

- **🔔 Dedicated notifications page** — Full-page notification center at `/notifications` with category tabs (Cron/Hooks/Heartbeat/Agent/Approval/Subagent/Tasks), search filter, date grouping (Today/Yesterday/This Week/Older), and jump-to-source buttons that navigate to the originating chat or Slack thread.

- **🎨 11-theme color system** — Choose from 11 themes (dark and light variants) via the Color Theme picker in Overview > Display tab. Includes configurable zoom level, font family, and cross-instance theme sync.

- **🏷️ Dashboard branding** — Customize the bot's name and avatar across the entire dashboard via `dashboard.bot_name` and `dashboard.avatar` config. Make it yours — call it Jarvis, Friday, or whatever you want.

- **💾 Session restore on startup** — Enable `dashboard.restore_sessions` to automatically restore active chat sessions when the gateway restarts. No more losing your conversation context after updates or crashes.

- **🔐 Per-cron approval mode** — Cron jobs can set `approval_mode: "auto"` so their tool calls execute without interactive approval prompts. The policy flows through to subagents: `cron(auto) → session(auto) → subagent(auto)`. Breaking change: subagent tool approval now defaults to deny when no policy is set (was fail-open).

- **📊 Memory Graph Explorer** — vis.js network visualization of semantic memory relationships in the Overview > Memory tab. Nodes represent memory entries, edges show similarity connections.

- **🔄 Context usage ring** — Real-time token usage indicator in the chat header shows how much of the context window is consumed. Helps you know when compaction is coming.

- **📦 One-line installer** — a single `curl ... | bash` command installs everything (kiro-cli, the managed agent package manager, MCP servers, Node.js, KiroCrew) without a build workspace. For non-contributors who just want to use KiroCrew.

- **⚙️ `kirocrew config` CLI** — `kirocrew config get [key]`, `kirocrew config set <key> <val>`, `kirocrew config edit` for managing `~/.kirocrew/config.json` without hand-editing JSON.

- **🔧 Configurable display** — Zoom level, font family, and theme all configurable from Overview > Display tab. Settings persist across sessions.

- **🌊 Agent world scenes** — Decorative themed environments (neural, wizard, underwater) at `/worlds`. Pure visual personality.

- **📋 Slack app manifest** — Included `slack-app-manifest.yaml` for one-click Slack App creation instead of manual scope configuration.

- **🔀 Streamable HTTP MCP servers** — Support for remote MCP servers via Streamable HTTP transport (URL-based, no local command needed).

### Fixes

- Guard `JSON.parse` in VectorMemoryCard to prevent overview page crash on raw string values
- Cache sync embedding results (LRU 128) to prevent redundant Ollama calls on tab switches
- Fix chat slot switching: persist drafts to sessionStorage, stop stale streaming on switch
- Fix process leak on cancel: sync kill provider, shield reset from CancelledError
- Fix orphaned MCP process leaks: full tree tracking, cycle-safe kill, drain, session cleanup
- Fix macOS process counting: `ps` fallback for `/proc`-dependent code paths
- Deduplicate managed agent installs to prevent inflated metrics (160K+ → ~500 real installs)
- Use Slack display names instead of raw user IDs in LLM context
- Route subagent completions via `KIROCREW_SESSION_KEY` env var
- Deny-by-default agent validation for `_SYSTEM_PREFIX` bypass
- Replace `mesh.claw` with `kirocrew.localhost` (RFC 6761 reserved)
- Gate subagent spawns behind approval when YOLO mode is off
- Throttle parallel step execution to prevent resource exhaustion
- Support SSH agent-forwarded certs in the SSO status check
- Handle ACP slash command notifications (`/compact`, `/clear`, `/agent`) with user-visible feedback
- Persistent `log_level` in config survives gateway restart
- Auto-rotate Slack stream on `appendStream` failures
- Split long cron messages instead of truncating

### Refactors

- Extract task runner into 4 modules: `task_executor`, `task_models`, `task_planner`, `task_reporter`
- Extract reusable `ChatInput` component with drag-to-resize
- Formalized config schema with JSON Schema generation (`config/schema.py`)
- Replace exact substring skill triggering with fuzzy word-overlap matching
- Extract `slack/files.py`, `slack/events.py`, `slack/client.py` from monolithic handler
- Add `aidlc/` project management models for dashboard Projects page
- Add `validation.py` for input validation across cron, config, and user actions
- Enforce format check on builds

### Docs

- Update AGENTS.md with 14 new modules, 5 new pages, complete MCP tools table
- Update README architecture tree, features, config example, and setup instructions
- Update channel-history spec with display name resolution
- Update dashboard spec with WorldsPage and Memory Graph Explorer
- Bundle user-facing docs in Python package for LLM context

### Contributors

Bolin Chen (Bolin Chen), Joe Guo (Joe Guo), Zezhen Xu (Zezhen Xu), Yuliang Qiao (Yuliang Qiao), Yao Bian (Yao Bian), Wenyu Yang (Wenyu Yang), Alex Truong (Alex Truong), Alex Avance (Alex Avance), Carter Trpik (Carter Trpik), Cole Whitley (Cole Whitley), Eduardo Vencovsky (Eduardo Vencovsky), Hoang Phan (Hoang Phan), Hugo Costa (Hugo Costa), Ian Auger-Juul (Ian Auger-Juul), James Joseph (James Joseph), Jingchao Cao (Jingchao Cao), Krish Dhasmana (Krish Dhasmana), Lanxiao Bai (Lanxiao Bai), Mark Asp (Mark Asp), Minglong Pan (Minglong Pan), Mohammed Elansary (Mohammed Elansary), Nagabharan Nagendran (Nagabharan Nagendran), Oscar Smith-Sieger (Oscar Smith-Sieger), Parikshit Desai (Parikshit Desai), Parimal Deshmukh (Parimal Deshmukh), Rohan Khanderia (Rohan Khanderia), Shawn Li (Shawn Li), Srihari Attuluri (Srihari Attuluri), Stephane Robin (Stephane Robin), Sugan Kumar (Sugan Kumar), Swapnil Dixit (Swapnil Dixit), Toby Wong (Toby Wong), Vaibhav Bhatia (Vaibhav Bhatia), Vamil Gandhi (Vamil Gandhi), Vasudeva H (a KiroCrew contributor), Abe Diaz (Abe Diaz)

## [1.1.0] — 2026-03-22

### Features

- **Token-based dashboard authentication** — Remote dashboard access now requires HMAC-SHA256 signed, IP-pinned, single-use tokens. Generate via `!dashboard [duration]` in Slack. Loopback access remains trusted for SSH tunnels. Configurable via `dashboard.url` in config.json.
- **Tiered sandbox modes with credential redaction** — Three sandbox levels (`auto`/`strict`/`off`) for kiro-cli process isolation. Standard mode enables git-over-SSH and AWS CLI via `credential_process` while hiding non-workflow credential stores. New `redact_credentials()` scans all LLM output for plaintext and base64-encoded credential patterns across all 5 output paths.
- **Kiro CLI-compatible script hooks** — Config-driven hook system for pre/post tool execution, enabling custom validation and transformation without code changes.
- **System theme auto-follow** — Dashboard theme toggle now supports a third "system" option that tracks macOS/OS dark mode preference via `prefers-color-scheme`.
- **Configurable slash command name** — Slack slash command name is now configurable via `slack.command` in config.json (default: `kirocrew`).
- **`kirocrew config` CLI** — New `config get/set/edit` subcommands for managing `config.json` from the command line with auto type detection and SEL audit logging.
- **Session title inline rename** — Double-click session titles in the sidebar or header to rename them inline.
- **Multi-select OPTIONS buttons** — LLM responses with `[OPTIONS: ...]` tags now render as interactive multi-select Block Kit buttons in Slack and the dashboard.
- **Unread indicator and typing dots** — Chat sidebar shows unread badges per slot and animated typing dots during LLM streaming. Slack-style grouped message layout.
- **KiroCrewAICapabilities package** — `kirocrew-lite` agent and skills now distributed as a managed agent package, auto-installed during setup and updates.
- **Task naming for task runner** — Tasks get auto-generated names from spec content, resolvable by name or ID.
- **Remote Streamable HTTP MCP servers** — MCP discovery now supports remote servers via Streamable HTTP transport in addition to local stdio servers.
- **Configurable ChatPage settings** — Per-user chat preferences (history expansion, notification limit, timestamps, send-on-enter) with localStorage persistence.
- **Remote embedding support** — Ollama embedding client now supports configurable remote servers with HTTPS validation and SEL audit logging.
- **Full command text in tool approvals** — Tool approval prompts now show the complete untruncated command via cached `rawInput`, with expand/collapse in the dashboard and code blocks in Slack.

### Fixes

- Show full cron job message in dashboard Jobs table
- Propagate default agent setting to new chat slots
- Preserve user customizations in `kirocrew.json` across gateway restarts
- Handle WebSocket not ready on early Slack events
- Nested variant scan for managed-agent skill path discovery
- Only load managed-agent skills from current event snapshot
- Slash command menu with context bypass for `stream()`
- Resolve tool permission bugs blocking all dashboard tool calls
- Restore interactive approval in normal mode (hooks commit broke it)
- Subagent Slack replies split into multiple messages instead of truncating at 3900 chars
- `!agent` command resolves agent name from spec JSON, not just filename
- Timezone-aware UTC timestamps for dashboard messages
- Disable systemd ollama service after Linux install
- Restore parallel execution, fix security sanitization and UI error handling
- Use `npm install` instead of `npm ci` in `build-frontend.sh`
- Remove dead `local_only` param and guard SEL in error handler
- Route subagent completions to original Slack channel
- Resolve flake8 errors (F401 unused import, N806 uppercase vars)
- Remove unused frontend imports to fix TypeScript build
- Run SSL CA bundle setup before aiohttp import caches empty context

### Refactors

- Redesign HooksPage to match dashboard design system (PageHeader + StatCard + Card pattern)

### Docs

- Add branching model and changelog writing rules to CONTRIBUTING.md
- Add macOS desktop app install and launch agent setup guide
- Remove broken SSO auth, update remote desktop docs
- Fix escape setting location in SLACK_SETUP.md and events.py

### Contributors

Arturo Acuaviva (Arturo Acuaviva), Bocheng Wu (Bocheng Wu), Bolin Chen (Bolin Chen), Chaoneng Quan (Chaoneng Quan), Giovanni Viviani (Giovanni Viviani), Hao Xu (Hao Xu), Hoang Phan (Hoang Phan), Hugo Costa (Hugo Costa), Joe Guo (Joe Guo), John Law (John Law), Lanxiao Bai (Lanxiao Bai), Luu Tran (Luu Tran), Oscar Smith-Sieger (Oscar Smith-Sieger), Parimal Deshmukh (Parimal Deshmukh), Petter Nilsson (Petter Nilsson), Rikiya Tsukidate (Rikiya Tsukidate), Rohan Khanderia (Rohan Khanderia), Rohit Mehra (Rohit Mehra), Shao-Cheng Wang (Shao-Cheng Wang), Toby Wong (Toby Wong), Udit Tumuluri (Udit Tumuluri), Vamil Gandhi (Vamil Gandhi), Zezhen Xu (Zezhen Xu)

## [1.0.2] — 2026-03-20

### Features

- **Inline markdown viewer panel** — Click any file path in chat to open a resizable side panel. Supports edit/preview/save with syntax highlighting, dirty tracking, and Cmd+S. Useful for reviewing config files, reading logs, or editing code without leaving the chat. Shift+click opens in Finder instead.
- **Slack allowlist & tracking channels** — Replace the single `KIROCREW_OWNER_ID` gate with a configurable allowed-users list and per-channel tracking. Use `!allowlist @user` to grant/revoke access, `!allowlist #channel` to auto-allow users who join a channel, and `/kirocrew` slash command for quick management. Unauthorized users get an ephemeral rejection. Gateway refactored into focused modules (events, interactions, allowlist).
- **Owner-only `!yolo` command** — YOLO mode now requires an explicit `!yolo on/off/status` command from the owner instead of a button on every approval prompt. Prevents accidental global permission escalation.
- **`!agent` command** — Switch kiro-cli agents on the fly via Slack with `!agent <name>`. Handy when you want a different personality or toolset mid-conversation.
- **Current date in session context** — The LLM now knows "today's date" in every session (Slack, dashboard, subagent, cron). No more wrong-day answers.
- **Persistent sessions (systemd + LaunchAgent)** — Docs and config for running the gateway as a systemd user service (Linux) or macOS LaunchAgent, with auto-restart and a persistent SSH tunnel that survives laptop sleep.

### Fixes

- `kirocrew learn` CLI now reads from vector store instead of only JSONL
- MCP runtime rebuild skipped when dependencies unchanged — fixes tool load timeout
- Subagent spawn now threads the agent name through the full pipeline
- IME composition: CJK Enter no longer triggers message send
- Sandbox exposes `~/.ssh/known_hosts` while hiding private keys
- Skip browser auto-open on remote-desktop sessions
- `kirocrew update` no longer times out on build-tool clean steps
- Preserve user-modified `kirocrew.json` fields across gateway restarts
- macOS `/usr/bin/stat` full path for gnubin users
- SEL audit events for subagent spawn and spawn field validation
- Markdown panel: tilde paths, draggable resize, word-break on long paths

### Refactors

- ChatPage split into ChatSidebar + NotificationViewer (795 → 319 lines)

### Docs

- Remote desktop setup guide rewritten for fresh remote desktops
- SSH tunnel instructions for remote dashboard access
- DCV setup fixes and YOLO mode hint in task runner UI

### Contributors

Alex Avance (Alex Avance), Bocheng Wu (Bocheng Wu), Bolin Chen (Bolin Chen), George Coll (George Coll), Graham Roberts (Graham Roberts), Hao Xu (Hao Xu), Joe Guo (Joe Guo), John Law (John Law), Mark Asp (Mark Asp), Sean Whipple (Sean Whipple), Setul Patel (Setul Patel), Swapnil Dixit (Swapnil Dixit), Rikiya Tsukidate (Rikiya Tsukidate), Xuecong Zang (Xuecong Zang), Zhe Lv (Zhe Lv)

## [1.0.1] — 2026-03-19

### Features

- **SSO credential TTL status card** — Overview page shows SSH cert expiry with color-coded status (green >4h, yellow <1h, red expired), polling every 30s (by Sungjin Yoo)
- **Visible acceptance step in task runner** — acceptance check now appears as a visible step in plan mode, plus dev workflow overhaul and doc cleanup (by Joe Guo)
- **Vercel React best-practices kiro skill** — comprehensive React performance rules covering rendering, rerenders, async patterns, and bundle optimization (by Zezhen Xu)
- **KiroCrew dashboard design file** — Pencil design file covering all 5 pages with dark/light theme axis mapped 1:1 from CSS design tokens (by Zezhen Xu)

### Refactors

- **Frontend component extraction and test infrastructure** — OverviewPage (807→66 lines) and ChatPage (794→649 lines) broken into focused sub-components; Vitest + React Testing Library with 162 tests across 21 files; ESLint 9 flat config; `npm run check` one-command validation (by Zezhen Xu)

### Fixes

- **Linux sandbox rewrite with identity UID mapping** — replaced the two-stage `unshare -rm` → `unshare -U` approach (UID 0 / 65534 broke JVM ByteBuddy, the build tool, Gradle) with the correct namespace pattern: fork → child `unshare(CLONE_NEWUSER)` → parent writes identity UID/GID map → child `unshare(CLONE_NEWNS)` → bind-mount → exec. Child retains real UID so all toolchains work without workarounds. Env var scrubbing (`AWS_SECRET*`, `SSH_AUTH_SOCK`, etc.) added. Tested on AL2 (kernel 5.10) and AL2023 (by Bolin Chen)
- **Restored v1.0.0 artifacts** — an inadvertent revert removed v1.0 changes; restored version, changelog, security spec, code-review config, CONTRIBUTING.md, and system specs (by Bolin Chen)
- **@builder-mcp missing from agent tools** — builder-mcp was configured in mcpServers but not in tools/allowedTools arrays, so its 35 tools were invisible to the LLM (by Tim Lee)
- **@kirocrew-core missing from agent tools** — agents/defaults.json was missing @kirocrew-core in tools/allowedTools, blocking spawn_run, learn_add, task_run and other MCP tools (by Bolin Chen)
- **Merge conflict resolution** — resolved OverviewPage.tsx and handlers.py conflicts for the SSO TTL feature branch (by Bolin Chen)

### Contributors

Bolin Chen, Joe Guo, Sungjin Yoo, Tim Lee, Zezhen Xu

## [1.0.0] — 2026-03-18

### Features

#### Desktop App
- **Electron app for macOS** — native wrapper with auto-start gateway, system tray icon, draggable title bar, loading screen, and retry dialog. Auto-builds DMG as part of the frontend pipeline.

#### Frontend — Complete React SPA Overhaul
- **New React + TypeScript + Tailwind SPA** replacing the legacy dashboard — AgentSelector, Agents page, notification center with read/unread state, vector memory UI, shared component library (`ui.tsx`), and global MCP control panel.
- **Redesigned markdown, code, and diff rendering** — block assembler state machine, dedicated DiffBlock component with line numbers, side-by-side toggle, copy-patch button, and sequenced WebSocket chunks with gap detection for streaming reliability.
- **Search filtering everywhere** — reusable SearchInput component with client-side filtering across MCP registry, chat slots, notifications, and history panels.
- **Welcome screen** — new WelcomeView for fresh chat sessions with agent picker dropdown.
- **Resizable sessions sidebar** — drag-to-resize on the sessions sidebar right edge with 180–800px constraints, orange highlight on hover, and width persisted to localStorage.
- **Feature request button** — in-app button for submitting feature requests directly from the UI.
- **UI polish** — fixed scrolling issues, input bar no longer refreshes the page, history/notification title truncation with two-line wrapping and hover-to-delete, MCP server command/tools columns truncated with hover tooltips, restored sidebar logo and branding.

#### Plan Mode (Task Runner V2)
- **Visible execution plans** — editable plans with step grouping and dependencies, planning progress indicator (pulsing banner) that survives tab switches.
- **Chat round-trip** — send plan to chat for LLM optimization, extract refined plan back into the task runner.
- **Post-completion acceptance check** — automated verification with 3-round remediation loop.
- **Cross-group dependency normalization** — partial deps expanded to full group. 6 API endpoints, 26 sync tests.
- **Git-aware task execution** — branch coordination, per-step isolated kiro-cli sessions, cycle detection in dependency graphs, multi-turn task refinement UI/API. ~5,900 lines with extensive tests.

#### Multi-Agent & Managed Agent Integration
- **Multi-agent switching** — select different agents per chat slot, each with its own personality, tools, and model. A managed agent package manager for installing, updating, and managing agents and skills.
- **Per-agent MCP scoping** — slot creation accepts an `agent` field; each agent only sees its configured MCP servers, reducing noise and improving accuracy.
- **Default agent** — star/favorite toggle on Agents page to mark a default agent, auto-selected for new chat slots. Persisted via new `GET/PUT /api/config/default-agent` endpoint.
- **Skills migrated to managed packages** — 18 bundled skills (~5,900 lines) removed from repo; now installed via the managed package manager for independent versioning.

#### Isolated Dev Environment
- **Dev mode support** — `KIROCREW_HOME` env var overrides config directory (default `~/.kirocrew`), `KIROCREW_PORT` overrides dashboard port (default 7777). `.kirocrew-dev/` gitignored for local dev data, `dev-seed.sh` copies real data into dev directory for migration testing, Vite proxy reads `KIROCREW_PORT` for frontend dev server.

#### Multi-Workspace Support
- **Workspace-scoped lessons** — KiroCrew remembers preferences per project so context doesn't bleed across repos. Workspace tracking on slots with workspace dropdown in frontend.

#### Vector Memory & Semantic Search
- **New vector memory system** — SQLite-backed semantic + episodic stores with Ollama embeddings, similarity search, conflict resolution, and decaying retention.
- **Comprehensive memory improvements** — MMR diversity reranking, hybrid semantic+keyword retrieval, relevance threshold filtering, tag-based filtering, episodic dedup, dashboard tag chip UI.
- **Memory consolidation** — lesson extraction, dedup, pruning, and background LLM distillation of long conversations into reusable knowledge.

#### Session & Context Management
- **ACP session resume** — persistent session ID mapping so conversations survive gateway restarts. Warm pool removed (caused race conditions with stale MCP configs).
- **Context budgets** — budget-aware context injection across preferences, projects, history, lessons, and conversation. Per-tab sessions with recycle and decaying memory.
- **Trust ACP native history** — stops injecting redundant context on follow-ups. Session titles and history pagination.

#### Subagents & Cron
- **Subagent completion injection** — results silently inject into parent session for LLM synthesis. Fire-and-forget `spawn_run` with 20-min timeout + 30-turn limit.
- **Wait mode for `spawn_run`** — MCP tool supports blocking execution. Task runner parallel ordering improvements.

#### Slack Integration
- **Thread-aware channel history** — separates current-thread vs other-thread messages for cleaner context injection. Message splitting for long responses exceeding Slack's character limit.
- **Token setup wizard** — interactive CLI wizard for guided Slack app configuration.
- **Isolated thread context** — channel history now only shows current thread messages, reducing noise.

#### Gateway & Backend
- **Dashboard backend expansion** — kiro usage API, vector memory endpoints, Ollama monitoring, agent/managed-agent/MCP registry endpoints, notification read state.
- **Ollama lifecycle management** — moved to GatewayOrchestrator with improved signal handling, graceful shutdown, and orphaned process cleanup.
- **Performance caching** — mtime-based caching for ConversationLog, SkillsLoader, LessonStore, and system metrics.
- **Doctor diagnostics** — Ollama health check (required for vector memory), auto-fix for common issues, expanded diagnostic output.

#### Platform & Compatibility
- **AL2 / AL2023 support** — Node 16 via nvm for GLIBC 2.26 compatibility, Docker-based Ollama embeddings fallback, Peru/dnf setup support.
- **kiro-cli 1.27 compatibility** — notification handling, MCP server init skip, session timeout fixes. Re-enabled kiro-cli auto-update (SQLite locking issue resolved).
- **Robust launcher** — `bin/kirocrew` with build-tool → pip → PYTHONPATH fallback chain.
- **macOS Gatekeeper quarantine** fix for zip distribution.

### Security

- **OS-level sandbox** — new `sandbox.py` hides credential paths (~/.aws, ~/.ssh, etc.) from kiro-cli subprocesses using Linux unshare bind-mounts or macOS Seatbelt profiles. Zero new dependencies. Includes fix for the AL2 sandbox shim where `unshare -rm` caused UID 0 resolution failures.
- **Prompt-injection hardening** — hook-layer blocking of file reads to sensitive credential directories + output scanning for credential-like query params before posting to Slack/dashboard.
- **Interactive owner check deny-by-default** — rewrote `_handle_interactive()` owner check to reject unless positively confirmed as owner (was fail-open when any value was falsy). Ephemeral message feedback for non-owners clicking buttons. Added `post_ephemeral()` to SlackClientOps ABC.
- **WebSocket origin validation** — validates Origin header on `/api/ws` upgrades, only allowing localhost/kirocrew.localhost origins.
- **SSO authentication & CSRF protection** — dashboard requires valid enterprise SSO cookies and blocks cross-origin mutations.
- **Loopback-only binding** — dashboard bound to `127.0.0.1` instead of `0.0.0.0`, preventing unauthenticated remote access.
- **Deny-by-default owner lock** — Slack gateway now denies all messages when `KIROCREW_OWNER_ID` is unset (was fail-open). Two defense layers: refuse connect + reject messages.
- **Slack interactive button verification** — 5 defense-in-depth layers prevent non-owners from clicking YOLO/approve buttons (HIGH severity code-review-bot fix).
- **SEL audit logging** — integrated across all 8 tool invocation surfaces (Slack, dashboard, taskrunner, subagent, MCP, cron, API middleware) with CLI commands and dashboard endpoints.
- **MCP tool input/output validation** — centralized type-safe schemas, Unicode normalization, hidden character stripping, enum allow-lists, range checks, length limits, response truncation (DoS prevention) across all 12 MCP tool handlers.
- **54 deniedCommands patterns** — new `security.py` module with dynamic reload blocking destructive operations at the kiro-cli level.
- **Frontend accessibility fixes** — replaced unsafe innerHTML, switched to ref callbacks, refactored to React text children instead of HTML strings.

### Fixes

- **Java auto-detection** — setup.sh and `kirocrew doctor` detect missing Java 8 (required by the build toolchain) and auto-install Corretto 8 on macOS. Includes README troubleshooting section and updated doctor checks.
- **build-runtime-exec bypass** — fixed macOS/AL2023 hang where it spawned runaway Ruby processes; launcher and setup now prefer runtime Python directly.
- **MCP server sync** — direct JSON write instead of `kiro-cli mcp add` which corrupted comma-separated args and broke builder-mcp.
- **Tool hook matching** — strip display prefixes ("Running: ", "Reading ") from tool titles before matching auto_approve/auto_deny patterns.
- **Dependency resolution** — use the build runtime exec for reliable module resolution; clean stale runtime symlinks on fresh installs.
- **Case-sensitive APFS** — fixed build-runtime-exec hang on case-sensitive filesystems.
- **Electron DMG build** — fixed APFS compatibility issue in DMG packaging.
- **Git integration tests** — skip marker for tests needing git binary (unavailable in build fleet sandbox).
- **Resource leaks** — fixed orphaned processes and pipe leaks in gateway orchestrator.
- **Force-quit process leak** — added `cleanup_orphaned_sessions()` to the double Ctrl+C path so kiro-cli processes don't survive force-quit. Increased graceful shutdown deadline from 3s to 10s.

## [0.3.1] — 2026-02-24

### Fixes
- **Hard stop** — Stop button now kills the kiro-cli process instantly instead of waiting for cooperative ACP cancel (which could block 30s+ during tool calls)
- **Queue after stop** — queued messages still process after stop via a fresh session with conversation history re-injected; second stop click clears the queue
- **Stopping UI** — shows "Stopping…" indicator with disabled input while stop is in progress, button changes to "Skip Queue" for second click
- **History preserved on update** — active chat slots are now saved to JSONL before auto-update and manual update restarts (previously lost on `os.execv`)

## [0.3.0] — 2026-02-24

### New Features
- **Send images in chat** — attach screenshots or images and the agent sees them directly. Drop a screenshot into the chat to ask "what's wrong with this error?" or share a UI mockup for feedback
- **File picker & screenshot** (macOS) — 📎 opens a native file picker to attach any file; 📷 captures a screen region. Select a log file to ask the agent to analyze it, or screenshot a failing test to get help
- **Response options** — the agent can offer clickable follow-up buttons after a response. Ask "what should I work on?" and pick from suggested tasks, or let cron jobs present action choices
- **Notification acknowledgment** — dismiss notifications with a ✓ button so you can track what you've already seen. Acknowledged state persists across refreshes
- **Diff highlighting** — file changes shown by the agent render with colored green/red lines. Quickly scan what was added or removed without reading raw patch output

### Fixes
- Dashboard chat now saves to memory — preferences, projects, and daily history update after conversations (was only working in Slack)
- Stop button reliably cancels the agent mid-response (no more "Prompt already in progress" on the next message)
- Dashboard update blocked when you have uncommitted local changes (prevents losing work)
- Text selection in dark mode is now visible on user messages
- PWA no longer serves stale cached assets after an update
- Tool messages no longer show a duplicate 🔧 emoji

## [0.2.2] — 2026-02-24

### Added
- **Auto-update kiro-cli** — `kirocrew update` and gateway auto-update now reinstall kiro-cli alongside KiroCrew updates
- **Startup version check** — gateway warns if kiro-cli is older than 1.26 (required for `--agent` flag)

## [0.2.1] — 2026-02-24

### Added
- **Security hardening: deniedCommands** — 54 denied command patterns enforced by kiro-cli agent config covering destructive AWS operations, git push, rm -rf, SQL drops, IaC destroy, and credential theft
- **Audit logging** — preToolUse hook logs every bash command execution to `~/.kirocrew/audit.log` with UTC timestamps
- **security.py module** — built-in deny patterns for tool names, suspicious bash command detection, and conversation history scanner
- **`kirocrew security` CLI** — `kirocrew security deny-list` shows active patterns; `kirocrew security audit` scans history for suspicious tool usage
- **Tamper-resistant config** — deniedCommands always sourced from bundled config, survives stale project copies, dashboard saves, and MCP discovery writes

## [0.2.0] — 2026-02-23

### Added
- **PWA support** — dashboard is installable as a standalone app via Chrome/Safari; includes web manifest, service worker with network-first caching, and app icons (by a KiroCrew contributor)
- **PWA auto-open on macOS** — gateway startup opens the installed KiroCrew PWA app directly instead of a browser tab; falls back to `webbrowser.open()` if not installed

## [0.1.9.5] — 2026-02-23

### Fixed
- **SSL errors on remote desktops** — auto-detect system CA bundle at startup when the bundled Python's default `/etc/ssl/cert.pem` is missing; sets `SSL_CERT_FILE` to `/etc/pki/tls/cert.pem` (AL2) or `/etc/ssl/certs/ca-certificates.crt` (Debian)

### Added
- **Cron job prior-run context** — each cron job now stores its last result and injects it into the next run's prompt, so the LLM reports only changes instead of repeating identical output; persisted to `crons.json` across restarts

## [0.1.9.4] — 2026-02-23

### Fixed
- **MCP servers not loading in agent session** — pass `--agent` flag to kiro-cli so it loads MCP servers from the agent config at startup; resolve MCP commands to absolute paths so kiro-cli finds binaries regardless of PATH
- **Silent prompt timeout** — `stream_events()` now raises `AcpTimeoutError` when the prompt deadline expires without a completion event, instead of silently returning with no response
- **Dashboard swallows ACP errors** — `_run_chat()` now shows a visible error message to the user on timeout/ACP errors, matching the Slack handler behavior
- **Gateway freezes on remote desktops** — `_auto_apply_update()` converted from blocking `subprocess.run()` to async subprocess, so the dashboard stays responsive during auto-update
- **Missing chat messages after tab switch** — when a browser tab is backgrounded the WebSocket can miss `chat_chunk` events; now re-fetches authoritative messages from the server on `chat_done` and on WS reconnect

### Added
- **Active MCP servers info** — `/api/mcp/active` endpoint + ℹ️ button in chat top bar showing MCP servers enabled for the current session

### Changed
- **Increased timeouts** — ACP prompt 5min→30min, ACP init 2min→4min, ACP read 2s→20s, cron job 5min→30min, tool approval 2min→10min, MCP probe 30s→120s, git fetch/pull/build timeouts increased

## [0.1.9.3] — 2026-02-23

### Fixed
- **kiro-cli schema error** — removed `welcomeMessage` field from agent config; newer kiro-cli versions reject unknown fields

## [0.1.9.2] — 2026-02-23

### Fixed
- **macOS workspace PermissionError** — `workspace_root()` no longer hardcodes `/Volumes/workplace`; falls back to `~/workplace` if the volume doesn't exist
- **Chat links not visually clickable** — links in assistant messages now always show an underline (was hover-only, invisible in light theme / Arc browser)

### Added
- **Configurable workspace directory** — `kirocrew setup` prompts for workspace path; saved to `~/.kirocrew/workspace_dir`. Also supports `KIROCREW_WORKSPACE` env var override

## [0.1.9.1] — 2026-02-23

### Fixed
- **macOS setup reliability** — `setup.sh` now shows build-tool platform-support output, adds `--force` flag, and retries once on failure
- **ACP init timeout** — increased from 30s to 120s with one automatic retry for slow kiro-cli first launches
- **Stale MCP servers** — `install_agent()` validates MCP server commands exist in PATH before writing config
- **kiro-cli auto-update** — `setup.sh` runs `kiro-cli update` during setup if kiro-cli is detected

### Added
- **`kirocrew setup --clean`** — fresh agent config install without merging stale MCP servers/tools from existing config
- **Troubleshooting guide** — README section covering macOS build failures, ACP timeouts, Slack setup, and stale MCP servers

### Changed
- **`kirocrew doctor`** — Slack section now labeled "not supported right now" instead of showing confusing ⚠️ warnings

## [0.1.9] — 2026-02-22

### Added
- **Concurrent task runner** — multiple tasks run simultaneously with per-task sessions, work dirs, and task_ids
- **Interactive tool approval** — Normal mode prompts user via dashboard notification (🔐 icon, ✅ Approve / 🚫 Reject buttons); Trust mode auto-approves background ops; YOLO auto-approves all; 2-hour timeout
- **Task runs persistence** — finished runs saved to `runs.json`, survive gateway restarts, delete via ✕ button
- **Expandable step details** — click any step in task card to see LLM response (streams in real-time during execution)
- **Detailed completion notifications** — steps passed/failed, elapsed time, tokens, work dir, full step list with icons
- **Per-task cancel** — ■ button cancels specific task (not all)
- **Delete finished runs** — ✕ button removes completed/failed/cancelled runs from dashboard and disk
- **Reveal in Finder** — click file paths anywhere in the UI to reveal in Finder (`open -R` macOS, `xdg-open` Linux)
- **Clickable paths globally** — `MarkdownRenderer` detects inline code containing file paths, makes them clickable across chat, notifications, and task details
- **Live elapsed timer** — client-side `useElapsed` hook ticks every 1s (same pattern as Overview uptime)
- **Streaming step results** — `step.result` updated during LLM streaming so expanded steps show partial output in real-time
- **Spec name in all notifications** — every task runner notification prefixed with `[spec_name]`

### Fixed
- **Watchdog kills task during approval** — `last_step_time` was 0.0 (never initialized), causing instant 29M-minute stall detection; now initialized to `started_at` and bumped on every text chunk, tool approval, and approval gate
- **Watchdog timeout alignment** — stall warn 30min→60min, stall reset 40min→2h; watchdog no longer fires before step timeout (3h) or approval timeout (2h)
- **Watchdog recovery** — stall flag cleared on `AcpProcessDied` recovery so watchdog can fire again if retry also stalls; `last_step_time` reset after recovery for fresh window
- **Duplicate step notification** — removed bare step success notification from `_execute_step` (was also in `_execute_single_step`)

### Changed
- **Tasks page redesign** — accordion-style run cards (newest first) replacing flat stat cards; collapsed shows status icon + spec name + steps + elapsed + mini progress bar; expanded shows full progress bar, stats, expandable step details
- **Status API enriched** — returns `spec_name`, `started_at`, `finished_at`, `step_details[]` with `result` (2K truncated) and `attempts`

## [0.1.8.2] — 2026-02-22

### Fixed
- **Task runner on fresh install** — create workspace/taskrunner directory before writing inline spec (was FileNotFoundError)

## [0.1.8.1] — 2026-02-22

### Added
- **kiro-cli login in setup** — guided SSO login step with instructions (Start URL, Region, browser confirmation)
- **curl fallback for kiro-cli** — if the packaged install of kiro-cli fails, falls back to `curl -fsSL https://cli.kiro.dev/install | bash`
- **Responsive restart button** — gradient glow button with shimmer animation, adapts text on resize (full → short → icon only)
- **History clear all** — "Clear all" button in Chat history section, deletes all JSONL files on disk with confirmation
- **Subagent delete/clear** — ✕ button per subagent, "Clear completed" button in Agents page

### Fixed
- **kiro-cli package name** — corrected the kiro-cli install package name from `kiro` to `kiro-cli` in setup.sh and setup.ps1
- **Auto-update startup order** — update check now runs before printing Dashboard/Remote URLs, no more duplicate URL output on restart
- **Auto-update version display** — shows "New version X available — auto-updating…" with actual version number
- **Changelog popup in incognito** — first visit silently records version instead of showing all entries
- **Session startup crash** — gracefully handle missing kiro-cli in background/dashboard sessions (warning instead of unhandled exception)

### Changed
- **Windows not supported** — kiro-cli is only available on macOS and Linux; updated all docs (README, AGENTS, DEPENDENCIES, DEVELOPMENT, system specs)

## [0.1.8] — 2026-02-22

### Added
- **kirocrew-core MCP server** — `spawn_run`, `spawn_list`, `learn_add`, `learn_list`, `learn_remove`, `task_run` as native MCP tools (kiro-cli calls directly, no bash needed)
- **MCP auto-sync at startup** — gateway discovers new servers from mcp.json and registers them via parallel `kiro-cli mcp add` (~3s for 6 servers, was ~17s sequential)
- **Chat tab tagging** — messages tagged with `[Chat tab: <name> | Feb 22, 2026, 04:32 PM]` so LLM distinguishes conversations across shared session
- **Diff changelog** — auto-update popup shows only new entries since last seen version, not full history
- **Global Apply & Restart** — moved to Overview page tab bar, accessible from all tabs
- **Delete/clear endpoints** — `DELETE /api/spawn/{id}`, `DELETE /api/spawn`, `DELETE /api/sessions` for subagent and history cleanup
- **Subagent management UI** — delete individual subagents, clear all completed

### Fixed
- **MCP sync reliability** — check returncode, log stderr, separate timeout handling, atomic config write (tmp + rename), cleanup orphan .tmp on failure
- **MCP discovery isolation** — inner try/except so `list_servers()` always runs even if discovery crashes
- **Context retention** — renamed `REFERENCE CONTEXT` to `SESSION CONTEXT` so LLM uses memory/lessons/history instead of ignoring it
- **Compact context preservation** — `/compact` now receives session context (memory, lessons, skills, truncated to 4K) so it survives compaction
- **Test pollution** — mock `Path.home()` in sync test to prevent writing `new-srv` to real kirocrew.json
- **Auto-update log** — prints old version before restart (`Auto-updated from 0.1.7.2`)

### Changed
- **MCP-first architecture** — system prompt references MCP tools only, removed CLI command wrappers from skills
- **Removed learn/subagent builtin skills** — replaced by kirocrew-core MCP tools, stale skills auto-cleaned
- **Agent config on startup** — `install_agent()` merges user-added MCP servers so dashboard additions survive restarts
- **Prompt improvements** — describes memory/lessons/history as usable session context, chat tab priority rules

## [0.1.7.2] — 2026-02-22

- Test release

## [0.1.7.1] — 2026-02-22

### Fixed
- **Changelog popup on first visit** — show changelog on any device's first visit (remote desktop, new browser), not only when `mc-last-version` already existed in localStorage
- **Changelog popup after auto-update** — fetch `/api/changelog` directly instead of calling `checkForUpdate()` which found nothing since code was already pulled
- **Auto-update log message** — print "🐾 Auto-updated to latest version, restarting…" before `os.execv` so the update is visible in terminal output
- **Flake8 F541** — removed f-string prefix from string without placeholders in gateway auto-update

## [0.1.7] — 2026-02-22

### Added
- **WebSocket multiplexing** — single WS at `/api/ws` replaces 3-5 persistent SSE connections + polling. Eliminates UI freezing caused by HTTP/1.1's 6-connection limit. Connection budget: 3-5 persistent → 1 persistent.
- **`ws.py`** — new WS handler with dashboard status push (5s), log subscribe/unsubscribe with ring buffer replay, immediate slots push on connect
- **`useWebSocket.ts`** — single WS hook with exponential backoff reconnect (1s→2s→4s→max 10s); re-fetches state via Redux on reconnect instead of page reload
- **`useUptime.ts`** — client-side uptime counter ticking every 1s from server `start_time`
- **Chat timestamps** — messages show full date (MMM DD, YYYY, HH:MM); history sidebar shows creation date with year
- **Optimistic slot mutations** — `addSlotOptimistic`/`removeSlotOptimistic` for instant sidebar updates without HTTP round-trip
- **WS-based chat streaming** — `?ws=1` mode: POST returns JSON immediately, chunks arrive via WebSocket
- **`_prepare_messages()`** — collapses `chunk` entries into `streaming` role so refresh during active streaming shows partial response
- **20 new skills** — agent-benchmark, writing, aspect-review, code-simplifier, build-tools, code-task-generation, agent-builder, mossy, npm-build-integration, pipeline-workspace, code-search-cli, pe-finder, cloudwatch-url, code-doc-analyzer, estimate-tokens, mcp-configure-tools, mcp-debug, multi-badger, retrospective-thematic-analyzer, tiny-url

### Fixed
- **`ws.send_str()` unawaited coroutine** — aiohttp 3.13's `send_str()` is a coroutine; was silently dropped as garbage-collected coroutine objects. Fixed with `asyncio.ensure_future()`.
- **Chat timestamps reset on resume** — `slot.append()` now accepts optional `ts` parameter; resume passes original timestamps from JSONL
- **History save overwrote timestamps** — replaced multi-step delete→append→patch with single-pass JSONL write preserving original `ts` and `created_at`
- **History not refreshed after chat** — added `push_refresh("history")` after `chat_done`
- **History sort key type mismatch** — `created` (ISO string) vs `modified` (float epoch) would crash on mixed comparison; defaults `created` to ISO from `st_mtime`
- **Slow gateway shutdown (36s)** — WS `async for msg` blocked until timeout; added `close_all_ws()` called before `AppRunner.cleanup()`

### Changed
- **Tasks page** — removed all polling intervals, WS-driven refresh only
- **Logs page** — WS subscribe/unsubscribe via `WsContext` instead of SSE
- **System/Overview pages** — uptime from `useUptime()` hook instead of server-pushed string
- **Dual broadcast** — `_broadcast()` in `state.py` sends to both SSE queues and WS clients for backward compatibility with legacy `dashboard.html`
- **Refine status** — pushed via `broadcast_ws` with throttled chunks (~4/sec)

## [0.1.6] — 2026-02-22

### Fixed
- **Process leak on session restart** — child processes (`kiro-cli-chat acp`) in different process groups now killed via `pgrep -P` sweep after `killpg`
- **Dashboard freeze during restart** — session shutdown runs in parallel via `asyncio.gather` instead of sequential awaits
- **Safe version comparison** — `_version_tuple()` parses versions numerically (`0.1.10 > 0.1.9`), supports patch versions like `0.1.5.1`
- **Doctor test on build fleet** — mock `subprocess.run` and `urlopen` so tests don't spawn real `kiro-cli` processes
- **Fresh config read for auto-update** — `KiroCrewConfig.load()` reads disk on every check, respects runtime toggle

### Added
- **Context window usage tracker** — Agents page shows per-session progress bars with model name, token usage (e.g. `134K / 200K`), prompt count, and health indicator (🟢/🟡/🔴); auto-refreshes every 15s
- **Full changelog viewer** — collapsible "View Full Changelog" in version modal, rendered with MarkdownRenderer
- **GET /api/sessions/context** — endpoint for live session context usage
- **GET /api/changelog** — endpoint to read full CHANGELOG.md

### Changed
- **Memory restart button** — uses `SendBtn` style (matches MCP Servers and Agent Config), shows loading/done feedback
- **Restart warning** — shared `<RestartWarning />` component shown on Memory, MCP Servers, and Agent Config tabs
- **Session restart waits for warm pool** — `await start_pool()` ensures new sessions are ready before returning success
- **Model token map** — comprehensive coverage for Claude 4.x, DeepSeek, Kimi, MiniMax, GLM, Qwen, and 1M context variants

## [0.1.5] — 2026-02-21

### Improvements

- **Changelog rendering** — version popup uses full Markdown (headers, lists, bold)
- **SSE auto-reconnect** — page auto-reloads when gateway restarts (no more stuck "Updating…" overlay)
- **Post-update changelog** — automatically shows what's new on first visit after an update
- **Watermark** — nav sidebar footer with links to Slack, the wiki, and the team directory

## [0.1.4] — 2026-02-21

### New Features

- **Auto-update** — gateway auto-applies updates on restart (toggle in dashboard version popup)
- **Global SSE chat delivery** — messages arrive in real-time even after page refresh (no more manual refresh)
- **Session lifecycle** — active ↔ history with stable keys; save on close/shutdown; no duplicates
- **Browser notifications** — push notifications + tab title badge + nav badge for cron/subagent results
- **Editable daily history** — Memory tab now includes editable history + lessons section
- **Process cleanup** — PID tracking for kiro-cli; orphan cleanup on startup; no more leaked sessions
- **Context delimiters** — previous history wrapped in `[REFERENCE CONTEXT]` so LLM doesn't confuse it with current request

### Bug Fixes

- Chat messages visible after refresh/tab switch without manual reload
- Approval buttons show and work in real-time (no refresh needed)
- No duplicate messages (HTTP stream + SSE mutual exclusion via `_has_reader`)
- No duplicate history entries on resume → close → resume cycles
- Streaming chunks cleaned up after response (prevents message bloat)
- Cron jobs now stateless — session reset after each run (no repeated responses)
- Agent config save auto-restarts kiro-cli sessions
- `os.execv` during update now cleans up sessions first

### Improvements

- In-memory message buffer: 500 → 5000; JSONL rotation: 512KB → 2MB
- Removed `autoAllowReadonly` from agent config (all bash commands require approval in Normal mode)
- Removed `ai-community-slack-mcp` from default agent config
- 32 new regression tests for chat session bugs
- AGENTS.md: single-command build workflow

## [0.1.3] — 2026-02-21

### New Features

- **Self-update** — `kirocrew update` pulls latest and rebuilds; dashboard topbar shows version badge with click-to-check changelog preview and "Update Now" button; only triggers on version bumps
- **`kirocrew status`** — new CLI command queries the running gateway for runtime stats
- **Enhanced `kirocrew doctor`** — checks git, node, project dir, credentials, kiro-cli version, and gateway connectivity
- **Remote dashboard access** — gateway startup prints hostname URL for remote desktop users
- **Run from anywhere** — `bin/kirocrew` now works outside the build workspace

### Improvements

- **Periodic update check** — auto-rechecks git remote every 12 hours; only flags when `__version__` is higher
- **Smart update** — skips rebuild when `git pull` reports "Already up to date"
- **Gateway help text** — updated to "Start the KiroCrew server (dashboard + Slack)"

## [0.1.2] — 2026-02-21

### New Features

- **On-demand MCP discovery** — MCP servers are no longer auto-scanned at startup. Only `builder-mcp` and `kirocrew-cron` load by default. Users trigger discovery from the dashboard via the new 3-step workflow: ① Probe All → ② Enable/Disable → ③ Apply & Restart Sessions
- **Session reset after MCP sync** — Applying MCP config changes now automatically resets all active kiro-cli sessions and re-warms the session pool, so new servers take effect immediately (~30s restart)
- **Config separation** — Dashboard MCP toggle/sync operations now write to `~/.kiro/agents/kirocrew.json` (installed config) instead of `agents/defaults.json` (project source of truth in git)
- **kiro-cli MCP registration** — `sync_to_agent_config()` uses `kiro-cli mcp add --agent kirocrew --force` for proper server registration; falls back to direct JSON edit if kiro-cli is unavailable
- **Probe result caching** — MCP probe results are cached for 30 minutes with TTL-based expiry; stale results show "Outdated" status badge
- **Zero-to-running setup scripts** — Rewrote `setup.sh` to auto-install ALL dependencies from scratch (enterprise SSO → build tooling → kiro/managed-agent/node → build → agent config). No manual prerequisite steps required
- **Windows setup script** — New `setup.ps1` (PowerShell) for Windows with the same zero-to-running flow using `pip install -e .` instead of the build tool
- **Windows launcher scripts** — New `bin/kirocrew.bat` (CMD) and `bin/kirocrew.ps1` (PowerShell) wrappers that set `KIROCREW_PROJECT_DIR` and invoke `python -m kiro_crew`
- **DEPENDENCIES.md** — New document listing all dependencies with install order and platform-specific bootstrap commands

### Cross-Platform Support

- **ACP process management** — Unix uses `start_new_session` + `killpg(SIGTERM/SIGKILL)`; Windows uses `CREATE_NEW_PROCESS_GROUP` + `terminate()/kill()`
- **Signal handling** — Unix uses `loop.add_signal_handler()`; Windows uses `signal.signal()` (asyncio signal handlers not supported on Windows)
- **File locking** — Cron service uses `fcntl.flock()` on Unix and `msvcrt.locking()` on Windows
- **System metrics** — macOS uses `sysctl`/`vm_stat`; Linux uses `/proc/*`; Windows uses `wmic`/`netstat -e`
- **Disk metrics** — Uses `Path.home().anchor` on Windows (e.g. `C:\`) instead of hardcoded `/`
- **Custom domain setup** — `kirocrew setup` now supports macOS, Linux, and Windows hosts file paths with appropriate privilege escalation
- **Frontend build** — `setup.py` tries Git Bash on Windows, falls back to `npm.cmd` directly

### Bug Fixes

- **builder-mcp probe failure** — Fixed asyncio's default 64KB readline buffer causing "Separator is not found, and chunk exceed the limit" errors; increased to 1MB (`limit=1024*1024`) on subprocess streams
- **Probe timeout** — Increased MCP probe timeout from 10s to 30s (builder-mcp can take ~12s to initialize)

### Improvements

- **Dashboard MCP tab** — Redesigned with numbered step buttons, arrow flow indicators, warning banner about ~30s session restart, and instant UI feedback on enable/disable toggle
- **Gateway startup** — Only logs configured MCP servers at startup (no auto-scan/sync), reducing startup time and avoiding unexpected config mutations
- **Spec docs updated** — `memory-skills-hooks.md`, `learn-cron-dashboard.md`, `cli.md`, `acp-client.md`, `AGENTS.md`, and `README.md` all updated to reflect cross-platform and MCP changes
- **Archived old task specs** — Removed completed task specs (`phase-8-12`, `phase-12-1`, `kirocrew-agent`, `kirocrew-internal-agent`, `project-level-config`)

## [0.1.1] — 2026-02-20

### New Features

- **Typing indicator** — Animated bouncing dots (iMessage/Slack style) appear in a chat bubble while the agent is processing, visible during tool execution and LLM streaming
- **Message timestamps** — All chat messages (user, assistant, tool) now display timestamps
- **Notification timestamps** — Cron and subagent notification dates display correctly (fixed "Invalid Date")
- **Auto-open dashboard** — Browser automatically opens the dashboard URL when the gateway starts

### Bug Fixes

- **Session tab isolation** — Messages from a running slot no longer leak into a different slot when the user switches tabs mid-stream; `processStream` guards Redux dispatches with `activeSlotRef` / `streamSlotRef` so only the viewed slot receives updates; SSE sync effect is slot-aware (only blocks SSE for the stream's own slot, not the slot the user switched to)

### Improvements

- **Code quality** — Extracted shared LLM helpers (`stream_and_collect`, `parse_llm_json`, `save_conversation_turn`), centralized config path resolution, consolidated string constants, extracted system metrics handlers, refactored gateway into `GatewayOrchestrator` class
- **Removed dead code** — Deleted legacy monolithic `dashboard.py` (275 lines)

## [0.1.0] — 2026-02-20

First release. KiroCrew is a personal AI agent — a thin orchestrator between user interfaces (CLI, Slack, Web Dashboard) and kiro-cli ACP (LLM calls, tool execution, MCP servers).

### Core Architecture

- **ACP Client** (`acp/`) — JSON-RPC 2.0 over stdio to kiro-cli, with streaming events (text chunks, tool permissions, completion), session lifecycle management, and graceful process cleanup
- **CLI** (`cli.py`) — `kirocrew` command with subcommands: `chat` (interactive REPL), `gateway` (long-running server), `setup` (credentials + agent config + custom domain), `cron` (list/add/remove), `learn` (add/list/remove), `mcp-cron` (MCP server mode)
- **Config** (`config/`) — Dataclass-based config loaded from `~/.kirocrew/config.json` with sensible defaults, credential management via `.env` file

### LLM Providers

- **ACP Provider** — Full-featured: streaming, tool execution, compaction, context tracking via kiro-cli
- **Provider Abstraction** — `LLMProvider` ABC for pluggable backends (ACP is the primary provider)

### Slack Integration

- **Socket Mode Gateway** (`slack/`) — Real-time Slack connectivity via WebSocket, owner-locked DMs, thread-keyed sessions, dedup cache, bot message filtering
- **Streaming Responses** — Real-time typing indicator + streamed text chunks to Slack
- **Tool Approval UX** — Interactive Block Kit buttons (Approve / Trust Session / Reject) for tool permission requests
- **Markdown Formatting** — `to_slack_mrkdwn()` converts LLM output to Slack-compatible markup
- **Dashboard-Only Mode** — Full functionality without Slack credentials; skip during `kirocrew setup`

### Web Dashboard

- **React 18 SPA** — TypeScript + Vite + Redux Toolkit + React Router v7 + Tailwind CSS 3
- **Multi-Session Chat** — Parallel chat tabs sharing a single kiro-cli session, background LLM streaming (survives browser disconnect), message queuing
- **Full Markdown Rendering** — `react-markdown` + `remark-gfm` + `rehype-raw` with Mermaid diagram support, code blocks with language labels and Copy button
- **Chat UX** — KiroCrew logo as assistant avatar, timestamps on all messages, auto-generated session titles via background LLM call
- **Notification System** — Real-time SSE push notifications for cron results, subagent completions, task runner events; click to view full content in main pane
- **Overview Page** — Tabbed management console: Memory (preferences/projects/history editor), Cron Jobs (CRUD), Lessons (CRUD), Skills (CRUD with SKILL.md editor), MCP Servers (probe/sync/toggle), Agent Config (JSON editor)
- **System Page** — Live metrics (2s refresh): CPU %, memory, network speed, disk; process info with full CWD path display
- **Logs Page** — Live log stream via SSE with persistent ring buffer (1000 entries, replays history on connect); runtime log level control (DEBUG/INFO/WARNING/ERROR)
- **Task Runner Page** — Autonomous multi-step task execution with live progress tracking
- **Agents Page** — Session and subagent management
- **Design** — Dark/light theme with amber accent, collapsible nav sidebar, CSS grid layout, animations (rise, slide, scale, shimmer, dot-breathe)
- **Security** — All `dangerouslySetInnerHTML` sanitized via DOMPurify; `sudo tee -a` for `/etc/hosts` (no shell injection)
- **Custom Domain** — `kirocrew setup` uses `kirocrew.localhost` (RFC 6761, no /etc/hosts needed) for `http://kirocrew.localhost:7777`

### Session Management

- **Warm Session Pool** — Pre-spawned kiro-cli processes for instant first response (~30s startup, then instant)
- **Per-Thread Sessions** — Each Slack thread gets its own LLM session; dashboard tabs share one
- **Background Session** — Persistent shared session for cron, heartbeat, lesson extraction, consolidation
- **Context Compaction** — At ≥90% context usage, fires background `/compact` to kiro-cli (fire-and-forget)
- **Circuit Breaker** — Force-reset after 5 consecutive failures on a session
- **Per-Session Semaphore** — Serializes concurrent prompts on the same session key
- **Idle Cleanup** — Sessions expire after 30min idle (configurable); persistent sessions exempt

### Memory System

- **Structured Memory** — `~/.kirocrew/workspace/memory/` with `preferences.md`, `projects.md`, `history/{date}.md`
- **FTS5 Search** — SQLite full-text search index with porter stemming, self-healing on corruption
- **LLM Consolidation** — After 10+ unconsolidated messages, background LLM extracts history entries, preference updates, and project context updates
- **Context Injection** — Memory + skills + lessons + conversation history injected on session start (50K char cap)

### Skills System

- **Two-Tier Loading** — Trigger keywords (fast path, zero LLM calls) + semantic fallback (LLM reads skill file via bash)
- **Project-Level Config** — `skills/` directory editable without rebuilding; auto-copied to `~/.kirocrew/skills/`
- **CRUD via Dashboard** — Create, read, update, delete skills with YAML frontmatter support
- **Built-in Skills** — `learn/SKILL.md`, `subagent/SKILL.md`, `cron/SKILL.md`

### Hooks

- **Config-Driven** — `config.json` → `hooks` section with auto_approve_tools, auto_deny_tools, auto_replies, transforms, context_rules
- **Tool Hooks** — Pattern matching (exact, prefix*, *suffix, *contains*) for auto-approve/deny
- **Message Hooks** — Auto-reply (skip LLM), transform (prepend/append), context injection

### Self-Learning

- **Lesson Store** — Append-only JSONL at `~/.kirocrew/lessons.jsonl`
- **CLI** — `kirocrew learn add/list/remove` for managing corrections
- **Categories** — `tool`, `preference`, `knowledge`
- **Context Injection** — Last 50 lessons injected as `[Learned corrections:]` block

### Cron Service

- **Schedule Types** — `every` (interval), `at` (one-shot), `cron` (5-field expression)
- **Natural Language** — `parse_wakeup()` for human-readable schedules
- **Persistence** — `~/.kirocrew/crons.json` with atomic writes and cross-process file locking
- **Mtime Sync** — Auto-reloads when file modified externally
- **MCP Server** — `kirocrew-cron` exposes `cron_list`, `cron_add`, `cron_remove`, `cron_pause`, `cron_resume` as MCP tools

### Heartbeat Service

- **Periodic Tasks** — Reads `HEARTBEAT.md` for pending tasks, executes via background session
- **Configurable Interval** — Default 60s

### Subagent Orchestration

- **Parallel Execution** — `kirocrew spawn run` spawns isolated background agents
- **Capacity Limits** — Configurable max concurrent subagents
- **Result Routing** — Announces results to dashboard notifications or Slack DM

### Task Runner

- **Spec-Driven** — Reads task specification files, executes multi-step plans autonomously
- **Progress Tracking** — Step-by-step status with success/failure/current indicators
- **Watchdog** — Global timeout and stall detection
- **History Integration** — Persists steps to conversation log for consolidation

### MCP Integration

- **Auto-Discovery** — Detects MCP servers from `~/.kiro/settings/mcp.json` and `~/.kirocrew/mcp.json`
- **Live Probing** — Spawns each server, sends JSON-RPC `initialize` + `tools/list` handshake
- **Auto-Sync** — Discovers new servers and adds to `agents/defaults.json`
- **Dashboard Toggle** — Enable/disable individual MCP servers without editing config files

### Conversation History

- **JSONL Per Session** — `~/.kirocrew/sessions/{key}.jsonl` with metadata + provenance
- **Rotation** — Auto-rotates at 512KB, keeps last 200 messages
- **Cross-Session Context** — Dashboard injects recent conversations from other tabs
- **Session Titles** — Auto-generated via LLM, persisted in JSONL metadata, pushed via SSE

### Developer Experience

- **Frontend HMR** — `./dev-frontend.sh` runs Vite dev server on port 3000 with API proxy
- **Build System** — the build tool compiles Python + builds React frontend
- **Verbose Logging** — `-v` for INFO, `-vv` for DEBUG; runtime level control via dashboard
- **Startup Banner** — Shows `kirocrew.localhost:7777` URL after session pool is warmed (~30s)

### Runtime Stats

- **Metrics Tracking** — Session created/cleaned counts, message counts
- **Dashboard Integration** — Stats exposed via `/api/status`

### Dependencies

- **Python**: `slack-sdk`, `aiohttp` (minimal external deps, prefer stdlib)
- **Frontend**: React 18, Redux Toolkit, React Router v7, Tailwind CSS 3, DOMPurify, react-markdown, remark-gfm, rehype-raw, mermaid, Vite
