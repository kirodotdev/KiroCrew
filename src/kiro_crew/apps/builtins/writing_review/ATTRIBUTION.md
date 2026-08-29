# Attribution

The Writing Review app was originally written by **jpontone** as a
first-party built-in app in an older internal version of a Kiro Crew
-family dashboard, and is ported here as a Kiro Crew built-in with the
original author's handle preserved in `app.json`.

## What was kept

- The product: a multi-scanner document review flow that runs ten
  specialised LLM scanners in parallel — clarity, naturalness,
  structure, evidence, consistency, attribution, audience,
  readability, plus a doc-type-triggered design or email conditional
  — collects findings as inline comments anchored to the exact
  section and paragraph, and hands the reviewed doc off to Watson
  (the writing-review-reviewer agent) for a discussion afterwards.
- The per-scanner brief structure: markdown briefs under
  `scanners/<name>.md` with role framing, rules, and Before/After
  examples, loaded at review time so a brief edit doesn't need a
  code change.
- The cross-validation pass: `_cross_validate_findings` inspects
  same-paragraph findings across scanners and tags them
  `clean` / `redundant` / `conflicts`, so the synthesis pass can
  drop duplicates and Watson can explain scanner disagreements.
- The four-layer defensive JSON stack for scanner responses:
  raw-response logging on parse failure, brace-count truncation
  detection, `TruncatedResponseError.partial_findings` recovery,
  and a merge retry that keeps the first attempt's findings if the
  retry also truncates.
- The `Ask` field (author's directive to the scanners), the
  `Additional context` field (per-scan exceptions and framing
  notes), and the `[VISUAL:...]` image-placeholder convention that
  suppresses false "missing diagram" findings from scanners that
  cannot see image content.

## What changed for this repository

- **Backend runtime** — rewritten from the original's provider-per-scan
  flow into Kiro Crew's `ScannerPool` worker-session pattern, so a
  bounded pool of Claude sessions is reused across reviews rather
  than each scanner spawning its own client per scan.
- **Backend integration** — registered as a Kiro Crew builtin via
  `app.json` under `backend.routes`, with the app-kit chat launcher
  driving the Watson handoff and `iconUrl` supplying a custom
  document + pencil SVG icon.
- **Frontend** — rewritten from the original's page component against
  Kiro Crew's own conventions: React Query for server state, the
  shared `useDialogFocusTrap` hook for modal a11y, chip toggles for
  the scanner picker with symmetric auto-check on doc_type changes,
  `DropdownWithOther` for custom audience/doc-type/tone values, and
  a `sessionStorage` mirror so an in-flight scan survives a
  same-tab remount (theme change, in-app navigation).
- **docx parser** — walks `document.element.body` in document
  order so paragraphs and tables interleave in the reviewer's view;
  tables render as markdown pipe-tables at the position they occupy
  in the source, and inline images and charts emit position-anchored
  `[Image]` / `[Chart]` / `[VISUAL:...]` placeholders with alt-text
  extracted from `<wp:docPr descr|title>` when present.
- **Dedup narrowing** — the collapse key was narrowed from
  `(section, paragraph)` to `(scanner, section, paragraph)`, so
  cross-scanner overlaps at the same location survive to
  `_cross_validate_findings`. The `conflicts` and `redundant`
  cross-validation tag paths (and the "Scanners disagree" pill and
  the "Also appears in" collation that consume them) are now
  reachable rather than dead code.
- **i18n** — 136 new source keys, translations across all 11
  non-English catalogs, and a shared `resolveScannerName()` helper
  keyed on `apps.writingReview.scannerNames.*`. Every
  scanner-ID render surface (chips, finding-card headers,
  related-location interpolations, failed-scanner list, and the
  Settings scanner-toggle panel) resolves through that helper, with
  `data-scanner-name` as the locale-agnostic test hook.
- **Discussion handoff prompt** — extracted to
  `lib/reviewChatHandoff.prompt.ts` under Kiro Crew's model-facing
  prompts boundary (`src/**/*.prompt.ts` in `eslint.i18n.config.js`);
  wire-contract field names (`review_id:`, `doc_name:`,
  `findings (N):`, and so on) are English-by-design and matched
  byte-for-byte by the writing-review-reviewer agent parser.
