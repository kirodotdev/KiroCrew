---
title: Owner approval rules, a named tool call that only the owner can approve
status: draft
author: unstablebrainiac
created: 2026-11-24
last-audited: 2026-11-24
audited-at: ff12400896
doc-pr:
implementation-prs: []
tracking-issues: [42181]
supersedes: []
superseded-by: []
---

# RFC: Owner approval rules

- Status: draft. Acceptance is requested from a maintainer; the status flips to
  `accepted` when one records it here. Nothing in this document is on main.
  Tracking issue: [#42181](https://github.com/kirodotdev/KiroCrew/issues/42181).
- Measured at `ff12400896`. Every statement about current behaviour names the
  file and symbol it was read from.
- Related: [rfc-deny-session-override.md](rfc-deny-session-override.md) (a deny
  rule a person overrides on the card),
  [rfc-owner-written-mcp-auto-approve.md](rfc-owner-written-mcp-auto-approve.md)
  (owner-written grants), the decision
  [2026-11-20-agents-ask-before-changing-gateway-trusted-files.md](../decisions/2026-11-20-agents-ask-before-changing-gateway-trusted-files.md)
  (only the person's click writes the files the gateway trusts), and the
  decision
  [2026-10-29-crew-member-approvals-notify.md](../decisions/2026-10-29-crew-member-approvals-notify.md)
  (a Bot worker's approval is decided on its conductor's card).

## Summary

An owner can name a tool call, by tool and by argument, that no agent may make
without the owner's own approval. I call one such entry an **owner approval
rule**. On a harness that can be made to send the call to Crew's gate, a call a
rule covers goes to the owner's approval card on every surface that has one,
and is refused on every surface that does not. Nothing that approves calls on
its own answers it: not Trust, not YOLO, not "approve for me", not an `auto`
cron, not a trusted pattern, not an `auto_approve_tools` grant, not a tool's
own `allowedTools` or `autoApprove` entry, and not an allow rule in the
harness's own settings. A session on a harness that cannot be made to send the
call to the gate does not start while a rule is on ("Holding a call, per
harness"). A rule can carry exceptions (the calls it does not cover) and can
let the owner approve ahead for one target, such as one code review.

Rules live in `config.json`, which the sandbox already seals read-only and
the file tools already refuse to write. Only the machine owner can create,
edit or delete a rule, turn an edition rule on or off, or approve ahead: a
Shared Crew teammate, an app token and an agent are refused ("Who can change a
rule"). An edition can ship rules the owner turns on. The approval is the
owner's answer on the card, which an agent cannot give.

## Motivation

### The case that started it

My standing rule for my own agents is that they never publish a code review,
and never turn on a review's auto-publish, unless I said "publish" for that
review. A short list of my own packages is the exception: there, an agent may
publish once its checks pass. Steering prose is the only thing that holds the
rule today, so I built a local PreToolUse hook to enforce it. It works for the calls it sees,
and I decided not to install it, because its own install notes list gaps that a
user hook file cannot close:

1. **An agent can remove it.** The hook is a file under `~/.kiro/hooks`. The
   sandbox seals `~/.kiro/agents` read-only
   (`sandbox_tiers._resolved_kiro_agents_targets`), and has no seal for the
   hooks directory (`agent.resolved_kiro_hooks_dir` is not read by any
   `sandbox*.py` module). Deleting the file turns the rule off from the next
   turn.
2. **Script crons skip it.** A script cron calls tools through
   `ScriptContext.call_tool` (`cron_script_context.py`), which reaches
   `KeptMcpServers.call_tool` (`cron_script_mcp.py`). That path refuses a tool
   the user switched off and runs no other gate: no `GATE_TIERS` row, no
   PreToolUse hook. A "publish once the checks pass" cron is written exactly
   that way.
3. **Agent-written scripts are opaque.** A hook reads the command line, so
   `python3 my_publish.py` tells it nothing.
4. **A timeout lets the call through.** A user hook file follows kiro-cli's
   rule that only exit code 2 refuses, so a hook killed for running past its
   time limit allows the call. The local hook had to keep its own lookups well
   under that limit for this reason.

### What main already has, and why none of it is this

| Mechanism on main | What it does | Why it does not enforce an owner rule |
|---|---|---|
| `hooks.confirm_tools` ([mcp.md, "Servers the operator asked to confirm"](../architecture/mcp.md)) | A tool pattern always asks; static grants on that server are withheld (`governance.may_skip_gate_now`) | Matches the tool only, never an argument, so a review tool's comment and publish actions ask alike. The same section says "a session in Trust or YOLO mode still approves what the user told it to". |
| Deny rules with the card override ([rfc-deny-session-override.md](rfc-deny-session-override.md)) | A refused call becomes a card in a dashboard session a person drives | Command text and tool-name globs only, never an argument, and elsewhere the call is simply refused. **Allow for this session** opens every later call of the same shape. No exceptions by target. |
| `auto_deny_tools` | Refuses a tool pattern | Refuses the call I do want, after I approve it. |
| Held calls (`mate_held_rules.held_for`, `web_ask_rule.needs_person`) | A call is never approved without a person: the dashboard (`dashboard/chat_turn/tool_approval.py`), Slack (`slack/gateway_runtime/turn_approvals.py`), the headless turn path (`llm_helpers_parts/tool_approval.py`) and an `auto` cron's approver (`cron_service/fields.auto_job_approver`) all send it to the card, and a surface with no card refuses it | Exactly the behaviour I need, but only two sources feed it: a Bot limit a migration could not carry, and an organisation's web `ask` rule. An owner cannot add a source. |
| Edition seams (`SecurityOverlay.extra_deny_patterns` in `platform/security_authority.py`, `DeniedRuleProvider.denied_rules` in `platform/interfaces.py`) | An edition adds deny patterns | Deny only. Neither can say "ask the owner". |

So the core already knows how to make a call reach a person and nobody else.
What is missing is a way for the owner to say which calls.

### Benefits, as outcomes

- A covered call made by an agent in YOLO, by a Bot worker, or by an `auto`
  cron stops at the owner's card. Today each of those paths approves it on its
  own once a grant or a trust mode applies.
- A `command` cron whose shell line a rule covers is refused when it is stored
  and when it fires, unless the owner approved that target ahead. Today it
  runs.
- Neither an agent nor a Shared Crew teammate can turn a rule off. Today an
  agent deletes one file and the local hook is gone.
- When the gate cannot decide (a lookup fails, the config cannot be read, the
  gate crashes), the call asks or is refused. It never runs unapproved. Today a
  hook that runs out of time allows the call.
- An owner's exceptions are data in their own config, not code in a hook
  script they must maintain.

## Use cases

### 1. An agent publishes a code review

The edition ships a rule for its code review tool, and I turn it on in
Settings > Security. The rule covers the review tool's `publish-revision`
action, the same tool called with auto-publish on, and the review CLI's
`--publish` and `--auto-publish` flags. Its exception covers a review whose
packages are all on my list. It lets me approve ahead per review for the
publish action, and not for auto-publish.

1. A worker under its conductor finishes a change in one of my packages, its
   checks pass, and it calls the publish action. The gate asks the edition's
   resolver for the review's packages. All are on my list, so the exception
   applies and the call goes through the normal gate: the worker's grants
   approve it as they do today.
2. The same worker, on another team's package, calls the publish action. No
   exception applies. The call is held. My conductor's card shows the rule's
   summary ("Publish a code review revision"), the review, and the packages.
   The worker's YOLO mode and its `allowedTools` grant do not answer it.
3. I am in a meeting. The card goes unanswered for
   `agent.tool_approval_timeout_secs` (600 seconds by default,
   `config/sections_agent.py`) and the call is declined with the existing
   "the approval prompt went unanswered" notice, so the worker knows nobody
   refused it and nothing was published.
4. Later I open the card's review and pick **Allow for this review**. The
   worker publishes. A later revision of the same review is covered by that
   approval; another review is not.
5. A different agent tries `--auto-publish`. The rule offers only **Allow
   once**, so even an approved review never turns auto-publish on without a
   fresh answer.

### 2. A Bot worker merges a pull request

This one needs no edition. I add a rule in Settings > Security for my GitHub
MCP server's `merge_pull_request` tool and for `gh pr merge` in the shell, with
no exceptions and no approval ahead.

1. A worker in YOLO decides a PR is ready and calls `merge_pull_request`. The
   server's `allowedTools` grant is withheld (the same withholding
   `confirm_tools` uses), so the call raises a permission request, and the
   held path sends it to the conductor's card.
2. The worker tries `gh pr merge 123 --auto` instead. The shell matcher holds
   that too.
3. A Shared Crew teammate looking at the conductor sees the card but cannot
   answer it: an owner approval takes the owner's answer only. The teammate
   cannot delete the rule or approve the PR ahead from Settings either.

## Design

### Rules

A rule is one object in a new list, `hooks.owner_approval_rules`, in `config.json`
or `config.local.json`. Both are in the sandbox's read-only set
(`sandbox_leaves._CREW_READONLY_LEAVES`), so an agent shell cannot write them,
and the file tools refuse them (`security/paths_lists._WRITE_PROTECTED_HOME_PATHS`).
The dashboard's Settings > Security page is the writer, for the machine owner
only ("Who can change a rule").

```json
{
  "hooks": {
    "owner_approval_rules": [
      {
        "id": "pr-merge",
        "summary": "Merge a pull request",
        "tool": "@github/merge_pull_request",
        "shell": [{"prefix": "gh pr merge"}],
        "approve_ahead": "",
        "except": []
      }
    ]
  }
}
```

| Field | Meaning |
|---|---|
| `id` | Stable key; the audit `rule_id`. |
| `summary` | The line the card leads with. |
| `tool` | One verified MCP identity, `@server/tool`. Matched against the host-trusted identity only, never the display title, so a model-chosen title cannot dodge it. |
| `when` | Optional. Argument conditions, all of which must hold: `{"action": "publish-revision"}` or `{"autoPublish": true}`. A string value is an exact match; a list is "any of". An argument that is absent fails the condition. |
| `shell` | Optional. Program-and-subcommand prefixes, matched at command position with the same grammar the deny rules use (`security.is_denied`), plus required flags: `{"prefix": "gh pr merge"}` or `{"prefix": "gh pr review", "flags": ["--approve"]}`. |
| `approve_ahead` | Optional. An argument (or a resolver fact) that names the target an approval covers, such as the review id. Empty means **Allow once** only. |
| `except` | Optional. Exceptions, below. |

Rules only add holds. A rule never approves anything, and a deny tier still
refuses a call before any rule is read.

### Per-owner exceptions

An exception says which covered calls the rule leaves alone. It is data, in the
owner's own config, in one of two forms:

- an **argument exception**, the same shape as `when`: the call is excepted
  when every condition holds;
- a **fact exception**: `{"fact": "packages", "all_in": ["PkgA", "PkgB"]}`. A
  fact is a value the call's arguments do not carry, read by a resolver an
  edition registers by name (below). `all_in` holds when the fact is a
  non-empty list and every member is in the given list (case-insensitive).
  `any_in` and `equals` are the other two operators.

An excepted call is not approved by the exception. It goes back to the normal
gate and is approved or asked exactly as it would be with no rule. A fact the
resolver cannot produce makes the exception fail, so the call is held.

### Edition rules and resolvers

A new optional seam, `OwnerApprovalRuleProvider`, beside `DeniedRuleProvider`
in `platform/interfaces.py`:

- `owner_approval_rules()` returns rules in the shape above, each with an
  `<edition>-` prefixed id. They are listed in Settings > Security and are
  **off** until the owner turns one on, because a rule changes how every
  matching call behaves for that person. The on or off state is stored in
  `config.json` like the rest of the section.
- `owner_approval_facts(rule_id, call)` returns the named facts for one call.
  It runs in the gateway process, outside the agent sandbox, under a 10-second
  limit. A raise, a timeout or a missing fact is "no fact".

An edition whose users publish to a code review service would ship its rule
this way, with a resolver that reads a review's packages as the owner. That
rule and resolver are the edition's own change, made in the edition's
repository once this seam exists.

### Who can change a rule

A teammate who cannot answer the held card must not be able to remove the
rule, turn an edition rule off, or approve ahead instead. So every write in
this RFC is owner-only, and "owner" means the machine owner alone.

Main already has that check:
`dashboard/handlers/source_providers.is_host_owner_dashboard_request`. It is
true only for a request whose `app` claim is present and empty and whose
`user` is the configured owner (`state.owner_id`), or, with no owner
configured, one of the signed local bootstrap subjects
(`_LOCAL_DASHBOARD_OWNER_SUBJECTS`). That one check refuses each caller this
section is about:

| Caller | Why `is_host_owner_dashboard_request` refuses it |
|---|---|
| A Shared Crew teammate | The ordinary owner gate, `is_owner_dashboard_request`, admits a teammate (`shared_crew/teammate_access.teammate_passes_owner_gate`). This check does not: a teammate's `user` is their own subject (`ssm:`, `relay:`, `tailnet:uid:`), never the owner's. |
| An app token | Its `app` claim names the app, so it is not empty. |
| An agent, through an MCP tool or the internal secret | `dashboard/token_auth.py` leaves the `app` claim absent for an internal caller, and the check needs it present and empty. |

One limit stays, and this RFC does not close it. An app's backend process that
the gateway verifies as the local owner can mint an owner token through
`/api/token/local` (`dashboard/handlers/core_local_token.api_token_local`).
That token carries no `app` claim, by design, so this check, like every owner
route on main, cannot tell it from the owner. An agent's shell or sandbox
cannot mint one: `member_memory_auth.local_owner_bootstrap_provenance` refuses
it there. So the owner-only writes rest on the owner trusting the apps they
installed, as the rest of the owner's dashboard already does.

The config PATCH route already uses it this way. A teammate passes
`require_owner_dashboard_request` there, and
`dashboard/handlers/core_config_patch.api_kirocrew_config_patch` then refuses
an admission key (`teammate_access.is_admission_config_key`) with 403
`owner_only` unless `is_host_owner_dashboard_request` holds. This RFC adds:

1. **Config writes.** `hooks.owner_approval_rules` and the edition rules' on or
   off state join the keys that branch refuses, so a PATCH of either from
   anyone but the machine owner is 403 `owner_only`. A change card is applied
   by the browser sending its plan step to that same route
   (`dashboard/handlers/change_cards.py`, module docstring), so a card cannot
   get round it. The card catalog also refuses to propose these keys, beside
   `change_cards_settings._refuse_locked`, so an agent cannot put such a card
   in front of a teammate.
2. **New routes.** The Settings page's rule, edition toggle and approve-ahead
   routes are added to `teammate_access.is_owner_only_for_teammates`, so the
   shared owner gate refuses a teammate on them by path, as it already does
   for the Terminal and a cron job's secret grants. Each handler also calls
   `is_host_owner_dashboard_request` itself before it writes, so a route added
   later without the path entry still refuses.
3. **Card approve-ahead.** **Allow for this review** on a held card writes its
   record through `POST /api/chat/slots/{slot}/approve`, which for a held owner
   approval refuses a teammate and an app token (next section).
4. **No MCP writer.** No MCP tool writes a rule, an edition toggle or an
   approve-ahead record, and this RFC adds none. An agent's shell and file
   tools cannot write `config.json` or the record leaf (Rules, above).

Every refusal is audited with the existing `log_api_access` row and returns
"only the Crew owner can change owner approval rules".

### Holding a call

Every surface already asks the held sources before any auto-approve
(Motivation table). Owner approval rules become a third source, read at those
same call sites:

1. `mate_held_rules.held_for` gains a sibling, `owner_approval_rules.held_for(event)`,
   returning the summaries of every rule that covers the call and is not
   excepted. Every call site of `held_for` and `needs_person` unions it into their held list.
2. A held call reaches the card with the rule's summary as the card notice
   (the existing `card_notice` slot), or is refused on a surface with no
   approver with "this call needs the owner's approval (rule `<id>`), and this
   surface has no approver".
3. The trust sweep that approves pending cards when a person turns trust on
   skips a held card, as it already skips a `web_ask_rule` card
   (`web_ask_rule.CARD_META_KEY`).

Those steps act only on a call that raises `session/request_permission`. A
call the harness approves by itself never reaches Crew's gate
(`agent_sdk/backends_routing.py`, "How a harness is made to ask"), so the
held path never sees it. The next section is how each harness is made to ask.

### Holding a call, per harness

The hold has two levels. **At the harness**, every call to a server an
enabled rule names, and with a `shell` rule every shell call, must raise a
permission request: the harness keeps no pre-approval for them. Shell is taken
whole because a prefix match on the harness's side would miss
`bash -c 'gh pr merge 1'`. **At Crew's gate**, only the call a rule covers is
held for the owner. A call that reached the gate only because its server or
shell lost its pre-approval is judged as if no rule existed, so an
`auto_approve_tools` grant, Trust or YOLO approves it as today. That is where
this differs from `confirm_tools`, whose gate match outranks every grant on its
server.

Bot held limits already need the harness level, and main does it per harness.
This RFC reuses those mechanisms, keyed by rule instead of by Bot, and adds a
named opt-in set, `ACP_BACKENDS_OWNER_RULE_HOLD`, so a harness is in it only on
evidence (harness parity H6):

| Harness | How a covered call still reaches the gate | In the set |
|---|---|---|
| Kiro CLI | The spec Crew writes takes no grant on a rule's server: every grant writer calls `governance.may_skip_gate_now`, which answers "no" for it as it does for a `confirm_tools` server. A `shell` rule strips the shell pre-approvals too (`allowedTools` shell refs, the grant-only `toolsSettings` keys such as `allowedCommands` and `autoAllowReadonly`, `permissions` allows), with the same `mate_held_rules._strip_caps` held limits use. A spec Crew does not write (a named agent's own file, read by `mate_held_rules._kiro_cli_spec`) is checked the way `kiro_cli_helper_refusal` checks a Bot helper: if it pre-approves a covered call (`_pre_approves_held`), or cannot be read, the session is not started on Kiro CLI. | yes |
| KAS | The session projection asks `may_skip_gate_now` for each of the spec's own allows (`acp/kas_agents._authored_allow_gate`), so a rule's server gets none, and `permissions` allows for the covered capability are removed as `_without_allows` does. A child the session starts (a named `spawn_run` agent, an `availableAgents` target) is narrowed where `held_child_spec` narrows it today (`acp/harness/kas.py`, `acp/kas_subagent_targets.py`): its pre-approvals go and an `ask` rule is added. A child spec that cannot be narrowed is not started (`HeldLimitsNotApplied`). | yes |
| Claude | The operator's own `~/.claude` allow rules and servers are outside Crew's spec, so the session envelope carries what the held-limit path carries (`acp/client_claude_session._claude_session_meta`): a `permissions.ask` rule per rule's server (`mcp__<server>`, from `claude_mcp_server_rule`) and, for a `shell` rule, for each shell built-in (`Bash`, `Monitor`, `PowerShell`, `TaskStop`, `CLAUDE_WORKER_BUILTINS_BY_KIRO_TOOL["execute_bash"]`). An ask outranks an allow there. It also carries `strictMcpConfig`, so no server from the user's, the project's or a plugin's config runs beside the asked ones, and `allowDangerouslySkipPermissions` off. The starting mode is pinned to `default` by `_pin_claude_starting_mode` on every such session, not only under `KIROCREW_CC_PIN_INHERITED_MODE`, and a failed pin stops the harness as it does today. An adapter below `CLAUDE_ACP_SETTING_SOURCES_MIN_VERSION` (0.84.0, the oldest the pin was checked on) is not started. | yes |
| pi, strands | No pre-approval exists to remove. Crew's gate extension turns every tool call, built-in or MCP, into `session/request_permission`, and a session without it loaded is refused before its first prompt (`Routing.VERIFIED_GATE_EXTENSION`; `agent_sdk/backends_mcp.py`, the `ACP_BACKENDS_HONOR_ZERO_TOOL_BAN` notes). | yes |
| opencode | The start read-back accepts only a resolved permission that asks for every call, a trailing `"*": "ask"` included, and refuses the session otherwise (`acp/harness/opencode._opencode_uniform_permission`). | yes |
| goose | Its user home's `permission.yaml` can pre-approve a tool in `approve` mode (`acp/harness/goose.py`). Only a Side Chat runs in a home without it. | no |
| codex | It runs a command its sandbox allows, and a tool from an MCP server in the operator's own `config.toml` that it approves itself, without asking (`acp/session_handle_permissions.PARTIAL_ZERO_TOOL_BAN_NOTICES`). | no |
| copilot, deepseek, mimocode, antigravity | No capture shows a covered call still asking against the harness's own pre-approvals. mimocode and antigravity run some of their own tools unasked (`ACP_BACKENDS_UNASKED_NATIVE_TOOLS`). | no |

**Outside the set, the session does not start.** While any rule is on, a
session on a harness outside `ACP_BACKENDS_OWNER_RULE_HOLD` is refused before
its first prompt, the way a Bot worker whose limits its harness cannot carry is
refused today (`mate_worker_tool_limits_supported`). The notice names the rule
and the harnesses that can hold it. A harness joins the set with a live capture
in which a call its own config pre-approves still raises a permission request.

**Sessions already running.** A spec or envelope is read when the harness
starts. So saving or turning on a rule ends the harness process of every live
session on Kiro CLI, KAS or Claude that started before the change, and the
session gets a new process on its next prompt. Crew's gate reads the rule at
once for every call that does reach it.

So "every surface" in the Summary means every surface of a session on a
harness in the set. No other session runs while a rule is on.

### How approval is recorded so an agent cannot forge it

- **Once.** The card's answer is the record. It reaches the gateway only on
  `POST /api/chat/slots/{slot}/approve` (`dashboard/chat_api/slot_approval.py`)
  or its conductor twin. That route takes the owner's dashboard token. The key
  that signs those tokens is masked in every sandbox
  (`sandbox_leaves._CREW_UNREADABLE_MASK_LEAVES`) and the CLI that mints one is
  a self-protection deny (`security/argv_floor_self.py`). For a held owner
  approval the route refuses two callers it accepts today for other cards: an
  app token holding the session approval grant, and a Shared Crew teammate.
- **Ahead, for one target.** **Allow for this review**, and the Settings page's
  approve-ahead form, write one record per `(rule id, target)`. Records are
  signed the way `kept_grants.py` signs kept grants (an HMAC under a key
  derived from the token signing key), with a new label, and live in a new
  leaf added to `_CREW_READONLY_LEAVES`. A record that is missing, unsigned,
  wrongly signed or for another rule reads as absent. Only the gateway process
  reads them, because the key is masked in every sandbox: the held-card path
  and the `command` cron's fire-time vet both run there. An owner removes one
  from the same page.
- **Never from text.** Nothing in a prompt, a tool result, a cron message or a
  file an agent can write counts as approval.

### Cron and script paths

| Path | Today | With this RFC |
|---|---|---|
| A prompt cron, any approval mode | Its turn's calls go through the held path; `auto_job_approver` already sends a held call to the card | Covered by step 1 of "Holding a call" with no extra change |
| A `command` cron | `mcp_cron_vet._vet_shell_command` refuses a deny match at store time, and the gateway re-vets at fire time (`cli_commands_cron.py`, `cron_service/execution.py`) | Also refuses a `shell` rule match at store time, and at fire time unless an approve-ahead record covers the target. Both checks run in the gateway |
| A script cron's `ctx.call_tool` | `KeptMcpServers.call_tool` checks switched-off tools only | Unchanged, and not covered. See below |
| A script an agent writes and runs from a shell | The shell line is judged; the script body is not | Unchanged. See Downsides |

**A rule cannot hold a script cron's tool calls.** `KeptMcpServers.call_tool`
runs inside the script's own process, which is agent-written code
(`cron_script_mcp.py`: `McpToolClient` spawns the MCP server from that process).
A check placed there is one the script can skip, by calling `McpToolClient`
directly or by replacing the method. That process also cannot read the token
signing key, so it could never verify an approve-ahead record. So script crons
fall under the existing non-goal for agent-written programs. The gap is real:
an owner's own script crons can publish a code review today, and a rule does
not stop them. "Future work" below names the broker that would close it.

### When the gate cannot decide

Every case fails toward a person, and never toward running the call:

| Case | Result |
|---|---|
| A resolver raises, times out or returns no fact | The exception does not apply; the call is held |
| The `hooks` section cannot be read as written (the cases `confirm_tools_unknown` covers today) | Every MCP call and every shell call is held, and every grant is withheld, until the file reads again |
| A rule entry is malformed | That entry holds every call to its `tool` server, and the Settings page shows it as broken |
| The rule check raises | The call is held. The check never raises out, like `mate_held_rules.held_for`, but where that function treats an unreadable record as "nothing held", this one treats any error as "held" |
| The card goes unanswered | Declined after `agent.tool_approval_timeout_secs`, as today |
| A surface has no approver (a `command` cron, read-only side chat, a channel with no card) | Refused with the rule id |
| The session's harness is outside `ACP_BACKENDS_OWNER_RULE_HOLD` | The session is not started while any rule is on |
| A Kiro CLI spec Crew does not write cannot be read, or a KAS child spec cannot be narrowed | That session or child is not started |
| A Claude session's mode pin fails, or its adapter is below the pin's version floor | The harness is stopped before its first prompt |

## Migration plan

Each phase is one PR and leaves main working.

1. **MCP rules, held on every turn surface.** The `hooks.owner_approval_rules`
   schema and its owner-only write check, `owner_approval_rules.held_for`,
   every held call site, the owner-only answer on held cards,
   `ACP_BACKENDS_OWNER_RULE_HOLD` with the harness-level hold for MCP on each
   member, and the start refusal for every other harness. Exit criteria: with a
   rule on `@kirocrew-core/send_message`, a call in a YOLO dashboard session
   reaches the card on Kiro CLI, KAS and Claude, including on Claude with a
   `~/.claude` allow rule for that tool; the same call in an `auto` cron turn
   reaches the cron's card; an app token and a Shared Crew teammate get 403 on
   that card and on a PATCH of `hooks.owner_approval_rules`; a codex session
   is refused at start; a malformed rule holds every call to its server.
2. **Shell matchers and command crons.** `shell` prefixes in the chat gate and
   in `mcp_cron_vet`, and the harness-level shell hold. Exit criteria:
   `gh pr merge 1` and `bash -c 'gh pr merge 1'` are held in a YOLO session on
   each member harness, on Claude with a `Bash` allow rule in `~/.claude` and on
   Kiro CLI with `gh` in `allowedCommands`; `ls` in the same session is approved
   by YOLO; a `command` cron running `gh pr merge 1` is refused at `cron_add`.
3. **Approve-ahead records.** The signed records, **Allow for this <target>**
   on the card, the Settings page, and the `command` cron's fire-time check.
   Exit criteria: after **Allow for this review** on review A, a later covered
   call for A runs without a card and one for B is held; a `command` cron's
   covered line runs at fire time for an approved target and is refused for
   another; a hand-edited record is ignored.
4. **Edition rules and facts.** `OwnerApprovalRuleProvider`, off by default,
   listed in Settings. Exit criteria: a test edition's rule is off until turned
   on; a resolver timeout holds the call; an `all_in` exception lets an
   excepted call through to the normal gate.

Owners with a local PreToolUse hook for the same purpose remove its file once
phase 3 is on their build. No stored data changes shape: with
`hooks.owner_approval_rules` absent, every phase behaves exactly as main does today.

### Future work: a gateway broker for script-cron tool calls

Script crons stay outside every phase above, and that leaves a real gap
(Motivation, gap 2). Closing it needs the script's tool calls to leave the
script's process before they reach a server. A gateway-side broker would do
that: the script's `ctx.call_tool` sends the server, tool and arguments to a
gateway route over the cron run's own credential, and the gateway runs the
rule check, reads approve-ahead records with the key it already holds, and
then calls the server itself. For that to bind, the script must lose its own
way to the server: it could no longer spawn MCP servers or read their
configured credentials, which changes what every existing script cron can do
and needs its own RFC.

## Backward compatibility

Compatible. A new optional config key and a new optional edition seam. With no
rules, no code path changes its answer. An edition that does not implement the
seam contributes nothing.

## Downsides, and how each is bounded

| Downside | Bound |
|---|---|
| More cards for the owner | Exceptions take routine targets out, and approve-ahead covers a whole review in one answer. Rules are off until the owner writes or enables one. |
| Unattended work that used to run now fails | Only for calls the owner named. The refusal names the rule and the page to approve ahead, so the failure is visible and one action fixes it. |
| The harness asks for every call to a rule's server, and with a `shell` rule for every shell call | The ask goes to Crew's gate, not to a person: an uncovered call is approved there by its `auto_approve_tools` grant, Trust or YOLO, so the cost is a gate round trip, and a card only where no grant or mode approves it. Only the covered call is held for the owner. |
| No session runs on goose, codex, copilot, deepseek, mimocode or antigravity while a rule is on | The refusal names the rule and the harnesses that hold it. Each harness joins the set on its own live capture. An owner who needs one of them turns the rules off for that time. |
| Saving a rule restarts live sessions | Only sessions on Kiro CLI, KAS or Claude that started before the change, and each resumes on its next prompt. |
| A resolver adds a network call and latency to a covered call | Only calls that match a rule with a fact exception pay it, bounded at 10 seconds, and a failure asks rather than allows. |
| A config read failure holds everything | Rare, logged, and it clears on the next good read. Running a call the owner may have forbidden is the worse failure. |
| Hand-built requests and agent-written programs, script crons included, are not covered | Stated as a non-goal. The gate judges named tools and named command shapes, as the deny rules do. Script crons have a named path to coverage ("Future work"). Other programs need the target service's own credential out of the agent's reach, which is per service. |
| A Shared Crew teammate cannot approve, edit a rule or approve ahead | Intended: the rule is the owner's. The owner can add an exception or approve ahead. |
| A third policy concept beside deny rules and `confirm_tools` | It reuses the held path and the grant withholding rather than adding a gate tier, so there is one new list, not a new engine. `confirm_tools` could later become a rule with no `when`; that is an open question, not part of this RFC. |

## Security considerations

- Rules and approve-ahead records are trusted files, written only by the
  owner's own dashboard routes, and sealed in the sandbox. This follows the
  2026-11-20 decision. Each write checks
  `is_host_owner_dashboard_request`, which refuses a Shared Crew teammate, an
  app token and an internal caller alike ("Who can change a rule").
- A rule binds only sessions that can hold it. A session whose harness would
  run a covered call without asking does not start while a rule is on, so a
  harness's own allow rules, servers or modes cannot run a covered call
  unseen ("Holding a call, per harness").
- The tool match uses the verified MCP identity. A call whose identity cannot
  be read (`identity_unreadable`) is held when any rule names any tool, so an
  unreadable identity cannot dodge a rule.
- Argument conditions read the call's real parameters (`raw_params`), the same
  object the server receives, not the card's display text.
- A held card is never answered by an app token, a widget, a Dashboard page or
  a teammate. The existing protections against a page answering a card
  (`dashboard/card_send.py`) keep applying.

## Non-goals

- Judging the body of an agent-written script, or any program the shell runs.
  A script cron is such a program: its tool calls run in its own process, so a
  rule does not hold them ("Cron and script paths").
- Replacing deny rules, `confirm_tools`, or an organisation's governance
  ceiling. A rule cannot loosen any of them.
- Shipping any edition's own rule. An edition's code review rule is its own
  change in its own repository.

## Alternatives considered

1. **Install the local PreToolUse hook.** Rejected for gaps 1 and 4 in
   Motivation, which this RFC closes. Sealing `~/.kiro/hooks` would close the
   first gap only. Gaps 2 and 3 stay open under either design: script crons
   until the broker in "Future work", and other agent-written programs as a
   non-goal.
2. **Extend `confirm_tools` with argument matching.** Its meaning, "ask, unless
   the person told the session to trust", is right for its users and is
   documented. Making it outrank YOLO would change every existing
   `confirm_tools` list.
3. **A deny rule with the card override.** Shell only, dashboard only, and
   **Allow for this session** is wider than one review.
4. **A new `GATE_TIERS` kind.** The table's kinds are deny, policy deny, grant
   and classify, and a held call is not any of them: the held path already sits
   above the gate on every surface. A new kind would duplicate it.
5. **Edition-only, as an un-weakenable overlay deny.** It would refuse the
   calls I approve, and no other owner could use it.

## Open questions

1. Should a Slack approval from the owner's claimed Slack identity count, or
   only the dashboard? This RFC assumes the dashboard only in phase 1.
2. Should an organisation's governance ceiling be able to require a rule (pin
   it on) for its members?
3. Should `confirm_tools` later be expressed as rules?
