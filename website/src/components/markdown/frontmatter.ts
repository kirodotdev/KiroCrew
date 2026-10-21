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
import { isMap, isScalar, isSeq, parseDocument } from 'yaml'
import type { Node } from 'yaml'

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
  /** The markdown body with the frontmatter block removed. Equals the input
   *  when there is no block, so a caller can always render `body`. */
  body: string
  /** True only when a well-formed block was found and parsed as a YAML map. */
  hasFrontmatter: boolean
}

function renderValue(node: Node | null): string {
  if (node == null) return ''
  if (isScalar(node)) return node.value == null ? '' : String(node.value)
  if (isSeq(node)) return node.items.map((it) => renderValue(it as Node)).join(', ')
  // A map or any other shape: compact one-line YAML, trimmed of the trailing
  // newline stringify adds, so nothing is silently dropped.
  try {
    return node.toString().replace(/\s*\n\s*/g, ' ').trim()
  } catch {
    return ''
  }
}

const NO_FRONTMATTER: FrontmatterSplit = { entries: [], body: '', hasFrontmatter: false }

// Matches a closing fence: a line that is exactly `---` (trailing spaces and CR
// tolerated), preceded by a newline and followed by a line break or the end of
// the document. The body is whatever follows that line break.
const CLOSING_FENCE = /\r?\n---[ \t]*(\r?\n|$)/

export function splitFrontmatter(raw: string): FrontmatterSplit {
  // The opening fence must be its own line: `---` followed by end-of-line.
  if (!/^---[ \t]*\r?\n/.test(raw)) return { ...NO_FRONTMATTER, body: raw }
  const openEnd = raw.indexOf('\n') + 1

  const rest = raw.slice(openEnd)
  const close = CLOSING_FENCE.exec(rest)
  if (!close) return { ...NO_FRONTMATTER, body: raw }

  const block = rest.slice(0, close.index)
  const body = rest.slice(close.index + close[0].length)

  const doc = parseDocument(block)
  if (!isMap(doc.contents)) return { ...NO_FRONTMATTER, body: raw }

  const entries: FrontmatterEntry[] = []
  for (const pair of doc.contents.items) {
    const key = isScalar(pair.key) ? String(pair.key.value) : String(pair.key)
    entries.push({ key, value: renderValue(pair.value as Node | null) })
  }
  if (entries.length === 0) return { ...NO_FRONTMATTER, body: raw }
  return { entries, body, hasFrontmatter: true }
}
