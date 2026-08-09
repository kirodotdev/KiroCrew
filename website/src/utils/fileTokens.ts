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
