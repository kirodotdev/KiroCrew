# Connector: Google Gmail MIME engine (G1)

The Gmail-side MIME parsing and construction engine for the connector
campaign's Google stream (`W03` in the
[connector-capability-manifest](connector-capability-manifest.md) DAG). The
subsystem is `src/kiro_crew/connections/vendors/gmail/` (`errors.py`,
`addresses.py`, `raw.py`, `attachments.py`, `threading.py`, `builder.py`,
`parser.py`), re-exported from that package's `__init__.py`. The
`connections/vendors/__init__.py` container anchor it lives under is minted by
`W01`'s control-plane seam (not by this slice), because `setup.cfg` builds with
`packages = find:` and would otherwise drop the whole `vendors/` subtree from
the wheel.

## Scope, and the boundary it does not cross

This engine is **pure business logic with zero auth dependency.** It parses and
constructs RFC 5322 / MIME messages and the base64url `raw` form Gmail
`users.messages.send` uses, operating only on bytes and data a caller supplies.
It has NO dependency on the shared control plane (`src/kiro_crew/connections/**`
— binding, auth, policy, reliability) that `W01` owns, mints or holds no
credential, and makes no live Gmail call anywhere. Real authorization is wired
against `W01`'s named interfaces in a later slice; this engine round-trips
bytes, not tokens, so its format logic is unit-testable in isolation from any
live account.

This separation is deliberate and load-bearing: the same reason
[connector-capability-manifest.md](connector-capability-manifest.md) keeps the
vendor's format logic out of `connections/**`. The Google auth layer Gmail and
Drive both authenticate through is `W03`'s shared responsibility (the
`W03 → W04` edge in that spec's DAG), not this engine's.

## What the engine does

| Concern | Entry point | Notes |
|---|---|---|
| Header + body encoding | `build_message`, `parse_message` | Non-ASCII (e.g. Chinese) `Subject` and display names are emitted as RFC 2047 encoded-words and decoded back; body text declares `charset="utf-8"` so it round-trips exactly. |
| `multipart/alternative` | `build_message` | Text + HTML together produce `multipart/alternative`; text-only or HTML-only stays a single leaf. |
| Inline images | `InlineImage`, `build_message` | An HTML `cid:<id>` reference is backed by an image part carrying `Content-ID: <<id>>` and `Content-Disposition: inline`, wrapped in `multipart/related`; with a text alternative present, the `related` (holding the HTML + its inline images) nests inside the top-level `multipart/alternative` alongside the `text/plain` leaf. |
| Attachments | `Attachment`, `build_message`, `parse_message` | Raw bytes, filename (including non-ASCII via RFC 2231 `filename*=UTF-8''…`), and a SHA-256 checksum computed at construction; `verify(bytes)` re-checks extracted bytes. |
| Reply threading | `reply_headers`, `build_references_chain`, `add_reply_prefix` | `In-Reply-To` is the parent `Message-ID`; `References` appends it to the parent chain without duplication; `Subject` gets exactly one `Re:` (idempotent, count-form aware). |
| Recipients | `RecipientSet`, `Mailbox` | `To` / `Cc` / `Bcc` parsed and constructed. All three — `Bcc` included — are written into the built `raw` message, because Gmail's [`users.messages.send`](https://developers.google.com/gmail/api/reference/rest/v1/users.messages/send) accepts only a `raw` field and "sends the specified message to the recipients in the `To`, `Cc`, and `Bcc` headers." There is no separate envelope on this transport, so a `Bcc` omitted from `raw` is a blind recipient who silently never receives the mail; Gmail strips the `Bcc` header from the copies it delivers, providing blind-copy privacy. |
| `sendAs` alias validation | `validate_send_as`, `SendAsAlias` | Pure gate: a requested `From` must match a verified alias in the account's declared `sendAs` set, or `InvalidAliasError` is raised. Validation only — the alias set is data the caller supplies; no `sendAs.list` call is made here. |
| `raw` wire form | `encode_raw`, `decode_raw`, `BuiltMessage.raw()` | base64url (RFC 4648 §5), emitted unpadded (as Gmail's own libraries do), decoded tolerantly. `encode_raw`/`decode_raw` round-trip exactly. |

## Negative paths are part of the contract

The engine refuses bad input rather than emitting a broken message or silently
dropping data. Each failure mode is a distinct `MimeError` subclass so callers
switch on type:

- **Malformed MIME** → `MalformedMimeError`: empty bytes, a `multipart`
  Content-Type whose boundary delimiter never appears (detected via the email
  parser's `StartBoundaryNotFoundDefect` /
  `MultipartInvariantViolationDefect` / `CloseBoundaryNotFoundDefect`, promoted
  to an exception rather than left as a collapsed body), an invalid base64
  Content-Transfer-Encoding (the stdlib appends an `InvalidBase64*Defect` only
  AFTER a part is decoded, so each part's defects are re-checked post-decode,
  not only at parse time), a body whose bytes are invalid for its declared
  charset (decoded STRICTLY, never silently `errors="replace"`-normalized into
  corruption carrying a checksum over that corruption), and an unparseable
  message.
- **Missing required header** → `MissingHeaderError`: a valid MIME message that
  lacks a header a particular caller requires (`require_headers`), and the
  construction-side refusal of a message with no sender, no recipient, or no
  body. Kept distinct from `MalformedMimeError`: valid MIME can still be
  missing what a caller needs.
- **Invalid / unverified alias** → `InvalidAliasError`: a `From` that is
  syntactically invalid, absent from the `sendAs` set, or present but
  unverified. `SendAsAlias.is_verified` defaults to **`False`** (fail-closed):
  a sender-identity gate treats an alias as unverified until the caller proves
  otherwise, so a future wiring author who maps `sendAs.list` and omits the
  verification flag gets a rejected alias, never a silently-accepted
  unverified sender. The gate requires `is_verified` to be the boolean `True`
  (identity, not truthiness), so a mis-populated truthy non-bool (e.g. the
  string `"false"`) cannot slip through.
- **Oversize attachment** → `AttachmentTooLargeError`: a single attachment over
  its cap (checked at `Attachment` construction, before any base64 expansion;
  for a `message/*` attachment the cap is RE-checked after CRLF normalization,
  which can grow the byte count, so the normalized form cannot slip over-cap)
  or an assembled message over `max_total_bytes`.

No silent data loss: a parsed `Bcc` header is surfaced on `ParsedMessage.bcc`
(not dropped), and an attached `message/rfc822` is captured whole as an
attachment with its own bytes and checksum rather than descended into (which
would lose the attachment and could let its nested body masquerade as the
parent's). `decode_raw` validates strictly — a non-ASCII or out-of-alphabet
character is a `MalformedMimeError`, never silently discarded. Repeated
`To`/`Cc`/`Bcc` headers are combined across **every** occurrence (via
`get_all`), so a non-conformant producer's later recipients are never dropped.
A single-value header carrying an RFC 2047 encoded-word whose bytes are
undecodable in its declared charset is a `MalformedMimeError` — the header path
matches the strict body-decode contract, never returning U+FFFD corruption.

Note on `Bcc` and the base64url `raw` wire form: `Bcc` IS written into the
built message, so `BuiltMessage.raw()` carries a `Bcc` header when the send has
blind recipients. This is not a leak but the delivery mechanism on this
transport: Gmail's [`users.messages.send`](https://developers.google.com/gmail/api/reference/rest/v1/users.messages/send)
accepts only a `raw` field and "sends the specified message to the recipients
in the `To`, `Cc`, and `Bcc` headers" — there is no separate envelope /
`RCPT TO` list the SMTP world uses, so a `Bcc` stripped from `raw` is a blind
recipient who silently never receives the mail (the exact silent-data-loss
class this engine exists to close, moved to the construction side). Gmail then
strips the `Bcc` header from the copies it delivers, so blind-copy privacy is
enforced by the provider on a header that MUST be present in `raw` for those
recipients to receive anything. Because the recipients ARE the To/Cc/Bcc
headers in `raw`, there is no separate `envelope_recipients()` list — a method
returning a synthetic union would describe a delivery channel that does not
exist on `messages.send`, so it is deliberately absent. A **Bcc-only** message
serializes with a `Bcc` header and no `To`/`Cc`; no `To: undisclosed-recipients:;`
is synthesized, because a bare `Bcc` header is already a valid RFC 5322
destination Gmail delivers from directly (synthesizing a `To` would add a
visible header the sender did not ask for). `require_headers(["Bcc"])` consults
`parsed.bcc` (an inbound message being parsed may legitimately carry a Bcc
header, which lands on `parsed.bcc`, not `parsed.headers`).

Further no-silent-corruption guarantees: EVERY `text/plain` and `text/html`
leaf is decode-validated, not only the first adopted as the body — a later
leaf's invalid base64 CTE (whose defect is appended only on decode) is caught
rather than silently skipped, and a SECOND `text/plain`/`text/html` body leaf
outside a `multipart/alternative` is captured as an attachment rather than
silently dropped after the first. An image explicitly `Content-Disposition:
attachment` is kept an attachment even when it also carries a `Content-ID`
(common in forwarded mail), so it never vanishes into `inline_images`; whereas a
named image carrying a `Content-ID` but NO disposition stays inline (a `cid:`
referent the HTML body points at), not misclassified as an attachment because it
has a `name=`. A parsed filename's strict encoded/extended-parameter check runs
for EVERY present filename parameter — including an RFC 2047 encoded-word that
decodes to empty (an erased/invalid word carries no U+FFFD to trip a
presence-gated check), so an erased filename is rejected rather than silently
accepted, and EVERY encoded-word within the value is strict-decoded even when
it is mixed with literal text (a malformed word embedded among plain text is
rejected, not silently dropped). It reads the parameter `get_filename` actually
selected (Content-Disposition `filename` first, Content-Type `name` only when
absent), so the check inspects the same value the decoded name came from. The
embedded-`message/rfc822` serializer uses `maxheaderlen=0`, so a forwarded
message's long headers are not refolded and its attachment bytes/checksum stay
stable; and when the `message/rfc822` (or `message/global`) part itself declares
`Content-Transfer-Encoding: base64`, the encoded body is DECODED back to the
forwarded `.eml` bytes rather than captured as opaque base64 text — and that
decode is STRICT: out-of-alphabet or truncated base64 raises
`MalformedMimeError` rather than stamping a valid checksum over corrupted
bytes, while standard MIME line-wrapping (CRLF/space/tab between base64 groups)
is stripped first so a legitimately line-wrapped forwarded message is accepted.
An attachment-dispositioned multipart that is
NOT `message/rfc822` (e.g. `multipart/appledouble`) is serialized WHOLE — every
child survives, not just the first. Any inline, non-image, non-text leaf (e.g.
an inline `application/pdf`)
is captured as an attachment rather than dropped for matching no body branch.
A leaf carrying a FILENAME (`Content-Type name=` or `Content-Disposition
filename=`) is treated as an attachment unless explicitly `inline` — so a
`text/plain; name="notes.txt"` with no disposition is captured with its bytes
and filename instead of silently vanishing into the body branch.
Recipient display names are decoded from the structured address parse so BOTH
RFC 2047 encoded-words AND raw UTF-8 (SMTPUTF8) names decode to their real
characters (never replacement characters), and a name folded across multiple
encoded-words is joined with no stray whitespace (RFC 2047 §6.2). A valid RFC
5322 quoted local part (`"John Doe"@example.com`) is accepted, not silently
dropped, while a bare comma in an UNQUOTED local part is rejected (it is an
address-list separator that would split the header). An RFC 5321
domain-literal recipient (`user@[192.168.0.1]`, `user@[IPv6:2001:db8::1]`) —
legal and producible by inbound mail — is accepted, not dropped by a
dotted-domain-only check. A CR/LF anywhere in an address OR a display name is
rejected at `Mailbox` construction (the classic header-injection class), rather
than passing validation and crashing later at header serialization with an
uncaught `ValueError`. A `message/*` attachment (`message/rfc822`,
`message/delivery-status`) is attached as a PARSED message rather than raw
bytes, so serialization does not crash; a non-rfc822 `message/*` subtype keeps
its declared media type instead of being silently relabeled `message/rfc822`
(which `add_attachment` of a `Message` object would otherwise emit), and the
media-type dispatch is case-insensitive so an uppercase `MESSAGE/delivery-status`
is not routed to the raw-bytes path that crashes. The embedded message is parsed
under compat32 so its bytes reach the wire without the default policy's header
refolding, and a `message/*` attachment is CRLF-canonicalized end to end: its
bytes are normalized to CRLF (the RFC 5322 wire form) at `Attachment`
construction, so the stored checksum, the serialized wire form, and the
extracted payload all agree — the checksum survives the build→parse round-trip
whether the source arrived LF-only or CRLF, instead of preserving arbitrary
source line endings verbatim through a library that normalizes them. A CR/LF in an attachment filename, or
a malformed media type (not a single `maintype/subtype` of valid MIME-token
characters — a stray delimiter on either side of `/` is refused, not silently
discarded), is refused at
`Attachment` construction with a typed `MalformedMimeError` rather than crashing
`add_attachment`; `InlineImage` enforces the same CR/LF and MIME-token guard on
its `content_id` / `content_type` / `filename` and requires a well-formed
`image/*` type. Duplicate inline-image `Content-ID`s are refused (an ambiguous
`cid:` reference; the filename update targets the just-appended related part,
never a first-match `Content-ID` search). On the parse side, a
non-rfc822 `message/*` attachment is captured as its EMBEDDED payload, not the
whole container — folding the container's own `Content-Type` / `Content-Disposition`
wrapper headers into the captured bytes would put the checksum over the wrapper
rather than the payload. A repeated recipient address carrying DIFFERENT display
names keeps each name (matched by header occurrence, not by an addr-spec key
that would give every later occurrence the first name). A local part the cheap
addr-spec regex admits but the stdlib `Address` rejects (e.g. a consecutive-dot
`a..b@example.com`) is refused at `Mailbox` construction — the `Address` the
builder later constructs is validated up front, so its `InvalidHeaderDefect`
surfaces as `InvalidAliasError` instead of an uncaught crash at serialization.
A CR/LF in the single-value construction inputs (sender, subject, In-Reply-To /
References / Message-ID) — which bypass the `Mailbox` gate — is refused as a
typed `MissingHeaderError` before assembly, rather than reaching the header
setter and crashing with an uncaught `ValueError` (the header-injection class
the `Mailbox` gate closes for recipients). The sender is additionally validated
as a real addr-spec (via `Mailbox`) and a caller-supplied `Message-ID` must be a
single RFC 5322 msg-id, both translated to `MissingHeaderError` on failure so an
unvalidated structured header cannot crash or desync the emitted output. On the
parse side, a malformed structured single-value header (whose typed access or
rendering would raise) is translated to `MalformedMimeError` rather than
escaping the parse as a raw exception. `Attachment` and `InlineImage` store the
NORMALIZED (stripped) media type, not the raw padded input.
An oversized inbound attachment
surfaces as `AttachmentTooLargeError`, not rewritten to `MalformedMimeError` by
a broad handler. A parsed attachment
filename is decoded strictly — an RFC 2231 `filename*=UTF-8''…` whose
percent-encoded bytes are undecodable in the declared charset is rejected as
decode-introduced corruption (checked against the ORIGINAL undecoded parameter,
across EVERY continuation segment `filename*0*` / `filename*1*` / …, since the
default policy's own accessor also shows U+FFFD), an RFC 2047 encoded-word
filename (`filename="=?utf-8?b?…?="`) is likewise strict-decoded so a malformed
encoded-word is rejected rather than returned as U+FFFD, while a filename that
VALIDLY encodes a literal U+FFFD is kept; a NUL byte is always rejected.
A raw non-encoded-word 8-bit header byte (e.g. `Subject: \xff`) that the default
policy decodes to U+FFFD is likewise rejected unless its bytes are valid UTF-8
(a raw SMTPUTF8 header is kept), so no header path returns replacement-character
corruption. An `InlineImage` must carry an `image/*` content type:
a non-image inline part round-trips into body text and vanishes from
`inline_images`, so it is rejected at construction. Display names are decoded
strictly too — a To/Cc/Bcc name carrying an
undecodable RFC 2047 encoded-word is a `MalformedMimeError`, never silently
replaced with U+FFFD. Strictness also covers **erasure**: an encoded-word with
invalid base64/QP decodes to empty bytes without raising, silently dropping
content. Both the single-value-header and display-name paths reject any
encoded-word chunk that decoded to empty — including when it is MIXED with
otherwise-valid text (`Hello =?utf-8?b?!!!!?=` is rejected even though `Hello `
survives), so a whole-string emptiness test cannot let a partial erasure
through. Legitimately-encoded non-ASCII (e.g. a valid Chinese encoded-word)
decodes normally.

## What this engine deliberately does not contain

- No Gmail API call, no OAuth, no token handling — auth is `W01`/`W03`'s, wired
  later against their named interfaces.
- No Drive logic — that is `W04`, in its own sibling subpackage under
  `src/kiro_crew/connections/vendors/` (e.g. `vendors/google_drive/`).
- No manifest entry, `ConformanceRun`, or `EvidenceReceipt` — those are
  populated by the campaign's later validator/runner rounds
  ([connector-capability-manifest.md](connector-capability-manifest.md)); this
  engine is the pure adapter logic such an entry's `adapter.module_ref` would
  eventually point at.
