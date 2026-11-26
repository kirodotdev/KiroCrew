# The Kiro account tab says when a sign-in there moves new chats to the built-in engine

Decided by: Bolin Chen (maintainer, @bolichen97)
Date: 2026-11-25

## Decision

While Kiro Agent runs on the Kiro CLI sign-in and a Kiro Crew sign-in would move new chats to the built-in engine, the "Using your Kiro CLI sign-in" line in Settings > Kiro account keeps "You don't need to sign in here" and adds that new chats move to the built-in engine after a sign-in there; the chat's "Sign in to Kiro Crew once" card is unchanged and still does not name the engine.

## Why

- A person saw the chat card ask for a sign-in while Settings said they did not need one, and could not tell what that sign-in would change.
- `2026-11-18-signed-out-sign-in-prompts-do-not-name-the-engine-route.md` keeps the engine out of the chat's sign-in prompts and shows it in Settings, and the pull request that added the card applied it there, so the sentence goes in Settings.

## Evidence

- https://github.com/kirodotdev/KiroCrew/issues/42267#issuecomment-7163440005 -- the maintainer decision on the issue.
- https://github.com/kirodotdev/KiroCrew/issues/42267 -- the issue.
- https://github.com/kirodotdev/KiroCrew/pull/39998 -- the pull request that added the card and its note on the wording.
