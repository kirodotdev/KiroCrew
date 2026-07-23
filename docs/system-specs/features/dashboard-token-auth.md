# Dashboard Token Authentication — Design Document

Last Updated: 2026-07-13 (doc-sync: note `/api/theme/boot` bypass & static cold-start fallback body)

## Overview

Slack-gated token authentication for the KiroCrew dashboard. The owner generates a time-limited, HMAC-SHA256 signed URL via the `!dashboard` Slack command. An aiohttp middleware validates the token on every request (query param or cookie fallback), sets a session cookie on first use, and pins the token to the client's IP. Static assets bypass checks. Loopback access (127.0.0.1) is always trusted regardless of mode — this ensures local processes (mcp-core, doctor, SSH tunnels) work without tokens. All generation and validation events are logged to SEL.

Up to `MAX_CONCURRENT_NONCES` (50) link nonces can be valid concurrently (FIFO eviction via `OrderedDict` when the limit is exceeded), allowing multiple browser tabs and CLI sessions without invalidating each other. All in-memory link-session state is managed by a thread-safe `TokenStateManager`. Auth is **not** purely in-memory: the HMAC signing key is the **persistent** `token_signing.key` (mode `0600`) and revoked access-cookie nonces persist to `token_revoked_nonces.json` (mode `0600`), so signed cookies and per-session logouts both survive a gateway restart. Users can revoke a single session via `POST /api/auth/logout` or all sessions via `kirocrew logout`.

The dashboard also issues a paired **refresh cookie** (`mc_refresh_{port}`, HttpOnly, path-restricted to `/api/auth`, up to 30-day TTL) alongside the access cookie on initial token-URL use. The SPA calls `POST /api/auth/refresh` shortly before the access cookie expires to silently rotate both cookies (rotation-on-use), so users only re-run `!dashboard` / `kirocrew token` roughly once per 30 idle days instead of every ~20h. Refresh tokens are HMAC-signed with the same persistent `token_signing.key` and enforce RFC 6819 §5.2.2.3 reuse detection: a consumed `jti` replayed outside a 60s same-IP multi-tab grace window auto-revokes the entire chain.

## Architecture

```mermaid
sequenceDiagram
    participant Owner as Slack Owner
    participant Handler as handler.py (!dashboard)
    participant Allowlist as allowlist.py (send_dashboard_link)
    participant TokenGen as token_auth.py
    participant Browser as Browser
    participant MW as Token Auth Middleware
    participant App as Dashboard App

    Owner->>Handler: !dashboard [TTL]
    Handler->>Allowlist: send_dashboard_link(slack, user_id, ttl)
    Allowlist->>TokenGen: generate_token(user_id, session_ttl)
    TokenGen-->>Allowlist: signed token string
    Allowlist-->>Owner: DM with URL containing ?token=...

    Browser->>MW: GET /?token=abc123
    MW->>TokenGen: validate_token("abc123")
    TokenGen-->>MW: valid (user_id)
    MW->>TokenGen: generate_token(user_id, remaining_ttl)  [session exchange]
    TokenGen-->>MW: fresh session token "xyz789" (distinct nonce)
    MW->>MW: bind IP to xyz789, set mc_token_{port}=xyz789 (max_age from session_exp)
    MW->>App: forward request
    App-->>Browser: dashboard page + Set-Cookie (xyz789, NOT the URL token)

    Browser->>MW: GET /api/status (cookie: mc_token_5476=xyz789)
    MW->>TokenGen: validate_token("xyz789", use_session_exp=True)
    TokenGen-->>MW: valid
    MW->>MW: check IP binding
    MW->>App: forward request
```

> **Token→session exchange (CWE-613).** The one-time link token that appears in
> URLs / Slack DMs / terminal history / access logs is NEVER reused as the
> long-lived session cookie. On first (query-param) auth the middleware mints a
> **separate** session token (fresh nonce, same identity, same remaining
> lifetime) and sets *that* as the cookie. Leaking the link therefore exposes
> only its 5-minute click window, not the 20-hour session credential. Combined
> with per-session revocation (`revoke_access_cookie`), an individual leaked
> session can be killed without the global generation bump.

Middleware chain (explicit ordering in `server.py`):

```mermaid
graph LR
    A[host_canonical_redirect] --> B[host_validation] --> C[no_cache] --> D[csrf] --> E[token_auth] --> F[sel_audit] --> G[spa_fallback]
```

1. CSRF checks run first (reject cross-origin mutating requests)
2. Token auth validates identity
3. SEL audit logs the authenticated operation

## Components

### 1. `token_auth.py` — Token Generator, Validator & Middleware

Location: `src/kiro_crew/dashboard/token_auth.py`

#### Token Format

`base64url(payload).base64url(HMAC-SHA256-signature)` where payload is compact JSON:

```json
{"sub":"U1234ABCD","exp":1711000300.0,"session_exp":1711003600.0,"iat":1711000000.0}
```

Two expiry times:
- `exp`: link click window — 5 minutes (`LINK_WINDOW_SECS = 300`). The URL must be opened within this time.
- `session_exp`: cookie session TTL — capped at 20 hours (`MAX_SESSION_TTL_SECS = 20 * 3600`). Once the cookie is set, the access session lasts this long; the refresh cookie (see Cookies) lets the SPA rotate it silently before expiry.

#### Public API

```python
def generate_token(user_id: str, ttl_seconds: int = 3600) -> str: ...
def validate_token(token: str, *, use_session_exp: bool = False) -> tuple[bool, str, str]: ...
    # Returns (valid, user_id, reason)
    # use_session_exp=True for cookie-based access (validates against session_exp)
    # use_session_exp=False for URL click (validates against exp / link window)

def bind_token_ip(token: str, ip: str) -> None: ...
def check_token_ip(token: str, ip: str) -> bool: ...

def mark_consumed(token: str) -> None: ...
def is_consumed(token: str) -> bool: ...
def try_consume(token: str) -> bool: ...
    # Atomically check-and-consume (prevents TOCTOU race)

def revoke_all_sessions() -> None: ...
    # Clears all nonces, IP bindings, and consumed tokens AND bumps the
    # persisted revocation-generation counter, so EVERY outstanding cookie
    # (for all users) is rejected. The nuclear option, used by `kirocrew logout`.

def revoke_access_cookie(token: str) -> bool: ...
    # Per-session revocation (CWE-613). Validates the token, then adds ITS nonce
    # to a persisted server-side denylist (token_revoked_nonces.json, mode 0600).
    # validate_token(use_session_exp=True) rejects any nonce on the denylist with
    # reason "session revoked". Called by POST /api/auth/logout so the caller's
    # own access cookie dies immediately — without the global generation bump
    # that revoke_all_sessions() applies. Returns False (no-op) for a malformed,
    # already-expired, or nonce-less token. Entries auto-evict at session_exp.

def parse_duration(s: str) -> int | None: ...
    # Parses '<int>h' or '<int>m', caps at MAX_SESSION_TTL_SECS (20h)
```

#### Middleware Factory

```python
def token_auth_middleware(local_only: bool = True) -> Callable[..., Any]:
```

The `local_only` parameter is accepted for backward compatibility but no longer controls loopback trust. Loopback requests (127.0.0.1, ::1, localhost) are **always** trusted — this ensures local processes like `mcp-core`, `kirocrew doctor`, and SSH tunnels work without tokens regardless of bind mode.

Request flow:
1. If request is from loopback → pass through (always trusted)
2. Bypass static assets (`/assets/`, `/static/`, `/logo.png`, `/manifest.json`, `/sw.js`, `/icon-*.png`, `/api/token/local`, `/api/shutdown`, `/api/theme/boot` — a GET-only, secret-free theme-boot endpoint the SPA reads before the token flow completes) plus the liveness/readiness probes (`/api/health`, `/api/live`, `/api/ready` — orchestrators and load balancers carry no auth cookie, so probes must be reachable without a token; remote callers receive only liveness/readiness booleans)
3. Extract token from `?token=` query param or `mc_token_{port}` cookie
4. Validate signature + expiry (link window for query param, session_exp for cookie)
5. Check IP binding
6. Check consumption state — if consumed token re-clicked and browser has valid cookie, redirect to strip token from URL
7. On first query-param use: bind IP, mark consumed, set cookie with `max_age` derived from `session_exp`
8. Log to SEL
9. Return 403 with JSON for `/api/*`, HTML for pages — **except** non-API `GET`/`HEAD` navigations, which are served the public SPA shell (see below)

#### Liveness / readiness probes (rec #6)

Three unauthenticated probe endpoints sit on the token-bypass boundary because
orchestrators / load balancers carry no auth cookie. Each returns only fixed,
low-cardinality markers — no paths, ids, counts, secrets, or user/session
content. **Security-boundary contract:** operators who bind the gateway to a
non-loopback interface accept anonymous service-presence and coarse lifecycle
disclosure on these paths. Their bypass membership, exact remote liveness
payload, and readiness 200/503 status plus `ready` boolean are a stable public
contract pinned by
`test_public_probe_contract_frozen_minimal_anonymous_surface_and_statuses`;
changing them requires an explicit public API/security migration. Other
readiness fields are privacy-bounded diagnostics, not frozen contract keys;
they may be added, renamed, or removed as internal checks evolve.

- `GET /api/health` / `GET /api/live` — **liveness**: 200 whenever the process
  can serve HTTP. Anonymous non-loopback callers receive only `{ok: true}`.
  Direct-local callers additionally receive `app` + exact `version` for the
  desktop production/nightly cross-app guard on the shared loopback port. Stays
  200 after shutdown is requested and until the HTTP server exits.
- `GET /api/ready` — **readiness**:
  - *Startup* → connection failure before bind is the external not-ready
    signal. After bind, `DashboardState.ready` remains false and the endpoint
    returns 503 while session restoration, channel relaunch, tunnel setup, and
    other post-bind initialization finish.
  - *Serving* → `DashboardState.ready` is set at the same final boundary as the
    boot-to-ready metric; the endpoint then returns 200 while required state is
    wired and no shutdown has been requested.
  - *Shutdown requested* → 503 with `shutting_down: true` as soon as
    `shutdown_event` is set, while liveness remains 200 until server exit. The
    endpoint does not itself impose or promise a minimum load-balancer drain
    delay.

#### SPA Shell Bypass (cold-start recovery)

The dashboard is a single-page application (SPA): the browser loads one static
HTML shell (`index.html`) once, then client-side JavaScript handles all
navigation.

**Summary:** after the access token expires (e.g. the laptop was off all
weekend), the browser requests `GET /` with no token. Instead of a dead-end
403, the middleware serves the static, secret-free SPA shell so the React app
can boot and silently refresh its own session. Only the shell HTML goes out
unauthenticated — all data stays gated.

**How it works:** any `GET`/`HEAD` request **outside** the excluded data
prefixes (`SPA_FALLBACK_EXCLUDED_PREFIXES`, below) is treated as a client-side
SPA navigation, and the middleware serves the shell **directly** (an injected
`spa_shell_handler`, i.e. `handlers.index`) — it does **not** fall through to
the matched route handler. The booted app then runs its cold-start
`GET /api/auth/me` → `POST /api/auth/refresh` recovery using the 30-day refresh
cookie. Without this, the refresh JS never loads and the app can never recover.
If `index()` cannot read the static bundle, its `FileNotFoundError` fallback
body (`_DASHBOARD_HTML_NOT_FOUND` in `handlers/core.py`) is likewise static and
secret-free, honoring the same unauthenticated cold-start contract.

**One exclusion list, no drift:** `SPA_FALLBACK_EXCLUDED_PREFIXES` in
`token_auth.py` is the single source of truth for "paths that are never the SPA
shell" — `/api/`, `/apps/`, `/v1/` (OpenAI-compat data API), and the static
mounts. Both the auth middleware (this bypass) and `server.py`'s SPA fallback
read the same list, so they cannot diverge. `/apps/` and `/v1/` are matched
routes that never reach the fallback anyway; listing them just makes the auth
gate explicit. `test_no_get_route_outside_shell_exclusions` fails CI if a new
data `GET` route is ever added outside this list.

Security invariants:
- **GET/HEAD only** — no state-changing method ever bypasses auth.
- **Default-deny** — the bypass fires only when a `spa_shell_handler` is wired
  AND the path is a shell navigation; it serves the shell **directly**, so an
  unauthenticated request never reaches any registered route's handler. If the
  handler is not configured, shell requests are denied like any other.
- **Shell only, never data** — `/api/*`, the `/apps/{name}/api/*` reverse
  proxy, and `/v1/*` (OpenAI-compat data API) still require a valid token; the
  shell carries no secrets.
- **Mint preserved** — a valid `?token=` is *not* short-circuited; it flows
  through the normal validate-and-mint exchange (steps 4–7).

#### State: in-memory vs. persisted

The **HMAC signing key** is loaded from (or created at) `<config_dir>/token_signing.key` (mode `0600`) by `token_secret.py` — it is **persistent**, not `os.urandom(32)` per process (that is only a can't-persist fallback). Signed access and refresh cookies therefore survive a gateway restart.

Mutable link-session state is encapsulated in `TokenStateManager`, a thread-safe singleton using `threading.Lock` (not `asyncio.Lock`, since token operations are called from both async middleware and sync CLI contexts):

```python
_SECRET: bytes                             # persistent HMAC key (token_secret.py)
_state: TokenStateManager                  # Singleton instance

class TokenStateManager:
    _nonces: OrderedDict[str, float]       # link nonce -> expiry (FIFO, max 50)
    _ip_bindings: dict[str, tuple[str, float]]  # token -> (ip, exp)
    _consumed: dict[str, float]            # token -> exp
```

Up to `MAX_CONCURRENT_NONCES` (**50**) link nonces are valid simultaneously. When the limit is exceeded, the oldest nonce is evicted via `OrderedDict.popitem(last=False)` (O(1)); a successful nonce check also refreshes a nonce's eviction position so an actively-used session isn't evicted by newer grants. The limit was **raised from 5 to 50** specifically so pending Slack link nonces aren't evicted by other token-minting activity (crons, dashboard links, etc.). This allows multiple browser tabs and `kirocrew token` invocations without invalidating prior sessions.

The in-memory `TokenStateManager` (link nonces, IP bindings, consumed set) is cleared on restart, but this does **not** log users out: an established session cookie is validated on the cookie path (`use_session_exp=True`), which needs only a valid HMAC signature (persistent key) + unexpired `session_exp` + a nonce not on the persisted denylist — it never consults the in-memory link-nonce set. Revoked-session state is durable: `RevokedNonceStore` persists to `token_revoked_nonces.json` (mode `0600`), so a logged-out cookie stays dead across restarts. Users can revoke a single session via `POST /api/auth/logout` (`revoke_access_cookie()`) or all in-memory sessions via `kirocrew logout` (`revoke_all_sessions()`).

#### App-token scope confinement (CWE-269)

An **app token** (payload carries a non-empty `app` claim, minted by the `X-App-Secret` exchange at `POST /api/apps/<name>/token`) must not have the same reach as a dashboard-user token. `_enforce_app_scope(request, app_name, path)` applies least privilege, **deny-by-default**:

- An app token may access **only** (1) its **own namespace** — `/apps/<name>/...` and `/api/apps/<name>/...`, matched on a path boundary so app `foo` cannot reach app `foo-bar` — and (2) the API path prefixes the app declared in its manifest `permissions.api` allowlist (`_app_api_allowlist`, cached ~30s; any load failure returns an empty tuple → confined to its own namespace only). Everything else is a 403 with an `app_scope_check` SEL audit event.
- It is enforced in **every** middleware branch that admits a token (the normal cookie/query-param flow and the cross-app `/apps/<other>/api` reverse-proxy re-check) — otherwise an app token could reach a mixed internal path (e.g. `/api/chat`, `/api/spawn`) with no app identity set and be mistaken for the dashboard user (privilege escalation).
- It is a **no-op for dashboard-user tokens** (empty `app` claim), which bypass the gate entirely.

### 2. `origin.py` — Dashboard URL & Bind Address Resolution

Location: `src/kiro_crew/dashboard/origin.py`

Centralizes dashboard URL parsing, bind-address resolution, origin-set construction, and per-request origin validation. Shared by `server.py`, `ws.py`, `gateway.py`, and `allowlist.py`.

Key functions:

```python
def parse_dashboard_url(url: str) -> tuple[str, int]: ...
    # Parses 'dashboard.url' config into (hostname, port)
    # KIROCREW_PORT env var always overrides port

def is_local_only(dashboard_host: str, slack_connected: bool) -> bool: ...
    # Determines bind address and CSRF origins (NOT token auth — loopback always trusted)
    # True when: no Slack, loopback host, or localhost machine → bind 127.0.0.1
    # False when: non-loopback host configured with Slack → bind 0.0.0.0

def bind_address_for(local_only: bool) -> str: ...
    # "127.0.0.1" if local_only, "0.0.0.0" otherwise

def resolve_dashboard_host(local_only: bool, configured_host: str = "") -> str: ...
    # Returns hostname for URL construction
    # Returns kirocrew.localhost directly for local-only mode (RFC 6761)

def build_allowed_origins(port: int, local_only: bool, configured_host: str = "") -> set[str]: ...
    # CSRF origin allowed list
```

### 3. `!dashboard` Command Handler

Location: `src/kiro_crew/slack/handler.py` → `_handle_slash_command`

Parses `!dashboard [duration]`, delegates to `allowlist.send_dashboard_link()`:

```python
if cmd == "!dashboard":
    parts = cmd_text.split()
    ttl = 3600
    if len(parts) >= 2:
        parsed = parse_duration(parts[1])
        if parsed is None:
            # reply with usage message
        ttl = parsed
    url = await send_dashboard_link(slack, user_id, ttl)
```

### 4. `send_dashboard_link()` — Token URL Generation & DM Delivery

Location: `src/kiro_crew/slack/allowlist.py`

Generates the token, constructs the URL using `origin.py` helpers, and DMs it to the owner (never posted in channels to prevent token leakage):

```python
async def send_dashboard_link(slack, user_id, ttl=3600) -> str:
    session_ttl = min(ttl, MAX_SESSION_TTL_SECS)
    cfg = KiroCrewConfig.load()
    configured_host, port = parse_dashboard_url(cfg.dashboard_url)
    local_only = is_local_only(configured_host, True)
    host = resolve_dashboard_host(local_only, configured_host)
    token = generate_token(user_id, session_ttl)
    url = f"http://{host}:{port}/?token={token}"
    # DM to user with click window + session duration info
    # Log to SEL: operation="slack.dashboard_token", outcome="ok"
    return url
```

### 5. `server.py` Integration

`start_dashboard()` accepts `local_only: bool` and `configured_host: str`, wires the middleware:

```python
app.middlewares[:] = [
    host_canonical_redirect,
    host_validation_middleware,
    no_cache_middleware,
    csrf_middleware,
    token_auth_middleware(local_only=local_only),
    sel_audit_middleware,
    spa_fallback,
]
site = web.TCPSite(runner, bind_address_for(local_only), port)
```

The two internal-path sets passed to `token_auth_middleware` are module-level
constants — `_STRICT_INTERNAL_API_PATHS` and `_MIXED_INTERNAL_API_PATHS` — so
the headless server (below) binds to the **same** sets and the two entrypoints
cannot drift.

#### `start_api_server()` — headless (`--slack-only`) parity

The `--slack-only` gateway starts `start_api_server()` instead of
`start_dashboard()`. It serves the **same** MCP tool route surface
(`_register_mcp_routes`), so it mounts an auth chain at parity:
`host_validation_middleware → csrf_middleware → token_auth_middleware(
internal_paths=_STRICT_INTERNAL_API_PATHS,
mixed_internal_paths=_MIXED_INTERNAL_API_PATHS, spa_shell_handler=None) →
sel_audit_middleware`. It generates and persists the same
`~/.kirocrew/.local_secret`, sets `app["local_secret"]`, and builds
`app["allowed_origins"]`. `spa_shell_handler=None` because there is no UI — a
request with no token is denied outright. Every in-repo caller (mcp-core, cron)
already sends `X-Internal-Secret`, so the change is purely additive.

The `sel_audit_middleware` **alone is not a security boundary** — it only logs.
Any minimal/alternate server that calls `_register_mcp_routes` MUST mount the
same token-auth chain; otherwise every state-changing MCP route (`/api/spawn`,
`/api/crons`, `/api/lessons`, `/api/send-message`, `/api/workflows/*`,
`/api/taskrunner`) is reachable unauthenticated on loopback (port forwarders and
browser CSRF reach `127.0.0.1`).

### 6. `gateway.py` Integration

`_init_dashboard()` resolves config and passes to `start_dashboard()`:

```python
configured_host, dashboard_port = parse_dashboard_url(self._cfg.dashboard_url)
self._local_only = is_local_only(configured_host, self._slack_enabled)
await start_dashboard(
    ...,
    slack_connected=self._slack_enabled,
    local_only=self._local_only,
    configured_host=configured_host,
)
```

`_init_api_server()` (the `--slack-only` / `--no-dashboard` path) resolves the
same `configured_host`/`local_only` and forwards them to `start_api_server()`,
so the headless server's CSRF origin allowlist and Host allowlist match the
dashboard's:

```python
configured_host, dashboard_port = parse_dashboard_url(self._cfg.dashboard.url)
self._local_only = is_local_only(configured_host, self._slack_enabled)
await start_api_server(
    ...,
    local_only=self._local_only,
    configured_host=configured_host,
)
```

## Configuration

Single `dashboard.url` field on `KiroCrewConfig` (default: `""`), loaded from `config.json → dashboard.url`.

```json
{
  "dashboard": {
    "url": "http://my-host.example.com:8080"
  }
}
```

`is_local_only()` determines the bind address and CSRF origins (not token auth):
- No Slack → local-only (bind 127.0.0.1, no remote access)
- Loopback host → local-only
- Non-loopback host → all interfaces (`0.0.0.0`), token auth required for non-loopback clients
- No URL + remote machine + Slack → all interfaces
- No URL + localhost machine → local-only

Note: Loopback access (127.0.0.1) is always trusted for both token auth and CSRF, regardless of `is_local_only`. This ensures `mcp-core`, `kirocrew doctor`, and SSH tunnels always work.

`KIROCREW_PORT` env var overrides the port (dev mode).

## Cookies

### Access cookie
- Name: `mc_token_{port}` (e.g. `mc_token_5476`)
- Value: the full access token string
- Attributes: `HttpOnly`, `SameSite=Lax`, `Path=/`
- `Secure`: set when `is_https_request(request)` is true — i.e. `request.scheme == "https"` **OR** an `X-Forwarded-Proto: https` header from a **loopback** peer (a TLS-terminating tunnel/reverse proxy that forwards plain HTTP to the loopback-bound gateway). Restricting the header to a loopback peer means a remote attacker can't forge it. Localhost plain HTTP must NOT set `Secure` or the browser refuses to send the cookie back (and the `wss://` dashboard WebSocket would flap online/offline)
- `max_age`: remaining seconds from `session_exp` (capped at `MAX_SESSION_TTL_SECS`, 20 hours)

### Refresh cookie
- Name: `mc_refresh_{port}` (e.g. `mc_refresh_5476`)
- Value: the refresh token string
- Attributes: `HttpOnly`, `SameSite=Lax`, `Path=/api/auth` (sent to both `/api/auth/refresh` and `/api/auth/logout`)
- `Secure`: conditional on `is_https_request(request)`, same rule as the access cookie
- `max_age`: remaining seconds from the refresh `session_exp` (capped at `MAX_REFRESH_TTL_SECS`, 30 days)

## Error Handling

| Scenario | HTTP Status | Response Format |
|----------|-------------|-----------------|
| No token (query or cookie) | 403 | JSON for `/api/*`, HTML for pages |
| Expired token (link window or session) | 403 | JSON for `/api/*`, HTML for pages |
| Invalid HMAC signature | 403 | JSON for `/api/*`, HTML for pages |
| IP mismatch | 403 | JSON for `/api/*`, HTML + SEL log |
| Consumed token re-click (browser has cookie) | 302 | Redirect to strip token from URL |
| Consumed token from different client | 403 | JSON for `/api/*`, HTML for pages |
| Malformed token (can't decode) | 403 | JSON for `/api/*`, HTML for pages |
| Invalid duration in `!dashboard` | N/A | Slack usage message |

HTML 403 page includes instructions to run `!dashboard` in Slack. The middleware never raises unhandled exceptions.

> **Note:** the *No token* / *Expired token* / *Invalid HMAC signature* rows above apply to `/api/*`, `/apps/*`, and non-`GET`/`HEAD` requests. A non-API `GET`/`HEAD` navigation in those same states is instead served the public SPA shell (200) so the app can cold-start its refresh flow — see *SPA Shell Bypass (cold-start recovery)*. `IP mismatch` is **not** relaxed: it remains a hard 403 (theft signal).

## SEL Audit Events

| Event | Operation | Outcome | Metadata |
|-------|-----------|---------|----------|
| Token generated | `slack.dashboard_token` | `ok` | `ttl=<seconds>` |
| Request accepted | `dashboard.token_auth` | `ok` | request path |
| Request denied | `dashboard.token_auth` | `denied` | rejection reason |
| SPA shell served on cold-start nav | `dashboard.token_auth` | `shell_unauth` (no token) / `shell_unauth_invalid_token` (expired/forged token) | request path. **These replace `denied`/403 for non-API `GET`/`HEAD` navigations** — any volume-based scanning/brute-force alert keyed on `denied` or 403 counts for nav paths MUST also watch these two outcomes, or credential-less probing of a remote-exposed dashboard goes invisible. A forged token on a nav serves the secret-free shell but keeps the distinct `shell_unauth_invalid_token` signal (not `ok`). |

## Security Properties

1. Per-process HMAC secret (`os.urandom(32)`) — process restart invalidates all tokens
2. Dual expiry: 5-minute link click window + configurable session TTL (max 20h)
3. IP pinning on first use — prevents token theft across networks
4. Single-use URL consumption — re-click from different client rejected; same client redirected to strip token
5. Dashboard link sent via DM only — never posted in channels
6. Loopback always trusted — local processes (mcp-core, doctor, SSH tunnels) never need tokens
7. CSRF middleware also trusts loopback — local POST requests (mcp-core API calls) bypass origin checks
8. Static assets bypass auth — error pages render correctly
9. Bounded concurrent nonces (max 50; raised from 5 so pending Slack link nonces aren't evicted by other token-minting activity) — prevents unbounded memory growth, limits exposure window; an active session refreshes its eviction position on each check
10. Explicit revocation via `kirocrew logout` — clears all nonces, IP bindings, and consumed tokens
11. App-token scope confinement (CWE-269) — an `app`-claim token is confined deny-by-default to its own namespace (`/apps/<name>`, `/api/apps/<name>`) + its manifest `permissions.api` allowlist, enforced at every grant point; no-op for dashboard-user tokens
12. Headless (`--slack-only`) auth parity — `start_api_server()` serves the same MCP route surface as the dashboard and mounts the same `host_validation → csrf → token_auth → sel_audit` chain against the shared `_STRICT_INTERNAL_API_PATHS`/`_MIXED_INTERNAL_API_PATHS` sets. Internal MCP routes require loopback **plus** `X-Internal-Secret` (loopback alone is not sufficient for these paths — port forwarders can spoof `127.0.0.1`); `sel_audit_middleware` alone only logs and is never a substitute for the token-auth chain
