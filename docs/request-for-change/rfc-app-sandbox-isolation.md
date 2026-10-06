---
title: App Sandbox and Isolation Roadmap
status: partial
kind: framework
author: Ray Xu (rayrayxu)
created: 2026-04-23
last-audited: 2026-10-05
audited-at: e281ecaf33
doc-pr:
implementation-prs: []
tracking-issues: []
supersedes: []
superseded-by: []
---

# App Sandbox & Isolation Roadmap

Status: partial. App tokens are confined to their own namespace plus the
manifest `permissions.api` allowlist, and slot and WebSocket visibility are
app-scoped. Process isolation, broad resource ownership, and the quota phases
remain incomplete.

A roadmap rather than a single reviewable change: it inventories what an app token
can reach today and stages the isolation work, so each stage is proposed and
approved on its own.

---

## Problem

An app token is deny-by-default: it reaches only its own namespace plus the
paths its manifest lists in `permissions.api`, and WebSocket events and chat
slots are app-scoped. The residual risk is inside what an app is granted:

- A granted path has no per-resource owner filter. An app granted `/api/crons*`
  can list, update, pause or remove every cron job, including other apps' and
  the user's; the same holds for any other granted resource route.
- `permissions.mcpTools` is declared and shown at install, but nothing enforces
  it at runtime.
- No quota bounds what a granted app may create (slots, crons, subagents,
  storage, request rate).

We need per-app resource ownership and quotas so one app cannot destroy another
app's state or degrade the user's Kiro Crew experience.

---

## Design Principle

**Manifest-declared, gateway-enforced.** Apps declare what they need in `app.json` `permissions`. The gateway enforces those declarations at request time using the `app` field in the JWT token. No SDK changes needed — the token already carries identity.

---

## Phases

### Phase 1 — Identity Only (current)

| Resource | Enforcement | Status |
|----------|------------|--------|
| Slots | App can only send/delete/inject into slots it created | Shipped |
| API surface | App token confined to its own namespace (`/apps/<name>/*`, `/api/apps/<name>/*`) + its manifest `permissions.api` allowlist; everything else denied (CWE-269). Enforced centrally in `token_auth_middleware` at every grant point (main flow + loopback/mixed internal branches) so app tokens can't escalate via mixed-internal paths. Reverse proxy re-checks `token.app == <name>`. | Shipped |
| WebSocket events | App token on `/api/ws` receives only its own slots' events and the global events listed in `permissions.events` (`dashboard/ws_event_scope.py`) | Shipped |
| Granted resources | Residual risk: a granted path (for example `/api/crons*`) has no per-resource owner filter, and `permissions.mcpTools`, `cron` and `network` are stored and shown at install with no runtime gate | Open (Phase 2) |
| Audit | `request["app"]` logged in SEL for all API calls | Shipped |

### Phase 2 — Resource Ownership

Not implemented: no cron, subagent or notification record carries `owner_app`.

Each mutable resource gets an `owner_app` field. Apps can only modify resources they own. Dashboard users (no app identity) can access everything.

| Resource | Enforcement Rule |
|----------|-----------------|
| Cron jobs | `cron.owner_app` set on create. App can only list/update/pause/remove its own crons. |
| Subagents | `subagent.owner_app` set on spawn. App can only list/status its own subagents. |
| Notifications | App can only ack notifications addressed to it (via `target_app` field). |
| MCP servers | App can only register/remove servers declared in its manifest `mcpServers`. |

**Gateway changes:** Add `owner_app` to cron store, subagent state, notification records. Add ownership check in each handler.

### Phase 3 — Data Isolation

| Resource | Enforcement Rule |
|----------|-----------------|
| Lessons | App lessons stored in `app:{name}:` namespace. App cannot read/write global lessons. Global lessons remain read-only for apps. |
| Memory | App can only search memory from its own slots. Memory consolidation scoped to app's sessions. |
| Chat history | App can only read history of its own slots. |
| App storage | Already directory-isolated (`~/.kiro/crew/apps/{name}/data/`). Add token-level check: app token can only access its own `name` in `/api/apps/{name}/config`. |
| Gateway config | Apps cannot modify gateway config (`/api/config/*`). Read-only access to non-sensitive fields only. |

### Phase 4 — Quotas & Rate Limiting

Not implemented: no quota key is read anywhere in the gateway.

Manifest declares resource tier. Gateway enforces limits.

```json
{
  "permissions": {
    "quotas": {
      "maxSlots": 3,
      "maxCrons": 5,
      "maxSubagents": 2,
      "maxStorageMB": 50,
      "apiRateLimit": 60
    }
  }
}
```

| Limit | Default | Enforcement |
|-------|---------|-------------|
| Slots per app | 3 | 429 on `createSlot` when limit reached |
| Crons per app | 5 | 429 on `addCron` when limit reached |
| Concurrent subagents | 2 | 429 on `spawn` when limit reached |
| Storage per app | 50 MB | 413 on write when quota exceeded |
| API calls per minute | 60 | 429 with `Retry-After` header |
| Message length | 100 KB | Already enforced in SDK |

---

## Implementation Notes

### Token Structure

App tokens already contain `"app": "mochi-pet"` in the HMAC payload. No token format changes needed.

### Backward Compatibility

- Dashboard users (tokens without `app` field) bypass all app restrictions — full access as today.
- Apps using legacy auth (`kirocrewSecret.ts` headers, no app identity) are treated as dashboard users — no restrictions. This is the correct fallback for old gateways.
- Phase 2+ enforcement is opt-in per gateway version. Old gateways ignore the `app` field.

### SDK Impact

None. The SDK already sends the `app` field in the token. All enforcement is gateway-side. Apps don't need to change code when enforcement is tightened.

### Manifest `permissions` Field

`docs/app-kit/manifest-reference.md` owns the full permissions list. Of these
keys, `api` and `events` are enforced at runtime; `mcpTools`, `cron` and
`network` are stored and shown at install with no runtime gate. Mochi's
`app.json` declares:

```json
{
  "permissions": {
    "api": ["/api/chat/*", "/api/notifications", "/api/approvals/*", "/api/status"],
    "events": ["chat_chunk", "chat_done", "notification", "approval"],
    "mcpTools": ["notify_user", "get_daily_briefing", "capture_screen_region"],
    "storage": true,
    "cron": false,
    "network": false
  }
}
```

Phase 2+ will enforce `api` paths (allowlist), `events` (WS filter), `mcpTools` (tool registration scope), and `cron`/`network` flags.

---

## Migration Path for Existing Apps

1. Apps already have `permissions` in manifest — no manifest changes needed
2. Gateway rolls out enforcement per-phase with feature flags
3. Apps that exceed their declared permissions get 403 with clear error message
4. Dashboard shows per-app permission audit in App Store detail page
