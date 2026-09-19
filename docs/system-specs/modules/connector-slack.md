# Connector: Slack actions core

## Overview

`kiro_crew/connections/vendors/slack/` is the **provider-side logic** of talking to the
Slack Web API correctly, isolated from the dispatcher and from any credential
handling. It holds five things:

1. **Per-method pagination** (`pagination.py`) — each method's scheme declared
   from its own reference, never assumed.
2. **Text / Block Kit business validation** (`validation.py`) — bounded by the
   shipped send-path split point, not the platform ceiling.
3. **The three-stage external upload protocol** (`upload.py`) — made explicit,
   with the conclusion on where `files_upload_v2` is not enough.
4. **Negative fault tests** keyed on Slack's own native error strings, whose
   verified spellings are recorded as data in `errors.py`.
5. **Error-string classification** (`error_mapping.py`) — maps each recorded
   Slack native string onto the RUN-01 taxonomy by consuming W01's
   control-plane `ErrorClass`, never forking a second vocabulary.

Everything here is pure logic: shapes in, shapes out. No IO, no credentials, no
socket, no dispatcher. The live inbound path (Socket Mode via
`slack.transport_dispatch` / `slack.events`) and the binding/authorization logic
in `slack/transport.py` are **untouched** by this slice.

## What this slice deliberately does NOT own

- **It does not own the RUN-01 taxonomy.** The campaign's single source of
  truth for connector error classification is the W01 control-plane slice
  (`kiro_crew.connections.control_plane`). This slice does not copy its enum and
  does not create any `connections/control_plane/**` file. Its
  `error_mapping.py` classifies Slack native error strings by **consuming** that
  taxonomy — importing W01's `ErrorClass` / `operation_error` (`it_d610cbb5`
  tracks that consuming relationship) — not by forking a second vocabulary.
- **It is not a second auth / governance / retry / envelope framework.** Retry
  policy classification already lives in `slack/retry.py`; this slice only
  *describes* recovery logic (e.g. that 429 recovery must not duplicate a send)
  and defines no retryability flag or classification of its own. It builds no new
  retry loop.
- **It does not own the `vendors` container anchor.** This slice lives under
  `kiro_crew/connections/vendors/slack/`. Its parent
  `kiro_crew/connections/vendors/` has no `__init__.py` in this slice on purpose
  — that container anchor and `connections`' public exports belong to the W01
  control-plane slice, which this slice neither creates nor modifies. Under
  the repo's test invocation (`pythonpath = src` in `setup.cfg`) the module
  resolves as a PEP 420 namespace subpackage and imports cleanly. With the anchor
  present, `setuptools`' `packages = find:` build discovers
  `kiro_crew.connections.vendors.slack`, so the wheel carries it — verified
  against `main`, which provides the anchor. This slice must not work around a
  missing anchor by creating one itself.

## Error classification — a mapping that consumes W01's taxonomy

This slice records Slack's own native error strings as data and classifies them
into the shared RUN-01 taxonomy by consuming it. `errors.py` records the
verified native strings (e.g. `invalid_cursor`, a pagination concept;
upload-stage error codes attributed in `upload.py`) — evidence-built frozensets
copied from the official method reference pages, grouped by the fault each
string names, with **no** classification enum of its own. `error_mapping.py`
then maps each recorded string onto a RUN-01 class by importing W01's
`kiro_crew.connections.control_plane` `ErrorClass` / `operation_error` /
`ERROR_CLASSES` — it CONSUMES that closed set rather than restating it, defines
no error-class enum of its own, and does not fork W01's taxonomy. `it_d610cbb5`
tracks this consuming relationship to the W01 control plane. An unrecognized
string degrades to the neutral `ambiguous` class rather than a wrong specific
one.

## `pagination.py` — three schemes, declared per method

Slack has no single pagination model. From the pagination guide
(https://api.slack.com/apis/pagination) and each method's reference:

| Scheme | In | Out | Done when | Methods |
|---|---|---|---|---|
| Cursor | `cursor`, `limit` | `response_metadata.next_cursor` | `next_cursor` empty/null/absent | `conversations.history`, `conversations.replies`, `conversations.list`, `conversations.members`, `files.info`, `reactions.list`, `users.list` |
| Legacy page/count | `page`, `count` | `paging: {count,total,page,pages}` | `page >= pages` | `files.list`, `search.messages`, `search.files`, `search.all` |
| None | — | whole set in one call | — | `usergroups.list` |

`search.messages` additionally offers a cursormark scheme (a `cursor` parameter
seeded with `*`); this slice does not use it and chooses legacy page/count.

Two load-bearing facts:

- **`conversations.history` and `conversations.replies` are two INDEPENDENT
  cursors.** Both are cursor-paginated, but a history cursor walks a channel's
  top-level messages and a replies cursor walks ONE thread; a cursor from one is
  meaningless to the other. `paginator_for` returns a fresh `CursorPaginator` per
  call and the two methods are distinct keys, so their state can never be shared.
- **`files.list` dual-doc ambiguity, resolved.** Older docs mention both a
  numeric `page`/`paging` scheme and a cursor for `files.*`. The **current**
  `files.list` reference (fetched 2026-09-15) offers ONLY `count` (default 100)
  and `page` (default 1) and returns a `paging` object with no `next_cursor`. The
  cursor-paginated file method is `files.info`, not `files.list`. Per the guide's
  own rule ("the individual documentation for each API method is your source of
  truth"), this slice takes **legacy page/count** for `files.list` and uses
  `paging.pages` as the terminal signal, because the reference exposes no cursor.

The only pagination-specific error is `invalid_cursor`.

## `validation.py` — bounded by the shipped send path

Text is validated against `SLACK_MSG_LIMIT` (3900), **imported** from
`slack/format.py` (the single source), which is where the send path splits an
outgoing message. This slice does **not** claim the platform `chat.postMessage`
~40000 `text` ceiling: a validator that advertised it would green-light a message
the renderer would never send whole. Over-limit text sets `needs_split` (a caller
splits with `format.split_message`); it is not a hard error, because long text is
normal.

Block Kit structural caps ARE hard errors (Slack rejects them as `invalid_blocks`
/ `invalid_blocks_format`): at most 50 blocks per message (100 per view surface),
`section`/`header` text caps, `context`/`actions` element caps, and a well-formed
`blocks` list of typed objects. Unknown block types are not rejected (Slack adds
types over time).

## `upload.py` — the three-stage external upload protocol

1. `files.getUploadURLExternal` (`length`, `filename` → `{upload_url, file_id}`).
2. A bare `POST` of bytes to `upload_url` (no `ok` envelope; HTTP 200 = success,
   non-200 = failure; processed async by a job handler).
3. `files.completeUploadExternal` (finalize; if never called the upload is
   aborted and the client gets an error).

`files.upload` (the single-call method) is **deprecated** and recorded only as
deprecated — never the current path.

`UploadSession` is the small state machine (`NEW → URL_ACQUIRED → BYTES_POSTED →
COMPLETED`, or `FAILED` with the failing stage). It refuses out-of-order
transitions and a **double-complete** — the stand-in for Slack's missing
idempotency key on `completeUploadExternal`. `stage_of_error` attributes a Slack
error string to the stage it arises in.

### `files_upload_v2` gap conclusion

Kiro Crew's current outbound upload (`slack/files.py` →
`SlackClientOps.upload_file` → `slack_sdk.files_upload_v2`) wraps all three
stages inside the SDK. That hides three things a caller needs: **per-stage error
attribution**, **`upload_url` expiry handling**, and **`completeUploadExternal`
idempotency**. This slice models all
three explicitly; it does not replace `files_upload_v2`, it is the model a caller
reasons against and the record of where the wrapper is not enough.

## Recovery / 429 (logic recorded, no new framework)

429 recovery must **not duplicate a send**. Slack has no idempotency key for
`chat.postMessage`, so a caller de-duplicates via its own correlation id (e.g. a
`metadata` field). Retryability and error classification remain outside this
slice; it does not build a retry loop — that reuses `slack/retry.py` and, when it
lands, the W01 retry surface.

## Evidence not yet live-verified

No live Slack workspace fixture was used for this slice; all behaviour is proven
by unit tests against recorded official API shapes. The following remain best-
recall until confirmed against a live workspace or a recorded fixture: the exact
`upload_url` TTL (Slack's method page does not document it — modeled as a POST-
stage failure state, not a hardcoded duration), Tier 1-4 per-minute limits,
per-message reaction caps, and group-DM member caps. Token-class facts to confirm
before any assertion: `search.messages|files|all` are user-token (xoxp) only;
`reminders.*` bot-token eligibility is undocumented (record user-token-only or
blocked, not both); native advanced/context search **entitlement is unknown and
must be recorded as blocked**, never substituted by `search.*` string search.
