---
title: A conversation's trust follows its founder, not its continuer
status: in-progress
author: Billy Gerhard
created: 2026-09-30
last-audited: 2026-10-03
audited-at: 8ef5ea40e5
doc-pr: 15457
implementation-prs: [13109]
tracking-issues: [13244]
supersedes: []
superseded-by: []
---

# RFC: A conversation's trust follows its founder, not its continuer

- Status: `in-progress` -- the change this document records is live in an open
  PR, [#13109](https://github.com/kirodotdev/KiroCrew/pull/13109), the same
  reading the index gives `rfc-composable-layout-mechanism` and
  `rfc-solo-spawn-gate`. The design was reviewed and accepted by maintainer
  bolichen97 on 2026-10-01, in
  [this document's PR](https://github.com/kirodotdev/KiroCrew/pull/15457#issuecomment-5927219425):
  "reviewed for a security regression and accepted as designed: it only
  narrows when chat-level trust auto-approves". The status stays
  `in-progress` rather than `accepted` because the index reads `in-progress`
  for a decision whose implementation is in flight rather than on main.
- Changed default: a subagent conversation continued from a chat other than the
  one that started it no longer runs under the continuing chat's trust. No
  chat's trust answers for such a continuation, so every request it raises that
  only a chat's trust would have approved is asked of a person, and the
  conversation stays that way. The grants that are not chat-level trust (YOLO,
  the parentless `agent.approval_mode` fallback, the
  `auto_approve_subagent_*` hooks) are unchanged and still approve it.
- The other half of the same change -- a trusted chat's trust reaching every
  subagent in its spawn tree -- is a fix, not a decision, and is not what this
  document is for.

## The decision

A subagent conversation (the `subagent:<id>` key shared by an original run and
each `spawn_continue` of it) has one **founder**: the chat, cron or channel
whose trust admitted the run that started it. A continuation from that same
root inherits the founder's trust as it always did. A continuation from a
**different** root marks the conversation **contested**, durably, and from then
on no chat's trust answers for it: the spawn prompt, and every tool prompt the
continuation raises, falls through the chat-trust rung and -- unless YOLO, the
parentless fallback or an `auto_approve_subagent_*` hook grants it, exactly as
they would any other run -- is asked of a person on the global approvals feed,
with copy that says why. A conversation whose founder cannot be read -- a founder
record that predates this change and names a nested parent, a folder that is
gone, a re-entry path that arrives without its stamp -- is contested too,
because "unknown founder" and "another chat's founder" must not be told apart
by which chat happens to be asking.

The remedy a person has today is to start the task again from one chat. An
in-product way to clear or re-found a contested conversation is
[#13244](https://github.com/kirodotdev/KiroCrew/issues/13244), and this
document records that shipping the contest ahead of that affordance is the
ordering bolichen97 accepted on 2026-10-01.

## What it replaces, and why that shape was wrong

On the base branch a continuation's run reads its trust off the **continuing**
chat: `run.py` asks `get_approval_policy(info.parent_session_key)`, where the
parent is whichever chat called `spawn_continue`. The founder is never
consulted. So a conversation whose turns were authored under chat A's key could
be picked up by chat B, and if B was trusted (a Trust press, YOLO, or a
`spawn_run` from a trusted tree), every request the resumed run made -- a
`shell(...)`, a file write, a further spawn -- was auto-approved on B's trust,
for work B never read.

That was a widening nobody chose. Trust in Kiro Crew is granted **per chat**: a
person presses Trust on a conversation they are watching, and the grant means
"the requests this conversation makes are mine". A continuation from another
chat carries into B a body of prior turns, tool results and pending intentions
that B's owner did not watch and may not be able to see, and the run's next
request is shaped by all of it. Reading B's trust for that request stretches
"the requests this conversation makes" to cover requests another conversation
made. Two humans on one gateway (a shared workstation, a household, a team
instance) is the ordinary case where this bites: A's untrusted exploration
becomes auto-approved the moment B continues it.

The base behaviour also had no notion of the founder at all, which is why the
same defect appeared at depth: a grandchild spawn under a trusted chat prompted
as if it had no chat (the fix half of #13109), and a continuation from an
untrusted chat of a trusted chat's conversation was refused nothing. Both fall
out of the same missing fact -- which root founded this conversation -- and the
implementation records it once, at admission, rather than re-deriving it from
whichever records happen to be live.

## Boundary: what "contested" means and does not mean

- **Same-root continuation is unchanged.** Chat A continues its own
  conversation and inherits its own trust; a cron's run continues itself under
  the policy registered on its own key. Nothing a single-chat user does today
  changes.
- **A contest is a trust stamp only.** The continuation's card still belongs to
  the continuing chat's tab, its frames still route there, and its result is
  still delivered where the continuer asked. Only the answer to "may this
  request proceed without a person" changes.
- **A contest is durable and one-way.** It is written onto the founder's run
  record before the continuation's first turn runs, so a restart cannot restore
  the founding trust, and a later same-root continuation reads the contest back
  rather than the founding root. The stamp does not record *which* case minted
  it (two chats, an unreadable founder, a stampless re-entry, a chat picking up
  a cron's run), so the one sentence shown to a person is the sentence that is
  true for all of them: the chat that started the task could not be confirmed
  as the one continuing it.
- **Unknown founder is contested, not trusted.** A conversation whose founder
  record predates the stamp and names a nested parent has no readable founding
  root; its first post-upgrade continuation is contested and stays so. A
  depth-one pre-upgrade founder still names its chat parent, which is the root
  admission would have stamped, so a same-chat continuation of such a run is
  spared. This is the one migration cost, and the remedy is the same: start
  the task again from one chat.
- **Approval is not a clearing.** Approving one contested prompt approves that
  request. The next one asks again, and the copy says so, so nobody approves
  expecting the prompts to stop.

## What is deliberately NOT in this change

- **No clearing or re-founding affordance.** Rewriting a founder record's
  `conversation_root` under one chat is a human action with its own UI, its own
  audit line and its own question about who may do it; it is
  [#13244](https://github.com/kirodotdev/KiroCrew/issues/13244)'s to design.
  Shipping the contest first means a contested conversation is prompt-forever
  until then. bolichen97 accepted that on 2026-10-01: the alternative was to
  keep auto-approving cross-chat continuations while the clearing tool was
  designed.
- **No routing of contested prompts into a messaging channel.** A contested
  prompt reaches the global approvals feed alone -- not the owner's Slack DM,
  whose card carries a Trust control that would record the contest marker as a
  trusted session; a person driving from Discord or Telegram sees the refusal
  as the parent's completion event rather than an inline prompt. Also #13244's.
- **No per-request "which chat is asking" attribution on nested prompts.** A
  badge in the Subagents panel naming the asking run is tracked there too.
- **No change to any other grant.** Explicit `approval_mode`, YOLO, the global
  `agent.approval_mode` fallback for parentless runs, and the
  `auto_approve_subagent_*` hooks keep their order. The change is only to
  *whose* chat-level trust a continuation reads.

## Alternatives considered

- **Ship the depth-trust fix alone and leave continuation trust as it was.**
  This is the subtraction the First Principles lane proposed. It would leave the
  base defect in place: a trusted chat's continuation of another chat's
  conversation would still auto-approve requests shaped by turns its owner
  never saw. The depth fix makes trust reach *further*; shipping it without the
  founder rule makes the cross-chat widening reach further too.
- **Refuse cross-chat continuation outright.** Simpler, but it removes a real
  workflow (a colleague picking up a run to read its result or steer it) to
  close a trust gap that prompting already closes. Contest keeps the
  continuation and removes only the silent auto-approval.
- **Let the continuing chat re-found the conversation on continuation.**
  This is the base behaviour restated, and it is the widening described above.
- **Make the contest in-memory only.** A restart would then restore the
  founding root and its trust for a conversation a person had already been told
  was contested. The durable write is what makes "asks again" true.

## Consequences

- A trusted chat's whole spawn tree runs to completion without a prompt nobody
  can see; a trusted chat no longer lends its trust to a conversation another
  chat authored.
- Multi-human gateways lose a silent escalation path.
- A cross-chat continuation now prompts on every request until #13244 lands,
  and pre-upgrade nested conversations are contested on first continuation.
  The prompts say what happened and what to do; they offer no button because
  there is not yet one to offer.
- The implementation is [#13109](https://github.com/kirodotdev/KiroCrew/pull/13109),
  whose Goal line records this decision in the words above. The current
  behaviour, once merged, is specified in
  [`../system-specs/modules/subagent.md`](../system-specs/modules/subagent.md)
  under the conversation root and admission stamps.
