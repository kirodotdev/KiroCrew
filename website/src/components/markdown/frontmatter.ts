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
// This module owns only what the preview adds on top of the shared reader: the
// fence scanner (`findFrontmatterBlock`) and the `splitFrontmatter` adapter that
// builds a body padded so every body line keeps its true source line number
// (see `body` below). The fields themselves come from `parseFrontmatter` in
// `SkillForm.tsx`, the repo's display-side frontmatter reader, so the preview
// strip and the skill meta strips flatten values the same way.
import { isMap, isScalar, isSeq, parseDocument } from 'yaml'
import { parseFrontmatter } from '../SkillForm'

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
const FENCE = '---'
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

export interface FrontmatterEntry {
  key: string
  /** The value as `parseFrontmatter` flattens it: scalars as text, sequences
   *  joined with `, `, and a nested map as its source text. */
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
  /** True only when a well-formed block was found and it held at least one field. */
  hasFrontmatter: boolean
}

const NO_FRONTMATTER: FrontmatterSplit = { entries: [], body: '', hasFrontmatter: false }

/** Whether a top-level value is a list holding an item `parseFrontmatter`
 *  cannot show. This only checks shape; values still come from the shared
 *  reader. */
function hasUnshownValue(block: string): boolean {
  const doc = parseDocument(block)
  if (!isMap(doc.contents)) return false
  return doc.contents.items.some((pair) => isSeq(pair.value) && pair.value.items.some((item) => !isScalar(item)))
}

export function splitFrontmatter(raw: string): FrontmatterSplit {
  const found = findFrontmatterBlock(raw)
  if (!found) return { ...NO_FRONTMATTER, body: raw }

  // Hand the shared reader exactly the block this scanner found, re-fenced. Its
  // own fence rule is looser (any line STARTING with `---` closes), so it can
  // stop early inside the block, e.g. at a `-----` line. It then returns the
  // rest as body. Strip only when it consumed the whole block; otherwise text
  // between its early stop and our fence would show in neither the strip nor
  // the body.
  const shared = parseFrontmatter([FENCE, found.block, FENCE, ''].join('\n'))
  if (shared.body !== '') return { ...NO_FRONTMATTER, body: raw }
  const entries: FrontmatterEntry[] = Object.entries(shared.meta).map(([key, value]) => ({ key, value }))
  if (entries.length === 0) return { ...NO_FRONTMATTER, body: raw }
  // The shared reader drops a list item that is not a plain value (a list of
  // maps, a list of lists), so the strip would show less than the file holds.
  // Leave such a document unsplit, so every word stays visible and findable.
  if (hasUnshownValue(found.block)) return { ...NO_FRONTMATTER, body: raw }

  // Pad the body with one blank line per stripped source line so body line
  // numbers stay aligned with the full document (see `body` doc above).
  const rawBody = raw.slice(found.bodyStart)
  const body = '\n'.repeat(found.blockLineCount) + rawBody
  return { entries, body, hasFrontmatter: true }
}
