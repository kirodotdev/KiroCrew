---
title: KAS runs Kiro Crew's own engine when the kiro-cli relay cannot run
status: implemented
author: zezhexu
created: 2026-10-31
last-audited: 2026-11-03
audited-at: 1555bae5fa
doc-pr: 26682
implementation-prs: [26393]
tracking-issues: [26385]
supersedes: []
superseded-by: []
---

# RFC: KAS runs Kiro Crew's own engine when the kiro-cli relay cannot run

- Status: implemented. This records the product owner's decision of
  2026-10-30 (Zezhen), posted on
  [#26393](https://github.com/kirodotdev/KiroCrew/pull/26393#issuecomment-6533143760).
  It shipped in [#26393](https://github.com/kirodotdev/KiroCrew/pull/26393)
  (merged 2026-11-02). The shipped behaviour is owned by
  [acp-client.md](../system-specs/modules/acp-client.md), "Choosing KAS takes
  Crew's own engine when kiro-cli cannot run", and the decision record
  [2026-10-30](../decisions/2026-10-30-choosing-kas-runs-kiro-crews-own-engine-when-kiro-cli-cannot.md).
- Audit: claims about main were checked at `1555bae5fa`.

## Summary

Choosing KAS means Kiro Crew's own engine and Kiro Crew's own sign-in. When
the kiro-cli relay cannot run a session, a KAS spawn takes the bundle route
and installs the release's pinned engine if it is missing. The switch is
always logged and shown in the dashboard and in `kirocrew doctor`. It is
never silent.

## Motivation

Before #26393 the KAS route was chosen by `agent.kas_bundle` alone, and that
switch is off by default:

- `resolve_spawn` in `src/kiro_crew/acp/harness/kas.py` took the bundle
  route only when `agent.kas_bundle` was true, and otherwise always spawned
  kiro-cli's relay.
- The same `resolve_spawn` refused a spawn when
  `agent.kas_bundle` was on but no engine was installed.
- `kas_bundle_route` in `src/kiro_crew/agent_sdk/kas_bundle.py` answered the
  same switch for every other reader.

KAS is the default backend. So a new user with no working kiro-cli could not
start a chat ([#26385](https://github.com/kirodotdev/KiroCrew/issues/26385)):

- With no kiro-cli, the spawn failed with `kiro-cli not found`.
- With a kiro-cli launcher that cannot run the session (its own sign-in
  absent and no `kiro-cli-chat` beside it to use Kiro Crew's), the launcher
  asked for `kiro-cli login` even after the user signed in through Kiro Crew.

On main now (`1555bae5fa`) the route is `src/kiro_crew/acp/kas_route.py`:
`resolve_spawn` and `kas_bundle_route` both read it, and the bundle route
installs a missing pinned engine (`src/kiro_crew/acp/kas_engine_install.py`)
instead of refusing.

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
4. kiro-cli resolves and is not the operator's `KIROCREW_KIRO_BIN`, its own
   sign-in is proven absent, and Kiro Crew's sign-in cannot stand in for it:
   the bundle route. A signed-in kiro-cli that cannot run the v3 engine is
   not covered here; it stays on the relay and is tracked in
   [#26629](https://github.com/kirodotdev/KiroCrew/issues/26629).
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
- `kirocrew doctor` prints the route and its reason in its `route:` row.
  `kirocrew kas route` prints which program owns the sign-in.
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
