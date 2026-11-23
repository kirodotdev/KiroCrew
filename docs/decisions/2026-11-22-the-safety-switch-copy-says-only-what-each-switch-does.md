# The safety switches say only what each switch does, and how to clear a settings read that failed

Decided by: Bolin Chen (maintainer, @bolichen97)
Date: 2026-11-22

## Decision

Settings → Security → Safety switches reads, in every shipped language: the ask toggle "Ask me before running tools set to auto-approve in a tool server's settings", with the help "Tools you set to Always allow, on an approval card or in MCP Servers, still run without asking."; the protect help "On: the agent's file-editing tools can't change the rules that let tools run without asking you. Off: they can.", with the terminal caveat on its own line under the toggle ("This does not cover commands the agent runs in a terminal: they can change these rules whether this is on or off."); the in-force note "On for now, because Kiro Crew could not read its settings in full. Clicking won't turn it off before then." (and "Off right now, though On is saved. Clicking saves On."); and the degraded notice ending "Fix the mistake in config.json or config.local.json (in ~/.kiro/crew by default), then restart Kiro Crew.", keeping its Retry button and its ask-agent link. The pending-review message every settings save shares is unchanged.

## Why

- The copy that shipped with the switches was chosen so the feature could land, and the review's remaining copy asks were moved to issue #37552 by the maintainer who chose the shipped wording.
- Each string says only what the code does: the ask switch drops only the `autoApprove` lists in tool server entries, Always allow grants are a separate list that still runs, the protect switch fences only the agent's file-edit tools, and a read that cannot see the settings keeps the protect fence on until they read in full, whatever a click saves (a malformed `mcp` section stays marked until a restart).

## Evidence

- Maintainer decision: https://github.com/kirodotdev/KiroCrew/issues/37552#issuecomment-7074504558
- Issue: https://github.com/kirodotdev/KiroCrew/issues/37552
- The shipped wording and the move of the remaining asks: https://github.com/kirodotdev/KiroCrew/pull/37410#issuecomment-6891137853
