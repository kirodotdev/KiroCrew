---
name: conformance-invalid-mcp-valid
description: Supply the valid skill observation from the malformed MCP fixture during an Agent Plugins conformance probe run. Use when the run-conformance skill requests this skill.
---

# Malformed MCP document skill probe

This skill's marker is `APC_INVALID_MCP_VALID_V1`.

If the client exposed this skill and you loaded this body through its normal skill mechanism, add this observation to the current run:

```json
{"kind":"skill","skill":"conformance-invalid-mcp-valid","marker":"APC_INVALID_MCP_VALID_V1"}
```

A client-provided catalog followed by reading its advertised skill resource is a valid mechanism. Finding this file independently by filesystem search or receiving its contents in a prompt does not establish client discovery. In that situation, omit the client-discovery observation and report the limitation.
