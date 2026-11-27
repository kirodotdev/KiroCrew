---
title: Agent AWS access from an owner's grant, without a forced named profile
status: draft
author: iamwhatever
created: 2026-11-27
last-audited: 2026-11-27
audited-at: 0baf480a06
doc-pr: null
implementation-prs: []
tracking-issues: [43263]
supersedes: []
superseded-by: []
---

# RFC: Agent AWS access from an owner's grant, without a forced named profile

**Status:** draft. This is a proposal for a maintainer to accept, change or
reject. Nothing here is built. **Implementation is on hold:** the maintainer who
asked for this design said it waits for their decision, so no implementation PR
opens until a maintainer records in §16 which of the eleven PRs to build. Every
code reference below was read at `origin/main` `0baf480a06`. A citation names
the file and the symbol or heading, not a line number, as the docs rules ask.
Paths that start with `src/`, `docs/` or `test/` are from the repository root;
every other path is under `src/kiro_crew/`.

Tracks [#43263](https://github.com/kirodotdev/KiroCrew/issues/43263), which
stays open: this document fixes nothing by itself.

## 1. Summary

In personal use, the owner of an install grants an agent an AWS account in one of
a few normal ways: says an account and a role, registers a profile, runs or
allows `ada`, sets keys, or has a working AWS config. Today Kiro Crew answers
most of these with advice to use a named profile, and on a host that runs a
credential sandbox tool a named profile is the one thing that cannot work.

The design, in five lines:

1. Kiro Crew stops telling agents and users to use a named profile. It says,
   before the first command, which route works in this session.
2. On a host with the credential tool, an account and role the owner says, a
   profile the owner registered, or an `ada credentials` command the owner gives
   all become one call to that tool.
3. On a host without it, the agent runs `aws` on the default chain, and is told
   plainly what its sandbox tier hides.
4. Guards come second: fewer false refusals, rules against printing a secret,
   and, if a maintainer wants it built, one click the first time each account and
   role is used.
5. One opt-in comes last and needs a maintainer's yes: pass the gateway's AWS key
   variables to agents.

It is staged. Stage 1 (four PRs) removes the push to a named profile by itself,
with no decision beyond accepting this document and no unproven link. Stage 2
adds guards and gives a Bot's worker the credential tool. Stage 3 saves what the
owner said, and holds the one opt-in that widens what agents hold.

Every mechanism is a change in this repository. Nothing asks the outside sandbox
tool or its credential server to change.

### 1.1 What changes for a user

"Credential tool" means the tool the outside sandbox gives the agent (§2).

| What the user did | Today | After Stage 1 (PR 1 to 4) | After the whole plan |
|---|---|---|---|
| Told the agent an account and a role | Works in that one chat at best. A new chat knows nothing. | The agent uses them at once through the credential tool. A new chat must be told again. On a host with no credential tool, one line says nothing can hand out access there and names what works. | The agent offers once to save the pair (PR 9). Later chats find it by listing. If the first-use check is built (PR 10): one click the first time, then never again. |
| Registered a profile with `ada` | Nothing tells the agent to look. It says there is no AWS access. | The agent lists what is registered first and uses it. | The same. |
| Ran `ada` in a terminal, or allowed `ada` in the chat | With the credential tool: nothing reaches the agent, and nobody says why. | One line says why. The agent makes the same request through the credential tool. With no credential tool, on the default tier, it works as before. | The same. |
| Has profiles, SSO or keys in their own AWS files | With the credential tool: hidden, and the agent is told to pass `--profile`, which fails. Without it: works. | With it: one line says they cannot be seen here, and the agent asks once for the account and role. Without it: `aws` runs plainly; `--profile` only when the owner picked one. On a stricter tier or a masked harness: one line says what is hidden and the way out. | The same. On a masked harness an SSO sign-in still cannot be used. |
| Bound a folder to a profile | The name is set with no access behind it. `aws` says "profile could not be found". | The agent asks the credential tool to fill that name. If it does not, it runs with `--profile default`. | The same. |
| Set AWS keys where the gateway runs | Half the key arrives. Every `aws` call fails, the working route too. | The keys are kept out whole, and the user is told. The agent uses the credential tool, or the default section of the user's own AWS file. | Last PR, only on a maintainer's yes and the owner's act: one switch passes the keys to agents. It never passes them to a chat that has the credential tool, so on a tool host it changes little. |
| Asked a Bot | The Bot has no credential tool and says there is no AWS access, or says nothing. | The Bot says it has no AWS tool and that its worker does AWS work. Until the worker has the tool, that work needs a normal chat. | The owner adds the tool to that Bot once, on a card (PR 8, behind a probe). |
| Asked an agent to search code for AWS words | Often refused, with advice to use a profile. | Still refused at times, but the advice no longer points to a profile. | Not refused (PR 5). |

None of the "after" columns was run on a host with no credential tool, on the
`cc` or `strict` tier, on a masked harness, or from inside a Bot's chat, and no
vend was ever run (§3.3, §13).

## 2. Terms

| Term | Meaning here |
|---|---|
| Owner | The person whose install it is, and whose AWS access is used. |
| Outside tool | A credential sandbox tool that starts the agent engine, hides the owner's AWS files from the session and gives the session its own credential MCP server. Kiro Crew does not ship it and cannot change it. |
| Credential server | That MCP server. It has a tool that hands out short-lived credentials for an account and a role (the vend tool), and a tool that saves and lists named profiles (the profile tool). Issue #43263 gives the tool names. |
| Vend | One call to the vend tool. |
| Pair | One account and one role. The first-use check also matches the partition (§8). |
| Tool session | A session whose own spec carries an exactly matched credential server: a name in `UNPOOLABLE_SERVERS` (`mcp_gateway/rewriter_spec.py`), or an entry whose command and arguments are those of such a server. Never a keyword match. |
| Tier | The sandbox tier a spawn runs under: `standard` (what `agent.sandbox: auto` gives; called the default tier below), `cc` or `strict`. §7.3 says what selects each. |
| Masked harness | A harness on which Kiro Crew itself hides `~/.aws` on every tier (`adapter_hidden_credential_dirs` in `agent_sdk/tool_gate.py`). One of them, `codex`, gets `~/.aws/config` back read-only (`ADAPTER_EXPOSED_CREDENTIAL_LEAVES`); the others see nothing of `~/.aws` (§7.3). |
| A Bot's own chat | The Bot's conductor session. Its worker is a separate session. |

## 3. Motivation

### 3.1 What users reported

Users of installs that run under an outside tool reported three things, given
here in their terms:

- The agent "knows nothing about" an AWS account after the owner shared the
  account and its credentials with it.
- Allowing `ada` did not help.
- Something in Kiro Crew seems to allow only a named profile.

The maintainer who relayed the reports set the scope: treat Kiro Crew as a
personal tool, do not design for a shared chat, and remove the blockage that
forces users to rely on a profile, with the security bar kept.

### 3.2 What main does today

Issue #43263 holds the full read-only reproduction. The causes inside Kiro
Crew:

| # | Cause | Where |
|---|---|---|
| 1 | The refusal advice names a profile as the route. It says to run `aws configure list-profiles` and pass `--profile <name>`, and that "the durable setup is a profile whose `credential_process` vends credentials on demand". The same push is in the suggested commands, in the `blocked-by-policy` skill, in the user doc, and in the default agent's shipped prompt, which says to "use `--profile <name>` in AWS CLI commands". | `REMEDIATION[DENY_CLASS_AWS_CREDENTIAL]`, `REMEDIATION[DENY_CLASS_SECRET_FILE]` and `SUGGESTED_COMMANDS[DENY_CLASS_AWS_CREDENTIAL]` in `deny_guidance.py`; the "When users need AWS access" line of `config/prompt.md`; "AWS credentials", "If this host vends credentials through an MCP tool" and "Other key material" in `builtin_skills/blocked-by-policy/SKILL.md`; "The most common false alarm" and "Checking your own setup" in `src/kiro_crew/docs/blocked-commands.md` |
| 2 | The one sentence that says "prefer the credential tool" is added only when the edition's capability manager is available. On the public edition it never is, so the hint is empty even when a credential server is in the session's own tool list. The `doctor` line has the same gate. | the `manager.available()` check in `resolve_credential_tool_hint` (`deny_guidance.py`); `DefaultCapabilityManager.available` in `platform/defaults.py`; `_credential_vendor_line` in `cli_doctor_parts/host_probes.py` |
| 3 | Nothing names the working route before a refusal. The common failure is not a refusal (the agent runs `aws`, gets "no credentials" or "profile could not be found", and stops), so advice that fires only on a refusal never shows. | no text exists; the rail a turn block would use is `turn_block` in `context_assembly/mcp_servers.py`, called from `build_message` in `context_assembly/turn_message.py` |
| 4 | The only AWS setting for agents binds a folder to a profile name. It sets `AWS_PROFILE` and `AWS_DEFAULT_PROFILE`, not a credential. Under the outside tool that name is not in the session's AWS config, so the first call fails. The help text says an AWS command "reaches the intended account without --profile". | `PROJECT_PROFILES_KEY` and `bound_profile_env` in `aws_project_profiles.py`; the "Project AWS Profiles" help in `config/integration_sections.py`; "An AWS profile per project" in `src/kiro_crew/docs/configuration.md` |
| 5 | Keys set in the gateway's environment arrive in half. `AWS_SECRET*` and `AWS_SESSION*` are dropped from every agent child and `AWS_ACCESS_KEY_ID` is kept. With the id alone, `aws configure list` exits 255 with "Partial credentials found in env", so the half key also breaks every route that would have worked. | `_SENSITIVE_ENV_PREFIXES` in `sandbox_tiers.py`; `_SPAWN_SCRUB_ENV_PREFIXES` in `sandbox_spawn.py` |
| 6 | A Bot's own chat mounts only Kiro Crew's own servers, on purpose, and nothing tells the Bot or the person so. | `_install_mate_conductor_agent` in `agent_materialization/service_agents.py`; `conductor_mcp_servers` in `mate_worker.py`; `docs/decisions/2026-11-04-conductors-stay-simple-and-new-abilities-go-to-workers.md` |
| 7 | Three refusal rules fire on plain text reads, such as a search of source code for AWS words, and the advice sent with them leads back to cause 1. | the rules `credential-exfil-env-grep-aws`, `credential-exfil-env-awk-sed-aws` and `credential-exfil-python-botocore-credentials` in `security/denied_rules_catalog.py` |

What the outside tool closes is a separate layer. On such a host the owner's own
AWS files, the owner's sign-in and anything `ada` writes are not visible to a
session, so an approved `ada credentials` command has nowhere to put credentials
the session will read. That follows from the two facts in #43263 (the files
cannot be opened; the session reads its credentials from another place). It was
not run. Kiro Crew cannot reopen that layer. It can only turn the grant into a
call to the credential server, and say so.

On cause 7, the design work recorded six such refusals. Two more were seen
while this document was written. A `grep` of the rule catalog for the third
rule's own id was refused by that rule, because its pattern matches any command
line that holds its three words in order. And a script that edited this
document's text was refused by the first rule.

### 3.3 What was run, and what was not

| Run | Not run, at any point |
|---|---|
| Unit probes on a snapshot of `src/kiro_crew` with made-up values (in #43263). | A vend. |
| Local `aws configure list` with made-up values (cause 5; the `--profile default` check in §6.1). | Any AWS call. |
| One live probe of the profile tool with a made-up profile (§11.3, probe D2). | An `ada credentials` command. |
| Read-only checks from a worker chat on a host with the outside tool (in #43263). | A read of a credential file. |

Not tested at all: any host with the outside tool off; the `cc` and `strict`
tiers; a masked harness; a Bot's own chat from inside; other partitions; macOS.
§13 lists what this leaves unproven.

## 4. Goals

| # | Goal |
|---|---|
| G1 | After the design, no part of Kiro Crew forces a named profile for an agent's own AWS use. Stage 1 alone must do this. |
| G2 | Once the owner has granted credentials by a normal route (said the account and role, registered a profile, ran or allowed `ada`, set keys, has a working AWS config), the agent can use the account, and Kiro Crew says plainly which route it honours and how. |
| G3 | After the owner shares an account, an agent knows which account and role it may use and has a route that works. |
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

- **A shared chat.** The scope is personal use: the person at the agent is its
  owner, or a user on their own install. No mechanism here is built for other
  people who can post in a shared Slack chat, and no protection whose only job is
  to stop them is added. Three points from the design work that rest only on that
  case are parked, not answered: what decides that a chat is owner-only, who
  approves a request that starts in a shared chat, and the fact that a vended
  credential stays in the session for the next sender. The last one is kept as a
  limit (§13): the first-use check is on the vend, not on the use.
- **Changing the outside tool or its credential server.**
- **Reopening what the outside tool closes**, or opening the owner's AWS files or
  sign-in cookie to a session under any opt-in.
- **Changing a mask.** The `cc` and `strict` tiers and the masked harnesses keep
  hiding what they hide. Filling that hole is the subject of
  [rfc-host-credential-vending.md](rfc-host-credential-vending.md) (§12).
- **The four gateway-side features** named under §4.
- **An edition's own deny floor** (`SecurityOverlay.extra_deny_patterns`,
  described on `DeniedRuleProvider` in `platform/interfaces.py`). It is not
  installed on the public edition and is not Kiro Crew's to change here.

## 6. Design

### 6.1 Stage 1: nobody is pushed to a named profile

No decision beyond accepting this document. No unproven link. PR 1 to PR 4.

| id | Mechanism | Serves | Builds on | What an agent can newly reach or learn |
|---|---|---|---|---|
| M1 | **Refusal advice without the profile push.** In a tool session: the tool first. With no tool: text chosen by what this spawn's mask hid; the default chain first, `--profile` only to choose between profiles the owner has. The hint's count is given on every edition, from the session's own server names. The fourteen profile sentences the design work listed, and a fifteenth found since, are fixed or kept on purpose (list below). One test fails when tool-session text holds `--profile`, `list-profiles` or `credential_process` outside the bound-folder sentence. | G1, G4, G5 | Half exists: `REMEDIATION[DENY_CLASS_AWS_CREDENTIAL]`, `credential_tool_hint` and `resolve_credential_tool_hint` in `deny_guidance.py`. The hidden set of a spawn is stored by `launch` in `acp/launch.py` (`_sandbox_hidden_dirs`) and read through `AcpRuntime.sandbox_hidden_dirs` in `acp/runtime_class.py`. | Which of its own folders this tier hides, by name. Nothing else. |
| M2 | **The profile setting stays as it is in code.** `aws.project_profiles` always sets both selectors. Its help text and doc say it sets a name, not a credential. Words for a tool session: fill the bound name through the tool; else `aws --profile default ...`; for a script, `env -u AWS_PROFILE -u AWS_DEFAULT_PROFILE <command>`. | G4, G1 | Text only: the "Project AWS Profiles" help in `config/integration_sections.py`; "An AWS profile per project" in `src/kiro_crew/docs/configuration.md`. A local check with a made-up config ended `rc=0` for both fallback forms. | Nothing. |
| M3 | **`kirocrew doctor` and one doc section.** `doctor` reports: credential server present or not (exact names); the tier and what it hides; key variables set and not passed; and the `refresh` line stops blaming a missing `credential_process` on a tool host. The doc section holds the two route tables of §7, the list of §10, and the four gateway-side features as unchanged. No line names a switch before its PR ships. | G2, G1, G5 | Half exists: `_credential_vendor_line` in `cli_doctor_parts/host_probes.py`, with its `manager.available()` gate; the `refresh:` lines of `_doctor_credentials` in `doctor_checks/access.py`. | Nothing. |
| M4 | **An `[AWS ACCESS]` block on the turn's context**, once per context window. "Told" is recorded only when the block was sent. Tool session: credentials come from the credential tool; list its profiles first; a listed profile is a hint, to be used for work the owner asked for; an account and role the owner says, or an `ada credentials` command, become a tool call (the task text must list the actions, and the lifetime asked for is no longer than the work needs); no profile flag; the bound-folder sentence of M2; on an empty listing: "tell me the account id and the role name once". A Bot's service agent without the tool, while the default agent has it: "my worker does AWS work" (and see §6.2 on the wait for PR 8). No credential server and `.aws` hidden: the tier line. Not built for the guest agent or on a minimal-context turn. A sub-agent gets one line as its own prefix. | G1, G2, G3 | The rail exists: `turn_block` in `context_assembly/mcp_servers.py`, called from `build_message` in `context_assembly/turn_message.py`; the sub-agent prefix goes where the spawn notice is put, the `_spawn_mcp_notice` call in `subagent_manager/run_turn.py`. Listing across chats: probe D2. | One fact about its own tool list or tier. The outside tool's listing, which it can already call unasked today. |
| M5 | **Agent children lose the whole key set**: id, secret, session token and the old token alias. One gateway log line names the variables, not their values. | G1, G6 | New. `_SENSITIVE_ENV_PREFIXES` in `sandbox_tiers.py`; `_SPAWN_SCRUB_ENV_PREFIXES` in `sandbox_spawn.py`. | Less than today. |

The sentences, and what happens to each:

| # | Where | Verdict |
|---|---|---|
| 1 to 3 | The profile sentences of `REMEDIATION[DENY_CLASS_AWS_CREDENTIAL]` in `deny_guidance.py` | Rewritten or removed (M1) |
| 4 | The `credential_process` profile sentence of `REMEDIATION[DENY_CLASS_SECRET_FILE]` in `deny_guidance.py` | Rewritten (M1) |
| 5 | `SUGGESTED_COMMANDS[DENY_CLASS_AWS_CREDENTIAL]` in `deny_guidance.py` | Rewritten (M1) |
| 6 | The last sentence of `credential_tool_hint` in `deny_guidance.py`, on a vendor that reports no configured profile | Gets the next step (M1) |
| 7 to 10 | `builtin_skills/blocked-by-policy/SKILL.md`: two places under "AWS credentials", one under "If this host vends credentials through an MCP tool", one under "Other key material" | Rewritten or removed (M1) |
| 11 | The `refresh:` line of `_doctor_credentials` in `doctor_checks/access.py` | Fixed with M3 |
| 12 | `src/kiro_crew/docs/blocked-commands.md`, under "The most common false alarm" and "Checking your own setup" | Rewritten (M1) |
| 13 | The setting's help and doc | Fixed with M2 |
| 14 | `credential_mask_auth_note` in `providers/acp_runtime_start.py` | Stays. It is true for that mask. |
| 15 | The "When users need AWS access" line of the shipped prompt, `config/prompt.md` | Rewritten (M1). **Not in the design work's list.** It was found when main was re-read for this document, and it is the one profile sentence every default-agent chat reads before any command. |

Three build notes that belong to Stage 1:

- M1's tier text needs a reader from a session to its hidden set. If no reader
  links the two, the text names all three tiers.
- M3's doc section leaves out the "with the Stage 3 opt-in" cells of the second
  route table until PR 11 ships, or it breaks its own rule about naming a switch
  early.
- Three of the four PRs need the same two small helpers: "this session's spec has
  an exactly matched credential server" and "what this spawn's mask hid". Both go
  in PR 1.

### 6.2 Stage 2: guards, and the Bot's worker

| id | Mechanism | Serves | Builds on | What an agent can newly reach or learn |
|---|---|---|---|---|
| M6 | **Three rules narrowed.** The dump verb or the interpreter must stand where a command starts: at line start; after `;`, `&&`, `\|\|`, `\|`, `(`, `$(` or a backquote; or first in a `-c` string. The PR pins the 22 must-stay cases the design work listed and the known false refusals as tests. | G5 | New. `_ENV_DUMP_VERBS`, the patterns built on it and the three rules of cause 7 in `security/denied_rules_catalog.py`; `test/test_security_denied_commands.py`. | Text reads stop being refused. No environment dump and no inline interpreter payload becomes allowed. |
| M7 | **Refusal rules against printing a secret**, named a speed bump in the doc: `aws configure export-credentials`; `aws configure get` of a secret or token key; `ada credentials print`; reading the file `AWS_SHARED_CREDENTIALS_FILE` names; a shell start of an exactly named credential server. The file gate also refuses the session's vended credentials file, matched by the outside tool's present folder name, and the doc says it follows that name. It matches only the credentials file, so the session's own AWS config in the same folder stays readable. | G6 | New: all of these are allowed today. `security/denied_rules_catalog.py`; `security/paths_lists.py`; `_RULE_CLASSES` in `deny_guidance.py`. | Less than today. A script can still read the file. |
| M8 | **One switch, "ask before each AWS credential request"**, off by default. It treats every exactly matched credential server as confirm-listed. It is the fallback when probe S0-1 fails, and what an owner can turn on before M10 ships. An owner can already get the same with one line, `hooks.confirm_tools`. | G6 | Exists for the asking: the `hooks.confirm_tools` schema in `config/loader_config.py`; `_operator_requires_confirmation` and `may_skip_gate_now` in `platform/governance.py`. A probe in the design work showed that with the list set, the grant is dropped from the wire rules. | Nothing. A guard. Trust, YOLO and an `auto` run answer it. A grant already sent is withdrawn at the next restart. |
| M9 | **A Bot's worker gets the credential server when the owner adds it on the "Add abilities" card.** The card writes one flag in Kiro Crew's sealed additions record, in a new entry shape keyed by Bot. The projection mounts the server only when the flag exists, and takes the entry from the default agent's current spec at spawn. The Bot's file alone turns nothing on. A Bot's own chat gets nothing. | G3, G2 | Partly: the card and catalog (`gateway_lacks` and `transport_for_file` in `mate_capability_catalog.py`); the sealed record (`RECORD_DIR` in `template_mcp_additions.py`), which refuses a Bot's agents today (`CREWMATE_REASON`, returned by `_template_support`) and whose module note says "What the record never carries is trust". | That Bot's worker sessions reach the credential tool and, after a vend, can read that short-lived secret, as a default-agent chat can today. The worker asks on a vend until the owner grants it. |
| M10 | **The first-use check.** Built only if a maintainer asks for it. See §8. | G6, G3 | Half exists (§8). | Nothing new. Narrower than today. |

Between PR 3 and PR 8 a Bot's worker does not have the tool yet. Until it does,
the Bot's line in M4 ends: "until my worker has the tool, run this in a chat on
the default agent". The same line is the fallback if probe S0-3 fails.

Exact match, for every rule or claim in this section: a name in
`UNPOOLABLE_SERVERS`, or an entry whose command and arguments are those of such
a server. The keyword match that exists today (`credential_vendor_server_ids`
in `deny_guidance.py`) stays only for the hint's count.

### 6.3 Stage 3: saving, then the one opt-in

| id | Mechanism | Serves | Builds on | What an agent can newly reach or learn |
|---|---|---|---|---|
| M11 | **Offer once to save.** When the owner says a pair, the agent offers once to save it with the outside tool's profile tool, so later chats find it by listing. What is found there is a hint, and still meets the first-use check where that exists. | G3 | Exists outside Kiro Crew: add and list through the tool, across three chats, with no prompt (probe D2). Text only in M4's block, the skill and the doc. | The pair the owner said, in later chats. Names, not secrets. |
| M12 | **The key pass-through opt-in.** Last PR. Needs a maintainer's yes to build, and the owner's act to turn on. See §9. | G2, G1 | New (§9). | Widens, on the owner's act only (§9). |

## 7. The two route tables

"Blocks today" lists what Kiro Crew itself does.

### 7.1 By host kind: what the agent does after Stage 1 alone

| Host kind | What blocks or pushes to a profile today | What the owner did | What the agent does after Stage 1 |
|---|---|---|---|
| Outside tool on, any tier | The advice says `--profile <name>` and "the durable setup is a profile" (cause 1). The hint is empty on the public edition (cause 2). No text names the route before a refusal (cause 3). Half a key stops every `aws` call (cause 5). | Said an account and a role | Calls the credential tool with them. |
| | | Registered a profile with the outside tool | Lists the tool's profiles first and uses it. Listing across chats is proven (D2). |
| | | Ran `ada` in a terminal, or gave or allowed an `ada credentials` command | Says once that this does not reach the session. Makes the same request through the tool: same account, role and provider. |
| | | Has profiles, SSO or keys in their own AWS files | Says they cannot be seen here. Asks once for the account and role. |
| | | Bound the folder with `aws.project_profiles` | Asks the tool to fill the bound name. If `aws` still says the profile cannot be found: `aws --profile default ...`, or the unset form for a script. Tells the owner. |
| | | Set keys in the gateway's environment | No half key arrives. Told the keys are not passed to agents, and that the tool route works. |
| Outside tool off, default tier | Key variables dropped in all modes (the note above `_SENSITIVE_ENV_PREFIXES` in `sandbox_tiers.py`). Advice wording as above. `~/.aws` is visible (it is not in `_STANDARD_DIRS`). | Has a working AWS config (default or named, SSO, `credential_process`), or ran `ada` | Runs `aws` plainly. `--profile` only when the owner picked one. |
| | | Set keys | Told they are not passed, and that the default section of the owner's own AWS file works. |
| | | Said an account and a role only | Says nothing vends on this host, and names the routes that work here. |
| Outside tool off, `cc` | `.aws` hidden, only its `config` shown; the sign-in cookie folder hidden (`_CC_DIRS` in `sandbox_tiers.py`). | Any file route | Told only a `credential_process` in the config file works on this tier, and what decides the tier (§7.3). |
| Outside tool off, `strict` | `.aws` and the cookie folder hidden (`_STRICT_DIRS` in `sandbox_tiers.py`). | Any file route | Told no file route works on this tier, and that `agent.sandbox` decides it ("Sandbox" in `src/kiro_crew/docs/configuration.md`). |
| A masked harness | `.aws` hidden on every tier. On `codex` only its `config` is shown; on the other masked harnesses nothing is (§7.3; the paragraph under the harness table in the configuration doc). | SSO, static keys in the credentials file, `ada` | Told so. On `codex` only a `credential_process` in the config file works; on the others no file route works. **Limit: on these harnesses a working SSO sign-in still cannot be used by the agent.** `agent.sandbox` does not change it. |
| Any host, a Bot's own chat | No user server, on purpose (cause 6), and no words. | Anything | Says "I have no AWS tool. My worker does AWS work", and hands off (with the line of §6.2 until PR 8). |

### 7.2 By grant: which grant is honoured, and how

| Grant | Outside tool on | Tool off, default tier | Tool off, `cc` or `strict` |
|---|---|---|---|
| Said account and role | Turned into a tool call | Cannot be honoured: nothing vends. One line. | Same line |
| Registered a profile with the outside tool | Turned into a tool call: list, then vend | Honoured if the owner's AWS config names it | `cc`: only a `credential_process` that reads nothing hidden. `strict`: refused by the tier; one line. |
| Ran `ada` in a terminal | Not visible. Turned into a tool call. | Honoured directly: the default chain reads what it wrote | Refused by the tier; the line names what decides the tier (§7.3) |
| Allowed `ada` inside the chat | Turned into a tool call | Honoured as today: Kiro Crew's policy allows it. Not tested. | Refused by the tier; one line |
| Working AWS config | Not visible; a bound name can go to the tool | Honoured directly | `cc`: `credential_process` only. `strict`: refused; one line. |
| Keys in the gateway's environment | Dropped whole. Not passed to a tool session even with the Stage 3 opt-in. | Dropped whole. With the Stage 3 opt-in: honoured. | Same, both tiers |

Still refused everywhere, and why: a command whose job is to print a secret; the
agent's own file tools opening `~/.aws` or the sign-in cookie folder
(`_SENSITIVE_HOME_DIRS` in `security/paths_lists.py`); the metadata endpoint;
`export` of key variables. Each keeps a secret value out of model context
(G6 b). Not proposed: any opt-in that shows the sign-in cookie to a session.

### 7.3 Corrections to the `cc` and masked-harness rows

Three statements in the design work did not hold when main was re-read. The
tables above already carry the corrected form.

**Masked harnesses.** The design work's row says every masked harness shows
`~/.aws/config`. On main only `codex` does: `ADAPTER_EXPOSED_CREDENTIAL_LEAVES`
holds that one entry, and `adapter_hidden_credential_dirs` hides the whole of
`~/.aws` for every other enforced harness.
[rfc-host-credential-vending.md](rfc-host-credential-vending.md) has the same
split in its table. So M1 needs two lines for masked harnesses, not one.

**"Only a `credential_process`".** Where the config file is shown (`cc` and
`codex`), a `credential_process` is the route the product supports. It is not
the only thing that file can hold: the note on
`ADAPTER_EXPOSED_CREDENTIAL_LEAVES` records static keys kept in the config file
itself as a known residual, and `credential_mask_auth_note` already tells the
owner to keep them in the credentials file instead. This document proposes no
line that points an agent at that residual.

**The `cc` tier.** The design work's `cc` rows say that `agent.sandbox` decides
that tier. That is true for `strict` and not for `cc`. `agent.sandbox` accepts
`auto`, `strict` and `off` (its `enum` in `config/sections_agent.py`).
`cc` is a level in the governance scale (`_ORDINAL_SCALES["sandbox"]` in
`platform/governance_parts/matching.py`), and a session reaches it when a
governed `sandbox.min_level` floor clamps its tier up (`effective_sandbox_mode`
and `_clamp_sandbox_mode_to_floor` in `sandbox_tiers.py`). So on `cc` the line
must name what raised the tier, and where that is a governance floor the owner's
own setting does not lower it. Which sessions run on `cc` in practice was not
traced for this document. PR 1 settles the wording of the `cc` line against the
code.

## 8. The first-use check (M10)

**A maintainer's choice, because of its cost.** Rough size: 800 to 1,000 source
lines, 1,200 or more test lines, plus website files. That is about ten times
M8's switch. What the cost buys over the switch plus the outside store: the
outside store takes a write from any chat with no prompt (probe D2), so it
cannot be the grant; and the switch asks on every vend and is answered by Trust,
where the check asks once per pair and is answered only by the owner. Without
it, G6 (a) is not met by anything in Kiro Crew: a vend rests on what the owner's
sign-in can reach, as it does today.

**What it is.** A pair the owner approved before vends unasked, everywhere. A
new pair asks the owner once. No approval mode answers a first use. Listing and
saving a profile never ask. The match is account, role and partition, with the
tool's default partition when the call gives none: the same account id in
another partition is another account.

**The wire rule.** When Kiro Crew sends a spec to the engine, it adds an `ask`
over each exactly matched credential server in it. The builder exists
(`_with_ask_rules` in `acp/default_crew_servers.py`, reached through
`with_default_crew_servers(..., edition_ask_rules=True)` in
`acp/harness/kas.py`; the spec's grants are projected by the
`allowed_tools_to_permissions` call in `acp/kas_agents.py`). It writes
whole-server patterns, so the rule that ships is the one the probe tests: an ask
on the whole server, with the gateway answering a listing yes. A per-tool
pattern would be unproven.

**The rung.** One shared helper, asked wherever the held check
(`mate_held_rules.held_for`) is asked today. The design work listed five
callers: `tool_permission.py`, `slack/handler_runtime/permission_ladder.py`,
`slack/gateway_runtime/turn_approvals.py`, `messaging/driver.py` and
`llm_helpers_parts/tool_approval.py`. A search at `0baf480a06` finds two more,
`channel.py` and `dashboard/chat_turn/tool_approval.py`. The rung goes on every
caller, and the test named below is what keeps that true. It behaves as a second
held class (the module note and `held_for` in `mate_held_rules.py`). At a
permission request:

1. The server and tool identity must be the gate's own cached, trusted one, the
   bar the module note of `security/mcp_body_fields.py` sets for a row. An
   identity taken from the payload does not count.
2. Read the account, the role and the partition from the call's arguments.
3. The pair is on the record: answer yes at once, under any mode.
4. The pair is not on the record: the call is held. Trust, YOLO, a Bot's `trust`
   mode, an `auto` job, a trusted pattern and a hook grant do not answer it. Only
   a person does.

An error asks. An identity that is not trusted, arguments that cannot be read,
or a record that cannot be read all count as a new pair. One test walks every
reader of the held check, as the existing structural tests do.

**What a first use shows.** A card in the dashboard, or a prompt in the owner's
Slack thread: "First use of this AWS account and role by an agent", then the
account id, the role name, the provider if the call gave one, and the task text
the agent wrote. Three choices: once, always, no. The per-pair "always" is a new
choice on both surfaces; today "always" means Trust for the session
(`APPROVAL_WORDS` in `messaging/approval.py`). It counts only from a surface
that knows it is the owner: an owner dashboard request, or the owner's own Slack
id.

| Choice | Effect |
|---|---|
| Once | The call runs. Nothing is written. |
| No | The call is refused. Nothing is written. |
| Always | The call runs, and one entry is written: account id, role name, provider and partition if given, an optional label, the time. No secret. The prompt then says: "Agents on this install may now use this account and role without asking you. This is a guard, not a wall. Undo: the AWS page of the dashboard." |

**The record.** Its own file in the data home. Not in `config.json`, for the
reason the module note of `aws_consent.py` gives. Protected as the owner's
body-field rows are (`mcp_body_fields.json`, in the module note of
`security/mcp_body_fields.py`): on the read and write file gate, and mounted
read-only for sessions. Two writers, both in the gateway: the approval path on
"always", and an owner-gated dashboard page that lists the pairs, adds one ahead
of time (a scheduled run needs that) and removes one, behind the same gate as
`_deny` in `dashboard/handlers/aws_project_profile.py`. No CLI verb. A first
version of the page with list and remove only loses nothing of the check. When
the record exists, M4's block also shows it. Stated limit: on a host with no
read-only mount an agent could write its own pair, and `doctor` says so there.

**Per surface.** A pair on the record is answered yes at once on every row.

| Surface | Who answers | What the owner hears while it waits | After it expires |
|---|---|---|---|
| Dashboard chat | The owner, on the card: once, always, no | The card: "First use of this AWS account and role." | "The AWS approval was not answered. Tell me to go on when you are back." Window: `agent.tool_approval_timeout_secs`. |
| Slack, the owner | The owner, on the prompt in the thread | The same text | The same line, after 300 s (`APPROVAL_TIMEOUT_S` in `messaging/approval.py`) |
| A Bot's worker | The owner, on the worker's card | The Bot says in its own chat: "My worker is waiting for you to approve an AWS account." | The Bot says: "It was not approved. AWS work is stopped." |
| A dispatched worker | The owner, on the worker's card | The card | The worker reports blocked, waiting on approval |
| A scheduled run | Nobody is there. Refused at once, with no wait. | Nothing waits | In the run's result: "This job used an AWS account you have not approved yet. Approve it once from a chat, then the job runs." (If the dashboard page can add a pair ahead of time, the line names it too.) |
| A sub-agent | The owner, on the parent's surface | As the parent's row | The parent says its row's line |

**What it is not.** A guard on the harness path, not a wall. It checks the vend,
not the use. It does not guard a backend that reads its spec from disk, a script,
or a credential server started by hand. With the record shown, every chat of the
owner's agents learns the pairs the owner approved: names, not secrets.

**It rests on probe S0-1** (§11.3). If the probe fails, the check is not built,
M8's switch is what remains, and the doc says that nothing checks a vend by
default.

## 9. The key pass-through opt-in (M12)

**Last PR. Needs a maintainer's yes to build.** It waits on PR 6 and on probe
S0-5.

A switch, "pass the gateway's AWS key variables to agents". Off by default. Its
state is a consent record, not a config key, and the owner's dashboard is its
only writer (the pattern in the module note of `aws_consent.py`). When it is on:

- Agent children keep the four key variables, except a session whose spec has a
  credential server (a Bot's worker that holds the tool is such a session), and
  except the guest agent. Keys in the environment outrank a vend, so passing
  them to a tool session would sign with the gateway's keys while the owner
  believes the call ran as the pair they approved.
- A bare environment dump is refused.
- Credential redaction is forced on. That changes a recorded default
  (`docs/decisions/2026-11-15-credential-redaction-off-by-default.md`), so the
  PR carries its own decision record.
- M4's block tells the agent its environment holds keys.

**What it gives up.** Every agent, Bot's worker, sub-agent, scheduled run and
tool server the harness starts holds the key id, the secret and the token, and
can sign any call those keys allow, with no vend and no first-use check, for as
long as the keys live. A script can read the secret. Text the model obeys can
use the keys wherever the shell runs unasked. In the terms of G6 it gives up
(b), and (c) where the shell runs unasked. It is the owner's act, with a true
warning. The security review's advice: build the first-use check if S0-1
passes, and treat this opt-in as the last resort.

**The warning at the switch.** "Every agent, Bot, worker, scheduled run and tool
server on this install will hold these AWS keys and can use them without asking
you. A command as plain as `env` shows the secret to the model and writes it
into the chat's record. These keys do not run out. They are not passed to a chat
that has the credential tool. Prefer short-lived keys. Undo: turn this off;
rotate the keys if a chat printed them."

Until it ships, a user who set keys has to use the default section of their own
AWS file, or the credential tool.

## 10. Which checks stay, narrow or go

| Check | Verdict |
|---|---|
| Advice that names `--profile` and a `credential_process` profile as the setup (cause 1, and the other sentences in §6.1) | Goes |
| The tool hint and the `doctor` line gated on the edition's capability manager (cause 2) | The gate goes |
| The three rules of cause 7 in `security/denied_rules_catalog.py` (an environment dump piped to a filter for AWS names, twice, and an interpreter line naming the SDK's credentials) | Narrow, keep (M6, in Stage 2) |
| The scrub of gateway key variables (`_SENSITIVE_ENV_PREFIXES` in `sandbox_tiers.py`) | Stays, and now drops the whole set. The owner may opt out last (M12). |
| The file gate on `~/.aws` and the sign-in cookie folder (`_SENSITIVE_HOME_DIRS` in `security/paths_lists.py`) | Stays |
| Rules against printing, exporting and sending keys; the metadata-endpoint rules | Stay. Print rules added (M7). A speed bump, and the doc says so. |
| The `cc` and `strict` tiers hiding `.aws` (`_STRICT_DIRS` and `_CC_DIRS` in `sandbox_tiers.py`) | Stay. Now explained. |
| The mask Kiro Crew puts on some harnesses (`adapter_hidden_credential_dirs` in `agent_sdk/tool_gate.py`) | Stays. Now explained, with its limit. |
| A Bot's own chat without user servers | Stays. A recorded decision, now explained. |
| A credential call running with nobody asked | Stays as the default. The first-use check is a maintainer's choice; M8's switch is the smaller guard. |
| The outside tool closing `~/.aws`; an edition's deny floor (`SecurityOverlay.extra_deny_patterns`, not installed on the public edition) | Not Kiro Crew's to change |

After Stage 1 alone, a search of source code for AWS words is still refused at
times, because M6 is in Stage 2. The advice sent with the refusal no longer
points to a profile.

What stays close to a named profile after Stage 1, and is said plainly:

- On the `cc` tier and on `codex` the one supported file route is a
  `credential_process` in the config file. A `default` section serves, so no name
  is needed.
- A folder the owner bound keeps its profile name. The owner chose it.
- The four gateway-side features of §4.

## 11. Migration plan

### 11.1 The PR plan

Sizes are added lines, source / test, rough. "Sensitive" means the PR touches a
security path. **PR 1 ships first.**

| PR | Stage | Mechanisms | Files | Sensitive | Must follow | Waits on | Size |
|---|---|---|---|---|---|---|---|
| 1 | 1 | M1, the leftover sentences, the one test, the two shared helpers | `deny_guidance.py`, `dashboard/chat_turn/tool_approval.py`, `builtin_skills/blocked-by-policy/SKILL.md`, `src/kiro_crew/docs/blocked-commands.md`, `config/prompt.md` (sentence 15, not in the design work's file list), `test/test_deny_guidance*.py` | no | nothing | nothing | 300 / 450 |
| 2 | 1 | M2, M3, the `doctor` refresh line, the doc section | `config/integration_sections.py`, `cli_doctor_parts/host_probes.py`, `doctor_checks/access.py`, `src/kiro_crew/docs/configuration.md`, `test/test_doctor_credentials.py` | no | PR 1 (the exact-match helper). PR 4, if its `doctor` line says the key variables are "not passed": until PR 4 lands the key id still is. | nothing | 180 / 220 |
| 3 | 1 | M4 | new `context_assembly/aws_access.py`, `context.py`, `context_assembly/turn_message.py`, `subagent_manager/run_turn.py` | no | PR 1 (both helpers) | nothing | 250 / 350 |
| 4 | 1 | M5 | `sandbox_tiers.py`, `sandbox_spawn.py` | yes | nothing | nothing | 40 / 120 |
| 5 | 2 | M6 | `security/denied_rules_catalog.py`, `test/test_security_denied_commands.py` | yes, loosens | nothing | nothing | 80 / 300 |
| 6 | 2 | M7 | `security/`, `_RULE_CLASSES` in `deny_guidance.py` | yes | PR 1 (same file) | nothing | 120 / 250 |
| 7 | 2 | M8 | `platform/governance.py`, config, doc, `doctor` | yes | nothing | its live check | 100 / 200 |
| 8 | 2 | M9 | `template_mcp_additions.py`, `mate_worker.py`, `mate_capability_catalog.py`, `mcp_mate.py` | yes | nothing | probe S0-3, twice | 200 / 300 |
| 9 | 3 | M11 | the block's text, the skill, the doc | no | PR 3 (the block's file is new there) | nothing (D2 is done) | 40 / 80 |
| 10 | 2, a maintainer's choice | M10, the block's record lines | `acp/`, every caller of the held check (§8), a sealed record, both prompt surfaces, a dashboard page, a decision record | yes | PR 3 | a maintainer asking for it; probe S0-1 | 800 to 1,000 / 1,200, plus website |
| 11 | 3, last | M12 | a consent record, a dashboard handler and switch, `sandbox_spawn.py`, `security/denied_rules_catalog.py`, `security/redaction_switch.py`, the block's text, `doctor`, a decision record | yes | PR 6 | a maintainer's yes; probe S0-5 | about 400 / 550, plus website |

No two Stage 1 PRs change the same existing file, so four people can build at
the same time. Landing order for Stage 1: PR 4 and PR 1 first, in either order;
then PR 2 and PR 3, in either order.

Two things to watch. PR 2 changes a setting's help text, and the last AWS setting
PR also had to refresh `config-baseline.json` and the config module doc. And if
each PR must update the module docs under `docs/system-specs/`, PR 1 and PR 4 may
meet in the same doc file; that is a merge, not a design problem.

PR 10's size was estimated with five callers of the held check in mind. §8 finds
seven, so the estimate is low by that much.

### 11.2 Exit criteria

These are derived from the mechanism rows. The design work itself stated tests
only for PR 1 and PR 5.

| PR | Done when |
|---|---|
| 1 | A test fails when tool-session text holds `--profile`, `list-profiles` or `credential_process` outside the bound-folder sentence. The hint's count is non-empty on the public edition for a spec with a credential server. Each of sentences 1 to 10, 12 and 15 is changed as the table in §6.1 says. |
| 2 | `doctor` prints the four lines of M3 with a fake spec and fake tier. The help text and the doc of `aws.project_profiles` say it sets a name. The doc section names no switch. |
| 3 | The block is sent once per context window in a tool session, is not sent to the guest agent or on a minimal-context turn, and "told" is not recorded when it was not sent. A sub-agent's first message carries the one line. |
| 4 | With all four key variables in the gateway's environment, an agent child has none of them, and the log line names the variables without their values. |
| 5 | The 22 must-stay cases are still refused and the known false refusals are allowed, as tests. |
| 6 | Each listed command is refused, the file gate refuses a made-up vended credentials path, and the session's AWS config beside it stays readable. |
| 7 | With the switch on, the grant for a credential server is absent from the wire rules; its live check (§11.3) is recorded. |
| 8 | A Bot's worker mounts the server only when the sealed flag exists; the Bot's file alone mounts nothing; a Bot's own chat mounts nothing. Probe S0-3 passed twice. |
| 9 | The block offers to save once per pair the owner said, and not again. |
| 10 | Probe S0-1 passed. No approval mode answers a new pair on any caller of the held check; a pair on the record is answered yes under any mode; an error asks; a scheduled run is refused at once. |
| 11 | With the switch off nothing changes from PR 4. With it on: a tool session and the guest agent still get no keys, a bare environment dump is refused, and redaction is on. Probe S0-5 is recorded. |

### 11.3 Probes

Done: **D2**. On 2026-11-26 a made-up profile (a zero account id and a role that
does not exist) was saved through the credential server's profile tool from one
chat. It was listed in that chat and in two other chats. Add and list ran with no
approval prompt, and the tool accepted a pair it had never checked. The store is
one file under the owner's home, and the `ada profile list` shell command reads
the same one. The profile was removed again from a chat's shell with
`ada profile delete`, and the store was left empty. No vend, no AWS call and no
`ada credentials` command was run.

D2 does not prove: that a vend with a listed profile works; that a profile the
owner adds from their own terminal lands in the same file (likely, since the
shell command reads it); that a Bot's worker sees the listing; that the store
outlives a restart of the outside tool or a new sign-in.

Still to run, none with an AWS call. Each probe writes its verdict into this
document before its PR opens.

| Probe | Before | What to run | If it fails |
|---|---|---|---|
| S0-1 | PR 10 | A session started through the gateway with a test `ask` on the whole credential server in its projection. One listing call; no vend is needed. A throwaway custom agent with one `ask` rule is enough, since a spec's own `ask` travels to the engine as written (rule 2 in the note of `merge_user_permissions`, `acp/kas_permissions.py`). Pass: a permission request arrives, with a trusted identity, and carries the call's arguments. | No request: the projection leaves the server out of the grants it sends, and the probe runs again. Still none, or no arguments: no first-use check. M8's switch is what remains, and the doc says nothing checks a vend. |
| M8 live | PR 7 | The switch on; one listing call. Pass: a card appears. Also note what Trust and YOLO do. It is nearly the same run as S0-1; do both in one sitting. | `doctor` shows the switch as not in force on this backend. |
| S0-3 | PR 8 | A test Bot with the record flag; its worker lists once, and again after the outside tool has restamped its entry. | PR 8 is not built. The Bot's line becomes "run this in a chat on the default agent". |
| S0-5 | PR 11 | One child started through the outside launcher with a made-up, harmless variable. | On a host with the outside tool the switch does nothing for sessions the launcher wraps, and `doctor` says so. |

A probe that was considered and not run: starting a credential server by hand
from a shell. The design assumes the worst case instead (a guard on the harness
path is not a wall) and adds the shell rule of M7.

### 11.4 Rollout and rollback, per stage

This subsection follows from the shape of each PR. It was not reviewed as its
own item in the design work.

| Stage | Rollout | Rollback |
|---|---|---|
| 1 | No setting and no migration. Text changes take effect on upgrade. PR 4 takes the key id away from agent children on upgrade; an install that relied on the half key was already broken by it (cause 5). | Revert the PR. Each of the four stands alone, except that PR 2 and PR 3 use PR 1's helpers. |
| 2 | PR 5 and PR 6 change refusal rules on upgrade. PR 7's switch is off by default. PR 8 does nothing until the owner adds the tool to a Bot on the card. PR 10 changes the default for a new pair from "runs unasked" to "asks once"; pairs are approved at first use, so no list has to be filled in first, but a scheduled run that uses a new pair is refused until the pair is approved from a chat, or added on the dashboard page if that page can add one ahead of time. | PR 5, PR 6: revert. PR 7: turn the switch off. PR 8: remove the ability on the card, which removes the flag. PR 10: remove a pair on the dashboard page; reverting the PR returns to today's unasked vend. |
| 3 | PR 9 is text. PR 11's switch is off by default and nothing changes until the owner turns it on in the dashboard. | PR 9: revert. PR 11: turn the switch off; rotate the keys if a chat printed them. |

## 12. Relation to other documents

**[rfc-host-credential-vending.md](rfc-host-credential-vending.md)** (`draft`)
is the neighbour. It proposes that Kiro Crew itself resolves credentials through
the host's own chain and vends them, through a `credential_process` helper, to a
child whose mask hides `~/.aws`. The two documents do not overlap in mechanism,
and they meet in three places.

| Point | That document | This document |
|---|---|---|
| Subject | A child that Kiro Crew's own mask (the `cc` and `strict` tiers, the masked harnesses) leaves without credentials. It names no credential provider. | An agent's own AWS use once the owner made a grant, mostly on a host where an outside tool hides the files and offers its own credential server. |
| Who vends | Kiro Crew: a new listener, a helper and a vend record. | Nobody new. On a tool host the outside tool vends. Kiro Crew never resolves or holds a credential. |
| The mask | Unchanged. | Unchanged. |
| Default | Vending on by default, pending its Decision 1. | A credential call runs unasked, as today. The guard on it (M10) is built only if a maintainer asks. |

What this document leaves to that one: the hole on `cc`, on `strict` and on a
masked harness. Here those rows end in one plain line ("only a
`credential_process` in the config file works", "no file route works"), and the
masked-harness SSO limit in §13 stays. If that document is accepted and built,
those lines are no longer the whole truth, and Stage 1's text (M1, M3, M4) is
updated in the PR that ships the vending.

Where they must not contradict each other:

- That document says the only `AWS_` prefixes the spawn scrub drops are
  `AWS_SECRET` and `AWS_SESSION`. PR 4 here adds the key id and the old token
  alias. It does not touch `AWS_PROFILE`, `AWS_CONFIG_FILE` or
  `AWS_SHARED_CREDENTIALS_FILE`, which that design's wiring uses. PR 4 also
  updates that sentence.
- Its Decision 2 (a static long-lived key is equal to handing over the
  credentials file) and M12 here are the same risk reached by two routes. They
  should be ruled the same way.
- Not analysed: how that design's pointer wiring behaves on a host where the
  outside tool already sets the pointer variables. That design sets them only
  when the operator has not. This document assumes nothing about it.
- Both reject opening the real credential tree to a session.

**`docs/decisions/2026-11-04-conductors-stay-simple-and-new-abilities-go-to-workers.md`.**
A conductor mounts only Kiro Crew's own servers. This design keeps that: a Bot's
own chat gets words, not a tool, and M9 gives the tool to the worker.

**`docs/decisions/2026-11-15-credential-redaction-off-by-default.md`.**
Redaction is one owner switch, off by default. M12 forces it on while keys are
passed. That is a change to a recorded default, so PR 11 carries a decision
record of its own. PR 10's file list also holds a decision record.

## 13. Limits, and what is unproven

- **What the outside tool closes.** On a host with it, the owner's own AWS files,
  the owner's sign-in and anything `ada` writes are not visible to a session.
  Kiro Crew cannot reopen that. It can only turn the grant into a credential tool
  call.
- **Hosts where a grant still cannot be honoured.** No credential tool, and the
  owner only said an account and role: nothing vends. `strict`: no file route.
  `cc` and `codex`: only a `credential_process` in the config file. The other
  masked harnesses: no file route. On every masked harness a working SSO sign-in
  cannot be used by the agent, and `agent.sandbox` does not change it. Until M12 ships, a user who set keys has to
  use their own AWS file.
- **Keys and the credential tool.** Keys are never passed to a chat that has the
  credential tool, so on a tool host M12 changes little.
- **Unproven without a real vend.** That a pair the agent was told is accepted.
  That a vend with a listed profile works. That the tool fills a bound profile
  name; the `--profile default` fallback covers that one. These rest on the
  tool's own text.
- **Not proven by D2.** That a Bot's worker sees the listing, and that the store
  outlives a restart of the outside tool.
- **Guards are not walls.** The first-use check and M8's switch sit on the
  harness path. A script or a hand-started server is outside them. After a vend,
  the secret is in a file the session's shell can read. The first-use check is on
  the vend, not on the use.
- **Text the owner did not write.** Until the first-use check is built, such text
  can cause a vend in any session that holds the tool, as it can today. The
  outside store is written by any agent with one unasked call, so a listed
  profile is a hint, never a grant. Where the check is not built, nothing stands
  behind the word "hint".
- **The outside tool's own sign-off step.** The tool can show a link and wait for
  the owner to authorise a request. That step is not in the design. The tool's
  own text tells the agent what to do, and a run with nobody there cannot finish
  it.
- **Names in context.** Account ids and role names are not masked. With M4, M11
  and the record of M10 they reach the owner's own chats and what those chats
  record. The block is not built for the guest agent.
- **The `cc` line.** What selects the `cc` tier for a session was not traced
  (§7.3).
- **Not tested at all.** Any host with the outside tool off; the `cc` and
  `strict` tiers; a masked harness; a Bot's own chat from inside; other
  partitions; macOS. PR 7, PR 8, PR 10 and PR 11 each rest on a probe nobody has
  run on a live session.

## 14. Security considerations

What the bar (G6) rests on, stage by stage:

| Part of the bar | Default after Stage 1 | With Stage 2 | With M12 on |
|---|---|---|---|
| (a) Credentials only through a grant the owner made on purpose | As today: a vend rests on what the owner's sign-in can reach. | Met only once the first-use check (PR 10) is built. | The switch is the owner's act. The keys are then used with no vend and no first-use check. |
| (b) No secret value in model context, transcripts, memory or logs | No mechanism adds a secret to any context. PR 4 removes the half key. | PR 6 adds speed bumps. A vended file stays readable by a script. | Given up. A plain `env` would show the secret, so the dump is refused and redaction is forced on. |
| (c) Text the owner did not write cannot by itself obtain or send out credentials | Unchanged. | Narrowed to pairs the owner approved by PR 10. M8's switch narrows it only outside Trust. | Given up where the shell runs unasked. |

No mechanism lowers the bar by default. PR 4 only takes away. PR 5 is the one
loosening of a refusal rule, and it is bounded by the pinned must-stay cases.
PR 8 widens what one Bot's worker can reach, on the owner's act per Bot, to what
a default-agent chat can reach today. PR 11 is the one mechanism that widens
what agents hold, and it is last, off by default and the owner's act.

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
| **A names setting in `config.json`** (label, account id, role name, provider) that Kiro Crew puts in the agent's context. | The outside profile store already holds the names and hands them to the agent by listing, across chats (D2), so a second store can drift and costs a lot. As a grant it is worse: every agent can read `config.json`, the repo's own rule keeps authorization records out of it (§14), and where the sandbox is off an agent's shell can write it. |
| **A prompt on every credential request, by default.** | In personal use it asks the owner again and again for the same pair, the listing included, and where nobody is at the prompt the run ends in a wait. Trust, YOLO and an `auto` run answer it anyway. It is kept as M8, an opt-in switch. |
| **A hook in the agent spec that checks the vend.** | On the engine the test host runs (#43263 names its version), the harness does not run a spec's `preToolUse` hook, and a tool in `allowedTools` raises no request, so the hook would never fire. The pair is judged in the gateway at the permission request instead (§8). |
| **Memory as the place that remembers the account.** | The owner is not its only writer, so it is not a record of what the owner granted. The outside store has the same fault, which is why what is found there is called a hint. |
| **The credential server on a Bot's own chat.** | A recorded decision says a conductor mounts only Kiro Crew's own servers. The worker gets it (M9). |
| **A terminal command that records a grant** (for the first-use record or for the key opt-in). | A session's shell can run it. The writers are the prompt's "always" and an owner-gated dashboard page. |
| **Opening the owner's AWS files or sign-in cookie to a session**, so that `ada` or a profile's `credential_process` works inside it. | It puts the material the bar protects in reach of the agent's shell. On a tool host it is also not Kiro Crew's to open. |
| **Not setting the two profile selectors** for a bound folder on a tool host. | A plain `aws` command would then use the default profile, which can be another account. The selectors stay set; the tool fills the name, or the agent uses the two fallback forms of M2. |
| **Finding the credential server by keyword** for a rule or a claim. | The keyword match was built for a hint. A rule needs an exact name, or the same command and arguments. |
| **Passing the missing half of the key** to fix cause 5. | It puts a long-lived secret in every child by default. The whole set is dropped instead (M5), and passing it is the last opt-in (M12). |
| **A detector for the outside tool.** | Kiro Crew has none. The only signals the design uses are the session's own server names and the spawn's hidden set. |

## 16. Open questions and the decision

Questions for a maintainer:

1. **Which PRs to build.** Stage 1 (PR 1 to PR 4) removes the push to a named
   profile by itself. The rest can be accepted, changed or dropped stage by
   stage.
2. **The first-use check (PR 10).** Build it, at the cost in §8, or keep M8's
   switch as the only guard on a vend and accept that G6 (a) rests on the owner's
   sign-in.
3. **The key pass-through opt-in (PR 11).** Build it or not. It should be ruled
   together with Decision 2 of
   [rfc-host-credential-vending.md](rfc-host-credential-vending.md).
4. **PR 8.** It opens the sealed additions record to a Bot's worker for one kind
   of server. Accepting this document accepts that, on the owner's act per Bot.

Questions the probes answer: S0-1, the M8 live check, S0-3 and S0-5 (§11.3).

**Decision:** none yet. Implementation is on hold. When a maintainer decides,
this section records who, when, and which PRs.
