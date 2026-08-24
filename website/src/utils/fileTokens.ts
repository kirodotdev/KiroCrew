/** Shared file-token utilities used by send() and renderUserContent(). */

import { decodeLocalPath } from './urlTransform'

export const IMG_EXT = /\.(png|jpe?g|gif|webp|bmp|svg)$/i

/** Video containers the upload boundary accepts, mirroring `_ALLOWED_VIDEO_EXT`
 *  in `dashboard/handlers/files.py`. Kept in sync deliberately: this drives the
 *  client's cap decision, and a client that is more permissive than the server
 *  only produces uploads that die at the door. */
export const VIDEO_EXT = /\.(mp4|m4v|mov|webm)$/i

/** Boundary-aware regex for @token matching. Prevents `@foo.ts` from matching
 *  inside `@foo.tsx` (right boundary) and inside `foo@bar.ts` (left boundary).
 *
 *  The left boundary is a CAPTURE GROUP, not a lookbehind: lookbehind is a
 *  `SyntaxError` at `new RegExp` time on Safari < 16.4, and this is a runtime
 *  `new RegExp` from a string that no bundler down-levels, so it would take
 *  the render/send path down on a supported browser (the same hazard
 *  `ReportView.tsx` documents and avoids). Consumers that REPLACE must
 *  therefore re-emit group 1 -- see replaceTokens and serializeDirTokens,
 *  which already follow this convention; `.test()` callers are unaffected.
 *
 *  The left boundary matters because without it `@README.md` inside unrelated
 *  text like `foo@README.md` reads as a real mention: hasExactRelMention would
 *  report a file "already mentioned" from that substring and skip inserting a
 *  clean token, and prepareSendPayload would splice `[attached_file N] ...`
 *  into the middle of that word at send time. */
function tokenRegex(token: string, flags = ''): RegExp {
  const escaped = token.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
  return new RegExp(`(^|\\s)@${escaped}(?=\\s|$)`, flags)
}

/** Parse file paths from message meta or [attached_file N] patterns in content. */
export function parseFiles(content: string, meta?: Record<string, unknown>): string[] {
  const metaFiles = (meta?.files || []) as string[]
  return metaFiles.length
    ? metaFiles
    : (content.match(/\[attached_file \d+\] (\S+)/g) || []).map(s => s.replace(/\[attached_file \d+\] /, ''))
}

/** Per-path display label: the shortest trailing path segments that make the
 *  label unique across `paths` (e.g. two `report.docx` in different dirs become
 *  `q3/report.docx` and `q4/report.docx`).
 *
 *  Widens until unique rather than stopping at two segments. Two paths that
 *  share their last TWO segments -- `/a/x/report.docx` and `/b/x/report.docx` --
 *  both collapsed to `x/report.docx`, so two distinct attachments rendered with
 *  the same chip label AND the same `mentionMap` key: the second overwrote the
 *  first, and clicking either chip opened whichever path won. */
export function buildFileLabels(paths: string[]): Map<string, string> {
  const map = new Map<string, string>()
  const partsOf = new Map(paths.map(p => [p, p.split('/')]))
  const labelAt = (p: string, depth: number) => {
    const parts = partsOf.get(p) ?? [p]
    return parts.slice(Math.max(0, parts.length - depth)).join('/') || p
  }
  const maxDepth = Math.max(1, ...paths.map(p => (partsOf.get(p) ?? []).length))
  for (const p of paths) {
    let depth = 1
    while (depth < maxDepth && paths.some(q => q !== p && labelAt(q, depth) === labelAt(p, depth))) {
      depth += 1
    }
    map.set(p, labelAt(p, depth))
  }
  return map
}

export interface ResolvedFileSegment {
  /** Display text with every attachment reference normalized to an `@label` token (embedded) or stripped (standalone). */
  display: string
  /** `@label` (without the leading @) -> full path, for files referenced inline IN THIS content. */
  mentionMap: Map<string, string>
  /** Standalone-upload paths whose token appears IN THIS content — render as cards. Does NOT include files that are absent from this content (the caller decides those at message level, to avoid per-segment duplication). */
  cardPaths: string[]
  /** Display label per path (basename, disambiguated). */
  labels: Map<string, string>
}

/**
 * Normalize a user-message text segment for rendering attachments consistently.
 *
 * Single source of truth for how attachment references become display. Both a
 * file the user wove into a sentence (an @-mention) and a bare upload serialize
 * to the SAME `[attached_file N] /path` plumbing in the persisted message, and
 * the server stores that token form in `content` while ALSO keeping
 * `meta.files` — so we cannot branch on `meta.files`, and the token itself does
 * not say which it was. The distinguishing signal is POSITION:
 *
 *   - A token embedded in a line with other text -> inline `@label` chip.
 *   - A token alone on its line -> standalone upload, stripped from the text and
 *     returned in `cardPaths` for the caller to render as a block card.
 * Path resolution is LOSSLESS: the token's number N is the 1-based index into
 * `orderedFiles`, so `orderedFiles[N-1]` recovers a path even when it contains
 * spaces (the serialized `[attached_file N] path` form is not whitespace-
 * delimited) AND even when earlier attachments are images (N indexes the
 * ORIGINAL list, so an image preceding a spaced-filename document still
 * resolves correctly). The whitespace-bounded `\S+` capture is used only as a
 * fallback when N is out of range (e.g. no-meta history replay where
 * `orderedFiles` was itself parsed from the tokens).
 *
 * SEGMENT-SCOPED: `cardPaths` contains ONLY standalone uploads whose token is
 * present in this `content`. Files in `orderedFiles` that are not referenced
 * here at all are NOT emitted — a message split into multiple segments (paste
 * tokens) would otherwise re-emit every unreferenced attachment in every
 * segment. The caller renders truly-unreferenced attachments exactly once at
 * message level via findUnreferencedAttachments.
 *
 * `orderedFiles` is the ORIGINAL ordered attachment list (as persisted / as
 * `meta.files`, IMAGES INCLUDED) so token indices line up. Images are filtered
 * out of `cardPaths` on OUTPUT only (they render as inline `![image]()`
 * markdown, never as file cards); an image referenced by an embedded token is
 * likewise never added to mentionMap.
 */
export function resolveFileSegment(content: string, orderedFiles: string[]): ResolvedFileSegment {
  const labels = buildFileLabels(orderedFiles)
  const mentionMap = new Map<string, string>()
  const cardPaths: string[] = []
  const seen = new Set<string>()

  const markerRe = /\[attached_file (\d+)\]([^\S\n]+)/g
  let display = ''
  let lastIdx = 0
  let m: RegExpExecArray | null
  while ((m = markerRe.exec(content)) !== null) {
    const n = parseInt(m[1], 10)
    const pathStart = m.index + m[0].length
    const indexed = n >= 1 && n <= orderedFiles.length ? orderedFiles[n - 1] : undefined
    let path: string
    let pathEnd: number
    if (indexed && content.startsWith(indexed, pathStart)) {
      // Lossless: the real path (possibly with spaces) sits verbatim at pathStart.
      path = indexed
      pathEnd = pathStart + indexed.length
    } else {
      // Fallback: whitespace-bounded capture (no-meta replay / index mismatch).
      const rest = content.slice(pathStart)
      const wsIdx = rest.search(/\s/)
      path = wsIdx === -1 ? rest : rest.slice(0, wsIdx)
      pathEnd = pathStart + path.length
    }

    // Embedded when non-whitespace text sits on the SAME line as the token.
    const beforeSlice = content.slice(0, m.index)
    const afterSlice = content.slice(pathEnd)
    const lineBefore = beforeSlice.slice(beforeSlice.lastIndexOf('\n') + 1)
    const nlAfter = afterSlice.indexOf('\n')
    const lineAfter = nlAfter === -1 ? afterSlice : afterSlice.slice(0, nlAfter)
    const embedded = lineBefore.trim().length > 0 || lineAfter.trim().length > 0
    const label = labels.get(path) || (path.split('/').pop() || path)
    const isImage = IMG_EXT.test(path)

    display += content.slice(lastIdx, m.index)
    if (embedded && !isImage) {
      mentionMap.set(label, path)
      display += `@${label}`
    } else if (!embedded && !isImage) {
      cardPaths.push(path)
      // Drop a trailing newline the standalone token owns so it leaves no blank
      // line; if it had a leading newline instead, drop that from the output.
      if (afterSlice.startsWith('\n')) pathEnd += 1
      else if (content[m.index - 1] === '\n') display = display.slice(0, -1)
    } else {
      // Image token: drop it silently (images render via ![image]() markdown).
      if (afterSlice.startsWith('\n')) pathEnd += 1
      else if (content[m.index - 1] === '\n') display = display.slice(0, -1)
    }
    seen.add(path)
    lastIdx = pathEnd
    markerRe.lastIndex = pathEnd
  }
  display += content.slice(lastIdx)

  // Recover any `@relative` mentions already present (fresh optimistic bubble),
  // for non-image files not already resolved from a token above.
  const notSeen = orderedFiles.filter(p => !seen.has(p) && !IMG_EXT.test(p))
  buildRelMap(notSeen, display).forEach((fullPath, suffix) => mentionMap.set(suffix, fullPath))

  return { display, mentionMap, cardPaths, labels }
}

/**
 * Message-level companion to resolveFileSegment: given the full (paste-collapsed)
 * message text and the ORIGINAL ordered attachment list (as persisted / as
 * `meta.files`, images included), return the non-image attachments that are not
 * referenced anywhere in the text — neither by an `[attached_file N]` token nor
 * by an `@relative` mention. The caller renders these exactly once as cards, so
 * a message split into multiple segments (paste tokens) can't duplicate them.
 *
 * CRITICAL: token number N indexes `orderedFiles` (the original list) — the same
 * list resolveFileSegment indexes with files[N-1]. It is NOT the image-filtered
 * list, so a mixed image+file upload probes the correct token. Non-image
 * filtering is applied only to the RESULT.
 */
export function findUnreferencedAttachments(text: string, orderedFiles: string[]): string[] {
  const referenced = new Set<string>()
  orderedFiles.forEach((p, i) => {
    const n = i + 1
    if (text.includes(`[attached_file ${n}]`)) { referenced.add(p); return }
    if (buildRelMap([p], text).size) referenced.add(p)
  })
  return orderedFiles.filter(p => !IMG_EXT.test(p) && !referenced.has(p))
}

/** Walk path segments to find the shortest @suffix present in text. */
export function buildRelMap(paths: string[], text: string): Map<string, string> {
  const map = new Map<string, string>()
  for (const p of paths) {
    const segs = p.split('/')
    for (let i = 1; i < segs.length; i++) {
      const suffix = segs.slice(i).join('/')
      if (tokenRegex(suffix).test(text) && !map.has(suffix)) { map.set(suffix, p); break }
    }
  }
  return map
}

/** Replace @rel tokens in text using a replacer function. */
export function replaceTokens(
  text: string, paths: string[], relMap: Map<string, string>,
  replacer: (fullPath: string, idx: number) => string,
): string {
  let result = text
  paths.forEach((p, i) => {
    const rel = [...relMap.entries()].find(([, v]) => v === p)?.[0]
    if (!rel) return
    // Re-emit group 1 (the captured leading boundary): tokenRegex matches the
    // whitespace/start before `@`, so dropping it would eat the separator.
    result = result.replace(tokenRegex(rel, 'g'), (_m: string, pre: string) => pre + replacer(p, i))
  })
  return result
}

/** Build send payload from raw input text and pending files. */
export interface SendPayload {
  txt: string        // LLM-facing content
  displayTxt: string // UI-facing content
  filePaths: string[]
  imgPaths: string[]
}

/** Windows path shapes the PRODUCER normalizes to forward slashes: drive
 *  letters and UNC shares. Deliberately WIDER than the consumer-side
 *  `WINDOWS_ABS_PATH_RE` (urlTransform.ts), and that asymmetry is the security
 *  design, not drift: this regex only ever sees paths returned by our own
 *  upload endpoint (trusted), while the consumer predicate classifies
 *  attacker-authorable markdown `src` values and must never admit a
 *  host-naming UNC shape. A UNC upload is emitted as `//host/share/…`, which
 *  reaches the renderer as a scheme-less relative URL and is validated against
 *  the gateway's trusted attachment roots server-side. */
const WIN_PRODUCER_PATH_RE = /^(?:[A-Za-z]:|\\\\[^\\/]+)[\\/]/

/** Forward-slash form of a Windows-shaped absolute path (drive letter / UNC).
 *  A path that is not Windows-shaped is returned untouched: on POSIX `\` is a
 *  legal filename character, so a blanket backslash rewrite would corrupt a
 *  real name (`weird\name.txt`) into a nonexistent nested path. */
export function normalizeWindowsPath(p: string): string {
  return WIN_PRODUCER_PATH_RE.test(p) ? p.replace(/\\/g, '/') : p
}

/** Append a picked file to the pending-attachment list, deduped by canonical
 *  Windows path identity. The `@`-picker stages a native `C:\…` path while the
 *  tree context menu stages the normalized `C:/…` form of the SAME file; an
 *  exact-string check treats those as two files and the send carries duplicate
 *  attachment markers.
 *
 *  A matching entry that is NOT already canonical (a restored draft or a
 *  failed-send restore predating canonical staging) is REPLACED with the
 *  canonical form, not merely kept: token bookkeeping and remove-chip lookups
 *  key on the staged string, so a retained legacy `C:\…` entry would miss the
 *  `C:/…` token key and strand the `@` mention in the composer. POSIX paths
 *  are untouched either way. */
export function addPendingFile(prev: string[], path: string): string[] {
  const canon = normalizeWindowsPath(path)
  const idx = prev.findIndex(p => normalizeWindowsPath(p) === canon)
  if (idx === -1) return [...prev, canon]
  if (prev[idx] === canon) return prev
  const next = prev.slice()
  next[idx] = canon
  return next
}

/** True when `text` already carries an `@` mention of EXACTLY `rel`, in
 *  either separator rendition (`@src/a/b.ts` or the native-Windows
 *  `@src\a\b.ts` the picker inserts) -- never a shorter basename suffix.
 *  Deliberately NOT a suffix walk (unlike buildRelMap): two staged files that
 *  share a basename (\`src/a/util.ts\` vs \`src/b/util.ts\`) can both suffix-
 *  match a single `@util.ts` mention, so a suffix-based guard reports the
 *  SECOND file as "already mentioned" from the FIRST file's token -- and the
 *  fallback chip-remove derivation (buildRelMap again) then strips that same
 *  token when removing the second file's chip, deleting the first file's
 *  mention instead. `rel` is the exact token `handleAddToContext` inserts, so
 *  comparing against exactly that string (both separators) cannot cross-match
 *  a different file. */
export function hasExactRelMention(text: string, rel: string): boolean {
  return tokenRegex(rel).test(text) || tokenRegex(rel.replace(/\//g, '\\')).test(text)
}

/** Markdown-safe destination for a local image path.
 *
 *  Raw paths break `![image](path)` in several ways (issue #3497):
 *  - CommonMark treats `\` before ASCII punctuation as an escape, so
 *    `C:\Users\me\.kiro\…` parses with `\.` collapsed to `.` — a mangled
 *    path. Windows accepts `/` in every file API, so drive-letter and UNC
 *    paths are emitted in forward-slash form (`\\host\share` → `//host/share`).
 *  - Whitespace or `(`/`)` ends a plain destination, and `<`, `>`, `\`
 *    terminate or escape inside CommonMark's `<…>` form.
 *
 *  The `<…>` wrap is also the PROVENANCE MARKER consumers key their decode
 *  on: a destination is emitted either as a conservative passthrough-safe
 *  subset (micromark leaves it byte-identical, decode is the identity) or
 *  wrapped — with `%` escaped to `%25` and `\`, `<`, `>` backslash-escaped —
 *  so exactly the wrapped form is percent-decoded after parsing. An unwrapped
 *  destination outside the safe subset can only be pre-existing history,
 *  which consumers must preserve verbatim (a legacy file literally named
 *  `photo%20copy.png` must not decode to `photo copy.png`).
 */
export function mdImageDest(p: string): string {
  const normalized = normalizeWindowsPath(p)
  if (/^[\w/.@:~-]*$/.test(normalized) && !normalized.includes('%')) return normalized
  const escaped = normalized.replace(/%/g, '%25').replace(/[\\<>]/g, c => '\\' + c)
  return `<${escaped}>`
}

/** Syntactic inverse of mdImageDest's `<…>` wrap: unwrap the angle brackets
 *  and undo the `\`, `<`, `>` escapes. Does NOT percent-decode — use
 *  mdImageDestToPath for the full inverse. */
export function unwrapMdImageDest(dest: string): string {
  const m = dest.match(/^<([\s\S]*)>$/)
  return m ? m[1].replace(/\\([\\<>])/g, '$1') : dest
}

/** Full inverse of mdImageDest for consumers that read the RAW markdown
 *  (pinned-prompt thumbnails, Mochi's sent-bubble parser): a `<…>`-wrapped
 *  destination is producer-emitted, so unwrap and percent-decode it; an
 *  unwrapped destination is either the passthrough-safe subset (decode is
 *  the identity, so skipping it changes nothing) or pre-existing history
 *  that must be preserved VERBATIM — a legacy file literally named
 *  `photo%20copy.png` must not decode to `photo copy.png`. */
export function mdImageDestToPath(dest: string): string {
  if (!/^<[\s\S]*>$/.test(dest)) return dest
  return decodeLocalPath(unwrapMdImageDest(dest))
}

export function prepareSendPayload(raw: string, pendingFiles: string[]): SendPayload {
  // All pending files (uploaded via button/drag-drop) are always included.
  // The @-token in text is used for display replacement, not as a gate.
  const files = [...new Set(pendingFiles)]
  const imgPaths = files.filter(p => IMG_EXT.test(p))
  const filePaths = files.filter(p => !IMG_EXT.test(p))
  const imgMd = imgPaths.map(p => `![image](${mdImageDest(p)})`).join('\n')
  const relMap = buildRelMap(files, raw)

  // Assign sequential indices to all non-image files, ordered by upload order.
  // Referenced files get lower indices, unreferenced get higher — but indices
  // may not be monotonically increasing in the rendered text if @-mentions
  // appear in a different order than the upload order.
  const referencedPaths = new Set([...relMap.values()])
  // Keep metadata in the same order as token numbers so backend consumers can
  // resolve [attached_file N] directly without scanning every path.
  const indexedFilePaths = [
    ...filePaths.filter(p => referencedPaths.has(p)),
    ...filePaths.filter(p => !referencedPaths.has(p)),
  ]
  const idxMap = new Map(indexedFilePaths.map((p, i) => [p, i + 1]))

  const llmRaw = replaceTokens(
    replaceTokens(raw, imgPaths, relMap, () => ''),
    filePaths, relMap, (p) => `[attached_file ${idxMap.get(p) ?? 0}] ${p}`,
  )
  const unreferenced = filePaths.filter(p => !referencedPaths.has(p))
  const unreferencedTokens = unreferenced.map(p => `[attached_file ${idxMap.get(p) ?? 0}] ${p}`).join('\n')
  const displayRaw = replaceTokens(raw, imgPaths, relMap, () => '')

  // Separate the pasted-image markdown from the typed text with a blank line
  // (a Markdown paragraph break) so the image renders in its own block and the
  // text drops to the next line, instead of flowing inline after the image (a
  // single '\n' is only a soft break). Applied to BOTH the LLM-facing `txt`
  // and the UI-facing `displayTxt`, so the *persisted* message keeps the break
  // on every surface that replays stored content — dashboard re-render after a
  // turn, gateway restart, Slack replay, exports — not just the in-memory
  // optimistic bubble. The extra blank line is safe for image attachment: the
  // ACP path (kiro-cli) extracts images in AcpClient._send_prompt by matching
  // the absolute file path and inlines them as a base64 `image` content block.
  // It is newline-agnostic and pulls the image into its own content block, so
  // the surrounding whitespace never changes what the model receives. The
  // caption keeps a single '\n' to its appended [attached_file N] tokens.
  const textBody = [llmRaw, unreferencedTokens].filter(Boolean).join('\n')
  return {
    txt: [imgMd, textBody].filter(Boolean).join('\n\n'),
    displayTxt: [imgMd, displayRaw].filter(Boolean).join('\n\n'),
    filePaths: indexedFilePaths,
    imgPaths,
  }
}

/** Producer-form image markdown line: `![image](dest)` alone on its line,
 *  where `dest` is exactly what mdImageDest emits — the conservative
 *  passthrough-safe subset, or the `<…>`-wrapped escaped form. Anchored to
 *  whole lines so an image the user wove into a sentence is left alone. */
/** One producer image line, sans anchors — a regex LITERAL so the word-bearing
 *  pattern text never sits in a string constant (i18n strict gate). */
const IMG_LINE_INNER = /!\[image\]\(((?:<(?:\\.|[^\\>])*>)|[\w/.@:~-]+)\)/
/** One producer image line, anchored to the whole string (per-line re-exec). */
const IMG_LINE_RE = new RegExp(`^${IMG_LINE_INNER.source}$`)
/** The producer's whole image block: `txt = [imgMd, textBody].join('\n\n')`
 *  puts every image line at the very START of the content, one per line,
 *  terminated by the join's blank line (or end of content when the body is
 *  empty). The terminator is consumed by the match so removing the block
 *  leaves the body byte-exact — including a body that itself begins with a
 *  newline (an expanded paste). */
const IMG_BLOCK_RE = new RegExp(`^(?:${IMG_LINE_INNER.source})(?:\n(?:${IMG_LINE_INNER.source}))*(?:\n\n|$)`)

/** A path shape the send path could actually have serialized: absolute POSIX
 *  (which also covers the producer's forward-slashed UNC form) or a Windows
 *  drive-letter path. Upload and picker paths are absolute, so a marker whose
 *  path is relative cannot be producer output — it is foreign text (a pasted
 *  transcript, a knowledge block) and must be left verbatim. */
const RESTORABLE_PATH_RE = /^(?:[/\\]|[A-Za-z]:[/\\])/

/** Composer state recovered from a queued message's serialized content. */
export interface RestoredComposerState {
  /** The typed text, with provably-lossless attachment markers stripped. */
  text: string
  /** Attachment paths (images included) to re-stage into pendingFiles. */
  files: string[]
}

/**
 * FALLBACK inverse of prepareSendPayload's attachment serialization, for
 * restoring a cancelled queued message into the composer.
 *
 * The PRIMARY restore path is ChatPage's send-side stash: `send()` records
 * the pre-serialization composer state ({typed text, staged files}) keyed by
 * the exact queued content, and `handleCancelQueued` restores from it
 * losslessly for every path shape. This parser covers the cases the stash
 * cannot — a reload, another tab, or a queue entry edited after send — and
 * its contract is strict: claim ONLY what is provably lossless, leave
 * everything else verbatim (never worse than the verbatim restore the base
 * behavior was).
 *
 * Provably lossless claims, and nothing more:
 *  - The producer's LEADING image block — `![image](dest)` lines at the very
 *    start of the content, one per line, ending at the `\n\n` paragraph
 *    break `prepareSendPayload` joins with (or at end of content). Claimed
 *    all-or-nothing: every line must recover an absolute image path, since
 *    the producer never emits anything else there. mdImageDest's `<…>` wrap
 *    makes each destination boundary exact, spaces included. An own-line
 *    image ANYWHERE ELSE is the user's own markdown and stays verbatim —
 *    position alone distinguishes producer output from user content.
 *  - An own-line `[attached_file N] <token>` whose remainder is a single
 *    whitespace-free token, with N ≥ 1 (the producer indexes from 1) and N
 *    unclaimed (the producer emits each index once) and the path absolute.
 *    Both possible readings — whole-line path vs path-plus-prose — are
 *    identical for this shape, so stripping the line and re-staging the path
 *    cannot corrupt either. The line vanishes; the path re-stages.
 *
 * Deliberately left VERBATIM, because their path boundary is not provable
 * from the wire text alone (any whitespace-bounded capture can truncate a
 * spaced path, staging a nonexistent file and re-sending the wrong one):
 *  - embedded (@-mention) markers sitting inline in prose;
 *  - own-line markers whose remainder contains whitespace — equally a spaced
 *    bare-upload path and a line-start mention followed by prose;
 *  - `[attached_dir N]` markers (always inline in prose);
 *  - marker-shaped text with relative paths, N ≤ 0, or duplicate N.
 * Claims are held in a list, never an N-indexed array, so malformed marker
 * text cannot build a sparse structure that throws mid-cancel and breaks
 * cancel entirely. Expanded paste blocks, knowledge blocks, and session-ref
 * links also stay in the text — their collapsed forms lived in drafts that
 * were cleared on send.
 *
 * The shape rules are only CANDIDATE generators. The final arbiter is a
 * byte-exact round trip: the claim stands only when re-serializing the
 * restored state (`prepareSendPayload(text, files).txt`) reproduces the
 * original content exactly; otherwise everything stays verbatim. That is
 * the literal definition of lossless, and it rejects what shape rules
 * cannot see locally — e.g. an own-line @-mention marker mid-text, which
 * would re-serialize as an appended token and reorder the user's words
 * around the attachment.
 *
 * Lossless inversion of EVERY shape needs attachment metadata on queue
 * entries — a backend schema change tracked in #5594 — after which this
 * parser can retire to legacy-entry duty.
 */
export function restoreQueuedContent(content: string): RestoredComposerState {
  const files: string[] = []
  let text = content

  // Image lines are claimed ONLY as the producer's leading block, and only
  // all-or-nothing: prepareSendPayload never emits an image line anywhere
  // else, and never emits one with a relative or non-image path — so a block
  // failing either test is foreign text (the user's own markdown) and stays
  // verbatim, as does an own-line image later in the content. The block match
  // consumes its own `\n\n` terminator, so nothing is stripped afterwards.
  const block = IMG_BLOCK_RE.exec(content)
  if (block) {
    const lines = block[0].replace(/\n+$/, '').split('\n')
    const paths = lines.map((l) => mdImageDestToPath(IMG_LINE_RE.exec(l)?.[1] ?? ''))
    if (paths.every((p) => IMG_EXT.test(p) && RESTORABLE_PATH_RE.test(p))) {
      files.push(...paths)
      text = content.slice(block[0].length)
    }
  }

  const claims: Array<{ path: string; matched: string }> = []
  const claimedN = new Set<number>()
  for (const m of text.matchAll(/^\[attached_file (\d+)\][^\S\n]+(\S+)[ \t]*$/gm)) {
    const n = parseInt(m[1], 10)
    if (n < 1 || claimedN.has(n) || !RESTORABLE_PATH_RE.test(m[2]) || IMG_EXT.test(m[2])) continue
    claimedN.add(n)
    claims.push({ path: m[2], matched: m[0] })
  }
  for (const c of claims) {
    const esc = c.matched.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
    // Remove the marker line together with exactly ONE adjacent newline — the
    // producer's own separator (`[llmRaw, tokens].join('\n')` appends trailing
    // markers, so a marker at end-of-content takes its LEADING newline
    // instead). Consuming the separator here is what lets the surrounding
    // user text survive byte-exact, leading/trailing whitespace included.
    text = text.replace(new RegExp(`^${esc}\\n|\\n?${esc}$`, 'm'), '')
    files.push(c.path)
  }

  // FINAL ARBITER — the definition of lossless, applied literally: a claim
  // stands only if re-serializing the restored state reproduces the original
  // content BYTE-FOR-BYTE. Shape rules above are only candidate generators;
  // this gate is what actually proves the round trip. It rejects what no
  // shape rule can see locally: a marker the producer put mid-text (an
  // own-line @-mention) re-serializes as an APPENDED token, reordering the
  // user's words around the attachment; an index that cannot renumber
  // identically; any residue the removals left. Anything that fails the
  // round trip stays fully verbatim — never worse than the base behaviour.
  const dedupedFiles = [...new Set(files)]
  if (dedupedFiles.length && prepareSendPayload(text, dedupedFiles).txt !== content) {
    return { text: content, files: [] }
  }
  return { text, files: dedupedFiles }
}

/* ------------------------------------------------------------------------- */
/* Folder references                                                          */
/* ------------------------------------------------------------------------- */
/* A folder reference lives in the composer as an `@rel/` token (trailing
 * slash), inserted by the file picker or typed by hand. Unlike a file there
 * is no upload and no side state: the token IS the reference. Staged chips,
 * the serialized `[attached_dir N] /abs/path` prompt marker, and the
 * sent-bubble chip all derive from it. This module owns that marker the same
 * way it owns `[attached_file N]`, so send() and renderUserContent() can
 * never disagree about the wire format. */

export interface DirToken {
  /** Relative path exactly as it appears in the token, WITH trailing slash. */
  rel: string
  /** The exact composer token including the leading `@`. */
  token: string
}

/** Boundary-checked folder token: `@` preceded by start/whitespace, a
 *  non-whitespace body ending in `/`, followed by whitespace/end. The body
 *  excludes `@` so an email-like `a@b.c/` never matches mid-word. */
const DIR_TOKEN_RE = /(^|\s)@([^\s@]*\/)(?=\s|$)/g

/** True when `rel` cannot be a folder reference: URLs (`://`) and
 *  slash-only bodies (`/`, `//`) carry no path segments to reference. */
function isNonDirRel(rel: string): boolean {
  return rel.includes('://') || /^[/\\]+$/.test(rel)
}

/** Extract folder tokens from composer text, deduped by rel, in appearance
 *  order. The single source of truth for staged folder chips: what this
 *  returns is exactly what will serialize on send. */
export function parseDirTokens(text: string): DirToken[] {
  const seen = new Set<string>()
  const out: DirToken[] = []
  for (const m of text.matchAll(DIR_TOKEN_RE)) {
    const rel = m[2]
    if (isNonDirRel(rel) || seen.has(rel)) continue
    seen.add(rel)
    out.push({ rel, token: `@${rel}` })
  }
  return out
}

/** Absolute form of a folder token's rel path, WITHOUT trailing slash.
 *  Separator-aware join mirroring makeRelative in FilePickerMenu: a Windows
 *  project root joins with `\`. A rel that is already absolute (POSIX `/x` or
 *  Windows `C:\x` — the picker falls back to the absolute path when a result
 *  lies outside the project root) is returned as-is. */
export function dirFullPath(rel: string, project: string): string {
  const trimmed = rel.replace(/[/\\]+$/, '')
  if (/^([/\\]|[A-Za-z]:[/\\])/.test(trimmed) || !project) return trimmed || rel
  const sep = project.includes('\\') && !project.includes('/') ? '\\' : '/'
  return project.replace(/[/\\]+$/, '') + sep + trimmed
}

/** Splice `@rel/` folder tokens into composer text — the drop-a-folder
 *  counterpart of the picker's applyPickedToken insertion, sharing the exact
 *  token grammar chips and serialization already parse. Each rel gets the
 *  trailing slash the picker's selectionFor guarantees, rels whose token is
 *  already present in `value` are skipped (parseDirTokens dedupes chips, but
 *  a duplicate token in the TEXT would still read twice), and the tokens are
 *  inserted at `caret` — or appended when the caret is unknown (`null`), the
 *  same append fallback the dictation splice uses for a never-touched
 *  composer. Whitespace-padded on both sides so the token stays
 *  boundary-checked per DIR_TOKEN_RE. Returns the new value, the caret
 *  offset just past the inserted run, and `changed` — false when every rel
 *  was a duplicate, so callers can skip state/caret updates entirely (arming
 *  a caret restore against an unchanged value would leave it stale until an
 *  unrelated edit fires it). */
export function spliceDirTokens(
  value: string,
  caret: number | null,
  rels: string[],
): { value: string; caret: number; changed: boolean } {
  // Exact-string dedupe. NOT separator-canonicalized: `\` is a legal POSIX
  // filename character, and this function only ever sees bare RELATIVE
  // tokens with no platform context to confirm a `\` is a Windows separator
  // rather than part of a literal name (`src/a\b/` vs `src/a/b/` are then
  // genuinely different directories). A caller that CAN prove Windows shape
  // (an absolute path with a drive-letter/UNC prefix, via normalizeWindowsPath)
  // owns that widened comparison itself -- see handleAddToContext (ChatPage.tsx).
  const existing = new Set(parseDirTokens(value).map(t => t.rel))
  const fresh: string[] = []
  for (const raw of rels) {
    // Match selectionFor in FilePickerMenu: append `/` unless the rel already
    // ends in either separator, so a Windows path is not given a second one.
    const rel = /[/\\]$/.test(raw) ? raw : raw + '/'
    if (existing.has(rel) || fresh.includes(rel)) continue
    fresh.push(rel)
  }
  const at = caret == null ? value.length : Math.max(0, Math.min(caret, value.length))
  if (!fresh.length) return { value, caret: at, changed: false }
  const before = value.slice(0, at)
  const after = value.slice(at)
  // Pad so the token sits on whitespace boundaries: a leading space when text
  // precedes it, a trailing space always (the picker inserts `@rel/ ` too).
  const lead = before && !/\s$/.test(before) ? ' ' : ''
  const run = lead + fresh.map(r => `@${r}`).join(' ') + ' '
  return { value: before + run + after, caret: before.length + run.length, changed: true }
}

/** Serialize folder tokens for the LLM: each `@rel/` becomes
 *  `[attached_dir N] /abs/path` (N = 1-based appearance order; a repeated
 *  token gets the same N). Display text keeps the `@rel/` tokens — the same
 *  fresh-message split files use (`meta.files` + `@rel` display vs
 *  `[attached_file N]` wire form). Returns the ordered absolute paths for
 *  `meta.dirs`, so token N indexes dirPaths[N-1] losslessly on replay. */
export function serializeDirTokens(raw: string, project: string): { llm: string; dirPaths: string[] } {
  const tokens = parseDirTokens(raw)
  if (!tokens.length) return { llm: raw, dirPaths: [] }
  const dirPaths = tokens.map(t => dirFullPath(t.rel, project))
  let llm = raw
  tokens.forEach((t, i) => {
    const esc = t.token.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
    // Replacement via callback so the PATH is inserted literally: a template
    // string would let `$1`/`$&`/`$$` inside the path expand as replacement
    // patterns and corrupt the marker (same rule replaceTokens follows for
    // file paths).
    llm = llm.replace(new RegExp(`(^|\\s)${esc}(?=\\s|$)`, 'g'), (_m, pre: string) => `${pre}[attached_dir ${i + 1}] ${dirPaths[i]}`)
  })
  return { llm, dirPaths }
}

/** Parse folder paths from message meta or `[attached_dir N]` markers in
 *  content — the dir counterpart of parseFiles, with the same precedence:
 *  meta wins (lossless, ordered), markers are the no-meta history fallback. */
export function parseDirs(content: string, meta?: Record<string, unknown>): string[] {
  const metaDirs = (meta?.dirs || []) as string[]
  return metaDirs.length
    ? metaDirs
    : (content.match(/\[attached_dir \d+\] (\S+)/g) || []).map(s => s.replace(/\[attached_dir \d+\] /, ''))
}

export interface ResolvedDirSegment {
  /** Content with every `[attached_dir N] /path` marker rewritten to `@label/`. */
  display: string
  /** `label/` (without the leading @, WITH trailing slash) -> full path. */
  dirMentionMap: Map<string, string>
}

/** Rewrite `[attached_dir N] /path` markers back to `@label/` display tokens.
 *  The dir counterpart of resolveFileSegment's marker pass, sharing its
 *  lossless indexing rule: N indexes `orderedDirs` 1-based, so a path with
 *  spaces recovers verbatim when it sits at the marker position; the
 *  whitespace-bounded capture is only the no-meta fallback. Every folder
 *  reference renders inline (folders are path references, never upload
 *  cards), so there is no embedded/standalone split. Labels are
 *  basename-first, widened until unique via the shared buildFileLabels. */
export function resolveDirSegment(content: string, orderedDirs: string[]): ResolvedDirSegment {
  const dirMentionMap = new Map<string, string>()
  if (!orderedDirs.length && !content.includes('[attached_dir ')) {
    return { display: content, dirMentionMap }
  }
  // buildFileLabels splits on `/` only, so normalize Windows separators for
  // LABEL computation (a backslash path would otherwise be one giant
  // "segment" and label as the full absolute path). Map values and tooltips
  // keep the original path untouched.
  const norm = (p: string) => p.replace(/\\/g, '/')
  const labels = buildFileLabels(orderedDirs.map(norm))
  const markerRe = /\[attached_dir (\d+)\][^\S\n]+/g
  let display = ''
  let lastIdx = 0
  let m: RegExpExecArray | null
  while ((m = markerRe.exec(content)) !== null) {
    const n = parseInt(m[1], 10)
    const pathStart = m.index + m[0].length
    const indexed = n >= 1 && n <= orderedDirs.length ? orderedDirs[n - 1] : undefined
    let path: string
    let pathEnd: number
    if (indexed && content.startsWith(indexed, pathStart)) {
      path = indexed
      pathEnd = pathStart + indexed.length
    } else {
      const rest = content.slice(pathStart)
      const wsIdx = rest.search(/\s/)
      path = wsIdx === -1 ? rest : rest.slice(0, wsIdx)
      pathEnd = pathStart + path.length
    }
    const label = (labels.get(norm(path)) || path.split(/[/\\]/).pop() || path) + '/'
    dirMentionMap.set(label, path)
    display += content.slice(lastIdx, m.index) + `@${label}`
    lastIdx = pathEnd
    markerRe.lastIndex = pathEnd
  }
  display += content.slice(lastIdx)

  // Recover `@rel/` tokens already in display form (fresh optimistic bubble:
  // meta.dirs present, no markers). Match each token to its meta path by
  // suffix so the chip opens the right absolute path.
  for (const t of parseDirTokens(display)) {
    if (dirMentionMap.has(t.rel)) continue
    const relNoSlash = t.rel.replace(/[/\\]+$/, '')
    const hit = orderedDirs.find(p => {
      const norm = p.replace(/[/\\]+$/, '')
      return norm === relNoSlash || norm.endsWith('/' + relNoSlash) || norm.endsWith('\\' + relNoSlash)
    })
    if (hit) dirMentionMap.set(t.rel, hit)
  }
  return { display, dirMentionMap }
}
