import { useMemo, useState, useRef, useEffect } from 'react'
import type { ContentBlock } from '../types'

const FENCE_OPEN = /^(`{3,})(\w*)\s*$/
// Escape ALL regex metacharacters before interpolating a captured fence run
// into a dynamic RegExp. The capture is currently backtick-only, but a
// complete escape (not a single-char `\`` replace) keeps the sanitization
// sound if FENCE_OPEN ever widens, and is the form static analysis recognizes.
function escapeRegExp(s: string): string {
  return s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
}
const FENCE_CLOSE_RE = (() => {
  const cache = new Map<string, RegExp>()
  return (tick: string) => {
    let re = cache.get(tick)
    if (!re) { re = new RegExp(`^${tick}\\s*$`); cache.set(tick, re) }
    return re
  }
})()
const DIFF_LINE = /^@@|^[+-]\d+:|^[+-][^+-\s]/
// Outer fence languages where nested code fence examples are expected.
// Only these languages trigger inner-fence depth tracking. Code languages
// (python, bash, json, etc.) skip tracking to avoid over-consuming content
// when a ```lang line appears as literal text inside them.
const NESTABLE_LANGS = new Set(['', 'markdown', 'md', 'mdx', 'rst', 'txt', 'text', 'html', 'xml', 'svg', 'asciidoc', 'adoc'])
// Per-line widget tag regexes. Matched against a line AFTER masking inline
// code spans, so tags appearing inside backtick-quoted prose are ignored.
// Match the open tag flexibly: capture the full attribute string so we can
// extract title= and slug= regardless of order.
const WIDGET_OPEN_LINE_RE = /<mcwidget((?:\s+\w+="[^"]*")*)\s*>/
const WIDGET_ATTR_RE = /(\w+)="([^"]*)"/g
const WIDGET_CLOSE_LINE_RE = /<\/mcwidget>/

/** Extract title/slug attributes from the attribute string captured by
 * WIDGET_OPEN_LINE_RE. Returns plain object, never throws. */
function parseWidgetAttrs(attrStr: string): { title?: string; slug?: string } {
  const out: { title?: string; slug?: string } = {}
  if (!attrStr) return out
  WIDGET_ATTR_RE.lastIndex = 0
  let m: RegExpExecArray | null
  while ((m = WIDGET_ATTR_RE.exec(attrStr)) !== null) {
    if (m[1] === 'title') out.title = m[2]
    else if (m[1] === 'slug') out.slug = m[2]
  }
  return out
}

/**
 * Mask inline-code regions within a single line.
 *
 * Returns a string of the same length as `line` where the contents of each
 * balanced backtick run (`` `...` ``, `` ``...`` ``, etc.) including the
 * delimiters themselves are replaced with space characters. Unbalanced runs
 * are left as-is by default. CommonMark inline code does not span newlines,
 * so a per-line mask is sufficient.
 *
 * Used to prevent widget tag detection from false-matching inside quoted
 * examples like `` `<mcwidget>...</mcwidget>` `` that an author wrote to
 * document the syntax.
 *
 * `streamingTrailingLine` (default false): when true, an unmatched opening
 * backtick run is treated as an inline-code span whose closing run has not
 * yet streamed in. Everything from the opening run to end-of-line is masked.
 * Only set this for the LAST line of a streaming buffer — for any line that
 * already has a newline after it the input is final and unmatched runs are
 * literal text per CommonMark.
 */
export function maskInlineCode(line: string, streamingTrailingLine = false): string {
  const out = line.split('')
  let i = 0
  while (i < line.length) {
    if (line[i] !== '`') { i++; continue }
    const runStart = i
    while (i < line.length && line[i] === '`') i++
    const runLen = i - runStart
    // Search forward for a backtick run of exactly the same length.
    let j = i
    let matchStart = -1
    while (j < line.length) {
      if (line[j] !== '`') { j++; continue }
      const r2Start = j
      while (j < line.length && line[j] === '`') j++
      if (j - r2Start === runLen) { matchStart = r2Start; break }
    }
    if (matchStart >= 0) {
      for (let k = runStart; k < matchStart + runLen; k++) out[k] = ' '
      i = matchStart + runLen
    } else if (streamingTrailingLine) {
      // Streaming: assume the closing run has not yet arrived. Mask the rest
      // of the line so any widget tag inside the (still-incomplete) span is
      // not falsely promoted to a widget. When the close arrives in the next
      // streaming snapshot the run is balanced and the normal branch above
      // takes over.
      for (let k = runStart; k < line.length; k++) out[k] = ' '
      i = line.length
    }
    // Otherwise unbalanced run on a final line: leave as-is per CommonMark.
  }
  return out.join('')
}

/** Classify whether code content looks like a unified diff. */
function isDiffContent(code: string, lang?: string): boolean {
  const lines = code.split('\n')
  const count = lines.filter(l => DIFF_LINE.test(l)).length
  return count >= 2 || (lang === 'diff' && count >= 1)
}

type State = 'outside' | 'fence' | 'widget' | 'widget-fence'

/**
 * Parse raw text into structured content blocks.
 *
 * Runs a single state-machine pass over input lines. States:
 *
 *   outside       — collecting markdown
 *   fence         — inside a ``` code block (not inside a widget)
 *   widget        — inside a <mcwidget>…</mcwidget> body
 *   widget-fence  — inside a ``` code block that is itself inside a widget;
 *                   the fence content is treated as opaque widget body and
 *                   a </mcwidget> tag inside it is ignored until the fence
 *                   closes.
 *
 * Widget tag detection runs against each line AFTER inline-code spans are
 * masked, so tags quoted in prose with backticks (e.g. when documenting the
 * widget syntax) are not falsely extracted as real widgets.
 *
 * Fences inside a widget body are preserved verbatim in the widget content
 * so a widget containing a ```code``` example is still emitted as a single
 * widget block, not shredded into markdown | code | markdown.
 *
 * When `streaming` is true, an unclosed fence or widget at end of input
 * produces a block with `complete: false` so the renderer can show a
 * provisional view.
 */
export function parseBlocks(raw: string, streaming: boolean): ContentBlock[] {
  const lines = raw.split('\n')
  const blocks: ContentBlock[] = []

  let state: State = 'outside'
  let mdBuf: string[] = []
  let codeBuf: string[] = []
  let widgetBuf: string[] = []
  let fenceTick = ''
  let fenceLang = ''
  let fenceLen = 0  // raw backtick count of the opening fence
  let innerFenceDepth = 0  // tracks nested fence pairs inside a code block
  let fenceNestable = false  // true when outer lang is markup/doc (nested fences expected)
  let widgetTitle = ''
  let widgetSlug = ''
  let widgetFenceTick = ''
  let mdStart = 1
  let codeStart = 1
  let widgetStart = 1

  const flushMd = () => {
    if (mdBuf.length === 0) return
    const text = mdBuf.join('\n')
    if (text.trim()) blocks.push({ type: 'markdown', content: text, complete: true, startLine: mdStart })
    mdBuf = []
  }

  const flushCode = (complete: boolean) => {
    const code = codeBuf.join('\n')
    const lang = fenceLang || undefined
    let type: ContentBlock['type'] = 'code'
    if (lang === 'mermaid') type = 'mermaid'
    // Checked before the diff heuristic: scene JSON is not diff-shaped today,
    // but an explicit language must never lose to content sniffing.
    else if (lang === 'excalidraw') type = 'excalidraw'
    else if (lang === 'diff' || isDiffContent(code, lang)) type = 'diff'
    blocks.push({ type, content: code, language: lang, complete, startLine: codeStart })
    codeBuf = []
    fenceTick = ''
    fenceLang = ''
    fenceLen = 0
    innerFenceDepth = 0
    fenceNestable = false
  }

  const flushWidget = (complete: boolean) => {
    blocks.push({
      type: 'widget',
      content: widgetBuf.join('\n').trim(),
      language: widgetTitle || 'Widget',
      complete,
      startLine: widgetStart,
      slug: widgetSlug || undefined,
    })
    widgetBuf = []
    widgetTitle = ''
    widgetSlug = ''
    widgetFenceTick = ''
  }

  const pushMd = (text: string, lineIdx: number) => {
    if (mdBuf.length === 0) mdStart = lineIdx + 1
    mdBuf.push(text)
  }

  for (let i = 0; i < lines.length; i++) {
    const line = lines[i]
    // Conservative masking only for the trailing line of a streaming buffer:
    // an unmatched opening backtick run there is treated as an inline-code
    // span whose closing run has not yet streamed. Prevents a transient
    // <mcwidget> tag inside that incomplete span from triggering a widget
    // transition. Once the close arrives in a later snapshot the run is
    // balanced and normal masking takes over.
    const isStreamingTail = streaming && i === lines.length - 1
    switch (state) {
      case 'outside': {
        const masked = maskInlineCode(line, isStreamingTail)
        const wOpen = WIDGET_OPEN_LINE_RE.exec(masked)
        if (wOpen) {
          const before = line.slice(0, wOpen.index)
          if (before) pushMd(before, i)
          flushMd()
          const attrs = parseWidgetAttrs(wOpen[1] || '')
          widgetTitle = attrs.title || ''
          widgetSlug = attrs.slug || ''
          widgetStart = i + 1
          const afterTag = line.slice(wOpen.index + wOpen[0].length)
          // Close tag on the same line as the open tag: single-line widget.
          const maskedAfter = maskInlineCode(afterTag, isStreamingTail)
          const wClose = WIDGET_CLOSE_LINE_RE.exec(maskedAfter)
          if (wClose) {
            widgetBuf.push(afterTag.slice(0, wClose.index))
            flushWidget(true)
            const afterClose = afterTag.slice(wClose.index + wClose[0].length)
            if (afterClose) pushMd(afterClose, i)
          } else {
            if (afterTag) widgetBuf.push(afterTag)
            state = 'widget'
          }
          break
        }
        const fenceMatch = FENCE_OPEN.exec(line)
        if (fenceMatch) {
          flushMd()
          fenceTick = escapeRegExp(fenceMatch[1])
          fenceLang = fenceMatch[2] || ''
          fenceLen = fenceMatch[1].length
          fenceNestable = NESTABLE_LANGS.has(fenceLang.toLowerCase())
          codeStart = i + 2
          state = 'fence'
          break
        }
        pushMd(line, i)
        break
      }

      case 'fence': {
        if (FENCE_CLOSE_RE(fenceTick).test(line)) {
          // Track nested fence depth: if the code buffer contains unmatched
          // inner fence opens (e.g. a markdown snippet showing ```python...```)
          // then this bare ``` is closing an INNER fence, not the outer one.
          // Only applied for markup/doc outer languages where nested fences
          // are expected; code languages (python, bash, json, etc.) pass
          // through to the original close behavior to avoid over-consuming.
          if (innerFenceDepth > 0) {
            innerFenceDepth--
            codeBuf.push(line)
          } else {
            flushCode(true)
            state = 'outside'
          }
        } else {
          // Check if this line opens a nested fence inside our code block.
          // Scoped to markup/doc outer languages where embedded code examples
          // are common. For code languages (python, js, bash, etc.) a line
          // like ```python is almost certainly literal content, not a nested
          // structural fence — so we skip depth tracking entirely.
          if (fenceNestable) {
            const innerMatch = FENCE_OPEN.exec(line)
            if (innerMatch && innerMatch[1].length === fenceLen && innerMatch[2]) {
              innerFenceDepth++
            }
          }
          codeBuf.push(line)
        }
        break
      }

      case 'widget': {
        const masked = maskInlineCode(line, isStreamingTail)
        const wClose = WIDGET_CLOSE_LINE_RE.exec(masked)
        if (wClose) {
          const before = line.slice(0, wClose.index)
          if (before) widgetBuf.push(before)
          flushWidget(true)
          const afterClose = line.slice(wClose.index + wClose[0].length)
          if (afterClose) pushMd(afterClose, i)
          state = 'outside'
          break
        }
        const fenceMatch = FENCE_OPEN.exec(line)
        if (fenceMatch) {
          widgetBuf.push(line)
          widgetFenceTick = escapeRegExp(fenceMatch[1])
          state = 'widget-fence'
          break
        }
        widgetBuf.push(line)
        break
      }

      case 'widget-fence': {
        widgetBuf.push(line)
        if (FENCE_CLOSE_RE(widgetFenceTick).test(line)) {
          state = 'widget'
        }
        break
      }
    }
  }

  // End of input: flush any open state.
  if (state === 'fence') {
    flushCode(!streaming)
  } else if (state === 'widget' || state === 'widget-fence') {
    flushWidget(!streaming)
  }
  flushMd()

  return blocks
}

const THROTTLE_MS = 100

/** Shared empty result so the streaming path allocates nothing per render. */
const NO_BLOCKS: ContentBlock[] = []

/**
 * Hook that parses raw message text into content blocks.
 * During streaming, unclosed fences or widgets produce provisional blocks.
 * On completion (streaming=false), does a clean full reparse.
 *
 * Perf: while streaming, the timer below is the ONLY caller of parseBlocks.
 * That is what bounds the work, which is otherwise quadratic in response
 * length because every chunk (~60/s) re-parses the whole accumulated string,
 * along with the GC pressure from the discarded intermediates. The eager parse
 * is gated off for the duration of the stream and resumes the moment streaming
 * ends, so the final output is identical to an unthrottled parse.
 */
export function useBlockAssembler(rawText: string, streaming: boolean): ContentBlock[] {
  // Gated on !streaming: during a stream this must not parse, or the throttle
  // below would only be deferring work the render had already paid for.
  const immediateBlocks = useMemo(
    () => (streaming ? NO_BLOCKS : parseBlocks(rawText, false)),
    [rawText, streaming],
  )

  // Gated too: the early return below never reads this when !streaming, so a
  // completed message would otherwise pay a second full parse for nothing.
  const [throttledBlocks, setThrottledBlocks] = useState<ContentBlock[]>(() =>
    streaming ? parseBlocks(rawText, true) : NO_BLOCKS,
  )
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const latestTextRef = useRef(rawText)

  // In an effect, not in render: a render React discards must not move the ref
  // the timer reads, or the timer would parse text from an abandoned render.
  useEffect(() => {
    latestTextRef.current = rawText
  }, [rawText])

  useEffect(() => {
    if (!streaming) {
      if (timerRef.current !== null) {
        clearTimeout(timerRef.current)
        timerRef.current = null
      }
      // Primes the snapshot so a later stream resumes from the current parse.
      setThrottledBlocks(immediateBlocks)
      return
    }
    if (timerRef.current === null) {
      timerRef.current = setTimeout(() => {
        timerRef.current = null
        setThrottledBlocks(parseBlocks(latestTextRef.current, true))
      }, THROTTLE_MS)
    }
  }, [rawText, streaming, immediateBlocks])

  useEffect(() => {
    return () => {
      if (timerRef.current !== null) {
        clearTimeout(timerRef.current)
        timerRef.current = null
      }
    }
  }, [])

  // This early return, not any effect, is what makes the final render exact.
  if (!streaming) return immediateBlocks
  return throttledBlocks
}
