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
| [mcp-entry-provenance.md](mcp-entry-provenance.md) | Which entries in a shared MCP config file a sync may rewrite: the write-authorship marker and its four outcomes. |
| [mcp-gateway-claim-push.md](mcp-gateway-claim-push.md) | Event-driven caller identity for pooled MCP stubs. |
| [mcp-gateway-oversize-response.md](mcp-gateway-oversize-response.md) | Handling an MCP tool response that exceeds the gateway read buffer. |
| [mcp-stub-decoupling.md](mcp-stub-decoupling.md) | Why the stub is emitted for every server, and why per-connection backends stay outside the pooling budget. |
| [profiling.md](profiling.md) | The debug-only stack sampler and desktop app metrics. |
| [tool-stall-watchdog-placement.md](tool-stall-watchdog-placement.md) | Which stall checks belong in the ACP read loop and which must be judged out of band. |
| [memory-benchmarks.md](memory-benchmarks.md) | Measuring the memory layer against LongMemEval and LoCoMo, and why the retrieval ruler is deterministic. |
