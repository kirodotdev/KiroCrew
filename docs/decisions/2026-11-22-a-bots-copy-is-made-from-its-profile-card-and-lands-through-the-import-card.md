# A bot's copy is made from its Profile card and lands through the import card

Decided by: Bolin Chen (maintainer, @bolichen97)
Date: 2026-11-22

## Decision

A bot's Profile card has a "Make a copy" row under "Export as file" that lists what the copy gets and what it does not, then raises the import card, whose name is editable before the press on every import card; a copy carries what Export carries, except the lessons skill, and the import card applies (today the description, picture, rules, first goal, the schedules it can carry with their time, message and script, arriving paused, the skills in the bot's own workspace and a built-in dashboard, and not its milestones, check-ins, Heartbeat, lessons skill, schedule options, tool limits, abilities, workspace files, notes, memory, chat history, running work, credentials or approval settings; the copy starts at Normal permissions and its other settings at their defaults).

## Why

- It reuses the one path that already decides what leaves a bot, and lists each part before anything is created.
- It carries the parts that are slow to rebuild by hand: rules, the goal, schedules and the bot's own skills; today a copy gets no milestones, and its check-ins and Heartbeat start off.

## Evidence

- https://github.com/kirodotdev/KiroCrew/issues/36866#issuecomment-7086531461 -- the maintainer's ruling.
- https://github.com/kirodotdev/KiroCrew/issues/36866 -- the request.
- https://github.com/kirodotdev/KiroCrew/issues/35646 -- the bot row menu the request was split from.
