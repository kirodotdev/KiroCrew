// Live inline markdown styling for the Lexical chat composer.
//
// The composer stays a plain-text editor (PlainTextPlugin): what it stores and
// sends is `$getRoot().getTextContent()`, byte for byte. This plugin only
// decorates. A TextNode transform re-parses the run of text around each edited
// node with `parseInlineMarkdown` and, when the result differs from what is on
// screen, rewrites that run as one text node per segment carrying the
// segment's Lexical format bits (bold, italic, strikethrough, code) and a
// dimmed style on marker characters. Format bits and styles never change the
// text content, so the prompt value, paste chips and send are untouched.
//
// A run is a maximal stretch of plain text nodes between line breaks, paste
// chips, or the paragraph edges. A run that already matches its parse is left
// alone, so typing inside a word changes one node's text and nothing else, and
// history keeps merging keystrokes the way it does without this plugin.
import { useEffect } from 'react'
import { useLexicalComposerContext } from '@lexical/react/LexicalComposerContext'
import {
  $createRangeSelection,
  $createTextNode,
  $getRoot,
  $getSelection,
  $isElementNode,
  $isRangeSelection,
  $isTextNode,
  $nodesOfType,
  $setSelection,
  HISTORY_MERGE_TAG,
  TextNode,
  type EditorThemeClasses,
  type ElementNode,
  type LexicalNode,
  type PointType,
} from 'lexical'
import { parseInlineMarkdown, type InlineSegment } from './composerInlineMarkdown'

/** Inline style on marker characters (`**`, `_`, `~~`, backticks): still
 *  visible and selectable, just quieter than the text they format. The theme's
 *  own muted colour, not a fixed alpha on the text colour: the alpha passed in
 *  dark themes but fell below 4.5:1 contrast in the light ones. */
export const INLINE_MARKER_STYLE = 'color: var(--muted)'

/** Lexical theme classes for the four format bits the parser emits. Lexical
 *  renders one inner tag per text node, so a node that is both bold and italic
 *  relies on these classes rather than nested `<strong><em>`. */
export const INLINE_MARKDOWN_THEME: EditorThemeClasses = {
  text: {
    bold: 'font-bold',
    italic: 'italic',
    strikethrough: 'line-through',
    code: 'font-mono bg-bg-elevated rounded',
  },
}

function isPlainTextNode(node: LexicalNode | null): node is TextNode {
  // Only ordinary text nodes take part: a TextNode subclass or a token/segmented
  // node belongs to some other feature and keeps its own look.
  return $isTextNode(node) && node.getType() === 'text' && node.getMode() === 'normal'
}

function $collectRun(node: TextNode): TextNode[] {
  const run: TextNode[] = [node]
  let previous = node.getPreviousSibling()
  while (isPlainTextNode(previous)) {
    run.unshift(previous)
    previous = previous.getPreviousSibling()
  }
  let next = node.getNextSibling()
  while (isPlainTextNode(next)) {
    run.push(next)
    next = next.getNextSibling()
  }
  return run
}

function styleFor(segment: InlineSegment): string {
  return segment.marker ? INLINE_MARKER_STYLE : ''
}

function runMatches(run: TextNode[], segments: InlineSegment[]): boolean {
  return run.length === segments.length && run.every((node, index) => {
    const segment = segments[index]
    return node.getTextContent() === segment.text && node.getFormat() === segment.format &&
      node.getStyle() === styleFor(segment)
  })
}

// Canonical character offsets from the start of the root: the same measure the
// composer's control API uses, so a rewrite can put the caret back exactly.
function $nodeStartOffset(node: LexicalNode): number {
  let offset = 0
  let current: LexicalNode | null = node
  while (current) {
    let sibling = current.getPreviousSibling()
    while (sibling) {
      offset += sibling.getTextContentSize()
      sibling = sibling.getPreviousSibling()
    }
    current = current.getParent()
  }
  return offset
}

function $pointOffset(point: PointType): number {
  const node = point.getNode()
  if (point.type === 'text') return $nodeStartOffset(node) + point.offset
  if (!$isElementNode(node)) return $nodeStartOffset(node)
  return $nodeStartOffset(node) + node.getChildren().slice(0, point.offset)
    .reduce((total, child) => total + child.getTextContentSize(), 0)
}

function $setPointAtOffset(point: PointType, offset: number): void {
  const visit = (parent: ElementNode, localOffset: number): void => {
    let consumed = 0
    const children = parent.getChildren()
    for (let index = 0; index < children.length; index += 1) {
      const child = children[index]
      const next = consumed + child.getTextContentSize()
      if (localOffset <= next) {
        if ($isTextNode(child)) {
          point.set(child.getKey(), localOffset - consumed, 'text')
          return
        }
        if ($isElementNode(child)) {
          visit(child, localOffset - consumed)
          return
        }
        point.set(parent.getKey(), index + (localOffset > consumed ? 1 : 0), 'element')
        return
      }
      consumed = next
    }
    point.set(parent.getKey(), children.length, 'element')
  }
  const root = $getRoot()
  visit(root, Math.max(0, Math.min(offset, root.getTextContentSize())))
}

function $rewriteRun(run: TextNode[], segments: InlineSegment[]): void {
  const selection = $getSelection()
  const saved = $isRangeSelection(selection)
    ? { anchor: $pointOffset(selection.anchor), focus: $pointOffset(selection.focus) }
    : null
  let previous: TextNode | null = null
  segments.forEach((segment, index) => {
    let node: TextNode | undefined = run[index]
    if (!node) {
      node = $createTextNode(segment.text)
      previous!.insertAfter(node)
    }
    if (node.getTextContent() !== segment.text) node.setTextContent(segment.text)
    if (node.getFormat() !== segment.format) node.setFormat(segment.format)
    if (node.getStyle() !== styleFor(segment)) node.setStyle(styleFor(segment))
    previous = node
  })
  for (const node of run.slice(segments.length)) node.remove()
  if (!saved) return
  const restored = $createRangeSelection()
  $setPointAtOffset(restored.anchor, saved.anchor)
  $setPointAtOffset(restored.focus, saved.focus)
  // The next keystroke should land in the node under the caret instead of
  // splitting off a node in a stale format, so carry that node's look.
  const anchorNode = restored.anchor.getNode()
  restored.format = $isTextNode(anchorNode) ? anchorNode.getFormat() : 0
  restored.style = $isTextNode(anchorNode) ? anchorNode.getStyle() : ''
  $setSelection(restored)
}

/** The last parse, keyed on its input. `$rewriteRun` leaves every node it
 *  touches dirty, so Lexical runs the transform once per node of the run it
 *  just restyled; each of those sees the same joined text. Reusing the parse
 *  keeps one keystroke at one parse, however many nodes the run has. The
 *  parser is pure, so a hit returns exactly what a fresh parse would. */
let lastParse: { text: string; segments: InlineSegment[] } | null = null

function parseRunText(text: string): InlineSegment[] {
  if (lastParse?.text !== text) lastParse = { text, segments: parseInlineMarkdown(text) }
  return lastParse.segments
}

/** Restyle the run around `node` to match its markdown. */
function $applyInlineMarkdown(node: TextNode): void {
  if (!isPlainTextNode(node) || !node.isAttached()) return
  const run = $collectRun(node)
  const segments = parseRunText(run.map(item => item.getTextContent()).join(''))
  if (!segments.length || runMatches(run, segments)) return
  $rewriteRun(run, segments)
}

function $clearInlineMarkdown(): void {
  for (const node of $nodesOfType(TextNode)) {
    if (!isPlainTextNode(node)) continue
    if (node.getFormat() !== 0) node.setFormat(0)
    if (node.getStyle() !== '') node.setStyle('')
  }
}

/** Styles bold, italic, strikethrough and inline code in place while
 *  `enabled`; turning it off returns every run to plain text. */
export default function InlineMarkdownPlugin({ enabled }: { enabled: boolean }) {
  const [editor] = useLexicalComposerContext()

  useEffect(() => {
    if (!enabled) {
      editor.update($clearInlineMarkdown, { tag: HISTORY_MERGE_TAG })
      return
    }
    const unregister = editor.registerNodeTransform(TextNode, node => {
      // Never touch the node an IME is composing into; the update that ends
      // the composition marks it dirty again and the transform runs then.
      if (editor.isComposing()) return
      $applyInlineMarkdown(node)
    })
    // Registering a transform marks every text node dirty in a pending update
    // so existing text gets styled. Merge that update into the previous
    // history entry: styling what is already there is not an undoable edit.
    editor.update(() => {}, { tag: HISTORY_MERGE_TAG })
    return unregister
  }, [editor, enabled])

  return null
}
