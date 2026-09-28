/**
 * Render LaTeX-native math delimiters -- `\( … \)` inline, `\[ … \]` display --
 * through the same KaTeX pipeline as `$$` math, by emitting remark-math's own
 * mdast nodes from ELIGIBLE TEXT NODES ONLY.
 *
 * Why a remark transform and not a source rewrite: only the parser knows what is
 * prose. A source scanner has to re-derive fenced code, indented code, blockquote
 * prefixes, inline code, link destinations, reference definitions and raw HTML
 * attributes -- and every one it misses is a place where an escaped bracket in
 * something that is NOT prose gets rewritten into `$$` (an href with
 * `Name_\(x\)` becomes a dead link; a raw `<a title="\(x\)">` is corrupted before
 * rehype-raw ever sees it). Here those are `code`, `inlineCode`, `html`, `link`,
 * `definition`, … nodes: they are never visited, so the guard is structural. The
 * renderer already pays for the parse, so this adds no second pass.
 *
 * Eligibility rules (unchanged from the scanner, now stated against the tree):
 *
 * - A delimiter is `\(`/`\)`/`\[`/`\]` whose backslash is NOT itself escaped
 *   (escape parity: `\\(` is an escaped backslash followed by a plain paren).
 *   Note remark has ALREADY consumed CommonMark escapes when it built the text
 *   node: `\[` in source arrives here as `[` -- so this plugin reads the RAW
 *   source slice through the node's position, which keeps the backslashes.
 * - Inline: the opener needs a closer later in the same text node.
 * - Display: `\[` must be preceded on its line only by whitespace and `\]`
 *   followed on its line only by whitespace, within the node, and the
 *   delimiters must be whitespace-shaped (`\[ x \]`, `\[\n…\n\]`). A literal
 *   escaped bracket hugs its text (`\[REDACTED\]`, `see \[ x \] here`) -- the
 *   ADF/Jira converters escape brackets precisely so text cannot become markup.
 * - A closer immediately followed by `(` is link/image syntax; not eligible.
 * - A `\[ … \]` that spans lines inside a paragraph becomes a `math` FLOW node:
 *   the paragraph is split around it so the tree stays valid mdast.
 *
 * Node shapes mirror mdast-util-math (`data.hName` / `hProperties` /
 * `hChildren`), so rehype-katex renders them exactly like `$$` math.
 */

import { isOpeningTag, pairedCloseIndices, singleTagName } from './htmlTagGrammar'

type Position = { start: { offset?: number; line?: number }; end: { offset?: number; line?: number } }

type MdNode = {
  type: string
  value?: string
  children?: MdNode[]
  position?: Position
  data?: Record<string, unknown>
  meta?: string | null
  lang?: string | null
}

/** Node types whose subtrees are never prose. Structural, not heuristic. */
const OPAQUE = new Set([
  'code',
  'inlineCode',
  'math',
  'inlineMath',
  'html',
  'link',
  'linkReference',
  'image',
  'imageReference',
  'definition',
  'footnoteDefinition',
  'footnoteReference',
  'yaml',
  'toml',
])

function inlineMathNode(value: string): MdNode {
  return {
    type: 'inlineMath',
    value,
    data: {
      hName: 'code',
      hProperties: { className: ['language-math', 'math-inline'] },
      hChildren: [{ type: 'text', value }],
    },
  }
}

function displayMathNode(value: string): MdNode {
  return {
    type: 'math',
    meta: null,
    lang: null,
    value,
    data: {
      hName: 'pre',
      hChildren: [
        {
          type: 'element',
          tagName: 'code',
          properties: { className: ['language-math', 'math-display'] },
          children: [{ type: 'text', value }],
        },
      ],
    },
  }
}

/** Whitespace-only from `from` back to the previous newline (or start). */
function lineStartBefore(s: string, from: number): boolean {
  for (let a = from - 1; a >= 0 && s[a] !== '\n'; a--) if (!/\s/.test(s[a])) return false
  return true
}

/** Whitespace-only from `from` forward to the next newline (or end). */
function lineEndAfter(s: string, from: number): boolean {
  for (let b = from; b < s.length && s[b] !== '\n'; b++) if (!/\s/.test(s[b])) return false
  return true
}

/** A math span located in RAW source: `open`/`close` index the delimiter backslashes. */
type MathSpan = { kind: 'inline' | 'display'; open: number; close: number }

/**
 * What a text node's RAW slice cannot tell on its own: whether a delimiter on
 * the node's first (last) line is really at the start (end) of the SOURCE line.
 * `**bold** \[ x \]` gives a text node whose raw begins at `\[`, so a
 * slice-local check reads it as owning its line when the source line begins
 * with `**bold**`. The mdast knows: `prevSharesLine` is true when the previous
 * sibling ends on the node's first line, `nextSharesLine` when the next sibling
 * starts on the node's last line. `display` is false where a display block can
 * never be placed (a heading, a table cell, inline containers like `strong`):
 * only a paragraph's direct text can be split around a flow `math` node.
 */
type SpanContext = { display: boolean; prevSharesLine: boolean; nextSharesLine: boolean }
const PLAIN_CONTEXT: SpanContext = { display: true, prevSharesLine: false, nextSharesLine: false }

/**
 * Locate math spans in a text node's RAW source. Single forward pass: an
 * opener is remembered and paired when its closer arrives, so a body of
 * unmatched openers costs one visit per character (the quadratic per-opener
 * rescan was a rendered-content DoS). Escape parity is tracked the same way —
 * `run` counts the consecutive backslashes ending at the current position, so
 * a long backslash run is O(n) too, not a backward rescan per position.
 */
function findMathSpans(raw: string, ctx: SpanContext = PLAIN_CONTEXT): MathSpan[] {
  if (!raw.includes('\\(') && !raw.includes('\\[')) return []
  const spans: MathSpan[] = []
  let consumed = 0 // raw index up to which text has been claimed by a span
  let pendingParen = -1
  let pendingBracket = -1
  const n = raw.length
  let run = 0 // consecutive backslashes ending at i-1
  // "Is this delimiter on the slice's first / last line?" is answered from two
  // offsets computed ONCE, never by slicing per closer: a `\[ \]` flood would
  // otherwise pay O(offset) per closer -- quadratic on external content.
  const firstNl = raw.indexOf('\n')
  const lastNl = raw.lastIndexOf('\n')
  for (let i = 0; i < n; i++) {
    if (raw[i] !== '\\') {
      run = 0
      continue
    }
    run++
    if (i + 1 >= n) break
    // A delimiter backslash is one that closes an ODD run: `\(` yes, `\\(` no
    // (escaped backslash + plain paren), `\\\(` yes again.
    if (run % 2 === 0) continue
    const next = raw[i + 1]
    if (next === '(') {
      pendingParen = i
    } else if (next === '[') {
      pendingBracket = i
    } else if (next === ')' && pendingParen >= 0) {
      const open = pendingParen
      const close = i
      // A closer immediately followed by `(` is link/image syntax.
      if (raw[close + 2] !== '(' && open >= consumed && raw.slice(open + 2, close).trim()) {
        spans.push({ kind: 'inline', open, close })
        consumed = close + 2
        pendingBracket = -1
      }
      pendingParen = -1
    } else if (next === ']' && pendingBracket >= 0) {
      const open = pendingBracket
      const close = i
      const wsAfterOpen = /\s/.test(raw[open + 2] ?? '')
      const wsBeforeClose = /\s/.test(raw[close - 1] ?? '')
      // "Owns its line" within the slice -- and, when the delimiter sits on the
      // node's first/last line, no sibling may share that source line.
      const onFirstLine = firstNl === -1 || open < firstNl
      const onLastLine = lastNl === -1 || close + 2 > lastNl
      const eligible =
        ctx.display &&
        raw[close + 2] !== '(' &&
        open >= consumed &&
        wsAfterOpen &&
        wsBeforeClose &&
        lineStartBefore(raw, open) &&
        !(onFirstLine && ctx.prevSharesLine) &&
        lineEndAfter(raw, close + 2) &&
        !(onLastLine && ctx.nextSharesLine) &&
        raw.slice(open + 2, close).trim() !== ''
      if (eligible) {
        spans.push({ kind: 'display', open, close })
        consumed = close + 2
        pendingParen = -1
      }
      pendingBracket = -1
    }
    if (next === '(' || next === '[' || next === ')' || next === ']') {
      // The delimiter consumed the next char; a run cannot continue through it.
      i++
      run = 0
    }
  }
  return spans
}

/**
 * Align a text node's RAW source with its parser-DECODED value.
 *
 * remark has already applied CommonMark escapes, character references,
 * blockquote continuation markers and continuation-line indentation when it
 * built `value`. Rather than re-implement that decoding (and get it wrong on
 * an unlisted entity or a `> ` marker), we walk raw and value together and
 * record, for every raw index, which value index it belongs to. `-1` marks a
 * raw character that produced NO value character (a `> ` marker, stripped
 * indentation). An escape `\x` and a reference `&amp;` map all their raw
 * characters onto the single value character they became.
 *
 * Returns null when the two cannot be reconciled (a node another plugin
 * synthesized, or source we do not understand) — the node is then left alone,
 * erring literal rather than math.
 */
function alignRawToValue(raw: string, value: string): Int32Array | null {
  const map = new Int32Array(raw.length).fill(-1)
  let i = 0
  let j = 0
  while (i < raw.length && j < value.length) {
    const rc = raw[i]
    const vc = value[j]
    // Escapes and references FIRST: a greedy equal-char match would pair the
    // first `\` of `\\` (value `\`) or the `&` of `&amp;` (value `&`) and
    // then fail on the next character.
    if (rc === '\\' && i + 1 < raw.length && raw[i + 1] === vc && /[!-\/:-@[-`{-~]/.test(vc)) {
      map[i] = j
      map[i + 1] = j
      i += 2
      j++
      continue
    }
    if (rc === '&') {
      // Bounded: a reference is at most 33 chars, so look no further. An
      // unbounded indexOf here was quadratic on an `&`-flood with no `;`.
      let semi = -1
      for (let k = i + 1; k < raw.length && k <= i + 33; k++) {
        if (raw[k] === ';') {
          semi = k
          break
        }
      }
      if (semi > i) {
        const token = raw.slice(i, semi + 1)
        const decoded = decodeOneCharRef(token)
        if (decoded !== null && value.startsWith(decoded, j)) {
          for (let k = i; k <= semi; k++) map[k] = j
          i = semi + 1
          j += decoded.length
          continue
        }
        // A reference our small table cannot name, which remark DID decode
        // (the value does not carry the literal `&`): it became exactly one
        // code point. Consume the token onto it — one or two UTF-16 units.
        if (decoded === null && /^&[A-Za-z][A-Za-z0-9]{1,31};$/.test(token) && vc !== '&') {
          const width = vc >= '\ud800' && vc <= '\udbff' ? 2 : 1
          for (let k = i; k <= semi; k++) map[k] = j
          i = semi + 1
          j += width
          continue
        }
      }
    }
    if (rc === vc) {
      map[i++] = j++
      continue
    }
    // Raw-only character: a blockquote marker or stripped continuation indent
    // (these only ever follow a newline), or whitespace the parser collapsed.
    if (rc === '>' || rc === ' ' || rc === '\t' || rc === '\r') {
      i++
      continue
    }
    return null
  }
  // Trailing raw with no value left: markers/whitespace only, or give up.
  for (; i < raw.length; i++) if (!/[\s>]/.test(raw[i])) return null
  if (j < value.length) return null
  return map
}

const NAMED_ENTITIES: Record<string, string> = {
  amp: '&', lt: '<', gt: '>', quot: '"', apos: "'", nbsp: '\u00a0',
  copy: '\u00a9', reg: '\u00ae', trade: '\u2122', hellip: '\u2026',
  mdash: '\u2014', ndash: '\u2013', laquo: '\u00ab', raquo: '\u00bb',
  ldquo: '\u201c', rdquo: '\u201d', lsquo: '\u2018', rsquo: '\u2019',
  bull: '\u2022', middot: '\u00b7', deg: '\u00b0', plusmn: '\u00b1',
  times: '\u00d7', divide: '\u00f7', para: '\u00b6', sect: '\u00a7',
  euro: '\u20ac', pound: '\u00a3', yen: '\u00a5', cent: '\u00a2',
  larr: '\u2190', rarr: '\u2192', uarr: '\u2191', darr: '\u2193', harr: '\u2194',
}
/** Decode ONE `&…;` token, or null when it is not a reference we can name. */
function decodeOneCharRef(token: string): string | null {
  const m = /^&(#[xX][0-9a-fA-F]{1,6}|#[0-9]{1,7}|[A-Za-z][A-Za-z0-9]{1,31});$/.exec(token)
  if (!m) return null
  const body = m[1]
  if (body[0] === '#') {
    const cp = body[1] === 'x' || body[1] === 'X' ? parseInt(body.slice(2), 16) : parseInt(body.slice(1), 10)
    if (!Number.isFinite(cp) || cp === 0 || cp > 0x10ffff || (cp >= 0xd800 && cp <= 0xdfff)) return '\ufffd'
    return String.fromCodePoint(cp)
  }
  return NAMED_ENTITIES[body] ?? null
}

// Tag recognition and pairing come from the ONE grammar the renderer's
// verbatim-unknown-tags pass uses (`htmlTagGrammar`), so the two passes cannot
// disagree about which span is "shown as source" -- a quoted `>` inside an
// attribute, for example, is a tag to both or to neither. Which TAGS are
// verbatim is the renderer's decision too, injected as `verbatimTag`: it is
// the same predicate its own pass uses, so there is no second copy here.

type Point = { line: number; column: number; offset: number }

/** Offset → 1-based line/column, from a precomputed line-start table. */
function makePointAt(source: string): (offset: number) => Point {
  const starts = [0]
  for (let i = 0; i < source.length; i++) if (source[i] === '\n') starts.push(i + 1)
  return (offset) => {
    let lo = 0
    let hi = starts.length - 1
    while (lo < hi) {
      const mid = (lo + hi + 1) >> 1
      if (starts[mid] <= offset) lo = mid
      else hi = mid - 1
    }
    return { line: lo + 1, column: offset - starts[lo] + 1, offset }
  }
}

export type RemarkLatexDelimitersOptions = {
  /**
   * Tags whose PAIRED contents are verbatim, never prose. Required: the
   * renderer supplies the same predicate its verbatim-unknown-tags pass uses,
   * so `\\(x\\)` inside `<customBlock>…</customBlock>` stays source exactly when
   * the tags around it are shown as source.
   */
  verbatimTag: (tag: string) => boolean
}

export function remarkLatexDelimiters(options: RemarkLatexDelimitersOptions) {
  const isVerbatimTag = options.verbatimTag
  return (tree: MdNode, file: { value?: unknown }) => {
    const source = typeof file.value === 'string' ? file.value : String(file.value ?? '')
    if (!source.includes('\\(') && !source.includes('\\[')) return
    const pointAt = makePointAt(source)
    const posOf = (startOffset: number, endOffset: number) => ({
      start: pointAt(startOffset),
      end: pointAt(endOffset),
    })

    /** Rewrite one text node into prose + math pieces, or null to leave it. */
    const rewriteText = (node: MdNode, ctx: SpanContext): MdNode[] | null => {
      const s = node.position?.start.offset
      const e = node.position?.end.offset
      const value = node.value ?? ''
      if (typeof s !== 'number' || typeof e !== 'number' || e <= s) return null
      const raw = source.slice(s, e)
      const spans = findMathSpans(raw, ctx)
      if (spans.length === 0) return null
      const map = alignRawToValue(raw, value)
      if (map === null) return null
      // Value index for a raw index, looking forward past raw-only chars.
      const valueAt = (ri: number): number => {
        for (let k = ri; k < raw.length; k++) if (map[k] >= 0) return map[k]
        return value.length
      }
      const out: MdNode[] = []
      let cursorRaw = 0
      let cursorVal = 0
      const pushText = (
        fromRaw: number,
        toRaw: number,
        fromVal: number,
        toVal: number,
        afterDisplay: boolean,
        beforeDisplay: boolean,
      ) => {
        // A display block owns its line: the single line break on either side
        // of it is layout, not prose. Dropping it (and moving the start point
        // past it) keeps the neighbouring paragraph anchored to its own line.
        // Inline math is part of the sentence, so a soft break next to it is
        // prose whitespace and stays; only the display-facing side is trimmed.
        let text = value.slice(fromVal, toVal)
        let startRaw = fromRaw
        while (afterDisplay && text.startsWith('\n') && raw[startRaw] === '\n') {
          text = text.slice(1)
          startRaw++
        }
        let endRaw = toRaw
        while (beforeDisplay && text.endsWith('\n') && raw[endRaw - 1] === '\n') {
          text = text.slice(0, -1)
          endRaw--
        }
        if (text.length === 0) return
        out.push({ type: 'text', value: text, position: posOf(s + startRaw, s + endRaw) })
      }
      let prevDisplay = false
      for (const span of spans) {
        const openVal = valueAt(span.open)
        const isDisplay = span.kind === 'display'
        if (openVal > cursorVal) {
          pushText(cursorRaw, span.open, cursorVal, openVal, prevDisplay, isDisplay)
        }
        // Math content comes from RAW (KaTeX needs the backslashes remark
        // would have consumed), minus raw-only characters such as blockquote
        // markers inside a multi-line display block.
        let inner = ''
        for (let k = span.open + 2; k < span.close; k++) {
          if (map[k] >= 0 || raw[k] === '\n') inner += raw[k]
        }
        const pos = posOf(s + span.open, s + span.close + 2)
        const math = span.kind === 'inline' ? inlineMathNode(inner) : displayMathNode(inner.trim())
        math.position = pos
        out.push(math)
        cursorRaw = span.close + 2
        cursorVal = valueAt(cursorRaw)
        prevDisplay = isDisplay
      }
      if (cursorVal < value.length) {
        pushText(cursorRaw, raw.length, cursorVal, value.length, prevDisplay, false)
      }
      return out
    }

    const transformChildren = (parent: MdNode): void => {
      const kids = parent.children
      if (!kids) return
      // Every decision below is made against ONE frozen snapshot of the
      // siblings: the pairing map, the verbatim window and the neighbour-line
      // checks all speak original indices. Rewritten text nodes are collected
      // and the children array is rebuilt once at the end -- splicing while
      // iterating would shift every index the pairing map still holds.
      const orig = kids.slice()
      // Paired raw HTML: `<code>` … `</code>` around a text sibling makes that
      // text verbatim even though remark typed it `text`. Only a tag that IS
      // closed among these siblings opens a context (an unclosed tag is a lone
      // tag, and what follows it is prose) -- the same rule the renderer's
      // verbatim-unknown-tags pass applies.
      let verbatimUntil = -1
      // Pairing is computed ONCE per sibling list (linear), never per opener.
      let pairs: Map<number, number> | null = null
      let rebuilt: MdNode[] | null = null
      for (let idx = 0; idx < orig.length; idx++) {
        const node = orig[idx]
        if (node.type === 'html') {
          if (idx > verbatimUntil) {
            const v = (node.value ?? '').trim()
            const tag = singleTagName(v)
            if (tag !== undefined && isOpeningTag(v) && isVerbatimTag(tag)) {
              pairs ??= pairedCloseIndices(orig)
              const close = pairs.get(idx) ?? -1
              if (close > idx) verbatimUntil = close
            }
          }
          rebuilt?.push(node)
          continue
        }
        if (OPAQUE.has(node.type)) {
          rebuilt?.push(node)
          continue
        }
        if (node.type !== 'text') {
          transformChildren(node)
          rebuilt?.push(node)
          continue
        }
        if (idx <= verbatimUntil) {
          rebuilt?.push(node)
          continue
        }
        // Display math is flow content: only a paragraph's direct text can be
        // split around it (splitParagraphs lifts it out). Inside a heading,
        // table cell, or inline container the same delimiters stay inline.
        // And a delimiter on the node's first/last line owns that line only if
        // no sibling shares it -- the slice alone cannot see `**bold**` before
        // it, but the sibling's position can.
        const prev = orig[idx - 1]
        const next = orig[idx + 1]
        const ctx: SpanContext = {
          display: parent.type === 'paragraph',
          prevSharesLine:
            prev !== undefined && prev.position?.end.line === node.position?.start.line,
          nextSharesLine:
            next !== undefined && next.position?.start.line === node.position?.end.line,
        }
        const replacement = rewriteText(node, ctx)
        if (!replacement) {
          rebuilt?.push(node)
          continue
        }
        // First rewrite: start the rebuilt list with everything already walked.
        rebuilt ??= orig.slice(0, idx)
        rebuilt.push(...replacement)
      }
      if (rebuilt) parent.children = rebuilt
    }

    // Display math is FLOW content: a paragraph that now contains `math` nodes
    // is split around them so the tree stays valid mdast (and rehype does not
    // have to unwrap a <pre> out of a <p>). Every synthesized paragraph carries
    // the source span of its children so `rehypeSourcepos` anchors correctly.
    const splitParagraphs = (parent: MdNode): void => {
      const kids = parent.children
      if (!kids) return
      for (let idx = 0; idx < kids.length; idx++) {
        const node = kids[idx]
        if (node.type !== 'paragraph') {
          if (!OPAQUE.has(node.type)) splitParagraphs(node)
          continue
        }
        const inline = node.children ?? []
        if (!inline.some((c) => c.type === 'math')) continue
        const out: MdNode[] = []
        let run: MdNode[] = []
        const flush = () => {
          const meaningful = run.some((c) => c.type !== 'text' || (c.value ?? '').trim() !== '')
          if (meaningful) {
            const first = run[0].position
            const last = run[run.length - 1].position
            const para: MdNode = { type: 'paragraph', children: run }
            if (first && last) para.position = { start: first.start, end: last.end }
            out.push(para)
          }
          run = []
        }
        for (const c of inline) {
          if (c.type === 'math') {
            flush()
            out.push(c)
          } else {
            run.push(c)
          }
        }
        flush()
        kids.splice(idx, 1, ...out)
        idx += out.length - 1
      }
    }

    transformChildren(tree)
    splitParagraphs(tree)
  }
}
