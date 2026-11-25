# The Slack card's sign-in button says it opens the setting, and its steps name the Agent Harness tab

Decided by: Bolin Chen (maintainer, @bolichen97)
Date: 2026-11-24

## Decision

When Kiro Crew's sign-in does not serve the Slack card yet, the card's button and the MCP Servers row's link read "Open Kiro Crew sign-in setting" and still open Settings → Developer → Feature Previews with the switch highlighted; the setup steps read "open the Kiro Crew sign-in setting with the button and make sure it is on" and "in Settings → Agent Harness, choose Kiro Agent", naming the tab by the label the Settings page shows in each language; and the line for a Slack server Kiro Crew does not sign in reads "The Slack server listed in MCP Servers handles its own sign-in, so there is nothing to do here."

## Why

- The copy that shipped with the card ("Turn on Kiro Crew sign-in", "in Settings, set chats to run on Kiro Agent", "Your own Slack entry ... signs in by itself") was chosen so the feature could land, and the review's remaining copy asks were moved to issue #37235 by the author of that change.
- The button is a link to a setting, not a switch, so its label says it opens the setting and whose sign-in it is: on the card the steps sit in a tip, so the button is often the only text a reader sees.
- The steps also show while the switch is already on and chats run on Kiro CLI, so the first step says "make sure it is on" rather than "turn it on".
- The MCP Servers list also shows servers that another tool's own config added, so the line says "listed in" rather than "you added".
- This replaces the shipped label, and the shipped test expectation that the tip does not name the Agent Harness tab; the label named in `docs/request-for-change/rfc-crew-oauth-sign-in-redesign.md` is the earlier one.

## Evidence

- Maintainer decision: https://github.com/kirodotdev/KiroCrew/issues/37235#issuecomment-7119369653
- Issue: https://github.com/kirodotdev/KiroCrew/issues/37235
- The options and the author's recommendation: https://github.com/kirodotdev/KiroCrew/issues/37235#issuecomment-6886773309
- The change that shipped the earlier copy: https://github.com/kirodotdev/KiroCrew/pull/34811
