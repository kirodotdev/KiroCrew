/**
 * The spoken form of a chat message: its markdown source reduced to the text a
 * reader would hear walking the RENDERED bubble.
 *
 * The transcript's live region announces a settled assistant turn (see
 * `TranscriptScrollShell`'s `announcement`). It cannot be handed `content`
 * directly: that is markdown SOURCE, so a live region holding it speaks the
 * asterisks, the backticks, the pipe characters of a table and the `](` of every
 * link — punctuation the rendered bubble never shows. Nor can it read the
 * rendered node's `textContent`, because the row that just settled may be
 * unmounted by the virtualizer when the reader has scrolled away, and an
 * announcement that silently depends on scroll position is worse than none.
 *
 * So this derives the text from the same AST the renderer draws from. It parses
 * with the subset of the renderer's plugin chain that changes what the TEXT is —
 * `remark-gfm` for tables and strikethrough — and with `remarkBoundDepth`, in
 * that order, because chat markdown is input-controlled and this file walks the
 * tree recursively (see `markdownDepthBound` for the RangeError this avoids).
 * The plugins left out cannot move a character of the result: `remark-math` and
 * the LaTeX pass retag `$…$` spans whose text is the same either way,
 * `remark-cjk-friendly` only reclassifies emphasis delimiters, and
 * `remarkAutolinkRules` only changes whether a URL becomes a link node — a link
 * contributes its text, never its destination, exactly as the rendered anchor
 * announces.
 *
 * Block boundaries are newlines here and collapse to spaces in the accessible
 * name; the pauses a reader makes come from the sentence punctuation already in
 * the prose. Deliberately NOT abbreviated or capped: the announcement is meant
 * to be the message, and speech is interruptible.
 */
import { unified } from 'unified'
import remarkGfm from 'remark-gfm'
import remarkParse from 'remark-parse'

import { remarkBoundDepth } from './markdownDepthBound'

/** Only the fields this walk reads; the real mdast carries far more. */
interface MdNode {
  type: string
  value?: string
  alt?: string
  children?: MdNode[]
}

/**
 * Nodes whose children are BLOCKS, so their text needs separating. Everything
 * else with children holds inline content (`paragraph`, `heading`, `emphasis`,
 * `link`, `tableCell`, …) and concatenates with nothing between, because a
 * space there would split a word that emphasis had merely interrupted.
 */
const BLOCK_CONTAINERS = new Set([
  'root',
  'blockquote',
  'list',
  'listItem',
  'footnoteDefinition',
])

/** Second belt under `remarkBoundDepth`: this walk recurses, so it never trusts
 *  the tree's depth even when the plugin is expected to have bounded it. */
const MAX_WALK_DEPTH = 200

/** Raw HTML reaches the bubble through `rehype-raw`, which renders it — so what
 *  a reader hears is the text between the tags, not the tags. */
const stripTags = (html: string): string => html.replace(/<[^>]*>/g, ' ')

const cells = (row: MdNode, depth: number): string =>
  (row.children ?? []).map((cell) => nodeText(cell, depth + 1)).filter(Boolean).join(', ')

function nodeText(node: MdNode, depth: number): string {
  if (depth > MAX_WALK_DEPTH) return ''
  switch (node.type) {
    case 'text':
    // Inline code and a fenced block both SHOW their contents, so both are
    // spoken. A reply whose answer is the code would otherwise announce as an
    // empty sentence with the code missing.
    case 'inlineCode':
    case 'code':
    // Present only when the math plugins ran; their value is the same source
    // text this would have read as a plain text node.
    case 'inlineMath':
    case 'math':
      return node.value ?? ''
    case 'image':
      return node.alt ?? ''
    case 'html':
      return stripTags(node.value ?? '')
    case 'break':
      return '\n'
    // A rule and a footnote marker draw no text a reader needs read back.
    case 'thematicBreak':
    case 'footnoteReference':
      return ''
    case 'table':
      return (node.children ?? []).map((row) => cells(row, depth + 1)).filter(Boolean).join('\n')
    case 'tableRow':
      return cells(node, depth)
    default:
      break
  }
  const children = node.children
  if (!children?.length) return ''
  const separator = BLOCK_CONTAINERS.has(node.type) ? '\n' : ''
  return children.map((child) => nodeText(child, depth + 1)).filter(Boolean).join(separator)
}

/** ORDER IS LOAD-BEARING: `remarkBoundDepth` registers an
 *  `mdast-util-from-markdown` transform that must run before `remark-gfm`'s,
 *  which recurses over the tree. Built once — the processor is reusable and
 *  `parse` is the only stage needed, since both extensions act inside it. */
const PROCESSOR = unified().use(remarkParse).use(remarkBoundDepth).use(remarkGfm)

/** The text to announce for a message, or `''` when it would say nothing. */
export function messageAnnouncementText(markdown: string): string {
  if (!markdown.trim()) return ''
  return nodeText(PROCESSOR.parse(markdown) as MdNode, 0)
    // Horizontal runs collapse to one space, matching what the accessible name
    // computed from this text would say anyway — a stripped tag pair and a
    // code block's indentation are both inaudible, so the value is kept equal
    // to the utterance rather than to the source. Newlines are preserved here
    // only so a block boundary survives into a readable test expectation.
    .replace(/[^\S\n]+/g, ' ')
    .replace(/ ?\n ?/g, '\n')
    .replace(/\n{2,}/g, '\n')
    .trim()
}
