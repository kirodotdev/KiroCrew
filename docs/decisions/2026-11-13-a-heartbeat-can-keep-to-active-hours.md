# A Bot's Heartbeat can keep to active hours, set on the Schedules tab

Decided by: Bolin Chen (maintainer, @bolichen97)
Date: 2026-11-13

## Decision

A Bot's Heartbeat has optional active hours, an "Only between" window of whole hours (00:00 to 24:00, the whole day, by default) in the zone its schedule runs in (the record's time zone when it names one, else the gateway's default zone, the host's unless one is configured), set on the Schedules tab's Heartbeat row under the interval; outside the window no Heartbeat wake is scheduled, and the window does not cross midnight, has no weekdays, and is not carried by `set_check_ins`.

## Why

- A Bot on a laptop that is closed every night kept its Heartbeat on all night, and the only way to keep it to the working day was to turn it off and on by hand.
- One optional window whose default is today's whole day stops those wakes and leaves every existing Heartbeat as it is.
- Weekdays only, a window across midnight, and setting it from a Bot's card were left for later; each can be added without changing this one.
- A tick missed while the host sleeps is not replayed, so no catch-up setting was needed.

## Evidence

- https://github.com/kirodotdev/KiroCrew/issues/35019 -- the request.
- https://github.com/kirodotdev/KiroCrew/issues/35019#issuecomment-6860140411 -- the maintainer's decision.
