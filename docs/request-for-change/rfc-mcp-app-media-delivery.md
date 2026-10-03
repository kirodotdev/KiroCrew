---
title: MCP-app media delivery — stop asking redaction to prove a negative about an opaque blob
status: draft
revision: v1
author: Kiro
created: 2026-09-10
last-audited: 2026-09-10
audited-at: 5bfda4a12
doc-pr: 10038
implementation-prs: []
tracking-issues:
  - "https://github.com/kirodotdev/KiroCrew/issues/9935"
supersedes: []
superseded-by: []
---

# RFC: MCP-app media delivery — stop asking redaction to prove a negative about an opaque blob

- Status: draft. Nothing implemented. A previous attempt to fix
  [#9935](https://github.com/kirodotdev/KiroCrew/issues/9935) inside the
  redaction layer was blocked three times, each time for a different reason, and
  §4 records why that layer cannot hold the decision at all. This document asks
  maintainers to settle a **trust-model question** (§6) rather than to approve a
  matcher.
- Author: Kiro
- Created: 2026-09-10
- Audited against: `5bfda4a12`
- Related: [`../system-specs/modules/security.md`](../system-specs/modules/security.md)
  (the redaction passes and the sink registry),
  [`../system-specs/modules/mcp-apps.md`](../system-specs/modules/mcp-apps.md)
  (the render and callback path this touches),
  [`../architecture/mcp.md`](../architecture/mcp.md),
  and [`rfc-app-sandbox-isolation.md`](rfc-app-sandbox-isolation.md) (apps still
  run with full privileges; the trust question here is a narrower instance of the
  same gap).
- Related unmerged work: draft PR
  [#9937](https://github.com/kirodotdev/KiroCrew/pull/9937)
  (`fix/media-data-uri-redaction`) is the redaction-layer attempt this document
  argues against, kept open as a draft so §4's three rejected bounds and their review
  verdicts stay readable. It is deliberately **not** an implementation of this RFC:
  Option A (§7) deletes everything in it. It is listed here rather than under
  `implementation-prs` for that reason.

## 1. Summary

An MCP app that returns an inline `data:image/*;base64,…` in its tool payload
renders a broken image. The gateway's redaction passes scan every string leaf of
the payload, a base64 media body is structurally indistinguishable from an
encoded secret, so the credential pass splices a `[REDACTED: credential]` tag
into the body ([#9935](https://github.com/kirodotdev/KiroCrew/issues/9935)).

The obvious fix — exempt inline media from the passes — cannot be justified at
the redaction layer, because every available justification either names a control
that does not cover the actual exfiltration channel (§4.1) or requires proving
that an opaque compressed blob contains no secret, which is not decidable (§4.2,
§4.3).

This RFC proposes to stop trying. The redaction passes stay media-unaware and gain
no carve-out. Instead the decision moves to **which payload fields are a defended
boundary at all** — and for the server-authored ones, none is.

The gateway returns the tool result to the model as `append_marker(result, …)`,
which copies `structuredContent` through untouched, and it performs no redaction on
that path. So the same bytes reach the model **unredacted** while a scanned copy
goes to the app that authored them (§5.1). The app's own `html` crosses unredacted
in the same frame (§5.2). Scanning the app's copy therefore strips a secret from the
one recipient that already had it, after the model's copy has gone out intact — and
charges a corrupted image for it.

Naming that lets media be delivered exactly as produced with no content inspection
anywhere, and confines redaction to `tool_input` — the one field whose content the
receiving server did not *author*, though §5.4 records that the server holds it raw
regardless, so the retained scan protects the browser-side surface rather than
withholding anything from the server.

## 2. Goals and non-goals

**Goals.**

- G1. An MCP app that returns inline `data:image/*` media in its tool payload
  renders it, whatever the container format, however it is compressed, and
  whatever the app declares in its CSP.
- G2. No security control is narrowed on the strength of a claim the code does not
  support, and nothing retained is described as protecting more than it does.
  Whatever is delivered unscanned is delivered unscanned *by a stated rule*,
  recorded where an operator reads posture, not by a content heuristic.
- G3. The redaction passes stay media-unaware, and every existing direct caller
  keeps scanning inline media in full.
- G4. Settle one question — which payload fields are a defended boundary — so the
  next reviewer reads a decision instead of re-deriving it.

**Non-goals.**

- N1. Making a payload safe against a **malicious** MCP server. It authors the
  app, its HTML, its CSP metadata and its `inputSchema`; nothing at this layer
  changes that.
- N2. Reducing what an app can send to its own server. The `tools/call` relay
  (§4.1) is untouched.
- N3. App sandboxing or privilege reduction —
  [`rfc-app-sandbox-isolation.md`](rfc-app-sandbox-isolation.md).
- N4. Streaming parity. A media URI split across chunk boundaries is still scanned
  by a media-unaware `StreamRedactor`; same defect class, different surface, its
  own change.
- N5. Introducing a new transport or endpoint for media bytes (§11 records why the
  out-of-band variant was rejected).

## 3. The defect

`mcp_apps_render._redact_leaves` applies the credential and exfiltration-URL
passes to every string leaf of `structured_content`, `tool_input` and
`result_content` before the `mcp_app_render` frame is sent
(`src/kiro_crew/mcp_apps_render.py`). Both passes are media-unaware. A webp body
routinely contains an `eyJ`-anchored run or a 40+ character base64 run, so the
heuristics match it and substitute a tag. What reaches the app:

```html
<img src="data:image/webp;base64,[REDACTED: credential]" alt="…">
```

The server's output was correct; the payload is altered in transit. Every MCP app
delivering an inline image is affected, and the platform *invites* one:
`buildMcpAppCsp` (`website/src/lib/mcpAppSrcdoc.ts`) grants the iframe
`img-src 'self' data:` and `font-src 'self' data:`.

Narrowing the matcher is not available: the base64 character class is deliberately
broad, and narrowing it reopens the chunking bypass `_B64_CHUNK_RE` /
`_BARE_SECRET_RUN_RE` are pinned against.

## 4. Why a redaction-layer exemption cannot be justified

This section is the reason the RFC exists. Three bounds were proposed and each
was rejected on review; recording them prevents a fourth attempt at the same
layer. Each is stated as the claim it made and the code that falsifies it.

All three are readable rather than described from memory: they were built and reviewed
on draft PR [#9937](https://github.com/kirodotdev/KiroCrew/pull/9937)
(`fix/media-data-uri-redaction`), whose diff carries the mask/restore machinery, the
container-signature table and the whole-body scan, and whose review thread carries the
verdicts. It is left open as a draft for that record. It is **not** a reference
implementation of anything this document proposes — it implements the approach §4 argues
against, and Option A deletes all of it (§7).

### 4.1 "It renders in a sandboxed iframe with `connect-src 'none'`, so it cannot egress"

False. An MCP app reaches its own MCP server through a relay that no CSP
directive governs:

- `website/src/components/McpAppFrame.tsx` — the frame `fetch`es
  `POST /api/mcp-apps/call`.
- `api_mcp_apps_call` (`dashboard/handlers/mcp_apps.py`, routed in
  `dashboard/server.py`) — the dashboard relays it to the gateway over the
  uid-gated socket.
- `handle_app_call` (`mcp_gateway/app_call.py`) — the gateway forwards
  `{"name": tool_name, "arguments": arguments}` to the server, where `arguments`
  is iframe-controlled (its own comment says so outright) and validated against
  an `inputSchema` **that same server authored**.

And the payload is handed to app JS in full before any of that: `McpAppFrame.tsx`
holds `payload.tool_input` / `payload.result_content` in `toolInputRef` and
`resultContentRef`, then delivers them over the `ui/notifications/tool-input` and
`ui/notifications/tool-result` notifications.

So `connect-src 'none'` does not mean "cannot exfiltrate". A CSP gate limits
direct CSP-governed egress only, and is never a licence for an exemption.

### 4.2 "The decoded head matches a container signature, so it is a real image"

Insufficient. A structurally valid PNG carries arbitrary bytes in `tEXt`, `zTXt`,
`iTXt`, `COM` or EXIF chunks. Any MCP server can produce a file whose first 12
bytes are a genuine signature and whose metadata carries a credential.

### 4.3 "Decode the whole body and scan the decoded bytes"

Insufficient, and not completable. Scanning decoded bytes does catch an ASCII
secret in a metadata chunk, but it misses anything the scanner cannot see as
text:

- **Compressed sections.** PNG `IDAT` is deflate; a compressed credential reads
  as binary noise.
- **Formats we cannot decompress at all.** `install_requires` in `setup.cfg`
  carries neither Pillow nor brotli, so the runtime has stdlib `zlib` only:

  | Format | Compression | Scannable with stdlib |
  |---|---|---|
  | bmp, ttf, otf | none | already fully scanned |
  | png, woff | zlib/deflate | yes |
  | gif | LZW | no |
  | jpeg | Huffman + DCT | no |
  | **webp** | VP8/VP8L | **no** |
  | avif, heic | AV1/HEVC | no |
  | woff2 | brotli | no |

  #9935 is a `data:image/webp`, so "decompress or refuse to exempt" leaves the
  reported defect unfixed.
- **Pixels.** The most ordinary way a credential ends up inside an image is a
  screenshot of a token on screen. That secret survives full decompression and is
  invisible to any byte-level scan; detecting it needs OCR.

The general shape: an exemption at this layer requires proving a negative about
an opaque blob. Adding a fourth bound would be the review smell
[`AGENTS.md`](../../AGENTS.md) already names — when a review finds "X also
reaches the fence via spelling Y", the question is whether the **subject** is
wrong.

## 5. The scan defends nothing: the same bytes already crossed unredacted

The subject is wrong, and two independent facts in the code say so.

### 5.1 The model receives the identical bytes, unredacted

The gateway returns the tool result to the agent as
`append_marker(result, spool_id)`. `append_marker` prepends the render marker to
the first **text** content item and copies everything else through, so
`structuredContent` reaches the agent exactly as the server wrote it. And the
gateway performs no redaction on that path at all: the only `redact` calls in
`mcp_gateway/backend.py` are on **log lines** (a command string and a stderr
line).

This is observed behaviour, not a recorded decision — whether it is intended is
**Q2**, and the argument below assumes the default answer there.

So for one tool result, the server's `structuredContent` travels twice:

| Destination | Path | Redacted? |
|---|---|---|
| the model | gateway → `append_marker` → kiro-cli | **no** |
| the app | spool → `handle_tool_result` → owner WS → iframe | yes |

A credential in `structuredContent` therefore reaches the model regardless. The
scan on the app path removes it from the copy going to the party that
**authored** it, after the copy going to the model has already gone out intact.
It cannot be the control that stops a leak, because the leak it would have to
stop happened on the other path first — and the price it charges is the corrupted
image in §3.

### 5.2 The app's own HTML crosses unredacted beside it

In the same `mcp_app_render` frame that carries the three redacted payload fields,
`mcp_apps_render.handle_tool_result` sends:

```python
"html": data.get("html", ""),          # server-authored, NOT redacted
...
"structured_content": red_structured,  # redacted
"tool_input": red_input,               # redacted
"result_content": red_result,          # redacted
```

The document the iframe actually executes — authored by the same MCP server —
crosses with no redaction. A server that wants a credential inside its own app
does not need a compressed PNG; it can write one into `html`.

### 5.3 What follows

For **server-authored** fields, payload redaction is not a boundary. It is a
partial scan of bytes that already reached the model unscanned, delivered to the
party that wrote them, alongside an unscanned channel into the same iframe. That
is why every attempt to justify a media exemption by content inspection has
failed: the exemption is not what is unsound, the claim that these fields are a
defended boundary is.

§5.4 records that this is a matter of degree rather than a clean line: the server
holds `tool_input` raw as well, so no payload field is a boundary *against the
server*. What differs is provenance and the browser-side surface, and §7 words the
posture entry to that narrower claim.

Note also that `structured_content` never transits the model *on the app's path*:
the app receives it directly over the owner WebSocket frame. So redacting it does
not protect the model's context either — the model's copy comes from §5.1 and is
unredacted anyway.

The three fields are not alike, though, and that distinction is the proposal:

| Field | Authored by | Does the receiving server already hold it raw? | Does scanning the app's copy protect anyone? |
|---|---|---|---|
| `html` | the MCP server | it wrote it | not scanned at all today |
| `structured_content`, `result_content` | the MCP server | it wrote it, and the model also holds an unredacted copy via `append_marker` (§5.1) | no |
| `tool_input` | the **model** | **yes** — it is the `params.arguments` of the `tools/call` the server executed, forwarded unscrubbed (§5.4) | **no, against the server.** Only against the browser-side surface |

### 5.4 `tool_input` is different in origin, but not in exposure to the server

`tool_input` is the one field the receiving server did not **author**: it is
`_PendingRequest.tool_arguments`, captured from `params.arguments` in
`mcp_gateway/backend.py`, so its content came from the model's context and may carry
a credential the model picked up from another server, a file read or the user's
environment.

That is a real difference in provenance, and it is **not** a difference in what the
server can see. Those arguments are the ones the gateway forwarded so the tool could
execute, and nothing scrubs them on the way out: `secret_uri.py` resolves
`secret://` in environment values at spawn, `rewriter.py` rewrites agent JSON, and
neither touches call arguments. So the server already holds `tool_input` raw, and
redacting the render-frame copy does not withhold anything from the server or from
an app that server authored.

What the scan still does is keep the value out of the **browser-side** surface — the
iframe DOM, and anything with reach into the page. That is a narrower claim than
"the model-authored field is protected", and §7 states it that way so the posture
entry does not overstate it. It is also why this RFC keeps the scan rather than
dropping all three: it is the conservative side of a question this document does not
need to settle, recorded as Q6.

The defect in #9935 is in `structured_content`, which is where an app returning a
prefetched, inlined product image puts it. Inline media in `tool_input` has no known
use case — see Q4, which records what would change if that turns out to be wrong.

## 6. The decision this RFC asks for

Maintainers pick one. Both are coherent; the current state is neither.

This choice is the **entry condition for any implementation** (§8). It is not made by
merging this document, and not made by the defaults in §12: Option A removes a security
scan from two payload fields, so it needs a recorded "yes" rather than an absence of
"no". Until then the shipped behaviour stands, #9935 stays open, and apps that inline
media stay broken — a cost this RFC accepts in order to keep the decision explicit.

**Who records it, and where.** [GOVERNANCE.md](../../GOVERNANCE.md): maintainers decide,
in public, on the pull request the decision belongs to, and where they disagree a
majority decides. No separate forum, meeting or vote exists or is needed here. So the
answer is a comment on **this document's own PR** naming Option A or Option B, from any
reviewing maintainer, cross-posted to
[#9935](https://github.com/kirodotdev/KiroCrew/issues/9935) because that is where the
affected app authors are watching. When it lands, the answer goes into this document's
front matter — `status: accepted` plus the option chosen — so it survives after the
thread scrolls away.

**If nobody answers.** Governance sets no deadlines and this RFC invents none. But a
stall is not neutral, so the fallback deliberately needs no maintainer action: the
descope in §11 is an ordinary documentation change, falls outside GOVERNANCE.md's scope
test for needing an RFC at all, and can be proposed directly — close #9935 as "an MCP app
payload cannot carry inline base64 media; reference a `ui://` resource instead" and say
so in [mcp-apps](../system-specs/modules/mcp-apps.md). That is the worst of the three
outcomes for app authors and the only one that requires no decision, which is precisely
why this section asks for one.

### Option A — accept that server-authored content crosses unredacted, and say so

- Keep strict, media-unaware redaction on `tool_input`.
- Treat `structured_content` and `result_content` as the same trust class as
  `html`: server-authored content delivered to that server's own app.
- Media then needs no exemption and no inspection, because nothing claims to have
  inspected it. Delivery is exact by construction.
- The security posture entry says what is true: *the payload fields a server
  authors are delivered to that server's app unredacted, consistent with `html`;
  the model-authored `tool_input` is scanned.*

Honest cost: a credential that leaks into a server's *own* tool result — say a
server that reflects an upstream error containing a token — reaches that server's
app unredacted. Today it is redacted in `result_content` and simultaneously
un-redacted if the same server puts it in `html`, so this makes an existing hole
visible rather than opening a new one.

### Option B — close the boundary properly

- Redact `html` too, and keep redacting all three payload fields.
- Inline media stops being expressible, so media needs a path that does not exist
  today: a **separate** media resource the gateway fetches and serves as bytes. Note
  what this is not — the app's existing `ui://` resource is its static shell, and
  per-call media cannot live there (§11), so "just use a `ui://` resource" is not
  available as advice. This is a new mechanism to build, not a redirect to one.
- #9935 is then resolved by that mechanism plus a migration, not by an exemption —
  and not by documentation alone.

Honest cost: redacting `html` will corrupt app markup the same way it corrupts a
base64 body (an app's inline `<script>` with a long token-shaped constant is
indistinguishable from a secret), so this needs its own answer to the same false
positive one layer up. It also breaks every app that inlines media today, and it is
the largest of the three options because the media path has to be built first.

I recommend **Option A**. It is the smaller change, it makes an existing
inconsistency explicit and testable, and it puts the trust decision in the
posture registry where an operator can read it — rather than encoding it as a
content heuristic that cannot be right.

## 7. Design: what Option A changes

Nothing in `src/kiro_crew/security/`. The passes stay media-unaware, no
`data:`/`blob:` pattern is added, and no new facade entry point exists. Concretely:

1. `_redact_leaves` is applied to `tool_input` only. `structured_content` and
   `result_content` are delivered as the server produced them.
2. The `security_posture` redaction-sink row for `mcp_apps_render.py` states the
   split **and its limit**: that `tool_input` is scanned on the way to the browser,
   NOT that the value is withheld from the server, which already received it as the
   call's arguments (§5.4). An entry reading "the model-authored `tool_input` is
   scanned" without that qualifier would overstate the protection. The registry test
   that requires a partially covered sink to disclose it applies here.
3. `docs/system-specs/modules/mcp-apps.md` and
   `docs/system-specs/modules/security.md` record the provenance split, and give as
   the reason the two facts in §5 — that the model already holds an unredacted copy
   via `append_marker`, and that `html` crosses unredacted in the same frame.
4. Tests pin the asymmetry directly: a credential in `tool_input` is redacted; a
   credential-shaped media body in `structured_content` is delivered
   byte-identical; and a test asserts `html` and `structured_content` are treated
   alike, so a future change cannot silently start scanning one without the other.

## 8. Migration plan

Two phases, each independently shippable and independently abandonable. **Neither is
unblocked.** Phase 1 does not begin until §6 is explicitly answered by choosing Option
A; Phase 2 is additionally gated on Q1 and Q2 (§12).

**No default in §12 authorizes shipping a behaviour change.** The defaults exist so
the decision request is answerable rather than open-ended — they say what this document
assumes while it is being read, not what may proceed if nobody replies. Phase 1 removes
a security scan from two payload fields; that is precisely the change this RFC exists to
have decided out loud, so silence is not consent to it. A PR citing "Phase 1" without a
recorded answer to §6 should be closed on that ground alone.

If Q1 or Q2 is answered "that path is the bug", Phase 1 is not merely postponed but
wrong, and §6 is re-decided before anything ships.

### Phase 1 — the provenance split (blocked on §6: Option A chosen)

Implements §7. **Entry condition: a maintainer has answered §6 by selecting Option A**,
as a comment on this document's PR and reflected in its front matter (`status: accepted`)
— not inferred from a default and not inferred from this RFC being merged. Merging the
RFC records the *question and the analysis*; it does not select an option. §6 names who
records the answer and where.

The whole behaviour change is three lines in `handle_tool_result`, quoted here rather
than left on a branch so the diff can be read before the decision without depending on
an artifact that may not outlive it:

```python
# was: all three fields through _redact_leaves
red_input = await asyncio.to_thread(lambda: _redact_leaves(data.get("tool_input")))
red_structured = data.get("structured_content")
red_result = data.get("result_content")
```

The rest is the docstring stating why, the `security_posture` row, the spec update, and
the tests in E1.1–E1.5. No implementation PR is open, and none should be until the entry
condition above is met.

Exit criteria, each a testable assertion:

- E1.1 A `data:image/webp` body in `structured_content` is delivered
  byte-identical, and a test asserts it with an app whose `csp` declares
  `connectDomains` — proving the answer does not depend on the app's CSP.
- E1.2 A credential in `tool_input` is still redacted, asserted in the same test
  file.
- E1.3 `git diff main -- src/kiro_crew/security/` is **empty**. The passes gain no
  media awareness and the facade gains no entry point.
- E1.4 The `security_posture` sink row for `mcp_apps_render.py` states the split,
  and the registry test that requires a partially covered sink to disclose it
  passes against that row.
- E1.5 `docs/system-specs/modules/mcp-apps.md` records the split and cites this
  document.

### Phase 2 — close or confirm the two unredacted paths (blocked on Q1, Q2)

If maintainers answer Q2 with "the model-bound result should be redacted", that path is
fixed first and this RFC's §5.1 argument no longer holds; §6 is re-decided and Phase 1
is reverted if it shipped. If they answer Q1 with "unredacted `html` is a bug", Phase 1 is reverted and
Option B (§6) replaces it: `html` is redacted too, media moves to a `ui://`
resource, and existing apps that inline media get a migration note. If they answer
"intended", Phase 2 is instead a one-line posture note recording that, and the RFC
moves to `implemented`.

Exit criteria:

- E2.1 Either `html` passes through the redaction passes, or the posture entry
  states in one sentence why it does not.
- E2.3 The same for the model-bound result: either `append_marker`'s output is
  redacted, or the posture entry records in one sentence that it is not and why.
- E2.2 No third state: a test asserts `html` and `structured_content` are treated
  alike, so a later change cannot start scanning one without the other.

## 9. Backward compatibility

- **Wire format unchanged.** The `mcp_app_render` frame keeps the same fields and
  types; only the *content* of two of them changes (from scrubbed to as-produced).
  No frontend change is required, and no app manifest field is added or read.
- **Apps that inline media**: currently broken, become correct. This is the only
  behaviour change an app author can observe.
- **Apps that do not**: unaffected.
- **Spool records** are unchanged, so `SCHEMA_VERSION` does not move and records
  written by an older gateway stay readable.
- **Under Option B** compatibility breaks deliberately: an app inlining media stops
  rendering it and must move to a `ui://` resource. That is why Option B carries a
  migration note and Option A does not.

## 10. Security considerations

Stated so nobody reads more into it than it claims.

- It does not reduce what an app can send to its own server. The `tools/call`
  relay in §4.1 stays exactly as it is.
- It does not isolate apps. That is
  [`rfc-app-sandbox-isolation.md`](rfc-app-sandbox-isolation.md).
- It does not make the payload safe against a **malicious** MCP server. Nothing
  at this layer can: the server authors the app, its HTML, its CSP metadata and
  its `inputSchema`.
- It does not address the streaming surface, where a media URI split across chunk
  boundaries is scanned by a media-unaware `StreamRedactor`. That is the same
  defect class on a different surface and deserves its own change.

## 11. Alternatives considered

- **Exempt media after a content scan.** §4. Rejected: not decidable.
- **Serve media out of band over HTTP.** Extract media at spool time, store the
  bytes as a sidecar, and serve them from a new authenticated endpoint. Rejected
  as the primary design for two reasons: the iframe is a null-origin `srcdoc`
  document with no `allow-same-origin`, so `img-src 'self'` does not match the
  dashboard origin and the CSP would have to be widened for every app; and it
  does not change the trust question at all, since the bytes still reach the same
  app. It relocates the problem and adds an endpoint.
- **Remote media via `resourceDomains`.** An app can declare
  `csp.resourceDomains: ["https://cdn.example"]`, which `buildMcpAppCsp` folds into
  `img-src 'self' data: https://cdn.example` (the same list also widens
  `font-src`, `media-src`, `script-src` and `style-src` — it cannot be scoped to
  images), and serve `<img src="https://cdn.example/…">` instead of inlining.
  `sanitizeCspDomain` accepts only `https://[*.]host[:port]` with no path, and
  silently drops anything else, so a malformed entry fails closed to `'self'
  data:` with no error explaining why.

  **This does not solve the defect; it relocates it.** The exfiltration scan flags
  the payload, not the destination — `scan_exfiltration_urls`'s own contract is that
  "fixed credentials and the base64/length heuristics inspect the URL path+query
  regardless of host" — so declaring a domain buys no exemption from redaction. Measured against the passes on this tree, an ordinary CDN image URL
  can be corrupted exactly the way an inline body is:

  | Image URL shape | Result |
  |---|---|
  | unsigned, short query, e.g. `…/2026-09-03/<uuid>.png` | survives |
  | Cloudinary-style transforms in the **path** | survives |
  | content-addressed filename (base64url of a digest) | `…cdn.example.[REDACTED: credential].png` — the credential pass matches the base64 run and mangles host and path together |
  | imgix-style transform chain (public params only) | `[REDACTED: suspicious URL to cdn.example.net]` — crosses `_EXFIL_QUERY_MIN_LEN` on length alone |
  | signed URL (`X-Amz-Signature`, or CloudFront `Signature`+`Key-Pair-Id`) | replaced with the same exfil tag |

  So the surviving set is unsigned, short-query, non-content-addressed paths, which
  excludes private and expiring media outright, and an app author has no way to
  learn that boundary short of reading `exfil.py`. The only exemption is
  operator-level (`_exfil_exempt_hosts`, companion-supplied exact hosts, and it
  waives only the base64/length heuristics — fixed credential patterns still
  apply), so an app author cannot fix this for themselves. Rejected as a remedy: it
  spends a real sandbox property — a null-origin frame that issues no outbound
  subresource requests at all — on a defect it does not remove.

- **Descope — accept the broken image.** Recorded as the fallback if neither option
  in §6 is accepted, and the likely outcome if Q2 is answered "the model path is the
  bug". **Not cheap, and not neutral**, on two counts that its obvious phrasing
  hides. First, "use a `ui://` resource instead" has no landing place: that resource
  is the app's static shell, while media in the reported case is per-call data
  arriving over `ui/notifications/tool-input` / `tool-result`, so there is no in-spec
  home for it there. Second, the only in-spec alternative left is the
  `resourceDomains` path above — which means declining to fix this would push
  affected app authors into widening their own CSP, trading the null-origin,
  no-outbound-request property for browser-direct egress the gateway never sees, and
  still leaves them exposed to the same false positive. A descope should be chosen
  knowing it makes app authors weaken their own sandbox to work around a control
  that, per §5, defends nothing on the fields in question.
- **Narrow the credential matcher.** Rejected: reopens the chunking bypass.

## 12. Open questions

Each carries a default so the decision request is answerable rather than open-ended. A
default records what this document assumes while it is being read; **none of them
authorizes an implementation to proceed.** The §6 choice is what gates Phase 1, and it
has to be made explicitly (§8).

1. **Q1 — is the `html` asymmetry intended or an oversight?** §5.2 rests on it, and
   Phase 2 is blocked on it. Q1 and Q2 are the same kind of question — "is this
   existing unredacted path intended?" — and either one answered "bug" re-opens §6.
   The two are independent: even if `html` were redacted tomorrow,
   `structuredContent` would still reach the model unredacted through
   `append_marker` unless Q2 is also answered "bug". If maintainers consider
   unredacted `html` a bug, Option B becomes the consistent path for that field.
   Default if unanswered: treat it as intended, because it has been the shipped
   behaviour and redacting `html` corrupts app markup the same way it corrupts a
   base64 body.
2. **Q2 — is the unredacted model path intended, or is it the bug?** §5.1 treats it
   as behaviour: the gateway returns `append_marker(result, …)` to the agent with
   `structuredContent` copied through, and performs no redaction on that path. This
   RFC's central argument — and Option A with it — rests on that being **intended**.
   If maintainers answer "that path is the bug", the consistent action is to redact
   the model-bound result, and then scanning the app's copy stops being redundant, so
   §5.1 no longer supports Option A and §6 must be re-decided. Two things would not
   change: the corrupted image in §3 is still a defect, and §4 still rules out fixing
   it by content inspection — the remedy would become Option B or the descope in §11.
   Default if unanswered: treat it as intended, because it is the shipped behaviour on
   every MCP tool result, redacting the model-bound result would corrupt tool output
   the same way it corrupts a base64 body, and no issue tracks it.
3. **Q3 — does `structured_content` ever carry model-authored data?** Traced from
   `mcp_gateway/backend.py` as the server's own `structuredContent`, so it is
   server-authored, but an MCP server is free to echo its inputs into it.
   **Answered for at least one real app**: the app that reported #9935 puts its
   inlined product images in `structured_content`, and the app receives that field
   directly over the owner WebSocket frame rather than through the model. Even when
   a server does echo its inputs, §5.1 still applies — the echoed copy reached the
   model unredacted on the other path. Default: proceed.
4. **Q4 — should `tool_input` be exempt for media too?** The proposal says no, and
   the reporting app does not need it to: its images are in `structured_content`.
   If an app is later found to pass an image *into* a tool call, note that
   exempting media there needs its own argument rather than an extension of this
   one — an image the model passes in came from the model's context, which is
   precisely where a credential could have been picked up elsewhere, and neither
   §5.1 nor §5.2 covers that field. Default: keep `tool_input` strict.
5. **Q5 — streaming parity** (N4) — separate change, or a prerequisite? Default if
   unanswered: separate, because the batch path is what #9935 reports.
6. **Q6 — should `tool_input` be scanned at all, given §5.4?** The server already
   holds those arguments raw, so the scan withholds nothing from the server or from
   an app that server authored; its only remaining effect is keeping the value out of
   the browser-side surface. Three consistent answers: (a) keep it, as this RFC
   proposes, accepting that the benefit is narrower than "model-authored data is
   protected"; (b) drop it too, on the grounds that no payload field is a boundary
   against the server, which makes `_redact_leaves` dead code and the sink row an
   honest "not scanned"; (c) keep it and additionally scrub outbound `params.arguments`
   at the gateway, which is the only option that would actually withhold a
   model-held credential from the server — a much larger change, and not one this
   document proposes. Default if unanswered: **(a)**, because it is the conservative
   side and because (b) and (c) both change behaviour for every MCP server rather
   than for the render path this RFC is scoped to.

## Note on citation style

The "Writing a new RFC" section of [README.md](README.md) asks for `file:line`
citations. `scripts/docs_lint.py` **fails** on line citations in prose and asks for
a symbol name instead, on the grounds that a name survives the refactor that moves
a line. The gate is enforced, so this document cites symbols and names the commit
it was measured at (`5bfda4a12`). Worth reconciling the two.
