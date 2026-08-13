# Dashboard Token Authentication — Design Document

## Overview

Token authentication for the Kiro Crew dashboard. The owner mints a time-limited, HMAC-SHA256 signed URL from the CLI (`kirocrew token`) or via the `!dashboard` Slack command (currently the only chat channel that mints links). An aiohttp middleware validates the token on every request (query param or cookie fallback), sets a session cookie on first use, and pins the token to the client's IP. Static assets bypass checks. Loopback access (127.0.0.1) is always trusted regardless of mode — this ensures local processes (mcp-core, doctor, SSH tunnels) work without tokens. All generation and validation events are logged to SEL.

Up to `MAX_CONCURRENT_NONCES` (50) link nonces can be valid concurrently (FIFO eviction via `OrderedDict` when the limit is exceeded), allowing multiple browser tabs and CLI sessions without invalidating each other. All in-memory link-session state is managed by a thread-safe `TokenStateManager`. Auth is **not** purely in-memory: the HMAC signing key is the **persistent** `token_signing.key` (mode `0600`) and revoked access-cookie nonces persist to `token_revoked_nonces.json` (mode `0600`), so signed cookies and per-session logouts both survive a gateway restart. Users can revoke a single session — access cookie **and** its refresh chain — via `POST /api/auth/logout`, or **all** sessions via `kirocrew logout`: the persisted revocation generation (`revocation_gen.py`) is embedded in both access and refresh tokens, and validation of either kind rejects a stale generation, so `kirocrew logout` ends established browser sessions and their refresh chains alike.

The dashboard also issues a paired **refresh cookie** (`mc_refresh_{port}`, HttpOnly, path-restricted to `/api/auth`, up to 30-day TTL) alongside the access cookie on initial token-URL use. The SPA calls `POST /api/auth/refresh` shortly before the access cookie expires to silently rotate both cookies (rotation-on-use), so users only re-run `!dashboard` / `kirocrew token` roughly once per 30 idle days instead of every ~20h. Refresh tokens are HMAC-signed with the same persistent `token_signing.key` and enforce RFC 6819 §5.2.2.3 reuse detection: a consumed `jti` replayed outside a 60s same-IP multi-tab grace window auto-revokes the entire chain.

The refresh scheduler is mounted by `DashboardBootstrap` outside the first-run
Kiro CLI prerequisite gate. A cold browser with a stale access cookie can
therefore rotate its refresh cookie even while the main dashboard tree is not
yet mounted, rather than being trapped behind the setup screen.

The first-run Kiro CLI routes (`GET /api/kiro-prerequisite` and
`POST /api/kiro-prerequisite/repair-specs` — Kiro Crew neither installs the CLI
nor signs in, so there is no install or login route) are deliberately **not**
token-bypass or internal-secret routes. They inherit normal dashboard-user authentication,
Host validation, POST CSRF protection, app-token deny-by-default scoping, and
SEL API auditing. Each handler also rejects every non-empty app claim even if
the app manifest declares this API prefix. The browser's
`X-Session-Key: dashboard:ui` is correlation metadata, not authorization.

### Multi-tab grace window (chain-head-only)

Rotation-on-use races when a refresh POST is duplicated (network retry / double-fire) or two tabs sharing one cookie jar fire near-simultaneously: tab A refreshes `jti1→jti2`, and the just-consumed `jti1` is presented again. To avoid falsely revoking the whole session on this benign single-refresh race, `RefreshStateManager` retains **exactly one** recently-consumed jti per chain — the single most-recently-rotated one (the **chain head**) — together with its freshly-minted replacement pair. `grace_replacement()` accepts a replay **only when the presented jti equals that chain head**, subject to same-source-IP and the 60s window, and re-serves the head's replacement pair (which carries the current live, not-yet-consumed refresh token) instead of minting another rotation. Each consumption overwrites the entry, so the retained pair is always the live head and a slow response can never roll the shared jar back to an already-consumed jti.

**Reuse-detection posture (reviewed, stronger option chosen):** an earlier revision widened this to a bounded history of the last 4 consumed jtis so multiple lagging tabs could each authenticate a same-IP in-window replay. The Design Review and Long-Term Impact reviewers flagged that as a deliberate weakening of the RFC 6819 §5.2.2.3 theft signal (any of the last 4 consumed jtis, replayed same-IP within 60s, resolved to the live head instead of revoking the chain) requiring security sign-off. That widening was **withdrawn**: grace is now chain-head-only, so any **older** rotated jti replayed within the window is treated as token reuse and revokes the chain — an undiluted theft signal. The consciously accepted trade is some multi-tab UX: a *second* stale tab that races a refresh (presenting a jti the active tab already rotated past) may be logged out. Same-IP remains the discriminator and `_client_ip` is `request.remote` (following `X-Forwarded-For` only where the deployment trusts it). The grace entry is in-memory only (never persisted): a gateway restart inside a grace window drops it (a lagging tab then re-mints via the token URL), keeping short-lived live-token material off disk. Any change to the chain-head-only rule, to `_client_ip`/XFF-trust, or to the head-serving behavior changes this security contract and must update this section in the same commit.

### Refresh rate-limit bucket bounding (fail-closed cap)

`POST /api/auth/refresh` is rate-limited per source IP (`_REFRESH_RATE_MAX_CALLS` per `_REFRESH_RATE_WINDOW_SECS`). The per-IP bucket map is bounded two ways: a periodic sweep reclaims stale/empty buckets (the only path by which a live bucket leaves the map — a still-in-window bucket is never evicted), and a hard cap (`_REFRESH_RATE_MAX_BUCKETS`, 4096) that **fails closed** — at capacity a previously-unseen source IP is denied outright rather than admitted by evicting a live bucket. Fail-closed is deliberate: eviction-to-admit was abusable (a saturated attacker's bucket freezes at exhaustion, so it became the eviction victim under an XFF/botnet pump, letting the attacker drop its own exhausted bucket and re-mint a fresh allowance). **Availability consequence:** under a sustained flood — a trusted-`X-Forwarded-For` deployment pumped with distinct spoofed IPs, or heavy organic IP churn — the map can stay pinned at capacity, and while pinned a legitimate previously-unseen source IP is denied refresh, surfacing as an unexplained forced logout. This is an accepted, bounded trade of the fail-closed posture; to keep the window as small as possible the sweep is invoked **unconditionally** (bypassing its interval throttle) whenever an insertion is refused at the cap, reclaiming any dead buckets exactly when it matters — without dropping a still-in-window bucket (a saturated attacker still cannot flood the map to reset its own bucket). Any change to the fail-closed cap, to the eviction/sweep behavior, or to this availability trade changes this security contract and must update this section in the same commit.

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

def bind_token_peer(token: str, peer_key: str) -> None: ...
def check_token_peer(token: str, peer_key: str) -> tuple[bool, str]: ...
    # The session pin is keyed by a PEER KEY: "ip:<addr>" (the default address
    # pin, byte-for-byte the pre-peer behaviour) or "ts:node:<login>|<node>" / "ts:login:<login>" for a
    # daemon-verified tailnet peer (rfc-tailnet-dashboard-access §3). check
    # returns (ok, mismatch_reason); the reason names what the STORED pin was
    # bound to — "IP mismatch" for an ip: pin, "device identity mismatch" for a
    # node-scoped ts: pin, "peer identity mismatch" for a login-scoped one. In
    # the middleware, a ts:-pinned session checked by a request on which NO
    # peer resolved is reported as "tailnet identity unverified" instead —
    # this request could not establish who is behind the proxy, which is not
    # evidence the device changed.
def bind_token_ip(token: str, ip: str) -> None: ...
def check_token_ip(token: str, ip: str) -> bool: ...
    # Thin compat wrappers over the peer functions for plain address pins.

def mark_consumed(token: str) -> None: ...
def is_consumed(token: str) -> bool: ...
def try_consume(token: str) -> bool: ...
    # Atomically check-and-consume (prevents TOCTOU race).
    # NOT WIRED INTO THE MIDDLEWARE LINK PATH — see "Link re-exchange is
    # deliberately allowed" below. These are a working primitive with unit
    # coverage and no production caller; do not assume presenting a link twice
    # is refused because they exist.

def revoke_all_sessions() -> None: ...
    # Clears all nonces, IP bindings, and consumed tokens AND bumps the
    # persisted revocation-generation counter, so every outstanding cookie
    # (for all users) is rejected. Used by `kirocrew logout`. The counter is
    # authoritative over BOTH cookie types: validate_token() AND
    # validate_refresh_token() reject a token whose embedded gen is stale, so
    # the bump ends established access cookies and refresh chains alike.

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
6. On query-param use: mint a SEPARATE session token, bind it to the peer key, add the link token's own nonce to the persisted denylist so the link string can never be presented as a cookie, and set `mc_token_{port}` with `max_age` derived from `session_exp`
7. Log to SEL
8. Return 403 with JSON for `/api/*`, HTML for pages — **except** non-API `GET`/`HEAD` navigations, which are served the public SPA shell (see below)

#### Link re-exchange is deliberately allowed (the link is not single-use)

Presenting the same `?token=` link more than once inside its 5-minute window
**succeeds**, and each presentation re-exchanges for a fresh session cookie bound
to the presenting peer. This is a deliberate design choice, not a missing control:
remote-instance iframes re-derive `/?token=` on navigation, and self-nudge polling
re-opens the same URL, so refusing the second presentation would break both.

What the exchange *does* guarantee is that the link never becomes the long-lived
credential. The cookie is a separate token with its own nonce, and the link's
nonce is added to `RevokedNonceStore` at exchange, so a link captured from Slack,
a log, or browser history cannot be replayed as `mc_token_{port}` on the cookie
path (`use_session_exp=True`, which does consult the denylist). The link path
(`use_session_exp=False`) does not consult it, which is what keeps re-navigation
working.

The residual exposure is therefore bounded by the 5-minute window: an observer who
sees the URL inside that window can exchange it for their own session, pinned to
their own peer. Two consequences follow, and both are accepted:

- The window — not single-use — is the bound on link replay.
- Deployments that need identity as a second factor must put one in front of the
  origin (for example an alias-scoped tunnel allowlist), because the link alone is
  a bearer credential for those 5 minutes.

Making the link single-use is a live option, but it is a **behaviour change with
known breakage** (iframe re-navigation, self-nudge polling) and needs an owner
decision, not a silent tightening. `try_consume` is the primitive it would use.

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

The in-memory `TokenStateManager` (link nonces, IP bindings, consumed set) is cleared on restart, but this does **not** log users out: an established session cookie is validated on the cookie path (`use_session_exp=True`), which needs only a valid HMAC signature (persistent key) + unexpired `session_exp` + a current revocation generation + a nonce not on the persisted denylist — it never consults the in-memory link-nonce set. Revoked-session state is durable: `RevokedNonceStore` persists to `token_revoked_nonces.json` (mode `0600`) and the revocation generation persists to `token_revocation.gen`, so a logged-out cookie stays dead across restarts while a restart alone (generation reloaded unchanged) logs nobody out. Users can revoke a single session via `POST /api/auth/logout` (`revoke_access_cookie()`) or all sessions — access cookies and refresh chains — via `kirocrew logout` (`revoke_all_sessions()`, which bumps the generation both token kinds embed and check).

If `token_revocation.gen` exists but cannot be read as an integer, both token
validators fail closed until the state is repaired. The gateway warning names
the exact file and advises deleting only that file to reset revocation state;
the warning also states the security consequence: resetting the counter can
re-enable unexpired sessions previously revoked by `kirocrew logout`.

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
`~/.kiro/crew/.local_secret` (or the explicit `KIROCREW_HOME`), sets
`app["local_secret"]`, and builds
`app["allowed_origins"]`. `spa_shell_handler=None` because there is no UI — a
request with no token is denied outright. Every in-repo caller (mcp-core, cron)
already sends `X-Internal-Secret`, so the change is purely additive.

The `sel_audit_middleware` **alone is not a security boundary** — it only logs.
Any minimal/alternate server that calls `_register_mcp_routes` MUST mount the
same token-auth chain; otherwise every state-changing MCP route (`/api/spawn`,
`/api/crons`, `/api/lessons`, `/api/send-message`, `/api/workflows/*`,
`/api/taskrunner`) is reachable unauthenticated on loopback (port forwarders and
browser CSRF reach `127.0.0.1`).

#### Unix-socket transport: kernel-attested `X-Session-Key` (POSIX)

TCP loopback + `X-Internal-Secret` authenticates the *installation* (any
same-uid process can read `.local_secret`), but the session identity in
`X-Session-Key` is entirely client-declared — a same-uid process could claim
any session's key. To close that gap, both server entrypoints additionally
bind a `web.UnixSite` on the **same** `AppRunner` at
`dashboard_socket_path(port)` (`~/.kiro/crew/dashboard-<port>.sock`,
port-suffixed so multi-instance homes don't collide; see
`server._start_unix_site`). Windows and any bind failure degrade to TCP-only
— today's behavior — after one log line. The socket file is unlinked
best-effort at shutdown and self-heals from stale files at startup.

For an internal/mixed-internal request arriving on that socket **and carrying
`X-Session-Key`**, `token_auth_middleware` kernel-verifies the claim before
either auth flavor can grant (see `_verify_unix_peer`):

1. `socketsec.check_peer_is_self` — anything but a positive `MATCH` (foreign
   uid, or credentials unreadable) → deny, mirroring gatewayd's
   deny-by-default register policy. On supported POSIX platforms an accepted
   `AF_UNIX` connection always yields peer credentials, so `UNVERIFIABLE`
   means the attestation mechanism itself failed.
2. `socketsec.get_peer_pid` (`SO_PEERCRED` / `LOCAL_PEERPID`) → peer pid.
3. `peer_resolve.resolve_peer_identity(..., signed_only=True)` (the same
   host-namespace /proc ancestry walk gatewayd uses for stub registration,
   offloaded to the subprocess executor) → the session key of the nearest
   ancestor whose `session_pid_<pid>.txt` **HMAC sidecar verifies**. The bare
   `.txt` is same-uid agent-writable and MUST NOT authorize: an unsigned
   mapping counts as unresolvable, so a planted
   `session_pid_<own_pid>.txt` cannot mint a verified identity (the sidecar
   is keyed by the agent-unreadable SEL trust root with the pid bound into
   the MAC).
4. Resolved key **differs** from the declared header → **403** + SEL
   `dashboard.peer-identity-mismatch` (outcome=denied, peer pid recorded).
   Resolved and equal → proceed with `request["peer_verified"] = True`.
   Unresolvable (warm-pool runtime before claim, cron scripts, pooled MCP
   backends — no pidfile in the ancestry; or a mapping published unsigned) →
   proceed under today's semantics.

CSRF interplay: `check_origin`'s no-Origin branch trusts the unix transport
(`origin.request_is_unix_socket`) exactly as it trusts loopback TCP — a
browser cannot connect to the unix socket, so the cookie-attaching
cross-origin threat the CSRF check exists for cannot arrive on it. Without
this, every mutating internal call on the socket would 403 at the CSRF layer
before token auth ran.

The posture is deliberately **verify-when-resolvable / deny-on-mismatch**:
never weaker than the TCP-era check, kernel-verified whenever the gateway's
own registry can attest the peer. Strict fail-closed denial of unresolvable
peers is explicitly out of scope (it would break warm-pool and cron callers).
TCP requests never engage the branch — browser cookies, Windows, and remote
`local_only=False` deployments are untouched.

Client side, `loopback_http.loopback_urlopen` accepts a `unix_socket_path`
and `mcp_core` prefers the socket for every `_API` request when the file
exists (`_api_urlopen`), falling back to TCP **only** when nothing answered
at connect time (`FileNotFoundError` / `ConnectionRefusedError` — cases that
provably never delivered the request, so the retry cannot double-send). HTTP
error statuses and read timeouts propagate unchanged, keeping every caller's
error shape identical.

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
| Link re-presented inside its 5-minute window | 200 | Allowed by design — re-exchanges for a fresh session cookie bound to the presenting peer (see *Link re-exchange is deliberately allowed*) |
| Link string presented as the `mc_token_{port}` cookie | 403 | Rejected: its nonce is on the persisted denylist from the moment of exchange |
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
3. Peer-keyed session pinning on first use — prevents token theft across networks. The pin binds to the client address (`ip:<addr>`), or — when the operator opted into `dashboard.tailscale.trust_identity` and the local daemon verified the forwarded peer — to the tailnet identity (`ts:node:<login>|<node>` or `ts:login:<login>` per `pin_scope`, ACL-tagged nodes always node-scoped). A verified login outside `allowed_logins` is denied outright. Resolution failure is fail-closed on identity and fail-open on availability for NEW sessions: they degrade to the address pin. A session already pinned to a tailnet identity is denied ("tailnet identity unverified") while the daemon cannot answer — never satisfiable by an unverified proxied request — and transient daemon failures (spawn error, timeout) are cached only ~2s so a startup blip clears quickly. Behind a non-Tailscale tunnel the pin binds to the tunnel's loopback address and is therefore shared (reported by Security Posture)
4. Single-use URL consumption — re-click from different client rejected; same client redirected to strip token
5. Dashboard link sent via DM only — never posted in channels
6. Loopback always trusted — local processes (mcp-core, doctor, SSH tunnels) never need tokens
7. CSRF middleware also trusts loopback — local POST requests (mcp-core API calls) bypass origin checks
8. Static assets bypass auth — error pages render correctly
9. Bounded concurrent nonces (max 50; raised from 5 so pending Slack link nonces aren't evicted by other token-minting activity) — prevents unbounded memory growth, limits exposure window; an active session refreshes its eviction position on each check
10. Explicit revocation via `kirocrew logout` — clears all nonces, IP bindings, and consumed tokens, and bumps the persisted revocation generation, ending every outstanding access cookie and refresh chain
11. App-token scope confinement (CWE-269) — an `app`-claim token is confined deny-by-default to its own namespace (`/apps/<name>`, `/api/apps/<name>`) + its manifest `permissions.api` allowlist, enforced at every grant point; no-op for dashboard-user tokens
12. Headless (`--slack-only`) auth parity — `start_api_server()` serves the same MCP route surface as the dashboard and mounts the same `host_validation → csrf → token_auth → sel_audit` chain against the shared `_STRICT_INTERNAL_API_PATHS`/`_MIXED_INTERNAL_API_PATHS` sets. Internal MCP routes require loopback **plus** `X-Internal-Secret` (loopback alone is not sufficient for these paths — port forwarders can spoof `127.0.0.1`); `sel_audit_middleware` alone only logs and is never a substitute for the token-auth chain
