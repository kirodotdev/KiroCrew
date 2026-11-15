# "Fork with another model" opens the new chat's own model picker

Decided by: Bolin Chen (maintainer, @bolichen97)
Date: 2026-11-11

## Decision

An assistant reply's More menu lists "Fork with another model" after the fork
entries. It forks with the earlier messages and then opens the new chat's own
composer model picker, so the model and the effort are chosen together in that
one control and the parent is unchanged. A model picked there replaces a routed
Auto choice in the new chat. The fork request gets no `model` field, and the
agent's fork is not part of it.

## Why

- The menu fork sends no prompt, so nothing runs in the new chat before the
  pick.
- Reusing the composer's picker keeps model and effort one control
  (`2026-06-14-chat-composer-model-and-effort-are-one-control.md`) with one
  validation path, instead of a second fork-only model list.

## Evidence

- https://github.com/kirodotdev/KiroCrew/issues/35226#issuecomment-6797914126 --
  the maintainer's ruling.
- https://github.com/kirodotdev/KiroCrew/issues/35226 -- the issue: asking a
  second model the same question took a fork, a model change and a retyped
  prompt.
