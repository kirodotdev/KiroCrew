# Office document engine (offline OOXML)

The offline half of the Office capability set: local, zero-network,
zero-credential read, template-create and in-place edit of Office Open XML
`.docx` and `.pptx` files. It never calls Microsoft Graph, never authenticates,
and never uploads. Cloud round-trip and any Graph-backed operation are separate,
later connector-campaign slices; this engine is what those slices call to touch
a file's bytes, not a client for a remote surface.

This spec owns the `office_documents` engine package under the Microsoft vendor
tree. Its place in the connector campaign is the `office_documents` capability
set named in [connector-capability-manifest.md](connector-capability-manifest.md)
(a horizontal Office capability group under `W07`, not a 13th provider). The
shared Graph runtime that the *cloud* Office operations build on is a different
subsystem and is specified elsewhere; nothing here depends on it.

## Why a new engine instead of `knowledge/readers.py` or `doc_parser.py`

`kiro_crew.doc_parser` already extracts flat text from `.docx`/`.pptx` uploads,
and `FileReader` in the knowledge ingest path reads `.docx`. Both are read-only
text extractors: they answer "what words are in this file," discard structure,
and never write. This engine answers a different contract — structured read
(paragraphs *and* tables, slides *and* speaker notes), faithful create from a
template, and targeted in-place edit that preserves every byte it did not
change. That write path is the reason it is its own subsystem rather than a few
more functions on the readers.

Two deliberate non-changes follow from that boundary, each an invariant:

- **`python-pptx` is not a dependency.** `FileReader._read_pptx` exists but
  `.pptx` is deliberately excluded from `FileReader.SUPPORTED` precisely because
  `python-pptx` is undeclared, and adding it is a behavior change with its own
  consequences (dependency-license gate, bundle size). This engine reads and
  edits `.pptx` with the standard library (`zipfile` plus a hardened XML parser)
  instead, which also matches how a `.pptx` is actually structured — there is no
  single presentation-body part; each slide is its own part.
- **`FileReader.SUPPORTED` is not touched.** That set is the knowledge
  folder-scan gate, and `test_knowledge.py` pins `CODE_EXTS` as a subset of it;
  widening it changes folder-scan semantics. This engine is invoked directly, not
  through the folder scanner, so it needs no entry there.

## The fidelity contract

An OOXML file is a zip of *parts*. The rule the edit path keeps is: **an edit
rewrites only the parts it changed, and every other part is copied through
byte-for-byte** — same bytes, same per-part compression type, same member order.
A full-document library that parses the whole package and re-serializes it drops
or reorders theme, styles, media, custom XML and relationships the authoring
application wrote; a targeted text edit must not. `rewrite_parts` in the
container module is the single write primitive that enforces this: it takes a
map of part-name → new bytes, and every part NOT in that map is copied through
verbatim.

Consequences that are load-bearing, not incidental:

- **Writes are atomic.** `rewrite_parts` builds the whole archive in a temp file
  in the destination directory and swaps it in with `os.replace` only once fully
  written, cleaning up the temp artifact on any failure. A failed or interrupted
  edit therefore never truncates the destination — which is what makes passing
  the same path as source and destination (in-place edit) safe.
- **An edit that cannot apply raises before any byte is written.** An
  out-of-range paragraph index or an unknown slide number raises
  `DocumentEditError` during the rewrite-planning step, so a bad edit leaves no
  half-written file. `replace_paragraph_text` (docx) and `replace_slide_text`
  (pptx) own this ordering.
- **A replacement must name a part that already exists in the source.**
  `rewrite_parts` takes a replacement map and rejects, as `MalformedDocument`,
  any key naming a part absent from the source archive — so a caller cannot
  silently no-op a typo'd part name or smuggle in a brand-new part. Parts not in
  the map are copied through verbatim.

The `TestFidelityMatrix` and per-format edit tests assert this two ways: every
untouched part is byte-identical after a round-trip, and the written file
reopens under a *different* reader than the one that wrote it (`python-docx` for
docx output; a from-scratch stdlib zip walk for pptx) and yields the expected
content — so a passing test is not the engine re-reading its own output through
its own code path.

## Structured read

- **docx** — `read_document` walks `word/document.xml` in body order, returning
  ordered `Paragraph` records (text plus the referenced paragraph-style id) and
  `Table` records (row-major cell-string grids). It parses the part directly with
  the hardened parser rather than leaning on a whole-document library, so the read
  path carries no library dependency of its own.
- **pptx** — `read_presentation` resolves slides in **presentation order**, not
  filename order: it reads `<p:sldIdLst>` in `ppt/presentation.xml`, mapping each
  `<p:sldId r:id=…>` through `ppt/_rels/presentation.xml.rels` to a slide part
  (a reordered deck keeps its `slideN.xml` filenames and only rewrites the id
  list, so filename order would report the wrong slide). The order signal is
  three-way: `sldIdLst` when stated; **filename-number order as a fallback ONLY
  when no order is stated at all** (the presentation part, its rels, or the id
  list is absent — then filename order is the sole available signal, and numeric
  so `slide10` sorts after `slide2`); and **fail closed (`MalformedDocument`)
  when an order IS stated but is broken** — a dangling `r:id`, or a present-but-
  unparseable presentation/rels part — rather than guessing an order that could
  target the wrong slide. For each slide it resolves the speaker-notes part by
  following the slide's `.rels` relationship of the `notesSlide` type,
  normalizing the relative Target against `ppt/slides/`. A slide with no notes
  relationship carries empty notes; a notes part that is present but unparseable
  surfaces as `MalformedDocument` rather than blank.

## Template create

`create_from_template` (per format, and dispatched by the engine facade) is a
rejection-gated verbatim copy of a local template file with an optional set of
targeted text substitutions applied through the same edit path — never a
fresh synthesis. The template's styles, theme and layout are carried into the new
file unchanged; only the addressed paragraphs (docx, by zero-based body-paragraph
index) or slides (pptx, by one-based slide number) are filled.

## The rejection gate

`classify` is the campaign's one `required=True` Office operation, and the
contract it keeps is **explicit refusal, never silent corruption**. Every read,
create and edit entry point runs a file through it (via `ensure_editable`) before
touching bytes. A document engine that re-zipped an encrypted package, dropped a
signature part, or parsed a legacy binary as if it were a zip would produce a
file that opens broken; refusing is the safe answer.

It returns a `Classification` (never raising for a document reason;
`ensure_editable` is the strict wrapper that turns a non-ok verdict into the
matching typed error), branching on the cheapest decisive signal first:

| Category | Signal | Verdict |
|---|---|---|
| Legacy OLE2 binary (`.doc`/`.ppt`/`.xls`) | OLE2 compound-file magic + legacy extension | `UnsupportedDocument` |
| Password / agile-encrypted OOXML | OLE2 magic (the encrypted package is a compound-file wrapper) or an `EncryptedPackage` member | `ProtectedDocument` |
| Macro-enabled | `.docm`/`.pptm`/… extension, or a `vbaProject.bin` member regardless of extension | `ProtectedDocument` |
| Digitally signed | presence of an `_xmlsignatures/` part | `ProtectedDocument` |
| IRM / rights-managed | presence of a DataSpaces protection part | `ProtectedDocument` |
| Not a container | no zip local-file header, an empty archive, or a rejected inventory | `MalformedDocument` |
| Duplicate member | a part name that appears more than once in the archive (a smuggling vector `set()` would silently collapse) | `MalformedDocument` |
| Wrong kind | a valid container with none of `word/document.xml`, `ppt/presentation.xml` or `xl/workbook.xml`, or the wrong one for the entry point | `MalformedDocument` / `UnsupportedDocument` |

**Signature detection is presence-only, and validity is deliberately not
asserted.** Structural presence of a signature part and the cryptographic
validity of that signature genuinely diverge, so this engine reports that a
signature exists and refuses to edit past it — it never claims the signature
verifies. `ProtectedDocument` is a subclass of `UnsupportedDocument` so a caller
that only wants "cannot handle this" catches the base, while a caller that must
explain *why* branches on the subclass or the `reason` discriminator.

**Recognized kinds are docx, pptx and xlsx.** On an `ok` verdict `classify`
resolves the kind from the mandatory main part — `word/document.xml` (docx),
`ppt/presentation.xml` (pptx) or `xl/workbook.xml` (xlsx). The xlsx kind exists
so a valid workbook is recognised as one rather than falling through as a
`malformed_document`; every protection and rejection category above applies to
xlsx identically (a new kind that skipped the gate would be worse than no kind),
which the negatives assert.

`TestRejectionGate` carries one negative test per category above, and
`TestXlsxRejectionGate` re-asserts the whole gate for the xlsx kind (valid
workbook classifies, plus encrypted / OLE2-legacy / macro / vbaProject /
signature / IRM / non-zip / empty / duplicate-member / wrong-kind / UNC). The
error hierarchy relationships are pinned by `TestErrors`.

## Container safety

Reads and rewrites both go through the shared `kiro_crew.zip_vet` inventory
guard, which bounds an untrusted archive's declared central-directory size
*before* `zipfile.ZipFile` is constructed (construction allocates from that
declared size, so a cap applied after opening is not a bound). Parts are then
read with a real decompressed-size cap that ignores a lying header, and parsed
with `defusedxml` so a crafted part cannot mount an XXE — the engine refuses to
fall back to the entity-resolving stdlib parser, degrading to a
`MalformedDocument` if the hardened parser is unavailable. Every path — a read
source AND an edit/template DESTINATION — is screened through
`kiro_crew.security.is_sensitive_path` and a remote-path check (UNC
`\\server\share` / url-scheme) before any file is opened or written. Screening
the destination matters as much as the source: an edit writes via
`mkstemp(dir=<dst_dir>)` + `os.replace`, so a remote destination would perform
an outbound SMB/NTLM write on Windows and leak the user's NTLM hash.
