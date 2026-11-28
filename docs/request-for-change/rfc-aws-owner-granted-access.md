---
title: Agent AWS access from an owner's grant -- no forced named profile, credentials vended by Kiro Crew after the owner confirms
status: partial
author: iamwhatever
created: 2026-11-27
last-audited: 2026-11-27
audited-at: c48cd2bdcb
doc-pr: 43318
implementation-prs: [43560]
tracking-issues: [43263]
supersedes: []
superseded-by: []
---

# RFC: Agent AWS access from an owner's grant

**Status:** partial. Maintainer bolichen97 accepted this document as a whole
on 2026-11-27, in
[the sign-off on #43318](https://github.com/kirodotdev/KiroCrew/pull/43318#pullrequestreview-6086476618),
and ruled that all three PRs may be built (§16). The maintainer who asked for
this design had asked that building wait for their word; they gave it on
2026-11-27 and ruled that the build stay lean. PR 1 is built by
[#43560](https://github.com/kirodotdev/KiroCrew/pull/43560), with M3 cut down
on that ruling: §6.1 and §11.2 say what was built and what was left out. PR 2
is not built and still waits on what §11.1 lists; its two probes were run on
2026-11-27 as far as a chat can run them, and §11.3 holds the verdicts. PR 3 is
recorded here when its own pull request lands. The design sections below were read at
`origin/main` `0baf480a06` and describe main before PR 1; the PR 1 parts marked
"as built" were read at `c48cd2bdcb`. A citation names
the file and the symbol or heading, not a line number, as the docs rules ask.
Paths that start with `src/`, `docs/` or `test/` are from the repository root;
every other path is under `src/kiro_crew/`.

Tracks [#43263](https://github.com/kirodotdev/KiroCrew/issues/43263), which
stays open: this document fixes nothing by itself.

**How far each part was reviewed.** §6.1, §6.3, §7.3 and §10 come from a design
round with several reviewers. §6.2, §8 and §9 are newer. They were written after
the maintainer ruled that the design must not use the credential tool an outside
sandbox gives a session, and no review round has seen them yet.

## 1. Summary

In personal use, the owner of an install grants an agent an AWS account in one of
a few normal ways: says an account and a role, has a profile, runs or allows
`ada`, sets keys, or has a working AWS config. Today Kiro Crew answers most of
these with advice to use a named profile. On a host where an outside sandbox
hides the owner's AWS files from the chat, that advice cannot work, `ada` inside
the chat does nothing useful, and nobody says why.

The design has two key actions:

- **A. Remove every instruction that pushes an agent onto a named profile.**
  Kiro Crew says, before the first command, what works in this session.
- **B. Kiro Crew hands the agent AWS credentials itself, after the owner
  confirms.** Kiro Crew's gateway is not inside the chat's sandbox, so it can run
  the owner's own AWS setup (a profile, SSO, a `credential_process`, `ada`) and hand
  the chat freshly resolved credentials when an AWS command asks for them. The first
  use of each account and role asks the owner. **The sandbox stays closed:** the
  owner's AWS files and sign-in never open to the chat.

B builds on a draft that already proposes the vending machinery,
[rfc-host-credential-vending.md](rfc-host-credential-vending.md). This document
depends on that one and adds four things to it (§12). It does not use, route
through or depend on the credential tool an outside sandbox may give a session.

Three PRs:

| PR | What it does | Waits on |
|---|---|---|
| 1 | **Remove the profile push** (action A). Refusal advice, the default prompt, the skill, the docs, `kirocrew doctor` and a per-chat AWS note say what is true for this session. Agent children lose the whole AWS key set, not half. | Accepting this document. No probe. |
| 2 | **Owner-confirmed credential vending** (action B). Kiro Crew vends credentials to the chat through its own helper. The first use of each account and role asks the owner. The `ada` case and a bound profile work through the host's own setup. | PR 1. The neighbour RFC's AWS route, accepted. Two probes (§11.3). |
| 3 | **Guards.** Fewer false refusals, and rules against printing a secret. | Nothing. |

PR 1 removes the push to a named profile by itself. Passing the gateway's AWS
key variables to agents is not in the plan; it is future work (§6.4). Every
mechanism is a change in this repository.

### 1.1 What changes for a user

| What the user did | Today | After PR 1 | After PR 2 and PR 3 |
|---|---|---|---|
| Told the agent an account and a role | On a host where the files are hidden, the agent is told to pass `--profile`, which fails. | The agent says once that this chat cannot see the owner's AWS setup, and does not push a profile. | If the host has a profile or a default setup that gives that account and role, the agent asks for it, the owner confirms once, and `aws` works (PR 2). If the host has none, one line names the owner's step (§13). |
| Has a profile, SSO or `ada` set up on the host | Where the files are hidden: nothing reaches the agent, and it is told to pass `--profile`. Where they are visible: works. | Where hidden: one line says why, with no profile push. Where visible: `aws` runs plainly; `--profile` only when the owner picked one. | Where hidden: `aws` works in the chat. Kiro Crew runs the host's setup outside the sandbox and hands over freshly resolved credentials. The first use of each account and role asks the owner (PR 2). |
| Ran `ada` in a terminal, or allowed `ada` in the chat | Where the files are hidden: nothing reaches the agent, and nobody says why. | One line says why: `ada` inside the chat cannot reach the owner's sign-in or files. | What `ada` set up on the host reaches the chat through Kiro Crew, after the owner confirms (PR 2). The agent still does not run `ada` inside the chat. |
| Bound a folder to a profile | The name is set with no access behind it. `aws` says "profile could not be found". | The help and the doc say the setting gives a name, not a credential. The agent says so and does not retry. | The bound name resolves through Kiro Crew's vending (PR 2). |
| Set AWS keys where the gateway runs | Half the key arrives. Every `aws` call fails. | The keys are kept out whole, and the user is told. | The same. Passing them on is future work (§6.4). |
| Asked a Bot | The Bot says there is no AWS access, or says nothing. | The Bot says it has no AWS route itself and that its worker does AWS work. | The Bot's worker is an ordinary session and gets the vending route with no extra step (PR 2). |
| Asked an agent to search code for AWS words | Often refused, with advice to use a profile. | Still refused at times, but the advice no longer points to a profile. | Not refused (PR 3). |

None of the "after" columns was run. PR 2 on a host with an outside sandbox
rests on a probe nobody has run (§9, §11.3).

## 2. Terms

| Term | Meaning here |
|---|---|
| Owner | The person whose install it is, and whose AWS access is used. |
| Gateway | Kiro Crew's own long-running process. It starts every agent session and is not inside any session's sandbox. |
| Host setup | Whatever the owner configured on the machine to reach AWS: `~/.aws` profiles, SSO, a `credential_process`, `ada`. The neighbour RFC calls it the host's own credential chain. |
| Outside sandbox | A sandbox that Kiro Crew did not apply. On some hosts the installed `kiro-cli` is a launcher shim that starts the engine inside one (the note above `_KIRO_CLI_BUNDLE_EXECUTABLE` in `acp/client_spawn_env.py`). It hides the owner's AWS files from the session. It may also give the session a credential tool of its own. This design does not use that tool, and no text of this design tells an agent to call it. |
| Vend | Kiro Crew resolves credentials through the host setup, outside the sandbox, and hands them to one AWS command in the chat. |
| Identity | What a vend resolves to: the account, the role or user, and the partition. The owner's confirm step is per identity. |
| Tier | The sandbox tier Kiro Crew spawns a session under: `standard` (what `agent.sandbox: auto` gives; called the default tier below), `cc` or `strict`. §7.3 says what selects each. |
| Masked harness | A harness on which Kiro Crew itself hides `~/.aws` on every tier (`adapter_hidden_credential_dirs` in `agent_sdk/tool_gate.py`). One of them, `codex`, gets `~/.aws/config` back read-only (`ADAPTER_EXPOSED_CREDENTIAL_LEAVES`); the others see nothing of `~/.aws` (§7.3). |
| A Bot's own chat | The Bot's conductor session. Its worker is a separate session. |

## 3. Motivation

### 3.1 What users reported

Users of installs on hosts with an outside sandbox reported three things, given
here in their terms:

- The agent "knows nothing about" an AWS account after the owner shared the
  account and its credentials with it.
- Allowing `ada` did not help.
- Something in Kiro Crew seems to allow only a named profile.

The maintainer who relayed the reports set the scope: treat Kiro Crew as a
personal tool, do not design for a shared chat, remove the blockage that forces
users to rely on a profile, and keep the security bar. The same maintainer named
the two key actions of §1, asked for a few large PRs, and ruled that the design
must not use the credential tool the outside sandbox distributes.

### 3.2 What main does today

Issue #43263 holds the full read-only reproduction. The causes inside Kiro
Crew:

| # | Cause | Where |
|---|---|---|
| 1 | The refusal advice names a profile as the route. It says to run `aws configure list-profiles` and pass `--profile <name>`, and that "the durable setup is a profile whose `credential_process` vends credentials on demand". The same push is in the suggested commands, in the `blocked-by-policy` skill, in the user doc, and in the default agent's shipped prompt, which says to "use `--profile <name>` in AWS CLI commands". | `REMEDIATION[DENY_CLASS_AWS_CREDENTIAL]`, `REMEDIATION[DENY_CLASS_SECRET_FILE]` and `SUGGESTED_COMMANDS[DENY_CLASS_AWS_CREDENTIAL]` in `deny_guidance.py`; the "When users need AWS access" line of `config/prompt.md`; "AWS credentials" and "Other key material" in `builtin_skills/blocked-by-policy/SKILL.md`; "The most common false alarm" and "Checking your own setup" in `src/kiro_crew/docs/blocked-commands.md` |
| 2 | Nothing says what works before a refusal. The common failure is not a refusal (the agent runs `aws`, gets "no credentials" or "profile could not be found", and stops), so advice that fires only on a refusal never shows. | no text exists; the rail a turn block would use is `turn_block` in `context_assembly/mcp_servers.py`, called from `build_message` in `context_assembly/turn_message.py` |
| 3 | The only AWS setting for agents binds a folder to a profile name. It sets `AWS_PROFILE` and `AWS_DEFAULT_PROFILE`, not a credential. Where the files are hidden that name resolves to nothing, so the first call fails. The help text says an AWS command "reaches the intended account without --profile". | `PROJECT_PROFILES_KEY` and `bound_profile_env` in `aws_project_profiles.py`; the "Project AWS Profiles" help in `config/integration_sections.py`; "An AWS profile per project" in `src/kiro_crew/docs/configuration.md` |
| 4 | Keys set in the gateway's environment arrive in half. `AWS_SECRET*` and `AWS_SESSION*` are dropped from every agent child and `AWS_ACCESS_KEY_ID` is kept. With the id alone, `aws configure list` exits 255 with "Partial credentials found in env", so the half key also breaks every route that would have worked. | `_SENSITIVE_ENV_PREFIXES` in `sandbox_tiers.py`; `_SPAWN_SCRUB_ENV_PREFIXES` in `sandbox_spawn.py` |
| 5 | A Bot's own chat mounts only Kiro Crew's own servers, on purpose, and nothing tells the Bot or the person what that means for AWS. | `_install_mate_conductor_agent` in `agent_materialization/service_agents.py`; `conductor_mcp_servers` in `mate_worker.py`; `docs/decisions/2026-11-04-conductors-stay-simple-and-new-abilities-go-to-workers.md` |
| 6 | Three refusal rules fire on plain text reads, such as a search of source code for AWS words, and the advice sent with them leads back to cause 1. | the rules `credential-exfil-env-grep-aws`, `credential-exfil-env-awk-sed-aws` and `credential-exfil-python-botocore-credentials` in `security/denied_rules_catalog.py` |
| 7 | **Kiro Crew has no route of its own.** Where a sandbox hides the owner's AWS files, nothing in Kiro Crew can hand the chat a credential. The one design for it is a draft with nothing built. | [rfc-host-credential-vending.md](rfc-host-credential-vending.md) |

What an outside sandbox closes is a separate layer. On such a host the owner's
own AWS files, the owner's sign-in and anything `ada` writes are not visible to
a session, so an approved `ada credentials` command has nowhere to put
credentials the session will read. That follows from the two facts in #43263
(the files cannot be opened; the session reads its credentials from another
place). It was not run. Kiro Crew cannot reopen that layer, and this design does
not try.

Some of Kiro Crew's own text points an agent at a credential tool that an
outside sandbox provides: the last sentence of
`REMEDIATION[DENY_CLASS_AWS_CREDENTIAL]`, a clause of
`REMEDIATION[DENY_CLASS_SECRET_FILE]`, `credential_tool_hint`, and the sections
"If this host vends credentials through an MCP tool" and "If the credential tool
appears to be missing entirely" of the skill. `credential_tool_hint` is emitted
only where the edition's capability manager is available
(`resolve_credential_tool_hint`; `DefaultCapabilityManager.available` in
`platform/defaults.py` is false on the public edition). This design adds no such
text. What happens to the existing sentences is in §6.2 and §16.

On cause 6, the design round recorded six such refusals. Two more were seen
while this document was written. A `grep` of the rule catalog for the third
rule's own id was refused by that rule, because its pattern matches any command
line that holds its three words in order. And a script that edited this
document's text was refused by the first rule.

### 3.3 What was run, and what was not

| Run | Not run, at any point |
|---|---|
| Unit probes on a snapshot of `src/kiro_crew` with made-up values (in #43263). | A vend of any kind. |
| Local `aws configure list` with made-up values (cause 4). | Any AWS call. |
| Read-only checks from a worker chat on a host with an outside sandbox (in #43263). | An `ada credentials` command. |
| One live probe of the outside sandbox's own profile store, with a made-up profile. It belongs to a route this design no longer has (§11.3). | A read of a credential file. |

Not tested at all: any host with no outside sandbox; the `cc` and `strict`
tiers; a masked harness; a Bot's own chat from inside; other partitions; macOS.
§13 lists what this leaves unproven.

## 4. Goals

| # | Goal |
|---|---|
| G1 | After the design, no part of Kiro Crew forces a named profile for an agent's own AWS use. PR 1 alone must do this. |
| G2 | Once the owner has a working AWS setup on the host (a profile, SSO, a `credential_process`, `ada`), the agent can use the account after the owner confirms, and Kiro Crew says plainly how. |
| G3 | The agent knows which account and role it is using, because the owner saw and confirmed it. |
| G4 | The design says what happens to the profile-only advice and to the profile setting. |
| G5 | The design names which checks are stricter than the security bar needs and which must stay. |
| G6 | The security bar is kept. It asks three things: (a) an agent gets credentials only through a grant the owner made on purpose; (b) a secret value does not land in model context, transcripts, memory or logs; (c) text the owner did not write (a fetched page, a file, a tool result) cannot by itself obtain or send out credentials. |
| G7 | Every mechanism is a change in this repository. |

G1 covers an agent's own AWS use. Four features that run on the gateway still
take a profile name and are unchanged, because they run where the owner's own
AWS files are visible: Artifact Deploy (`_aws` in `deploy/engine.py`), the AWS
Control app's accounts (the module note of
`apps/builtins/aws_control/backend/accounts.py`), the voice profile fields, and
the project pill's health check (the status words in
`dashboard/handlers/aws_project_profile.py`).

## 5. Non-goals

- **Using the outside sandbox's credential tool.** The design does not call it,
  route through it or depend on it. That such a tool exists on some hosts is a
  fact this document states; no rule or text of the design tells an agent to
  call it.
- **Opening the sandbox.** No mechanism shows the owner's AWS files or sign-in
  cookie to a session, with or without a confirm. Kiro Crew's own masks keep
  hiding what they hide, and what an outside sandbox closes stays closed.
- **A shared chat.** The scope is personal use: the person at the agent is its
  owner, or a user on their own install. No mechanism here is built for other
  people who can post in a shared Slack chat, and no protection whose only job is
  to stop them is added. Three points from the design round that rest only on
  that case are parked, not answered: what decides that a chat is owner-only, who
  approves a request that starts in a shared chat, and the fact that an approved
  identity stays usable in the session for the next sender. The last one is kept
  as a limit (§13).
- **Changing an outside sandbox or anything it ships.**
- **SSH.** The neighbour RFC also vends SSH signatures. That half is its own.
- **The four gateway-side features** named under §4.
- **An edition's own deny floor** (`SecurityOverlay.extra_deny_patterns`,
  described on `DeniedRuleProvider` in `platform/interfaces.py`). It is not
  installed on the public edition and is not Kiro Crew's to change here.

## 6. Design

Ten mechanisms, M1 to M10, in three PRs. Each table says what an agent can newly
reach or learn.

### 6.1 PR 1: remove the push to a named profile

Action A. It needs no decision beyond accepting this document, no probe, and no
unproven link. Its text must be true by itself, before PR 2 exists: it says what
works in this session today, names no route that is not built, and does not
tell the agent to call an outside tool.

| id | Mechanism | Serves | Builds on | What an agent can newly reach or learn |
|---|---|---|---|---|
| M1 | **Refusal advice without the profile push.** The text is chosen by what this spawn's own mask hid; the default chain first, `--profile` only to choose between profiles the owner has. Where `aws` finds no credentials or no profile, the advice says that something outside Kiro Crew may be hiding the owner's AWS files from this chat, that `--profile` and `ada` inside the chat will not help, and to say so once. The fifteen profile sentences on main are fixed, kept on purpose, or left for PR 2 (list below). One test fails when the AWS advice a session gets holds `--profile`, `list-profiles` or `credential_process` outside the lines that are true for that session's mask. | G1, G4, G5 | Half exists: `REMEDIATION[DENY_CLASS_AWS_CREDENTIAL]` in `deny_guidance.py`. A spawn's tier is on `AcpRuntime.sandbox_mode` and its extra masks on `AcpRuntime.sandbox_hidden_dirs` (`acp/runtime_class.py`); the hidden set is the tier's list plus those extras, and deriving it is work this PR does. | Which of its own folders this tier hides, by name. Nothing else. |
| M2 | **The profile setting stays as it is in code.** `aws.project_profiles` always sets both selectors. Its help text and doc say it sets a name, not a credential. | G4, G1 | Text only: the "Project AWS Profiles" help in `config/integration_sections.py`; "An AWS profile per project" in `src/kiro_crew/docs/configuration.md`. | Nothing. |
| M3 | **`kirocrew doctor`'s `refresh` lines only.** As built, on the maintainer's ruling of 2026-11-27 that the build stay lean: the two `refresh:` lines of `_doctor_credentials` say whether the profile in use has a `credential_process` as a plain fact, with no mark that presents one as the setup to have or its absence as a fault. Left out: `doctor` lines for the tier, for what it hides and for key variables that are set and not passed, and the doc section with the route tables of §7 and the list of §10. What takes their place: M5's log line names the key variables, and "The most common false alarm" in `src/kiro_crew/docs/blocked-commands.md` has a short table of what each tier shows. | G1 | Half exists: the `refresh:` lines of `_doctor_credentials` in `doctor_checks/access.py`. | Nothing. |
| M4 | **An `[AWS ACCESS]` note on the turn's context**, once per context window. "Told" is recorded only when the note was sent. It carries M1's line for this session's mask, the sentence about an outside sandbox, and the bound-folder sentence of M2. A Bot's service agent: "my worker does AWS work". Not built for the guest agent or on a minimal-context turn. A sub-agent gets one line as its own prefix. PR 2 changes its text to name the vending route. | G1, G2 | The rail exists: `turn_block` in `context_assembly/mcp_servers.py`, called from `build_message` in `context_assembly/turn_message.py`; the sub-agent prefix goes where the spawn notice is put, the `_spawn_mcp_notice` call in `subagent_manager/run_turn.py`. | One fact about its own tier. |
| M5 | **Agent children lose the whole key set**: id, secret, session token and the old token alias. One gateway log line names the variables, not their values. The same list scrubs the gateway's own `aws` runs (`run_aws` in `cloud/aws.py`), so they lose the key id too; they already could not use credentials that exist only as environment variables, so nothing that works today stops. | G1, G6 | New. `_SENSITIVE_ENV_PREFIXES` in `sandbox_tiers.py`; `_SPAWN_SCRUB_ENV_PREFIXES` in `sandbox_spawn.py`. | Less than today. |

The profile sentences, and what happens to each:

| # | Where | Verdict |
|---|---|---|
| 1 to 3 | The profile sentences of `REMEDIATION[DENY_CLASS_AWS_CREDENTIAL]` in `deny_guidance.py` | Rewritten or removed (M1) |
| 4 | The `credential_process` profile sentence of `REMEDIATION[DENY_CLASS_SECRET_FILE]` in `deny_guidance.py` | Rewritten (M1) |
| 5 | `SUGGESTED_COMMANDS[DENY_CLASS_AWS_CREDENTIAL]` in `deny_guidance.py` | Rewritten (M1) |
| 6 | The last sentence of `credential_tool_hint` in `deny_guidance.py`, on a vendor that reports no configured profile | Left as it is in PR 1. It is one of the sentences about an outside tool (§6.2). |
| 7 to 10 | `builtin_skills/blocked-by-policy/SKILL.md`: two places under "AWS credentials", one under "If this host vends credentials through an MCP tool", one under "Other key material" | The two under "AWS credentials" and the one under "Other key material": rewritten or removed (M1). The third: with §6.2. |
| 11 | The `refresh:` line of `_doctor_credentials` in `doctor_checks/access.py` | Fixed with M3 |
| 12 | `src/kiro_crew/docs/blocked-commands.md`, under "The most common false alarm" and "Checking your own setup" | Rewritten (M1) |
| 13 | The setting's help and doc | Fixed with M2 |
| 14 | `credential_mask_auth_note` in `providers/acp_runtime_start.py` | Stays. It is true for that mask. |
| 15 | The "When users need AWS access" line of the shipped prompt, `config/prompt.md` | Rewritten (M1). Not in the design round's list. It was found when main was re-read, and it is the one profile sentence every default-agent chat reads before any command. |

Build notes for PR 1:

- If no reader links a session to its hidden set, M1's text names all three
  tiers.
- PR 1 adds one small helper the later PRs use: "what this spawn's mask hid".

As built:

- **M1 and M4 name every tier.** No reader links a session to its hidden set
  where a refusal or a prompt is built, so the fallback above applies. The text
  says what `standard`, `strict` and `cc` show, that some agent harnesses are
  masked on every tier, and that a chat reads its own tier from
  `KIROCREW_SANDBOX_LEVEL` (`_IN_SANDBOX_LEVEL_VAR` in `sandbox_tiers.py`). The
  lines are `AWS_PROFILE_LINE`, `AWS_TIER_LINE` and `AWS_HIDDEN_LINE` in
  `deny_guidance.py`, and every surface quotes them.
- **The helper "what this spawn's mask hid" was not built**, for the same
  reason. PR 2 adds it if it needs it.
- **The `cc` line** (§7.3): a chat reaches `cc` only through a governance floor.
  Both renderers hide `~/.aws` on `cc` and show only its `config`
  (`RendererCapabilities.cc_unmaskable_dirs` in `sandbox_plan.py` is empty for
  both), so the line holds on macOS as on Linux.
- **M4's "told"** is recorded when a turn's settle says the turn landed
  (`settle` in `context_assembly/aws_access.py`, called from
  `rollback_skill_bodies` and `commit_skill_bodies`). Until then every build
  for that session carries the note, so a caller that never settles its turns
  sends it on each of them. A turn with no session key gets no note. The bound-folder sentence rides when `aws.project_profiles`
  binds any folder, and does not say that this chat's folder is bound.
- **The outside-tool sentence of `REMEDIATION[DENY_CLASS_AWS_CREDENTIAL]`**
  lost its words "instead of a named profile" and is otherwise as it was.

### 6.2 PR 2: owner-confirmed credential vending

Action B. The vending machinery is the neighbour RFC's: a listener in the
gateway, a `credential_process` helper the AWS SDK runs inside the session, a
per-session folder for the helper's config, a bearer that authorises only that
route, and a record of each vend. This document does not restate it. It adds
three mechanisms on top, and §12 says which document owns what.

| id | Mechanism | Serves | Builds on | What an agent can newly reach or learn |
|---|---|---|---|---|
| M6 | **Vending reaches every session whose AWS files are hidden.** The neighbour grants a session only when Kiro Crew's own mask hides a store it vends for (`~/.aws` or `~/.ssh`). This adds the case where an outside sandbox hides the AWS files. Kiro Crew cannot see inside an outside sandbox, but it can tell when it is about to start one: the installed `kiro-cli` is a launcher shim with a bundle binary behind it. A session spawned through such a shim gets the grant too, unless the spawn is a delegated one (`delegate_to_kiro` in `sandbox_wrap.py`), which the neighbour gives no grant. A session at the default tier with no shim keeps reading the owner's files directly, as today. | G2 | The neighbour's grant predicate (its "How it works"). A close test exists as stat work: `_kiro_cli_bundle_binary` in `acp/client_spawn_env.py`, used today only for a pod child (`apply_pod_bundle_spawn`). It answers "is there a bundle binary a shim would fall back to", which is not quite "is a shim in the chain": with `KIRO_CLI_PATH` set it says yes with no shim present. PR 2 tightens it. | After the owner confirms: freshly resolved credentials for the confirmed identity, one AWS command at a time. Before: nothing. |
| M7 | **The owner's confirm step.** A session asks for access with one new tool on a Kiro Crew server. The gateway resolves which identity the request means, outside the sandbox, and the owner confirms the first use of each identity. The listener vends only an identity that was confirmed. See §8. | G6, G3 | Half exists (§8). | Nothing until the owner says yes. Then the account and the role or user it is using: names, not secrets. |
| M8 | **The host setup is the source, `ada` included.** A request names a profile, or nothing for the default. The gateway resolves it through the owner's host setup, where a profile, SSO, a `credential_process` and `ada` all work as they do in the owner's terminal. A folder's bound profile name resolves the same way, because the helper's config carries that name (the neighbour's rule for `AWS_PROFILE`). The agent does not run `ada` inside the chat. | G2, G4 | The gateway already runs `aws` against the owner's profiles for its own features, through one chokepoint: `run_aws` in `cloud/aws.py`, which `probe_identity` in `aws_consent.py` uses. Its comments say it runs a profile's `credential_process`, and that it does not support credentials that exist only as environment variables. | Nothing beyond M7. |

**How one request runs.**

1. The per-chat note (M4, changed in this PR) says: credentials in this chat
   come from Kiro Crew; ask for access once, then run `aws` plainly; the owner's
   files and `ada` are not reachable here and do not need to be.
2. The agent calls the access tool with a profile name, or with nothing.
3. The gateway resolves the identity through the host setup and reads its
   account, its role or user and its partition (`probe_identity`: one
   `sts:GetCallerIdentity`, free and read-only). If the host setup fails, the
   tool returns that error, with secrets redacted, and the owner's step (for
   example, sign in again in a terminal).
4. An identity the owner approved before is answered yes. A new one asks the
   owner: once, always, no (§8).
5. From then on, an `aws` command in that session runs the helper, the helper
   asks the listener, and the listener hands back freshly resolved credentials
   for that identity. An identity that was not confirmed is refused, and the
   refusal names the access tool.

"Freshly resolved" is the neighbour's word, and it is not "short-lived". The
route returns what the host setup returns. For SSO or a `credential_process`
that is a short session credential. For a static key in the owner's credentials
file it is that key, which does not run out. Whether such a key is vended at all
is the neighbour's Decision 2 (§12).

**Existing sentences about an outside tool.** The places named in §3.2 tell an
agent to prefer a credential tool an outside sandbox provides. This PR
replaces them with the vending route, so no host loses advice that works today
before something else works. A maintainer may instead want them gone in PR 1
(§16).

**A Bot.** A Bot's worker is an ordinary session, so M6 covers it with no card
and no flag. A Bot's own chat is unchanged: it hands AWS work to its worker.

### 6.3 PR 3: guards

| id | Mechanism | Serves | Builds on | What an agent can newly reach or learn |
|---|---|---|---|---|
| M9 | **Three rules narrowed.** The dump verb or the interpreter must stand where a command starts: at line start; after `;`, `&&`, `\|\|`, `\|`, `(`, `$(` or a backquote; or first in a `-c` string. The PR pins the 22 must-stay cases the design round listed and the known false refusals as tests. | G5 | New. `_ENV_DUMP_VERBS`, the patterns built on it and the three rules of cause 6 in `security/denied_rules_catalog.py`; `test/test_security_denied_commands.py`. | Text reads stop being refused. No environment dump and no inline interpreter payload becomes allowed. |
| M10 | **Refusal rules against printing a secret**, named a speed bump in the doc: `aws configure export-credentials`; `aws configure get` of a secret or token key; `ada credentials print`; reading the file `AWS_SHARED_CREDENTIALS_FILE` names. One more rule of the same kind ships in PR 2, with the thing it guards: a direct shell run of Kiro Crew's own vend helper, whose whole output is a credential. | G6 | New: all of these are allowed today. `security/denied_rules_catalog.py`; `security/paths_lists.py`; `_RULE_CLASSES` in `deny_guidance.py`. | Less than today. A script can still do what a rule refuses. |

### 6.4 Future work, not in this plan: key pass-through

An earlier cut had a last, optional switch: "pass the gateway's AWS key
variables to agents", off by default, the owner's dashboard as its only writer.
It is not one of the three PRs. It is the one mechanism that widens what every
agent holds: each child would hold a long-lived secret and could use it with no
vend and no confirm. The maintainer asked for the plan to hold the key actions
only, and this is neither.

PR 2 does not replace it as things stand. The gateway's own `aws` runs drop key
variables too (`run_aws` in `cloud/aws.py` says credentials that exist only as
environment variables are not supported), so vending built that way would not
serve keys set where the gateway runs. Whether the neighbour's AWS route should
is its own question, next to its Decision 2 on a static long-lived key.

If a maintainer wants the switch later, it needs a yes of its own, PR 3's print
rules in place first, its own decision record (it forces credential redaction
on, which changes
`docs/decisions/2026-11-15-credential-redaction-off-by-default.md`), and probe
S0-5 (§11.3). Until then a user who set keys uses the default section of their
own AWS file.

## 7. The route tables

### 7.1 By host kind

"Hidden by" says which layer hides the owner's AWS files from the session.

| Host kind | Hidden by | After PR 1, the agent | After PR 2, the agent |
|---|---|---|---|
| Outside sandbox, any tier | The outside sandbox. Kiro Crew's own mask too, on `cc`, `strict` or a masked harness. | Runs `aws` once. On "no credentials" or "profile could not be found", says that something outside Kiro Crew hides the owner's AWS files from this chat, and that `--profile` and `ada` here will not help. Does not push a profile. | Asks for access; the owner confirms the first use; `aws` works through vending. Rests on probe S0-6 (§9). |
| No outside sandbox, default tier | Nothing. `~/.aws` is visible (it is not in `_STANDARD_DIRS` in `sandbox_tiers.py`). | Runs `aws` plainly on the default chain. `--profile` only when the owner picked one. | The same. No grant, no confirm: the session reads the owner's files as today. |
| No outside sandbox, `cc` | Kiro Crew: `.aws` hidden, only its `config` shown (`_CC_DIRS` in `sandbox_tiers.py`; the Linux list, see §12 on macOS). | Told only a `credential_process` in the config file works on this tier, and what decides the tier (§7.3). | Asks for access; the owner confirms; `aws` works through vending. **Unproven where a governance floor raised the tier:** the floor raises the gateway's own `aws` runs too (§9). |
| No outside sandbox, `strict` | Kiro Crew: `.aws` hidden (`_STRICT_DIRS` in `sandbox_tiers.py`). | Told no file route works on this tier, and what decides it: `agent.sandbox` ("Sandbox" in `src/kiro_crew/docs/configuration.md`), or a governance floor (§7.3). | The same as `cc` after PR 2, with the same caveat for a governance floor. |
| A masked harness | Kiro Crew, on every tier. On `codex` only the `config` file is shown; on the others nothing is (§7.3). | Told so. On `codex` only a `credential_process` in the config file works; on the others no file route works. | The same as `cc` after PR 2. This is the case the neighbour RFC was written for. |
| Any host, a Bot's own chat | Not applicable: it does not do AWS work itself. | Says "I have no AWS route. My worker does AWS work", and hands off. | The same. Its worker follows its own row. |

### 7.2 By what the owner did

| The owner | Files visible to the session | Files hidden, after PR 1 | Files hidden, after PR 2 |
|---|---|---|---|
| Has a working AWS config: a default or a named profile, SSO, a `credential_process` | Honoured directly, as today | One true line. No profile push. | Honoured through vending, after the confirm |
| Ran `ada` in a terminal | Honoured directly: the default chain reads what it wrote | One true line | Honoured through vending: the gateway's host setup reads what `ada` wrote |
| Uses `ada` as a profile's `credential_process` | Honoured directly | One true line | Honoured through vending: the gateway runs it outside the sandbox, where the sign-in works |
| Gave or allowed an `ada credentials` command in the chat | Honoured as today: Kiro Crew's policy allows it. Not tested. | The agent says once that it cannot reach the owner's files from here | The agent does not run it. If the host setup already gives that account and role, vending serves it. If not, one line asks the owner to run the command in a terminal (§13, §16). |
| Said an account and a role only | Nothing on the host gives it: one line | The same line | The same: vending resolves what the host setup has. One line names the owner's step. |
| Bound the folder with `aws.project_profiles` | Honoured if the owner's config names it | The agent says the name resolves to nothing here | The bound name resolves through vending |
| Set keys in the gateway's environment | Dropped whole; told so | Dropped whole; told so | Dropped whole. Future work (§6.4). |

Still refused everywhere, and why: a command whose job is to print a secret; the
agent's own file tools opening `~/.aws` or the sign-in cookie folder
(`_SENSITIVE_HOME_DIRS` in `security/paths_lists.py`); the metadata endpoint;
`export` of key variables. Each keeps a secret value out of model context
(G6 b).

### 7.3 Corrections to the `cc` and masked-harness rows

Three statements in the design round did not hold when main was re-read. The
tables above already carry the corrected form.

**Masked harnesses.** The design round's row says every masked harness shows
`~/.aws/config`. On main only `codex` does: `ADAPTER_EXPOSED_CREDENTIAL_LEAVES`
holds that one entry, and `adapter_hidden_credential_dirs` hides the whole of
`~/.aws` for every other enforced harness. The neighbour RFC has the same split
in its table. So M1 needs two lines for masked harnesses, not one.

**"Only a `credential_process`".** Where the config file is shown (`cc` and
`codex`), a `credential_process` is the route the product supports. It is not
the only thing that file can hold: the note on
`ADAPTER_EXPOSED_CREDENTIAL_LEAVES` records static keys kept in the config file
itself as a known residual, and `credential_mask_auth_note` already tells the
owner to keep them in the credentials file instead. This document proposes no
line that points an agent at that residual.

**The `cc` tier.** The design round's `cc` rows say that `agent.sandbox` decides
that tier. That is true for `strict` and not for `cc`. `agent.sandbox` accepts
`auto`, `strict` and `off` (its `enum` in `config/sections_agent.py`). `cc` is a
level in the governance scale (`_ORDINAL_SCALES["sandbox"]` in
`platform/governance_parts/matching.py`), and a session reaches it when a
governed `sandbox.min_level` floor clamps its tier up (`effective_sandbox_mode`
and `_clamp_sandbox_mode_to_floor` in `sandbox_tiers.py`). The same floor can
raise a session to `strict`. So the line must name what raised the tier, and
where that is a governance floor the owner's own setting does not lower it. Which sessions run on `cc` in practice was not
traced. PR 1 settles the wording of the `cc` line against the code.

## 8. The owner's confirm step (M7)

**The core of PR 2.** Without it, vending to a session is the neighbour's
"default-on" question with no answer: a credential would reach the agent with
nobody asked. With it, G6 (a) is met by Kiro Crew itself.

**What it is.** An identity the owner approved before is vended unasked,
everywhere. A new identity asks the owner. No approval mode answers a first use.
The match is the account, the role or user, and the partition, read from what
the request resolves to. The same account id in another partition is another
account.

**How the ask is raised.** Through a tool call, so it rides the approval paths
that exist. The access tool is on a Kiro Crew server and is a held capability:
one shared helper, asked wherever the held check (`mate_held_rules.held_for`) is
asked today. The design round listed five callers: `tool_permission.py`,
`slack/handler_runtime/permission_ladder.py`,
`slack/gateway_runtime/turn_approvals.py`, `messaging/driver.py` and
`llm_helpers_parts/tool_approval.py`. A search at `0baf480a06` finds two more,
`channel.py` and `dashboard/chat_turn/tool_approval.py`. The rung goes on every
caller, and one test walks every reader of the held check, as the existing
structural tests do. At the permission request:

1. The tool identity must be the gate's own cached, trusted one. An identity
   taken from the payload does not count.
2. The gateway has already resolved the request to an identity (§6.2, step 3).
   That identity, not the agent's words, is what the owner is shown and what is
   matched.
3. The identity is on the record: answer yes at once, under any mode.
4. The identity is not on the record: the call is held. Trust, YOLO, a Bot's
   `trust` mode, an `auto` job, a trusted pattern and a hook grant do not answer
   it. Only a person does.

An error asks. A tool identity that is not trusted, a record that cannot be
read, or an identity that cannot be resolved never ends in a yes.

**The check does not rest on the ask.** The listener itself refuses to vend an
identity that is neither on the record nor confirmed for that session. So a
harness that fails to raise the ask leaves the agent with no credentials, not
with unconfirmed ones.

**What a first use shows.** A card in the dashboard, or a prompt in the owner's
Slack thread: "First use of this AWS account and role by an agent", then the
account id, the role or user name, the profile the request named, and the task
text the agent wrote. Three choices: once, always, no. The per-identity "always"
is a new choice on both surfaces; today "always" means Trust for the session
(`APPROVAL_WORDS` in `messaging/approval.py`). It counts only from a surface
that knows it is the owner: an owner dashboard request, or the owner's own Slack
id.

| Choice | Effect |
|---|---|
| Once | That session may use the identity until it ends. Nothing is written. A later session asks again. On `codex` the neighbour's grant is per runtime, shared by every session on it, so "once" there lasts for the runtime and covers its sessions. |
| No | The request is refused. Nothing is written. |
| Always | One entry is written: account id, role or user name, partition, an optional label, the time. No secret. Later sessions are answered yes. The prompt then says: "Agents on this install may now use this account and role without asking you. This is a guard, not a wall. Undo: the AWS page of the dashboard." |

**The record.** Its own file in the data home. Not in `config.json`, for the
reason the module note of `aws_consent.py` gives. Protected as the owner's
body-field rows are (`mcp_body_fields.json`, in the module note of
`security/mcp_body_fields.py`): on the read and write file gate, and mounted
read-only for sessions. Two writers, both in the gateway: the approval path on
"always", and an owner-gated dashboard page that lists the identities, adds one
ahead of time (a scheduled run needs that) and removes one, behind the same gate
as `_deny` in `dashboard/handlers/aws_project_profile.py`. No CLI verb. The page
needs all three actions from its first version: a scheduled run, and the
fallback of probe S0-1, both rest on "add". When the record exists, the per-chat
note also shows it. Stated limit: on a host with
no read-only mount an agent could write its own identity, and `doctor` says so
there.

**Per surface.** An identity on the record is answered yes at once on every row.

| Surface | Who answers | What the owner hears while it waits | After it expires |
|---|---|---|---|
| Dashboard chat | The owner, on the card: once, always, no | The card: "First use of this AWS account and role." | "The AWS approval was not answered. Tell me to go on when you are back." Window: `agent.tool_approval_timeout_secs`. |
| Slack, the owner | The owner, on the prompt in the thread | The same text | The same line, after the same window (`_APPROVAL_TIMEOUT` in `slack/handler.py` reads `agent.tool_approval_timeout_secs`). The design round gave 300 s here; that is the shared messaging gate other channels use (`APPROVAL_TIMEOUT_S` in `messaging/approval.py`). |
| A Bot's worker | The owner, on the worker's card | The Bot says in its own chat: "My worker is waiting for you to approve an AWS account." | The Bot says: "It was not approved. AWS work is stopped." |
| A dispatched worker | The owner, on the worker's card | The card | The worker reports blocked, waiting on approval |
| A scheduled run | Nobody is there. Refused at once, with no wait. | Nothing waits | In the run's result: "This job used an AWS account you have not approved yet. Approve it with 'always' from a chat, or add it on the AWS page, then the job runs." |
| A sub-agent | The owner, on the parent's surface | As the parent's row | The parent says its row's line |

**What it is not.** A wall. The confirm is on getting credentials, not on each
use: a confirmed identity stays usable in that session, by whatever the model
does next. The bearer the helper presents is in the session's own environment,
as the neighbour RFC says, so the agent's shell can run the helper itself; M10
adds a speed bump there and nothing more.

**Rough size.** In its earlier form, on a harness permission for an outside
tool, the confirm step was estimated at 800 to 1,000 source lines, 1,200 or more
test lines, plus website files. The form above has the same record, card, page
and held-class rung, and adds the tool and the listener's check. It was not
re-estimated.

## 9. Can vending reach a session an outside sandbox started?

The neighbour RFC did not analyse this. What the code shows, and what it does
not:

**The spawn chain on such a host.** The gateway starts Kiro Crew's own launcher
(tier mask and environment scrub), which starts the installed `kiro-cli`. The
code notes that this is often a shim that starts the engine inside an outside
sandbox, and falls back to a bundle binary (the note above
`_KIRO_CLI_BUNDLE_EXECUTABLE`, and `_kiro_cli_bundle_binary`, in
`acp/client_spawn_env.py`). The agent's shell, the `aws` command and the helper
all run inside both layers.

That is the chain where Kiro Crew applies its own layer. It does not always:
on Windows, and on macOS when kiro-cli's internal sandbox is on, the spawn is
delegated (`delegate_to_kiro` in `sandbox_wrap.py`;
`_delegate_to_kiro_internal_sandbox` in `sandbox_delegation.py` keeps the
launcher and applies the environment scrub and no directory mask). A delegated
spawn gets no grant, here as in the neighbour. If something still hides the
owner's files from it, it gets PR 1's line and nothing more.

| Part | Where it runs | What the code shows |
|---|---|---|
| The listener | In the gateway, outside both layers. An `AF_UNIX` socket under `runtime_dir()`, which is `<data home>/mcp-gateway` (`mcp_gateway/rewriter_overlay.py`). | The neighbour's choice, unchanged. |
| Resolving the host setup | In the gateway, outside both layers. | The gateway's own `aws` runs go through `run_aws` in `cloud/aws.py`, wrapped in Kiro Crew's sandbox with `mode="standard"`, where `~/.aws` is visible. Its comments say it runs a profile's `credential_process`, which is how an `ada`-backed profile would resolve. `probe_identity` uses it. Not run for this document. **One catch:** `wrap_argv` raises that mode to a governed `sandbox.min_level` floor (`_clamp_sandbox_mode_to_floor`, called in `sandbox_wrap.py`). On a host whose floor is `cc` or `strict`, the gateway's own `aws` runs are confined like a session's, so a host setup that needs a hidden store would not resolve this way. PR 2 has to resolve on a path the floor does not confine, or say that such a host is not served. |
| The helper | Inside the session: the AWS SDK runs it as a `credential_process`. The neighbour puts it in its own pre-created read-only directory, not in the per-session folder. | It must be readable and runnable inside the outside sandbox, and must reach the listener's socket. |
| The helper's config | A per-session folder under the data home, opened to that session by Kiro Crew's launcher. | Kiro Crew's launcher is the outer layer. Whether the outside sandbox then leaves that folder visible is not in this repository. |
| `AWS_CONFIG_FILE`, `AWS_SHARED_CREDENTIALS_FILE` | Set by Kiro Crew on the process it spawns, only when the operator has not set them (the neighbour's rule). | Kiro Crew's own scrub keeps them (`CREDENTIAL_POINTER_ENV_VARS` in `acp/client_spawn_env.py` is removed only for a pod child). What the outside sandbox does to them is not in this repository. |

**What is known about the outside sandbox, from this repository and from
#43263.** It hides `~/.aws` from the session. Inside it both pointer variables
name a per-session folder that is not `~/.aws` (read live in #43263). The code
says its mount plan is built around the real user home (`apply_pod_bundle_spawn`
in `acp/client_spawn_env.py`), and that a session's environment "can point at a
session-scoped credentials location" (`REMEDIATION[DENY_CLASS_AWS_CREDENTIAL]`).

**What was observed, not probed.** On the test host, a session the outside
sandbox started reads files under the data home, and Kiro Crew's own tools
answer inside it. Those tools reach the gateway by one of three routes: an
agent socket (`open_with_capability` in `agent_ipc_client.py`, which
`_api_urlopen` in `mcp_core_parts/gateway.py` uses first and alone when the
process holds a capability), a socket in the data home, or loopback TCP when
nobody answers on that socket (`loopback_urlopen` in `loopback_http.py`). Which
one carried them was not checked. If it was either socket, a process inside the
outside sandbox can reach an `AF_UNIX` socket of the gateway.

**So the answer is: probably, and not shown.** Three links are unproven, and one
probe settles all three with no credential involved (S0-6, §11.3):

| Link | If it holds | If it does not |
|---|---|---|
| A pointer variable Kiro Crew set survives the outside sandbox | `aws` works plainly after the confirm | The access tool's reply and the per-chat note give the agent the helper's config path to put in front of its command. The session's shell sets its own environment, so this needs nothing from the outside sandbox. |
| The helper and its config are readable and runnable inside | Vending works | Vending cannot reach such a session as designed. |
| The listener's socket is reachable from inside | Vending works | The same. |

If the second or third link fails, PR 2 still serves the hosts where Kiro
Crew's own mask is the one hiding the files, and a host with an outside sandbox
gets only PR 1's true line. This document does not decide what happens then.
The remaining routes each give something up (§15), and a maintainer rules.

## 10. Which checks stay, narrow or go

| Check | Verdict |
|---|---|
| Advice that names `--profile` and a `credential_process` profile as the setup (cause 1) | Goes (PR 1) |
| Advice that points at an outside sandbox's credential tool (§3.2) | Replaced by the vending route (PR 2) |
| The three rules of cause 6 in `security/denied_rules_catalog.py` (an environment dump piped to a filter for AWS names, twice, and an interpreter line naming the SDK's credentials) | Narrow, keep (M9, PR 3) |
| The scrub of gateway key variables (`_SENSITIVE_ENV_PREFIXES` in `sandbox_tiers.py`) | Stays, and drops the whole set (PR 1) |
| The file gate on `~/.aws` and the sign-in cookie folder (`_SENSITIVE_HOME_DIRS` in `security/paths_lists.py`) | Stays |
| Rules against printing, exporting and sending keys; the metadata-endpoint rules | Stay. Print rules added (M10, PR 3). A speed bump, and the doc says so. |
| The `cc` and `strict` tiers hiding `.aws` (`_STRICT_DIRS` and `_CC_DIRS` in `sandbox_tiers.py`) | Stay. Now explained (PR 1), and served by vending (PR 2). |
| The mask Kiro Crew puts on some harnesses (`adapter_hidden_credential_dirs` in `agent_sdk/tool_gate.py`) | Stays. The same. |
| A Bot's own chat without user servers | Stays. A recorded decision, now explained. |
| What an outside sandbox closes; an edition's deny floor (`SecurityOverlay.extra_deny_patterns`, not installed on the public edition) | Not Kiro Crew's to change |

After PR 1 alone, a search of source code for AWS words is still refused at
times, because M9 is in PR 3. The advice sent with the refusal no longer points
to a profile.

What stays close to a named profile after PR 1, and is said plainly:

- On the `cc` tier and on `codex` the one supported file route is a
  `credential_process` in the config file, until PR 2. A `default` section
  serves, so no name is needed.
- A folder the owner bound keeps its profile name. The owner chose it.
- The four gateway-side features of §4.

## 11. Migration plan

### 11.1 The PR plan

Sizes are added lines, source / test, rough. "Sensitive" means the PR touches a
security path.

| PR | What it does | Mechanisms | Files | Sensitive | Needs | Waits on | Size |
|---|---|---|---|---|---|---|---|
| 1 | Remove the profile push | M1 to M5 | `deny_guidance.py`, `dashboard/chat_turn/tool_approval.py`, `builtin_skills/blocked-by-policy/SKILL.md`, `src/kiro_crew/docs/blocked-commands.md`, `config/prompt.md`, `config/integration_sections.py`, `doctor_checks/access.py`, `src/kiro_crew/docs/configuration.md`, new `context_assembly/aws_access.py`, `context.py`, `context_assembly/turn_message.py`, `subagent_manager/run_turn.py`, `sandbox_tiers.py`, `sandbox_spawn.py`, `test/test_deny_guidance*.py`, `test/test_doctor_credentials.py`, and one sentence of `docs/request-for-change/rfc-host-credential-vending.md` (§12) | yes, in part: M5 changes the spawn scrub | nothing | nothing | about 770 / 1,140, the sum of the design round's estimates for these mechanisms |
| 2 | Owner-confirmed credential vending | M6, M7, M8, and the AWS half of the neighbour's machinery | The neighbour's files for its AWS route (its Scope and Phases); the grant predicate; a Kiro Crew server for the access tool; every caller of the held check (§8); a sealed record; both prompt surfaces; a dashboard page; the note's text, the skill, `deny_guidance.py`, the docs; `doctor`; a decision record; the refusal rule for a direct run of the vend helper (§6.3); the grant predicate's text in `docs/request-for-change/rfc-host-credential-vending.md` (§12) | yes | PR 1: the note's file and the mask helper are new there | The neighbour RFC accepted for its AWS route. Its phase 2 ships the AWS route and the SSH proxy together and waits on all four of its decisions, so either that phase is split into an AWS half and an SSH half, with Decisions 1, 2 and 4 ruled, or Decision 3 is ruled too (§12). Probe S0-6. Probe S0-1. | not estimated. The neighbour gives no sizes; §8 gives the confirm step's earlier figure. |
| 3 | Guards | M9, M10 | `security/denied_rules_catalog.py`, `security/paths_lists.py`, `_RULE_CLASSES` in `deny_guidance.py`, `test/test_security_denied_commands.py` | yes; M9 loosens | nothing | nothing | about 200 / 550 |

How the three stand to each other:

- **PR 1 stands alone.** It removes the push to a named profile by itself.
- **PR 2 needs PR 1 landed.** It does not need PR 3.
- **PR 3 needs nothing.** If it is open beside PR 1 or PR 2, they meet in
  `deny_guidance.py`; that is a merge, not a dependency.
- **Each can be reverted alone**, as long as nothing built on it has landed.
  PR 2 and PR 3 revert without touching another PR. PR 1 reverts alone until
  PR 2 lands; after that PR 2 comes out first.

PR 2 can be built in two ways, and a maintainer picks (§16): the neighbour's AWS
phases land first as that document plans them and PR 2 adds M6 to M8 on top; or
PR 2 carries the AWS half itself. Either way vending is never on for a session
without the confirm step in the same release.

Two things to watch. PR 1 changes a setting's help text, and the last AWS
setting PR also had to refresh `config-baseline.json` and the config module doc.
And PR 2 is large and unsized.

### 11.2 Exit criteria

These are derived from the mechanism rows. The design round itself stated tests
only for M1 and M9.

| PR | Done when |
|---|---|
| 1 | A test fails when the AWS advice a session gets holds `--profile`, `list-profiles` or `credential_process` outside the lines that say when each applies (as built, those lines name every tier, §6.1). The text of `credential_tool_hint`, which holds `--profile` and stays until PR 2, is exempt until then, and so is the skill's section on a host that vends credentials through an MCP tool. No text added by this PR tells an agent to call a tool that Kiro Crew does not own. Each of sentences 1 to 5, 11 to 13 and 15, and the three named skill sentences, is changed as the table in §6.1 says. The two `refresh:` lines of `doctor` no longer mark a `credential_process` as the setup to have; M3's new `doctor` lines and its doc section were left out on the maintainer's ruling of 2026-11-27 (§6.1). The note is sent once per context window, is not sent to the guest agent or on a minimal-context turn, and "told" is not recorded when it was not sent; a sub-agent's first message carries the one line. With all four key variables in the gateway's environment, an agent child has none of them, and the log line names the variables without their values. |
| 2 | Probes S0-6 and S0-1 are recorded in this document. A session whose `~/.aws` is hidden by Kiro Crew's mask, and a session spawned through a launcher shim, each get the grant; a default-tier session with no shim does not, and neither does a delegated spawn. A direct shell run of the vend helper is refused. The listener refuses an identity that is not confirmed, and the refusal names the access tool. No approval mode answers a new identity on any caller of the held check; an identity on the record is answered yes under any mode; an error never ends in a yes; a scheduled run is refused at once. A bound profile name resolves. The existing sentences about an outside tool (§3.2) are gone. Every vend leaves a record with no credential in it (the neighbour's criterion). |
| 3 | The 22 must-stay cases are still refused and the known false refusals are allowed, as tests. Each command M10 lists is refused. |

### 11.3 Probes

Still to run, none with a credential and none with an AWS call. Each probe
writes its verdict into this document before the PR that waits on it opens.

| Probe | Waits on it | What to run | If it fails |
|---|---|---|---|
| S0-6 | PR 2 (M6) | On a host with an outside sandbox, start one session through the gateway with four made-up things in place: a harmless value in `AWS_CONFIG_FILE`; a small file in a per-session folder under the data home, opened by Kiro Crew's launcher, where the helper's config would sit; a small runnable file in a pre-created read-only directory, where the neighbour puts the helper; and a test socket under `<data home>/mcp-gateway` that answers one fixed word. Inside the session: read the variable, read the first file, run the second, connect to the socket. | Per link, as the table in §9 says. If the variable is reset: the agent is given the config path to put in front of its command. If the file or the socket cannot be reached: vending does not reach such a host, PR 2 ships for the other hosts, and a maintainer rules what such a host gets. |
| S0-1 | PR 2 (M7) | A session started through the gateway with the access tool held. One call with made-up arguments. Pass: a permission request reaches the owner's surface, with a trusted tool identity, and carries the call's arguments. | The listener still refuses every unconfirmed identity (§8), so nothing is vended unasked. The owner then approves on the dashboard page, which is the record's other writer, and the tool's refusal says so. |

**Verdicts, 2026-11-27.** Both probes were run as far as a chat can run them,
on one host with one version of each layer: a gateway, an outside sandbox, and
`kiro-cli` on its v3 engine. A worker's shell on that host is inside the outside
sandbox, so the parts that need a process outside it were not run. No credential
was used and no AWS call was made.

| Probe part | Verdict | What was seen |
|---|---|---|
| S0-6, file link | pass | A made-up config file was read, and a made-up script was run, directly and from a child process with no shell, in a per-session folder under the data home that Kiro Crew's launcher opened. Kiro Crew's read-only folder that holds the gate artifacts, beside which the neighbour puts the helper, is visible inside and mounted read-only. The two files were written from inside; a helper written by the gateway was not made. |
| S0-6, socket link, weak form | pass | Kiro Crew's own tool processes inside a session each hold a live connection to the gateway's socket under `<data home>/mcp-gateway`, and its listener runs outside. This also answers §9's "which one carried them was not checked": the socket in the data home. The socket existed before the session started. |
| S0-6, socket link, strict form | not run | A new socket made after the session started, with its listener outside, was not tried. It needs a person to start the listener in a terminal. |
| S0-6, pointer variable link | not tested | On the probed host the outside sandbox sets both pointer variables itself, so a value Kiro Crew set would likely be replaced. §9's fallback (the agent is given the config path to put in front of its command) is expected to be the working path there. |
| S0-1, Kiro Crew's half | pass, with fakes | A made-up held tool was sent through every caller of the held check. At each one the trusted tool identity and the arguments reach the ask, and no approval mode answers it. An identity that is only in the request payload is not trusted, and such a call is still held. |
| S0-1, live half | not run | A real card on the owner's surface was not raised. It needs the owner. |

Two notes for PR 2, from the S0-1 run:

- The background card and the channel card name the tool by its title string
  only. The trusted server and tool pair is in the caller's hands at the held
  check and is not passed on to those two cards, so the card text for a first
  use has to take its identity from the event's trusted fields on those paths.
- A tool on a pre-approved server raises no permission request at all, so the
  held check never runs for it. The default agent's spec pre-approves every
  tool on Kiro Crew's core server today. The access tool must be kept out of
  that pre-approval.

None of the parts that were not run can stop the build of PR 2: each decides
which written fallback applies. Nothing here covers another host kind, another
version of the outside sandbox, macOS, Windows or a delegated spawn.

Not in this plan, named so they are not lost:

| Probe | State |
|---|---|
| S0-5 | Belongs to the key pass-through (§6.4): one child started through the outside launcher with a made-up, harmless variable. To run only if that switch is ever built. |
| D2 | Run on 2026-11-26. A made-up profile was saved to, listed from and removed from an outside sandbox's own profile store, across three chats, with no approval prompt. It was evidence for a route through that sandbox's credential tool. The design no longer has that route, so nothing here rests on it. |
| S0-3, and a live check of an "ask before each credential request" switch | Both tested that same route (a Bot's worker with the outside tool mounted, and a confirm list over it). Retired with it. |

A probe that was considered and not run: starting an outside credential tool by
hand from a shell. Nothing in this design depends on it now.

### 11.4 Rollout and rollback

This subsection follows from the shape of each PR. It was not reviewed as its
own item.

| PR | Rollout | Rollback |
|---|---|---|
| 1 | No setting and no migration. Text changes take effect on upgrade. M5 takes the key id away from agent children on upgrade; an install that relied on the half key was already broken by it (cause 4). | Revert the PR (PR 2 first, if it has landed). |
| 2 | Sessions whose AWS files are hidden start getting the vend wiring on upgrade. Nothing is vended until the owner confirms an identity, so the first AWS command after the upgrade asks once. A scheduled run that needs AWS is refused until its identity is approved with "always" or added on the dashboard page. A default-tier session with no launcher shim sees no change. | Remove an identity on the dashboard page; or revert the PR, which returns such a session to no credentials and PR 1's line. |
| 3 | The refusal rules change on upgrade. | Revert the PR. |

## 12. Relation to other documents

**[rfc-host-credential-vending.md](rfc-host-credential-vending.md)** (`draft`).
This document depends on it for action B. That document proposes that Kiro Crew
resolves credentials through the host's own chain and vends them to a child
whose mask hides them, through a `credential_process` helper, and records each
vend. Nothing of it is built, and its phase 2 waits on four decisions.

Which document owns what:

| Thing | Owner |
|---|---|
| The vend listener, the AWS route and its request contract, the helper, the helper's config and the environment wiring, the per-session folder, the vend-only bearer, the vend record | The neighbour |
| SSH signatures through an agent proxy | The neighbour. Not used here. |
| Which sessions get a grant when Kiro Crew's own mask hides `~/.aws` | The neighbour |
| A grant for a session an outside sandbox hides the files from (M6), and the fallback when that sandbox resets the pointer variables (§9) | This document. PR 2 writes it into the neighbour's predicate. |
| The owner's confirm on first use per identity, its record, its card and its page (M7) | This document |
| The access tool, the `ada` case, a bound profile (M8) | This document. What one request carries is the neighbour's open question 1; M8 answers it: a profile name, or nothing. |
| The per-chat note and every sentence about what works (M1 to M4) | This document |
| The profile-push removal and the guards (PR 1, PR 3) | This document |

What this document adds to that one, in short: the owner's confirm; the `ada`
case stated; the per-chat note that names the working route; hosts where an
outside sandbox hides the files.

How this document answers the neighbour's four decisions, for AWS only:

| Its decision | Answer here |
|---|---|
| 1. Default-on vending | On for the sessions M6 names, and never without the owner's confirm on the first use of each identity. That is the recorded acceptance this document asks a maintainer for. |
| 2. A static long-lived key | Not decided here. The confirm card is the natural place to say that an identity is a long-lived key. The neighbour rules whether it is vended. |
| 3. No agent, keys on disk | SSH. Not this document's. |
| 4. Is vending governed by the ceiling | Not decided here. The access tool is an ordinary tool call, which the helper's own request is not. Whether that is enough for the ceiling is the neighbour's ruling. |

Where the two must not contradict each other:

- That document says the only `AWS_` prefixes the spawn scrub drops are
  `AWS_SECRET` and `AWS_SESSION`. M5 here adds the key id and the old token
  alias. It does not touch `AWS_PROFILE`, `AWS_CONFIG_FILE` or
  `AWS_SHARED_CREDENTIALS_FILE`, which that design's wiring uses. PR 1 also
  updates that sentence.
- That document says a delegated `kiro-cli` spawn gets no grant, because it
  reads the real `~/.aws`. M6 keeps that by saying so: a spawn through a
  launcher shim gets the grant only when it is not a delegated one.
- That document's phase 2 ships the AWS route and the SSH proxy together, and
  waits on all four of its decisions. PR 2 here needs only the AWS half, so it
  asks for that phase to be split, or for Decision 3 to be ruled as well.
- That document's table says a `cc` child on macOS keeps `~/.aws` visible and
  gets no grant. The `cc` rows here were written from the Linux list
  (`_CC_DIRS`), and macOS was not checked for this document. The two agree on
  the rule, which is the neighbour's: the grant follows the mask that was
  actually applied. If that table's macOS cell is out of date, PR 2 corrects it
  with the predicate text.
- Both reject opening the real credential tree to a session.

**`docs/decisions/2026-11-04-conductors-stay-simple-and-new-abilities-go-to-workers.md`.**
A conductor mounts only Kiro Crew's own servers. This design keeps that: a Bot's
own chat hands AWS work to its worker, and the worker is an ordinary session.

**`docs/decisions/2026-11-15-credential-redaction-off-by-default.md`.** Not
changed by the three PRs. The future-work switch of §6.4 would change it.

## 13. Limits, and what is unproven

- **Vending under an outside sandbox is not shown to work.** §9 names the three
  links and the probe. Until S0-6 has run, a user on such a host gets PR 1's
  true line and no working route from Kiro Crew. If the probe fails on the
  helper or the socket, that stays so after PR 2.
- **That the gateway can resolve every host setup is not shown.** It rests on
  what `run_aws` says of itself (§9). A profile whose `credential_process` is
  `ada` was not run through it. On a host whose governance floor raises the
  sandbox tier, the code says the gateway's own `aws` runs are raised with it,
  so vending is not shown to work there, and the `cc` and `strict` cells of
  §7.1 say so.
- **The shim signal can be wrong both ways.** `_kiro_cli_bundle_binary` tells
  that a bundle binary sits behind the installed `kiro-cli`, not that a shim's
  sandbox is on, and with `KIRO_CLI_PATH` set it says yes with no shim at all. A session
  can get a grant it did not need. A host that hides the files some other way
  gets none. In the first case the owner sees a confirm that was not strictly
  needed; in the second the agent gets PR 1's line.
- **Nothing on the host, nothing to vend.** Vending resolves what the owner's
  host setup has. If the owner only said an account and a role, and no profile
  and no default setup gives it, the agent says so and names the owner's step:
  set it up on the host the usual way, with `ada` or SSO, as a default or under
  any name. Kiro Crew does not run a command built from chat text outside the
  sandbox (§16).
- **`ada` inside the chat still does nothing useful** where the files are
  hidden. The design routes around it; it does not make it work.
- **Keys set where the gateway runs** are dropped whole and stay out (§6.4).
- **A guard, not a wall.** The confirm is on getting credentials, not on each
  use. After it, the session can ask the listener again, and its shell can run
  the helper itself. Text the owner did not write can cause AWS calls in a
  session whose identity was confirmed.
- **The outside sandbox's own credential tool is still in the session's tool
  list** on such hosts. This design neither uses it nor removes it, and adds no
  guard on it. An agent can still call it unasked, as today. An owner can
  already make every call to a named server ask first with `hooks.confirm_tools`
  (`config/loader_config.py`).
- **Account ids and role names are not masked.** With the confirm card, the
  record and the per-chat note they reach the owner's own chats and what those
  chats record. The note is not built for the guest agent.
- **The neighbour's risks apply** to PR 2 as that document states them: a bearer
  in the child's environment, a new listener, a helper the child runs.
- **The `cc` line.** What selects the `cc` tier for a session was not traced
  (§7.3).
- **Not reviewed.** §6.2, §8 and §9 have had no review round (see the top of
  this document).
- **Not tested at all.** Any host with no outside sandbox; the `cc` and `strict`
  tiers; a masked harness; a Bot's own chat from inside; other partitions;
  macOS. PR 2 rests on two probes nobody has run.

## 14. Security considerations

What the bar (G6) rests on, PR by PR:

| Part of the bar | After PR 1 | With PR 2 and PR 3 |
|---|---|---|
| (a) Credentials only through a grant the owner made on purpose | Kiro Crew hands out nothing. A session that can read the owner's files uses them as today. | Kiro Crew vends only an identity the owner confirmed (M7). The listener enforces it, not the harness. |
| (b) No secret value in model context, transcripts, memory or logs | No mechanism adds a secret to any context. M5 removes the half key. | The secret goes from the listener to the helper to the AWS SDK, not through the model. M10 refuses the plain ways to print it. A script can still obtain it. |
| (c) Text the owner did not write cannot by itself obtain or send out credentials | Unchanged. | Such text cannot get a new identity approved: only a person answers the confirm. It can use an identity that is already confirmed for that session. |

No mechanism lowers the bar by default. M5 only takes away. M9 is the one
loosening of a refusal rule, and it is bounded by the pinned must-stay cases.
M6 with M7 is the one mechanism that gives an agent credentials it could not
reach before, and it does so only for an identity the owner confirmed.

One thing the gateway newly does on the agent's request: it runs the owner's
host setup, and makes one free, read-only identity call, before the owner has
confirmed. That is needed to show the owner what they are confirming. The
result goes to the owner's card, and to the agent only after a yes.

No record that acts as a grant is stored in `config.json`, and none has a CLI
writer. That is the rule in the module note of `aws_consent.py`: a grant is an
authorization record, not a preference, and a terminal command that records a
grant is a grant an automated caller can take. The note also says `config.json`
is writable by an agent's shell. On main the sandbox mounts both config files
read-only for a session (`_CREW_READONLY_LEAVES` in `sandbox_leaves.py`), so
that part holds only where the sandbox is off or cannot mount read-only. The
design keeps the rule all the same: every agent can read `config.json`, and
where the seal is absent an agent can write it.

Nothing in Kiro Crew is called a limit on which account an agent may use. The
word is "guard".

## 15. Alternatives rejected

| Alternative | Why it was rejected |
|---|---|
| **Route through the outside sandbox's credential tool**: tell the agent to list its profiles and call it, mount it on a Bot's worker, save to its store, and put the confirm on calls to it. | The first cut of this design did this. The maintainer ruled it out: Kiro Crew must not use or depend on a tool it does not own. It also could not be made a grant: any agent can write that tool's store unasked (probe D2), and the confirm would sit on another party's server. |
| **Open the owner's AWS files or sign-in cookie to the session after a confirm**, so that `ada` or a profile works inside the chat. | It puts the material the bar protects in reach of the agent's shell for the rest of the session, and a confirm on opening a folder says nothing about which account is then used. Where an outside sandbox is the layer that hides them, it is also not Kiro Crew's to open. Vending gives the same result with the files still closed. |
| **Start the engine without the outside sandbox**, by running the bundle binary behind the shim and letting Kiro Crew's own sandbox confine it, as `apply_pod_bundle_spawn` does for a pod child. | It takes away a layer the host put there, for every session, to fix one route. Not proposed. It is one of the routes a maintainer could weigh if probe S0-6 fails (§9). |
| **Run an `ada credentials` command from the chat outside the sandbox**, after a confirm. | It would have the gateway run a command built from chat text, and would name one provider's tool in Kiro Crew's code, which the neighbour keeps out. The host setup already covers an owner who uses `ada`. Left as an open question (§16). |
| **Raise the confirm from the listener**, on the first vend, with no tool. | The agent would need no extra step. But a `credential_process` would then block on a person, and the ask would start in the gateway, not in a tool call, so none of the approval paths in §8 would carry it as they are. |
| **Vend to every session**, the default tier included. | A default-tier session with no outside sandbox can already read the owner's files. A confirm there would add a click and guard nothing. |
| **A names setting in `config.json`** (label, account id, role name) that Kiro Crew puts in the agent's context. | The owner's host setup already holds what is needed, and the confirm card shows it. As a grant it is worse: every agent can read `config.json`, the repo's own rule keeps authorization records out of it (§14), and where the sandbox is off an agent's shell can write it. |
| **Ask on every credential request.** | In personal use it asks the owner again and again for the same identity, and where nobody is at the prompt the run ends in a wait. The confirm is once per identity, with "always". |
| **Memory as the place that remembers the account.** | The owner is not its only writer, so it is not a record of what the owner granted. |
| **A terminal command that records a grant.** | A session's shell can run it. The writers are the prompt's "always" and an owner-gated dashboard page. |
| **Not setting the two profile selectors** for a bound folder where the files are hidden. | A plain `aws` command would then use the default, which can be another account. The selectors stay set, and PR 2 makes the name resolve. |
| **Passing the missing half of the key** to fix cause 4. | It puts a long-lived secret in every child by default. The whole set is dropped instead (M5). |
| **Many small PRs, one per mechanism.** | The first cut of the plan had that shape. The maintainer ruled it too granular: the work is two key actions, in a few large PRs. |

## 16. Open questions and the decision

Questions for a maintainer:

1. **Which PRs to build.** PR 1 removes the push to a named profile by itself.
   PR 3 can be accepted or dropped without the others.
2. **PR 2 and the neighbour.** PR 2 needs the neighbour's AWS route. Accept that
   document for AWS first and build PR 2 on it, or let PR 2 carry the AWS half
   (§11.1). Either way its Decisions 1, 2 and 4 need a ruling, and its phase 2
   needs splitting from SSH, or Decision 3 a ruling too (§12).
3. **The shim signal (M6).** Granting a session because its launcher is a shim
   is the one place this design looks at an outside sandbox at all. Accept it,
   or grant on another signal.
4. **The existing sentences about an outside tool.** Replace them in PR 2, as
   proposed, or remove them in PR 1 and accept that a host which relies on them
   today has no named route until PR 2. `credential_tool_hint` is only emitted
   on an edition with a capability manager, so that edition's maintainers have a
   say.
5. **An account and a role with nothing on the host.** Leave it at one line
   that names the owner's step, as proposed, or have Kiro Crew run a provider
   command outside the sandbox after a confirm (§15).
6. **If probe S0-6 fails.** What a host with an outside sandbox gets (§9).
7. **The key pass-through.** Leave it as future work, as proposed (§6.4).
8. **A host whose governance floor raises the sandbox tier.** The floor confines
   the gateway's own `aws` runs too (§9). Either PR 2 resolves the host setup on
   a path the floor does not confine, which is a governance ruling, or such a
   host is not served by vending.

Questions the probes answer: S0-6 and S0-1 (§11.3).

**Decision:** accepted as a whole on 2026-11-27 by maintainer bolichen97, in
[the sign-off on this PR](https://github.com/kirodotdev/KiroCrew/pull/43318#pullrequestreview-6086476618).
All three PRs may be built: PR 1, PR 2 and PR 3. The sign-off does not rule on
questions 2 to 8 above; each is settled in the PR that meets it, and PR 2 still
waits on what §11.1 lists. PR 1 is built (see the status line at the top).
