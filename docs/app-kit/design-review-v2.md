# KiroCrew App Platform & Client SDK — Design Review

**Author:** Ray Xu (rayrayxu) **Date:** 2026-04-22 **Status:** Approved - Implemented
* * *

## 1. Problem Statement

KiroCrew's contributor community has grown rapidly. Contributors are building increasingly ambitious features on top of the platform — Slack inbox management (Secretary), multi-agent collaboration (Channels), visual agent monitoring (Worlds), desktop pet assistants (Mochi). This is a great problem to have, but it creates two tensions:

**For core maintainers:** Every new feature lands directly in the main codebase. Contributors modify shared files (App.tsx, server.py, handlers.py) to add their features, which means core maintainers must review and maintain code for features they didn't build. As the contributor base grows, this doesn't scale.

**For app builders:** There's no standard way to build on KiroCrew. Contributors must dig through source code to understand endpoints, auth, response shapes, and dashboard wiring. External tools have no way to interact with the Gateway without reimplementing HTTP/WS patterns from scratch.

**User Stories:**

* **Secretary (lanxib@)** — Built a Slack inbox management feature for KiroCrew. The biggest pain point was the development cycle. Every code change required manually stopping and restarting the Gateway in the terminal to see the effect. Tried to avoid modifying core KiroCrew functionality to reduce risk, but still had to register routes in shared files. Not being familiar with UI design, had to iterate by trial and error. It would have been much easier with standardized UI components and clear API access.
* **Mochi (rayrayxu@)** — Built a desktop pet assistant as an Electron app that connects to the KiroCrew Gateway running on a remote host. Spent significant time figuring out the auth flow. Ray has to read source code to understand cookie formats, secret file paths, and the token exchange mechanism. When the token auth system was introduced, it took multiple iterations to get the Electron app to authenticate correctly. There was no documented way for an outside app to obtain and refresh tokens. Even after getting it working, there was no way to distribute Mochi to other KiroCrew users without asking them to clone the repo and set it up manually.
* **Pipeline Health Tool (moelansa@)** — Wanted to build a pipelines dependency visualization tool with in-place AI actions for issue resolution. Started building it outside KiroCrew and tried to call Gateway APIs directly. Ran into auth issues and couldn't distribute it to teammates easily. Quote: *"What if we have a way to accommodate custom apps in KiroCrew, similar to VS Code extensions — each app has its own config, a defined interface to talk to agents and sessions, and can be distributed via a package manager."*

* * *

## 2. What is a KiroCrew App

A KiroCrew App is a self-contained extension that adds functionality to the KiroCrew platform. An app is defined by a manifest (`app.json`) and can include any combination of agents, skills, cron jobs, a dashboard UI page, and a backend service. An app must do something beyond what a single capability package provides. It may coordinate multiple resources, adds a UI, runs a backend service, or manages its own scheduling. An app may bundle capability packages as part of its resources, so the two systems compose naturally.

### App types

|Type	|Where it runs	|Example	|
|---	|---	|---	|
|Platform app	|Inside the Gateway — UI pages, backend service, crons, agents	|Secretary (Slack inbox management with UI + polling + crons), Pipeline Health Tool (dependency visualization with AI actions)	|
|---	|---	|---	|
|External app	|Outside the Gateway as a separate process, connects via SDK. Could also have dashboard UI pages	|Mochi (Electron desktop pet assistant)	|

### Plugins vs Apps

A key architectural distinction: **plugins extend the Gateway's capabilities; apps consume them.**

|A	|Plugin	|App	|
|---	|---	|---	|
|Relationship to Gateway	|Extends — adds new APIs, event types, or memory providers	|Consumes — uses existing Gateway APIs	|
|---	|---	|---	|
|Example	|Context injection (adds a new endpoint for silent LLM context), custom memory provider	|Secretary (uses existing chat, cron, notification APIs), Mochi (uses existing slot, spawn APIs)	|
|Permission level	|Higher — can modify platform behavior	|Standard — operates within declared permissions	|
|Review bar	|Requires core team review (changes platform contract)	|Standard pull-request review	|

Apps can depend on plugins (e.g., Mochi depends on the context injection plugin), but apps themselves do not modify KiroCrew's behavior. This separation ensures that the core platform remains stable — only reviewed plugins can change how the Gateway works.

### KiroCrew App Store

The App Store is the single place where users discover, install, and manage apps (see Section 5 for the full design).

### Apps vs capability packages

|A	|Capability Package	|KiroCrew App	|
|---	|---	|---	|
|Scope	|Single agent, skill, or MCP server	|Full application with lifecycle	|
|---	|---	|---	|
|UI	|None	|Optional dashboard pages	|
|Backend	|None	|Optional backend service	|
|Lifecycle	|Install/uninstall/enable/disable/update	|Install/uninstall/enable/disable/update	|
|Distribution	|git (package manager)	|git (App Store)	|
|Inclusion criteria	|Any useful agent or skill	|Must provide differentiated experience beyond agents/skills alone	|

* * *

## 3. Requirements

Derived from the user stories and the "what is an app" definition. These requirements apply to both app types (platform and external):

|Requirement	|Rationale	|
|---	|---	|
|Apps must be installable without modifying core KiroCrew files	|Decouples app development from core releases	|
|---	|---	|
|Apps must have a standard manifest format declaring capabilities and permissions	|Enables automated validation, App Store display, and security enforcement	|
|App builders need a type-safe client library for Gateway HTTP and WebSocket APIs	|Eliminates reimplementation of auth, retry, reconnection across consumers	|
|Platform apps with UI must render inside the dashboard without breaking the host	|Enables rich app experiences while protecting platform stability	|
|Apps must be isolated from each other — a buggy app cannot destroy another app's data	|Protects user data and other apps from accidental damage	|
|The platform must support apps that run outside the Gateway process (desktop apps, CLI tools)	|Enables Mochi, external CLI tools, and future external integrations	|
|App installation and auth must be transparent to the developer	|Reduces friction and auth-related bugs	|
|App builders must be able to distribute their apps to other KiroCrew users without requiring manual setup	|Enables an app ecosystem where users discover and install apps with one click	|
|The security model must protect critical data (memory, lessons, chat history) from unauthorized access	|Prevents data leakage between apps	|

|10|Apps must declare what data they access (memory, lessons) and why|Gives users visibility and control over their data, similar to OS-level permission prompts|
* * *

## 4. Architecture

A three-layer architecture addresses these requirements:

```
  ┌──────────────────────────────────────────────────────────────┐
  │  Layer 3: App Framework                                      │
  │  Manifest (app.json), scaffold CLI, App Store, lifecycle     │
  │  — every app uses this layer                                 │
  ├────────────────────────────┬─────────────────────────────────┤
  │  Layer 2: App SDK          │  Layer 1: Client SDK            │
  │  (@kirocrew/app-sdk,       │  (kirocrew-client, Python; or   │
  │   host-provided)           │   direct REST/WS)               │
  │  React hooks + shared      │  HTTP + WS client for Gateway   │
  │  components for dashboard  │  Auth, retry, reconnection      │
  │  UI pages                  │                                 │
  └────────────────────────────┴─────────────────────────────────┘
```

**Why three layers:**

* **Layer 1 (Client SDK)** solves R3, R6, R7 — type-safe Gateway communication for any runtime (Node.js, Python, browser). Handles auth, retry, reconnection so app builders don't have to.
* **Layer 2 (App SDK)** solves R4 — React hooks and shared components for dashboard UI pages. Permission-scoped API context prevents apps from accessing undeclared endpoints.
* **Layer 3 (App Framework)** solves R1, R2 — manifest format, scaffold CLI, registry, and lifecycle management. Apps are self-contained directories that install without touching core files.

R5 and R9 (isolation and security) are cross-cutting and enforced at the Gateway level via app-scoped tokens (Section 6).

R8 (distribution) is addressed by the App Store (Section 5).

### Key design decisions

**SDK is a wrapper, not a gateway.** The SDK wraps existing Gateway HTTP endpoints. No new protocol or middleware. This keeps the Gateway as the single source of truth — no version skew between SDK and Gateway.

**Shared React Tree for UI (not Shadow DOM or iframe).** App UI pages load into the dashboard's React tree, sharing the host's React instance and component library. Shadow DOM only isolates CSS, not JS. iframe would provide real isolation but at significant DX cost. We chose DX over isolation for trusted internal apps, with a documented upgrade path to iframe sandbox for untrusted apps.

**Sidebar is data-driven.** The sidebar's Apps group loads from `/api/apps` instead of hardcoded items (see Section 5). Users can hide apps they don't use.

### Backward compatibility

The SDK wraps existing Gateway endpoints. If a newer SDK calls an endpoint that doesn't exist on an older Gateway, it degrades gracefully with a clear error. Core APIs (slots, chat, spawn, cron, lessons) are stable across all Gateway versions. Newer features (context injection, MCP management) are optional — apps that use them handle the fallback.
* * *

## 5. App Store & Distribution

The App Store solves the distribution problem. App builders can reach all KiroCrew users without requiring them to clone repos or run manual setup.

### Discovery

The App Store lives in the dashboard sidebar. It has two tabs:

* **Apps** — Full KiroCrew Apps with UI, backend, or workflow integration. Curated — each app must provide a differentiated experience.
* **Agents & Skills** — capability packages (agent configs, skills, MCP servers).

### Installation

One-click install from the App Store for platform apps. The platform handles downloading, manifest validation, resource registration (agents, skills, crons), and secret generation for app authentication. Apps with UI pages appear in the sidebar immediately after install.

External apps declare their type in the manifest. When the platform can handle installation directly (e.g., downloading a binary), it offers one-click install. When client-side setup is required (e.g., a desktop Electron app on a remote host), the App Store shows installation instructions instead. The app registers itself with the Gateway when it first connects.

### Publishing

App builders publish by submitting their app to a curated git-based registry via pull request. The registry entry points to the app's git repository. When a user clicks Install, the platform clones the app and runs the install lifecycle.

### Self-managed apps

External apps (like Mochi) that run as separate processes register with the Gateway at runtime. The App Store shows them as "installed (self-managed)" — users can see what's connected but lifecycle is managed by the app itself.

### Builder workflow

Scaffold → develop → test locally → publish to registry. The platform provides a scaffold CLI that generates a complete app directory structure. Agent and skill changes take effect immediately; UI changes require a build step. Details in Appendix E.
* * *

## 6. Security

The Gateway API is open, meaning app builders can always bypass the SDK and call endpoints directly. The security model's goal is not to restrict what builders can do, but to make the safe path the easy path. The SDK handles auth, scoping, and isolation automatically so builders don't have to think about it. App-scoped tokens (Section 6.1) and resource ownership checks (Section 6.2) provide a safety net at the Gateway level — even if an app doesn't use the SDK, a buggy delete call can only affect that app's own resources.

### 6.1 App Identity & Authentication

Apps need a cryptographically verified identity so the Gateway can enforce isolation. We extend KiroCrew's existing HMAC-SHA256 token system with an `app` scope.

**Flow:**

1. **Install** — Gateway generates a per-app secret and stores it securely on disk
2. **SDK init** — SDK reads the secret automatically. Developer only writes `KiroCrewClient({ appName: 'my-app' })`
3. **Token exchange** — SDK exchanges the secret for a short-lived HMAC token with the app's identity embedded
4. **Request auth** — Every request carries the token. Gateway middleware verifies the signature and extracts the app identity
5. **Refresh** — On token expiry, SDK auto-re-exchanges using the on-disk secret. Developer never sees a token error

**Key properties:**

* App identity is cryptographically verified — cannot be spoofed
* Token lifecycle matches user tokens (default 1h, max 20h)
* Developer experience is zero-config — no manual token management
* Existing API calls without app scope work exactly as before (backward compatible)

**Security checks beyond install time:** The secret is validated on every token exchange, and the token is validated on every request. If the secret is deleted, the app cannot authenticate. If the Gateway restarts, all tokens are invalidated and the SDK auto-refreshes.

**User identity:** Apps authenticate as themselves (app name), not as a user. For self-managed apps running as external processes, the app identity is the only identity.

Implementation details (API paths, file permissions, header formats) are in Appendix F.

### 6.2 App Isolation

See Open Question #1 for the scoping boundary discussion.

The guiding principle: **protect against bugs, not just malice.** A buggy app that accidentally deletes resources in a loop should only damage its own data.

**Resource tagging:** Resources created through an app-scoped token are tagged with the app name (from the verified token). Resources created by the dashboard UI are unscoped.

**Destructive operation scoping:**

|Operation	|Rule	|
|---	|---	|
|Delete slot, cron, lesson, MCP server	|App can only delete resources it created	|
|---	|---	|
|Clear notifications	|Dashboard session only — app tokens blocked	|
|Context injection, send message	|App can only target slots it owns (or unscoped)	|

**Read and create operations are unrestricted** — apps can list all slots, create new crons, etc.

**Unscoped resources** (user-created via dashboard) cannot be deleted by any app token. Only dashboard sessions can delete unscoped resources. This is deny-by-default.

### 6.3 Context Injection (Proposed)

**Motivation:** Apps like Mochi need to feed background information (e.g., watchlist check results, subagent completions) into a chat session's LLM context without triggering a visible message or an LLM response. Today there's no way to do this — apps either send a visible message (disruptive to the user) or keep the information local (invisible to the LLM on the next turn). Context injection solves this by allowing apps to silently queue content that gets consumed on the next user-initiated message.

**Why this is a security concern:** It allows apps to put arbitrary text into the LLM prompt — the highest-risk surface in the app platform.

**Enforcement:** App-scoped slot ownership (Section 6.2) applies.

**Limits:** Per-entry content size (40,000 chars), per-slot pending queue (50 entries, FIFO eviction), optional TTL per entry.

**No content sanitization (by design).** Content is prepended to the LLM prompt as-is. The trust boundary is at the app level, not the content level. If we ever support untrusted apps, context injection must be the first capability to get server-side permission gating.

**Memory exposure:** Context injection does not expose the user's memory store. Apps can search memory via a separate read-only endpoint, subject to the same permission model. Memory write operations are not exposed through the SDK.

### 6.4 Permission Model

App permissions are declared in the manifest (`app.json`), specifying which API paths and WebSocket events the app can access. The App SDK checks declared permissions before each request — undeclared paths are blocked. This helps builders catch misconfiguration early rather than at runtime.

**Memory access permissions:** Apps must declare what level of memory access they need and why — similar to how macOS apps request access to contacts or location.

Memory write operations are not exposed to apps — only the Gateway's internal consolidation process writes to memory. This prevents apps from corrupting the user's learned context.

**Note:** KiroCrew's memory system is actively being redesigned to support multiple tiers of memory. The permission levels above will evolve as the memory architecture matures — apps may need to declare which memory tiers they access, not just the access level.

### 6.5 Trust Model & Review

* All apps are from git repositories, reviewed via pull request before being added to the registry
* App UI runs in the shared React tree (no JS isolation) — acceptable for trusted, reviewed code
* Install scripts run with the same trust level as any code you clone and run

**Automated app review:** App repo pull requests will include an automated code-review pass checking for manifest validation, destructive API usage, potential prompt injection in `injectContext` calls, hardcoded secrets, and permission mismatches. Advisory initially — human reviewer makes the final call.

### 6.6 Audit

All security-relevant operations are logged via SEL:

|Operation	|Logged fields	|
|---	|---	|
|App token exchange	|app name, outcome, error	|
|---	|---	|
|Context injection	|app name, slot key, outcome	|
|Slot mutation by app	|app name, slot key, outcome	|
|App install/uninstall	|app name, outcome	|

* * *

## 7. Rollout Plan

|Phase	|Status	|What it covers	|
|---	|---	|---	|
|**Phase 0: App Store**	|✅ Complete	|App manifest, manager, registry, App Store UI, builtin app registration, data-driven sidebar, blob proxy, reverse proxy	|
|---	|---	|---	|
|**Phase 1: Client SDK**	|✅ Complete	|`@kirocrew/app-sdk` (dashboard UI hooks, host-provided via import map) and `kirocrew-client` (Python, pip) — HTTP client, WebSocket, retry, context injection, manifest validation, app lifecycle, gateway manager, agent/skill/MCP installation helpers	|
|**Phase 2: App Identity**	|🔧 In progress	|Per-app secrets, app-scoped tokens, `request["app"]` in middleware, resource `_app` tagging, destructive operation scoping	|
|**Phase 3: Validation**	|⏳ Next	|Mochi migration to SDK	|
|**Phase 4: Hardening**	|⏳ Planned	|Automated app review	|

**What's ready for review:** Phase 0 (App Store) and Phase 1 (Client SDK) have working implementations. This design review covers the overall architecture and security model that governs all phases.

**What's experimental:** Context injection, app-scoped tokens, destructive operation scoping. API may change based on consumer feedback.
* * *

## 8. Future Vision

The SDK is location-agnostic by design — it takes a `baseUrl` and authenticates via app-scoped tokens (Section 6.1), regardless of where the Gateway runs. Today that's localhost or a remote host over an SSH tunnel. If KiroCrew moves to a dedicated cloud service, the same `KiroCrewClient` works without changes — only the auth model evolves (local secret → OAuth). This positions the SDK as the universal integration point for any tool that wants to tap into a user's KiroCrew context (memory, lessons, agents).
* * *

## 9. Open Questions

**Destructive operation scoping boundary.** Is "destructive only" the right cut? Should we also scope update operations (e.g., `PUT /api/crons/{id}`)? Or is that over-engineering for internal apps?

**SDK distribution.** The dashboard UI SDK (`@kirocrew/app-sdk`) ships inside the KiroCrew frontend and is provided to apps at runtime via the host's import map (`window.__kirocrew_modules`) — apps do not install it. The Python client (`kirocrew-client`) is a standalone package installable via `pip` from this repo (`packages/kirocrew-client-py/`). Node.js/Electron apps call the Gateway REST/WS endpoints directly.

**Memory access scoping.** Should `GET /api/memory/episodic/search` be scoped per-app, or is read access to the user's memory acceptable for all apps? Current design: unrestricted read, no write.
* * *

## Appendix

### A. SDK Implementation Details

**Client SDK (`@kirocrew/app-sdk` for dashboard UI / `kirocrew-client` for Python)** provides:

* Authenticated HTTP requests (Cookie-based port-specific tokens, localhost skip, app-secret auto-exchange)
* Retry with exponential backoff (5xx, 429 with Retry-After, network errors)
* WebSocket connection with auto-reconnect and event filtering
* Typed methods for all Gateway endpoints (slots, spawn, cron, lessons, MCP, memory, approvals, models, config)
* Context injection with local buffering and auto-flush
* App lifecycle management and manifest validation
* Agent config, skill, and MCP server installation helpers (Node.js / filesystem)
* Gateway process management (start, stop, health check)

The SDK is a thin wrapper over existing Gateway endpoints — it does not add new endpoints or change the API contract. Apps can always fall back to raw `fetch()`.

**Node.js compatibility:** The SDK detects browser vs Node.js at runtime (`typeof ws.on === 'function'`) and adapts WebSocket handling accordingly. For environments where the global `WebSocket` is unavailable, a `WebSocketImpl` option accepts a custom constructor:

```
import WebSocket from 'ws'
const mc = new KiroCrewClient({ WebSocketImpl: WebSocket })
```

**API stability tiers:**

|Tier	|Methods	|Expectation	|
|---	|---	|---	|
|Stable	|`ping`, `getStatus`, `createSlot`, `listSlots`, `deleteSlot`, `sendMessage`, `spawn`, `listCrons`, `addCron`, `listLessons`, `approveAction`, `rejectAction`, `listModels`	|Unlikely to change	|
|---	|---	|---	|
|Settling	|`connect`, `onChatChunk`, `onChatDone`, `onConnectionChange`, WS event types, `getSlotHistory`, `setSlotModel`, `getApprovalMode`, `setApprovalMode`	|Shape solid, payloads may evolve	|
|Experimental	|`injectContext`, `flushPendingContext`, `registerMcpServer`, `registerAppMcp`, `installAgentConfig`, `installSkill`, `AppLifecycle`, `GatewayManager`	|May change based on feedback	|

* * *

### B. Mochi Migration Path

Mochi currently manages tokens manually (`obtainToken` → `kirocrew token` CLI). After migration:

1. `registerExternal()` writes `.app_secret` to `~/.kirocrew/apps/mochi/`
2. Mochi passes `appName: 'mochi'` to the SDK
3. SDK auto-reads secret, auto-exchanges token, auto-refreshes on expiry
4. Mochi's custom `obtainToken` / `onAuthExpired` code can be removed

* * *

### C. Package Structure

```
packages/kirocrew-client-py/         # kirocrew-client (pip — standalone Python client)
├── kirocrew_client/
│   ├── errors.py              # KiroCrewError + error codes
│   ├── client.py              # KiroCrewClient (async HTTP, aiohttp)
│   ├── ws_client.py           # WebSocket client
│   ├── manifest.py            # AppManifest validation
│   ├── lifecycle.py           # App lifecycle management
│   └── gateway_manager.py     # Gateway process management
├── tests/
└── pyproject.toml

website/src/app-sdk/                 # @kirocrew/app-sdk (host-provided, import-map)
├── index.ts                   # React hooks (useAppApi, useAppEvents, etc.)
├── shared-modules.ts          # Registers host modules on window.__kirocrew_modules
├── ChatPanel.tsx / ChatEmbed.tsx / ... # Shared chat components
└── useChatSession.ts
```

The `@kirocrew/app-sdk` hooks are bundled into the dashboard and exposed to apps
at runtime through the import map (`@kirocrew/app-sdk` → `/vendor/*.mjs` stubs →
`window.__kirocrew_modules`) — there is no separately published npm package, and
no separate TypeScript gateway-client package. Node.js/Electron apps call the
Gateway REST/WS endpoints directly.

The Python client includes WebSocket support (`ws_client.py`), gateway
process management, and app lifecycle management — covering the full Gateway
API surface.
* * *

### D. App → Core Feature Pipeline

Apps serve as an incubation ground for platform capabilities. When multiple apps independently implement the same pattern, that's a signal to extract it into core:

* **Scheduling + polling:** Both Mochi and Secretary implement their own polling loops → candidate for a Gateway "watch" primitive
* **Background agent dispatch + result injection:** Both Mochi and external CLI tools need async agent dispatch → led to the Context Injection API
* **Notification routing:** Secretary routes Slack messages; Mochi routes desktop notifications → candidate for a unified notification pipeline

This creates a virtuous cycle: apps innovate → patterns emerge → patterns get extracted into core → SDK distributes them → all apps benefit.
* * *

### E. Builder Workflow Details

```
mkdir my-app && cd my-app              # 1. Create app directory
# Create app.json, agents/, skills/    # 2. Develop
cd ui && npm run build                 # 3. Build UI (if app has UI)
# POST /api/apps/install               # 4. Install locally
# POST /api/apps/{name}/enable         # 5. Enable
# Submit to app-registry.json via PR   # 6. Publish to App Store
```

The app directory should contain:

* `app.json` — manifest with name, version, capabilities
* `agents/` — agent config JSON files
* `skills/` — skill directories with SKILL.md
* `ui/` (if app has UI) — Vite + React using the host-provided `@kirocrew/app-sdk` hooks and `@kirocrew/app-sdk/ui` components
* Cron entries in manifest (if app needs scheduling)

Agent and skill changes take effect on the next agent invocation (no rebuild). UI changes require `npm run build` + dashboard refresh.
* * *

### F. Security Implementation Details

**App secret storage:** Per-app secret generated via `os.urandom(32).hex()`, written to `~/.kirocrew/apps/{name}/.app_secret` with file mode `0o600`.

**Token exchange endpoint:** `POST /api/apps/{name}/token` with `X-App-Secret` header. Returns `{ "token": "<HMAC token>" }`. Token payload includes `"app": "<name>"`.

**Middleware extraction:** After HMAC signature validation, middleware decodes the token payload and sets `request["app"]` (app identity) alongside `request["user"]` (human identity).

**Resource tagging:** `_ChatSlot` gains an `_app` field set from `request["app"]` at creation time. Same pattern for cron jobs, lessons, and MCP servers.

**Isolation enforcement:** Destructive endpoints check `resource._app` against `request["app"]`. If both are non-empty and don't match → 403. If `request["app"]` is empty (dashboard user) → allowed. If `resource._app` is empty (unscoped) and `request["app"]` is non-empty → denied for delete, allowed for send/inject.

**Permission model:** `app.json` declares `permissions.api` (path allowlist) and `permissions.events` (WebSocket event allowlist). Client-side: `AppApiProvider` checks before each request. Server-side (planned): Gateway middleware reads manifest and rejects undeclared paths.

**Audit logging:** All operations logged via SEL with fields: app name, operation, outcome, resources, error.
