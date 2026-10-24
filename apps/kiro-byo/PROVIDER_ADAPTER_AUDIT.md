# Provider Adapter Audit — Final 0.1.0

This audit follows the Batch 04 extraction of `http_sse.py` and
`chat_completions_common.py`. The rule is simple: a vendor/product name is not a
protocol boundary. New commercial plans must not create adapter classes.

## What was consolidated in this batch

- `providers/registry.py` no longer defines one forwarding class per vendor.
  `_ModuleAdapter` binds a transport id to the protocol module once; aliases such
  as MiniMax→Anthropic Messages and Ark/DeepSeek/xAI/Groq/Qwen/Qianfan→Responses
  are data entries in the registry.
- OpenAI-compatible, Anthropic Messages, Mistral Chat and Cohere V2 now open JSON
  POST requests through the provider-neutral `http_sse.post_json` transport.
  Together with Batch 04's four shared Chinese Chat adapters, ordinary request
  opening/error normalization is no longer copied across those adapters.
- Zhipu/Moonshot/Qwen/Xiaomi continue to share `chat_completions_common.py` for
  Chat-Completions continuation/SSE/tool assembly. Mistral now uses the same
  engine through a `ChatContentCodec`, retaining native ThinkChunk replay without
  carrying a private HTTP/SSE loop.
- Responses-family differences are declared in `providers/capabilities.py`; the
  shared Responses engine consumes flags for continuation, cache/store policy,
  reasoning wire shape and request-field stripping instead of provider-name `if`s.

## Intentional protocol-local code

`responses_api.py` and `gemini_interactions.py` still own their initial request
open because both require the original HTTP status code to decide whether an
expired server-side continuation id should be retried statelessly. This is a
protocol semantic, not vendor boilerplate. Their response-body reading and SSE
iteration still use `http_sse.py`.

Message/event parsers remain protocol-local where the wire format is genuinely
different (Anthropic content blocks, Gemini interaction outputs, Cohere events
and Responses items). Mistral's structured ThinkChunk merge is now a narrow
content codec injected into the shared Chat engine rather than a second parser/
stream stack.

## Resulting boundary

`Platform/Product/Profile -> adapter_transport -> protocol module -> http_sse`

Only the `adapter_transport` value chooses executable protocol code. Platform,
product, billing plan, region and entitlement are metadata and may change
without adding an adapter.
## Final edge-audit classification

The final audit keeps the same protocol boundary and explicitly classifies the remaining apparent duplication:

- only `http_sse.py`, `responses_api.py` and `gemini_interactions.py` construct raw `urllib.request.Request` objects in the provider package; the latter two intentionally need the original HTTP status to invalidate expired continuation handles before a stateless retry;
- vendor modules may pass `urllib.request.urlopen` as an injectable opener, but ordinary request construction/error/interrupt mechanics remain in `http_sse.py`;
- model-specific curated quirks remain fallback facts, not an allowlist: live discovery wins when the provider states a value, unknown capability remains unknown, and manual metadata may override locally;
- Platform/Product/Profile metadata may change endpoints, regions, entitlements and credential-prefix routing while reusing a code-owned adapter, but it cannot create a new executable transport. A new wire protocol still requires reviewed code and registry support.

The four formerly duplicated native Chinese Chat modules remain **416 lines total** (1,493 before the extraction, a 72.1% reduction). The full `providers/` Python package is **3,879 lines** after adding the reusable transport, shared chat engine and product/catalogue infrastructure; line count is recorded only as an audit signal, not as a target to minimize at the expense of protocol semantics.

