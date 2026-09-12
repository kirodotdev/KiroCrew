# Session ledger

The session ledger is a small durable record of what a long-running session is
doing: the goal, the phase it has reached, the concrete next step, the approaches
already tried and rejected, and pointers to whatever it produced. It lives on
disk, so it outlasts the conversation's own memory.

That is the point. A very long session eventually has its earlier turns
compacted away to make room, and a monitor loop's cycle can start hours after the
one before it. The ledger is what the session reads to re-establish where it was
instead of re-deriving it from a transcript that may no longer contain the
answer.

## What you get from it

Ask "where are we on this?" in a long-running session and the answer comes from
the ledger rather than from the agent scrolling back — so it stays accurate after
compaction, after a gateway restart, and across a monitor loop's cycles.

The useful entries are written as intent, not status: "add the Windows branch to
the permission helper, the test already fails" tells a cold resume what to do,
where "in progress" does not.

## What it is not

The ledger has **no dashboard page**. It is storage the agent reads and writes,
not a view you browse. To see it, ask the session about it.

It is also only for genuinely long-horizon work — a babysit loop, a multi-wake
task, a goal that spans hours. A single-turn request does not get one, and
nothing is lost by that.

## Finishing

A ledger is marked finished when its work is done or abandoned, which stops it
being re-injected into later cycles. A stale ledger quietly steering a session
that has moved on is the failure this avoids.

## If the ledger reports a missing identity channel

New installations route `kirocrew-core` through the MCP gateway automatically.
That starts a broker process and a stub for each core connection; sharing
backends across sessions remains off unless you enable it separately.

An upgrade preserves saved routing lists, including an empty
`mcp_gateway.stub_servers: []`. Older versions could save that empty default
during an ordinary configuration update, even if you never chose to turn
routing off. Upgrading alone does not change it.
`kirocrew doctor` reports this empty-list upgrade guidance on every platform.

If ledger calls are refused because core is unrouted, open **Developer → MCP
Management** and enable routing for **kirocrew-core**. Apply the change between
tasks and restart Kiro Crew, then ask the session to read its ledger. For a
headless setup, add `"kirocrew-core"` to `mcp_gateway.stub_servers`, preserving
any other entries, and restart. An empty ledger is a successful read if the
session has not recorded work yet.

## Related docs

- [Monitor loops](monitor-loops.md): the repeated-wake work a ledger most often backs
- [Task runner](task-runner.md): autonomous multi-step execution from a spec file
- [Memory and learning](memory-and-learning.md): what persists across *different* sessions
