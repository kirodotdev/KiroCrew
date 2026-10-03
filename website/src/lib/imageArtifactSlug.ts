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

/**
 * Mirrors `IMAGE_MD_RE` in kiro_crew/messaging/outbound_files.py
 * (`!\[((?:[^\]\\]|\\.)*)\]\(`): a DIRECT image opener `![alt](` whose alt may
 * contain backslash-escaped characters. Hand-walked rather than a regex so the
 * scan is ONE pass over the text: a regex retries at every position after a
 * failed attempt, and a message full of `![` with no closing `]` (ordinary
 * persisted text, re-frozen on every reload) makes that quadratic in the alt
 * text. After an attempt at `at` fails at position `fail`, no attempt starting
 * inside `(at, fail)` can succeed: every such start walks the same alt
 * characters with the same escape alignment to the same `]` (or end), so the
 * scanner resumes at `fail` and finds exactly the openers the regex finds.
 *
 * Returns the index just past `](` for an opener beginning at `at`, or the
 * negated index (minus one) at which the attempt failed, so `-1` means it failed
 * at position 0 and a non-opener start fails at `at + 1`.
 */
function walkImageOpener(text: string, at: number): number {
  if (text.charCodeAt(at) !== 0x21 /* ! */ || text.charCodeAt(at + 1) !== 0x5b /* [ */) {
    return -(at + 1) - 1
  }
  let i = at + 2
  const n = text.length
  while (i < n) {
    const c = text.charCodeAt(i)
    if (c === 0x5c /* \\ */) {
      // `\\.`: an escape needs a following character that `.` matches, and the
      // `.` that counts is PYTHON's (the backend's `IMAGE_MD_RE` numbers the
      // ordinals): it excludes only LF, so `\\`+CR, U+2028 and U+2029 keep the
      // alt alive here as they do there. A JS regex `.` would exclude all four
      // and shift every later ordinal by one against the backend.
      const next = i + 1 < n ? text.charCodeAt(i + 1) : -1
      if (next === -1 || next === 0x0a) {
        return -Math.max(at + 1, i) - 1
      }
      i += 2
      continue
    }
    if (c === 0x5d /* ] */) {
      return text.charCodeAt(i + 1) === 0x28 /* ( */ ? i + 2 : -(i + 1) - 1
    }
    i++
  }
  return -n - 1
}
// `_MD_ESCAPABLE` there: a backslash escapes only these inside a destination,
// so a native Windows path keeps its separators.
const MD_ESCAPABLE = new Set(['(', ')', '[', ']', '\\', '<', '>', '"', "'"])

/**
 * Longest destination the scanner will walk before giving up on an opener.
 * A destination is a local path the backend could store an image behind, and
 * the longest such path is a Windows extended-length path: 32,767 characters,
 * plus the closing `)`. Linux PATH_MAX is 4,096, so the bound covers every
 * platform the backend registers on; a destination past it is not a file any
 * supported filesystem can open. The bound is what keeps the scan linear: an
 * unterminated `![alt](` costs at most this much, so a message full of them
 * costs `openers * 32768`, not `openers * length`. Ordinals are unaffected —
 * every opener is still numbered (see buildImageOrdinalMap); only its
 * destination becomes unknown.
 */
export const MAX_IMAGE_DESTINATION_CHARS = 32768

/**
 * The markdown destination that begins at `start` in `text`, the position
 * right after `![alt](` — a port of `md_destination` (`_walk_destination` +
 * `_finish_destination`) in kiro_crew/messaging/outbound_files.py. `null`
 * when the destination never closes, is malformed, or runs past
 * MAX_IMAGE_DESTINATION_CHARS. Parity with the backend is the whole point:
 * the same text must yield the same key on both sides for every destination
 * the backend can store.
 *
 * The walk reads `text` in place: nothing is copied until the destination
 * closes, and then only the destination itself. A caller scanning a message
 * dense with openers therefore pays for the destinations it finds, never a
 * fixed 32 KiB slice per opener.
 */
export function parseMarkdownImageDestination(text: string, start = 0): string | null {
  let depth = 1
  const n = text.length
  const end = Math.min(n, start + MAX_IMAGE_DESTINATION_CHARS)
  // Pieces of the destination with escapes resolved; `segStart` is the start
  // of the piece not yet pushed. Left untouched on the no-escape fast path.
  let pieces: string[] | null = null
  let segStart = start
  for (let i = start; i < end; i++) {
    const ch = text[i]
    if (ch === '\\' && i + 1 < n && MD_ESCAPABLE.has(text[i + 1])) {
      if (pieces === null) pieces = []
      pieces.push(text.slice(segStart, i), text[i + 1])
      i++
      segStart = i + 1
      continue
    }
    if (ch === '(') depth++
    else if (ch === ')') {
      depth--
      if (depth === 0) {
        const tail = text.slice(segStart, i)
        return finishDestination(pieces === null ? tail : pieces.join('') + tail)
      }
    }
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
  const n = text.length
  let i = text.indexOf('![')
  while (i !== -1 && i < n) {
    const r = walkImageOpener(text, i)
    if (r >= 0) {
      out.push({
        start: i,
        dest: parseMarkdownImageDestination(text, r),
      })
      i = text.indexOf('![', r)
    } else {
      i = text.indexOf('![', -r - 1)
    }
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
 * file, so the caller matches its own occurrence and tries no other.
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
 * occurrences before the block. The result is at most ONE ordinal: the other
 * copies stored for the same destination are never tried, because a source
 * file rewritten between two captures of one message leaves them holding
 * different bytes, and a stale picture is a worse answer than the chip.
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
  return [list[k]]
}

/**
 * The destination of the direct image opener that begins exactly at `offset`
 * in `text`, or `null` when the text there is not a direct `![alt](` opener
 * (a reference-style image, for instance, which the backend never registers).
 */
export function imageDestinationAt(text: string, offset: number): string | null {
  const at = Math.max(0, offset)
  const after = walkImageOpener(text, at)
  if (after < 0) return null
  return parseMarkdownImageDestination(text, after)
}
