/** Shared file-token utilities used by send() and renderUserContent(). */

export const IMG_EXT = /\.(png|jpe?g|gif|webp|bmp|svg)$/i

/** Boundary-aware regex for @token matching. Prevents `@foo.ts` from matching inside `@foo.tsx`. */
function tokenRegex(token: string, flags = ''): RegExp {
  const escaped = token.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
  return new RegExp(`@${escaped}(?=\\s|$)`, flags)
}

/** Parse file paths from message meta or [attached_file N] patterns in content. */
export function parseFiles(content: string, meta?: Record<string, unknown>): string[] {
  const metaFiles = (meta?.files || []) as string[]
  return metaFiles.length
    ? metaFiles
    : (content.match(/\[attached_file \d+\] (\S+)/g) || []).map(s => s.replace(/\[attached_file \d+\] /, ''))
}

/** Per-path display label: basename, disambiguated to the last-2 path segments
 *  when two paths share a basename (e.g. two `report.docx` in different dirs). */
export function buildFileLabels(paths: string[]): Map<string, string> {
  const basenames = paths.map(p => p.split('/').pop() || p)
  const dupes = new Set(basenames.filter((n, i) => basenames.indexOf(n) !== i))
  const map = new Map<string, string>()
  for (const p of paths) {
    const name = p.split('/').pop() || p
    map.set(p, dupes.has(name) ? p.split('/').slice(-2).join('/') : name)
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
    result = result.replace(tokenRegex(rel, 'g'), () => replacer(p, i))
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

export function prepareSendPayload(raw: string, pendingFiles: string[]): SendPayload {
  // All pending files (uploaded via button/drag-drop) are always included.
  // The @-token in text is used for display replacement, not as a gate.
  const files = [...new Set(pendingFiles)]
  const imgPaths = files.filter(p => IMG_EXT.test(p))
  const filePaths = files.filter(p => !IMG_EXT.test(p))
  const imgMd = imgPaths.map(p => `![image](${p})`).join('\n')
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
