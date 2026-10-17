import type { Nodes as MdastNodes, Root as MdastRoot } from 'mdast'
import remarkParse from 'remark-parse'
import { unified } from 'unified'

import { fixCodeFences } from '../components/MarkdownRenderer'
import { REMARK_BASE_PLUGINS } from '../components/markdown/remarkBase'
import { isPathCandidate } from '../components/markdown/pathReferences'
import { parseBlocks } from '../hooks/useBlockAssembler'
import { capWhitespaceRuns } from './markdownDepthBound'

/**
 * Find the files an assistant reply offers to open, so the transcript can show
 * each one as an "Open file" chip under the reply.
 *
 * The signal is a hidden HTML comment the agent writes after a file the user
 * should see: `<!-- kirocrew:open-file path="/abs/path/notes.md" -->`.
 * rehypeRaw drops HTML comments from the rendered message, so the marker itself
 * is invisible, like the `kirocrew:preview` marker it sits beside (see
 * detectPreviewUrl).
 *
 * The marker only decides that a chip is SHOWN. Nothing opens until the user
 * clicks it, and the click goes through the same handler a click on a path chip
 * uses, so whether the file may be read stays the read endpoint's decision.
 *
 * Only a marker the transcript treats as hidden HTML counts. The reply goes
 * through the transcript's own source preparation -- the block splitter
 * (`parseBlocks`, which lifts fenced code out of the prose), then, for each
 * prose block, `capWhitespaceRuns` and `fixCodeFences` (which opens a fence
 * glued to the text before it) -- and is parsed with the same base remark
 * plugins. Only the raw-HTML nodes of that tree are searched, so a marker the
 * reader can SEE as code -- a fence, an indented code block, inline code, a
 * fence inside a blockquote or glued to a sentence -- gets no chip.
 */

// `<!-- kirocrew:open-file path="..." -->`, tolerant of surrounding whitespace.
const MARKER_RE = /<!--\s*kirocrew:open-file\s+path="([^"]*)"\s*-->/gi

/** Most chips one reply shows; markers past this are ignored. */
export const OPEN_FILE_MARKER_MAX_CHIPS = 8

/** A POSIX absolute path (`/x`), or a Windows drive-qualified one (`C:\x`, `C:/x`). */
const ABSOLUTE_RE = /^(?:\/|[A-Za-z]:[\\/])/

const PARSER = unified().use(remarkParse).use(REMARK_BASE_PLUGINS).freeze()

/**
 * Is `raw` a path an open-file marker may offer? The path-chip test
 * (`isPathCandidate`, which already refuses UNC roots and command lines) plus
 * absoluteness: a relative or `~` path would resolve against a directory the
 * agent did not name.
 */
export function isOpenableMarkerPath(raw: string): boolean {
  return ABSOLUTE_RE.test(raw) && isPathCandidate(raw)
}

/** The values of every raw-HTML node in `tree`, in document order. */
function htmlValues(tree: MdastNodes, out: string[]): string[] {
  if (tree.type === 'html') out.push(tree.value)
  if ('children' in tree) for (const child of tree.children) htmlValues(child as MdastNodes, out)
  return out
}

/**
 * The paths one assistant reply's open-file markers name, in the order they
 * appear, each once. A marker shown as code, or naming a path
 * `isOpenableMarkerPath` refuses, contributes nothing; the others still count.
 * At most `OPEN_FILE_MARKER_MAX_CHIPS` paths are returned.
 */
export function openFileMarkerPaths(content: string): string[] {
  if (!content || !content.includes('kirocrew:open-file')) return []
  const html: string[] = []
  for (const block of parseBlocks(content, false)) {
    // Code, diff, diagram and widget blocks render as code or a frame: a
    // marker inside one is never hidden HTML.
    if (block.type !== 'markdown' || !block.content.includes('kirocrew:open-file')) continue
    const source = fixCodeFences(capWhitespaceRuns(block.content))
    htmlValues(PARSER.runSync(PARSER.parse(source)) as MdastRoot, html)
  }
  const paths: string[] = []
  for (const value of html) {
    MARKER_RE.lastIndex = 0
    let m: RegExpExecArray | null
    while ((m = MARKER_RE.exec(value)) !== null) {
      const path = m[1]
      if (!isOpenableMarkerPath(path) || paths.includes(path)) continue
      paths.push(path)
      if (paths.length >= OPEN_FILE_MARKER_MAX_CHIPS) return paths
    }
  }
  return paths
}
