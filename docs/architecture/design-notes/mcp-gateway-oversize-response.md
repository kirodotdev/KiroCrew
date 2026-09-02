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

### Layer 3: Inline-image budget (tool-result image blocks)

Runs before the spill step, on any response line that can carry an
`{"type": "image", ...}` content block. The pre-filter's negative answer is
provable, not heuristic: evading the literal `"image"` byte probe requires
writing at least one of the letters as a JSON `\u` escape (the only escape
that yields a letter), so the filter matches on either the literal or the
escape marker and lines that skip the parse provably cannot carry an image
block. Every image block in a `tools/call` result is held to the same
per-image budget the prompt path enforces (`kiro_crew/imaging.py`:
`MAX_IMAGE_EDGE_PX` = 2000 px longest edge, `MAX_IMAGE_B64_BYTES` = 5 MiB
base64):

1. An image inside both caps passes through **byte-identical** after a
   structural integrity check (`verify()`, no pixel decode): a truncated
   file with a readable header is a corrupt block and wedges history the
   same way an oversized one does, so it fails closed too. Byte-identical
   requires canonical base64: a compliant image with wrapped/whitespace
   base64 keeps its bytes but is re-emitted canonically, because the byte
   ceiling is measured on the canonical form and forwarding the inflated
   original could exceed the very limit it just passed.
2. An oversized image is downscaled in place (`data`/`mimeType` replaced).
   Base64 is decoded whitespace-tolerantly, so 76-column-wrapped producers
   (`base64.encodebytes`) are not regressed.
3. An image whose compliance cannot be established is replaced by a text
   block saying it was omitted — **fail closed per block** — because
   kiro-cli replays the full conversation history every turn, so one
   forwarded oversized image permanently wedges the session on the backend's
   many-image dimension limit. This covers: undecodable bytes, a Pillow
   decompression-bomb refusal, a source over `MAX_IMAGE_SOURCE_PIXELS`
   (64M px, refused on the header read before any decode, bounding the cost
   an adversarially compressed small-bytes/huge-area raster can impose),
   still over the byte ceiling at the 256 px floor, and Pillow absent.

Cumulative decode work per response is bounded by a source-PIXEL budget
(`MAX_FRAME_SOURCE_PIXELS`, 4x the per-image ceiling): each block's
header-read area is charged before its decode, and blocks past the budget are
replaced by an "omitted -- too much image processing" text block **without
being decoded**. A pixel budget rather than a block count lets legitimate
many-image results (e.g. ten small PDF-page renders) through whole while
bounding the adversarial case at four maximum-size decodes.

Omission notes themselves are capped (4 per response, the rest collapsed
into one counted summary block): one-for-one replacement of a many-block
frame would amplify it -- each note is ~10x an empty image block -- past the
relay read limit, turning a rewrite into a dropped response.

The work is split across two pools: the byte probe's false positives (any
frame carrying escaped non-ASCII text matches its `\u` half) are cleared by
a cheap parse-confirm stage on the maintenance pool (like the spill rewrite),
and only genuinely image-bearing frames reach the 2-worker `mc-image` pool --
so one server's screenshot decodes cannot head-of-line block other servers'
text-only results.

Structural failures (non-JSON frame, no `result.content`, re-serialization
error) forward the original line, mirroring the spill layer: those frames
carry no image the budget can verify, and failing them would break the call
for every co-pooled tenant. Pillow work runs on the dedicated `mc-image`
executor pool (`executors.image_executor`), never the event loop and never
the maintenance workers the orphan sweeps need. Implementation:
`mcp_gateway/image_budget.py`.

Scope: this seam covers every **brokered MCP server** (the gateway is the one
hop Kiro Crew owns between an MCP server and kiro-cli's history). Two shapes
deliberately stay outside it: images produced by kiro-cli's own built-in
tools (e.g. its file-read tool in image mode) never transit Kiro Crew and
must be capped upstream in kiro-cli; and MCP embedded resources
(`{"type": "resource", "resource": {"blob": ...}}`) are not rewritten because
no brokered path renders resource blobs to the model today.

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
