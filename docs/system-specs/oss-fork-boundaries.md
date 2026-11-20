# OSS fork boundaries

This repo is the de-Amazoned public fork of an internal package. The scrub is not a
one-time cleanup: an upstream sync, a copied snippet, or a well-meaning "restore the
missing module" commit can put an internal coupling back, and most of them fail
silently in a public build rather than loudly. This doc is the list of what must never
come back, what is deliberately inert, and where the fork's UX diverges on purpose.

[post-launch-removals.md](post-launch-removals.md) lists the migration scaffolding to
delete once no legacy data can exist — today only the legacy `~/.kirocrew`
security-path spelling. This file is a standing boundary.

## Never re-add

- **Build and infra.** Brazil (`Config`; the root `AUTOSDE.yaml` is NOT this and is
  live), `CODE_APPROVERS.yaml`, `npm-pretty-much`, the toolbox bundler, AIM hooks,
  CodeArtifact registries. The public build is setuptools plus public PyPI and public
  npm, and `website/.npmrc` deliberately pins no registry so the system-configured one
  applies.
- **Services and auth.** The public build bundles no Enterprise SSO, MCS, Kerberos,
  federated login, device-posture tunnels, Cognito pools or RUM app ids, no
  internal developer-tooling MCP servers, and no internal wiki or ticketing
  integration. The authoritative list of markers is the private one the gate below
  fetches, not a copy kept here. Generic compatibility
  paths may still preserve user-supplied MCP server names, and optional skills may name
  an unavailable external governance source; neither is a bundled integration.
- **Removed product surfaces.** Internal feature-app pages, tabs, API-client methods
  and the credential-TTL card are not in the public build. A downstream edition re-adds
  them **additively** through the extension seams, never by editing core. The
  credential-TTL card's backend route stays as an inert seam: `GET /api/sso-ttl`
  (`handlers_system.api_sso_ttl`) answers the identity seam's status, which is
  unavailable in the public build, and no public frontend calls it.
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
| `dashboard/handlers/sso_login.py` | Keeps the `api_sso_login_ws` symbol that `dashboard/routes/connections.py` routes at `/api/sso-login` when `DashboardContributor.sso_login_handler()` returns `None`; accepts the WebSocket, reports unavailable, closes. Spawns no subprocess. |
| `tunnel/manager.py` | A thin wrapper that delegates lifecycle **unconditionally** to `current_context().tunnel`. The public core ships `DefaultTunnelProvider`, a no-op, so there is deliberately no edition branch in the manager itself. The one public provider is the Kiro relay client (`RelayTunnelProvider`), which `platform/bootstrap.py` composes in its place only when `edition_locks.relay_selected` says `tunnel.relay_enabled` is on. Unset (the default) is on only for a team Crew or a host with a workload identity; a personal sign-in stays off until `kirocrew remote-control on`; an LTS build ignores the switch. `DefaultTunnelProvider` itself stays a no-op. |
| `website/src/rum.ts` | An inert no-op stub. Keep it inert; never add `aws-rum-web`. |

## OSS-flipped defaults

The public fork chooses different defaults, not different code paths. Do not flip them
back while syncing: always-on in-process embeddings, the host `system` TTS provider
(`speech_providers.DEFAULT_PROVIDER`, re-exported by `voice_reply`) by default, a default-open Slack enterprise gate, lazy
STT extras.

## Fork UX divergences

- **Channels** stays out of sight until someone turns it on: it is a core page behind
  `dashboard.channels_enabled` (default off), not an App Store app. Its Settings row
  (`website/src/pages/settings/ChannelsSetting.tsx`) is drawn only while the setting
  is on, and off hides only the sidebar row and the Search Everywhere result. There
  is no **Board** app. An upstream sync must not restore a Board app or a Channels
  app, nor offer Channels where it is not offered.
- **The built-in app set is closed, and it is being emptied.** Kiro Crew accepts no
  new built-in app: every app, first-party included, lives in a dedicated GitHub
  repository listed in the official registry (`kirodotdev/KiroCrewApps`). The
  built-ins that still ship move out one at a time without changing anything for the
  people who already use them
  ([decision](../decisions/2026-11-01-kiro-crew-accepts-no-new-built-in-apps.md)).
  The `no-new-builtin-apps` rule in `AUTOSDE.yaml` blocks re-adding an app that has
  moved out and blocks any NEW built-in app, per-app core table row, per-app frontend
  registry entry, or app-specific core MCP tool.

## Keep the generic security controls

The scrub removes internal *identity*, never protection. Credential
redaction, the destructive-command deny rules, `~/.aws` and `~/.ssh` path blocking, and
the SEL audit log are all generic and all stay. The legacy `~/.kirocrew` spelling stays
in the sensitive-path deny lists for the reason given in
[post-launch-removals.md](post-launch-removals.md): nothing migrates or deletes a
legacy home any more, so dropping the spelling would un-gate real credentials still on
disk.

## The gate

`.github/workflows/internal-content-scan-gate.yml` invokes the reusable
`.github/workflows/internal-content-scan.yml` for same-repository `pull_request`
(to `main` and `release/**`), `merge_group`, and pushes to `main`. It checks **only the lines a change adds**,
against a marker list that
is deliberately **not in this repo** — it lives in a private bucket and is fetched
per run over GitHub's OIDC identity, with no long-lived AWS keys anywhere.

Three things follow from that, and they are the point rather than side effects:

- **Pre-existing content is out of scope.** You are never asked to clean up
  someone else's line to land yours, and adopting the gate needed no repo-wide
  cleanup first.
- **A list kept outside the repo it polices cannot be read off to find out what to
  avoid writing.** A wordlist in this repo would also drift unseen, because nothing
  checks it against the private ruleset. Do not add wordlists to `scripts/`.
- **A false positive is fixed by fixing the rule**, not by adding yourself to an
  exemption file — there isn't one. The rule lives in a private package; say so on
  the PR and it gets fixed at the source.

Reading a failure:

```
docs/foo.md:42:15: [internal-domain-amazon] see https://<internal-wiki-host>/SomePage
```

Path, line, column, the rule id, and your own added line. The real output shows
the host verbatim; it is redacted here so this file does not carry the thing it
warns about.

Exit 1 means remove the marker from your change. Exit 2 means the gate could not
reach a verdict — a broken ruleset, or a diff that was not intact. That is not
your change's fault and not something to retry past. It fails the build on
purpose: a scan that reaches no conclusion must never be read as a pass.

### The built-artifact scan is a different check

`scripts/verify_no_internal_host.py` runs in `build.yml` and `build-wheel.yml`
against the BUILT wheel and website `dist/`, not against a diff. It answers a
question the gate above cannot: does a file that ships to users name an internal
host, whenever and however it got there.

It keeps its denylist as sha256 digests of a few host and package names, not as
a readable wordlist, so the rule above still holds: nothing in `scripts/` can be
read off as a list of what to avoid writing. The digests are unsalted hashes of
short names, so anyone who already suspects a name can confirm it by hashing;
they keep the names out of the text, they do not make them secret. The private
ruleset stays the authority for the diff gate.

`EXCLUSIONS` there is not an exemption from the gate. It names only shipped
files whose job is to DETECT internal hosts, each with a reason and the exact
digests that file may contain; any other denied host in the same file still
fails the build.

### Three ways a scan passes without scanning

The gate is built so that none of these can happen, and a change to it must keep
it that way:

- **A skipped pass reads as a pass.** A check that reads a file CI never has (an
  uncommitted alias list, say) prints `skipped` and exits 0 on every run. A missing
  input must fail the run, which is what exit 2 above does.
- **A history pass on a shallow checkout sees nothing.** On a `fetch-depth: 1`
  checkout `git log --all` sees one commit. The gate scans the change's added lines
  instead of walking history.
- **A wordlist in the public repo it polices** tells anyone exactly what not to
  write down. The marker list stays in the private bucket.

**How it blocks, and on which path.** The check is blocking: `PR Readiness` — the
one required status on `main` — reads it as a named lane, so an added internal
marker fails readiness and the PR cannot merge. It is deliberately *not* in the
branch-protection required-checks list; that list would need the reusable
workflow's composed check name (`scan / internal-content-scan`), and routing
through `PR Readiness` is how this repo already handles `Fast Gate`.

Two paths produce that one verdict, because a fork head receives no OIDC token:

| Pull request from | Workflow | Why |
|---|---|---|
| this repository | `internal-content-scan-gate.yml` on `pull_request` | a same-repo PR does get an OIDC token |
| a fork | `fork-internal-content-scan.yml`, Stage 2 | runs privileged from the default branch after `Fast Gate`, never checks out or executes fork code, and posts a check-run under the same `Internal Content Scan` name |

`push` to `main` stays as the backstop for anything reaching the branch without a
pull request. `merge_group` scans the merge queue's temporary commit, the tree that
actually lands on `main`; `merge-queue-readiness.yml` waits on it alongside CI, Fast
Gate and Build, so a red there holds the group as it holds a pull request.

Two lanes are marked ineligible rather than pending, the same treatment CodeQL
gets: a hand-made **stacked** PR (base is another feature branch) never starts the
`branches:`-filtered workflow (a PR in a GitHub-native stack on `main` does run
it, and readiness counts it), and a fork PR's same-repo lane is skipped in favour
of the Stage-2 one above.

**Why the `pull_request` trigger is load-bearing.** A `push` run on `main` sees
content only after it is public, and post-push detection on a public repository is
post-disclosure. The `pull_request` run is what reaches a verdict before the change
lands; do not remove it in favour of the `push` backstop.
