# OSS fork boundaries

This repo is the de-Amazoned public fork of an internal package. The scrub is not a
one-time cleanup: an upstream sync, a copied snippet, or a well-meaning "restore the
missing module" commit can put an internal coupling back, and most of them fail
silently in a public build rather than loudly. This doc is the list of what must never
come back, what is deliberately inert, and where the fork's UX diverges on purpose.

Why each internal system was removed, and the pre-launch removals still pending, are
[post-launch-removals.md](post-launch-removals.md) — that file is a dated ledger of
migration scaffolding, this one is a standing boundary.

## Never re-add

- **Build and infra.** Brazil (`Config`; the root `AUTOSDE.yaml` is NOT this and is
  live), `CODE_APPROVERS.yaml`, `npm-pretty-much`, the toolbox bundler, AIM hooks,
  CodeArtifact registries. The public build is setuptools plus public PyPI and public
  npm, and `.npmrc` deliberately pins no registry so the system-configured one applies.
- **Services and auth.** Enterprise SSO, MCS, Kerberos, federated login, device-posture
  tunnels, Cognito pools and RUM app ids, builder-mcp, `arcc`, Quip, internal
  ticketing. The internal marker names are scrubbed from code, comments and docs.
- **Removed product surfaces.** Internal feature-app pages, tabs, API-client methods
  and the credential-TTL card were deleted together with their backend. A downstream
  edition re-adds them **additively** through the extension seams, never by editing
  core.
- **Other providers.** Kiro Crew is KiroACP-only: `agent.provider` is fixed to `acp`
  (`enum=["acp"]`) and kiro-cli is REQUIRED. A second harness is selected at
  `agent.acp_backend` and adapted, never added as a second `agent.provider` value,
  because a second provider would route around every harness-parity invariant.
  Amending this rule is what
  [rfc-pluggable-model-providers.md](../request-for-change/rfc-pluggable-model-providers.md)
  asks for; until it is accepted the boundary holds.

## Deliberately inert, and it stays that way

These are live modules whose public implementation does nothing. They hold the import
graph and the seam, so a caller keeps importing and awaiting the same symbols; an
enterprise companion package composes a real provider behind them. Do not "finish" them
and do not delete them.

| Where | What it is |
|---|---|
| `sso_status.py` | No-op stubs so the dashboard and the Slack handlers keep the same symbols. Reports `available: False`. |
| `dashboard/handlers/sso_login.py` | Keeps the `api_sso_login_ws` symbol routed in `dashboard/server.py`, accepts the WebSocket, reports unavailable, closes. Spawns no subprocess. |
| `tunnel/manager.py` | A thin wrapper that delegates lifecycle **unconditionally** to `current_context().tunnel`. The public core ships `DefaultTunnelProvider`, a no-op, so there is deliberately no edition branch in the manager itself. |
| `website/src/rum.ts` | An inert no-op stub. Keep it inert; never add `aws-rum-web`. |

## OSS-flipped defaults

The public fork chooses different defaults, not different code paths. Do not flip them
back while syncing: always-on in-process embeddings, Piper TTS by default, a
default-open Slack enterprise gate, lazy STT extras.

## Fork UX divergences

- The **Channels** app is hidden from the App Store, and the **Board** app is removed.
  An upstream sync must not restore either.
- **The built-in app set is closed.** The `no-new-builtin-apps` rule in `AUTOSDE.yaml`
  blocks restoring those two and blocks any NEW built-in app: new apps ship as external
  apps through the KiroCrewApps registry.

## Keep the generic security controls

The scrub removes internal *identity*, never protection. AKIA/ASIA credential
redaction, the destructive-command deny rules, `~/.aws` and `~/.ssh` path blocking, and
the SEL audit log are all generic and all stay. The legacy `~/.kirocrew` spelling stays
in the sensitive-path deny lists for the reason given in
[post-launch-removals.md](post-launch-removals.md): nothing migrates or deletes a
legacy home any more, so dropping the spelling would un-gate real credentials still on
disk.

## The gate

`scripts/scrub-lint.sh`'s internal-marker pass scans `src/`, `website/src/`,
`website/docs/`, `docs/`, `skills/`, `scripts/`, `config/`, `packaging/` and the
top-level markdown. It deliberately skips `test/`, where the pattern hits hundreds of
legitimate lines; narrower ARCC and review-id passes cover that tree instead, and a
separate identity pass (personal paths and employee emails) does include it. Run the
script before pushing a sync.

The allowlist (`scripts/scrub-allowlist.txt`) carries the lines that must name a
removed system in order to forbid it. Most entries are scoped to one file and one
pattern, so a marker that escapes into a third file is still caught — but **`^docs/` is
a whole-tree exemption**, so this doc's own list passes and no internal marker anywhere
under `docs/` is gated. Keeping `docs/` clean is therefore a convention, not a gate.
