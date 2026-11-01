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
person is driving, the refusal becomes an approval card. The card names the rule
and offers three answers: **Allow once**, **Allow for this session**, or **Reject**.
Every rule of the deny list can be overridden this way, floors included. A rule that
an enterprise governance policy pins can never be overridden, and nothing outside
the deny list changes.

Owner rulings, by the security owner Bolin Chen (@bolichen97), are on the issue:
[the first ruling](https://github.com/kirodotdev/KiroCrew/issues/26984#issuecomment-6563928608)
and [the second](https://github.com/kirodotdev/KiroCrew/issues/26984#issuecomment-6564945615),
which replaces the first one's line about floors: floors are overridable too.

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
| `deny-rules` (`_tier_deny_rules`, which calls `security.is_denied`) | Yes: catalog regex rules, operator rules, `auto_deny_tools` globs and the companion overlay, the git-publish floor (including its unverifiable-target branch), the self-protection floors, the rm floor, the chmod/chown argv rules, the interpreter-joined match, and the sent-title re-check |
| every other tier (unverifiable shell, targets, sensitive paths and exfiltration, parameter paths, write protection, MCP auto-deny, search targets, governance, spec allowlists) | No, unchanged |
| a `deny-rules` rule whose id a governance ceiling or profile pins | No |

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

The git-publish floor's unverifiable-target branch has its own key (the
brace-expansion row's pattern, which the refusal names). Waiving it lets a push
whose destination cannot be determined go through, and every resolvable push target
in the same command is still judged by its own rule.

The key travels through `PolicyAuthority.is_denied`, `is_denied_synthesized_target`,
`GateFacts.deny_waivers`, `HookManager.judge` / `on_tool_call(deny_waivers=...)` and
the transport floor (`permission_floor.refusal_for`, reached through each transport's
`approve_tool(deny_waivers=...)`), which judges the call again when it is approved.

## Who can override, and where

The card leads with what the rule blocks, in the catalog row's own words
(`deny_waiver.deny_key_description`), then the rule's key, so a person can judge
the command without knowing rule ids. The card is offered only when all of these hold:

- the refusal came from the `deny-rules` tier and its key is not pinned;
- the slot is one a person drives from the dashboard: not app-owned, not unattended,
  not created by a cron job or by the gateway, not linked to a cron, workflow, task
  or channel session, not mirrored to Slack, and the turn did not arrive from a
  messaging channel;
- the request is the agent's own shell command, not a backend subagent's, and the
  transport delivered its bytes unredacted;
- the transport's `approve_tool` can carry the keys to its floor.

Otherwise the refusal is today's, byte for byte.

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
  call: a second rule is a second, separate decision the agent can ask for again.
- **Allow for this session.** The key is added to the slot's `_deny_waivers`, then the
  call is judged as for Allow once. Later calls in the same slot pass the slot's keys
  to the gate and to the floor, and each use writes its own audit event. The set lives
  in memory only: it ends with the slot or a gateway restart, is never written to
  config, and never touches `denied_commands.json`.
- **Reject.** The call is refused as the rule refused it.

The transcript records each override that let a call run, with the rule key and
whether it was for one call or the session.

## Trust boundary and threat

The trust boundary is the person at the dashboard. An agent cannot reach an override
by itself: the card's two decisions are accepted only from the owner's dashboard
session, and the waiver set is written only from that answer.

The threat this design accepts is a prompt-injected agent that, after a person granted
a session override for a rule, uses that rule again for a different command within the
same session. The person chose that scope. The limits are: the override covers one
rule key, not the gate; every other tier and every other rule still apply to each
call; each use writes an audit event or refuses; the scope ends with the slot or a
restart; and subagents never inherit it. Allow once has no such exposure. The widest
session key is the unverifiable-target push: for the rest of the session it lets
through any push whose destination an expansion hides, including one that resolves
to a protected branch, because that destination is exactly what the floor cannot
see. The card names the rule key, and the person decides with that in view.

## Why governance pins stay hard

A governance pin is an administrator's statement that the rule must hold for this
deployment regardless of what anyone at the dashboard prefers. An override is a
person at the dashboard preferring otherwise, so it must not reach a pinned rule.
Both the card offer and the gate read the same pin set: the active ceiling's pins
and every loaded profile's (`pinned_builtin_command_ids_for_snapshot`). The gate
drops every pinned key from the waivers before `is_denied` sees them, on every
call, so a pin that lands mid-session, in the ceiling or in a profile, wins over a
session override granted before it. The union over-locks a profile the session is
not bound to, which only ever keeps a refusal. A profile's commands-scope denials
are also enforced by the governance tier, which is not overridable. A pin on any git-publish rule also pins
the unverifiable-target key, because the destination that key lets through may be
the branch the pinned rule protects (`deny_waiver.deny_key_is_pinned`).

The pin reads are the existing accessors and keep their fail-soft contract: a
governance resolution error reads as no pins, exactly as it does for the force
re-add of a pinned rule today, so an override cannot reach further than an
operator opt-out already can in that state. An accessor that raises outright
offers no override and applies none.

## Alternatives considered

- **Re-parse the reason text in the gate.** Rejected: the key is already the first
  line's bytes, and passing it in as data keeps one producer.
- **Waive the whole tier for the call.** Rejected: it would let a second rule in the
  same command through without anyone seeing it.
- **Ask again for a second rule within the same card flow.** Not done: refusing with
  the second rule's reason is the smaller mechanism, and the agent can retry so the
  person sees a fresh card naming that rule.
- **A Settings view to list and clear active session overrides.** Left out: closing
  the session or restarting clears them, and the transcript names each one.

## Consequences

- Every override that lets a call run is an audit event with mechanism
  `session_override`, in addition to the approved tool invocation record with
  `reason: deny_override`.
- Behaviour for every refusal that is not offered a card is unchanged.
- Tests: `test/test_deny_session_override.py`.
