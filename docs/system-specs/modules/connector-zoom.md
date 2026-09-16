# Connector: Zoom contract semantics (W11-A)

The pure contract-semantics slice for the Zoom connector (`W11` in the
[connector-capability-manifest](connector-capability-manifest.md) DAG): the
identity, paging, processing-state, and error/redaction logic a later Zoom
adapter reuses, with **zero network, zero auth, zero dispatch**. It owns
`src/kiro_crew/connections/vendors/zoom/` and is deliberately a leaf of pure
functions and data — it constructs no HTTP client, no OAuth token, no meeting,
and no dispatch path. Auth, binding, dispatch, approval, and the ambiguous-write
decision belong to the shared control plane (`W01`,
`src/kiro_crew/connections/control_plane/`); this slice **consumes** that real
interface — folding its generic error taxonomy, error envelope, redaction
discipline, credential-mode axis, and opaque pagination cursor into
`control_plane` — and never builds a second runtime, copies the anchor, or
ghost-writes the shared `vendors/` container (W01's sole ownership).

### What is folded into the W01 control plane

The concerns that are generic across every provider stream live in
`control_plane` and this slice reuses them rather than re-deriving them:

- **Error taxonomy** — failures classify into the shared RUN-01
  `ErrorClass` (the twelve-value closed set `auth`/`scope`/`consent`/
  `not_found`/`forbidden`/`quota`/`throttle`/`conflict`/`input`/`temporary`/
  `partial`/`ambiguous`), not a Zoom-local enum. Zoom's contribution is the
  *mapping* from Zoom code/status to a RUN-01 class.
- **Typed error + redaction** — a failure is the shared `OperationError`, built
  through `operation_error()` so its `detail` is redacted-then-truncated by the
  site-wide discipline (`redacted_detail()`, capped at `MAX_ERROR_CHARS`).
- **Pagination** — a page's continuation is expressed as the opaque
  `OperationResult.next_cursor` (a token, or `None` when complete), never a raw
  vendor locator.
- **Credential axis** — Zoom's two auth modes are the shared `CredentialMode`
  values `oauth_user` and `service_to_service` (Zoom issues no
  `fine_grained_pat`), not a parallel Zoom enum; and an operation is described
  by the shared `OperationDescriptor`'s `service_id`/`operation_kind`/`effect`/
  `credential_mode`.

What stays **vendor logic** here is everything Zoom-specific: the UUID
double-encoding rule, the recurrence/occurrence and time semantics, the
per-endpoint pagination discipline, the AI Companion tri-state, the Zoom
code→RUN-01 mapping, and the Zoom-SHAPE credential detector/redactor (Zoom
signed `/rec/` media URLs and bare token fields the site-wide scanner does not
recognize — see the errors unit).

Scope boundary, stated up front so a reader with only this repo checked out can
place it: this document governs **what a Zoom identifier/cursor/state/error
MEANS and how it is classified** — the invariants a correct adapter must not
violate — and the tests that pin those invariants. It does not implement the
adapter, the manifest entries for Zoom (those are a later slice, `W11-B`, whose
manifest directory and validator ride a separate, not-yet-landed PR and are not
created here), or any live call. Where this slice's code changes what this
document states, the owning-spec rule applies exactly as everywhere else in this
tree: the doc is updated in the same commit as the code.

## Why pure logic is the right slice boundary

Every fact below is a property of the Zoom REST contract itself — how a UUID
must be encoded, which endpoints carry a cursor, what a summary's readiness
signal is, which error code is ambiguous — none of which needs a socket to state
or to test. Isolating them here means the adapter round (which does hold auth
and dispatch) inherits verified, unit-tested predicates instead of re-deriving
them inline against a live account, and a defect in the contract reading is
caught by a fast pure test rather than a live conformance run. The four units
are `identity`, `paging`, `processing`, and `errors` (redaction lives with
`errors` because a leaked credential is an error-path concern).

## The Meetings app is not this connector

Kiro Crew's built-in [Meetings app](meetings.md) transcribes a meeting through
Kiro Crew's **own** streaming speech-to-text and fans the transcript out to a
small crew of background agents that produce a local, model-generated summary.
Zoom's **AI Companion meeting summary** is a distinct, vendor-side product:
Zoom's own models generate it on Zoom's infrastructure, and this connector reads
it back through `meeting_summary` endpoints. **These two must never be
conflated.** The builtin app's locally-produced summary is never substituted for
Zoom's native AI Companion summary, and the AI Companion summary this connector
reads is never treated as, or fed into, the Meetings app's own STT+LLM pipeline.
They answer different questions ("what did Kiro Crew's agents make of the audio
we captured" versus "what did Zoom's AI Companion produce for the host"), have
different provenance and different entitlements, and a reader or a later adapter
that treats one as the other is producing a false record.

## Two auth modes, kept distinct from the target host

Zoom exposes two authentication modes, and this slice keeps them as first-class,
non-interchangeable concepts:

- **User-level OAuth** — the shared `CredentialMode` value `oauth_user`; a
  3-legged `authorization_code` grant tied to one human user; requires a browser
  redirect and consent; issues both an access token and a refresh token; the
  token's scope is that user's own Zoom permissions.
- **Server-to-Server OAuth** — the shared `CredentialMode` value
  `service_to_service`; a 2-legged `account_credentials` grant; the app
  authenticates with its own client id/secret plus an account id; issues **only
  an access token** (no refresh token — a fresh token is requested on expiry);
  the token represents an account/app-level service identity scoped to whatever
  the account admin pre-authorized for that app.

These are the shared W01 `CredentialMode` values, not a parallel Zoom-local
enum: the credential axis is the control plane's, and Zoom only declares WHICH
two of its three values apply (it has no `fine_grained_pat`).

Both modes issue bearer access tokens valid for 3600 seconds.

**Credential identity and target host are distinct concepts, and this slice
refuses to collapse them.** An account-level Server-to-Server credential does
**not**, by the fact of being account-level, prove it can (or cannot) target a
specified human host: reaching a specific host depends on the scopes the account
admin actually authorized for that app, which is a per-operation, per-account
fact, not an inference from the credential's mode. So an unverified
`(auth_mode, host)` combination stays **`unknown`** in either direction — it is
never asserted reachable because the credential is account-level, and never
asserted unreachable for the same reason. Every create-path operation therefore
**reads back `host_id` and asserts it equals the requested target**, because a
create that silently ran against a different host than intended is a correctness
failure the credential's mode alone cannot rule out. An earlier `W00`-derived
causal claim — that an account-level Server-to-Server credential settles host
reachability on its own — is **withdrawn**; it generalized from the credential's
name rather than from Zoom's own per-operation scope grants, and this document
supersedes it. Verification of a `(auth_mode, operation)` pair is done against
Zoom's own official scope documentation and the account/host reachability those
scopes actually grant, per operation, never assumed from the mode.

## Unit 1 — identity

A Zoom meeting or webinar is addressed by one of two identifier shapes, and the
adapter must pick the right one per endpoint:

- **Numeric `meetingId`** — the long-format meeting number. Used by the
  scheduled-object endpoints (`GET /meetings/{meetingId}`, its update, its
  recordings, its summary).
- **UUID** — a per-instance identifier. Each meeting/webinar instance generates
  its own UUID; a new one is minted for the next instance of a recurring series.

### The UUID double-encoding rule

A meeting/webinar UUID that **begins with `/`** or **contains `//`** must be
**double URL-encoded** before it is placed in a path segment: percent-encode the
raw UUID once, then percent-encode that already-encoded string again. A single
encoding pass is insufficient and Zoom returns a "does not exist"-class response
(error code `3001`). This affects every UUID-path endpoint — `past_meetings`,
`past_webinars`, recordings, transcript/archive, and AI Companion reads.

The load-bearing consequence, which the `errors` unit also carries: **an
un-double-encoded UUID producing `3001` is a FALSE ABSENCE**, not a real "meeting
does not exist". The identity unit exposes a predicate that, given a raw UUID,
decides whether double-encoding is required (the `/`-prefix / `//`-contains
test), so the adapter encodes correctly and so a `3001` on a UUID that needed
double-encoding but did not get it is distinguishable from a genuine absence.
Confirmed against Zoom's official Meetings API reference (the `uuid` field notes
on `GET /meetings/{meetingId}` and `GET /past_meetings/{meetingId}` both state
the double-encode rule).

### Recurrence, occurrences, and time semantics

- `GET /past_meetings/{meetingId}/instances` is keyed by the recurring series'
  **numeric id**, not by an occurrence UUID. Passing an occurrence UUID where the
  series id is expected is a distinct error from a genuine absence, and the
  identity unit does not conflate the two identifier roles.
- The presence or absence of `occurrence_id` distinguishes a **single
  occurrence** of a recurring series from the **parent series**: a recurrence
  update carrying an `occurrence_id` targets that one occurrence, while the same
  update **missing** `occurrence_id` targets the parent series (and therefore the
  whole series). Dropping `occurrence_id` on what was meant to be a
  single-occurrence update silently retargets the parent series — a negative-fault case
  the tests pin.
- Occurrence time is **per-occurrence `start_time` combined with the series'
  `timezone`**. The identity unit performs **no local-time inference**: it never
  guesses a wall-clock time from a bare timestamp, and it never drops the series
  timezone in favor of the host's or the reader's local zone. A `start_time`
  without its governing `timezone` is carried as-is, not resolved to an instant
  in some assumed zone.

## Unit 2 — paging

Zoom's pagination is **not uniform across endpoints**, and this unit encodes the
per-endpoint contract rather than assuming one:

- **Cursor endpoints** use `next_page_token` + `page_size`: request with a
  `page_size`, and if more results exist the response carries a `next_page_token`
  to pass on the next request; the token is absent on the final page. This is the
  list/search shape (`GET /users/{userId}/meetings`, `.../webinars`,
  `past_meetings/.../participants`, `contacts`, `GET /devices`, etc.). Confirmed
  against the official API reference's pagination section and each endpoint's own
  `next_page_token`/`page_size` fields.
- **`recordings.list` is additionally windowed** by `from`/`to` date parameters
  on top of the cursor: paging it correctly means holding the date window fixed
  across every `next_page_token` request (Zoom resets a future `to` to the
  current time and expects the same `to` on subsequent pages, or returns an
  invalid-token error). The paging unit treats the `(from, to)` window as part of
  the cursor's stable state, not as an independent knob to change mid-iteration.
- **Cursor-less endpoints are declared cursor-less, never assumed paged.** The
  instances list (`GET /past_meetings/{meetingId}/instances`) and single-resource
  GETs (`GET /meetings/{meetingId}`, a single summary read) return their whole
  result in one response and carry no `next_page_token`. Assuming a cursor on one
  of these — looping until a token that never appears, or treating the absent
  token as "more pages pending" — is a defect the tests pin: the unit answers
  "this endpoint is not cursor-paged" for them.
- **`report/daily` iterates month-by-month, not by cursor.** `GET /report/daily`
  is parameterized by `month`/`year` and reports one month's daily rows;
  covering a range means iterating the months, not following a `next_page_token`.
  Confirmed against the official Reports API reference (the daily report's
  month/year parameters and its "recent 6 months" bound). The paging unit
  classifies `report/daily` as month-iterated and does not look for a cursor on
  it.

The paging unit's job is classification and cursor-state carriage: given an
endpoint identity, it says which pagination discipline applies (cursor,
date-windowed cursor, cursor-less, or month-iterated) and, for the cursor
shapes, carries the token and any window that must stay fixed. It performs no
request; the adapter drives the actual calls.

## Unit 3 — processing (tri-state)

An AI Companion summary read is **tri-state**, never two-state and never a
200-empty fake success:

- **`completed`** — the summary exists and is ready to read.
- **`pending`** — the summary has been requested/started but is not ready yet.
  The readiness signal is the webhook `meeting.summary_completed`; until that
  fires (or the read's own status field reports completion), a read must surface
  the **pending** state and must **not** fabricate a placeholder summary or
  return an empty body dressed as success.
- **`not_entitled`** — the account/user lacks the entitlement the operation
  needs: AI Companion (for the summary), Cloud Recording (for recordings), or the
  Webinar add-on (for webinar-family reads). This is distinct from both
  `completed` and `pending`: it is a permanent-until-entitlement-changes "this
  feature is not enabled here", not a transient "not ready yet" and not a real
  "no data". Zoom signals it with a forbidden/feature-not-enabled response (e.g.
  the AI Companion `2314` "enable the Meeting summary with AI Companion setting"
  forbidden, or a plan-missing `200`-coded error), never by silently returning an
  empty-but-successful body.

Collapsing these three into two — treating `not_entitled` as `pending`, or
`pending` as `completed`-with-no-data — loses the exact information a caller
needs to decide whether to wait, to prompt for an entitlement, or to report a
real absence. The processing unit keeps all three distinct and maps a read's
observed shape to exactly one of them.

### AI Companion summary entitlement (verified)

The AI Companion meeting summary read
(`GET /meetings/{meetingId}/meeting_summary` and the list variants) requires,
per Zoom's official Meetings API reference, a host on a **Pro, Business, or
higher** subscription plan with the **Meeting Summary with AI Companion** user
setting enabled (the Webinar variant needs the Webinar Summary setting), and
End-to-End Encrypted meetings do not support summaries. The base AI Companion
summary is therefore **not** a Basic/free-tier feature. This resolves the `W00`
"minimum license tier unknown" gap to an official-baseline fact: the minimum
tier is a paid licensed plan (Pro or above), not free. **Summary templates**
(`summary_template_id`, the "Get user summary templates" API) are a further,
separately-gated capability of **Custom AI Companion** (a paid add-on), distinct
from the base summary tier. A broader "AI meeting templates" REST surface beyond
summary templates is **not** confirmed as a distinct public API and stays
`unknown` / `not_yet_sourced` — it is neither invented nor deleted here.

## Unit 4 — errors and redaction

### Error classification

- **Code `3001` is ambiguous** between a genuine "meeting/webinar does not exist"
  and an un-double-encoded UUID that Zoom could not resolve (see the identity
  unit). The errors unit maps Zoom's `3001` to the RUN-01 `ambiguous` class
  (never to `not_found`), so a caller can decide to re-issue the request with a
  correctly double-encoded UUID before concluding the resource is absent —
  rather than reading every `3001` as a definitive absence.
- Every other Zoom code/status is mapped into the shared RUN-01 `ErrorClass`
  (`auth`/`forbidden`/`not_found`/`throttle`/`input`/`temporary`/…) so the
  adapter and governance layers switch on the one campaign-wide taxonomy rather
  than parse free text. The mapping is data-driven (a Zoom-code→RUN-01 table),
  so adding a newly-observed Zoom code is a data change, not a control-flow edit;
  an unclassified failure resolves to `temporary` (the safe retryable default)
  rather than being guessed into a more specific class. A typed failure is the
  shared `OperationError`, built through the control plane's `operation_error()`.

### Redaction (a leaked credential is never logged)

Two credential shapes cross this connector and **must never reach a log line, an
error string, a metric, or any trace**:

- **Signed media URLs** — `download_url` and `play_url` values embed short-lived
  signed access tokens as query parameters.
- **OAuth bearer credentials** — access tokens (both modes) and refresh tokens
  (user-OAuth mode).

Redaction of an error `detail` is **two composed passes**, and the split is
load-bearing. The site-wide discipline the control plane applies
(`redacted_detail()` → `security.redact_and_truncate`) catches `Bearer` tokens
but, verified here, does **not** recognize a Zoom signed `/rec/` media URL or a
bare `access_token`/`refresh_token` field — so folding into the control plane
alone would leak those Zoom shapes. The Zoom vendor pass (`redact_zoom_secrets`)
therefore scrubs the Zoom-SHAPE credentials FIRST, and `zoom_operation_error()`
then hands the result to `operation_error()` for the site-wide pass; the two
together leave no credential behind. A Zoom-shape **detector**
(`contains_zoom_credential`) exists alongside, used by the tests to PROVE no
Zoom credential shape survives — it is a detector, not a second redaction
policy. Both the detector and the vendor redactor are self-contained pure logic,
unit-testable without a socket.

**Named follow-up (a later slice, not this one).** The OAuth `access_token` /
`refresh_token` FIELD-shape patterns `redact_zoom_secrets` scrubs are
OAuth-generic, not Zoom-specific — only the signed `/rec/` media URL shape is
genuinely Zoom's. Carrying the generic field-shape patterns vendor-side means
each later connector slice (`W12`+) would re-implement them or leak token fields
past the site-wide pass. The steady end state is that those generic patterns
live in the control plane's site-wide scanner (a small, separately-scoped
follow-up under W01's ownership of `control_plane`), leaving only the Zoom-shape
`/rec/` URL redaction here. This slice does not make that change — it does not
ghost-write the shared module — but names it so the direction is on record.

## Negative fault tests (all six)

The test suite pins six negative-path behaviors, one per real failure mode this
contract has:

1. **Un-double-encoded UUID** — a UUID starting with `/` (or containing `//`)
   sent without double-encoding maps to the ambiguous-`3001` class, not to a
   genuine absence.
2. **Recurrence update missing `occurrence_id`** — an update meant for a single
   occurrence but missing `occurrence_id` is recognized as hitting the parent series
   series, not silently accepted as the intended single-occurrence edit.
3. **Cursor assumed on a cursor-less endpoint** — asking the paging unit to
   follow a cursor on the instances list or a single-resource GET is refused: the
   unit reports the endpoint is not cursor-paged rather than looping for a token
   that never comes.
4. **Pending-summary read** — reading an AI Companion summary whose status is
   pending surfaces the `pending` state and does not fabricate a placeholder.
5. **Unentitled webinar/recording read** — a read against an account lacking the
   Webinar add-on / Cloud Recording / AI Companion entitlement maps to
   `not_entitled`, distinct from `pending` and from a real empty result.
6. **Leaked token or signed URL** — a token or a signed `download_url`/`play_url`
   placed into an error/log string is caught by the redaction predicate and does
   not survive.

## Evidence provenance

The contract facts above are drawn from Zoom's own official developer
documentation (the `developers.zoom.us` Meetings and Reports API references and
Zoom support articles on AI Companion licensing), verified pointwise for the
four items `W00` had left unresolved: the recording-archive REST surface, the AI
Companion summary minimum license tier, the reports/devices family shape, and AI
meeting templates. Community forum posts were treated as untrusted and were not
used to assert any endpoint. Where an item could not be confirmed against an
official rendered page it is recorded as `unknown` / `not_yet_sourced` rather
than guessed; no requirement is deleted for lack of a source, and no other
provider was rescanned. Reusable `W00` evidence (the campaign's Zoom operation
catalog, `auth_facts`, and per-operation readback assertions) is referenced
rather than re-derived, and its `unknown`-status entries are treated as unknown,
not as vendor-proven.
