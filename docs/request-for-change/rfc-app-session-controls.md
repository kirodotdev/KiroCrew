---
title: App Session Controls — a composer seam for per-chat app state
status: accepted
author: omerrubi
created: 2026-08-31
last-audited: 2026-09-01
audited-at: 1d705a03f
doc-pr:
implementation-prs: [7573]
tracking-issues: []
supersedes: []
superseded-by: []
---
# RFC: App Session Controls — a composer seam for per-chat app state

- Status: accepted — this document ships in the same PR as its implementation
  (#7573), so §4 describes code that lands with it rather than code on a branch.
  §5 and §9 describe what has not been decided.
- Author: omerrubi
- Created: 2026-08-31
- Related: `rfc-navigation-placement-seam.md` (the sibling problem — a manifest
  UI field that nothing reads; §3.3 there names the app-label i18n hole this
  document inherits), `rfc-everything-is-an-app.md` (its inventory of eleven
  declared-but-unread manifest fields is the trap §5 of this document is shaped
  to avoid), `rfc-federated-app-platform.md` (the app UI loading path this
  rides), `docs/app-kit/manifest-reference.md` (the manifest contract this
  extends)

## Summary

Add `contributes.sessionControls[]` to the app manifest, and the composer-bar host that
renders it. An app declares a compact control; the dashboard renders it as a chip
beside the agent, model and project chips, and hands the control **the active
session's identity** when the user opens it. An optional `statusPath` lets the
chip report state before it is opened.

The field and its reader land together, on purpose. See §5.

## 1. Problem statement

An app can hold state that is meaningful per conversation, and has nowhere to put
the control for it. The gap is not the UI — it is that **an app cannot find out
which chat the user is looking at.**

Verified on main `1d705a03f` (2026-09-01):

- **The app SDK hands over no *active* session.** `AppInfo` in
  `website/src/app-sdk/index.ts` is `{ name, version, permissions }`, and `AppApi`
  is five HTTP verbs. The chat modules the SDK exports do carry a slot key — but
  always one the **app itself supplies**: `ChatEmbed` and `ChatPanel` take
  `slotKey` as a prop, and
  `useChatSession` derives one from a path the app passes in. Grepping the
  chat/session modules for `activeSession` or `currentSession` returns nothing.
  There is no host→app channel for the session on screen; every existing path is
  the app naming a session, not observing one.

- **`useChatSession` answers a different question.** It is documented as
  "workspace-scoped chat session management for apps" and takes a
  `workspacePath` the app supplies. Its slot is a deterministic function of that
  input: `const slotName = appName + '-' + hashStr(workspacePath)`
  (`slotName` in `website/src/app-sdk/useChatSession.ts`). So it answers *"a session for this
  path, creating one if needed"*. It cannot answer *"the session on screen right
  now"*, and no amount of calling it will.

- **The manifest's UI surfaces are all session-blind.** `UIConfig`
  (`UIConfig` in `src/kiro_crew/apps/manifest.py`) declares `entry`, `pages`, `overlays`
  and `sidebar`. A `UIPage` is routed and full-surface; a `UIOverlay`
  (`UIOverlay`) is explicitly *"not routed: it floats above whatever the
  user is looking at"*, and its required `replaces` field names a host slot the
  app **takes over** while enabled. None of the four carries a session.

The consequence is a bad choice for the app author, both branches of which are
worse than the feature deserves. Either bind the setting at a coarser grain the
app *can* name — a workspace or a folder — and accept that two chats in one
directory cannot differ; or put the control on a routed page and make the user
navigate away from the conversation to set a value that is about that
conversation, re-stating which chat they mean to a UI that already knows.

Meanwhile the composer is already the home for exactly this class of state. The
agent, model and project chips are per-chat controls sitting in
`website/src/components/ChatInput.tsx`. An app-contributed per-chat control
belongs beside them, and nowhere else.

## 2. Goals

1. An app can bind state to **the chat the user is in**, and set it without
   leaving that chat.
2. The seam is **additive**: several apps each contribute a control and compose.
3. A control can report state **before it is opened**, so a configured setting is
   visible at a glance.
4. A control gains **no privilege** its app has not already declared.
5. The manifest field and the code that reads it are **one indivisible change**.

## 3. Design principles

1. **The seam's whole purpose is the session identity.** A control that is not
   handed the active session is not worth adding a manifest field for.
2. **Additive, not slot-replacing.** Two apps must each be able to contribute a
   chip. This is the axis on which `ui.overlays` is the wrong precedent.
3. **Reuse the page loading path.** A control is a lazily-imported ESM module,
   exactly like a page, so the existing import map and the single-React
   guarantee apply unchanged. No second bundler, no second React.
4. **A chip must be able to report state before it is opened.** Otherwise a
   control that *is* configured looks unset until the user clicks it, which is
   the failure mode that makes an ambient indicator worthless.
5. **Declaring a field without reading it is a defect, not a phase.** Two RFCs in
   this directory already document that state.

## 4. Design

### 4.1 Manifest — `contributes.sessionControls[]`

`SessionControlContribution` joins `Contributes`, alongside the contributed
commands #7423 established there:

| Field | Meaning |
|---|---|
| `id` | Stable per-app identifier, kebab-case (e.g. `"env-picker"`) |
| `entryPoint` | ESM bundle path relative to `ui/` |
| `label` | Accessible name, and the chip tooltip |
| `icon` | lucide icon name |
| `statusPath` | Optional backend route reporting per-session chip state |

Validation, enforced at install:

- `MAX_SESSION_CONTROLS_PER_APP = 2` (`manifest.py`). A cap rather than an
  unbounded list, because the composer is a fixed-width surface shared with the
  host's own chips. This bounds one manifest; §4.2 bounds the bar.
- `id` must match `^[a-z0-9]+(?:-[a-z0-9]+)*$`.
- `statusPath` must match `^[a-z0-9][a-z0-9/_-]{0,63}$` — see §7.
- Invalid entries are **reported, not raised**: a malformed `sessionControls`
  must not abort the whole manifest parse, matching how an unroutable page route
  is handled today.

The composite key a control is addressed by is `<appName>:<id>`, so two apps
declaring the same `id` cannot collide.

### 4.2 Frontend — resolver, host, chips

- A resolver turns the installed-app list into resolved controls, dropping
  duplicate keys and sorting by key for a stable render order.
- **The composer renders at most two chips in total**, across all apps:
  `MAX_INLINE_SESSION_CONTROLS = 2` (`website/src/hooks/useSessionControls.ts`),
  applied as `out.slice(0, MAX_INLINE_SESSION_CONTROLS)` in `useSessionControls`. Two rather
  than three because the chips render in their own separated region and
  `max-two-buttons-per-row` (`website/AUTOSDE.yaml`, `blocking: true`) caps a
  horizontal group at two action controls, so the region sits at the cap
  rather than over it. Controls past
  the cap are **dropped, not overflowed**. That is a deliberate trade recorded in
  the code — the bar competes with the message input for one row, so an overflow
  menu is a follow-up and a dropped chip beats a composer that cannot be typed
  in. It is also a user-visible limit: an installed, enabled app's declared
  control can simply not appear, with nothing saying so. §9 keeps that open.
- Opening a chip mounts the app's module inside the existing `AppApiProvider`,
  so the control inherits the app's own declared `permissions.api` /
  `permissions.events` allowlist — a control gets no privilege its app does not
  already have.
- The mounted surface is a labelled dialog naming **both** the control and its
  app, because two apps may contribute a similarly-labelled chip and the user
  has to be able to tell which one is open.
- **The control is keyed on the session** (`SessionControlHost.tsx`:
  `key={session.sessionKey}`), so switching chats remounts it and a control
  holding per-session state cannot leak it across a switch.
- **Failure is local.** A control sits on the path of every turn, so its error
  boundary renders a compact inline notice and unmounts nothing but itself.

**The props contract.** This is the public interface an app author codes
against — `SessionControlContext` in
`website/src/components/SessionControlHost.tsx`:

```ts
export interface SessionControlContext {
  /** Session key, e.g. `dashboard:chat-2-1787502679`. Empty pre-slot. */
  sessionKey: string
  /**
   * Folder the chat is filed in, or '' when it is at the top level.
   * A folder is a dashboard grouping with its own id — not a directory — so an
   * app storing a per-folder setting must key on this and not on `cwd`.
   */
  folderId?: string
  /** Folder's display name, for an app that wants to name it back to the user. */
  folderName?: string
  /** Working directory recorded for the session, when known. */
  cwd: string
}
```

The component is mounted as `<Control session={…} onClose={() => void} />`: the
context above plus an `onClose` callback so a control can dismiss itself after
committing a change.

### 4.3 Optional status — `statusPath`

When declared, the host GETs `<the app's own route base>/<statusPath>` with
`session_key`
always, `folder_id` when the chat is in a folder, and `folder_name` alongside it
when known — a control holding a per-folder setting cannot answer without the
folder, and a brand-new chat is exactly the case where it has no record of its
own to fall back on. It reads `{ state, tooltip }`, where `state` is `ok` |
`warn` | `none`; the chip tints with `--ok` or `--warn` respectively
(the `sessionControls` chip render in `ChatInput.tsx`) and the tooltip is length-bounded.

The route base is derived from the manifest, not declared: an app with
`backend.entryPoint` runs its own backend process and is reverse-proxied at
`/apps/<app>/api/`, while one with only `backend.hooks.routes` is registered
in-gateway under `/api/apps/<app>/`. Both prefixes are constructed host-side from
the app name, so `statusPath` remains the only app-authored segment. Picking one
prefix for both was a real defect found in review — the hook prefix answers `502
no reachable backend` for a process-backed app, which the chip would render as a
permanently stateless control with nothing saying why.

A control with no `statusPath` is never polled, and no poll is issued before a
session exists. Polling **fails closed**: a third-party app that is down is not
retried at the composer's expense, and a malformed or unknown payload degrades
to `none` rather than rendering an unknown state.

## 5. Migration plan

**S1 and S2 must land together.** Shipping S1 alone would add a twelfth
declared-but-unread manifest field, which is the exact state
`rfc-everything-is-an-app.md` inventories (its §2.3 table lists eleven, measured
at `e6b06685e`) and `rfc-navigation-placement-seam.md` exists to correct. They may
be two commits in one PR, or a stacked pair where S1 does not merge alone — but
no release should contain S1 without S2.

That is a deliberate departure from this directory's phase discipline, which asks
for phases that are independently shippable. S1 and S2 are two *reviewable* units
and one *shippable* one; the split exists to make the diff readable, not to make
the schema releasable on its own.

| Phase | Scope | Exit criteria |
|---|---|---|
| **S1** | Manifest schema + validation | `sessionControls` round-trips through `to_dict`/`from_dict`; the per-app cap is enforced; a bad `id` or `statusPath` is reported by `validate()` and does not raise; existing `pages` validation is unchanged |
| **S2** | Resolver, host, composer chips | A declared control renders one chip, up to the global cap of two; opening it mounts the module in a dialog whose accessible name carries control and app; an app declaring none produces zero DOM change and zero requests |
| **S3** | `statusPath` polling | A control with `statusPath` reflects `ok`/`warn`/`none`; one with none issues no request; a traversal or cross-origin `statusPath` is refused before any fetch |

S3 is independently abandonable: without it the seam still works, and chips
simply carry no state until opened.

## 6. Backward compatibility

The change is additive in both directions.

- **An app that declares nothing is unaffected.** `UIConfig.from_dict` reads
  `data.get("sessionControls", [])` (`manifest.py`), so a manifest without
  the key parses to an empty list and behaves exactly as before — no chip, no
  request, no DOM change.
- **No existing manifest changes on disk.** `to_dict` emits the key only when
  non-empty (`SessionControlContribution.to_dict`), so re-serializing an existing app's manifest
  produces byte-identical output.
- **An older gateway ignores the field rather than failing.** `sessionControls`
  is nested inside `contributes`, and `Contributes.from_dict` reads only the keys
  it knows, so a build predating this change parses such a manifest without error
  and simply renders no chip. A build predating the `contributes` block itself
  keeps the whole object in `extra` untouched, which is the same outcome by a
  different route. An app can therefore declare a control and still
  install on an older host, degraded but working. This is also why the capability
  should be probed rather than gated on a version number.
- **No wire or storage format changes.** The status route is a new GET on a path
  the app already owns under `backend.routes`.

## 7. Security model

- **`statusPath` is app-scoped and validated at three layers.** The backend
  regex bounds charset and length at install (`_SESSION_CONTROL_STATUS_PATH_RE`); the frontend
  resolver independently refuses a path that would traverse into another app, go
  protocol-relative, reach another origin, or corrupt the appended query string,
  failing closed to an empty path; and the API client refuses again before the
  URL is constructed. Excluding `.` from the charset makes `..` unrepresentable
  rather than filtered. A control whose `statusPath` does not survive validation
  is simply never polled.
- **No new privilege.** A control runs under its app's existing declared
  allowlist via `AppApiProvider`. Status polling is a GET to a route the app
  already declares in `backend.routes`.
- **Bounded cost.** The bound that matters is the **global** cap of two (§4.2).
  It is the same number as the per-app cap but does different work: the per-app
  cap bounds one manifest, and only the global cap bounds the bar and the number
  of pollers however many apps are installed.
- **The session key is given to an app the user installed and enabled**, and only
  for the session on screen. It is not a capability to enumerate other sessions:
  the host passes one identity, and the app has no listing route it did not
  already declare.

## 8. Non-goals

- Not a general-purpose composer plugin API. One surface ships — the composer
  session bar. A second would need its own argument, and the manifest field to
  select between them is deliberately deferred until there is one (§9.4).
- Not a replacement for `ui.pages`. A control is compact and session-scoped;
  anything larger stays a page.
- No cross-app state, and no host-mediated write path. A chip's only way to
  persist anything is its app's own declared API.
- Does not fix `ui.sidebar` or `ui.overlays` being unread. That is
  `rfc-navigation-placement-seam.md`'s scope, not this one's.

## 9. Open questions

1. **App-provided labels are untranslated.** `label` is authored in the
   manifest and rendered as-is, which is the same hole
   `rfc-navigation-placement-seam.md` §3.3 identifies for app nav labels. Should
   this seam wait on that translation path, or ship untranslated labels and adopt
   it when it exists? Shipping first is what the implementation does today.
2. **The global cap drops silently — should it overflow instead?** The composer
   is capped at two controls and anything past the cap is dropped
   (`useSessionControls`), which the code itself flags as a follow-up. A
   user with three contributing apps has one control that is simply absent, with
   nothing disclosing it. An overflow menu, or at minimum a disclosure, is the
   open question — not whether a global cap should exist.
3. **Should status be pushed rather than polled?** `rfc-local-notification-bus.md`
   has a bus whose Phase 2 is wired with no producer. A control's status change is
   a plausible producer, and would retire the poll.
4. **How does a second surface get selected?** An earlier draft shipped a
   `placement` field for this, with `session-bar` as its only legal value. It was
   removed before merge: nothing branched on it, so it was manifest schema —
   which cannot be withdrawn once apps write it — bought against a surface that
   does not exist. Adding a field when the second surface arrives is a
   backward-compatible change; removing one is not. The same reasoning removed
   `agent`, `model` and `workspace` from the props contract in §4.3.

## 10. Alternatives considered

**Declare it under `ui` instead of `contributes`.** Rejected — and this is the
namespace the seam first shipped with, so the correction is recorded rather than
quietly applied. `Contributes` draws the line in its own docstring: `ui` is where
an app declares surfaces of its OWN, while a contribution is a row inside a
surface the host renders and controls. A composer chip is the second kind — the
dashboard owns the bar, orders the chips and enforces the cap; the app supplies a
row. #7423 established the block for exactly that, and #7955 / #7975 are defining
fields in the same place. Because a manifest field cannot be withdrawn once apps
write it (the §9.4 argument, applied to this seam), the namespace is settled once
here rather than a PR at a time.

**Reuse `ui.overlays`.** Rejected on two counts, both structural. An overlay's
`replaces` field is required and names a host slot the app **takes over**, so the
model is one-app-per-slot; session controls are additive and must compose. And an
overlay is documented as floating "above whatever the user is looking at" — it is
as session-blind as a page, so it would not solve the actual problem even if the
contribution model fitted.

**Reuse `ui.pages` and pass the session in the route.** Rejected: it makes the
user leave the conversation to configure the conversation. It also puts a session
key in a URL, which is a sharing and history-leak surface a chip does not have.

**Give every app the active session key through the app SDK.** Rejected as too
broad for the problem. Every app page would receive ambient session identity
whether or not it has any per-session behaviour, which widens the surface for all
apps to serve the few that need it. A declared seam is the narrower change: an app
that wants session identity says so in its manifest, and gets it only inside the
control it declared.

**Let apps use `useChatSession`, `ChatEmbed` or `ChatPanel`.** Not an
alternative, on inspection — as §1 shows, each of them takes or derives a slot key
the **app supplies**, so they name a session rather than observing the active one.

**Do nothing; bind at workspace or folder grain.** This is today's behaviour and
it is a real option for some apps. It is not sufficient in general: several chats
routinely share one working directory, so a workspace-grained binding cannot
express a value that differs between two of them.
