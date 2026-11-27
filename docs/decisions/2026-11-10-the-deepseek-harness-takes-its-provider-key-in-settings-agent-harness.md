# The DeepSeek Harness takes its provider key from a field in Settings → Agent Harness

Decided by: Bolin Chen (maintainer, @bolichen97)
Date: 2026-11-10

## Decision

The DeepSeek Harness row in Settings → Agent Harness has a Provider key field
that saves the key to Settings → Secrets and maps it in `agent.deepseek_env`, so
that harness needs no terminal and no config edit, and its no-key messages name
that field.

## Why

- The DeepSeek Harness was the one harness with no dashboard path at all: its
  messages sent the user to `agent.deepseek_env`, a config key with no UI.
- The other harnesses' fixes already had a dashboard path that their messages
  now name; this closes the remaining gap.

## Evidence

- https://github.com/kirodotdev/KiroCrew/issues/30605#issuecomment-6757030634 --
  the maintainer's ruling, part 2 of 2.
- https://github.com/kirodotdev/KiroCrew/issues/30605 -- the issue.
