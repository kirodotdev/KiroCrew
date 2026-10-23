/**
 * Line-anchored commenting for fenced code blocks on the artifact page.
 *
 * Code fences render through Pierre (`@pierre/diffs`), whose rows live in
 * shadow DOM — invisible to both the light-DOM selection capture in
 * `ArtifactDetailPage.handleMouseUp` and the `TreeWalker` in
 * `useMarkdownCommentHighlights`. Instead of piercing the shadow boundary
 * (browser-variant, and structurally at odds with Pierre's row
 * virtualization, where offscreen lines have no DOM at all), code comments
 * are LINE-anchored and ride Pierre's own primitives: line selection + the
 * gutter utility button create them, and `lineAnnotations` render them —
 * virtualization-correct because Pierre owns the rows.
 *
 * Anchors reuse the existing `CommentAnchor` columns with one convention:
 * for a code comment, `start_offset`/`end_offset` are offsets into the
 * ARTIFACT SOURCE (not the rendered text), `quote` is the source slice
 * between them (the server may truncate a stored quote), and
 * `prefix`/`suffix` are exact raw-source context around it. Routing prefers
 * the stored offsets and falls back to a context-gated quote search when a
 * later edit shifted them — see `routeCodeComments`. Prose comments store
 * rendered-text offsets and 32 chars of RENDERED-text context
 * (`anchorFromRange`). Rendered text drops markdown syntax and holds no
 * Pierre row text, so a prose prefix only equals the raw source in front of
 * an in-fence occurrence of its quote when the reader selected code text
 * itself (the plain stand-in shown before highlighting loads) or the same
 * text repeats verbatim with the same 32 chars before it. The first is a
 * comment on that code line, where a chip belongs; in the second the thread
 * shows as a chip on the code copy instead of a prose highlight, and stays in
 * the sidebar either way.
 */
import { createContext } from 'react'
import { parseBlocks } from '../hooks/useBlockAssembler'
import type { ArtifactComment } from '../types'

/** One code block located in the artifact markdown source. */
export interface CodeFenceSpan {
  /** 1-based source line of the block's first CONTENT line — the block
   *  assembler's `startLine`, which is also the handle a rendered `CodeBlock`
   *  names its span with (`sourceStartLine`). */
  contentStartLine: number
  /** Char offset (into the artifact source) of the first content char. */
  contentStart: number
  /** Char offset one past the last content char (the closing-fence line is
   *  excluded; an unclosed fence runs to the end of the source). */
  contentEnd: number
}

/** Locate commentable code blocks in markdown source.
 *
 *  Deliberately NOT a CommonMark fence scanner: the artifact body renders
 *  fences through the block assembler (`parseBlocks`), whose rules differ
 *  from CommonMark (backtick-only, no indent, `\w*` info string, nested-fence
 *  depth tracking, mcwidget masking). Deriving spans from the SAME parser is
 *  what guarantees a routed comment lands on a block that actually renders
 *  as a `CodeBlock` — and that blocks rendering as something else (diff,
 *  mermaid, excalidraw, widget) are never claimed. */
export function codeFenceSpans(source: string): CodeFenceSpan[] {
  const lines = source.split('\n')
  // Char offset of each 1-based line start; index 0 unused.
  const lineOffset: number[] = new Array(lines.length + 2)
  lineOffset[1] = 0
  for (let i = 0; i < lines.length; i++) lineOffset[i + 2] = lineOffset[i + 1] + lines[i].length + 1
  const offsetOf = (line: number) => Math.min(lineOffset[line] ?? source.length, source.length)
  const spans: CodeFenceSpan[] = []
  for (const b of parseBlocks(source, false)) {
    if (b.type !== 'code' || b.startLine == null) continue
    const contentStart = offsetOf(b.startLine)
    spans.push({
      contentStartLine: b.startLine,
      contentStart,
      contentEnd: Math.min(contentStart + b.content.length, source.length),
    })
  }
  return spans
}

/** A routed code-comment thread, rendered as a Pierre line annotation. */
export interface CodeLineAnnotationMeta {
  /** Root comment id (the thread handle for activate/flash). */
  id: string
  /** 1-based line within the fence's CONTENT the chip sits under: the last line of the anchored range. */
  line: number
  /** Thread size (root + replies) for the bubble count. */
  count: number
  /** Thread has unread content (bubble renders filled). */
  unread: boolean
}

export interface RoutedCodeComments {
  /** Annotations per fence, keyed by the fence's opening-line number. */
  byFence: Map<number, CodeLineAnnotationMeta[]>
  /** Threads claimed by a fence — excluded from the light-DOM highlight
   *  overlay so a short code quote can't mis-highlight a prose occurrence. */
  claimedThreadIds: Set<string>
}

/** Server-side cap on stored anchor strings (`_anchor_str` in the comments
 *  handler): a longer quote is truncated at write time while the offsets and
 *  the suffix (captured after the ORIGINAL selection end) are stored as sent. */
const SERVER_ANCHOR_QUOTE_CAP = 2000

/** Does `source` carry the anchor's stored context around an occurrence of
 *  its quote at `at`? The PREFIX is the discriminator and is always required:
 *  code-comment anchors store exact raw-source context (buildCodeCommentAnchor)
 *  while prose anchors store RENDERED-text context (or none, for an older or
 *  iframe-free anchor), so a raw prefix that matches inside a fence is the
 *  code-anchor signature (see the module note for when a prose prefix can
 *  match too, and why that lands the chip on code text). The SUFFIX is verified only when it is verifiable:
 *  a server-capped quote puts the stored suffix after the original selection
 *  end (not at `quote.length`), and a selection that reached the end of the
 *  source in an unclosed fence stored no suffix (the server nulls empty
 *  anchor strings). */
function contextMatches(source: string, at: number, quote: string, prefix: string, suffix: string | undefined): boolean {
  if (at < prefix.length || source.slice(at - prefix.length, at) !== prefix) return false
  if (!suffix || quote.length >= SERVER_ANCHOR_QUOTE_CAP) return true
  return source.slice(at + quote.length, at + quote.length + suffix.length) === suffix
}

/** Route anchored comments to the code fences that own them.
 *
 *  Resolution is two-tier, mirroring how the prose highlighter treats stored
 *  offsets as a preference rather than a requirement (the backend's anchor
 *  rescan flips `anchor_orphaned` but NEVER updates offsets on a content
 *  write, and the server caps stored anchor strings at 2000 chars while storing
 *  the offsets as sent — so a capped quote is a PREFIX of the source slice,
 *  never equal to it):
 *
 *  1. EXACT — the offsets sit inside a fence, the source at `start_offset`
 *     starts with the quote, AND the anchor's raw-source context matches
 *     around it (prefix required; suffix when verifiable — see
 *     {@link contextMatches}). `startsWith` (not equality) is deliberate: it
 *     also accepts a server-capped quote at its untouched offsets. The
 *     context gate keeps a prose anchor whose rendered-text offset happens
 *     to satisfy `startsWith` inside a fence from being claimed.
 *  2. DRIFT — an edit elsewhere in the document shifted the fence, so the
 *     offsets are stale. Search each fence for the quote and accept an
 *     occurrence only when the anchor's raw-source context matches around it
 *     (prefix always required — the code-anchor signature; suffix when
 *     verifiable, see {@link contextMatches}); among matches, the one nearest
 *     the stored offset wins. Nearest-wins mirrors the prose highlighter's
 *     own proximity ranking for repeated quotes: when identical code exists
 *     in several places the chip lands on the copy closest to where the
 *     comment was made, which is the same ambiguity contract prose comments
 *     already have.
 *
 *  A comment neither tier claims keeps its sidebar presence and its
 *  `anchor_orphaned` handling — only the in-fence chip is absent, which is
 *  the same degradation the prose overlay has for an unresolvable anchor. */
export function routeCodeComments(
  source: string,
  spans: CodeFenceSpan[],
  comments: ArtifactComment[],
  unreadRootIds?: Set<string>,
): RoutedCodeComments {
  const byFence = new Map<number, CodeLineAnnotationMeta[]>()
  const claimedThreadIds = new Set<string>()
  if (spans.length === 0 || comments.length === 0) return { byFence, claimedThreadIds }
  const threadCounts = new Map<string, number>()
  for (const c of comments) {
    threadCounts.set(c.thread_id, (threadCounts.get(c.thread_id) ?? 0) + 1)
  }
  for (const c of comments) {
    if (c.parent_id) continue
    // Resolved threads show no anchor presence — same visibility contract as
    // the prose overlay (InlineCommentOverlay filters status !== 'resolved'),
    // so resolving a code thread removes its chip instead of leaving it lit.
    if (c.status === 'resolved') continue
    const a = c.anchor
    const quote = a?.quote
    if (!quote) continue
    let at = -1
    let span: CodeFenceSpan | undefined
    // Tier 1: exact (offset-pinned; tolerates a server-capped quote). The
    // context signature is REQUIRED here too, not only in the drift tier: a
    // prose anchor's rendered-text offset can coincidentally land inside a
    // fence span at a position where the raw source starts with its quote
    // (rendered text after a fence is a compressed copy of nearby source),
    // and without the prefix gate that thread would silently jump from prose
    // to code. Code anchors always store a raw-source prefix, so requiring
    // it costs no legitimate routing.
    if (
      typeof a.start_offset === 'number'
      && a.prefix
      && source.startsWith(quote, a.start_offset)
      && contextMatches(source, a.start_offset, quote, a.prefix, a.suffix)
    ) {
      const s = spans.find(sp => a.start_offset! >= sp.contentStart && a.start_offset! + quote.length <= sp.contentEnd)
      if (s) { at = a.start_offset; span = s }
    }
    // Tier 2: drift (quote search, gated on the raw-source context signature).
    if (at < 0 && a.prefix) {
      let best: { at: number; span: CodeFenceSpan; dist: number } | null = null
      for (const sp of spans) {
        let idx = source.indexOf(quote, sp.contentStart)
        while (idx !== -1 && idx + quote.length <= sp.contentEnd) {
          if (contextMatches(source, idx, quote, a.prefix, a.suffix)) {
            const dist = typeof a.start_offset === 'number' ? Math.abs(idx - a.start_offset) : idx
            if (!best || dist < best.dist) best = { at: idx, span: sp, dist }
          }
          idx = source.indexOf(quote, idx + 1)
        }
      }
      if (best) { at = best.at; span = best.span }
    }
    if (at < 0 || !span) continue
    // The chip goes under the range's LAST line: Pierre draws an annotation
    // row below its line, so pinning it to the first line of a multi-line
    // range would split the range and hide how far it reaches.
    // The stored offsets span the ORIGINAL selection, so a server-capped quote
    // still ends the chip on the range's real last line.
    const storedLen = typeof a.start_offset === 'number' && typeof a.end_offset === 'number'
      ? a.end_offset - a.start_offset
      : 0
    let line = 1
    const end = Math.min(at + Math.max(quote.length, storedLen), span.contentEnd)
    // Every newline before `end` starts a new line, the last one included: a
    // quote never carries its last line's terminator, so a trailing newline
    // means the range ends on a selected EMPTY line, and the chip goes there.
    for (let i = span.contentStart; i < end; i++) {
      if (source.charCodeAt(i) === 10) line++
    }
    const list = byFence.get(span.contentStartLine) ?? []
    list.push({
      id: c.id,
      line,
      count: threadCounts.get(c.thread_id) ?? 1,
      unread: unreadRootIds?.has(c.id) ?? false,
    })
    byFence.set(span.contentStartLine, list)
    claimedThreadIds.add(c.thread_id)
  }
  return { byFence, claimedThreadIds }
}

/** The comments a light-DOM prose overlay should paint over a markdown body:
 *  every thread except the ones a code fence claims. A code thread left in
 *  would be painted by the overlay's quote match on any prose copy of the code
 *  line. Surfaces that show no code chips (the chat side panel) still need
 *  this, or a code comment lands on the wrong text. Returns `comments` itself
 *  when nothing is claimed. */
export function proseOverlayComments(source: string, comments: ArtifactComment[]): ArtifactComment[] {
  if (comments.length === 0) return comments
  const { claimedThreadIds } = routeCodeComments(source, codeFenceSpans(source), comments)
  return claimedThreadIds.size === 0 ? comments : comments.filter(c => !claimedThreadIds.has(c.thread_id))
}

/** How many chars of surrounding source to store as anchor prefix/suffix —
 *  disambiguates a repeated quote for the backend's anchor rescan. */
const ANCHOR_CONTEXT_CHARS = 32

/** The popover-ready anchor for a line range inside a fence: quote is the
 *  exact source text of the selected lines, offsets are source offsets, and
 *  `line` is the 1-based DOCUMENT line of the first selected line. Returns
 *  null for a blank selection (an anchor that can never be rescanned). */
export function buildCodeCommentAnchor(
  source: string,
  span: CodeFenceSpan,
  start: number,
  end: number,
): { quote: string; prefix: string; suffix: string; startOffset: number; endOffset: number; line: number } | null {
  const content = source.slice(span.contentStart, span.contentEnd)
  const lines = content.split('\n')
  const s = Math.max(1, Math.min(Math.min(start, end), lines.length))
  const e = Math.max(s, Math.min(Math.max(start, end), lines.length))
  let off = span.contentStart
  for (let i = 0; i < s - 1; i++) off += lines[i].length + 1
  const quote = lines.slice(s - 1, e).join('\n')
  if (!quote.trim()) return null
  return {
    quote,
    prefix: source.slice(Math.max(0, off - ANCHOR_CONTEXT_CHARS), off),
    suffix: source.slice(off + quote.length, off + quote.length + ANCHOR_CONTEXT_CHARS),
    startOffset: off,
    endOffset: off + quote.length,
    line: span.contentStartLine + s - 1,
  }
}

/** A gutter-initiated comment request from a code block: a 1-based content
 *  line range within the block whose first content line (in the artifact
 *  source) is `blockStartLine`, plus the viewport coordinates to open the
 *  popover at. */
export interface CodeCommentRangeRequest {
  blockStartLine: number
  start: number
  end: number
  x: number
  y: number
}

export interface CodeCommentContextValue {
  /** Annotations for the block whose first content line is `blockStartLine`. */
  annotationsFor(blockStartLine: number): CodeLineAnnotationMeta[]
  /** Open the anchored-comment popover for a line range within a fence. */
  onCommentRange(req: CodeCommentRangeRequest): void
  /** Active thread id (annotation renders in its active state). */
  activeId: string | null
  /** Open the thread. The popover finds the chip through
   *  {@link codeAnchorElement}, so a chip click passes no rect. */
  onActivate(id: string, rect?: { x: number; y: number; w: number; h: number }): void
  /** The block and 1-based content lines a comment was last posted on;
   *  `seq` changes per post. That block clears its line selection when it is
   *  still that range, so the shading stops reading as the new comment's
   *  highlight. */
  postedFor?: PostedCodeRange | null
}

export interface PostedCodeRange { blockStartLine: number; start: number; end: number; seq: number }

/** The fence and content-line range a successful post was made on, or null
 *  when it was not a code range (a prose post, whose offset is in rendered
 *  text, does not slice the source to its quote inside a fence). */
export function postedCodeRange(
  source: string,
  spans: CodeFenceSpan[],
  posted: { startOffset: number; quote: string; seq: number },
): PostedCodeRange | null {
  const { startOffset: at, quote, seq } = posted
  if (!quote || !source.startsWith(quote, at)) return null
  const span = spans.find(s => at >= s.contentStart && at + quote.length <= s.contentEnd)
  if (!span) return null
  const lineAt = (off: number) => {
    let line = 1
    for (let i = span.contentStart; i < off; i++) if (source.charCodeAt(i) === 10) line++
    return line
  }
  return { blockStartLine: span.contentStartLine, start: lineAt(at), end: lineAt(at + quote.length), seq }
}

/* Live anchors for code threads, by root comment id. A chip lives in Pierre's
 * shadow DOM, where `document.querySelector('[data-mc-cid]')` cannot reach it,
 * so the thread popover asks here instead: the chip while it is mounted, else
 * the code block that owns the thread (a block far from the viewport renders
 * no chip yet). Looked up on every reposition, so the popover follows the chip
 * as the page scrolls, and a sidebar click can scroll the block into view. */
const codeChipEls = new Map<string, HTMLElement>()
const codeBlockEls = new Map<string, HTMLElement>()

const codeAnchorListeners = new Set<() => void>()
const notifyCodeAnchors = () => codeAnchorListeners.forEach(fn => fn())

function holdAnchor(map: Map<string, HTMLElement>, id: string, el: HTMLElement): () => void {
  map.set(id, el)
  notifyCodeAnchors()
  return () => { if (map.get(id) === el) { map.delete(id); notifyCodeAnchors() } }
}

/** Call `fn` whenever a code anchor mounts or unmounts (a chip appearing once
 *  its block is highlighted moves the popover onto it); returns the unsubscribe. */
export function onCodeAnchorsChange(fn: () => void): () => void {
  codeAnchorListeners.add(fn)
  return () => { codeAnchorListeners.delete(fn) }
}

/** Register a thread chip; returns the release to call on unmount. */
export const holdCodeChip = (id: string, el: HTMLElement) => holdAnchor(codeChipEls, id, el)
/** Register the code block that owns a thread; returns the release. */
export const holdCodeBlock = (id: string, el: HTMLElement) => holdAnchor(codeBlockEls, id, el)

/** The element a code thread's popover should anchor to and scroll to. */
export function codeAnchorElement(id: string): HTMLElement | null {
  return codeChipEls.get(id) ?? codeBlockEls.get(id) ?? null
}

/** Provided by the artifact detail page when inline commenting is available;
 *  null everywhere else (chat, previews), which keeps `CodeBlock` inert. */
export const CodeCommentContext = createContext<CodeCommentContextValue | null>(null)
