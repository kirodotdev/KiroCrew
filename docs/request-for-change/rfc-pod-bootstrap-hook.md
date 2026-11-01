---
title: Pod bootstrap hook — an operator-approved setup script run once per pod
status: draft
author: jcjaskula-aws
created: 2026-10-31
last-audited: 2026-10-31
audited-at: 2718088836
doc-pr: 26734
implementation-prs: []
tracking-issues: [23414]
supersedes: []
superseded-by: []
---

# RFC: Pod bootstrap hook

A pod starts from an empty, isolated home, so every pod needs the same manual
setup before it can test anything that talks to a forge: sign `glab`, `gh` or
`az` in again, enable the app under test, add a repo. `pod down` deletes the
home, so the next pod repeats all of it. This RFC proposes one operator-owned
script that `pod up` runs once per pod home, after the gateway is healthy. It
can write CLI config into the pod's own config dirs and call the pod's API
through `kirocrew pod api`. The script runs only when its exact contents were
approved by the operator on a surface agents cannot drive, because the obvious
places to put it are writable from an agent sandbox.

Nothing is built. Everything below was measured at `2718088836`.

## Summary

- One phase, **post-boot**, run by the `pod up` process (host side) after the
  health wait and seed verification, before the token is printed.
- The script lives at a fixed path, `<KIROCREW_POD_ENV_DIR>/bootstrap`
  (default `~/.kiro/crew/pods/bootstrap`). There is no per-invocation path flag
  in v1.
- It runs only if a digest of its body matches an approval the operator
  recorded on a human-only surface. A missing approval or a changed body makes
  `pod up` refuse **before** the unit is started.
- It runs once per home. Success writes a marker into the pod home; a re-`up`
  of that home skips the hook. Failure halts the pod, keeps the home and its
  log, and writes no marker, so the next `pod up` runs the hook again.
- With no script present, `pod up` behaves exactly as it does today.

## Motivation

### Current state

`build_pod_env` (`src/kiro_crew/pod/runtime.py`) points every forge CLI's
config dir inside the pod home: `GH_CONFIG_DIR`, `GLAB_CONFIG_DIR`,
`AZURE_CONFIG_DIR` and `AZURE_EXTENSION_DIR`. That is
deliberate: a throwaway pod must not read or mint the operator's real
credentials. It also means the CLIs start signed out in every pod. The same
function leaves `HOME` as the operator's real home, which is the only reason a
setup script could read the operator's own config at all.

The boot body (`_boot_unguarded` in `pod/runtime_boot.py`) does, in order:
read the pod env file, check the venv and the built dist, derive the port,
apply a named scenario (`seed_home_from_scenario`) or write the pod
`config.json` (`write_pod_config`), seed the OS home (`_seed_pod_os_home`),
build the pod env (`build_pod_env`), probe the kiro-cli child
(`_probe_pod_child_bootstrap`) and `exec` the gateway.
None of those steps runs operator code.

`--seed` is the only way to pre-populate a home, and it cannot do this job:

- A **named scenario** is a fixture packaged under
  `kiro_crew/tests_fixtures/<name>/`. It is shared, not personal, and it is
  copied, so it cannot carry a token minted for this pod.
- A **directory seed** contributes only a sanitized `config.json`
  (`src/kiro_crew/pod/README.md:107-112`). It does not copy
  `.config/glab-cli/` or anything else. (The tracking issue left this as
  unchecked; it is now answered.)

Neither can call the pod's API, because both run before the gateway exists.

### Problems

1. Testing a GitLab Issue Radar change in a pod means signing `glab` in by hand
   inside every pod. This came up while testing #20119; once #23408 put `glab`
   on the pod `PATH`, it was found but had no login.
2. Apps the change touches (Issue Radar, Dev Fleet) have to be enabled, and
   repos added, through the pod's UI before the scenario under test exists.
3. The setup is lost on `pod down` and repeated on every new pod. An agent
   that drives pods through the `pod_up` tool can only reach these scenarios
   by repeating the same manual steps (`pod exec`, `pod api`) in each pod.

## Goals

- An operator can describe, once, how a fresh pod is made ready for their
  usual tests, and every `pod up` of a new home applies it.
- The hook can do the two things `--seed` cannot: write per-pod CLI config, and
  call the pod's own API.
- A hook that an agent planted or edited never runs.
- A hook that fails leaves no pod serving in a half-configured state.
- No change at all for operators who never create the script.

## Non-goals

- Copying the operator's live CLI config dirs into pods by default. The hook
  may choose to copy what it wants; Kiro Crew does not.
- Running the hook from the worktree, or letting a branch supply one.
- A hook that runs inside the pod's service unit (see "Alternatives").
- Enforcing least privilege on what the hook stages. That stays the operator's
  call, and the docs say so.
- Windows in the first implementation phase.

## Design

### Where the script comes from

One path: `<pods_dir>/bootstrap`, where `pods_dir` is the existing
`KIROCREW_POD_ENV_DIR` (default `~/.kiro/crew/pods`, `pod/README.md:357`,
`PodConfig.env_file` in `pod/config.py`). It cannot collide with a pod's own
files there, which are all `<name>.env` or `<name>.refused`. The file must be a
regular file owned by the operator and executable; it is opened without
following links, the same way the cron script runner reads a script body
(`hooks.safe_read_file_bytes_nolink`, used by `cron_script_grants.py`).

A worktree-relative path is never consulted. Code from the checkout would let a
branch run as the operator on every `pod up`.

### Approval pin

The fixed path is writable from an agent sandbox. On this host, at
`2718088836`, a write probe from a Kiro Crew agent session created and removed a
file in both `~/.kiro/crew/pods/` and `~/.kiro/crew/`. A hook that ran because
it existed would therefore be a way for any agent to run code as the operator
the next time anyone, including another agent through the `pod_up` tool,
brings a pod up.

So the hook runs only when a keyed digest of its body matches an approval:

- The pattern is the one script crons already use for vault-secret grants:
  `cron_script_grants.py` pins a keyed digest over the script body and the grant
  and fails the run closed on a mismatch (`_secret_env_precheck`). The hook
  reuses the same digest helper and pin key rather than inventing a second one.
- Approval is recorded from a human-only surface: a dashboard confirmation card
  for the hook's current digest, like the cron secret approval on the Schedule
  page. A CLI subcommand an agent can run from its own shell is not enough.
- `pod up` checks the pin **before** `_up` calls `start_pod`. A missing
  approval or a mismatch refuses with the digest it saw and where to approve
  it, and the unit is never started. Refusing rather than skipping matters: a
  pod booted without its usual setup looks healthy and then fails the test for
  an unrelated reason.

### When it runs

Host side, in `_up` (`pod/cli.py`), after `_wait_healthy` succeeds and after
`_verify_seed_landed`, and before `_up` calls `mint_token` and prints the
handle. At that point the gateway is serving, so the
hook can call `kirocrew pod api <name> POST ... --allow-write`, which already
mints its own token and talks over the pod's private socket
(`pod/README.md:177-200`). The hook needs no new credential surface.

A post-boot write into the pod's `GLAB_CONFIG_DIR` should be picked up by the
next `glab` the gateway spawns, because these CLIs read their config on each
invocation. Phase 0 checks that, including whether any gateway code caches a
"signed out" verdict.

### What it receives

- `argv`: the script path alone, executed directly, with no shell.
- Environment: the result of `build_pod_env` for this pod, so `KIROCREW_HOME`,
  `KIROCREW_PORT`, `KIROCREW_PROJECT_DIR`, `KIROCREW_POD=1` and the pod's CLI
  config dirs are the pod's, and `HOME` is the operator's. That env already
  carries the scrub of `SLACK_*`, `WECOM_*`, `MICROSOFT_APP_*`, `FEISHU_*`,
  `JIRA_TOKEN_*` and non-AWS `*_TOKEN`. Two additions:
  `KIROCREW_POD_NAME=<name>` and `KIROCREW_POD_BOOTSTRAP_PHASE=post-boot`.
- Working directory: the pod home. `stdin`: `/dev/null`.

### Contract

- **Timeout:** `KIROCREW_POD_BOOTSTRAP_SECS`, default 120. On expiry the whole
  process group is killed.
- **Output:** stdout and stderr go to `<pod home>/bootstrap.log`, capped in
  size and passed through the same token scrub the pod env uses. The log lives
  in the home, so `pod down` reclaims it.
- **Success** (exit 0): `pod up` writes `<pod home>/.bootstrap-done` holding
  the digest that ran, then continues to the token step as today.
- **Failure** (non-zero or timeout): `pod up` calls `halt_pod`
  (`pod/runtime_lifecycle.py`), which stops the unit and keeps the home, writes
  no marker, prints the last lines of the log and the log path, and exits
  non-zero. The next `pod up` runs the hook again. Choosing `halt_pod` over
  `stop_pod` keeps the log for inspection; see open question 3.
- **Once per home:** a home whose marker exists skips the hook, the same way a
  populated home is never re-seeded. A populated home with no marker (the
  script was added after the pod was first created) runs it.
- **Opt-out:** `pod up --no-bootstrap` skips the hook for that invocation and
  writes no marker.
- **Audit:** each run emits `pod.bootstrap` through the existing `_audit`
  helper with the pod name, digest, outcome and duration, never the output.

### Teardown

Whatever the hook writes under the pod home is reclaimed by `pod down` as
today. Anything it writes elsewhere is the operator's responsibility, and the
pod README says so next to the existing Safety section.

## Migration plan

Each phase is one PR and can be abandoned on its own.

### Phase 0: probes

Answer two questions and record the verdicts in this document.

- Does a config written into `GLAB_CONFIG_DIR`, `GH_CONFIG_DIR` and
  `AZURE_CONFIG_DIR` after the gateway is up take effect for Issue Radar and the
  source-provider panels without a pod restart?
- Which of `~/.kiro/crew/pods/`, `~/.kiro/crew/` and the cron grant pin-key
  location can an agent sandbox write or read, on Linux and macOS? The Linux
  answer for the first two is already "write" (see "Approval pin").

**Exit criteria:** both verdicts are written into this RFC with the commit they
were measured at. If the first answer is "no", this design moves the hook
before the gateway starts and the RFC is revised before Phase 1.

### Phase 1: the hook (Linux, macOS)

Blocked on open questions 1 and 2.

The runner, the approval pin, the marker, `--no-bootstrap`, the log and the
audit event.

**Exit criteria**, each a test:

- With no script present, `pod up` output and behaviour are unchanged.
- An unapproved script, or one changed after approval, makes `pod up` exit
  non-zero before `start_pod` is called, and the unit is never started.
- An approved script runs once, the marker holds its digest, and a second
  `pod up` of the same home does not run it.
- A script that exits non-zero or outlives the timeout leaves the unit halted,
  the home and log present, no marker, and `pod up` non-zero; a later
  `pod up` runs it again.
- A timed-out script's child processes are gone when `pod up` returns.
- A token-shaped string the script prints does not appear in
  `bootstrap.log`.
- The pod README documents the path, the approval step, the env and the
  teardown boundary.

### Phase 2: re-run and reporting

`kirocrew pod bootstrap <wt>` re-runs an approved hook against a running pod,
for example to refresh an expired token, and rewrites the marker. The `pod_up`
MCP tool's reply says whether a hook ran and which digest, so an agent knows
the pod was customised.

**Exit criteria:** re-running against a down pod refuses; re-running an
unapproved body refuses; the tool reply names the digest.

### Phase 3: Windows

Direct execution of a non-`.exe` script needs an interpreter decision, and the
Windows backend has its own stop path (`_stop_pod_windows`). Deferred until
Phase 1 has been used.

## Backward compatibility

No existing file, flag, env key or exit code changes meaning. The hook is
inert until an operator creates the script and approves it. The marker and log
are new files inside the pod home, which only `pod down` reads.

## Security considerations

- **Planted or edited scripts.** The default path is agent-writable on at least
  Linux (measured above). The approval pin is the control. It holds only if
  agents can neither record an approval nor forge a pin, which is why approval
  is a human-only surface and why the pin reuses the cron grant key rather than
  a plain hash. Phase 0 checks the pin key's readability.
- **Who can trigger it.** Any caller of `pod up`, including an agent through
  the `pod_up` tool, triggers the hook. That is acceptable only because the
  content is pinned; the trigger is not the boundary.
- **Credentials crossing into the pod.** A hook that copies the operator's
  `glab` config gives every agent turn in that pod the operator's forge
  identity. That is the point of the feature, and it is a change to what the
  pod README's Safety section promises today, which covers messaging identities
  only. The docs should recommend a narrowly scoped token over copying the
  operator's main config, and state that the hook's staging is not scrubbed.
- **No new privilege.** The hook runs as the operator's UID, the same identity
  that runs `pod up`. The pod README already records that pod separation is not
  a security boundary against same-UID processes (`pod/README.md:16-23`).
- **Output.** Hook output may echo secrets, so it is scrubbed, kept in the pod
  home, never written to the audit event and never included in the `pod_up`
  tool reply.

## Alternatives considered

- **A per-tool flag, `pod up --stage-auth glab`.** Narrower and easier to
  review: Kiro Crew copies one named CLI's config and nothing else, with no
  operator code involved. It needs a new flag value per tool and cannot enable
  apps or add repos, so problems 2 and 3 remain. It is the right fallback if
  maintainers reject a hook under the pod trust model.
- **A personal directory `--seed`.** Not possible today: a directory seed
  contributes only a sanitized `config.json` (`pod/README.md:107-112`).
  Widening it would turn a sanitizing copy into a credential copy, and it would
  still be static.
- **A pre-boot hook inside `_boot_unguarded`.** It would run before the
  gateway reads its config and could use the existing refusal machinery
  (`_refuse`, exit 78). It also runs inside the service unit on every restart
  unless marked, spends the 90 s health budget (`KIROCREW_POD_HEALTH_SECS`)
  before the gateway binds, and cannot call the pod API. The pod API covers
  config through `pod api POST config`, so this is deferred until something
  needs state the gateway only reads at start.
- **A hook path passed per invocation, `--bootstrap <path>`.** More flexible,
  but every distinct path needs its own approval, and the fixed path covers the
  reported use. It can be added later without changing the contract.
- **Inheriting the operator's real CLI config dirs.** Rejected. This is the leak
  the pod-scoped config dirs exist to prevent.

## Open questions

1. Is an operator hook acceptable under the pod trust model at all, or should
   this be the narrower `--stage-auth` flag? (Maintainer decision; Phase 1 is
   blocked on it.)
2. Which human-only surface records the approval: a dashboard card like the
   cron secret grant, or a CLI step that refuses without an interactive
   terminal? (Phase 1 is blocked on it.)
3. On hook failure, should a home that `pod up` itself created be reclaimed
   (`stop_pod`) rather than halted, given there is no prior state to protect?
4. Should the `pod_up` MCP tool run the hook by default, or only when its
   caller asks for it?
