# The crewmate's Heartbeat is a row on the Schedules tab, not a check-in on the Goals tab

Decided by: Zezhen Xu (maintainer, @CrysisDeu)
Date: 2026-10-22

## Decision

On a crewmate's Profile card, the Heartbeat switch (with its interval picker) is the first row of the **Schedules** tab. It does not appear on the **Goals** tab: the Goals tab's Check-ins are the three goal check-ins (Daily Goal Reminder, Weekly report, Monthly review) and nothing else. The Heartbeat keeps its storage, its schedule and its wording; only its place changes.

## Why

- The Heartbeat wakes the mate to find its own work. It is not a reading of the goals, which is what a check-in is.
- The Schedules tab answers "what wakes this mate on its own"; a wake that lives anywhere else is one the user has to know to look for.

## Evidence

- https://github.com/kirodotdev/KiroCrew/issues/23262 -- the issue carrying the decision ("heartbeat 可以放进schedule里面", restated there in English).
- https://github.com/kirodotdev/KiroCrew/pull/23280 -- the pull request that implements it, where the maintainer restates the decision.

This supersedes the placement in `docs/request-for-change/rfc-crewmate-heartbeat.md` (§ "Where": a full-width row under the three check-in tiles), which had no entry in this directory.
