import { deriveWidgetSlug } from './widgetSlug'

/**
 * Derive the artifact slug used by the backend for a markdown image impression.
 *
 * Keep this paired with `kiro_crew.image_artifacts._derive_image_slug`: the
 * backend namespaces the message id with `#image`, then applies the shared
 * widget slug function to the image's zero-based ordinal in the message.
 */
export function deriveImageArtifactSlug(messageTs: string, imageIndex: number): string {
  return deriveWidgetSlug(`${messageTs}#image`, imageIndex)
}

// Mirrors `IMAGE_MD_RE` in kiro_crew/messaging/outbound_files.py: a DIRECT
// image opener `![alt](`, alt may contain backslash-escaped characters.
const IMAGE_OPENER_RE = /!\[(?:\\.|[^\]\\])*\]\(/g
const IMAGE_OPENER_AT_RE = /^!\[(?:\\.|[^\]\\])*\]\(/
// `_MD_ESCAPABLE` there: a backslash escapes only these inside a destination,
// so a native Windows path keeps its separators.
const MD_ESCAPABLE = new Set(['(', ')', '[', ']', '\\', '<', '>', '"', "'"])

/**
 * The markdown destination in `rest`, the text right after `![alt](` — a port
 * of `md_destination` (`_walk_destination` + `_finish_destination`) in
 * kiro_crew/messaging/outbound_files.py. `null` when the destination never
 * closes or is malformed. Parity with the backend is the whole point: the
 * same text must yield the same key on both sides.
 */
export function parseMarkdownImageDestination(rest: string): string | null {
  let depth = 1
  let out = ''
  for (let i = 0; i < rest.length; i++) {
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
 */
export function buildImageOrdinalMap(raw: string): Map<string, readonly number[]> {
  const out = new Map<string, number[]>()
  IMAGE_OPENER_RE.lastIndex = 0
  let m: RegExpExecArray | null
  let index = 0
  while ((m = IMAGE_OPENER_RE.exec(raw)) !== null) {
    const dest = parseMarkdownImageDestination(raw.slice(m.index + m[0].length))
    if (dest !== null) {
      const list = out.get(dest)
      if (list) list.push(index)
      else out.set(dest, [index])
    }
    index++
  }
  return out
}

/** Number of direct image openers in `text` — every position the backend numbers. */
export function countImageOpeners(text: string): number {
  IMAGE_OPENER_RE.lastIndex = 0
  let n = 0
  while (IMAGE_OPENER_RE.exec(text) !== null) n++
  return n
}

/**
 * How many direct image openers with destination `dest` begin before `end` in
 * `text` — this node's occurrence index among same-destination images.
 */
function countImageOccurrencesBefore(text: string, dest: string, end: number): number {
  const prefix = text.slice(0, Math.max(0, end))
  IMAGE_OPENER_RE.lastIndex = 0
  let m: RegExpExecArray | null
  let count = 0
  while ((m = IMAGE_OPENER_RE.exec(prefix)) !== null) {
    // The destination may run past `prefix`; parse against the full text.
    if (parseMarkdownImageDestination(text.slice(m.index + m[0].length)) === dest) count++
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
  ordinals: ReadonlyMap<string, readonly number[]> | null,
  raw?: { message: string; blockStart: number; blockEnd: number },
): number[] {
  const dest = imageDestinationAt(text, offset)
  if (dest === null || !ordinals) return []
  const list = ordinals.get(dest)
  if (!list || list.length === 0) return []
  if (list.length === 1) return [list[0]]
  if (!raw) return []
  const before = countImageOccurrencesBefore(raw.message, dest, raw.blockStart)
  const inRawBlock = countImageOccurrencesBefore(raw.message, dest, raw.blockEnd) - before
  const inRendered = countImageOccurrencesBefore(text, dest, text.length)
  if (inRawBlock !== inRendered) return []
  const k = before + countImageOccurrencesBefore(text, dest, offset)
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
  const head = text.slice(Math.max(0, offset))
  const m = IMAGE_OPENER_AT_RE.exec(head)
  if (!m) return null
  return parseMarkdownImageDestination(head.slice(m[0].length))
}
