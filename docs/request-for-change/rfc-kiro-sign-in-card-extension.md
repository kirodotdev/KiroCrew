---
title: An edition extension point on the Kiro CLI first-run sign-in card
status: draft
author: RohanK6
created: 2026-11-09
last-audited: 2026-11-09
audited-at: d0e911b617
doc-pr: null
implementation-prs: []
tracking-issues: [15346]
supersedes: []
superseded-by: []
---

# RFC: An edition extension point on the Kiro CLI first-run sign-in card

**Status:** draft. Nothing built; this document changes no code. Every main-side
reference was read at `d0e911b617`. Acceptance is requested from a maintainer;
the status flips to `accepted` when one records it here.

Credit: the ask is [#15346](https://github.com/kirodotdev/KiroCrew/issues/15346)
(sign in to Kiro CLI without a terminal), and the shape proposed here is the one
mrbeag suggested in their
[2026-10-22 comment](https://github.com/kirodotdev/KiroCrew/issues/15346) on that
issue: "an extension point on the 'Sign in to Kiro' card would be enough for
editions to add their own".

## Summary

Part of what #15346 asked for has shipped since it was filed (see
[What main already does](#what-main-already-does)). The step that still needs a
terminal is the sign-in itself, and the core deliberately does not run
`kiro-cli login` (commit `cb1a54371`).

This RFC proposes one small seam: `registerKiroSignInAction`, a frontend extension
point on the Kiro CLI sign-in card of the **first-run** gate. An edition that ships
its own sign-in helper registers one action there. When nothing registers, the card
renders exactly as it does today. The core gains no login route, no process spawn,
and no access to the CLI's credential store.

The scope is first-run sign-in only. Signing in again after a mid-use sign-out on
the native Kiro CLI backend is a separate gap, recorded below and left out of this
RFC.

## What main already does

#15346 was filed on 2026-09-30. Measured at `d0e911b617`:

| #15346 asked for | On main | Where |
|---|---|---|
| Tell a sign-out apart from a model error | yes | `src/kiro_crew/acp/session_provider.py` `_connection_failure_is_signed_out` raises `AcpAuthRequired`, confirmed by `kiro-cli whoami` through `src/kiro_crew/kiro_prerequisite_probe.py` `signed_out_confirmed` |
| A sign-in error instead of "switch models" | yes | `src/kiro_crew/dashboard/chat_turn/turn_failures.py` `_on_auth_required` posts the error card, never a Continue button |
| A link from that card to the sign-in card | KAS only | `AcpAuthRequired.__init__` in `src/kiro_crew/acp/transport_errors.py` tags `auth_required` only for `ACP_BACKENDS_HOST_AUTH_CALLBACK`, which holds only KAS; on the native Kiro CLI backend the card is plain error text |
| Keep the waiting work after a sign-out | yes | `slot._last_turn_auth_required` holds the queue (checked by every drain in `src/kiro_crew/dashboard/chat_turn/queue_cycle.py`); `recovery._requeue_auth_retry` puts a failed synthetic or subagent input back |
| Resume that work on its own after sign-in | no | the hold lifts when the next turn ends without an auth error, which in practice is the person's next send |
| Stop trusting a stale "ready" | yes | `_on_auth_required` calls `_mark_kiro_signed_out` |
| The exact command for a bundled CLI (Windows desktop) | yes, first run | `website/src/components/KiroPrerequisiteGate.tsx` `SignInCommands` shows the personal (`login_command`) and organisation SSO (`sso_login_command`) commands, each in a `CopyCommand` |
| Re-check without a restart | yes, first run | the card's own Check again (an explicit probe), plus a 5 s coalesced poll while the first-run gate is the whole UI (`cb1a54371`) |
| A notice when sign-in ends mid-use | LTS edition only | `website/src/components/KiroPrerequisiteRelockNotice.tsx`, rendered from `KiroPrerequisiteGate`'s `unlessRelocked` |

On an install that has finished setup, the default edition shows a mid-use
sign-out only as the error card in the chat that failed. That is deliberate:
`f0c782e033` (#820) removed the persistent reauthentication banner, and the gate's
comment above its `app()` helper in `KiroPrerequisiteGate.tsx` records the rule
("the ONLY sign-out signal the dashboard shows"). This RFC does not change it; see
[Alternatives considered](#alternatives-considered).

## Motivation

On the desktop build the CLI is bundled, so the first-run card's install step is
already done, and its sign-in step asks the user to paste a long command into a
terminal (`src/kiro_crew/kiro_prerequisite_status.py` `login_commands_for`).
mrbeag reports that this is where people who chose the desktop app to avoid a
terminal give up.

The core cannot simply run the login again. `cb1a54371` (bolichen97, 2026-08-05)
removed `POST /api/kiro-prerequisite/login`, the `kiro-cli login --use-device-flow`
spawn and the code that scraped its URL, because the sign-in and the credential
store belong to Kiro CLI; the gate now only detects the result, and every CLI spawn
is one of two read-only probes (`--version`, `whoami`).

An edition can still have a better answer than the core can: a packaged desktop app
can open a system terminal on the exact command, or hand off to a vendor sign-in
helper it already ships. Today it has nowhere to put that button.

## Goals

1. An edition can add one sign-in action to the first-run Kiro CLI sign-in card.
2. The stock build renders the card exactly as it does today.
3. The action is handed both sign-in commands the card shows, so it can keep the
   personal / organisation choice the card exists to offer.
4. The core still never runs `kiro-cli login`, never reads or writes the CLI's
   credential store, and never sees a token.
5. After the action, the card's existing Check again and poll decide readiness, as
   for a terminal sign-in.

## Non-goals

- A login route, process spawn, or URL scraping in the core (`cb1a54371`).
- A standing sign-in status indicator in the default edition (#820).
- Signing in again after a mid-use sign-out on the native Kiro CLI backend: its
  error card has no link to a sign-in card today, and the held queue waits for the
  person's next send. Both are real gaps; closing them changes the error card and
  the queue, which is a different RFC (open question 1).
- Any change to the chat error card, the queue hold, the LTS sign-in screens or the
  relock notice.
- A backend hook. The seam is frontend only; whatever the action does, it does
  through the edition's own code.

## Design

### The seam

A new module, `website/src/components/kiroSignInAction.ts`, in the shape of the
existing single-slot seams (`website/src/pages/overviewPanel.tsx`
`registerOverviewPanel` / `getOverviewPanel`):

```ts
export interface KiroSignInAction {
  /** Stable id, for the collision report. */
  id: string
  /** Rendered in the sign-in card; owns its own labels and i18n. */
  render: (ctx: KiroSignInActionContext) => ReactNode
}

export interface KiroSignInActionContext {
  /** The personal (Builder ID) command the card shows, as the gateway sent it. */
  loginCommand: string
  /** The organisation SSO command the card shows, as the gateway sent it. */
  ssoLoginCommand: string
  /** Re-run the card's own Check again (an explicit probe). */
  recheck: () => void
}

export function registerKiroSignInAction(action: KiroSignInAction): void
export function getKiroSignInAction(): KiroSignInAction | null
```

- **One slot.** A second registration is a collision reported through
  `website/src/apps/seamCollision.ts` `reportSeamCollision`, as
  `registerOverviewPanel` does: it throws in dev and test, and in production it
  warns and the second action is ignored.
- **No-op when empty.** `getKiroSignInAction()` returns `null` in the stock build,
  and the card renders nothing extra.
- **Where it renders.** In `KiroPrerequisiteGate.tsx`, inside the first-run card's
  existing `status.installed && !status.authenticated` branch, directly under
  `SignInCommands`, so the copyable commands stay first and the action is an
  addition, never a replacement.
- **What it is given.** The two command strings the card already shows, and
  `recheck`. Nothing else: no token, no store path, no gateway credential.
- **The tier choice stays explicit.** `SignInCommands` shows two commands because a
  user on an organisation plan who signs in with the personal one succeeds and only
  finds out later, as missing models. An action must therefore let the person
  choose between the two, the way the card does; the seam's doc comment says so,
  and an action that offers a single button is out of contract.
- **Owner only.** The branch it renders in is already the owner's setup card; a
  viewer sees the "owner must finish setup" screen and never the action.
- **Failure.** Phase 1 wraps the action in its own small error boundary, so an
  action that throws renders nothing and the commands stay usable.

### After the action

Nothing new. The action may open a terminal or a helper; the user signs in there;
Kiro CLI writes its own store. The card's Check again, or the 5 s poll while the
first-run gate blocks, sees `whoami` succeed and lifts the gate exactly as for a
terminal sign-in. On first run no chat has run yet, so no work is held.

## Migration plan

### Phase 1 — the seam (one PR)

`kiroSignInAction.ts`, the render site in `KiroPrerequisiteGate.tsx`, an entry in
the seam catalogue `website/docs/extension-seams.md` and the registration comment
in `website/src/extensions.ts`, and tests.

**Exit criteria:**
- With nothing registered, the first-run sign-in card renders the same DOM as
  today.
- With one action registered, it renders under `SignInCommands` and receives the
  card's `login_command`, `sso_login_command` and a working `recheck`.
- A second registration throws in test (the dev/test branch of
  `reportSeamCollision`).
- An action whose `render` throws leaves the commands on screen.
- A viewer's screen never renders the action.
- `extension-seams.md` lists the seam.

No edition registers anything in this repository, so the shipped product is
unchanged.

### Phase 2 — none in this repository

An edition that wants a button registers it in its own code. This RFC does not
propose a stock action.

## Test plan

Vitest component tests beside the existing gate tests, one per exit criterion above.
No backend tests: the backend is unchanged.

## Backward compatibility

Compatible: with nothing registered the card is unchanged, and no API, payload or
config key changes.

## Security considerations

The threat this RFC must not reopen is the one `cb1a54371` closed: the core
spawning a login, scraping a URL from a child, and writing the user's credential
store.

- The core still spawns only the two read-only probes. The seam adds no route and no
  process.
- The action receives the two command strings the owner can already read and copy
  on the same card, and a `recheck` that can already be pressed. It receives no
  token, no store path and no gateway credential, so registering an action grants an
  edition nothing it did not already have.
- Code that registers an action is edition code running in the same SPA bundle, the
  same trust level as `registerTopBarWidgets` and `registerOverviewPanel`.
  Phase 1 does not export the seam to the app SDK, so an app installed from the App
  Store cannot register an action.
- The action renders only on the owner's setup card.

## Alternatives considered

**A. Run `kiro-cli login` from the core again (an in-app "Sign in" button).** This
is #15346's original suggestion and the most direct answer. It is rejected because
it reverses `cb1a54371`, in which bolichen97 removed exactly that: the
`/api/kiro-prerequisite/login` route, the `--use-device-flow` spawn, the URL
extraction and its host allowlist, so that the sign-in and the credential store
belong to Kiro CLI and the core only detects the result. Bringing it back would need
that maintainer to reverse their decision; this RFC does not ask for that.

**B. A standing sign-in status line in the default edition.** Rejected because
`f0c782e033` (#820) removed the persistent reauthentication banner on purpose:
readiness is latched at boot and is never fresh enough to show as a standing state,
so a banner either nags after a terminal sign-in or hides a real sign-out. The chat
error card is the sign-out signal by design (the comment above `app()` in
`KiroPrerequisiteGate.tsx`).

**C. Do nothing.** The core already does what it should for first run. But an
edition that can do better (a packaged desktop app) has no place to put the button,
so the step most likely to stop a first-time desktop user stays a terminal paste.

## Open questions

1. Mid-use sign-out on the native Kiro CLI backend: should its error card link to a
   sign-in card the way KAS's does, and should a successful re-check release the
   queue hold instead of waiting for the next send? Both change shipped behaviour
   in the error card and the queue, so they belong in their own RFC; this one does
   not depend on the answer.
2. Should the slot also render on the LTS sign-in screen (`LtsSignInRequired` in
   `website/src/components/KiroPrerequisiteSetupShell.tsx`)? This RFC says no: LTS
   has its own sign-in owner.
3. Is one slot enough, or do editions need an ordered list? One slot matches
   `registerOverviewPanel`; a list can be added later without breaking it.
