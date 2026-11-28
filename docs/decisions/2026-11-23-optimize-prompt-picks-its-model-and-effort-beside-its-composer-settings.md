# Optimize prompt picks its own model and effort beside its Composer settings, defaulting to Background

Decided by: Bolin Chen (maintainer, @bolichen97)
Date: 2026-11-23

## Decision

Optimize prompt has its own model and reasoning-effort choice, the `optimizer` role
(`agent.role_models.optimizer`, `agent.role_efforts.optimizer`), shown as Optimize
Prompt Model and Optimize Prompt Effort in Settings > Chat > Composer right after
Optimize Before Sending; each offers its Background row's list, its unset value
reads "Same as Background" and keeps the optimizer on what other background work
runs on, and neither row depends on Show Optimize Prompt.

## Why

- A rewrite waits between writing a prompt and sending it, so its speed matters
  more than for other background work, and lowering the shared Background settings
  would change every background job.
- The per-role settings are already where a model pin for one kind of work lives,
  with its validation and its effort pairing; this adds a role, not a mechanism.
- The rows sit with the optimizer's own two settings, where someone looking for what
  Optimize prompt runs on looks. Only the optimizer gets such a pair: titles, memory
  consolidation and heartbeat keep the shared Background settings.
- The chat composer's single model-and-effort control
  (`2026-06-14-chat-composer-model-and-effort-are-one-control.md`) is unchanged:
  these are Settings rows, which already pair a model row with an effort row per role.

## Evidence

- https://github.com/kirodotdev/KiroCrew/issues/39525 -- the request.
- https://github.com/kirodotdev/KiroCrew/issues/39525#issuecomment-7092529731 -- the
  maintainer decision on record.
