// Split a leading YAML frontmatter block off a markdown document for the FILE
// PREVIEW surface, so the preview shows it as a metadata strip instead of
// mis-parsing the `---` fence as a GFM thematic break + setext heading. The
// chat MarkdownRenderer does not call this; it is reached only through
// ContentRenderer's frontmatter split.
//
// The document must open with a `---` line and carry a later line that is
// exactly `---` to close the block. Parsing is deliberately tolerant and
// read-only — a display surface never rewrites the file — and preserves key
// order so the strip reads top-to-bottom as authored.
//
// This module owns the whole preview primitive: the fence scanner
// (`findFrontmatterBlock`), the tolerant map parse (`parseFrontmatterMap`), the
// display flattener (`flattenValue`), and the `splitFrontmatter` adapter that
// builds the strip's ordered entries plus a body padded so every body line
// keeps its true source line number (see `body` below). The skill editor keeps
// its own frontmatter handling (`SkillForm.splitBlock`) because its save/splice
// path must preserve the exact source bytes it did not touch; nothing here is
// shared with it.
import { isMap, isScalar, isSeq, parseDocument } from 'yaml'
import type { Node } from 'yaml'

/** The boundaries of a leading frontmatter block within the raw document.
 *  All offsets are character indices into the original `raw` string, so a
 *  caller can slice whatever shape it needs (trimmed body, padded body, the
 *  block text) without re-running the fence scan. */
export interface FrontmatterBlock {
  /** The YAML text between the fences (no fence lines, no surrounding newlines). */
  block: string
  /** Index in `raw` where the body begins (the char after the closing fence's
   *  terminating line break, or end-of-string when the block ends the file). */
  bodyStart: number
  /** Number of source lines the block occupied, fences included — i.e. the line
   *  number (1-based) at which the body's first line sits in the full document.
   *  The preview pads the body with this many blank lines so every body line
   *  keeps its true source line number. */
  blockLineCount: number
}

// Opening fence: `---` on its own first line (trailing spaces/tabs and CR
// tolerated). Closing fence: a line that is exactly `---` (same tolerance),
// preceded by a newline, followed by a line break or end-of-document.
const OPENING_FENCE = /^---[ \t]*\r?\n/
const CLOSING_FENCE = /\r?\n---[ \t]*(\r?\n|$)/

/** Find a leading frontmatter block, or null when the document does not open
 *  with a well-formed `--- … ---` block. Pure boundary finding — no YAML parse. */
export function findFrontmatterBlock(raw: string): FrontmatterBlock | null {
  if (!OPENING_FENCE.test(raw)) return null
  const openEnd = raw.indexOf('\n') + 1
  const rest = raw.slice(openEnd)
  const close = CLOSING_FENCE.exec(rest)
  if (!close) return null

  const block = rest.slice(0, close.index)
  const bodyStart = openEnd + close.index + close[0].length
  // Lines consumed before the body = newlines in raw up to bodyStart. The body's
  // first line is therefore 1-based line (blockLineCount + 1) in the full doc.
  const consumed = raw.slice(0, bodyStart)
  const blockLineCount = (consumed.match(/\n/g)?.length ?? 0)
  return { block, bodyStart, blockLineCount }
}

/** Parse a block's YAML as a map, tolerantly, or null when it is not a map.
 *  A display surface never rejects content it can show, so this is lenient. */
export function parseFrontmatterMap(block: string) {
  const doc = parseDocument(block)
  return isMap(doc.contents) ? doc.contents : null
}

/** Render a parsed YAML node as a single display string. Scalars render verbatim;
 *  sequences join with `, `; maps and anything else fall back to compact one-line
 *  YAML so no value is ever silently dropped. */
export function flattenValue(node: Node | null): string {
  if (node == null) return ''
  if (isScalar(node)) return node.value == null ? '' : String(node.value)
  if (isSeq(node)) return node.items.map((it) => flattenValue(it as Node)).join(', ')
  try {
    return node.toString().replace(/\s*\n\s*/g, ' ').trim()
  } catch {
    return ''
  }
}

export interface FrontmatterEntry {
  key: string
  /** The value rendered as a single display string. Scalars render verbatim;
   *  sequences join with `, `; maps and anything else fall back to compact
   *  one-line YAML so no value is dropped. */
  value: string
}

export interface FrontmatterSplit {
  /** Ordered key/value entries, empty when there is no frontmatter block. */
  entries: FrontmatterEntry[]
  /** The markdown body the preview renders. When a block was stripped this is
   *  the body PADDED with as many leading blank lines as the block occupied, so
   *  every body line keeps the same 1-based source line number it has in the
   *  full document. That alignment is load-bearing: the preview renders this
   *  string with `sourcePos`, but the inline-comment resolver indexes the full
   *  file, so an unpadded body would shift every `data-sourcepos` line up by the
   *  block's height and anchor body comments to the wrong source line. Leading
   *  blank lines render as nothing, so the preview looks unchanged. Equals the
   *  input verbatim when there is no block. */
  body: string
  /** True only when a well-formed block was found and parsed as a YAML map. */
  hasFrontmatter: boolean
}

const NO_FRONTMATTER: FrontmatterSplit = { entries: [], body: '', hasFrontmatter: false }

export function splitFrontmatter(raw: string): FrontmatterSplit {
  const found = findFrontmatterBlock(raw)
  if (!found) return { ...NO_FRONTMATTER, body: raw }

  const map = parseFrontmatterMap(found.block)
  if (!map) return { ...NO_FRONTMATTER, body: raw }

  const entries: FrontmatterEntry[] = []
  for (const pair of map.items) {
    const key = isScalar(pair.key) ? String(pair.key.value) : String(pair.key)
    entries.push({ key, value: flattenValue(pair.value as Node | null) })
  }
  if (entries.length === 0) return { ...NO_FRONTMATTER, body: raw }

  // Pad the body with one blank line per stripped source line so body line
  // numbers stay aligned with the full document (see `body` doc above).
  const rawBody = raw.slice(found.bodyStart)
  const body = '\n'.repeat(found.blockLineCount) + rawBody
  return { entries, body, hasFrontmatter: true }
}
