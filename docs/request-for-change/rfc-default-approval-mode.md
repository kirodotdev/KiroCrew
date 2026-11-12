---
title: Start a new dashboard chat on the owner's chosen approval mode
status: draft
author: RohanK6
created: 2026-11-12
last-audited: 2026-11-12
audited-at: 0feaa8be90
doc-pr: null
implementation-prs: []
tracking-issues: [6812]
supersedes: []
superseded-by: []
---

# RFC: Start a new dashboard chat on the owner's chosen approval mode

- **Status:** Draft. Acceptance requested from a maintainer; the status flips to
  `accepted` when one records it on this line.
- **Verified at:** `0feaa8be90`. Every code reference below was read at that commit.
- **Tracks:** [#6812](https://github.com/kirodotdev/KiroCrew/issues/6812). The
  maintainer asked for a request-for-change on 2026-11-05.
- **Scope in one line:** the owner picks Normal or Reads as the mode a plain chat
  they open from the dashboard starts on, seeded the way a crewmate thread and a
  Slack code channel are seeded today. Trust is offered only if this RFC is accepted
  with it (section 6). Crewmate chats, channels, cron, subagents, apps and
  agent-created sessions keep how they start today.

## 1. Problem

Every plain dashboard chat (not a crewmate's) starts on Normal. A person who works
every chat at Reads or Trust re-picks it in the composer's approval-mode picker each
time, and again after a gateway restart (#12275). #6812 has 8 reactions and three
requests closed as duplicates of it; the one implementation attempt (#8984, closed)
stalled on whether Trust may be a stored default.

## 2. What exists today

- **Plain chats have no start mode.** A new slot starts with `_trust = False` and
  `_trust_reads = False` (`dashboard/state_parts/slot_turns.py`), and the browser
  store starts at `approvalMode: 'normal'` (`website/src/store/dashboardSlice.ts`).
  No key or Settings row sets it for the owner's plain chats.
- **Two stored start modes already exist, both seeded.**
  `dashboard/slot_approval_seed.py`'s `seed_slot_approval` gives a slot nobody has
  touched yet the mode its owner chose ahead of time, for a crewmate's thread (the
  Profile's `approval_mode`, `agents.<name>.approval_mode`, whose values are
  `MEMBER_APPROVAL_MODES` = `normal`, `trust_reads`, `trust`, and whose unset value
  means `trust`, `MEMBER_APPROVAL_MODE_UNSET_EFFECTIVE`) and for a Slack code
  channel's session (`slack.code_channel_defaults.approval_mode`). A seed writes what
  the picker writes and the same `mode_change:<mode>` SEL record, but it never
  touches the process-wide YOLO override, never replaces a posture the slot already
  holds, and clamps to the policy. So a stored Trust start mode is not new: crewmates
  have one, and it is their default.
- **Created sessions inherit.** `session_create` and `session_fork` copy the
  creator's posture once, at birth (`dashboard/approval_inheritance.py`); a
  crewmate's dispatched worker is seeded from its Profile.
- **The picker's setter is a different path.** `apply_approval_mode`
  (`dashboard/chat_api/slot_approval.py`) is the mutation half of
  `POST /api/chat/mode` and the set site the governance spec lists for every
  dashboard, cron `set_session_mode`, crewmate Profile and workflow write. A
  person's Reads or Trust press through it ends a declared YOLO grant, which is why
  a default must not use it (section 3.3).
- **The governance cap exists now.** The `approval_modes` scope (governance spec,
  "Which tool-approval modes a deployment may select") governs `trust_reads`,
  `trust` and `yolo`, with `normal` as the non-deniable floor.
  `approval_mode_policy.session_mode_permitted` answers for the two session modes
  from the installed ceiling's rule, at set sites and where a grant is honoured. The
  2026-09-25 triage on #6812 predates this: a ceiling can now forbid Reads or Trust.
- **Trust does more than Reads.** A slot on Trust carries `approval_policy ==
  "auto"`, which also auto-approves that session's subagent spawns
  (`subagent_manager/admission/gate.py`, `parent_trusted`). Reads auto-approves Kiro
  Crew's own read-only MCP tools only (`hooks._FIRST_PARTY_READ_ONLY_MCP_TOOLS`).
- **Yolo is not a session mode.** It is process-wide on its own
  `agent.yolo_duration`; it is out of scope here, as it was in #8984.

## 3. Proposal

### 3.1 The setting

One owner setting, **Settings → Chat → "New chats start on"**, with the values
`normal` (default) and `trust_reads`. Stored in `config.json` under one new key
(proposed `chat.default_approval_mode`; phase 2 settles the section it joins), and
written through the owner-gated dashboard config API like the other Settings rows.
Any other stored value, a value of the wrong type, or an unreadable config reads as
`normal`.

Why `config.json` is the right store: it and `config.local.json` are in
`_CREW_READONLY_LEAVES` (`sandbox_leaves.py`), so in-sandbox code can read them but
not write them, and the writers that leaf's comment names (the dashboard config API,
the channel handlers, the gateway's boot-time default and migrations, `kirocrew
config` from the operator's own terminal) run outside the sandbox. The agent
therefore cannot choose the start mode of the owner's next chat. The crewmate
Profile's `approval_mode` lives in the same file.

Why Trust is not in phases 2-3: crewmates show that a stored Trust start mode can
work, but a crewmate's is one named member the owner set up, and the owner's plain
chats are every chat they open. A plain chat started on Trust runs any tool and
auto-approves its own subagent spawns from its first message. Section 6 leaves that
to a maintainer.

### 3.2 Where it applies

Only to a plain chat the dashboard owner creates through `POST /api/chat/slots`: a
browser session that `is_owner_dashboard_request` accepts, with no app token, no
internal secret and no attested cron key, whose execution is not a crewmate's
(`crewmate_approval.crewmate_name_for` returns `""`). A crewmate's chat keeps its
Profile seed: a create-time Reads would otherwise block that seed, since a seed
never replaces a posture the slot already holds. Every other path keeps how it
starts today:

| Creation path | Start mode |
|---|---|
| Owner's New chat in the dashboard, plain | the setting |
| Owner's chat on a crewmate | unchanged (the crewmate's Profile seed) |
| A Shared Crew teammate's new chat | unchanged (Normal) |
| Agent `session_create`, `session_fork`, Ultra Mode members | unchanged (the creator's posture, `approval_inheritance`) |
| A crewmate's dispatched worker | unchanged (the Profile) |
| Subagents and spawns | unchanged (their own admission rules) |
| Cron sessions, `set_session_mode` | unchanged |
| Channel threads, a Slack code channel | unchanged (its own seed) |
| App-created sessions | unchanged (Normal) |

### 3.3 How it applies

After the slot is minted and before the route answers, the route calls
`seed_slot_approval(state, slot, mode, caller="chat_default_mode")` with the stored
value, the same seeder the crewmate and Slack code-channel defaults use. Not
`apply_approval_mode`: that is a person's press, and it ends a declared YOLO grant
(an operator's `--approval yolo` or a configured one) whenever it sets Reads or
Trust. Opening a chat must not end it. Through the seeder:

- The mode is clamped to the policy. A ceiling whose `approval_modes` rule denies
  Reads leaves the chat on Normal; the create still succeeds, and the reply carries
  `default_mode_refused: "policy"` (the clamped mode differs from the stored one) so
  the composer can say why, once.
- The honour-site masking drops the grant if a later ceiling forbids it, as for any
  slot.
- YOLO, an existing posture and pending approvals are left alone.
- The SEL records `mode_change:trust_reads` under the caller `chat_default_mode`, so
  an audit can tell a default from a pick.

A failed seed never fails the create: the chat exists, on Normal.

### 3.4 What the person sees

The Settings row shows the two choices with one line under it: "Applies to chats
you start from the dashboard. Crewmates, channels, schedules and agents keep their
own settings." When a ceiling denies Reads the row is shown disabled with the
policy reason, the same treatment the composer picker gives a denied mode.

## 4. Phases

| # | Ships | Size |
|---|---|---|
| 1 | This RFC (docs only) | small |
| 2 | `chat.default_approval_mode` (Normal / Reads), seeded at the owner's plain create through `seed_slot_approval`, the refusal field, the SEL caller, specs (`config.md`, the seeder's spec) | medium |
| 3 | The Settings row, en/de screenshots, the composer's one-line notice | small |
| 4 | Trust as a third value of the same key, only if section 6 says yes | small |

Each phase is one PR that ships alone.

## 5. Relation to other work

- #12275 (a chat's mode is lost on restart) is distinct: this sets the start mode
  of new chats and does not restore old ones. A seeded mode is in-memory like every
  slot's.
- #11058 (`agent.approval_mode: auto` has no effect on new dashboard sessions) is
  adjacent; this RFC does not change that key.
- #9105 (where trust-bearing settings live) is adjacent: `config.json` is already
  sealed, and the crewmate Profile's start mode already lives there.

## 6. Not decided here

1. **Whether the owner's plain chats may start on Trust.** Not in phases 2-3.
   Crewmates already may, so the store is not the question; the reach is. If it is
   wanted, phase 4 adds it to the same key, and the `approval_modes` ceiling already
   lets a managed fleet forbid it.
2. **A per-person default under Shared Crew.** Out of scope; teammates start on
   Normal.

## 7. Questions for the maintainer

1. Is a Normal / Reads default in `config.json`, seeded like the crewmate and Slack
   code-channel defaults, acceptable as phases 2-3?
2. Should plain chats be offered Trust at all (phase 4)?
3. Should a Reads default refused by the ceiling be silent, or should the composer
   say so once (proposed: once)?
