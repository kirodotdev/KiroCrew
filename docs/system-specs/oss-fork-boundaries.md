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

`.github/workflows/internal-content-scan.yml` runs on `merge_group` and on pushes
to `main`. It checks **only the lines a change adds**, against a marker list that
is deliberately **not in this repo** — it lives in a private bucket and is fetched
per run over GitHub's OIDC identity, with no long-lived AWS keys anywhere.

Three things follow from that, and they are the point rather than side effects:

- **Pre-existing content is out of scope.** You are never asked to clean up
  someone else's line to land yours, and adopting the gate needed no repo-wide
  cleanup first.
- **A list kept outside the repo it polices cannot be read off to find out what to
  avoid writing.** The gate this replaced hardcoded its wordlists here, which is
  also how they drifted for months without anyone noticing. Do not add wordlists
  back to `scripts/`.
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

### What it replaced, and why that one did not work

The two files that gate used to live in — scripts/scrub-lint.sh and its
allowlist — **no longer exist in this repo**. Their names are written here without
backticks on purpose: the docs linter reads a backticked repo path as a citation of
live code and rightly fails on one that resolves to nothing. That gate was vacuous
in three independent ways, and the third is why the wordlists could not simply be
moved:

- Its alias pass read `scripts/.scrub-aliases.txt`, a file deliberately never
  committed, so in CI it printed `skipped` — and a skip counted as a **PASS on
  every run since the check was written**.
- CI invoked it with `--no-history` on a `fetch-depth: 1` checkout, where
  `git log --all` sees one commit, so the history pass was a no-op.
- Its wordlists lived in the public repo they were meant to police. Anyone could
  read them to learn precisely what not to write down.

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
pull request. `merge_group` is declared but fires zero times — this repository has
no merge queue — and is kept only because it is the trigger a queue would use.

Two lanes are marked ineligible rather than pending, the same treatment CodeQL
gets: a **stacked** PR (base is another feature branch) never starts the
`branches:`-filtered workflow, and a fork PR's same-repo lane is skipped in favour
of the Stage-2 one above.

**This was fixed the hard way.** The gate shipped without a `pull_request` trigger,
and within hours the `push` run caught real internal content — an internal ticket
id and an internal workplace path — that had already reached public `main` in a
merged PR. Post-push detection on a public repository is post-disclosure. That
window is what the trigger above closes.
