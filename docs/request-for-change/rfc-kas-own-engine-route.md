---
title: KAS runs Kiro Crew's own engine when the kiro-cli relay cannot run
status: accepted
author: zezhexu
created: 2026-10-31
last-audited: 2026-10-31
audited-at: 76bf6e7c4e
doc-pr: null
implementation-prs: [26393]
tracking-issues: [26385]
supersedes: []
superseded-by: []
---

# RFC: KAS runs Kiro Crew's own engine when the kiro-cli relay cannot run

- Status: accepted. This records the product owner's decision of 2026-10-30
  (Zezhen), posted on
  [#26393](https://github.com/kirodotdev/KiroCrew/pull/26393#issuecomment-6533143760).
  It is not yet the code; the implementation is
  [#26393](https://github.com/kirodotdev/KiroCrew/pull/26393).
- Audit: claims about main were checked at `76bf6e7c4e`.

## Summary

Choosing KAS means Kiro Crew's own engine and Kiro Crew's own sign-in. When
the kiro-cli relay cannot run a session, a KAS spawn takes the bundle route
and installs the release's pinned engine if it is missing. The switch is
always logged and shown in the dashboard and in `kirocrew doctor`. It is
never silent.

## Motivation

On main the KAS route is chosen by `agent.kas_bundle` alone, and that switch
is off by default:

- `resolve_spawn` in `src/kiro_crew/acp/harness/kas.py` takes the bundle
  route only when `agent.kas_bundle` is true, and otherwise always spawns
  kiro-cli's relay.
- The same `resolve_spawn` refuses a spawn when
  `agent.kas_bundle` is on but no engine is installed.
- `kas_bundle_route` in `src/kiro_crew/agent_sdk/kas_bundle.py` answers the
  same switch for every other reader.

KAS is the default backend. So a new user with no working kiro-cli could not
start a chat ([#26385](https://github.com/kirodotdev/KiroCrew/issues/26385)):

- With no kiro-cli, the spawn failed with `kiro-cli not found`.
- With a kiro-cli launcher that cannot run the session (for example no
  `kiro-cli-chat` beside it, or a kiro-cli without the KAS engine), the
  launcher asked for `kiro-cli login` even after the user signed in through
  Kiro Crew.

## Goals

- A KAS spawn on a host where the relay cannot run uses Kiro Crew's own
  engine and sign-in, with no setting to flip.
- The user always learns that the route switched, and why.

## Non-goals

- Changing the route on a host where the relay works. That host keeps the
  relay exactly as today.
- Moving a stored Kiro CLI pick (`agent.acp_backend: "kiro"`) off kiro-cli.
- Second-guessing an operator's `KIROCREW_KIRO_BIN`. That binary keeps the
  relay.
- Installers and update paths.
- Moving `agent.kas_bundle` out of Feature Previews.

## Design

One route rule, evaluated per spawn:

1. `agent.kas_bundle` on: the bundle route.
2. A stored Kiro CLI pick: the relay.
3. No kiro-cli resolves: the bundle route.
4. kiro-cli cannot run the KAS engine, or its own sign-in is proven absent
   and the vault cannot reach the engine through it: the bundle route.
5. Anything unknown: the relay. A failed probe never switches the route.

On the bundle route a missing engine is installed, not refused:

- Only the release's pinned engine installs unattended. A version picked with
  `KIROCREW_KAS_VERSION` is refused with its own install command.
- One install runs at a time, and none runs inside an agent sandbox.
- A failed install names the cause and `kirocrew kas install`.

The switch is never silent:

- An automatic route change is logged at WARNING.
- The dashboard shows a notice while the engine downloads, and on failure
  offers Retry.
- `kirocrew doctor` and `kirocrew kas route` print the route and its reason.
- Sign-in text on the KAS backend names only the Kiro sign-in.

## Backward compatibility

Hosts where the relay runs see no change. `agent.kas_bundle: true` keeps the
bundle route, and now installs a missing engine instead of refusing.

## Security considerations

The new behaviour downloads code without a prompt. It is limited to the
release's own pinned engine, through the same install path
`kirocrew kas install` already uses, and is refused inside an agent sandbox.
No other version installs unattended.

## Alternatives considered

- **Keep `agent.kas_bundle` as the only switch.** Rejected: the default
  backend stays broken for every user without a working kiro-cli.
- **Ask before installing the engine.** Rejected by the owner's decision: the
  user chose KAS, and the switch is shown, not hidden.
- **Send the user to install and sign in to kiro-cli.** Rejected: Kiro Crew
  offers its own sign-in, and that sign-in must work on its own.

## Open questions

None.
