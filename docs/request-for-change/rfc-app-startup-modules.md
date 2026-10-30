---
title: App Startup Modules, app UI that runs while the dashboard is open
status: draft
author: presidentarrow
created: 2026-10-25
last-audited: 2026-10-29
audited-at: fc33a2919b
doc-pr:
implementation-prs: []
tracking-issues: []
supersedes: []
superseded-by: []
---
# RFC: App Startup Modules, app UI that runs while the dashboard is open

- Status: draft. Nothing is built.
- Author: presidentarrow
- Created: 2026-10-25
- Related: `rfc-app-session-controls.md` (a manifest field that landed with its reader),
  `rfc-federated-app-platform.md` (the app UI loading path this rides),
  `rfc-everything-is-an-app.md` (it gates third-party replacement of host surfaces on
  sandboxing, which section 7 weighs), `rfc-app-sandbox-isolation.md`,
  `docs/app-kit/manifest-reference.md` (the manifest contract this extends)

## Summary

Add an optional `ui.startup` entry to the app manifest: a module whose default export is a
React component that renders nothing. The dashboard mounts it on every load, on every
route, for each enabled app whose startup entry the owner has approved, and unmounts it in
every open tab when the app is disabled, uninstalled, updated or loses trust. A
third-party app qualifies only if all of its code runs in the browser. Today an installed
app's code runs only while one of its surfaces is open, so an app that changes the whole
dashboard has to be reopened after every reload.

## 1. Problem

An enabled app that adds something to the whole dashboard, such as a companion that
replaces a resting pointer, a keyboard layer or an ambient status indicator, should be
present whenever the dashboard is open and gone once the app is disabled.

What happens instead, verified on main `fc33a2919b` (2026-10-29):

- Installed-app code enters the page through two dynamic imports only: the one in
  `importAppBundle` (`website/src/components/appBundleImport.ts`), which `AppHost`
  (`website/src/components/AppHost.tsx`) calls to render an app page or side-panel tab,
  and the one in `SessionControlHost` (`website/src/components/SessionControlHost.tsx`),
  which renders an open composer popover. No other non-test
  `import(/* @vite-ignore */ ...)` exists in `website/src`, and `resolveSlotOverlays`
  (`website/src/apps/overlaySlots.ts`) refuses host overlay slots to non-builtin apps.
- So dashboard-wide behaviour starts when the user opens the app's page and ends at the
  next reload. A prototype idle-pointer companion has to ask its user to reopen its page
  after every reload; a keyboard layer or a status indicator has the same shape.
- Disabling the app does not fully stop that code. `AppHost` swaps a disabled app's page
  for `AppDisabled`, and React runs the page's effect cleanups, but anything created
  outside React effects, for example at module evaluation, stays until the tab reloads.
- Other tabs do not hear about the change. The shell refreshes its app list on a local
  `mc:apps-changed` event (its listener is in `website/src/shell/appShell.tsx`), which
  only the acting tab dispatches (for example `announceAppsChanged` in
  `website/src/pages/apps/useAppsData.ts`), or on a WebSocket reconnect. The only app
  broadcast the gateway sends is the dev-mode `app_reload` in
  `src/kiro_crew/apps/dev_mode.py`.
- An update needs a reload, because importing the same module URL again returns the copy
  the browser already holds.

The closest workaround is a contributed side-panel tab. Open app tabs are restored after
a reload (`useAllAppTabs` in `website/src/hooks/usePanelTabs.ts`) and keep the side panel
mounted (`shouldMountSidePanel` in `website/src/pages/chat/sidePanelMount.ts`), so the
tab's entry runs again. It does so only on the chat page, and only after the user has
opened the tab and left it open.

## 2. Goals

1. An enabled builtin app, or a third-party app whose code all runs in the browser, can
   mount one declared component on every dashboard load, on every route, without the user
   opening anything.
2. The component unmounts in every open tab, without a reload, when the gateway
   disables, uninstalls or updates its app or stops trusting it. Code that outlives the
   unmount stops at a reload the tab prompts for.
3. It runs only after the owner approves that app's startup entry, approval is asked
   again when the app's version changes or its startup entry is added or changed, and a
   safe reload starts none.

## 3. Non-goals

- New data reach. The component receives the same SDK context as the app's pages.
- Isolating app UI code. Page code already runs in the dashboard's origin; this RFC
  changes when it runs, not what it can reach.
- Ordering between apps' startup components.
- Changes to builtin overlays or host slots.

## 4. Design

### 4.1 Manifest

```json
"ui": {
  "entry": "page.mjs",
  "startup": "startup.mjs",
  "pages": [{ "route": "/apps/pointer-pal", "label": "Pointer Pal" }]
}
```

`ui.startup` belongs under `ui` because `ui` declares what the app owns, while a
contribution "is data, never code" (`docs/app-kit/manifest-reference.md`). It is a path
under the app's `ui/` directory, served by the same static route as `ui.entry`. It gets
the containment check `ui.entry` gets (`validate` in `src/kiro_crew/apps/manifest.py`)
plus a new `.mjs` or `.js` check, since that route also serves `.css` and `.json`. Unlike
`backend.hooks.on_startup`, it runs in the browser, not in the gateway.

### 4.2 Module contract

```js
// ui/startup.mjs
import { useEffect } from 'react'

export default function PointerPal() {
  useEffect(() => {
    const onKey = event => { /* ... */ }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [])
  return null
}
```

The host renders the default export inside the same `AppApiProvider`, error boundary and
`Suspense` that `AppHost` wraps around a page, so the component uses the existing hooks
(`useAppApi`, `useAppEvents`) and cleans up through its effects. Work done at module
evaluation cannot be undone, so it belongs in an effect. One instance runs per tab, and
StrictMode mounts it twice in development, as it does every page.

### 4.3 Admission and approval

`/api/apps` reports each app's startup state, computed on every read rather than stored at
enable time, so a later trust or governance change applies on the next load:

- `admitted`: the app is enabled, passes the current execution and governance checks,
  meets the eligibility rule below, and the owner approved its `ui.startup` at its current
  version.
- `needs-approval`: the entry is new, or the entry or the app's version changed since the
  owner last approved it.
- `denied`, with the reason.

The per-read execution check is `third_party_ceiling_closed`
(`src/kiro_crew/apps/execution.py`), which takes a name only and writes no audit row. Its
docstring gives both reasons: a path resolved at re-check time comes from files the app
can write, and an `allowed` row per app per read would bury the decisions the audit trail
exists to show. Builtin status is decided once on the vetted path, as
`AppProcess.admitted_builtin` is, and an audit row is written through
`app_execution_denied` when an app's startup state changes.

A third-party app is eligible only when all of its code runs in the browser. Its manifest
declares nothing the gateway or an agent runs: no backend, `backend.hooks`, cron job,
`setup` script, `openCommand`, agent, MCP server or skill, and the app is not self-managed
(`resources: "app"`, `src/kiro_crew/apps/manager_external.py`). `backend.hooks` run inside
the gateway with its full privileges (`load_app_module` in
`src/kiro_crew/apps/module_loader.py`), and this RFC does not rely on every process launch
path being confined, so the rule admits no process at all. The check fails closed: a
manifest section it does not recognise denies. Open question 1 asks whether to widen it.
An ineligible app is reported `denied`, with the reason, before any approval is asked.

Approval is the owner's consent to running on every load, not a review of the code. It is
keyed to the app's name, its version and its `ui.startup` path, so an install that adds
the entry and an update that changes the version or the entry hold it back until the
owner approves. Page code also runs an update's new code without a new consent.
Startup approval is asked again because a startup module runs that code on the next
load, without the click that opens a page. Only the owner can approve
(`require_owner_dashboard_request`, as for `app_enable`), under its own audit operation.
The record cannot live in the app's own folder, which code the app runs can write,
`app.json` included. It is a sidecar at the apps root, sealed the way the dev-mode grant
is (`dev_mode_granted_root` in `src/kiro_crew/apps/dev_mode.py`): it joins
`apps/.dev-grants.json` on `_CREW_READONLY_LEAVES` in `src/kiro_crew/sandbox_leaves.py`,
which the OS sandbox mounts read-only, and the gateway creates it before any sandbox
starts, since a launcher can only seal a file that exists. The seal binds only processes a
sandbox confines, and code the gateway runs unconfined can write the record like any other
file, as `src/kiro_crew/apps/backend.py` notes for the governance cache it seals; an
eligible third-party app runs no such code.

If open question 3 keeps approval in the dashboard, the approve action goes on the app's
detail page, beside a line such as "Runs its own code in every dashboard tab, from the
moment the dashboard opens". The trust dialog cannot carry this line: `capabilities()` in
`website/src/components/appstore/TrustAppModal.tsx` is a fixed list, and the dialog opens
only when an enable is refused, so an app trusted earlier would never show it. Builtin
apps need no approval and stay subject to governance; dev-mode apps count as third-party.

### 4.4 Host

`App` (`website/src/App.tsx`) renders a `StartupModules` component in its main dashboard
layout. `main.tsx` mounts `App` on every route except `/worlds-popout`, inside the router
and behind `KiroPrerequisiteGate`, so startup components run on every dashboard route and
never before the dashboard's prerequisites pass. The host renders nothing in the pop-out
and embed frames `App` also serves, in an embedded instance pane (`isEmbeddedPane`), or in
a non-owner session, such as one a Telegram, Teams or Webex allowlist link opens
(`src/kiro_crew/dashboard/websocket_hub.py`). It reads the installed-app list through
`useInstalledApps` (`website/src/hooks/panelTabRegistry.ts`) and renders one component per
admitted app, keyed by name and version:

- When an app becomes admitted, the host imports `/apps/<name>/ui/<startup>` through
  `withBase` and mounts its component.
- When it stops being admitted, the host unmounts it, which runs its cleanups. Code that
  does not clean up in an effect, such as work done at module evaluation or a listener it
  never removes, stops only when the tab reloads. So a tab that imported the module of an
  app that was uninstalled, lost trust or was denied prompts for a reload, as it does for
  a version change.
- When its version changes, the app needs approval again, so the host unmounts it and
  shows that the new version starts once the owner approves it and the tab reloads.
  Importing the same URL returns the cached module, and versioning the entry URL
  alone would still resolve its relative imports to cached modules.
- A failed import or a render error is caught by that app's boundary, logged with the
  app's name, and shown on its app page beside the startup state. Other apps and the
  shell keep running.
- A dev-mode `app_reload` for the app reloads the tab, as `AppHost` does on
  `mc:app-reload`.
- A safe reload mounts no startup component. `?safe=1` already exists
  (`captureSafeReload` in `website/src/lib/safeReload.ts`), and the desktop app adds it
  after a renderer crash or hang.

### 4.5 Every tab hears about changes

The gateway broadcasts `apps_changed` wherever it changes an installed app's enabled
state, version, manifest or trust. That includes trust revocation, whose handlers call
`disable_app` directly (`api_trusted_app_revoke` and `_stop_apps_running_on_blanket_trust`
in `src/kiro_crew/dashboard/handlers/security_trusted_apps.py`). The per-socket gate,
`_ws_client_allowed` in `src/kiro_crew/dashboard/websocket_hub.py`, denies by default and
passes a dashboard socket every frame except two kinds: the per-member event log's
(`MEMBER_LOG_EVENTS` in `src/kiro_crew/dashboard/ws_event_scope.py`), which reach only the
owner's socket, and an app's own output (`app_origin_frame` there), which a relayed
teammate's socket never receives. `apps_changed` is neither, so every dashboard tab
receives it. The message is a hint to refetch, handled like `mc:apps-changed`: each tab
refetches the list and renders from the newest response, and React runs a removed
component's cleanups before a replacement's effects.

So `kirocrew app disable <name>` unmounts the component in every open tab when the CLI
reaches the running gateway. A change the gateway does not make applies at each tab's next
`/api/apps` read, on load or reconnect: an edit to `agent.apps_trusted` or
`agent.apps_allow_third_party` in the config file, a setting `_revoke_if_ceiling_closed`
in `src/kiro_crew/apps/backend_runtime/supervision.py` notes has writers besides the
dashboard, or an app rewriting its own `app.json`. A disable written only to disk
(`_print_file_only_app_result` in `src/kiro_crew/cli_commands_app.py`) takes effect at the
next gateway start.

## 5. Migration plan

**Phase 1: the field, admission, approval, host and safe reload together.** `UIConfig`
carries `startup`, validation covers it, `/api/apps` reports the startup state, the app
page shows that state and any startup error, and `StartupModules` mounts admitted
components. Exit criteria: an approved `ui.startup` mounts once per tab on every dashboard
route and not on `/worlds-popout`, in a pop-out or embed frame, or in a non-owner session;
an unapproved one does not mount, and a non-owner session cannot approve it; an update
that changes the app's version or its `ui.startup` stops it until approved again;
disabling the app in the same tab unmounts it; a component that throws leaves the shell
rendering and its error on the app page; a load with `?safe=1` mounts none; a third-party
app that declares a process, `backend.hooks`, an agent, an MCP server or a skill, or is
self-managed, is reported `denied` and never mounts. Phase 1's admission rule follows the
answer to open question 1, and Phase 1 builds the approve action only once open question 3
says where approval is taken.

**Phase 2: cross-tab changes.** The gateway broadcasts `apps_changed`. Exit criteria: in
a pod, disabling an app or revoking its trust in one tab unmounts its component in a
second tab without a reload, and revoking trust prompts that second tab to reload.

Phase 2 depends only on Phase 1, and the two ship in the same release, so every release
that mounts startup components also unmounts them in every open tab when another tab
disables the app or revokes its trust, as Goal 2 requires. Open question 2 blocks neither
phase. Rolling back Phase 1 removes the host and the startup state; manifests that declare
the field keep working as page-only apps, as they do on gateways that predate it.

## 6. Backward compatibility

The field is optional and additive. A gateway without Phase 1 drops it, because
`UIConfig.from_dict` in `src/kiro_crew/apps/manifest_sections.py` keeps only `entry`,
`pages`, `overlays`, `sidebar` and `loading`, so the app installs and works as a page-only
app. The exception is a signed manifest on a gateway that requires signatures:
`signing_payload` in `src/kiro_crew/apps/manifest.py` covers `ui.to_dict()`, which on that
gateway drops `startup`, so the signature no longer matches and `app_admission_denied` in
`src/kiro_crew/apps/admission.py` refuses the app as unsigned or invalid.
`install_from_registry` in `src/kiro_crew/apps/registry_pipeline/install.py` runs that
admission before it checks `minKiroCrewVersion`, so an install or update from the registry
gets the signature refusal whatever version the manifest declares. An install or update
from a local path checks the version first, in `_validate_source_path`
(`src/kiro_crew/apps/manager_tree.py`). An app that depends on startup behaviour sets
`minKiroCrewVersion` to the release that ships Phase 1, so a gateway without Phase 1 that
reaches the version check refuses it with a version message instead of installing it as a
page-only app.

## 7. Security considerations

- **Reach over time.** A startup component is page code, in the same origin and with the
  same SDK scope, that now runs on every load without a click. A malicious or broken app
  therefore reaches every session sooner. Owner-only approval, admission recomputed on
  every read and the safe reload answer a broken app and let the owner withdraw a
  malicious one.
- **Self-approval.** Section 4.3 admits only third-party apps whose code all runs in the
  browser, so an eligible app has no background code that could write its own approval
  record. Approval is consent, not a boundary against the app's page code: once its page
  is open, that code shares the dashboard's origin and can call the approve route as the
  owner, since dashboard routes cannot tell an app's page from the operator (the dev-mode
  toggle in `src/kiro_crew/apps/dev_mode.py` notes the same). The boundary against such an
  app is trust.
- **Host UI.** A startup component can change host UI that `resolveSlotOverlays` withholds
  from non-builtin apps; `website/src/apps/overlayRegistry.ts` says a host slot is not "a
  licence to displace arbitrary host UI", and `rfc-everything-is-an-app.md` blocks
  third-party replacement of a core surface on `rfc-app-sandbox-isolation.md` Phases 1 to
  3. Page code can already do the same while its page is open; a startup component makes
  it last. This RFC proposes accepting that for approved third-party apps whose code all
  runs in the browser, since a trusted app's page code already runs in the dashboard's
  origin whenever its page is open. Open question 1 asks whether to widen that rule.
- **The static route.** No new exposure. `handle_app_ui_file` in
  `src/kiro_crew/apps/routes_static.py` already serves any allowed file under an app's
  `ui/` without authentication or an enabled check. Admission decides what the dashboard
  runs, not what the route serves.
- **The main thread.** Boundaries catch thrown errors, not a hang. A component that
  freezes its tab never handles `apps_changed`; the way out is a safe reload, then
  disabling the app.

## 8. Alternatives considered

- **Persisted side-panel tabs.** Available today, but limited to the chat page and
  dependent on a tab the user opened and left open.
- **Theme packs.** They load at start, but their overlays run in sandboxed iframes
  (`sandbox="allow-scripts"` in `website/src/components/ThemeExperienceLayer.tsx`) that
  cannot reach the dashboard's DOM, and a pack is not an app.
- **A host idle-pointer state**, an attribute such as `data-pointer-idle` that theme CSS
  can target. It runs no third-party code and is worth doing for cursor themes, but it
  serves one case and not a keyboard layer or a status indicator.
- **A named export on `ui.entry`.** It needs no new field, but the host would import every
  enabled app's full page bundle on every load.
- **A `start(app)` function that returns a stop function.** It needs a non-hook form of
  the SDK and its own error and teardown handling, which `AppHost`'s component path
  already provides.
- **Keep-alive pages.** Keeping a page mounted after navigating away does not survive a
  reload.

## 9. Open questions

1. Should the eligibility rule in section 4.3 widen before `rfc-app-sandbox-isolation.md`
   Phase 3, to apps whose processes all run in the OS sandbox, or to every trusted app?
   Each step admits code outside the browser that could write the approval record, so the
   first needs every process launch path shown to be confined. Owner: maintainers.
2. Should an update restart the component in place? That needs versioned URLs for every
   file a module imports, not only its entry. Owner: app-kit maintainers.
3. Should approval be taken where an app's page code cannot reach it, such as the CLI or
   the desktop app, and should it also cover `ui.entry`, whose code already runs in the
   page? Owner: security reviewers.
4. Approval keyed to the version does not see startup code that changes while the version
   stays the same, as a dev-mode app's code does. Should approval follow the code itself?
   That needs a hash over every file the module imports, not only its entry, for the
   reason open question 2 gives. Owner: maintainers.
