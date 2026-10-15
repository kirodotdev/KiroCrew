---
title: A crewmate's dispatched workers run on its model and effort, with a profile override
status: accepted
author: iamwhatever
created: 2026-10-12
last-audited: 2026-10-12
audited-at: e5a0b3d3b2
doc-pr:
implementation-prs: [19368]
tracking-issues: [19344]
supersedes: []
superseded-by: []
---

# RFC: A crewmate's dispatched workers run on its model and effort, with a profile override

- Status: accepted. This is the product owner's decision, given 2026-10-12 in
  the "Crew page polish" goal (item Y): a worker session a crewmate opens
  through `session_create` inherits that crewmate's model and reasoning effort
  when the call names neither; the crewmate's profile gets a **Worker model** /
  **Worker reasoning effort** pair that, when set, its workers use instead; an
  explicit `model` or `reasoning_effort` on the call always wins. It amends the
  "Pin a cheaper model" rule of
  [model-selection.md](../system-specs/common/model-selection.md) and extends
  [rfc-crewmate-profile-settings.md](rfc-crewmate-profile-settings.md) (the
  crewmate's own pickers) in this document, so that accepted document is not
  edited. Claims about today's code were checked at `e5a0b3d3b2` (main).
- Author: iamwhatever
- Implementation: [#19368](https://github.com/kirodotdev/KiroCrew/pull/19368);
  tracks [#19344](https://github.com/kirodotdev/KiroCrew/issues/19344).

## 1. Today

At `e5a0b3d3b2`, a crewmate's own Crewmates-page thread runs on the model and
effort its record pins (`KiroCrewAgentConfig.model` / `.reasoning_effort`,
read by `_session_model` in `session_parts/policy.py` and `resolve_session_effort` in
`config/loader.py`). `create_session` in `dashboard/session_control.py` takes
an optional `model` and no effort; a call that names no model leaves the child
on the agent's or the global default (`_default_session_model` in
`chat_runner.py`). So a conductor on one model dispatches workers that run on
another, and nothing on the profile shows or steers it. The only lever is to
pass `model` on every `session_create` call from the agent's prompt.

`model-selection.md` records a related rule for the worker ROLES
(`agent.role_models.background` / `.subagent`): they default to `"auto"` and
"deliberately do NOT inherit `agent.model`, so a user's chat model does not
silently become the price of every background task", and a cheaper model is
pinned "only through `agent.role_models.<role>`".

## 2. Decision

1. **Default inheritance.** When a crewmate calls `session_create` and omits
   `model` and/or `reasoning_effort`, the child takes the crewmate's pair: the
   profile's `worker_model` / `worker_reasoning_effort` when set, else the
   crewmate's own `model` / `reasoning_effort`. The crewmate is the caller's
   member record (its protected execution record; a slot with no record falls
   back to its agent name). A caller that is not a crewmate, a dispatched
   template worker running on its crewmate's store, and a child that is itself
   a crewmate (an explicit `agent` selecting a member, which runs its own
   profile) inherit nothing; so does a crewmate with nothing pinned. Behaviour
   for all of those is exactly today's.
2. **Explicit wins.** A `model` or `reasoning_effort` named on the call is
   taken as before; `reasoning_effort` is new on `session_create` and accepts
   the five canonical levels `session_set_model` takes.
3. **A bad inherited pin is dropped, not fatal.** An explicit model the picker
   guard refuses still fails the create. An inherited model the guard refuses
   (a stale, delisted or display-only id, or an id the dispatching crewmate's
   harness serves and the child's does not) is dropped: the child starts on the
   default and the drop is recorded in the create's SEL record, so a stale
   profile never stops a conductor from opening workers.
4. **Worker effort follows the worker model.** The crewmate's own effort
   carries over only while the workers run the crewmate's own model
   (`worker_model` unset). With `worker_model` set, the only effort a worker
   takes is `worker_reasoning_effort`; nothing hidden reaches the provider
   under a model the card shows no effort row for.
5. **The profile pair.** The crewmate Profile card's model/effort section gets
   **Worker model** ("Same as <name>" = unset, "Default model (Auto)" = the
   backend default, or a listed model) and **Worker reasoning effort**
   ("Same as <name>" when the workers run the crewmate's model, else "Model
   default", or a level). Both write the record only (`PUT /api/agents/{name}`,
   read on `GET /api/members`); nothing touches a live thread. `worker_model`
   is read raw: `"auto"` is a real pin there, where the crewmate's own `model`
   folds it to inherit.

## 3. What this amends

- model-selection.md, "Pin a cheaper model only through
  `agent.role_models.<role>`": read as "only through `agent.role_models.<role>`
  for the worker roles, and through the crewmate's `worker_model` /
  `worker_reasoning_effort` for the sessions a crewmate dispatches with
  `session_create`". The roles still do not inherit `agent.model`; a
  crewmate's dispatched session inherits the CREWMATE's pin, which the person
  chose on that crewmate's profile, not the user's chat model. `worker_model:
  "auto"` is the per-crewmate spelling of that rule's cost cap, not a second
  role pin: `agent.role_models` is process-wide, this is one crewmate's.
- rfc-crewmate-profile-settings §3: the model/effort section carries the two
  worker pickers below the crewmate's own.

## 4. Non-goals

- `session_fork`, which copies its source's model already.
- How a crewmate's own model and effort resolve or are picked.
- A per-session worker override UI; the pair lives on the profile only.
- Letting `session_create` pick `auto:jev` or any owner-only routing.

## 5. Backward compatibility

Additive. Both record fields default to `""`; `reasoning_effort` on
`session_create` is optional. The one behaviour change is the intended one: a
crewmate's child that fell to the default runs the crewmate's model, and
passing `model` on the call keeps the old result.

## 6. Alternatives considered

- **Opt-in pin only (no default inheritance).** Rejected by the product owner:
  the common case is "workers run what the conductor runs", and an opt-in
  leaves every conductor on the default until someone edits every profile.
- **Reuse `agent.role_models.subagent` (or a new role) for dispatched
  sessions.** Rejected: a role pin is one process-wide value, read by
  `AgentConfig.resolve_model(role)` with no caller identity, and the ask is per
  crewmate: one conductor on a strong model whose workers should be cheap, next
  to another whose workers must run its own model to keep its quality. A role
  cannot say "cheap for this crewmate, same-as-me for that one", and a session
  dispatched through `session_create` is not a subagent run (`spawn_run`), so
  the `subagent` role would also change the wrong population. The per-crewmate
  pair lives where the person already sets that crewmate's own model, on its
  profile, and the AGENTS.md / model-selection.md rule is amended to name it as
  the one exception.
- **Read the caller slot's live model instead of the profile record.**
  Rejected: the Crewmates page has no per-chat picker, so the record is the
  source of truth there, and it survives a restart.
- **Fail the create on a bad inherited pin.** Rejected (decision 3): the
  failure would land on the agent, and the only remedy would be the per-call
  `model` this change exists to remove.
