/**
 * Input-side markdown nesting clamp.
 *
 * Chat messages are input-controlled markdown. Deeply nested CONTAINER
 * prefixes -- blockquote markers and list-item markers, in any interleaving
 * CommonMark allows (`>>>>…`, `>  >  >  …`, `> - > - …`) -- and progressively
 * indented list items parse into a tree whose depth equals the nesting count.
 * Recursive layers downstream (remark-rehype's mdast->hast transform first,
 * then this module's own tree walkers) recurse to that depth and throw
 * `RangeError: Maximum call stack size exceeded` while rendering a single
 * message. Measured on this codebase: micromark parse survives a 5,000-deep
 * blockquote run in ~70ms, but the mdast->hast transform overflows the stack --
 * so no per-walker guard inside our code can reach the crash. The only fix
 * shape that covers every recursive layer at once is clamping nesting depth in
 * the SOURCE STRING before it is parsed, at the single choke point where
 * message markdown enters the renderer.
 *
 * The unit of clamping is the CommonMark CONTAINER, not any one marker shape:
 * per spec, each container level is up to 3 spaces of indent plus either a
 * blockquote marker (`>`, one following space/tab consumed) or a list-item
 * marker (`-` / `*` / `+` / `1.` / `1)`, one following space/tab required).
 * Counting only consecutive `>` characters would miss `>  >  >` (the 0-3
 * space re-open) and `> - > -` (quote/list interleave) -- the same tree-depth
 * class through a different spelling.
 *
 * RAW HTML is the third spelling of the same class. The renderer wires
 * `rehype-raw` into both rehype pipelines, so raw HTML embedded in a message
 * is parsed (parse5 semantics) into HAST that the module's recursive walkers
 * traverse BEFORE sanitization ever runs. A run of open tags (`<div><div>…`)
 * therefore builds tree depth exactly like a `>` run, unclamped by the
 * container pass. The HTML pass below tracks tag-nesting depth per message
 * with a conservative model of parse5's stack of open elements and, past
 * MAX_HTML_TAG_DEPTH, neutralizes further opening tags by rewriting their
 * `<` to `&lt;` -- literal text to BOTH micromark (entity in paragraph text)
 * and parse5 (entity inside an already-open HTML block), so no spelling
 * context lets a neutralized tag re-enter tag parsing. `&lt;` costs 3 extra
 * bytes per neutralized tag but is the only rewrite that is inert in both
 * contexts (a backslash escape is CommonMark-only: inside an open HTML block
 * a `\` passes through raw and the tag would still parse).
 *
 * The HTML depth model errs OVER-COUNT ONLY (fail-closed: worst case is
 * cosmetic neutralization of pathological content, never an unclamped deep
 * run). Concretely, judged by parse5's semantics:
 *   - Void elements never push (they cannot nest).
 *   - Self-closing syntax on a NON-VOID element (`<div/>`) still pushes:
 *     HTML ignores the slash there, so the tag opens -- treating it as
 *     closed would under-count (the exact alternate-spelling bypass).
 *     Foreign-content self-closers (`<svg/>`) are honored by parse5 but
 *     still counted here as opens: over-count, safe.
 *   - A start tag whose name equals the stack top AND is in the HTML5
 *     same-name implied-end set (`li`, `p`, `td`, `tr`, `option`, …)
 *     REPLACES the top instead of pushing -- parse5 closes the previous
 *     sibling, so depth is flat (`<li><li><li>` never nests). Cross-name
 *     implied closes (`<tr>` ending an open `<td>`) are NOT modeled:
 *     close-omitted mixed sequences over-count, safe, documented.
 *   - A close tag pops ONLY when it matches the stack top exactly.
 *     Mismatched closes are ignored (parse5's recovery never leaves the
 *     real stack deeper than this model's). Popping on any-name closes
 *     would let interleaved bogus closers (`</span>` under a `<div>` run)
 *     drain the counter while real depth grows -- an under-count bypass.
 *   - Tag text inside inline CODE SPANS is literal to micromark, so parse5
 *     never sees it. A close tag inside a code span must therefore NOT pop
 *     (fake-close bypass: real opens outside spans, backticked closers
 *     draining the counter). The span mask below approximates CommonMark
 *     code spans conservatively: within a line, equal-length backtick runs
 *     pair left-to-right; an unpaired opening run latches "maybe in span"
 *     across lines until a candidate closing run appears. While masked,
 *     closes never pop and opens still COUNT (if the mask is wrong about a
 *     real open, counting it keeps the model safe) but are never rewritten
 *     (rewriting non-tags corrupts visible code text for no depth gain).
 *   - `>` inside quoted attribute values does not end a tag, and a
 *     `</div>` inside a quoted attribute value is attribute data, not a
 *     close tag (`<div data-x="</div>">` opens ONE div). The tag scanner
 *     consumes quoted attribute values, and an unterminated tag latches
 *     "in tag" across lines; `<` inside an open tag never starts a new tag
 *     (parse5 reads it as attribute junk).
 *   - Comments (`<!--`), doctypes, processing instructions and autolinks
 *     (`<https://…>`) never match the tag-name shape and are ignored.
 *
 * Clamping is minimal-mutation: a line is rewritten only when its leading
 * container run exceeds MAX_BLOCKQUOTE_DEPTH units, and the rewrite keeps the
 * first MAX_BLOCKQUOTE_DEPTH units BYTE-IDENTICAL, then inserts a single
 * backslash before the next marker's punctuation. Per CommonMark, the escaped
 * marker is literal text, so container parsing stops there and the remainder
 * of the line (kept byte-identical) renders as visible content at the clamp
 * depth. Nothing is dropped, and source-position drift on a clamped line is
 * bounded to one inserted byte. Ordinary content is untouched. A line whose
 * list-item indent exceeds MAX_LIST_INDENT_COLS has the indent truncated (the
 * progressive-indent vector; multi-line, so per-line marker counting cannot
 * see it). Known cosmetic edge, fail-closed by design: a thematic break
 * spelled `- - - -…` past the bound counts as list units and gets clamped --
 * marker art is rewritten rather than ever letting a deep run through.
 * The HTML pass shares the philosophy: everything below the depth bound is
 * byte-identical, and past it only the neutralized tags' `<` bytes change.
 *
 * Fenced code blocks are exempt: marker runs inside ``` / ~~~ fences are
 * literal text, not nesting. The standard for "is this line a fence?" is
 * MICROMARK's CommonMark semantics (spec 4.5), not `fixCodeFences`' looser
 * line shape: in particular a backtick fence's info string may not contain a
 * backtick, so such a line is a paragraph to the parser and must NOT open an
 * exemption window here. Where this pass is unsure it fails CLOSED (does not
 * enter fence state): the worst case is cosmetic clamping of marker art the
 * parser would have treated as fenced, never an unclamped deep run reaching
 * the parser. Fence exemption covers the HTML pass identically: tags inside
 * genuine fences are literal text and are neither counted nor rewritten.
 *
 * HTML BLOCKS shield fences the same way fences shield tags -- and the guard
 * must judge both windows by the parser's semantics (CommonMark 4.6). A line
 * opening an HTML block (`<div>`, `<pre>`, `<!--`, a complete tag alone on a
 * line, ...) swallows every following line as RAW BLOCK CONTENT until the
 * block's end condition -- including a ```-shaped line, which is NOT a fence
 * to micromark there. Entering fence state on it would exempt everything
 * after (a guard bypass: a 3-token shield prefix carrying an unclamped deep
 * tag run to parse5). So while an HTML-block latch is open: fence-OPEN
 * recognition is suppressed (backtick and tilde alike -- suppression is
 * context-based, not marker-based); the container/indent clamps do not apply
 * (`>` runs there are literal text to micromark, not containers); the inline
 * code-span mask is inert (no spans exist in raw content -- a swallowed
 * ``` line must neither open a fence nor latch the span mask, or masked
 * past-bound opens would flow through unrewritten); and the HTML tag pass
 * KEEPS applying, because parse5 nests raw block content -- this is exactly
 * where the tag clamp must keep working. End conditions per the spec's
 * table: known-block-name and complete-tag-alone openers (conditions 6/7)
 * end BEFORE a blank line; `<pre`/`<script`/`<style`/`<textarea` (condition
 * 1) at a line containing the matching textual closer; `<!--` at `-->`;
 * `<?` at `?>`; `<!LETTER` at `>`; `<![CDATA[` at `]]>` -- closers may match
 * on the opening line itself (a single-line block latches nothing). Openers
 * are detected after stripping the leading container run (`- <div>` opens a
 * block inside a list item). Fail-closed approximations, all over-latch
 * (cosmetic clamping of content the parser might have fenced, never an
 * unclamped deep run): the latch is tracked globally rather than per
 * container stack, condition 7's attribute grammar is approximated
 * quote-aware rather than validated, and paragraph-interruption rules are
 * not modeled. Block tag names come from micromark's own
 * micromark-util-html-tag-name list, so the guard's notion of "block name"
 * is exactly the downstream parser's.
 */

/** Maximum retained leading container depth per line: blockquote markers and
 *  list-item markers combined. (Name kept from the blockquote-only first
 *  version of this guard; tests import it.) */
export const MAX_BLOCKQUOTE_DEPTH = 100

/** Maximum retained leading indent (chars) for a list-item line. With 2-space
 *  indents this allows ~128 genuine nesting levels -- far past anything a
 *  human writes, far below the ~thousands where recursion overflows. */
const MAX_LIST_INDENT_COLS = 256

/** Maximum retained raw-HTML tag-nesting depth per message. Benign HTML in
 *  chat rarely nests past a few dozen levels; recursion overflows live in the
 *  thousands. Sequential (non-nesting) markup is unaffected: well-formed
 *  open/close pairs oscillate the tracked depth, and same-name implied-end
 *  elements (`<li><li>…`) replace rather than push. */
export const MAX_HTML_TAG_DEPTH = 100

/** One CommonMark container unit: up to 3 spaces of indent, then either a
 *  blockquote marker (optional one space/tab consumed) or a list-item marker
 *  (bullet or ordered; one space/tab -- or end of line for an empty item --
 *  required, consumed when present). Sticky: scanned iteratively from the
 *  line start so interleaved quote/list prefixes count as one run. */
const CONTAINER_UNIT = /(?: {0,3})(?:>[ \t]?|(?:[-*+]|\d{1,9}[.)])(?:[ \t]|$))/y

/** List-item marker after an indent: bullet or ordered (CommonMark shapes). */
const LIST_AFTER_INDENT = /^([ \t]+)(?=(?:[-*+]|\d{1,9}[.)])[ \t])/

/** Fence open/close detection, mirroring fixCodeFences. */
const FENCE_LINE = /^ {0,3}(```+|~~~+)/

/** HTML void elements: never push nesting depth (cannot contain children). */
const VOID_ELEMENTS = new Set([
  'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input',
  'link', 'meta', 'param', 'source', 'track', 'wbr',
])

/** Elements whose start tag implies closing a same-name open sibling (HTML5
 *  implied-end-tags): `<li><li>` is flat, not nested. Replacing the stack top
 *  for these matches parse5; every other same-name repeat genuinely nests
 *  (`<div><div>`) and must push. */
const SAME_NAME_IMPLIED_END = new Set([
  'p', 'li', 'dd', 'dt', 'option', 'optgroup', 'rt', 'rp', 'td', 'th', 'tr',
])

/** CommonMark 4.6 condition-6 tag names, copied from the renderer's own
 *  parser dependency (micromark-util-html-tag-name htmlBlockNames), so the
 *  guard's notion of "known block name" is exactly micromark's. */
const HTML_BLOCK_NAMES = new Set([
  'address', 'article', 'aside', 'base', 'basefont', 'blockquote', 'body',
  'caption', 'center', 'col', 'colgroup', 'dd', 'details', 'dialog', 'dir',
  'div', 'dl', 'dt', 'fieldset', 'figcaption', 'figure', 'footer', 'form',
  'frame', 'frameset', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'head', 'header',
  'hr', 'html', 'iframe', 'legend', 'li', 'link', 'main', 'menu', 'menuitem',
  'nav', 'noframes', 'ol', 'optgroup', 'option', 'p', 'param', 'search',
  'section', 'summary', 'table', 'tbody', 'td', 'tfoot', 'th', 'thead',
  'title', 'tr', 'track', 'ul',
])

/** CommonMark 4.6 condition-1 raw tag names (micromark htmlRawNames). */
const HTML_RAW_NAMES = new Set(['pre', 'script', 'style', 'textarea'])

/** Condition-1 textual end markers (spec: the line CONTAINS one of these). */
const HTML_RAW_CLOSERS = ['</pre>', '</script>', '</style>', '</textarea>']

/** HTML-block latch kinds. 0 = none. BLANK_ENDED covers conditions 6 and 7
 *  (both end before a blank line); 1-5 end at their textual closers. */
const HTML_BLOCK_NONE = 0
const HTML_BLOCK_RAW = 1
const HTML_BLOCK_COMMENT = 2
const HTML_BLOCK_PI = 3
const HTML_BLOCK_DECL = 4
const HTML_BLOCK_CDATA = 5
const HTML_BLOCK_BLANK_ENDED = 6

/**
 * Scan the line's leading container run. Returns the unit count capped at
 * maxUnits+1 (enough to know "exceeds") and the string offset immediately
 * after unit #maxUnits (the clamp boundary when exceeded).
 */
function scanContainerRun(line: string, maxUnits: number): { count: number; boundary: number } {
  let count = 0
  let pos = 0
  let boundary = 0
  // Stops at maxUnits+1: the clamp needs only "exceeded" plus the boundary,
  // so per-line work is bounded by the clamp constant, not the run length.
  while (count <= maxUnits) {
    CONTAINER_UNIT.lastIndex = pos
    const m = CONTAINER_UNIT.exec(line)
    if (!m) break
    pos = CONTAINER_UNIT.lastIndex
    count++
    if (count === maxUnits) boundary = pos
  }
  return { count, boundary }
}

/**
 * Neutralize the container marker that starts at or after `from`: insert one
 * backslash before its punctuation so the parser reads literal text instead
 * of another container. Escaping the punctuation covers every unit shape
 * (`\>`, `\-`, `\*`, `\+`, `1\.`, `1\)` are all literal per CommonMark).
 */
function escapeNextMarker(line: string, from: number): string {
  let i = from
  while (i < line.length && line.charCodeAt(i) === 32 /* ' ' */) i++ // <=3 by construction
  if (line.charCodeAt(i) >= 48 && line.charCodeAt(i) <= 57 /* 0-9 */) {
    // Ordered marker: escape its '.'/')' (the scan matched, so it is there).
    while (i < line.length && line.charCodeAt(i) >= 48 && line.charCodeAt(i) <= 57) i++
  }
  return line.slice(0, i) + '\\' + line.slice(i)
}

function clampLine(line: string, maxDepth: number, maxListIndentCols: number): string {
  const run = scanContainerRun(line, maxDepth)
  let out = line
  let contentAt = 0
  if (run.count > maxDepth) {
    // Keep the first maxDepth units byte-identical; escape the next marker so
    // everything after renders as literal text at the clamp depth.
    out = escapeNextMarker(line, run.boundary)
    return out // remainder is literal text now; indent clamp is moot
  }
  // Unclamped run: the indent clamp applies to the remainder after the
  // container prefix, so a moderate prefix cannot smuggle an unbounded list
  // indent behind it.
  if (run.count > 0) {
    // Re-scan cheaply to the run end (count <= maxDepth, bounded work).
    let pos = 0
    for (let n = 0; n < run.count; n++) {
      CONTAINER_UNIT.lastIndex = pos
      CONTAINER_UNIT.exec(line)
      pos = CONTAINER_UNIT.lastIndex
    }
    contentAt = pos
  }
  const rest = out.slice(contentAt)
  const li = LIST_AFTER_INDENT.exec(rest)
  if (li && li[1].length > maxListIndentCols) {
    out = out.slice(0, contentAt) + ' '.repeat(maxListIndentCols) + rest.slice(li[1].length)
  }
  return out
}

const CH_LT = 60 // '<'
const CH_GT = 62 // '>'
const CH_SLASH = 47 // '/'
const CH_BACKTICK = 96 // '`'
const CH_SQUOTE = 39 // "'"
const CH_DQUOTE = 34 // '"'

function isNameStart(c: number): boolean {
  return (c >= 65 && c <= 90) || (c >= 97 && c <= 122) // A-Z a-z
}
function isNameChar(c: number): boolean {
  return isNameStart(c) || (c >= 48 && c <= 57) || c === 45 // 0-9 -
}

/** Cross-line state for the HTML tag-nesting pass. */
interface HtmlScanState {
  /** Conservative model of parse5's stack of open elements (names, lowercase). */
  stack: string[]
  /** Inside an unterminated tag's attribute region (tag spans lines). */
  inTag: boolean
  /** Inside a quoted attribute value within that tag (quote char code, or 0). */
  tagQuote: number
  /** Latched "maybe inside an inline code span" (unpaired backtick run seen). */
  maybeInSpan: boolean
}

/**
 * Detect an HTML-block start condition (CommonMark 4.6) at the start of a
 * line, after stripping the leading container run and up to 3 spaces of
 * indent (`- <div>` opens a block inside a list item). Returns the latch
 * kind, or HTML_BLOCK_NONE. Fail-closed direction: an over-detected open
 * only suppresses fence exemption (cosmetic clamping), never widens one.
 */
function htmlBlockOpenKind(line: string): number {
  let pos = 0
  for (;;) {
    CONTAINER_UNIT.lastIndex = pos
    if (!CONTAINER_UNIT.exec(line)) break
    pos = CONTAINER_UNIT.lastIndex
  }
  let i = pos
  let sp = 0
  while (i < line.length && line.charCodeAt(i) === 32 && sp < 3) {
    i++
    sp++
  }
  if (line.charCodeAt(i) !== CH_LT) return HTML_BLOCK_NONE
  const c1 = i + 1 < line.length ? line.charCodeAt(i + 1) : 0
  if (c1 === 33 /* '!' */) {
    if (line.startsWith('<!--', i)) return HTML_BLOCK_COMMENT
    if (line.startsWith('<![CDATA[', i)) return HTML_BLOCK_CDATA
    const c2 = i + 2 < line.length ? line.charCodeAt(i + 2) : 0
    return isNameStart(c2) ? HTML_BLOCK_DECL : HTML_BLOCK_NONE
  }
  if (c1 === 63 /* '?' */) return HTML_BLOCK_PI
  const isClose = c1 === CH_SLASH
  const j = isClose ? i + 2 : i + 1
  if (j >= line.length || !isNameStart(line.charCodeAt(j))) return HTML_BLOCK_NONE
  let k = j + 1
  while (k < line.length && isNameChar(line.charCodeAt(k))) k++
  const name = line.slice(j, k).toLowerCase()
  const after = k < line.length ? line.charCodeAt(k) : 0
  if (!isClose && HTML_RAW_NAMES.has(name)) {
    // Condition 1: `<pre`/`<script`/`<style`/`<textarea` + ws / `>` / EOL.
    return after === 0 || after === 32 || after === 9 || after === CH_GT
      ? HTML_BLOCK_RAW
      : HTML_BLOCK_NONE
  }
  // Condition 6: known block name (open or close) + ws / `>` / `/>` / EOL;
  // content after the tag is allowed.
  const delimited =
    after === 0 || after === 32 || after === 9 || after === CH_GT ||
    (after === CH_SLASH && k + 1 < line.length && line.charCodeAt(k + 1) === CH_GT)
  if (delimited && HTML_BLOCK_NAMES.has(name)) return HTML_BLOCK_BLANK_ENDED
  // Condition 7: a COMPLETE tag ALONE on the line (whitespace-only tail).
  // Open tags of the raw names were handled above; close tags of any name
  // qualify. Attribute grammar approximated quote-aware (over-latch, safe).
  let e = k
  let quote = 0
  while (e < line.length) {
    const ce = line.charCodeAt(e)
    if (quote !== 0) {
      if (ce === quote) quote = 0
    } else if (ce === CH_SQUOTE || ce === CH_DQUOTE) {
      quote = ce
    } else if (ce === CH_GT) {
      break
    }
    e++
  }
  if (e >= line.length) return HTML_BLOCK_NONE // no `>`: not a complete tag
  let t = e + 1
  while (
    t < line.length &&
    (line.charCodeAt(t) === 32 || line.charCodeAt(t) === 9 || line.charCodeAt(t) === 13)
  ) {
    t++
  }
  return t >= line.length ? HTML_BLOCK_BLANK_ENDED : HTML_BLOCK_NONE
}

/** End condition for closer-ended latch kinds (1-5) on this content line.
 *  Blank-ended kinds (6/7) are handled by the caller's blank-line check. */
function htmlBlockEndsOnLine(kind: number, line: string): boolean {
  switch (kind) {
    case HTML_BLOCK_RAW: {
      const lower = line.toLowerCase()
      return HTML_RAW_CLOSERS.some((c) => lower.includes(c))
    }
    case HTML_BLOCK_COMMENT:
      return line.includes('-->')
    case HTML_BLOCK_PI:
      return line.includes('?>')
    case HTML_BLOCK_DECL:
      return line.includes('>')
    case HTML_BLOCK_CDATA:
      return line.includes(']]>')
    default:
      return false
  }
}

/**
 * Scan one line for raw HTML tags, updating the depth model and neutralizing
 * (`<` -> `&lt;`) opening tags that would push past maxDepth. Returns the
 * (possibly rewritten) line. All model errors are over-count-only; see the
 * module header for the semantics table. With `raw` set the line is HTML
 * BLOCK content (CommonMark 4.6): backticks are plain text there -- no code
 * span can exist, so they neither pair, latch, nor mask (a masked open past
 * the bound would flow through unrewritten -- the html-block shield bypass).
 */
function clampHtmlLine(line: string, st: HtmlScanState, maxDepth: number, raw: boolean): string {
  let out = ''
  let emitted = 0 // source index up to which `out` has been appended
  let i = 0
  const n = line.length
  // Line-local span mask: pair equal-length backtick runs left to right.
  // `maybeInSpan` carries an unpaired opener across lines; any backtick run
  // on a later line is treated as its potential closer (conservative both
  // ways: while masked, closes never pop and opens still count).
  let spanRun = 0 // open backtick run length within this line (0 = none)

  while (i < n) {
    const c = line.charCodeAt(i)
    // Resume an unterminated tag from a previous line: consume attribute
    // region (respecting quotes) until `>`; no new tag can start inside.
    if (st.inTag) {
      if (st.tagQuote !== 0) {
        if (c === st.tagQuote) st.tagQuote = 0
      } else if (c === CH_SQUOTE || c === CH_DQUOTE) {
        st.tagQuote = c
      } else if (c === CH_GT) {
        st.inTag = false
      }
      i++
      continue
    }
    if (c === CH_BACKTICK) {
      if (raw) {
        i++ // raw block content: a backtick is just a byte
        continue
      }
      let runLen = 1
      while (i + runLen < n && line.charCodeAt(i + runLen) === CH_BACKTICK) runLen++
      if (st.maybeInSpan) {
        st.maybeInSpan = false // candidate closer for a cross-line span
      } else if (spanRun === 0) {
        spanRun = runLen // open a line-local span
      } else if (runLen === spanRun) {
        spanRun = 0 // close the line-local span (equal run per CommonMark)
      }
      i += runLen
      continue
    }
    const masked = !raw && (spanRun !== 0 || st.maybeInSpan)
    if (c !== CH_LT) {
      i++
      continue
    }
    // Possible tag at i. Classify by the character after '<'.
    const c1 = i + 1 < n ? line.charCodeAt(i + 1) : 0
    if (c1 === CH_SLASH) {
      // Close tag candidate: </name \s* >
      let j = i + 2
      if (j < n && isNameStart(line.charCodeAt(j))) {
        let k = j + 1
        while (k < n && isNameChar(line.charCodeAt(k))) k++
        let e = k
        while (e < n && (line.charCodeAt(e) === 32 || line.charCodeAt(e) === 9)) e++
        if (e < n && line.charCodeAt(e) === CH_GT) {
          const name = line.slice(j, k).toLowerCase()
          // Pop only on exact top match, and never from inside a code-span
          // mask (fake-close bypass). Mismatched closes are ignored.
          if (!masked && st.stack.length > 0 && st.stack[st.stack.length - 1] === name) {
            st.stack.pop()
          }
          i = e + 1
          continue
        }
      }
      i++ // malformed close: literal text to parse5, ignore
      continue
    }
    if (!isNameStart(c1)) {
      i++ // comment/doctype/PI/autolink/stray '<': never a start tag shape
      continue
    }
    // Start tag: read the name, then consume attributes respecting quotes.
    let k = i + 2
    while (k < n && isNameChar(line.charCodeAt(k))) k++
    // The char after the name must end the name region (ws, '/', '>', or the
    // line ending inside the tag); anything else (e.g. ':') is not a tag.
    const after = k < n ? line.charCodeAt(k) : 0
    if (k < n && after !== CH_GT && after !== CH_SLASH && after !== 32 && after !== 9) {
      i++
      continue
    }
    const name = line.slice(i + 1, k).toLowerCase()
    // Consume the attribute region.
    let e = k
    let quote = 0
    let closed = false
    while (e < n) {
      const ce = line.charCodeAt(e)
      if (quote !== 0) {
        if (ce === quote) quote = 0
      } else if (ce === CH_SQUOTE || ce === CH_DQUOTE) {
        quote = ce
      } else if (ce === CH_GT) {
        closed = true
        break
      }
      e++
    }
    if (!closed) {
      // Tag spans lines: latch and stop scanning this line. The open is
      // PROCESSED NOW (counted / neutralized) -- deferring it would let a
      // line-spanning tag escape the model.
      st.inTag = true
      st.tagQuote = quote
    }
    const tagEnd = closed ? e + 1 : n
    if (VOID_ELEMENTS.has(name)) {
      i = tagEnd
      continue // voids never nest; self-closing slash irrelevant
    }
    // NOTE (parse5 semantics): a trailing '/' on a non-void, non-foreign
    // element is IGNORED -- `<div/>` still opens. Counting it as an open is
    // required, not conservative. Foreign-content self-closers (`<svg/>`)
    // are honored by parse5; counting them anyway is over-count, safe.
    if (
      st.stack.length > 0 &&
      st.stack[st.stack.length - 1] === name &&
      SAME_NAME_IMPLIED_END.has(name)
    ) {
      i = tagEnd
      continue // sibling replaces top: depth unchanged (`<li><li>` is flat)
    }
    if (st.stack.length >= maxDepth) {
      if (!masked) {
        // Neutralize: this open would exceed the bound. `&lt;` is literal in
        // both micromark and parse5 contexts, so the tag cannot parse.
        out += line.slice(emitted, i) + '&lt;'
        emitted = i + 1
        // Not pushed (it is text now). If the tag was unterminated, the
        // latch above is WRONG for a neutralized tag (its `>` is plain text
        // now) -- clear it: subsequent text scans normally.
        if (!closed) {
          st.inTag = false
          st.tagQuote = 0
        }
      }
      // Masked pseudo-tag past bound: counted nowhere, rewritten never.
      i = tagEnd
      continue
    }
    st.stack.push(name)
    i = tagEnd
  }
  // An unpaired line-local backtick run at end of line may open a multi-line
  // code span: latch conservatively (closes stop popping until the next
  // backtick run; opens keep counting either way).
  if (spanRun !== 0) st.maybeInSpan = true
  if (emitted === 0) return line
  return out + line.slice(emitted)
}

export function clampNestingDepth(s: string): string {
  // Cheap bail: no single line can exceed the container/indent bounds if the
  // whole string is shorter than the smaller bound, and a string that short
  // cannot hold MAX_HTML_TAG_DEPTH open tags either (each costs >= 3 chars).
  if (s.length <= Math.min(MAX_BLOCKQUOTE_DEPTH, MAX_LIST_INDENT_COLS)) return s
  const lines = s.split('\n')
  let inFence = false
  let fenceMarker = ''
  let changed = false
  let htmlBlock = HTML_BLOCK_NONE
  const html: HtmlScanState = { stack: [], inTag: false, tagQuote: 0, maybeInSpan: false }
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i]
    if (htmlBlock !== HTML_BLOCK_NONE) {
      // Inside an open HTML block: every line is RAW BLOCK CONTENT to
      // micromark until the block's end condition -- never a fence open,
      // never a container, never inline-span text. The HTML tag pass still
      // applies: parse5 nests raw block content, so this is exactly where
      // the clamp must keep working.
      if (htmlBlock === HTML_BLOCK_BLANK_ENDED && /^[ \t\r]*$/.test(line)) {
        htmlBlock = HTML_BLOCK_NONE // conditions 6/7 end before a blank line
        continue
      }
      let cur = line
      if (html.inTag || cur.indexOf('<') >= 0) {
        cur = clampHtmlLine(cur, html, MAX_HTML_TAG_DEPTH, true)
      }
      if (htmlBlock !== HTML_BLOCK_BLANK_ENDED && htmlBlockEndsOnLine(htmlBlock, line)) {
        htmlBlock = HTML_BLOCK_NONE // closer line is still block content
      }
      if (cur !== line) {
        lines[i] = cur
        changed = true
      }
      continue
    }
    const fm = FENCE_LINE.exec(line)
    if (fm) {
      const fence = fm[1]
      if (inFence) {
        if (
          fence[0] === fenceMarker[0] &&
          fence.length >= fenceMarker.length &&
          /^[ \t\r]*$/.test(line.slice(line.indexOf(fence) + fence.length))
        ) {
          inFence = false
        }
        // In-fence lines (content or closer) are exempt either way.
        continue
      }
      // Fence OPEN candidate. CommonMark (spec 4.5): the info string of a
      // BACKTICK fence may not contain a backtick -- micromark reads such a
      // line as a paragraph, not a fence. Opening a fence here would exempt
      // every following line from clamping (a guard bypass), so the
      // fail-closed choice is to NOT enter fence state and let the line fall
      // through to the normal clamp path. Tilde fences may carry any info
      // string per spec.
      const info = line.slice(fm[0].length)
      if (!(fence[0] === '`' && info.includes('`'))) {
        inFence = true
        fenceMarker = fence
        continue
      }
      // else: paragraph line to the parser -- fall through, stays clampable.
    }
    if (inFence) continue
    // Container/indent clamps: per-line, so a short line cannot exceed them
    // (each container unit costs >= 1 char) -- cheap bail applies.
    let cur = line
    if (cur.length > Math.min(MAX_BLOCKQUOTE_DEPTH, MAX_LIST_INDENT_COLS)) {
      cur = clampLine(cur, MAX_BLOCKQUOTE_DEPTH, MAX_LIST_INDENT_COLS)
    }
    // HTML-block OPEN detection (CommonMark 4.6), after the container clamp
    // so the latch sees what the parser will see. The opener line itself is
    // already block content (raw context); a single-line block (closer on
    // the opening line) latches nothing but the line still scans raw.
    let rawLine = false
    if (cur.indexOf('<') >= 0) {
      const kind = htmlBlockOpenKind(cur)
      if (kind !== HTML_BLOCK_NONE) {
        rawLine = true
        // Block boundary: a pending cross-line span candidate belonged to a
        // paragraph that just ended -- it cannot mask block content.
        html.maybeInSpan = false
        if (kind === HTML_BLOCK_BLANK_ENDED || !htmlBlockEndsOnLine(kind, cur)) {
          htmlBlock = kind
        }
      }
    }
    // HTML tag pass: depth is CUMULATIVE ACROSS LINES (unclosed opens stack
    // up in parse5 across the whole message), so the per-line length bail
    // must not gate it -- 200 short `<div>` lines nest 200 deep. Cheap gate:
    // only lines that contain '<' (or continue an unterminated tag) matter.
    if (html.inTag || cur.indexOf('<') >= 0 || cur.indexOf('`') >= 0) {
      cur = clampHtmlLine(cur, html, MAX_HTML_TAG_DEPTH, rawLine)
    }
    if (cur !== line) {
      lines[i] = cur
      changed = true
    }
  }
  return changed ? lines.join('\n') : s
}
