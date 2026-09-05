# Design notes

Accepted design records for changes narrow enough that they have no owning module
spec. Each states current behavior and the reasoning behind it. A note whose subject
grows into a subsystem should become a spec under
[../../system-specs/modules/](../../system-specs/README.md) instead.

| Note | Covers |
|---|---|
| [soft-stop.md](soft-stop.md) | Cooperative cancel: acknowledging a stop before a hard kill so session state survives. |
| [session-slack-linking.md](session-slack-linking.md) | How a Slack thread maps onto a Kiro Crew session, and how thread state stays in sync. |
| [mcp-oauth-ownership.md](mcp-oauth-ownership.md) | Who owns an MCP server's OAuth tokens, and why that ownership is contested. |
| [connections-status-tiers.md](connections-status-tiers.md) | The authorization axis behind a Connections card: grant-presence status, source-backed connected-since, what Cancel releases, and which mint tiers exist. |
| [connections-disconnect.md](connections-disconnect.md) | What "disconnected" is allowed to mean: which local artifacts Disconnect removes, why the response carries three separate answers, and why entry identity is the endpoint rather than the name. |
| [connections-l1-smoke.md](connections-l1-smoke.md) | The Connections launch-gate ladder, and what an automated smoke run over a real stored grant can and cannot prove. |
| [connections-warm-table.md](connections-warm-table.md) | Serving every card's approval URL from one shared kiro-cli process: why the session is held, why the spec set is grant-blind, and what a respawn costs. |
| [mcp-entry-provenance.md](mcp-entry-provenance.md) | Which entries in a shared MCP config file a sync may rewrite: the write-authorship marker and its four outcomes. |
| [mcp-gateway-claim-push.md](mcp-gateway-claim-push.md) | Event-driven caller identity for pooled MCP stubs. |
| [mcp-gateway-oversize-response.md](mcp-gateway-oversize-response.md) | Handling an MCP tool response that exceeds the gateway read buffer. |
| [oversized-image-session-wedge.md](oversized-image-session-wedge.md) | Why one oversized screenshot fails every later turn, which capture path escapes the inline-image caps, and how to repair a wedged transcript. |
| [mcp-stub-decoupling.md](mcp-stub-decoupling.md) | Why the stub is emitted for every server, and why per-connection backends stay outside the pooling budget. |
| [profiling.md](profiling.md) | The debug-only stack sampler and desktop app metrics. |
| [tool-stall-watchdog-placement.md](tool-stall-watchdog-placement.md) | Which stall checks belong in the ACP read loop and which must be judged out of band. |
| [memory-benchmarks.md](memory-benchmarks.md) | Measuring the memory layer against LongMemEval and LoCoMo, and why the retrieval ruler is deterministic. |
| [slots-ordering-token.md](slots-ordering-token.md) | Planned: the two wire contracts that would let the client stop reconstructing slots ordering and close-failure determinism, and the code each one deletes. |
