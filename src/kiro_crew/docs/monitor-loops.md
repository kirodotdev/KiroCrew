# Monitor loops

A monitor loop keeps one conversation checking something on a schedule — a pull
request, a CI run, a ticket, a deployment — without you re-asking. Say "keep an
eye on this PR and tell me when it is review-ready" and the session wakes itself
on an interval, does the check, and only speaks up when there is something to
say.

The loop runs in **the same conversation**, not a new one. It carries the same
context, the same tools, and the same history, so it does not have to be
re-briefed each round. Each cycle appends a turn to that conversation.

## Starting and stopping one

Ask for it in words: "monitor", "keep checking", "babysit", "let me know when".
The agent arms the loop with the check instructions and an exit condition, then
ends its turn. You do not have to stay in the tab.

To stop it, say so — or use the dashboard control below.

## The 🎯 goal control

The composer carries a goal button (a target icon). It shows whether a loop is
active, which cycle it is on, and the countdown to the next wake. Open it to set
a goal, change the seconds between wakes, adjust the cycle cap, or stop the loop
yourself.

## Timing

The interval is the gap between wakes, and it is **deadline-preserving**: your
own messages defer a wake that comes due until your turn finishes, but they do
not restart the clock. A loop stays on schedule in a conversation you are
actively using.

A loop the agent arms caps at **24** cycles by default; the goal control's own
field starts at `0`, so a loop you arm there is unlimited unless you set a
number. Either way the cap is a runaway backstop, not a finish line —
a loop that reaches its cap ran out of rope rather than completing. A cap of `0`
is unlimited, which is worth asking for only when you genuinely want an
unbounded loop.

A separate wall-clock budget can bound elapsed time instead of cycles, which
suits a loop whose turns are slow or whose interval is long. When the budget is
spent the loop deactivates and tells you.

## One at a time

A session holds one automation. Arming a new loop is refused while an active one
exists, so a second request does not silently replace the first. A loop the
system already stopped — an approval stall, a spent cap or budget, a finished
subject — is replaced by the new one; a loop you paused or stopped yourself is
preserved.

Loops survive a gateway restart.

## Watching a pull request specifically

For a public GitHub pull request there is a cheaper path than a general loop: a
structured watch that probes the provider directly and wakes the session **only
when a new revision needs action**. An unchanged, pending, retrying, or finished
probe costs no agent turn at all. Ask to watch a PR until it is review-ready and
you get this instead of a turn every interval.

You can ask what the current watch is doing, or stop it, at any time.

## Where loops work

Dashboard chat, Slack threads, Discord DMs, and Webex. A loop bound to one of
those channels stays bound to it. Anywhere else — a cron job, a webhook session, a
subagent — the arm is refused with a note to use a schedule instead.

The structured pull-request watch above is narrower: it needs typed wake delivery,
which exists for dashboard, Slack and Discord only. Ask for it from a Webex
session and you get the general loop, not the cheap probe.

## Related docs

- [Subagents](subagents.md): parallel fan-out rather than repeated checks
- [Cron and scheduling](cron-and-scheduling.md): a schedule that starts a *fresh* session each time
- [Dashboard](dashboard.md): the composer and the side panel
