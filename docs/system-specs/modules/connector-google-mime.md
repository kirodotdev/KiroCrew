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
| Inline images | `InlineImage`, `build_message` | An HTML `cid:<id>` reference is backed by an image part carrying `Content-ID: <<id>>` and `Content-Disposition: inline`, wrapped in `multipart/related`; with a text alternative present, the `alternative` nests inside the `related`. |
| Attachments | `Attachment`, `build_message`, `parse_message` | Raw bytes, filename (including non-ASCII via RFC 2231 `filename*=UTF-8''…`), and a SHA-256 checksum computed at construction; `verify(bytes)` re-checks extracted bytes. |
| Reply threading | `reply_headers`, `build_references_chain`, `add_reply_prefix` | `In-Reply-To` is the parent `Message-ID`; `References` appends it to the parent chain without duplication; `Subject` gets exactly one `Re:` (idempotent, count-form aware). |
| Recipients | `RecipientSet`, `Mailbox` | `To` / `Cc` / `Bcc` parsed and constructed. **`Bcc` never enters the serialized header block** — `RecipientSet.visible_header_pairs()` emits only `To`/`Cc`, and the Bcc addresses reach the transport solely through `envelope_recipients()`. This one choke point is the blind-copy-leak invariant. |
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
  unverified.
- **Oversize attachment** → `AttachmentTooLargeError`: a single attachment over
  its cap (checked at `Attachment` construction, before any base64 expansion)
  or an assembled message over `max_total_bytes`.

No silent data loss: a parsed `Bcc` header is surfaced on `ParsedMessage.bcc`
(not dropped), and an attached `message/rfc822` is captured whole as an
attachment with its own bytes and checksum rather than descended into (which
would lose the attachment and could let its nested body masquerade as the
parent's). `decode_raw` validates strictly — a non-ASCII or out-of-alphabet
character is a `MalformedMimeError`, never silently discarded.

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
