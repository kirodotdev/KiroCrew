# App Platform Trust Model

Kiro Crew's app platform loads app Python directly into the gateway process
(`apps/module_loader.py` → `importlib` → `exec_module`). This page documents the
resulting trust boundary and how Kiro Crew makes it explicit.

## What an app can do

When you **enable** an app, its backend hooks, route handlers, and lifecycle
scripts run **in-process with full gateway privileges**:

- Arbitrary `import`, filesystem, and network access
- Access to anything in the gateway process's memory (including resolved credentials)
- Manifest `setup` lifecycle scripts run via `/bin/bash -c` (OS-sandbox-wrapped, but
  the script body comes from the app's `app.json`)

The app **permission system** (`permissions.py`, `context.py`, `app.json`
`permissions.mcpTools`) gates only the **SDK tool surface** handed to the app
context. It does **not** restrict imports, filesystem, network, or subprocess use
by the loaded module. There is currently **no process-level sandbox** around app
code itself.

> **Installing/enabling an app is therefore equivalent to running that code with
> the same privileges as Kiro Crew itself.** Only enable apps you trust.

## How Kiro Crew makes the boundary explicit

- **Builtin vs third-party split** — apps shipped inside the package
  (`apps/builtins/`) are trusted like core. Anything loaded from outside that
  directory is treated as third-party.
- **One-time SECURITY warning** — the first time a third-party app's Python is
  executed, `module_loader` logs a loud warning naming the app and the privilege it
  receives.
- **SEL audit** — every module load is recorded in the Security Event Log with its
  trust class (`builtin` / `third_party`), so app-code execution is auditable.
- **Hard off switch** — set `agent.apps_allow_third_party=false` (in
  `~/.kiro/crew/config.json` or the config modal) to refuse running any app whose
  Python lives outside `apps/builtins/`. Both app-**Python** execution paths
  consult the switch: `module_loader` raises `ImportError` before `exec_module`
  (in-process hooks), and `backend._start_app_backend_body` returns `None` before
  `Popen` (the out-of-process app backend), each recording a `denied` SEL entry —
  so untrusted app **Python** never runs, in-process or out. Defaults to `true`
  (apps are operator-installed).

  > **Scope (known gap):** this switch gates only app **Python** (the in-process
  > module loads and the out-of-process backend). It does **not** gate
  > app-authored lifecycle *shell* scripts (`setup.onInstall` / `onEnable` /
  > etc., run via `_run_lifecycle_script` → `/bin/bash -c`). Those still run when
  > the switch is off; they are gated instead by the admission policy
  > (`apps/admission.py`) and OS-sandbox wrapping, not by
  > `apps_allow_third_party`. Disabling the switch is therefore not a substitute
  > for not installing an untrusted app.

### App-token scope confinement (CWE-269)

App tokens (minted via the `X-App-Secret` exchange at `POST /api/apps/<name>/token`)
are **deny-by-default** confined by the dashboard auth middleware
(`token_auth.py` `_enforce_app_scope` / `app_token_path_allowed` / `_app_owns_path` /
`_app_api_allowlist`) to the app's own namespace (`/apps/<name>/*` and
`/api/apps/<name>/*`) plus the API path prefixes the app declares in its manifest
`permissions.api` allowlist. Every other path returns `403`, and the
`/apps/<name>/api` reverse proxy (`apps/routes.py` `handle_app_api_proxy`)
independently re-checks that the caller's token app matches the target app, since
the proxy signs requests with the target app's secret.

### WebSocket event scope (CWE-269)

`/api/ws` is a *third* surface reachable with the same app token, and it is scoped
separately: connecting no longer grants the full event stream. On connect the socket
records the caller's app identity and its manifest `permissions.events` declarations
(`dashboard/ws.py`), and every fan-out is filtered per socket at a single chokepoint
(`DashboardState._send_ws_all` → `_ws_client_allowed` → `dashboard/ws_event_scope.py`).
Both dispatch paths — `broadcast_ws()` and the `_broadcast()` `_type` translation —
funnel through it, as does the subagent-subscriber fan-out.

Events fall into three tiers. Tier 0 (`dashboard`, `refresh`, `update_progress`) carries
no sensitive payload and is always delivered. Tier 1 is slot-scoped: delivery depends on
the slot's `SlotOrigin` and the app's `slots:*` declarations (`slots:own` is the default,
then `slots:user`, `slots:app:<name>`, `slots:all`); `subagent:*` is an independent
dimension so an app can watch subagent status without receiving chat content. Tier 2 is
global and requires an explicit declaration; notifications split further by source, so
`notification` covers the app's own pushes while gateway-internal ones (cron output,
`send_message`, watchlist results) need `notification:system` — bundling them would make
one declaration a broad grant. Cross-app visibility (`slots:app:X`) also
requires the *observed* app to opt in via `permissions.exposeToApps`, so an app cannot
name a sibling unilaterally.

Narrowing `permissions.events` takes effect on sockets that are ALREADY open: each
decision intersects the connect-time set with what the manifest declares now, so a
revoked scope stops being honoured within about half a minute and no reconnect is
needed. Disabling or uninstalling the app declares nothing AND marks it revoked,
which collapses its open sockets to the always-delivered tier — including the log
stream, which is re-checked per send rather than only at subscribe. The revoked
mark is needed on top of the empty declaration set because an app sees its OWN
slots by default, without declaring anything, so emptying the declarations alone
would leave a disabled app's chat streaming. A disabled app that RECONNECTS is
refused the socket outright, since disabling does not invalidate its token and the
initial slot list would otherwise be served from the still-intact manifest.
Widening does not work
that way — a new scope reaches the app only on its next connection, so an edit can
never hand a live session more than it opened with.

Filtering a frame's payload is not always enough: the `slots` re-push is a full slot list,so it is re-filtered per app on the send path (`DashboardState._serialize_for_client`) —
but its *envelope* also carries global safety-posture booleans that no slot scope narrows.
`yolo` (is the blanket approval override active) is therefore gated by the same `yolo`
declaration that gates the `yolo_expired` event, and `channelTrusted` is withheld from app
tokens outright — no scope declares it and no app SDK consumer reads it. A withheld field
is *omitted*, never sent as `false`: a falsy default still answers a question the app must
not be able to ask, and answers it wrongly whenever the override is on.

A slot's `SlotOrigin` is declared by the layer that actually knows it, and an undeclared
slot stays untagged. `get_or_create_slot` cannot tell a person typing in the dashboard
from a background injection, so it does not guess: the request layer decides `USER` vs
`APP` from whether a token was presented (`request_slot_origin`), cron declares `CRON`,
and rehydration restores the persisted value rather than re-deriving it. An untagged slot
matches no cross-slot scope, so a caller that forgets to declare loses visibility instead
of leaking — inferring `USER` there put cron output inside `slots:user`.

`permissions.events: ["*"]` predates this vocabulary and still means every event. It is
expanded into the full scope set when the socket's allow-set is built, not carried through
as a literal, because as an opaque member no gate would recognise it and such a manifest
would keep its subscription while receiving nothing.

All other scopes are **self-declared**: they are read from the app's own manifest, with
no install-time approval check. That is consistent with the privilege an installed app
already holds (see above), so the tiers structure and audit what an app receives rather
than defending against a hostile manifest. `exposeToApps` is the one asymmetric case,
because there the manifest being trusted is not the one being widened.

Two payloads need more than a yes/no gate. The `slots` re-push carries every slot, so it
is re-filtered per app in `_serialize_for_client` (failing closed to an empty list); and
the log ring-buffer replay plus the subagent reconnect replay write to the socket
directly, so `ws.py` gates those at the source.

Dashboard-user sockets are exempt, identified by a **positive** `is_dashboard_user`
claim set by the auth middleware — never by the absence of an app claim, which would
fail *open* on any path that forgot to set it. Because the stream is now filtered,
`/api/ws` is implicitly allowed for app tokens (`_APP_TOKEN_IMPLICIT_ALLOW`) rather
than requiring every app to declare the transport; that grant is recorded in the
Security Event Log. `/api/status` is **not** implicitly allowed — it has no
response-level filter to match what event scoping gives `/api/ws`, and it returns
the owner id hash, host specs, cron and usage stats, and the live safety-override
state. An app that needs it declares it in `permissions.api` like any other
capability.

### The frontend `useAppApi` / `useAppEvents` scoping is a guardrail, not a boundary

An app's UI runs **in the dashboard page, with the dashboard user's own authority**. The
SDK's `createScopedApi()` checks the `permissions.api` allowlist *in that same page*
before issuing a same-origin `fetch`, so the request reaches the gateway as a
dashboard-user token (empty app claim) — which `_enforce_app_scope` deliberately never
gates. An embedded app could call `fetch('/api/anything')` directly and succeed. This is
inherent to embedding app UI in the dashboard page, not a defect, but it means the
frontend scoping prevents *accidental* use rather than abuse. The enforceable boundaries
are the app **token** ones above (HTTP paths and WS events), which apply to app-owned
*processes* holding their own credential.

This is an **HTTP-reach boundary distinct from the in-process module-loading
privilege**: an app's loaded Python still runs with full gateway privileges (the
warning above stands), but an app's own HTTP token can no longer reach arbitrary
gateway or sibling-app endpoints. Dashboard-user tokens (empty app claim) are never
subject to this gate.

## Future work

True isolation (running app code in a separate sandboxed subprocess rather than
in-process) is intentionally **out of scope** for now — the open-source app
registry ships empty and all installs are operator-consented. Process isolation
is tracked as a separate design to be revisited if/when a public app store lands.
Until then, operators who install no apps (or run untrusted ones) can set
`agent.apps_allow_third_party=false` to block third-party execution entirely —
both in-process module loads and out-of-process backend spawns. (Corresponds to
CSE finding SEC-012.)
