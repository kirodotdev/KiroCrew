# The Conductor introduces itself and reports with a task board in every chat

Decided by: Bolin Chen (maintainer, @bolichen97)
Date: 2026-10-02

## Decision

Every `kirocrew-conductor` chat introduces itself in plain words and reports with a live task board that puts "Needs you" first, wherever it is opened (agent picker, Crew Mode, cron, CLI), not only chats where Crew Mode is on.

## Why

- The goal is to make the Conductor comfortable for everyday users. The introduction and the task board are part of what the Conductor is, wherever it is opened.
- Limiting them to Crew Mode would need an extra flag, and that flag would give people two different Conductors.

## Evidence

- https://github.com/kirodotdev/KiroCrew/pull/16088#issuecomment-5948162487 -- the maintainer's on-record decision on the pull request that adds this entry.
- https://github.com/kirodotdev/KiroCrew/pull/16088 -- the pull request that adds the "Talking to the person" section to the Conductor's prompt.
