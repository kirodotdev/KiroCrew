# MCP Gateway: Oversized Response Handling

## Problem

MCP tool responses (e.g. from `ReadInternalWebsites` reading a large CR page)
can exceed the gateway's read buffer limit. Before this fix, the response was
silently dropped and the calling session hung for 600s until the ACP watchdog
killed it.

## Config Keys

### `mcp_gateway.read_buffer_limit_bytes`

Maximum bytes for a single JSON-RPC response line on the backend stdout pipe.
Responses exceeding this trigger per-request fast-fail with a descriptive
JSON-RPC -32000 error.

- **Default:** 67108864 (64 MiB)
- **Env override:** `KIROCREW_MCP_READ_LIMIT`
- **Minimum:** 1024

### `mcp_gateway.response_spill_threshold_bytes`

Tool-call results larger than this (but under the read limit) have their text
content written to a sidecar file and truncated inline to 16 KiB + a file path
marker. This prevents large responses from bloating the LLM's context window.

- **Default:** 262144 (256 KiB)
- **Env override:** `KIROCREW_MCP_SPILL_THRESHOLD`
- **Set to 0:** Disables spilling (all under-limit responses pass through)

## Behavior

### Layer 1: Transport (read buffer limit)

When an MCP backend emits a response line exceeding `read_buffer_limit_bytes`:

1. The oversize line is drained (never stored in memory).
2. The JSON-RPC `id` is parsed from the first ~512 bytes of the drained head
   (a JSON prefix parse, then a targeted `"id":` regex fallback).
3. If id is recovered → ONLY that pending request receives a -32000 error.
4. If the oversize line was an `initialize` response (or the id is
   unrecoverable), the whole shared backend is recycled — the handshake can
   never complete, so no single-request fail would unwedge it.
5. **Nothing ever hangs.** Every in-flight request gets a response or the
   backend is respawned.

The error message is self-explanatory:

```
MCP response too large (1482937 bytes > limit 67108864); raise
mcp_gateway.read_buffer_limit_bytes or narrow the query
```

### Layer 2: Spill-to-file (large tool results)

For responses that fit within the read limit but exceed the spill threshold:

1. Parse the response as JSON-RPC.
2. Check if it's a `tools/call` result (has `result.content` list with `text` items).
3. Write the **full original response** to `~/.kiro/crew/mcp_spill/<server>-<request_id>-<timestamp>.json`.
4. Truncate each text item to the first 16 KiB.
5. Append a marker: `[KiroCrew: response truncated -- full <N> bytes at <path>. Read with bash: head/grep/jq.]`
6. Forward the rewritten (smaller) response.

Non-tool-result frames, errors, and small responses are **never** spilled.

Any spill failure (disk full, permissions) → original forwarded unmodified.

### Spill file format

- **Directory:** `~/.kiro/crew/mcp_spill/` (mode 0700)
- **Filename:** `<server_name>-<request_id>-<unix_timestamp>.json`
- **Content:** Complete original JSON-RPC response line
- **Cleanup:** Files older than 24h are deleted on gatewayd startup

### Inline marker format

```
[KiroCrew: response truncated -- full 1482937 bytes at /home/user/.kiro/crew/mcp_spill/example-mcp-gw-12345-7-1721200000.json. Read with bash: head/grep/jq.]
```

## Troubleshooting

If you see `-32000 "MCP response too large"` errors:

1. **Narrow the query** — ask the tool for less data (e.g. specific sections vs full page).
2. **Raise the limit** — set `KIROCREW_MCP_READ_LIMIT=134217728` (128 MiB) in your env, or add to `~/.kiro/crew/config.json`:
   ```json
   {
     "mcp_gateway": {
       "read_buffer_limit_bytes": 134217728
     }
   }
   ```
3. **Restart the gateway** — `kirocrew restart`

If responses are being truncated and you need full content:

1. The spill file path is in the truncation marker — read it directly.
2. To disable spilling entirely: `"response_spill_threshold_bytes": 0`
3. To raise the threshold: `KIROCREW_MCP_SPILL_THRESHOLD=1048576` (1 MiB)
