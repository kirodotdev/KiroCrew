---
title: Crew Enterprise Edition -- a build-time edition that requires login, fetches governance rules before it opens, and turns every governable feature off by default
status: accepted
author: iamwhatever, with kirocrew-worker
created: 2026-10-22
last-audited: 2026-10-29
audited-at: a7ecdf3f1
doc-pr: null
implementation-prs: []
tracking-issues: [23206]
supersedes: []
superseded-by: []
---

# RFC: Crew Enterprise Edition

- Status: accepted. The owner approved this design on 2026-10-23; D1 to D5 in section 5 are owner decisions (D4 and D5 added 2026-10-29). This document landed first so the implementation pull requests that follow have a design for the First Principles and Design lanes to check against: the enterprise edition changes defaults (every governable feature off) and adds a startup gate.
- Tracking issue: [#23206](https://github.com/kirodotdev/KiroCrew/issues/23206)
- Related: [rfc-central-governance-ceiling](rfc-central-governance-ceiling.md) (the policy ceiling this edition fetches and enforces), [platform-context](../system-specs/modules/platform-context.md) (the existing edition seam), [governance](../system-specs/modules/governance.md) (the evaluator and catalog this edition tightens)

## Summary

Kiro Crew gains an enterprise edition built from the same codebase. The build picks the edition (`make desktop EDITION=enterprise`, default `oss`) and writes it into the package as a constant. At run time the enterprise edition will not open until the user has a kiro login and Crew has fetched the organisation's governance rules. With no login it opens only a sign-in screen; any other failure makes Crew exit (amended 2026-10-29). Every action is then checked against those rules, and every governable feature is off by default or keeps only a minimal set until a rule turns it on. The `oss` edition is unchanged.

## Motivation

Measured at `5c32cf32b`.

An organisation that wants to run Crew today can install a governance ceiling ([rfc-central-governance-ceiling](rfc-central-governance-ceiling.md), `src/kiro_crew/platform/policy_distribution.py`), but three things keep that from being a managed product:

1. **Nothing requires a login or a policy before Crew opens.** Crew starts and serves its dashboard whether or not a kiro login or a governance ceiling exists. The login check in `src/kiro_crew/kiro_prerequisite_status.py` reports status; it does not gate startup.
2. **An unnamed control is allowed.** `_query_level` in `src/kiro_crew/platform/governance_parts/evaluator.py` returns `_PERMIT_NOT_GOVERNED` when the policy carries no control for a scope, so a policy that forgets a control leaves that feature fully on. That is the right default for a standalone install and the wrong one for a managed install.
3. **Many local shims have no governance key.** Of the 66 controls an organisation expects to govern (the appendix), several have only an on/off switch and many have no key in `src/kiro_crew/platform/governance_parts/catalog.py` or `carriers.py` at all, so no rule can reach them.

## Goals

1. Crew requires a kiro login before it opens the dashboard (amended 2026-10-29, D4 and D5). With no login it opens only a sign-in screen; every other startup failure makes it exit.
2. Most local shims are turned off. Governance controls the real behaviour.
3. No new repository: one codebase builds both editions.

## Non-goals

- Changing the `oss` edition's behaviour or defaults.
- Defining the governance service's API (see Open questions).
- Adding governance keys for every control in this document. Controls without a key are hardcoded to their enterprise default until that key lands in its own pull request.

## Design

### 1. Edition is a build-time constant

The build picks the edition: `make desktop EDITION=enterprise`. The build writes the edition into the package as `src/kiro_crew/edition.py` (default `oss`), then packages and signs the app.

At run time, code reads only that built-in `EDITION` value. It never reads a config file or an environment variable for this, so neither a user nor an agent can switch an installed app's edition.

This is a different axis from the existing [platform-context](../system-specs/modules/platform-context.md) seam, which composes edition adapters into the core at run time. `EDITION` decides whether the enterprise gate and defaults apply; the seam still decides which adapters are composed.

### 2. Enterprise gate: login and governance are one gate

The identity from login is the key that fetches the rules. One sign-in resolver (D4) gives that identity to the login check, the rules fetch and the account lock.

```mermaid
flowchart LR
    S[start] --> L[resolve sign-in]
    L -->|ok| F[fetch governance rules]
    L -->|no sign-in| G[sign-in-only gateway]
    G -->|signed in| F
    L -->|other failure| X1[exit]
    F -->|ok| O[open]
    F -->|fail| X2[exit]
```

| When | What the gate does |
|---|---|
| Start | Resolve the sign-in, then fetch rules. No sign-in: open the sign-in-only gateway (D5). Any other failure: exit |
| Every action | Check the rules, then allow or deny |
| Login expires | Close the gate and log in again |
| Rules change | Re-fetch on the policy's `crew_policy_refresh_seconds` |

Any failed step denies (fail-closed). While the gate is closed, gateway middleware denies every route outside the sign-in allow-list: sign-in, health and static routes (D5).

### 3. Every governable feature off by default

In the enterprise edition, every governable feature is off by default, or keeps only a minimal set. A feature turns on only when a governance rule turns it on. The appendix lists all 66 controls with each one's default.

| Class | Count | Enterprise behaviour |
|---|---|---|
| Green | 33 | Off by default; governance turns it on |
| Yellow | 20 | Off by default. Until the Crew work lands, minimal set only |
| Red | 13 | Governance cannot control it; hardcoded to the minimal set |
| Local floor | 2 | Always on; see below |

For controls that already have a key, "off by default" comes from one change: in the enterprise edition `_query_level` denies a control the policy does not name, where today it permits; the built-in baseline policy keeps each minimal set (D3). For controls with no key yet, the enterprise edition reads `EDITION` at the feature's entry and turns it off.

The local floor is Crew's own protection, not a governance rule:

- Credential file protection: the agent cannot read or write files such as `~/.aws` and `~/.ssh`.
- No self-disable: the agent cannot turn the protections above off.

### 4. Tamper resistance

- The edition is set at build time, not read from config.
- The install directory is on the write-protected list, so the agent cannot change it.
- The app is signed. A changed package breaks the signature.

The browser (wheel) build can carry the same `EDITION` stamp, but its tamper resistance is weaker: a wheel has no signature, and it installs into a user-writable `site-packages`, so a user can edit `edition.py` in place. The signed desktop app is the edition this design defends.

### 5. Decisions

The repository owner decided D1 to D3 on 2026-10-23. They hold in an enterprise build only; the `oss` edition is unchanged.

**D1. One enterprise fact.** The build-time `EDITION` is the only source for "is this enterprise". In an enterprise build, `resolve_profile` in `src/kiro_crew/platform/profile.py` returns the enterprise profile and ignores the `KIROCREW_PROFILE` environment variable, the SSO-marker probe and the installed `kirocrew.plugins` entry points, all of which it reads today. `PlatformContext.is_enterprise` stays the one property callers read. Consequence (amended 2026-10-28, see the decision log below): an enterprise build with no companion installed boots on the core defaults under the enterprise profile, so every enterprise gate (login, governance fetch, deny-unnamed, edition locks) stays on. An installed companion that is broken, rejected at admission, ambiguous or contract-mismatched still fails closed (the `PlatformCompositionError` path in `src/kiro_crew/platform/discovery.py` and `src/kiro_crew/platform/bootstrap.py`). No environment variable or config changes the profile or the edition; `KIROCREW_PROFILE=standalone` still does not escape it.

**D2. Locked policy source.** In an enterprise build, the governance policy URL is a build-time constant. The `POLICY_URL_ENV` environment variable (`KIROCREW_POLICY_URL`, defined in `src/kiro_crew/platform/policy_distribution_source.py`) and local config cannot change it. The address is public, so it lives in the repository. Until the real endpoint is chosen it is the placeholder `https://governance.example.invalid/crew/policy`, on a reserved domain that never resolves; it is replaced before the enable step (Migration plan phase 2). Testing against a different address needs its own build; local environment cannot do it.

**D3. No local loosening.** Remote rules are the only thing that can turn a control on. A control the remote policy does not name is locked to its minimal set, or off when it has no minimal set. Local config, environment variables, profiles and dashboard settings cannot widen any control in an enterprise build; they may only narrow it further. A built-in baseline policy holds each control's minimal set (the appendix "Minimal set" column), and enterprise `_query_level` denies anything outside it. This closes the earlier open question on how deny-unnamed keeps each minimal set.

| Source | Can widen | Can narrow |
|---|---|---|
| Remote policy | yes | yes |
| Built-in baseline | minimal set only | — |
| Local config | no | yes |
| Environment variables | no | yes |
| Profiles | no | yes |
| Dashboard settings | no | yes |

The owner added D4 and D5 on 2026-10-29, after the enterprise smoke test (see the decision log below).

**D4. One sign-in.** In an enterprise build, one resolver answers "who is signed in". The login check at start, the rules fetch and the account lock (the profile pin from [#24661](https://github.com/kirodotdev/KiroCrew/pull/24661)) all read it, so they can never name different accounts. It accepts two sign-ins, in this order:

| Order | Sign-in | Written by |
|---|---|---|
| 1 | Crew's own vault | `kirocrew kas login`, or the dashboard's Sign in |
| 2 | kiro-cli's own sign-in | `kiro-cli login` |

The vault is preferred because kiro-cli will be removed later; kiro-cli's sign-in is accepted until then. The source is picked by presence (amended 2026-10-30): when the vault holds an entry, the vault is the only source, and a lapsed vault entry is not signed in rather than a reason to read kiro-cli; when it holds none, kiro-cli's sign-in is the only source. Crew does not renew kiro-cli's token, so an expired one is not signed in. Step 2 reads kiro-cli's own credential store, the one the KAS relay already uses when the vault holds no identity; it is not the `kiro-cli whoami` probe. When both hold a sign-in with different profile ARNs, Crew refuses and names both accounts. When neither holds one, the message names both ways to sign in (`kirocrew kas login` and `kiro-cli login`).

The `oss` edition reads the same resolver with the same order (amended 2026-10-30): when the vault holds an entry, the vault is the only source, and a lapsed or unreadable entry is not signed in (the message names `kirocrew kas login --provider idc`, and says that removing the Crew sign-in with `kirocrew kas logout` hands over to kiro-cli's); when it holds none, kiro-cli's sign-in is used. The two are never mixed. Two differences from the enterprise build: two stored sign-ins on different accounts use the vault and log one warning naming both, instead of refusing; and a sign-in with no profile (a Builder ID) is accepted. The KAS spawn, `kirocrew doctor` and the Settings sign-in status in `oss` all read this pick.

Each refusal names one real cause, from this list: both sign-ins disagree, governance service unreachable, rules that cannot be applied, account not eligible for enterprise management, or account changed while running. The account lock pins the first sign-in the resolver returns, at start or after a sign-in in the sign-in-only gateway (D5), and compares every later answer with that pin.

**D5. Signed-out start opens a sign-in screen.** When the resolver finds no sign-in at start, an enterprise build does not exit. It opens a sign-in-only gateway bound to `127.0.0.1`. That gateway keeps the dashboard's token auth, serves an allow-list of sign-in, health and static routes, refuses every other route, and shows a sign-in screen on web and desktop naming both ways to sign in. After the user signs in, the resolver runs again with D4's rules, then the rules fetch runs. A refusal from either still exits with its named cause. A failure from D4's cause list at start exits as before and never opens this gateway.

**Decision log.**

- 2026-10-28: the owner amended D1's consequence. Why it changes: (1) on 2026-10-23 D1 made a missing companion fail closed because the governance fetch was then planned to live in a companion package; (2) on 2026-10-27 the owner moved the control-plane policy fetch into this repository, reusing the existing control-plane client, and it merged as [#24661](https://github.com/kirodotdev/KiroCrew/pull/24661); (3) every enterprise gate (the login gate, the policy fetch that exits on failure, deny-unnamed, local-only-stricter and the edition locks) now lives in the core behind the build-time `EDITION`, so a companion supplies nothing the enterprise edition needs and the D1 refusal only stops enterprise builds from starting. New consequence: an enterprise build with no companion boots on the core defaults with every enterprise gate on. What does not change: a host with a companion installed takes the same path as before; a broken, rejected, ambiguous or contract-mismatched companion still fails closed with `PlatformCompositionError`; no environment variable or config changes the profile or the edition; the `oss` edition is unchanged.
- 2026-10-29: the owner added D4 and D5. Why it changes: the enterprise smoke test found that (1) the login check at start read only kiro-cli's sign-in (`kiro-cli whoami`) while the rules fetch read only Crew's vault, so a user signed in through kiro-cli was refused and the two checks could name different accounts; (2) the rules were fetched before the login check ran; (3) a signed-out user got a generic message and no way to sign in, and the desktop app had no sign-in path at all. New consequence: the login check, the rules fetch and the account lock read one resolver, vault first because kiro-cli will be removed later (D4); a signed-out enterprise start shows a sign-in screen on `127.0.0.1` instead of exiting (D5). What does not change: D1 to D3; every other startup failure exits; the gate stays fail-closed; the `oss` edition is unchanged.
- 2026-10-30: the owner amended D4. Why it changes: in review of the code PR (#26100), the gate, the rules fetch and the account lock each picked the sign-in source on their own, by liveness, so a lapsed vault entry next to a live kiro-cli sign-in could make the fetch use one account while the lock saw another, refusing every start. New consequence: one selection, by presence, read by every check: a vault entry means the vault only, and a lapsed one is not signed in; no vault entry means kiro-cli's sign-in only. Crew does not renew kiro-cli's token at boot (follow-up #26183). What does not change: the vault is preferred; two stored sign-ins on different accounts are refused naming both; D5; the `oss` edition is unchanged. Delivery: the vault sign-in lands first (#26100); the kiro-cli fallback lands in its own change, written so the `oss` edition can reuse the same resolver.
- 2026-10-30: the owner extended D4's sign-in order to the `oss` edition. Why it changes: users moving from older Kiro Crew, which needed kiro-cli, must keep working on their kiro-cli sign-in without a new Crew sign-in (a migration path); and `oss` picked its KAS sign-in with its own rule, which quietly fell back from a lapsed or unreadable vault entry to kiro-cli's account, so the two sign-ins could mix. New consequence: `oss` reads D4's resolver: a vault entry means the vault only, and a lapsed or unreadable one is not signed in; no vault entry means kiro-cli's sign-in; two accounts use the vault with one warning. The old silent fallback from a lapsed vault entry to kiro-cli is removed on purpose. What does not change: the enterprise rules in D4, including the refusal of two accounts; D5; the kiro-cli bundle route, which has no kiro-cli and stays Crew-owned. Delivery: #26841 (issue #26829).

### 6. Changes

| Where | What changes |
|---|---|
| `src/kiro_crew/edition.py` | New `EDITION` constant, written by the build |
| Startup entry point | Enterprise edition passes the enterprise gate first |
| Gateway middleware | While the gate is closed, deny every route outside the sign-in allow-list (D5) |
| Sign-in resolver (new) | Vault first, then kiro-cli's own sign-in; read by the login check, the rules fetch and the account lock (D4). Replaces `kiro-cli whoami` (`src/kiro_crew/kiro_prerequisite_status.py`) as the enterprise login check |
| Sign-in-only gateway (new) | Signed-out start serves only sign-in, health and static routes on `127.0.0.1` (D5) |
| Sign-in screen, web and desktop (new) | Shown by the sign-in-only gateway; names both ways to sign in (D5) |
| Governance client (new) | Fetch, cache and query rules from the organisation's governance service |
| `src/kiro_crew/platform/profile.py` | `resolve_profile`: enterprise build returns the enterprise profile from `EDITION` alone (D1) |
| `src/kiro_crew/platform/policy_distribution_source.py` | Enterprise build uses the build-time policy URL; `POLICY_URL_ENV` and config are ignored (D2) |
| Built-in baseline policy (new) | Grants each control's minimal set beneath the remote policy (D3) |
| `src/kiro_crew/platform/governance_parts/evaluator.py` | `_query_level`: a control the policy layer does not name is allowed today. Enterprise edition denies anything outside the remote policy and the baseline (D3) |
| Local config, env, profile and dashboard settings readers | Enterprise build lets them narrow a control, never widen it (D3) |
| Features Crew has no key for yet | Enterprise edition reads `EDITION` at the feature entry and turns it off |
| `Makefile` | Add the `EDITION=enterprise` build |

## Migration plan

Each phase is one pull request and changes nothing in the `oss` edition.

1. **Edition stamp.** Add `edition.py`, the `EDITION=enterprise` build, the build-time policy URL placeholder and the `resolve_profile` change (D1, D2). Exit: an `oss` build and an enterprise build differ only in `edition.py`; a test asserts that no config key or environment variable changes `EDITION`; in an enterprise build `KIROCREW_PROFILE=standalone` still resolves the enterprise profile, boot with no companion composes the core defaults under the enterprise profile while boot with a broken or mismatched companion raises `PlatformCompositionError` (amended 2026-10-28), and setting `KIROCREW_POLICY_URL` does not change the policy URL.
2. **Enterprise gate.** Login check, governance fetch, middleware deny, expiry and refresh. The placeholder URL is replaced with the real endpoint before this phase enables the gate. Exit: with no sign-in, an enterprise build opens only the sign-in-only gateway (D5), and after sign-in it fetches the rules; with the governance fetch failing, or any other startup failure, it exits; the login check, the rules fetch and the account lock read the one sign-in resolver (D4); while the gate is closed every route outside the sign-in allow-list is refused; tests cover the disagreement refusal, the sign-in screen naming both commands, each refusal naming its cause, and a route outside the allow-list being refused (amended 2026-10-29). Blocked on the Open question.
3. **Deny unnamed controls, no local loosening.** Enterprise `_query_level` denies anything outside the remote policy and the built-in baseline policy (D3). Exit: an empty remote policy leaves each `yes` control at its appendix minimal set; a local config, environment, profile or dashboard setting that would widen a control has no effect, and one that narrows it does.
4. **Hardcoded defaults for controls with no key.** Exit: each `no` and Red control behaves as its appendix row in an enterprise build.

## Backward compatibility

The `oss` edition is the default and reads the same constant, so every existing build and install behaves as today. The enterprise edition is new and has no installed base.

## Security considerations

The gate is fail-closed: a missing sign-in opens only the sign-in-only gateway (D5); any other failed login, fetch or refresh denies. The sign-in-only gateway (D5) binds only `127.0.0.1` and serves only its allow-list, so a signed-out start exposes no other route. Two sign-ins that name different accounts refuse the start rather than picking one (D4). The edition, the profile and the policy URL cannot be changed by config or environment (D1, D2), no local source can widen a control (D3), and the desktop app's install directory is write-protected and signed. The wheel build's weaker tamper resistance is recorded in Design section 4. The local floor (credential files, no self-disable) stays on whatever the policy says.

## Alternatives considered

- **A separate enterprise repository or fork.** Rejected: Goal 3, and two codebases drift.
- **Edition from config or an environment variable.** Rejected: a user or agent could flip it.
- **Policy URL overridable by `KIROCREW_POLICY_URL` for testing.** Rejected (D2): a local override could point the build at a permissive policy. A test address gets its own build.

## Open questions

1. **The governance service API is not decided.** Endpoint, authentication (how the login identity is presented) and response format are TBD. The design names it only as the organisation's governance service, and the build carries the placeholder URL from D2 until the endpoint is chosen.

2. **A sign-in lost while Crew runs.** The table in Design section 2 says the gate closes and the user logs in again. Whether that reuses the sign-in-only gateway's screen is not decided (added 2026-10-29). Signing back in as a different account is refused by the account lock (D4).

How deny-unnamed keeps each control's minimal set was open in the first draft; D3 decides it with the built-in baseline policy.

## Appendix: control defaults

"In Crew": `yes` = the key already exists in `src/kiro_crew/platform/governance_parts/catalog.py` or `carriers.py`. `partial` = the on/off switch exists, the list does not. `no` = must be added.

`yes` controls become off by default through the `_query_level` change. `no` controls are not reachable by governance today; the enterprise edition hardcodes them to the "Enterprise default" column for now.

"Minimal set": what stays when no rule turns the control on. `—` means fully off.

### Green (33)

| Control | Policy term | Crew key | In Crew | Enterprise default | Minimal set |
|---|---|---|---|---|---|
| Chat services | `messaging_channel` | `channels` | yes | off | — |
| Agent-initiated messages | `messaging` | `capabilities.messaging` | yes | off | — |
| Agent harness | `harness_use` | `agent_backend` | yes | off | `kiro` |
| Approval modes | `permission_mode` | `approval_modes` | yes | off | `normal` |
| MCP servers | `mcp` | `mcp` | yes | off | `@kirocrew-core` |
| Theme persona | `theme_persona` | `capabilities.theme_persona` | yes | off | — |
| Theme install | `theme_install` | `capabilities.theme_install` | yes | off | — |
| Publish destinations | `artifact_publish` | `capabilities.publish` | yes | off | `default` |
| Deploy regions | `cloud_deploy` | `publish.regions` | no | off | — |
| Browser | `browser_use` | `capabilities.browse` | yes | off | — |
| Phone pairing | `mobile_connect` | `capabilities.mobile_connect` | yes | off | — |
| Tailscale access | `remote_connect` | `capabilities.tailnet_origin` | yes | off | — |
| Allowed tailnets | `crew_remote_tailnets` | `tailnet_origin.tailnets` | no | off | — |
| Durable memory writes | `memory_writes` | `capabilities.memory_writes` | yes | off | — |
| Scheduled jobs | `cron` | `capabilities.cron` | yes | off | — |
| Crew apps | `app_use` | `apps` | yes | off | — |
| App versions | `crew_app_versions` | `app_versions` | no | off | — |
| Sub-agents | `subagent` | `capabilities.spawn` | yes | off | — |
| Script hooks | `script_hook_execute` | `capabilities.script_hooks` | yes | off | — |
| Hook trigger events | `crew_script_hook_events` | `script_hooks.events` | no | off | — |
| Optional telemetry | `telemetry_export` | `capabilities.telemetry` | partial | off | — |
| Diagnostic telemetry target | `crew_telemetry_otlp_endpoint` | `telemetry.otlp_hosts` | no | off | — |
| Feature content download | `feature_content_download` | `capabilities.feature_videos_download` | partial | off | — |
| Feature content hosts | `crew_feature_content_hosts` | `feature_videos_download.hosts` | no | off | — |
| Share to sites | `social_share` | `capabilities.social_share` | partial | off | — |
| Decision providers | `decision_provider_use` | `capabilities.decisions` | partial | off | — |
| Decision models | `decision_model_use` | `decisions.models` | no | off | — |
| Decision kinds | `decision_point_use` | `decisions.points` | no | off | — |
| Incoming webhooks | `webhook_use` | `webhooks` | no | off | — |
| App developer mode | `developer_mode` | `app_dev_mode` | no | off | — |
| Update source | `crew_update_source` | `updates.source` | yes | official source only | official source |
| Minimum version | `crew_update_min_version` | `updates.min_version` | yes | not set | — |
| Update channel | `crew_update_channel` | `updates.channel` | no | stable | stable |

### Yellow (20)

| Control | Policy term | Crew key | In Crew | Enterprise default | Minimal set |
|---|---|---|---|---|---|
| Tools | `tool` | `tools` | yes | off | read-only tools |
| Shell commands | `shell` | `commands` | yes | off | — |
| File read / write | `fs_read` / `fs_write` | `filesystem.read` / `write` | yes | off | read: inside the workspace. write: — |
| Reachable hosts | `web_fetch` | `network.egress` | yes | off | — |
| Extension install | `extension_install` | `capability_install` | no | off | — |
| Extension sources | `crew_extension_registry_hosts` / `_local_sources` | `external_access.registry_hosts` | no | off | — |
| Extension publishers | `crew_extension_publishers` | `capability_install.publishers` | no | off | — |
| Extension uninstall | `extension_uninstall` | `capability_install` | no | off | — |
| Plugin sync | `plugin_sync` | `capability_install` | no | off | — |
| Auto-approval duration | `crew_auto_approval_max_seconds` | `yolo_max_seconds` | no | off | — |
| Slack organisations | `messaging_posture` | `channels.posture` | yes | off | — |
| Slack channels | `crew_slack_allowed_urls` | `channels.posture.slack.allowed_channel_ids` | no | off | — |
| Decision endpoints | `crew_decision_endpoints` | `decisions.endpoints` | no | off | — |
| Publish audiences | `crew_publish_audiences` | `publish.audience` | no | off | — |
| Deploy accounts | `crew_deploy_accounts` | `publish.accounts` | no | off | — |
| Dashboard origins | `crew_remote_origins` | `dashboard_origins` | no | off | this machine only |
| Bind address and tunnel | `remote_connect` | `bind_address` | no | off | `127.0.0.1` only |
| Desktop control | `computer_use` | `computer_use` | no | off | — |
| Policy cache and refresh | `crew_policy_max_age_seconds` / `_refresh_seconds` | `distribution` | yes | Crew's current defaults | Crew's current defaults |
| When rules cannot be fetched | `crew_policy_fallback` | — | no | not used; deny | — |

### Red (13)

Governance cannot control these. The enterprise edition hardcodes them as below.

| Control | Policy term | Enterprise behaviour |
|---|---|---|
| Hooks by name | `script_hook_execute` | Follows `script_hooks`; all off by default |
| Webhooks by source | `webhook_use` | Follows `webhooks`; all off by default |
| Signed extensions only | `crew_extension_require_signatures` | Follows install; all off by default |
| Extension versions | `crew_extension_versions` | Follows install; all off by default |
| Workload identity | `workload_identity_use` | `capabilities.agentcore` off |
| Identity settings | `crew_identity_*` | Same; off |
| Session runtimes | `session_environment_use` | Crew has no such feature; not handled |
| KAS spend cap | `crew_kas_budget` | Crew cannot enforce it; not handled |
| Minimum sandbox level | — | Crew's current sandbox default |
| Boot requirements | — | Crew does not read them; not handled |
| Content sent to external decisions | `crew_decision_external_content` | Follows `decisions`; off by default |
| Image share and copy | `social_share` | Crew cannot stop it; not handled |
| Managed update commands | `crew_update_commands` / `crew_update_provider` | Always off |
