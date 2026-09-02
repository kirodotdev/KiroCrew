# App Sandbox & Isolation Roadmap

**Author:** Ray Xu (rayrayxu)
**Date:** 2026-04-23
**Status:** Planned

---

## Problem

A buggy or malicious app with a valid app token can currently:

- Delete all cron jobs (including other apps' and user's)
- Clear the user's lessons
- Register/remove MCP servers
- Read user memory and chat history
- Spawn unlimited subagents, exhausting compute
- Send notifications impersonating the system

The app identity system (App Kit §6) provides authentication but not authorization. We need per-app sandboxing so one app cannot destroy another app's state or degrade the user's Kiro Crew experience.

---

## Design Principle

**Manifest-declared, gateway-enforced.** Apps declare what they need in `app.json` `permissions`. The gateway enforces those declarations at request time using the `app` field in the JWT token. No SDK changes needed — the token already carries identity.

---

## Phases

### Phase 1 — Identity Only (current)

| Resource | Enforcement | Status |
|----------|------------|--------|
| Slots | App can only send/delete/inject into slots it created | ✅ Done (beta) |
| API surface | App token confined to its own namespace (`/apps/<name>/*`, `/api/apps/<name>/*`) + its manifest `permissions.api` allowlist; everything else denied (CWE-269). Enforced centrally in `token_auth_middleware` at every grant point (main flow + loopback/mixed internal branches) so app tokens can't escalate via mixed-internal paths. Reverse proxy re-checks `token.app == <name>`. | ✅ Done |
| All others | No enforcement — any app token can access anything | ⚠️ Partial (api-path done; resource-ownership below still open) |
| Audit | `request["app"]` logged in SEL for all API calls | ✅ Done (beta) |

### Phase 2 — Resource Ownership

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
| App storage | Already directory-isolated (`~/.kirocrew/apps/{name}/data/`). Add token-level check: app token can only access its own `name` in `/api/apps/{name}/config`. |
| Gateway config | Apps cannot modify gateway config (`/api/config/*`). Read-only access to non-sensitive fields only. |

### Phase 4 — Quotas & Rate Limiting

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

Already defined in Mochi's `app.json`:

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
