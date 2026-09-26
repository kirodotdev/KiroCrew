import { deriveIndexSlug } from './widgetSlug'

/**
 * Derive the artifact slug used by the backend for a markdown image impression.
 *
 * Keep this paired with `kiro_crew.image_artifacts._derive_image_slug`: the
 * backend namespaces the message id with `#image`, then applies the shared
 * ordinal slug function to the image's zero-based ordinal in the message.
 */
export function deriveImageArtifactSlug(messageTs: string, imageIndex: number): string {
  return deriveIndexSlug(`${messageTs}#image`, imageIndex)
}

// Mirrors `IMAGE_MD_RE` in kiro_crew/messaging/outbound_files.py: a DIRECT
// image opener `![alt](`, alt may contain backslash-escaped characters.
const IMAGE_OPENER_RE = /!\[(?:\\.|[^\]\\])*\]\(/g
const IMAGE_OPENER_AT_RE = /!\[(?:\\.|[^\]\\])*\]\(/y
// `_MD_ESCAPABLE` there: a backslash escapes only these inside a destination,
// so a native Windows path keeps its separators.
const MD_ESCAPABLE = new Set(['(', ')', '[', ']', '\\', '<', '>', '"', "'"])

/**
 * Longest destination the scanner will walk before giving up on an opener.
 * A destination is a path or URL; a local path longer than PATH_MAX (4096 on
 * Linux) cannot resolve, so the backend never stores an image behind one, and
 * a URL that long is not a raster the transcript can show. The bound is what
 * keeps the scan linear: an unterminated `![alt](` no longer costs the rest of
 * the message, so a message full of them costs `openers * 4096`, not
 * `openers * length`. Ordinals are unaffected — every opener is still numbered
 * (see buildImageOrdinalMap); only its destination becomes unknown.
 */
export const MAX_IMAGE_DESTINATION_CHARS = 4096

/**
 * The markdown destination in `rest`, the text right after `![alt](` — a port
 * of `md_destination` (`_walk_destination` + `_finish_destination`) in
 * kiro_crew/messaging/outbound_files.py. `null` when the destination never
 * closes, is malformed, or runs past MAX_IMAGE_DESTINATION_CHARS. Parity with
 * the backend is the whole point: the same text must yield the same key on
 * both sides for every destination the backend can store.
 */
export function parseMarkdownImageDestination(rest: string): string | null {
  let depth = 1
  let out = ''
  const limit = Math.min(rest.length, MAX_IMAGE_DESTINATION_CHARS)
  for (let i = 0; i < limit; i++) {
    const ch = rest[i]
    if (ch === '\\' && i + 1 < rest.length && MD_ESCAPABLE.has(rest[i + 1])) {
      out += rest[i + 1]
      i++
      continue
    }
    if (ch === '(') depth++
    else if (ch === ')') {
      depth--
      if (depth === 0) return finishDestination(out)
    }
    out += ch
  }
  return null
}

function finishDestination(raw: string): string | null {
  if (raw.includes('\r') || raw.includes('\n')) return null
  let dest = raw.trim()
  if (dest.startsWith('<')) {
    const end = dest.indexOf('>')
    if (end === -1) return null
    dest = dest.slice(1, end).trim()
  } else if (dest.includes(' ') || dest.includes('\t')) {
    dest = dest.split(/[ \t]/, 1)[0]
  }
  for (const ch of dest) {
    const code = ch.charCodeAt(0)
    if (code < 32 || code === 127) return null
  }
  return dest || null
}

/** One direct image opener in a scanned text: where it starts and what it points at. */
export interface ImageOpener {
  readonly start: number
  readonly dest: string | null
}

/**
 * Every direct image opener in `text`, in order, each with its (bounded)
 * destination. ONE pass: the opener regex advances left to right and each
 * destination walk is capped, so the cost is linear in the text. Everything
 * that needs to count or locate openers reads this list instead of rescanning.
 */
export function scanImageOpeners(text: string): ImageOpener[] {
  const out: ImageOpener[] = []
  IMAGE_OPENER_RE.lastIndex = 0
  let m: RegExpExecArray | null
  while ((m = IMAGE_OPENER_RE.exec(text)) !== null) {
    const after = m.index + m[0].length
    out.push({
      start: m.index,
      dest: parseMarkdownImageDestination(text.slice(after, after + MAX_IMAGE_DESTINATION_CHARS)),
    })
  }
  return out
}

/**
 * Destination → zero-based ordinals of every direct image in the RAW message
 * text, numbered exactly as `register_images` numbers them (position among
 * ALL openers, whether or not each one was stored). Keyed by destination
 * rather than position because the renderer preprocesses each block (strips
 * stray protocol tags, repairs fences…) before it sees node offsets, so no
 * position in the rendered tree can be trusted to line up with the raw scan.
 * A destination that appears more than once lists each ordinal in order: the
 * backend normally copies the same bytes under each, but a replay after a
 * per-message budget stop can store a LATER ordinal from a since-changed
 * file, so the caller matches its own occurrence and falls through the rest.
 *
 * The map also carries the scan itself (`openers`, ordinal → opener), so a
 * caller locating an occurrence by raw position never rescans the message.
 */
export class ImageOrdinalMap extends Map<string, readonly number[]> {
  constructor(readonly openers: readonly ImageOpener[]) {
    super()
    openers.forEach((opener, ordinal) => {
      if (opener.dest === null) return
      const list = this.get(opener.dest) as number[] | undefined
      if (list) list.push(ordinal)
      else this.set(opener.dest, [ordinal])
    })
  }
}

export function buildImageOrdinalMap(raw: string): ImageOrdinalMap {
  return new ImageOrdinalMap(scanImageOpeners(raw))
}

/** Number of direct image openers in `text` — every position the backend numbers. */
export function countImageOpeners(text: string): number {
  IMAGE_OPENER_RE.lastIndex = 0
  let n = 0
  while (IMAGE_OPENER_RE.exec(text) !== null) n++
  return n
}

/**
 * How many openers with destination `dest` begin before `end` — an
 * occurrence index among same-destination images, read off a finished scan.
 */
function countOccurrencesBefore(openers: readonly ImageOpener[], dest: string, end: number): number {
  let count = 0
  for (const opener of openers) {
    if (opener.start >= end) break
    if (opener.dest === dest) count++
  }
  return count
}

/**
 * Artifact ordinals to try for the image at `offset` in the rendered block
 * text, best candidate first. Empty when the node is not a direct
 * `![alt](dest)` opener, the destination is unknown to the map, or the exact
 * ordinal cannot be determined.
 *
 * Exact or nothing: a wrong artifact is worse than the broken-image chip, so
 * this never guesses. A destination that appears ONCE in the raw message is
 * unambiguous. A repeated destination needs the block's raw span (`raw`), and
 * is resolved only when the raw slice of this block holds the same number of
 * `dest` openers as the rendered block text — nothing was stripped or added by
 * preprocessing — so the node's in-block occurrence maps onto the raw
 * occurrences before the block. Copies stored for the same destination are
 * appended as fallbacks AFTER the exact ordinal (they normally hold identical
 * bytes; they are only reached when the exact copy is missing).
 */
export function imageOrdinalCandidates(
  text: string,
  offset: number,
  ordinals: ImageOrdinalMap | null,
  raw?: { message: string; blockStart: number; blockEnd: number },
): number[] {
  const dest = imageDestinationAt(text, offset)
  if (dest === null || !ordinals) return []
  const list = ordinals.get(dest)
  if (!list || list.length === 0) return []
  if (list.length === 1) return [list[0]]
  if (!raw) return []
  // Raw occurrences come off the message's finished scan; the rendered block is
  // scanned once here (it is one block, not the message) — no per-node rescan
  // of the message and no suffix walk past the destination bound.
  const rendered = scanImageOpeners(text)
  const before = countOccurrencesBefore(ordinals.openers, dest, raw.blockStart)
  const inRawBlock = countOccurrencesBefore(ordinals.openers, dest, raw.blockEnd) - before
  const inRendered = countOccurrencesBefore(rendered, dest, text.length)
  if (inRawBlock !== inRendered) return []
  const k = before + countOccurrencesBefore(rendered, dest, offset)
  if (k >= list.length) return []
  const exact = list[k]
  return [exact, ...list.filter(i => i !== exact)]
}

/**
 * The destination of the direct image opener that begins exactly at `offset`
 * in `text`, or `null` when the text there is not a direct `![alt](` opener
 * (a reference-style image, for instance, which the backend never registers).
 */
export function imageDestinationAt(text: string, offset: number): string | null {
  const at = Math.max(0, offset)
  IMAGE_OPENER_AT_RE.lastIndex = at
  const m = IMAGE_OPENER_AT_RE.exec(text)
  if (!m) return null
  const after = at + m[0].length
  return parseMarkdownImageDestination(text.slice(after, after + MAX_IMAGE_DESTINATION_CHARS))
}
