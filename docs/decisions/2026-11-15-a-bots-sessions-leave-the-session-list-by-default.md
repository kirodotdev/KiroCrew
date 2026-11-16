# A Bot's sessions leave the person's session list by default

Decided by: Joe Guo (maintainer, @iamwhatever)
Date: 2026-11-15
Supersedes: none (turns on by default what `2026-11-03-a-crewmates-sessions-leave-the-sidebar-for-its-sessions-tab.md` described behind a preview)

## Decision

The "Bot sessions on the bot's profile" Feature Preview is retired and its
behavior is the default: the sessions a live Bot thread dispatched leave the
person's own session lists and show on the Bot's Sessions tab. A stored
`mate_sessions` switch is ignored. Each Bot's Settings carries one switch,
**Show in my Sessions list** (`show_sessions_in_list`), off by default; on, that
Bot's sessions also stay in the person's lists. The Sessions tab lists them
either way. The LTS edition follows the same default: its edition lock held the
preview off, and with the preview gone there is no lock to hold.

## Why

- Bot sessions crowd the main session list.
- The preview ran for a week with no reported problems.
- A person who still wants a Bot's sessions in the main list can turn that on
  per Bot, without a global switch.

## Evidence

- The owner's request, restated in the issue this entry closes:
  https://github.com/kirodotdev/KiroCrew/issues/37712
- The entry this turns on by default:
  `2026-11-03-a-crewmates-sessions-leave-the-sidebar-for-its-sessions-tab.md`
