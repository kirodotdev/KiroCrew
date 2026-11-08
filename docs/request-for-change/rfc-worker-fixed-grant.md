---
title: A dispatched worker runs read-only commands and writes inside its own worktree without a prompt under Normal
status: draft
author: kirocrew-worker, directed by iamwhatever
created: 2026-11-08
last-audited: 2026-11-08
audited-at: a398062bc0
doc-pr: 33372
implementation-prs: []
tracking-issues: []
supersedes: []
superseded-by: []
---

# RFC: A dispatched worker runs read-only commands and writes inside its own worktree without a prompt under Normal

Status: draft. Nothing built; this document changes no code. It moves a trust
boundary, so it is RFC-first under
[CONTRIBUTING.md](../../CONTRIBUTING.md#pull-request-workflow). **The security
trade-off in section 6 is the repository owner's (iamwhatever) and the security
reviewer's decision**, not the author's. Every code reference below was read at
`a398062bc0`. Paths are under `src/kiro_crew/` unless they start with `docs/`,
`website/` or `test/`.

[rfc-conductor-own-child-verbs.md](rfc-conductor-own-child-verbs.md) section 4
("Not in scope: worker shell and file-write prompts") leaves worker shell and file-write prompts to "a later RFC". This
is that RFC. That document is implemented
([#33373](https://github.com/kirodotdev/KiroCrew/pull/33373)): a conductor's
`session_send` and `session_close` to its own children no longer prompt, and
the workers those sends reach still prompt on every shell call and write.

## 1. Summary

Under the Normal approval mode a dispatched worker (`kirocrew-worker`, or a
Bot's `kirocrew-mate-worker` whose Profile is Normal) stops on every shell
command and every file write until a person answers. An unattended Ultra Mode
or Bot run therefore stalls on `grep`, `cat` and an edit to a file in its own
worktree as often as on a `git push`.

This RFC adds one fixed, server-side, deterministic grant for those two agents,
decided per call at approval time:

| rule | approved without a prompt when | fact it reads |
|---|---|---|
| R1 read-only shell | `is_read_only_bash(cmd)` and not `command_invokes_git(cmd)` | `is_read_only_bash`, `command_invokes_git` in `security/readonly_bash.py` |
| R2 write in own worktree | `fs_write`, `fs_append` or `str_replace` naming one path inside the worker's recorded worktree, outside the fenced subpaths | `path_trust_target` (`trust_patterns.py`), `AcpEvent.path_scope_trusted` (`acp/types.py`) |
| R3 local git (phase 2) | a git subcommand on a fixed allowlist that does not publish, run inside the worktree, with no repository-configured program in reach | new, section 5.4 |

Everything else still goes to the person, on the conductor's approval card or
the worker's own tab, exactly as today. A config switch turns the grant off.
No model judges any call.

## 2. Motivation

### 2.1 Current state

- **A worker is born with its creator's posture.** `create_session` zeroes the
  child's trust in `create_session`
  (`dashboard/session_control_parts/session_create.py`) and then copies the
  creator's (`inherit_posture`). A worker of a Normal
  conductor is Normal.
- **Normal approves nothing by rule.** In `_decide_permission_request`
  (`dashboard/chat_turn/tool_approval.py`) a shell call reaches the
  Trust-reads rung only when the slot itself has `_trust_reads` set
  (the "Trust-reads" branch), and the Trust/YOLO rung only when
  `_slot_is_trusted` holds. With neither, every call falls to `_on_permission_prompt`
  (`dashboard/chat_turn/permission_prompt.py`), the card.
- **Trusted patterns do not reach workers.** A person's per-command grants on
  the conductor are deliberately not inherited (the
  `_trusted_patterns -- NOT inherited` comment in `create_session`): "a dispatched worker runs model-authored
  work they have not seen, so the same glob can admit a command the grant was
  never asked about".
- **Bot workers follow the Profile.** `kirocrew-mate-worker` takes its mode
  from the crewmate's Profile, not its creator
  (`is_profile_worker` in `dashboard/approval_inheritance.py`). An unset Profile mode is
  effective Trust (`MEMBER_APPROVAL_MODE_UNSET_EFFECTIVE` in
  `config/sections_agent_fields.py`), so only a Profile set
  to Normal or Reads hits this problem; this RFC changes only Normal.
- **The conductor cannot decide a worker's card.** The only decider is the
  person, through the owner-only route
  `POST /api/chat/slots/{slot}/crew-workers/{worker}/approve`
  (`api_crew_worker_approve` in `dashboard/handlers/crew_workers.py`). The card
  says why: "only the person may grant it" (the header comment of
  `website/src/components/crew/CrewApprovalCards.tsx`).
  The conductor sees only the tool title, redacted and truncated
  (`_pending_approval_fields` in
  `dashboard/session_control_parts/session_status.py`).

### 2.2 The problem

The owner's intent: under Normal, a worker should not stop on every command.
When a command needs a check, it is checked before it reaches the person, not
handed over by default.

Most of a worker's calls are reads of the repository and edits inside its own
worktree. Both are facts a program can check from the call itself. Today they
cost the same person-click as a push or a delete, so the person either answers
dozens of routine cards per item or picks Trust.

### 2.3 What people do today: Trust

Trust on the conductor reaches every local worker it creates (inheritance
above). It is wider than the problem: every tool, every argument, no path
fence, the whole child tree, and it is lost on a gateway restart
("Not persisted at birth" in `create_session`). It stays available; this RFC does not change
it.

## 3. Goals

- G1. A dispatched `kirocrew-worker` or `kirocrew-mate-worker` under Normal
  runs R1 and R2 calls with no prompt. Reads is unchanged: it keeps approving
  read-only shell on its own rung, and in-worktree writes still prompt there.
- G2. Every other call prompts exactly as today, on the same card.
- G3. The decision reads only the verified call (tool identity, raw arguments,
  the worker's recorded worktree). It reads no prose: not the worker's report,
  not issue or PR text, not the conductor's view.
- G4. Deny rules and PreToolUse hooks run first and still win.
- G5. One config key turns it off.
- G6. Every call it approves writes one SEL row naming the rule.

## 4. Non-goals and not in scope

- **Conductor review of the leftover cards** (plan step 7 of the research note
  this RFC grew from). An LLM conductor approving another model's calls is a
  separate RFC, and only if this one leaves too many cards (section 7).
- **Keeping Trust across a restart.** Trust read back from disk an agent can
  edit is its own trust-boundary question.
- **Remote (mesh) peers.** "Approvals stay the peer's"
  ([rfc-conductor-remote-dispatch.md](rfc-conductor-remote-dispatch.md)).
  A peer's worker gets this grant only from its own gateway's config.
- **Card "Trust this crew member" carrying to a worker's own children.**
  Unchanged (`trust_carries` in `dashboard/approval_posture.py`).
- `crew_mode.APPROVAL_ROUTING_LIVE`. That switch moves questions and a few
  conductor reads; it does not touch worker tool approvals. Not flipped here.
- Any change to Trust, YOLO, trusted patterns, or what the person may approve
  by hand.
- Other agents (`kirocrew`, conductors, research, a person's own tab running a
  worker template).

## 5. Design

### 5.1 Where the decision is made

A new rung in `_decide_permission_request`, after the deny-rule card
(the `TOOL_DENY` branch) and the trusted-pattern rung, before the
Trust-reads rung. Before it approves, it fires the PreToolUse hooks when they
have not fired yet this call (`turn._pre_tool_hooks_fired`), the way the Trust
rung does; the earlier hook pass runs only inside the
`if state.context_builder:` block. It is a rung, not an
`allowedTools` name grant, because each rule depends on the arguments.

It applies only when all of these hold; otherwise it does nothing and the
existing rungs run:

1. The slot's server-held agent is `kirocrew-worker` or `MATE_WORKER_AGENT`
   (`mate_worker.py`).
2. The slot has a gateway-vouched creator (`_vouched_creator_of`
   in `tool_approval.py`). A person's own tab running the worker template is
   not a dispatched worker and gets nothing.
3. The slot is on Normal: not trusted, no `_trust_reads`, and YOLO does not
   reach it.
4. The event is full fidelity: not `child_low_fidelity`, not
   `tool_input_redacted`, and for R2 `path_scope_trusted`.
5. `agent.worker_fixed_grant` is true (section 5.5).
6. Not the claude backend, for the reason the own-worker exemption gives
   (the `_claude_backend` guard).

### 5.2 R1: read-only shell

The Trust-reads test, verbatim: `is_read_only_bash(cmd) and not
command_invokes_git(cmd)`, then the program-name check
`_name_grant_refusal_off_loop(cmd)` that Trust-reads runs, then the PreToolUse hooks (section 5.1). A
PATH shim or an unreadable program position falls to the card. No git, because
git runs whatever `.git/config` names (the `command_invokes_git` docstring).

### 5.3 R2: writes inside the worker's own worktree

**The fence.** A worker slot gains one server-held field, `write_root`: an
absolute, normal-form, link-free path. It is set once, at `session_create` or
`work_dispatch`, from a new optional `worktree` argument the conductor passes,
and is persisted with the slot. The gateway refuses the argument unless the
path:

- exists and is a linked git worktree (its `.git` is a file) of the repository
  at the slot's project directory (`slot.project`, set in `create_session`);
- is not that repository's main checkout;
- is not the `write_root` of another live slot.

No `worktree` argument means no `write_root`, and R2 does nothing; R1 still
applies. A worker cannot set or change its own `write_root`.

**The rule.** Approved when the tool is `fs_write`, `fs_append` or
`str_replace` (not `delete_file`, which is in `PATH_TRUST_TOOLS`
in `trust_patterns.py`), `path_trust_target` returns one path, the path is
under `write_root`, `path_trust_unresolved` is false (no link at or above it), and the path is not under a fenced subpath:

| fenced subpath | why |
|---|---|
| `.git` (file or directory, any depth) | hooks and config run programs |
| the effective `core.hooksPath`, when inside the fence | a hook written there runs on the next commit |
| `.kiro/`, `.claude/`, `.codex/` | agent config, steering and hooks |

The fenced list is a constant in the code PR, pinned by a test. Matching is
not a case-sensitive prefix test: both the path and each fenced subpath are
casefolded, and on Windows the path first goes through the alias folding that
`security/paths.py` applies (`_fold_windows_alias`), so `.KIRO/`, `.Claude/`
or a short-name alias still reaches the fence on macOS and Windows.

### 5.4 R3: local git that does not publish (phase 2)

Separate phase, so phase 1 ships without it. Approved when the command is one
git invocation, run with cwd inside `write_root`, with no `-c`, `-C`,
`--git-dir`, `--work-tree`, `--exec-path` or env prefix, whose subcommand is
on this allowlist:

| allowed | notes |
|---|---|
| `status`, `diff`, `log`, `show`, `rev-parse`, `branch --show-current` | reads |
| `add`, `commit` | writes the worktree's index and its own branch only |
| `fetch origin`, `rebase origin/<branch>` | no `-i`, `--exec`, `-x` |

Refused to the card: `push`, `config`, `remote`, `stash` (shared across
worktrees), `worktree`, `submodule`, `reset --hard`, `clean`, `checkout --`,
`restore` (drops uncommitted work), `branch -D`, `gc`, `prune`, `filter-*`,
`gh` of any kind.

Precondition, read at decision time: the effective config sets no
`core.fsmonitor`, no `core.sshCommand`, no `core.hooksPath` inside the fence,
no `filter.*`, `diff.*.textconv`, `diff.external`, `diff.*.command` or
`merge.*.driver`. Any one set sends the call to the card.

Hooks are a second precondition. `commit` and `rebase` run the hooks in the
effective hooks directory, which is shared by every worktree, and hook
managers there (pre-commit, lefthook, husky) read config files the worker can
write under R2 (`.pre-commit-config.yaml`, `lefthook.yml`, `.husky/`). So an
auto-approved `commit` or `rebase` would run commands the worker chose. R3
therefore cards `commit` and `rebase` whenever the effective hooks directory
holds any hook those commands run; it approves them only when no such hook
exists. If the owner and security reviewer find this too fragile, R3 is
dropped and git keeps prompting (section 10 Q2).

### 5.5 Default and config switch

New key `agent.worker_fixed_grant`, boolean. This RFC proposes default `true`
for R1 and R2, and R3 behind a second key `agent.worker_fixed_grant_git`,
default `false` until phase 2 is reviewed. Off restores today's behaviour
exactly. The code PR adds each key to the config schema, the settings page and
`config/defaults.json` together. The default is an open question (section 10
Q1).

### 5.6 What still goes to the person

Unchanged: deletes (`rm`, `delete_file`), `git push`, `gh pr create` and every
`gh` call, network and package installs (`curl`, `pip install`, `npm i`),
writes or shell outside the worktree, any shell that is not read-only, a
governance deny (card in every mode, the `TOOL_DENY` branch), a
low-fidelity request, cron authoring (already withheld from workers by
`_WORKER_EXCLUDED_GRANTS` in `agent_parts/spec_grants.py`), and the five ask cases
(credentials, spend, irreversible or someone else's work, an uninferable
product choice, the person asked to decide).

### 5.7 Audit

Each approval writes the row the other rungs write:
`sel().log_tool_invocation(outcome="auto_approved", metadata={"reason":
"worker_fixed_grant", "rule": "read_only_shell" | "worktree_write" |
"local_git", "write_root": <path>})`. The title is redacted as on the Trust
rung (`redact_exfiltration_urls`, `redact_credentials`). A refusal of the
`worktree` argument at create writes one row with the reason.

## 6. Threat model and security considerations

**The security trade-off below is the repository owner's and the security
reviewer's decision.**

### 6.1 The threat

A worker reads untrusted text: issue bodies, PR descriptions, review comments,
CI logs, web pages, and the conductor's seed, which may quote them. Prompt
injection in that text can steer the worker's next call. Today a person reads
every call first. With this RFC, an injected worker can, with no person:

- read any file the worker's user can read (R1);
- overwrite files inside its own worktree (R2);
- make local commits and fetches in that worktree (R3, phase 2).

### 6.2 What still stops it

| layer | what it bounds | where |
|---|---|---|
| no network write | R1 admits no network program; push, `gh`, installs prompt | `readonly_bash.py` deny-by-default tables |
| worktree fence | R2 writes land only under a gateway-checked `write_root`, never `.git` or agent config, never through a link | section 5.3 |
| no publish | nothing leaves the host without a card, so a poisoned worktree reaches nobody until a person approves a push | section 5.6 |
| governance deny rules | a deny rule goes to a card in every mode | `TOOL_DENY` branch of `_decide_permission_request` |
| PreToolUse hooks | an operator hook can block any call this rung would approve | fired as on the Trust rung (`_reject_hook_blocked`) |
| no pattern inheritance | the worker gets none of the conductor's trusted patterns | `_trusted_patterns` comment in `create_session` |
| fidelity | a call whose arguments are not harness-verified prompts | `child_low_fidelity`, `path_scope_trusted` |
| audit | every approval is an SEL row naming the rule | section 5.7 |

### 6.3 What it does not stop

- **Reading secrets.** R1 lets an injected worker `cat` a credential file. It
  cannot send it anywhere without a card, but it can put it in the worktree
  (R2) or in its `work_report` text, which the conductor reads. Reads mode has
  carried this exposure for every Reads chat since it shipped.
- **Poisoning the worktree.** R2 lets an injected worker add a malicious test
  or script. It does not run (running it is not read-only) and it is not
  published (push prompts), but a person who later approves `pytest` or a push
  without reading the diff runs or ships it. The files that run on such a
  later approved call are the ones to weigh for the fence: `conftest.py`,
  `Makefile`, the `scripts` in `package.json`, `.pre-commit-config.yaml` and
  `.envrc`. This RFC leaves them writable; section 10 Q5 asks whether R2
  should card them too. Under R3 these files would no longer run only on a
  later approved call: a git hook manager reads them on `commit` and
  `rebase`. Section 5.4's hooks precondition is what keeps those two
  subcommands on the card while any hook is installed.
- Today's Trust risk, unchanged, for workers of a Trust parent.

### 6.4 Why fixed rules, not an LLM conductor judge

| | conductor (LLM) decides | fixed server rules (this RFC) |
|---|---|---|
| who can talk it into yes | anything the conductor read: issues, PR comments, the worker's own `work_report`, the worker's tool args. That is why sends are withheld (the `_CREW_ROOT_OWN_WORKER_VERBS` comment) | nobody; it reads the call, not prose |
| stronger than the worker | no: same model family, same ingested text, plus worker-written text | n/a |
| same answer twice | no | yes, testable |
| covers new kinds of call | yes | only what the rules name; the rest asks |
| parts to reuse | none; needs a new verb, full-argument view, and a reversed product rule (`CrewApprovalCards.tsx` header) | `is_read_only_bash`, `command_invokes_git`, `path_trust_target`, `path_trust_unresolved`, the name-grant refusal |
| rough cost | 200-350 code + ~200 test lines | 80-150 code + 100-150 test lines |

The checks a conductor would run on these calls are fixed facts a program
reads better than a model. The rows that need judgment go to the person in
both designs.

### 6.5 Why this is narrower than Trust

| | Trust | this RFC |
|---|---|---|
| tools | every tool | read-only shell, three write tools, (phase 2) listed git |
| paths | any | inside one gateway-checked worktree |
| who | the whole child tree | dispatched `kirocrew-worker` / `kirocrew-mate-worker` only |
| restart | lost | the rule is config; `write_root` is persisted |
| off switch | pick Normal per chat | one config key |

## 7. Alternatives considered

- **Trust only.** Zero code. Rejected as the default path: every tool, no path
  fence, lost on restart (section 2.3). It stays available.
- **Conductor review (plan step 7).** Deferred, not rejected. Only if this RFC
  leaves too many cards; then behind these fixed rules, with full verified
  arguments, Approve/Reject once only, never Trust or deny-override, and an SEL
  row naming the conductor. Section 6.4 says why it is not the first step.
- **Inherit the conductor's trusted patterns.** Rejected for the reason the
  `_trusted_patterns` comment in `create_session` records.
- **Set Reads on every worker.** Covers R1 only and changes the worker's mode
  the person sees; R2 is most of the cards.
- **Fence on the slot's project directory instead of a named worktree.** The
  project directory is usually the main checkout, which workers must not edit.
- **Status quo.** What `agent.worker_fixed_grant = false` gives.

## 8. Migration plan and test plan

Phase 1 (R1, R2, `write_root`, the switch, the audit row): one code PR after
this RFC is accepted. Phase 2 (R3): its own PR, independently abandonable.

Exit criteria for phase 1 (tests in that PR, mostly in
`test/test_tool_approval*.py` and the session-create tests):

1. Normal, dispatched `kirocrew-worker`, `grep -rn x src/`: approved, SEL row
   `reason=worker_fixed_grant rule=read_only_shell`.
2. Same, `git status`: card (no git in R1).
3. Same, `rm -rf build`, `curl ...`, `pip install x`, `gh pr create`: card.
4. Read-only command whose program resolves through a PATH shim: card.
5. `fs_write` to `<write_root>/src/a.py`: approved, `rule=worktree_write`.
6. `fs_write` to `<write_root>/.git/hooks/pre-commit`, `<write_root>/.kiro/x`,
   the effective `core.hooksPath` inside the fence: card.
7. `fs_write` outside `write_root`, relative, with `..`, or through a link: card.
8. `delete_file` inside `write_root`: card.
9. No `write_root` on the slot: R2 cards, R1 still approves.
10. `worktree` argument naming the main checkout, a non-worktree directory,
    another live slot's `write_root`, or a link: create refused, SEL row.
11. A person's own tab on the `kirocrew-worker` template (no vouched creator):
    card for cases 1 and 5.
12. `kirocrew`, a conductor, or `kirocrew-research` caller: card.
13. Bot worker with Profile Normal: as cases 1 and 5. Profile unset (Trust):
    unchanged.
14. Governance deny rule matching a case-1 call: deny card. PreToolUse hook
    blocking it: blocked.
15. Low-fidelity event, redacted input, unverified params, claude backend: card.
16. `agent.worker_fixed_grant = false`: card for every case above.
17. `write_root` survives a gateway restart on the restored slot.
18. `fs_write` to `<write_root>/.KIRO/x` or `<write_root>/.Claude/x`, and on
    Windows a short-name alias of a fenced subpath: card.
19. A worker slot on Reads: case 1 approves on the existing Trust-reads rung
    (no `worker_fixed_grant` row); case 5 cards.

Exit criteria for phase 2: every allowlisted subcommand approves in the fence;
every refused subcommand and every flag in section 5.4 cards; each
precondition config value set cards; cwd outside the fence cards; `commit`
and `rebase` card while the effective hooks directory holds any hook they
run, and approve once it holds none.

The phase 1 PR updates the `_trusted_patterns` comment in `create_session`
to name the new grant, the team-lead and
goal-conductor skills so the conductor passes `worktree` at dispatch, the
session spec (`docs/system-specs/modules/session.md`), and flips this
document's status.

## 9. Backward compatibility

The `worktree` argument is optional; a caller that omits it gets today's write
behaviour. `write_root` is a new persisted slot field; a slot saved without it
reads as none. The config keys are new; a config without them reads the
default. Setting them false restores today's behaviour exactly. A `git revert`
of the code PR undoes it; a restored slot carrying `write_root` after the
revert ignores the unknown field.

## 10. Open questions

- Q1. Default on or off for R1 and R2? This RFC proposes on. Owner and
  security reviewer decide.
- Q2. Ship R3 at all, or keep every git call on the card? R3's config
  precondition is the part most likely to miss a case.
- Q3. Who names the worktree: the conductor at dispatch (proposed), or the
  gateway creates it itself at dispatch so no agent ever picks the path? The
  second drops the `worktree` argument and three of the four create checks in
  section 5.3, so the path stops being an agent-supplied trust input. It is the
  simpler design and is weighed first.
- Q4. Should R1 refuse reads of known credential paths (`~/.aws`, `~/.ssh`,
  token files) even though Reads mode does not?
- Q5. Should R2 send writes to files that run on a later approved command
  (`conftest.py`, `Makefile`, `package.json`, `.pre-commit-config.yaml`,
  `.envrc`) to the card, as it does for `.git` and agent config?
