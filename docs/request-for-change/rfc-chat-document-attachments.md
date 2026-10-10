---
title: Dashboard chat document attachments — a text sidecar the agent can read
status: draft
author: kamleshnanda
created: 2026-10-09
last-audited: 2026-10-09
audited-at: 8c6e7eba90
doc-pr: null              # filled when the doc PR opens
implementation-prs: []
tracking-issues: [14570]
supersedes: []
superseded-by: []
---

# RFC: Dashboard Chat Document Attachments — a Text Sidecar the Agent Can Read

> **Status:** `draft`. Acceptance is requested from a maintainer; the status
> flips to `accepted` when one records it in §11 with the date. Nothing is on
> main. Every claim below was verified at `decba819400e` (main, 2026-10-09)
> and re-read at `8c6e7eba90` the same day, where none of the cited files had
> changed; `file:line` anchors refer to those commits. This document lands first because
> it changes what every dashboard prompt carrying a `.pdf`/`.docx`/`.pptx`
> attachment says to the agent, and the First Principles lane reads that
> decision from the base branch.

## 1. Summary

When a dashboard user attaches a `.pdf`, `.docx` or `.pptx`, the upload
handler extracts its text once, at upload time, into a redacted sidecar file
`<upload>.txt` beside the upload. At prompt-build time the ACP prompt builder
appends `[document text: <sidecar path>, N pages]` (the count only where the
format defines one) after the attachment path,
in the outgoing prompt only. The agent then reads the sidecar with its
built-in `read` tool: no shell call, no approval prompt, no subagent detour.

Eight chat channels already convert documents to text before the model sees
them (§2.2). This proposal closes the dashboard's parity gap with a design that
fits the dashboard's path-based attachment model, and leaves the
`[attached_file N] path` marker grammar, which three independent readers pin,
untouched.

A fourth, independent change (§5, Phase D) points the in-process PDF byte
scanner that those eight channels still run at the memory-bounded extractor
the knowledge library and file search already use. It is separable and should
ship on its own.

## 2. Motivation

### 2.1 Current state: the dashboard

- `api_upload_file` (`src/kiro_crew/dashboard/file_api/uploads.py`)
  validates magic bytes, writes the file as
  `~/.kiro/crew/uploads/<uuid32>_<safe_name>` (0600 via
  `_write_file_restricted`), and returns `{"paths": [...]}`.
  Cap: `_MAX_UPLOAD_BYTES = 50 MiB` (`dashboard/handlers/files.py`).
- The frontend serializes each path as `[attached_file N] /path`
  (`website/src/utils/fileTokens.ts`). That string is the persisted user row,
  and two backend readers parse it: `chat_title._strip_attached_file_tokens`
  (`dashboard/chat_title.py`) and the queue repository's
  `_ATTACHMENT_MARKERS` (`dashboard/slot_queue_repository.py`).
  `test/test_attachment_marker_grammar_pin.py` fails if any of the three
  spellings drift. Changing the marker is therefore a three-site contract
  change, not a string edit.
- `build_prompt_blocks` (`src/kiro_crew/acp/prompt_blocks.py`) is the one
  place the outgoing message is rewritten before `session/prompt`: readable
  image paths become `image` blocks and the path is replaced by
  `[image: <name>]` in the text (its docstring). This rewrite is applied
  to the outgoing prompt only; the persisted row keeps the original string, so
  the three marker readers never see it. Documents are not touched: the agent
  receives a bare path to bytes its `read` tool cannot decode.
- The agent's only route is a shell call to `pdftotext`/`pdftoppm`, which
  raises a tool-approval prompt (600 s window). Issue #14570 records three
  expired prompts and one no-op subagent before a two-page PDF was read.

### 2.2 Current state: every other channel already converts

`kiro_crew.messaging.attachments.ingest_attachments` classifies an inbound
file as `DOCUMENT` when `doc_parser.is_parseable_document` says so
(`classify`), runs `extract_text` in a worker thread, redacts and truncates
to `IngestLimits.max_text_inject = 50 KiB` (`_clean_text`), and appends
`[Document: <name>]\n…\n[End of document]` to the prompt
(`append_attachment_context`). Callers: `slack/files.py`,
`discord/attachments.py`, `telegram/attachments.py`, `webex/attachments.py`,
`teams/attachments.py`, `whatsapp/attachments.py`, `wecom/attachments.py`,
`weixin/attachments.py`.

So the product question raised in the issue's triage — whether Kiro Crew should
convert documents before the model sees them — was answered when those
channels shipped. The dashboard is the exception, and the one surface where
the file is already on local disk, which is what makes a sidecar rather than
inline injection the natural shape (§5.1, §9).

### 2.3 Current state: the extractors

- `kiro_crew.pdf_extract.extract_pdf_segments` (`src/kiro_crew/pdf_extract.py`)
  runs pdfplumber in a child under `RLIMIT_PROFILE_EXTRACTOR`, with a
  `max_chars`, `max_pages` (`PDF_MAX_PAGES = 2000`) and a monotonic
  `deadline`; it never raises and returns `(label, text)` page segments
  (`PdfExtraction`). Callers: knowledge ingest and the dashboard file-grep
  document pass (`_grep_doc_segments` in `dashboard/file_api/grep.py`
  explains why PDFs are routed there rather than to `extract_text`).
  pdfplumber is a core dependency (`install_requires` in `setup.cfg`);
  pypdfium2 comes with it.
- `doc_parser._extract_pdf` (`src/kiro_crew/doc_parser.py`) is an
  in-process `stream…endstream` zlib scan. It is **live**, not dead code: it is
  the PDF branch of `extract_text`, which is what the eight channels in
  §2.2 call. The office preview never reaches it
  (`_OFFICE_PREVIEWABLE_EXT = {".docx", ".pptx"}` in `handlers/files.py`)
  and file-grep routes PDFs around it. So its poor output on object-stream PDFs
  and its in-process allocation are a channel-side problem today.
- `_rasterize` in `dashboard/handlers/office_slides.py` renders PDF pages with
  pypdfium2 in-process under a pixel ceiling (`MAX_SLIDE_PIXELS = 4_000_000`).

### 2.4 Corrections to issue #14570

Verified against `decba819400e`; the RFC supersedes the issue text on these:

1. `_gate_upload_file` (`handlers/files.py`) is the admission gate for
   shipping a local file **out** to a channel. It does not scan chat uploads.
   The sidecar's redaction comes from the same redactors the channels use
   (§6), not from that gate.
2. `kiro_crew/imaging.py` does not use pypdfium2. The in-process PDF rasterizer
   with a memory ceiling is `office_slides.py`; Phase C reuses that.
3. `knowledge/readers.py` does not call pdfplumber inline; it imports
   `kiro_crew.pdf_extract`, which is the hardened seam this RFC targets.
4. "Upgrade `_extract_pdf`" is not an office-preview improvement (the preview
   never calls it); it is a fix for the eight channel integrations.

## 3. Goals

- A text PDF/DOCX/PPTX attached in dashboard chat is quotable by the agent
  through `read` alone, in the same turn, with no approval prompt.
- The `[attached_file N] path` grammar and its three readers are unchanged.
- Extraction cost is bounded (bytes, chars, pages, wall clock, address space)
  and cannot break or delay the upload beyond a fixed ceiling.
- A sidecar is never less redacted than the channel inline text is today.
- Failure is explicit to the agent: it is told the text was not extracted and
  why, rather than left to discover an unreadable path.

## 4. Non-goals

- OCR for image-only PDFs. Phase C renders page images; it does not read them.
- Formats beyond `.pdf`, `.docx`, `.pptx`. (`.xlsx` has its own sheet preview
  path and `grep.py` worksheet walker; out of scope here.)
- Changing how channels present documents (§2.2 stays as is, apart from the
  extractor swap in Phase D).
- Inlining document text into the dashboard prompt. Considered and deferred,
  §9 and §10.

## 5. Design

### 5.1 Text sidecar at upload (Phase A)

In `api_upload_file`, after the existing write and diagnostic block for a
`.pdf`/`.docx`/`.pptx` (the `_ALLOWED_DOC_EXT` intersection with
`doc_parser.DOC_EXTENSIONS`; `.doc`, `.ppt`, `.odt` etc. get no sidecar):

1. Extract, off the event loop, under one deadline shared by every file in
   the request (`deadline = monotonic() + SIDECAR_DEADLINE_SECS`, computed
   once before the first file; `_MAX_UPLOAD_FILES` is 20, so a per-file
   ceiling would be a 200 s attach):
   - `.pdf`: `await asyncio.to_thread(extract_pdf_segments, fileobj,
     max_chars=SIDECAR_MAX_CHARS + 1, deadline=deadline)`. The child is out of
     process and kills itself at the deadline; `to_thread` keeps its
     `communicate()` off the loop.
   - `.docx`/`.pptx`: `await asyncio.wait_for(asyncio.to_thread(extract_text,
     path, max_chars=SIDECAR_MAX_CHARS + 1, fileobj=…), timeout=deadline -
     monotonic())`, the same `extract_text` call the office preview and
     file-grep make (`extract_slides`/`join_slides` for a deck, so slide
     headers survive). A thread cannot be killed, so on timeout the handler
     records `timeout` (step 4) and drops the result when the thread finishes;
     the thread itself is bounded by `max_chars` and `doc_parser`'s zip-bomb
     and entity-expansion limits.
   - A file reached after the deadline has passed gets `timeout` without
     starting an extractor. A 20-document attach therefore yields sidecars
     for the first files and `timeout` for the rest, never a 200 s spinner.
   - Constants: `SIDECAR_MAX_CHARS = 1_000_000` (file-grep uses 400 000 for a
     search; a document the agent will page through can be larger),
     `SIDECAR_DEADLINE_SECS = 10`.
2. Redact before capping, exactly as `messaging.attachments._clean_text`:
   `redact_exfiltration_urls` then `redact_credentials`, then truncate to
   `SIDECAR_MAX_CHARS` with a `[… truncated]` tail. Move `_clean_text` (or a
   twin) to a shared leaf so both callers spell one implementation.
3. Write `<dest>.txt` with `_write_file_restricted` (0600). First line is a
   header the agent can rely on:
   `# extracted · <unit count> · <complete|truncated> · <safe_name>`, with
   the fixed fields first because `safe_name` has no length cap and the
   prompt builder reads only the first 256 bytes. The unit count is
   `<N> pages` for a PDF (from the segment labels),
   `<N> slides` for a deck, and the literal `pages n/a` for a `.docx`, because
   `_extract_docx` returns one string and Word files have no page structure
   until rendered. PDF page segments are separated by `--- page N ---` lines
   so the agent can cite pages; a deck keeps `_extract_pptx`'s
   `--- Slide N ---` headers.
4. Any failure (`PdfExtraction.failure` set, empty text, exception, deadline)
   leaves the upload intact and writes **no sidecar**. The failure reason is
   logged and recorded in a small sibling `<dest>.txt.failed` containing one
   word from the `PdfExtraction.failure` vocabulary (`timeout`, `memory`,
   `parse`, `unavailable`, …) or `empty`. Rationale: the prompt builder (§5.2)
   has to tell the agent *why* with no extraction of its own.
   Every sidecar or `.failed` path is appended to a second request-scoped
   list, `derived`, the moment it is written, and `_cleanup()` unlinks
   `[*paths, *derived, *also]`, so a later refusal or cancellation in the
   same request removes it with the uploads. It is a separate list because
   `paths` is also the admission counter (`len(paths) >= _MAX_UPLOAD_FILES`)
   and the response body; putting sidecars there would halve the file cap
   and leak them to the client. Without the cleanup hook, a batch of two
   where the second is rejected leaves the first's sidecar on disk with no
   upload beside it.
5. Upload response shape is unchanged: `{"paths": [...]}`. The sidecar is
   derivable from the path, so no client change is needed.

Naming cannot collide: every upload carries its own `uuid4().hex` prefix
(`api_upload_file`), so a user file literally named `report.pdf.txt` gets a
different prefix from `report.pdf`'s sidecar.

Why synchronous, at upload, with a deadline, rather than in the background:
the upload happens when the user *attaches*, before they finish typing, so
the latency is paid where nobody is waiting on the agent. A single ceiling
per request (10 s, however many files it carries) turns "extraction is slow"
into "no sidecar, reason `timeout`", which
the agent is told about. There is no pending state, no meta update, and no
race between send and extraction. This is the product call the issue's
acceptance criteria left open; the RFC makes it.

### 5.2 Telling the agent (Phase A, same PR)

`build_prompt_blocks` gains a document pass beside the image pass. For every
path candidate whose suffix is in `doc_parser.DOC_EXTENSIONS`, the builder
first checks containment: the candidate, resolved with the image pass's
no-follow rules, must lie inside the uploads root (the only place sidecars
are written). Both `uploads.py` and the prompt path read that root from one
leaf helper (`data_home() / "uploads"`, honouring the `_UPLOAD_DIR` override
that `handlers/files.py` carries today), so the writer and the checker can
never disagree about where sidecars live; the prompt path must not import the
upload handler, whose module pulls in `aiohttp.web` and would trip the
agent-sdk boundary gate on `prompt_blocks.py`. Only then does it stat the
sidecar (`Path(raw + ".txt").is_file()`)
and, when it exists, append to the text immediately after the path:

    [document text: /Users/u/.kiro/crew/uploads/3f…_report.pdf.txt, 2 pages]

The trailing `, <unit count>` is parsed from the header's unit field and
admitted only when it matches `^\d+ (pages|slides)$`; `pages n/a` (a
`.docx`) and anything else yield a note with no count. No other header byte
reaches the prompt string.

If instead `<raw>.txt.failed` exists:

    [document text: not extracted (timeout)]

Properties:

- Outgoing prompt only. Like `[image: …]`, this never reaches the persisted
  row, so `fileTokens.ts`, `chat_title.py` and `slot_queue_repository.py` are
  unaffected and the grammar pin test needs no change. A new pin test asserts
  the builder's note is absent from what the dashboard persists.
- Containment is the gate, then the sidecar's existence. The builder never
  extracts, never opens the document, and never stats a sidecar for a path
  outside the uploads directory, so a Slack user typing `/Users/me/x.pdf`
  sees no change even when `/Users/me/x.pdf.txt` exists.
- `_TYPED_MARKER_RE`'s treatment is extended so a user-typed
  `[document text:` is escaped before the builder writes its own, matching the
  existing `[image:` rule in `build_prompt_blocks`.
- Replay-safe: history reconstruction after compaction re-runs the builder,
  and the sidecar is still on disk, so the note reappears without state.
- The unit count comes from the sidecar header line, read with
  `safe_read_file_bytes_nolink(path, within_root=<uploads dir>,
  max_bytes=256, allow_truncate=True)`, the bounded reader the image pass
  already uses; `safe_read_file_bytes` has no byte bound and would read the
  whole sidecar on every prompt build and replay. No PDF parsing at prompt
  time.

The agent then calls `read` on the sidecar. Today the dashboard's agent reads
`.txt` uploads with `read` and no approval appears; the sidecar is the same
extension in the same directory. Phase A's exit criteria check this
end-to-end rather than assume it.

### 5.3 Agent guidance (Phase A, same PR)

One sentence in the dashboard agent prompt where attachments are described:
"A `.pdf`/`.docx`/`.pptx` attachment is followed by `[document text: <path>]`,
with a page or slide count when one is known; read that path. If it says `not extracted`, say so and ask before
shelling out." This is the whole of the behavioural contract; the sidecar
format above is what makes it hold.

### 5.4 Page images for text-poor PDFs (Phase C, optional)

When a PDF sidecar has fewer than `MIN_CHARS_PER_PAGE = 200` characters per
page averaged over the document (scanned or drawn), render up to
`MAX_PAGE_IMAGES = 20` pages to PNG under `<dest>.pages/page-NNN.png`, using
`office_slides.py`'s pypdfium2 render with its `MAX_SLIDE_PIXELS` ceiling, and
extend the note: `[document text: …, 12 pages, page images: <dir>]`. The agent
can then use its image reader on individual pages. Blocked on open question
§10.1.

### 5.5 Point `doc_parser._extract_pdf` at the bounded extractor (Phase D)

`_extract_pdf(path)` becomes a thin adapter: open the file, call
`extract_pdf_segments(fh, max_chars=<caller's cap or 50 MiB-equivalent>,
deadline=monotonic() + 30)`, join segments with `--- page N ---` headers, and
return `""` on `failure` (which `ingest_attachments` already maps to
`[Attached document: <name> — could not extract text]`).
The zlib scan and its `_safe_decompress` helper go away with their tests
replaced. `extract_text`'s docstring note "PDF extraction is byte-scan based" is
updated.

This phase changes nothing the dashboard sees. It makes the eight channels'
PDF text come from the same parser as knowledge ingest and file search, and
moves their PDF allocation out of the gateway process, which is the whole
reason `pdf_extract.py` exists (its module docstring). Per the triage
comment on #14570 it is a candidate for its own issue and the automated queue;
this RFC records the design so that issue can cite it.

## 6. Security considerations

- **Redaction parity.** The sidecar is redacted with the same two redactors the
  channels apply to inline text, before truncation, so a secret cannot survive
  by sitting past the cap. A sidecar is never less redacted than today's Slack
  inline text.
- **Memory.** PDF text extraction runs out of process under
  `RLIMIT_PROFILE_EXTRACTOR`; the gateway never holds a `page.chars` list. DOCX
  and PPTX go through `doc_parser`'s zip-bomb and entity-expansion limits.
  Phase C's renderer keeps `MAX_SLIDE_PIXELS`.
- **Time.** One deadline per upload request (10 s shared by all its files,
  up to `_MAX_UPLOAD_FILES = 20`). A PDF child past it is killed by
  `extract_pdf_segments` itself; an OOXML thread past it is abandoned and its
  result dropped; files reached after it get `timeout` without starting an
  extractor. The upload still returns.
- **Filesystem.** Sidecars are written only inside the uploads directory,
  0600, by the same helper as the upload, and join the request's cleanup set
  as they are written. The prompt builder checks containment under the
  uploads root before it stats `raw + ".txt"` / `raw + ".txt.failed"`, and
  reads the header through `safe_read_file_bytes_nolink` with a 256-byte
  cap, so the sensitive-path gate and no-follow rules apply.
  Windows UNC and linked-ancestor screens in the image pass are reused
  verbatim for the document pass.
- **Prompt injection.** Document text reaches the model only through a file
  the agent chooses to `read`, the same trust position as any attached `.txt`.
  Nothing from the document is written into the prompt string itself except
  a unit count the builder parses from the header against a fixed pattern
  (§5.2); a header that does not match contributes nothing.
- **Lifecycle.** Uploads have no retention sweep today (none found under
  `dashboard/file_api/` or `handlers/files.py`). Sidecars inherit that: they
  live and die with their upload. If a sweep is added later it must treat
  `<x>.txt`, `<x>.txt.failed` and `<x>.pages/` as owned by `<x>`.

## 7. Migration plan

Each phase is one PR and independently abandonable.

**Phase A — sidecar + prompt note + agent guidance.** Exit criteria:
- Attaching a 2-page text PDF in dashboard chat and asking "quote the second
  paragraph" yields the quote with exactly one `read` tool call on the
  sidecar and no permission request in the transcript.
- Same for a `.docx` and a `.pptx` (the deck's answer cites a slide number).
- `test_attachment_marker_grammar_pin.py` passes unmodified; a new test
  asserts the persisted user row contains no `[document text:`.
- A PDF whose extraction is forced to time out uploads successfully; the
  prompt carries `[document text: not extracted (timeout)]`; the original
  bytes are byte-identical to the upload.
- A PDF fixture containing an AWS key pattern yields a sidecar in which the
  key is redacted and a prompt that contains no key material.
- The deadline path is covered by a test that stubs the extractor; no test
  asserts a wall-clock duration (the determinism gate refuses that shape).
  Child start cost is about 0.2 s per the `pdf_extract.py` module docstring.
- A two-file request whose second file is refused leaves no sidecar or
  `.failed` sibling for the first on disk.
- A `.docx` upload yields a header reading `pages n/a` and a prompt note with
  no count.
- A message naming a document outside the uploads directory gets no note,
  even when a `.txt` sibling exists there.
- Uploading `report.pdf` and a user file named `report.pdf.txt` in one request
  produces three distinct files.

**Phase B — nothing.** Reserved: if §10.2 resolves toward inlining a head
excerpt, it lands here; otherwise the number stays unused and the plan is
honest about it.

**Phase C — page images for text-poor PDFs.** Blocked on §10.1. Exit criteria:
- A scanned 3-page PDF yields `<dest>.pages/page-001..003.png`, each within
  `MAX_SLIDE_PIXELS`, and the note names the directory.
- A text PDF yields no `.pages/` directory.
- Rendering past `MAX_PAGE_IMAGES` stops and the note says `(first 20)`.

**Phase D — `_extract_pdf` delegates to `extract_pdf_segments`.** Exit:
- An object-stream PDF fixture that the zlib scan renders as garbage comes
  back as page-labelled text through `extract_text`.
- `doc_parser.py` contains no `zlib` import and no `endstream` regex.
- The Slack ingest test for a PDF attachment still produces
  `[Document: …]` text, and a `PdfExtraction.failure` produces the existing
  `could not extract text` rejection.

## 8. Backward compatibility

- Upload API response, persisted message rows, `meta.files`, the marker
  grammar, the office preview and file-grep are unchanged.
- Agents that ignore the note behave as today (they see the path and may
  shell out). Agents that follow it stop shelling out.
- Phase D changes the *text* channels get for PDFs (better layout, page
  headers) and the failure vocabulary that reaches logs; the user-visible
  rejection string is unchanged.
- Hosts without pdfplumber (`pdfplumber_available()` false) get
  `not extracted (unavailable)` and today's behaviour.

## 9. Alternatives considered

**Inline the text into the prompt, as the channels do.** Exact parity, zero
tool calls. Rejected as the primary shape for the dashboard because: the 50 KiB
cap truncates anything longer with no way for the agent to read on; the text
is replayed into every later turn of the session (≈12k tokens for a full-cap
document, per turn, for as long as the session lives), whereas a sidecar costs
tokens only when read; and the dashboard already has the file on disk, which
channels do not. Kept as an open question for a small head excerpt (§10.2).

**Extend the `[attached_file N] path` marker itself** (the issue's step 2).
Rejected: it is a three-site contract pinned by
`test_attachment_marker_grammar_pin.py`, and the persisted row would then carry
sidecar paths and page counts the title and queue readers have to learn to
skip. The prompt-time note gives the agent the same information with zero
contract change.

**Extract at send time instead of upload time.** Rejected: it puts the parse
on the path the user is waiting on, and a deadline there means a visible
"thinking" stall rather than a slightly longer attach spinner.

**Background extraction with a pending state.** Rejected: it needs a meta
update channel to the agent or a "text pending" message that the agent has to
poll for, and the race between send and completion is exactly the ambiguity
the triage comment asked a product owner to remove.

**Teach the agent's `read` tool to parse PDFs.** Out of Kiro Crew's hands;
`read` belongs to kiro-cli.

## 10. Open questions

1. **Phase C scope.** Is rendering page images worth it without OCR? A vision
   model can read a scanned page from the PNG, so the answer is probably yes
   for ≤ 20 pages, but each page the agent opens costs image tokens on every
   later turn. Phase C is blocked until a maintainer says yes or no.
2. **Head excerpt.** Should the note also carry the first ~2 KiB of redacted
   text so a one-page PDF needs no tool call at all? Cheap, but it is the first
   document content to enter the prompt string itself. Default in this RFC:
   no; the agent reads the sidecar.
3. **Sidecar visibility in the dashboard file tree.** The uploads directory is
   browsable; should `*.txt` sidecars be hidden or shown as a child of the
   document? Default: shown, since they are useful to the user too.

## 11. Acceptance

Proposed 2026-10-09 by kamleshnanda. Not yet accepted. A maintainer accepting
this document is recorded here with their name and the date, and the status
above moves to `accepted` in the same change.
