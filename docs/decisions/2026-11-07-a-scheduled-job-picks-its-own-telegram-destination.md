# A scheduled job picks its own Telegram destination on the Schedule page

Decided by: Bolin Chen (maintainer, @bolichen97)
Date: 2026-11-07

## Decision

A scheduled job can carry one optional destination on another messaging channel,
saved as `send_message`'s `channel_type` and `target_id` pair, beside (never
together with) its Slack `channel`. The Schedule form picks it from the Telegram
private chat and the private topics the bot already has a conversation in; the
dashboard does not create topics. There is no shared default-destination setting:
pointing many jobs at one topic is a batch action on the Schedule page's
selection. Changing the destination edits the job in place, so it keeps its id,
history and times.

## Why

- It lets all job output go to one topic, and lets a job choose its own, with one
  pair of job fields and one read of known topics.
- It reuses the destination ids and checks `send_message` already has, so nothing
  becomes reachable that was not already.
- A generalized `channel` would make every Slack reader of it tell two kinds of
  id apart, and a default-destination setting would add a second, implicit
  destination every delivery leg and the grant fingerprint would have to read.

## Evidence

- https://github.com/kirodotdev/KiroCrew/issues/24112#issuecomment-6704236206 -- the
  maintainer's decision on the issue.
- https://github.com/kirodotdev/KiroCrew/issues/24112 -- the issue.
- https://github.com/kirodotdev/KiroCrew/pull/31123 -- the `topic:<user id>:<topic id>`
  target this builds on.
