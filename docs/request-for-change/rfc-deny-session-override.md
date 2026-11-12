---
title: A person can override a command deny rule for one call or one session
status: accepted
author: Bolin Chen
created: 2026-11-01
last-audited: 2026-11-01
audited-at: 69a0d28622
doc-pr: null
implementation-prs: []
tracking-issues: [26984]
supersedes: []
superseded-by: []
---

# RFC: A person can override a command deny rule for one call or one session

## The decision

When the command deny list refuses a shell command in a dashboard session that a
person is driving, the refusal becomes an approval card. The card leads with one
plain line, in the person's language, on what the rule protects: the server sends
the rule's family (`dashboard.deny_override.deny_key_family`: git publish, cloud,
wipe, permissions and so on) and the dashboard translates it,
with a generic line for a rule no family covers. The rule's id and catalog text sit
behind a "Technical details" disclosure. The card offers three answers: **Allow once**, **Allow for this session**, or **Reject**.
Every rule of the deny list can be overridden this way, floors included, except the
rules that guard the agent's own controls (see *Rules that are never overridable*).
A rule that an enterprise governance policy pins, or that the companion
`SecurityOverlay` adds, can never be overridden either. The card is offered only on a backend whose sub-agent calls
its transport is proven to tell apart. Nothing outside the deny list changes.

Owner rulings, by the security owner Bolin Chen (@bolichen97), are on the issue:
[the first ruling](https://github.com/kirodotdev/KiroCrew/issues/26984#issuecomment-6563928608)
and [the second](https://github.com/kirodotdev/KiroCrew/issues/26984#issuecomment-6564945615),
which replaces the first one's line about floors: floors are overridable too. Three
later rulings by the same owner, on review of this PR, are final:

- a backend that cannot tell a sub-agent's tool call from the parent's (codex, and
  any backend without a verified child-call mark) gets no card and no session
  waiver;
- a rule from the companion `SecurityOverlay` is not overridable, the same as a
  governance pin;
- "Allow for this session" keeps the key only after the call it was granted for
  passed the second judgement with that key waived.

A ruling on a maintainer's review of this PR narrows the second ruling's line about
floors: no self-protection floor, no rule of the self-protection or credentials
families, and not the git-publish floor's unverifiable-target refusal can be
overridden, once or for the session.

## Why

A deny rule refuses a command by its shape, not by its intent. A person who has read
the command and knows it is safe (a `git push` to a branch a floor reads as
protected, an `rm -rf` on a throwaway directory, a `chmod 777` in a build tree) has
only two ways forward today: switch the rule off in Settings for every session, or
run the command in a terminal. The first loosens the policy far wider than the one
call needed, and the second takes the work out of the session. An override covers the
one call, or the one session, and nothing more.

## What is overridable

| Gate tier (`GATE_TIERS` in `hook_runtime/gate_tiers.py`) | Overridable |
|---|---|
| `deny-rules` (`_tier_deny_rules`, which calls `security.is_denied`) | Yes: catalog regex rules, operator rules, `auto_deny_tools` globs, the git-publish floor's per-rule refusals, the rm floor, the chmod/chown argv rules, and the sent-title re-check |
| a `deny-rules` self-guard rule: a self-protection floor, any self-protection or credentials rule, or the unverifiable-target push (below) | No |
| every other tier (unverifiable shell, targets, sensitive paths and exfiltration, parameter paths, write protection, MCP auto-deny, search targets, governance, spec allowlists) | No, unchanged |
| a `deny-rules` rule whose id a governance ceiling or profile pins | No |
| a `deny-rules` pattern the companion `SecurityOverlay` adds | No |

### Rules that are never overridable

`deny_waiver.deny_key_is_self_guard` names them, and they keep today's flat refusal:

- every ungated self-protection floor, which has no catalog row and no opt-out:
  `restart`, `update`, `gateway restart`, destructive `cloud` operations and
  `skills authority-retire`;
- every argv self-protection floor the security spec lists: credential minting,
  process termination, the dev-mode attestation flag and the ssh-to-self escape;
- every catalog rule of the `self-protection` or `credential-exfil` category,
  including the credential-mint rule and its interpreter-argv twin, matched by
  category or id prefix, so an operator rule typed identical to one is held too;
- the git-publish floor's unverifiable-target refusal (reported under the
  brace-expansion row's key), which also has no opt-out. That branch reads no push
  target at all, so a waiver for it would also let through a protected-branch or
  force push written with any `$(...)` or `${...}` in it, without another card.

Why: these are the controls that stop the agent disabling its own controls. The
credential-mint floor is the sharpest case. The dashboard's local mint endpoint
issues an owner credential, and on some hosts and sandbox modes the argv floor is
the main barrier between the agent's shell and that endpoint. With the floor
waived, even once, the agent could mint an owner credential and use it to answer
its own override cards and every other approval card. So the statement below, that
an agent cannot reach an override by itself, holds only while these rules stay out
of reach. Three places enforce it: the card is not offered for such a key, the
`_DenyWaiver` inside `is_denied` drops it whatever the caller passes, and the
self-protection floor sites, the unverifiable-target branch and the
interpreter-joined match never consult the waiver at all.

## The override key

Every refusal from `is_denied` starts with `DENY_REASON_PREFIX` followed by a key:
the catalog or operator pattern, the git-publish rule id, or the floor rule id that
its return site passes to `_deny_reason`. That key is what an override waives. The
first line keeps its bytes, because `RecoveryCard.tsx` parses it.

`is_denied(..., waived=frozenset[str])` checks the key at every return site. A site
whose key is waived writes a `deny_exception` audit event with mechanism
`session_override` and, if the write succeeded, steps aside: evaluation continues, so
a second rule the same command breaks still refuses under its own key. If the audit
write fails, the site refuses exactly as before. Waiving one key never disables a
different rule.

An event records that one rule stepped aside in one judgement, not that the call
ran: a call is judged up to three times (the gate, the second judgement after the
click, and the transport floor on approval), each writes its own event, and a
second rule can still refuse the call after an event for the first. Writing the
event at the site, before the step-aside, is what keeps a failed write fail-closed.
The call's outcome is its own `tool_invocation` record (`reason: deny_override`
when an override let it run).

The key travels through `PolicyAuthority.is_denied`, `is_denied_synthesized_target`,
`GateFacts.deny_waivers`, `HookManager.judge` / `on_tool_call(deny_waivers=...)` and
the transport floor (`permission_floor.refusal_for`, reached through each transport's
`approve_tool(deny_waivers=...)`), which judges the call again when it is approved.

## Who can override, and where

The card says the command has not run and shows the refused command itself
(redacted as the label is), then what the rule blocks, in the catalog row's own
words (`deny_waiver.deny_key_description`), then the rule's key, so a person can
judge the command without knowing rule ids. Its allow control is a menu trigger
("Choose how to allow") that runs nothing; each row names its reach: "Allow once"
runs this command one time, and "Allow for this session: stop this rule from blocking
..." lets any command this rule blocks through "until you close this chat (Session
options → Close session)" or the gateway restarts; the parenthesis is the path to
the control that ends it, in the UI's own labels (the chat title menu's "Session
options" name and its "Close session" item), so the end is named both as what the
person sees and as where they go to end it. The card is offered
only when all of these hold:

- the refusal came from the `deny-rules` tier, its key is not pinned, it is not
  a companion overlay pattern, and it is not a self-guard key;
- the request came from a backend whose sub-agent calls its transport is proven to
  tell apart (see *Backends* below);
- the slot is one a person drives from the dashboard: not app-owned, not unattended,
  not created by a cron job or by the gateway, not linked to a cron, workflow, task
  or channel session, not mirrored to Slack; and the turn is one the owner sent
  from the dashboard (not another dashboard user's turn, a cron delivery, an app
  message, a loop wake or a sub-agent's completion), did not arrive from a messaging channel and has no
  messaging-channel steer admitted into it;
- the request is the agent's own shell command, not a backend subagent's (a
  foreign-session child, or a native sub-agent sharing the session id: the
  transports mark its calls `native_child_call`, and the dashboard marks any call a
  sub-agent activity event named), and the transport delivered its bytes unredacted;
- the transport's `approve_tool` can carry the keys to its floor.

Otherwise the refusal is today's, byte for byte.

### Backends

The sub-agent check above is only as good as the transport's mark. On a backend
whose child calls arrive under the parent's session id with no mark, a sub-agent's
command looks exactly like the parent's, so neither the card nor a session waiver
could be kept from it. The design therefore uses an explicit allowlist, kept per
transport because each reads different marks, and fails closed for everything not
on it (`security.deny_waiver`):

| Transport | Backends | The mark it reads |
|---|---|---|
| direct `AcpClient` (`_is_child_permission_frame`) | Kiro CLI, Claude | a frame under a foreign `sessionId`; a Claude tool call stamped `_meta.claudeCode.parentToolUseId` (recorded in `test/fixtures/claude_native_subagents/`) |
| shared runtime `AcpSessionHandle` | Kiro CLI, KAS | a frame under a foreign `sessionId`; a native KAS sub-agent's call, told apart by the id its `agentSubtaskId` frame recorded |

Each transport stamps `AcpEvent.child_calls_marked` when its backend is on its list.
The flag defaults to false, so codex, every other backend, a backend added later,
an unreadable backend value and any other event source get no card and no session
waiver: today's refusal, byte for byte. Adding a backend means proving its mark on
that transport first.

Only the dashboard owner's own click answers the card. The slot approve route
(`_deny_override_answer_refusal`) refuses the two override decisions from an app
token, a Crew Mode conductor, an invited person, a loopback caller, or for any card
that is not an override card. Each override decision must also name the card row
it was clicked on (`origin: "native"` with the row's `request_mid`), which the route
checks against the slot's live instance: request ids recur when a session's
connection is replaced, and a stale card must not approve a newer command under the
same id. Every other approval route (approve by id, an app's session grant, the
bulk resolver) goes through `ApprovalCoordinator.resolve_on_slot`, which settles
nothing when asked to approve an override card, so the card stays open. A trust or YOLO grant never answers an override card:
the sweep that approves pending cards skips it, and any answer other than the two
override decisions is read as a rejection (`override_decision`). The card shows in
trust and YOLO mode too, because the refusal returns from the decision before any
auto-approve rung runs. An unanswered card times out and is refused. A refused or
timed-out card is reported to the agent as the rule's refusal, not as "User denied".

An approval card can be answered only from the dashboard, so the card is not mirrored
to Slack, and the Trust choices are withheld from it.

## What each answer does

- **Allow once.** The call is judged again with the key waived. If the gate then
  allows it, it is approved with the same keys passed to the transport floor. If it
  denies on another key, the call is refused with that rule's reason. One card per
  call, and it waives one rule: a command that breaks two overridable rules is
  refused and cannot be waived in this version (see *Alternatives considered*).
- **Allow for this session.** The call is judged as for Allow once. Only when that
  second judgement allows it is the key added to the slot's `_deny_waivers`; if
  another rule still refuses, nothing is stored and the refusal stands, so a session
  override never outlives a call it did not let run. Later calls in the same slot pass the slot's keys
  to the gate and to the floor only in a turn the owner sent from the dashboard,
  and each use writes its own audit event. The set lives
  in memory only: it ends with the slot or a gateway restart, is never written to
  config, and never touches `denied_commands.json`.
- **Reject.** The call is refused as the rule refused it.

The transcript records each override that let a call run, with the rule key and
whether it was for one call or the session.

## Trust boundary and threat

The trust boundary is the person at the dashboard. An agent cannot reach an override
by itself: the card's two decisions are accepted only from the owner's dashboard
session, the waiver set is written only from that answer, and no rule that guards
that boundary (the credential-mint floor and the other self-guard rules above) can
be waived, so no override can hand the agent the means to answer a card.

The threat this design accepts is a prompt-injected agent that, after a person granted
a session override for a rule, uses that rule again for a different command in a later
turn the owner sent in the same session. The person chose that scope. The limits are:
the override covers one rule key, not the gate; every other tier and every other rule
still apply to each call; each use writes an audit event or refuses; the scope ends
with the slot or a restart; subagents never inherit it; a turn the owner did not
send from the dashboard (another dashboard user's turn, a cron delivery, an app message, a loop wake, a sub-agent's
completion, or a turn a messaging-channel message was steered into) neither reuses
the waiver nor is offered a card; and on a backend that cannot mark a sub-agent's call no override exists at all. Allow once has no such exposure. A session key is
rule-wide, not command-wide: a waiver granted for one recursive delete, or one push
to a protected branch, covers every command that rule refuses for the rest of the
session. The card's session row names the rule's family, and the person decides
with that in view.

## Why governance pins and the companion overlay stay hard

A governance pin is an administrator's statement that the rule must hold for this
deployment regardless of what anyone at the dashboard prefers. An override is a
person at the dashboard preferring otherwise, so it must not reach a pinned rule.
Both the card offer and the gate read the same pin set: the active ceiling's pins
and every loaded profile's (`pinned_builtin_command_ids_for_snapshot`). The gate
drops every pinned key from the waivers before `is_denied` sees them, on every
call, so a pin that lands mid-session, in the ceiling or in a profile, wins over a
session override granted before it. The union over-locks a profile the session is
not bound to, which only ever keeps a refusal. A profile's commands-scope denials
are also enforced by the governance tier, which is not overridable.

The pin reads are the existing accessors and keep their fail-soft contract: a
governance resolution error reads as no pins, exactly as it does for the force
re-add of a pinned rule today, so an override cannot reach further than an
operator opt-out already can in that state. An accessor that raises outright
offers no override and applies none.

The companion `SecurityOverlay` is ADD-only enterprise policy: no operator setting
can remove one of its patterns, so no override may step one aside either. Its
patterns (`PolicyAuthority.effective_patterns`) are held as hard as a pin in three
places: the card offer offers none of them (and offers nothing when the overlay
cannot be read), the deny-rules tier drops them from the waivers with the pinned
keys, and `PolicyAuthority.is_denied` / `is_denied_synthesized_target` remove them
from `waived` once more before `security.is_denied` sees it. A key whose text is
both an overlay pattern and an operator or catalog pattern is treated as the
overlay's.

## Alternatives considered

- **Re-parse the reason text in the gate.** Rejected: the key is already the first
  line's bytes, and passing it in as data keeps one producer.
- **Waive the whole tier for the call.** Rejected: it would let a second rule in the
  same command through without anyone seeing it.
- **Ask again for a second rule within the same card flow.** Not done in this
  version: a command that breaks two overridable rules is refused and cannot be
  waived. A retry does not help, because the gate stops at the first rule again and
  nothing was stored, so the person only ever sees that rule's card. A per-call
  waiver that lets the second rule's card appear is tracked in #28547.
- **A Settings view to list and clear active session overrides.** Left out: closing
  the session or restarting clears them, and the transcript names each one.

## Consequences

- Every override that lets a call run is an audit event with mechanism
  `session_override`, in addition to the approved tool invocation record with
  `reason: deny_override`.
- Behaviour for every refusal that is not offered a card is unchanged.
- Tests: `test/test_deny_session_override.py`.
