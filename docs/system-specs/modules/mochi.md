# Mochi (builtin app)

A desktop pet companion: an always-on-top animated character plus a chat panel,
a watch list, an appearance gallery, and an autonomous "owner loop" that plans
moves/moods, checks watched items, and delivers notifications. The pet windows
render only in the KiroCrew desktop (Electron) shell; the dashboard page is a
browser-visible status/watch/plan surface.

`defaultEnabled: false` and `platform.requiresDesktopApp: true` — it appears in
the App Store, is opt-in, and its window surfaces need the Electron shell.
`permissions`: `api`, `storage`, `events`, `cron`, `spawn`.

## Layout

| Path | What it is |
|---|---|
| `src/kiro_crew/apps/builtins/mochi/app.json` | manifest (`backend.routes`, `backend.hooks`, `backend.mcpServers`, `ui.pages`, agents, permissions, `platform.requiresDesktopApp`) |
| `.../hooks.py` | **the owner loop** — `MochiRuntime`, the `on_startup`/`on_shutdown` lifecycle hooks, the poller/idle/watchlist callback bags, and the notify path |
| `.../queue_file.py`, `.../queue_poller.py` | the behaviour queue (planned moves/moods/reminders) + the poller that executes it — a **file-based scheduler that lives beside core `cron.py`**, not on top of it |
| `.../watchlist_file.py`, `.../watchlist_service.py` | watch items (add/cancel/remove/update), cross-process-locked RMW |
| `.../pinned_files_service.py`, `.../stats_service.py`, `.../idle_manager.py` | pinned-file tracking, companion stats, idle/presence |
| `.../notification_gate.py` | leading-edge notify gate (merge window, silent mode, critical bypass) |
| `.../agent_policy.py` | builds the per-agent MCP policy (grants + **neutralize**) |
| `.../soul_loader.py` | persona text + rendered agent prompt |
| `.../mcp_server.py` | the stdio `mochi:mochi` MCP server (get/update plan, perform_pet_action, watchlist, pins, read_mochi_file) |
| `.../redact.py` | shared credential/exfiltration-URL redactor for agent-authored data |
| `.../backend/routes.py` | the dashboard/panel HTTP surface (gated + validated) |
| `.../agents/mochi.json`, `.../agents/mochi-bg.json` | the two shipped agent specs (chat + background) |
| `.../agents/context/`, `.../agents/skills/` | the behaviour prompt + bundled skills |
| `website/src/apps/mochi/` | vendored renderer (`ChatPanel`, `WatchlistPanel`, `GalleryPanel`, `SettingsPanel`, pet overlay) + the `mochiApi` seam |
| `website/electron/mochi/` | the Electron main-process shell (pet overlay windows, panel/settings/avatar windows, global shortcuts, multi-instance) |

## Load-bearing contracts

- **Reserved `mochi` chat slot.** The chat panel is backed by a dedicated core
  chat slot keyed `mochi`. It is the app's identity for streaming/approvals; do
  not repurpose it.
- **First `backend.hooks` lifecycle consumer.** Mochi is the first builtin to
  use the App Kit `on_startup`/`on_shutdown` lifecycle hooks. `on_startup` is
  **async** — it loads persisted stats/pins off the event loop via
  `asyncio.to_thread` (the lifecycle dispatcher awaits a coroutine-returning
  hook), so enabling/disabling Mochi never blocks gateway requests or heartbeats.
- **MCP neutralize policy.** `agent_policy.py` grants the app's own tools and
  **neutralizes every ungranted ambient MCP server** — including an unprobed one
  (empty tool list) — by handing the bridge a `disabled: true` server-level
  entry. Deferring the unprobed case would be a fail-open that leaks ambient
  access. The shipped agent templates carry **no `allowedTools`** (that would
  skip the PreToolUse gate); tools live in `tools` and are gated per call.
- **File-based scheduler beside `cron.py`.** The behaviour queue + poller are a
  separate, app-owned scheduler (moves/moods/reminders on `nextCheckAfter`), not
  a layer over core cron. The `update_watchlist` MCP schema must advertise every
  op the poller/prompts send (`add`/`cancel`/`update`/`remove`, plus
  `historyEntry`/`notified`/`nextCheckAfter`) or a watch-check write is rejected
  and the item re-spawns forever.
- **Agent-authored data is redacted at every browser sink.** notify/mood/
  watchlist/chat-push/pins all pass through `redact.redact_tree` (or
  `_redact_plan_tree`) before reaching the browser.

## Deliberate divergences (do NOT "fix" in an upstream sync)

- **The vendored `ChatPanel`/`panelBridge` are a deliberately owned fork, not a
  convergence-pending copy.** The product intentionally keeps Mochi's original
  chat surface rather than adopting the dashboard's `ChatEmbed`. Consequence,
  accepted: an approval-flow or widget-protocol change made in the dashboard
  chat must be ported to Mochi's panel too — the two surfaces are maintained in
  parallel on purpose. Do not replace `ChatPanel` with `ChatEmbed`.
- **The Electron main-process code under `website/electron/mochi/` is a
  first-party exception**, not a precedent that third-party builtins may ship
  main-process code. App Kit apps are renderer + backend only; Mochi's shell
  windows exist because it is a first-party desktop pet.

## Cross-platform

Shortcut hints resolve `CommandOrControl` for the current platform via
`website/src/apps/mochi/src/shared/shortcut.ts` (⌘ on macOS, `Ctrl` on
Windows/Linux) — the pet's Hide hint is the only recovery path once hidden, so a
wrong glyph there strands the user. Route/pageUrl and window discovery are
generic App Kit mechanisms (`/app-windows/<app>/<name>.html`), carrying no
mochi-specific branching in core.
