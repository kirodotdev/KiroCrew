# App Platform Trust Model

Kiro Crew's app platform loads app Python directly into the gateway process
(`apps/module_loader.py` → `importlib` → `exec_module`). This page documents the
resulting trust boundary and how Kiro Crew makes it explicit.

## What an app can do

When an app's executable surfaces are **admitted** and enabled, its in-process backend hooks and route handlers run **with full gateway privileges**; lifecycle scripts are separately OS-sandbox-wrapped but still execute app-authored shell commands:

- Arbitrary `import`, filesystem, and network access
- Access to anything in the gateway process's memory (including resolved credentials)
- Manifest `setup` lifecycle scripts run via `/bin/bash -c` (OS-sandbox-wrapped, but
  the script body comes from the app's `app.json`)

The `AppContext` capability permissions (`permissions.cron`,
`permissions.events`, `permissions.spawn`, `permissions.storage`, and
`permissions.jobs`) gate only which SDK objects `context.py` populates.
`permissions.mcpTools` is validated and displayed by `permissions.py`, but it
currently has no runtime call site and must not be treated as an execution
boundary. Neither mechanism restricts imports, filesystem, network, or
subprocess use by the loaded module. There is currently **no process-level
sandbox** around app code itself.

> **Admitting and enabling an app is therefore equivalent to running that code with the same privileges as Kiro Crew itself.** Only trust apps you trust.

## How Kiro Crew makes the boundary explicit

- **Builtin vs third-party split** — apps shipped inside the package
  (`apps/builtins/`) are trusted like core. Anything loaded from outside that
  directory is treated as third-party.
- **One-time SECURITY warning** — the first time a third-party app's Python is
  executed, `module_loader` logs a loud warning naming the app and the privilege it
  receives.
- **SEL audit** — every module load is recorded in the Security Event Log with its
  trust class (`builtin` / `third_party`), so app-code execution is auditable.
- **Execution admission defaults to deny** — `agent.apps_allow_third_party` defaults to `false`. A non-builtin app needs either an explicit per-app `agent.apps_trusted` grant (with its repository binding, where applicable) or the broad `apps_allow_third_party=true` grant. `app_execution_denied` is consulted before in-process module loading, backend spawning, enable-time side effects, and manifest shell lifecycle commands; allowed and denied decisions are SEL-audited. Builtin status is accepted only when the registered app name and resolved path prove shipped provenance.
- **Turning admission off REVOKES, it does not merely stop admitting** — the app's
  tracked BACKEND PROCESS is stopped, so the setting is never a label that changes
  nothing until the next restart. Three paths enforce it, and they exist because
  the setting has three writers:
  - `PUT /api/security/trusted-apps/allow-all` sweeps on the falling edge before
    persisting `false`, so each app's `on_shutdown` hook can still load, then
    sweeps a second time after the write to catch an app enabled during the
    window. It reports what it could not stop rather than claiming success, and
    `agent.apps_allow_third_party` is excluded from the generic settings PATCH so
    no caller reaches the setting without that sequencing.
  - `start_enabled_app_backends` revokes at boot: an app the ceiling does not
    admit has its agents, skills, and MCP entries deregistered and its backend
    is not spawned. A policy tightened while the gateway was down therefore does
    not survive the restart.
  - the per-backend liveness watch re-reads the ceiling each sweep and stops a
    backend the ceiling does not admit. This is what closes the CLI and the
    hand-edited `config.json`: both reach the setting without passing the
    endpoint, and without the watch a backend they un-trust would keep serving
    until the next boot. Bound is one `_HEALTH_WATCH_INTERVAL`.

  Scope is the executing surface. An app with its own `agent.apps_trusted` grant
  keeps running while that grant stands — the blanket flag does not govern it — and
  non-executable resources (agents, skills, MCP declarations, cron definitions)
  are outside the ceiling. Anything that tries to RUN app code meets
  `app_execution_denied` and fails closed on its own.
  The liveness watch is level-triggered on the ceiling rather than edge-triggered
  on one setting, so REMOVING an app's own grant from `config.json` also stops its
  backend within one interval. That follows from the same rule and is intended: an
  app the gateway would refuse to load is an app it should not keep running.
  Turning the blanket flag off, on its own, never touches an app that still holds
  its own grant.

  **What revocation does NOT reach.** Only processes the gateway TRACKS are
  stoppable, because only those have a recorded identity to signal. An
  `openCommand` child is launched fire-and-forget by `POST /api/apps/<name>/open`
  and is never recorded, so one already running when the ceiling closes keeps
  running until it exits or the user closes it. What the closed ceiling does stop
  is the next one: that endpoint calls `app_execution_denied` before it spawns, so
  no new open is admitted. The same holds for any process an app's own backend
  spawned as a child of itself, which dies with its parent only if it is in the
  parent's process group.

  **Adopted backends are never builtin-exempt.** A backend found already answering
  a declared port is adopted rather than launched, so the gateway never vetted the
  executable behind it and cannot classify it as shipped code — there is no
  portable way to read a listening process's executable path. It is therefore
  recorded as third-party and is revocable, which fails closed. The consequence is
  that a revoked adopted backend is not respawned until the next gateway start. No
  shipped builtin can reach this: adoption requires a manifest to declare a
  concrete port, and every shipped builtin either declares `"auto"` or omits the
  key, which defaults to `"auto"`. A test pins that, so a future builtin that
  declares a fixed port fails CI rather than silently losing its exemption.

  **An unreadable policy is a deny.** `third_party_execution_allowed` fails closed,
  and the config loader falls back to defaults when neither config file can be read,
  where the flag is `false` and the trusted set is empty. Because the liveness watch
  re-reads the ceiling each sweep, that answer stops running backends as well as
  refusing new admissions. This is deliberate: sparing a backend whenever the
  policy cannot be read would make deleting `config.json` the one operator action
  guaranteed to stop nothing. An app's `installed.json` is app-writable,
  so it is consulted only to remove trust and never spares a backend from the
  ceiling; `config.json` belongs to the operator, so its absence is honoured as a
  withdrawal. The cost is
  availability and it is bounded: a genuine transient read fault stops third-party
  backends for that sweep, and they return at the next gateway start.

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

User-session routes add a second, semantic check. An enabled app must declare
`permissions.sessionApproval: true` before its app token can send a message to
an existing local user-owned session, choose a generated response option,
approve or deny a pending tool request, or change that session's approval mode.
The app still needs the matching route in `permissions.api`. Cron, system,
remote, member-mode, and other apps' sessions are denied; an app's existing
access to its own slots is unchanged. A message the app sends runs as a turn: a
send carrying a change to the session's agent binding, persona settings, or a
harness slash command is refused before anything is written or queued
(`chat_handlers._deny_app_session_settings`, pinned by
`test_chat_mode_security`). The send cannot steer, its row is echoed to the
user's open tabs, it neither starts nor names the session's auto-title (a
queued send restored after a gateway restart has lost that attribution), and
its SSE stream ends with the app's own turn. Mode changes must name a live allowed slot, which
prevents one app call from silently widening every session, and are limited to
Normal, Reads and Trust. YOLO is a process-global override: an app token can
neither arm it nor revoke it, so it stays a dashboard-only decision.

Every `/api/chat/slots/{slot}/*` route takes the same ownership decision before
its handler runs (`dashboard/slot_ownership.py`). This holds because a
`permissions.api` entry such as `/api/chat` matches that whole family. An app
passes only on a slot it owns that still runs on its own session (a task-runner
result tab's own session is the one minted for it). Approving or
denying a pending tool request is the one per-slot action the `sessionApproval`
grant also reaches on a local user session. Any other app, on any other slot,
gets the same `404 slot_not_found` a missing slot gets, and a refusal for a slot
that exists is recorded in the security-event log. The decision is keyed by the
slot in the path. The `/api/approvals` and `/api/sessions` families and
`POST /v1/chat/completions` are outside it. A request to `POST /api/chat`,
`POST /api/chat/slots` or the resume route that names a slot to create is
decided the same way before anything is created. A persisted transcript that
records a different app, or none, counts as not owned, and so do member, cron and
workflow keys and a key that matches a live slot's key or transcript only up to
letter case. So an app cannot reopen a closed user session as
its own, or hold a key a scheduled job's results are bound to. The full contract
is in [App Kit platform contracts §13](../system-specs/modules/app-kit-platform.md).

The guard reads the live manifest so that removing the flag revokes the grant at
once. Live-read is not a grant path for this flag: `update_app` compares the old
and new manifests, and a version that newly declares `sessionApproval` on an
enabled app comes back disabled with a `session_approval_reconsent` notice that
the detail page renders in place of its "updated" toast. `register_external_app`
applies the same comparison to self-managed apps, which author their own
manifest and re-register on every launch: a registration that newly declares the
flag (first or later) is written disabled, so a self-managed app cannot grant
itself session control. Enabling is the consent moment those two gates lean on,
and `disable_app` leaves the app's token valid, so `handle_enable_app` refuses
app-token callers outright (`app_token_forbidden`): an app cannot POST its own
`/api/apps/<name>/enable` to restore a grant the user has not re-consented to.

The consent surface is the **detail page**: the update notice and the Permissions
card. The trust dialog opens only when repo trust is missing, so re-enabling an
already-trusted app from a store card shows no dialog. At first install the
dialog's session row comes from the catalog or registry projection, not from the
cloned manifest, so a projection that omits the flag under-discloses. Two sibling
grants are also live-enforced and are not re-gated on update today
(`permissions.api`, `permissions.events`). A generic widened-permission check
across install, update and enable, and a structured enable-route refusal that
drives the dialog, are tracked in issue #11212.

A `permissions.api` entry is a prefix match, so declaring `/api/approvals` or
`/api/sessions` would otherwise reach every session on the instance. The
following routes therefore decide an app caller (an app token, or an
internal-secret caller whose calling session belongs to an app) by ownership:

- `POST /api/approvals/{id}/{action}` applies the slot approve route's rule. An
  app without `sessionApproval` is refused with the same 403 as
  `POST /api/chat/slots/{slot}/approve`, even for its own slots. An app holding
  the grant may resolve a request on its own slot or on a local user session,
  and the id must name exactly one such pending request: request ids can recur
  across sessions, so an ambiguous id is refused and the slot route, which names
  the session, decides it. Background (state-level) approvals raised by cron,
  autonudge, subagents or the task runner are never resolvable by an app, and
  `GET /api/approvals`, which lists only those, is empty for an app.
- `GET /api/sessions`, `GET /api/sessions/search`, `GET /api/sessions/{key}`,
  `DELETE /api/sessions/{key}` and `POST /api/sessions/summarize` reach only
  transcripts whose metadata records the calling app as owner. A delete is also
  refused when the live slot it would close is not the app's, because the slot
  is the server-side record. The whole-history routes, `DELETE /api/sessions`
  and `GET /api/sessions/clearable/count`, are refused to an app outright.
- The metadata owner is recorded by the app's own slot, so an app cannot open a
  new slot over a transcript it does not own: a named `POST /api/chat/slots`,
  a `POST /api/chat` that would create its slot, and a resume answer the same
  404 there. A new name or the app's own transcript is admitted.

Apart from the missing-grant 403, every refusal is the same 404 a missing target
returns, and the reason goes to the Security Event Log, as does every access
the ownership rule allows. Ownership is judged again under the transcript lock
at the read, summary or delete itself, so a transcript replaced after the first
check is refused rather than served. Dashboard-user callers
are unaffected. Other routes under the `/api/sessions` prefix are not
ownership-judged yet: `/api/sessions/{id}/agents*` (subagent results),
`POST /api/sessions/restart`, and the `usage`, `health` and `memory` reads. An
app that declares `/api/sessions/*` still reaches them; narrowing them is a
tracked follow-up.

### WebSocket event scope (CWE-269)

`/api/ws` is a *third* surface reachable with the same app token, and it is scoped
separately: connecting does not grant the full event stream. On connect the socket
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

Filtering a frame's payload is not always enough: the `slots` re-push is a full slot list, so it is re-filtered per app on the send path (`DashboardState._serialize_for_client`) —
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
directly, so `ws.py` gates those at the source. A persisted subagent run is replayed
or listed only when its recorded app equals the slot's current owner, failing closed
both ways, because slot keys are not namespaced by app; see
[subagent](../system-specs/modules/subagent.md).

Dashboard-user sockets are exempt, identified by a **positive** `is_dashboard_user`
claim set by the auth middleware — never by the absence of an app claim, which would
fail *open* on any path that forgot to set it. Because the stream is filtered,
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

The scoped client's generic `request` method and JSON verb helpers share the
same initial-path check. Request options do not add API grants or expand wildcard
strings; browser redirect targets are not rechecked, and callers can use
`redirect: 'error'` to refuse redirects. Session attribution is host-owned: a bound host overrides any supplied
`X-Session-Key`, and a host without a binding rejects caller-supplied session
identity. Routed app pages explicitly use `dashboard:ui`, the core API client's
dashboard-page identity; chat surfaces retain their actual bound session. This
remains a guardrail inside the dashboard document, not a sandbox or a replacement
for server-side authorization. Hosts of restricted chat sessions must provide the
real session key; absence cannot identify the restricted session to the backend.

This is an **HTTP-reach boundary distinct from the in-process module-loading
privilege**: an app's loaded Python still runs with full gateway privileges (the
warning above stands), but an app's own HTTP token cannot reach arbitrary
gateway or sibling-app endpoints. Dashboard-user tokens (empty app claim) are never
subject to this gate.

## Future work

True isolation (running app code in a separate sandboxed subprocess rather than in-process) is intentionally **out of scope** for now. Process isolation is tracked as a separate design to be revisited if/when a public app store lands. Until then, third-party executable surfaces require a per-app trust grant or `agent.apps_allow_third_party=true`; the default configuration denies them. (Corresponds to CSE finding SEC-012.)
