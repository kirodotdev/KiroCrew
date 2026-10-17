# Every agent turn ends with a closing text or a `nothing_to_do` call, never a bare tool stop

Decided by: Zezhen Xu (maintainer, @CrysisDeu)
Date: 2026-10-03

## Decision

After zero or more tool calls, an agent turn on every surface ends in exactly one of two ways: a closing assistant text, or a `nothing_to_do` call with nothing after it. A turn that stops right after an ordinary tool with no text is a contract violation, and it keeps the empty-response recovery ladder (silent replay, one continue nudge, then the give-up card). The ladder is the enforcement of the contract; `nothing_to_do` is the only sanctioned silent exit.

## Why

- The recovery ladder shipped first (https://github.com/kirodotdev/KiroCrew/pull/375) as the fix for turns that ended empty, and a later fix stopped it from replaying a productive tool-only turn verbatim. Neither said what a turn is supposed to end with, so each new terminal shape (the ask_question card in #9320, a structured signal in #9324) re-opened the question.
- The maintainer settled it on issue #16392: "After zero or more tool calls, a turn MUST end in exactly one of two ways: 1. a closing assistant text, or 2. a `nothing_to_do` call (no text after it). Anything else -- a turn that stops right after an ordinary tool with no text -- is a contract violation and keeps today's empty-response recovery (continue nudge, then give-up card). So the ladder stays as the *enforcement* of the contract, and `nothing_to_do` is the only sanctioned silent exit."

## Evidence

- https://github.com/kirodotdev/KiroCrew/issues/16392#issuecomment-5966402445 -- the maintainer's statement of the contract.
- https://github.com/kirodotdev/KiroCrew/pull/16429 -- the change that implements it; its body records that a bare tool stop keeps today's recovery on purpose.
- https://github.com/kirodotdev/KiroCrew/pull/375 -- the empty-response recovery ladder this contract turns into enforcement.
- https://github.com/kirodotdev/KiroCrew/pull/9320 -- the ask_question card, the first silent exit that needed a rule.
- https://github.com/kirodotdev/KiroCrew/pull/18409 -- the pull request that adds this entry; the maintainer restates the decision in a comment there.
