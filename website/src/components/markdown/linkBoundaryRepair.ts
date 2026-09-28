import type { MdastNode } from './sanitize'

// ── CJK autolink boundaries ────────────────────────────────────────────────
//
// GFM's autolink-literal extension ends a bare `https://…` run only at ASCII
// whitespace or `<`. CJK punctuation written directly after a URL — the way
// Chinese and Japanese prose actually writes it, with no space — is therefore
// swallowed INTO the href:
//
//   （https://example.com/pull/1，`abc`）：`ready`
//   -> href="https://example.com/pull/1%EF%BC%8C%60abc%60…"
//
// The wrong href is the smaller half of the damage. The run also eats the
// OPENING backtick of the code span that follows, which shifts every later
// backtick pairing in the paragraph by one: prose renders as inline code and
// real code renders with literal backticks. One missing space corrupts the
// rest of the message.
//
// The same swallow takes the CLOSING `**` of a bold-wrapped URL, the shape
// `**https://…**（revision 1）` that CJK prose writes with no space between the
// emphasis and the punctuation after it. GFM trims a TRAILING `*`, so this only
// breaks when a non-space follows: the `**` stops being a delimiter, the opening
// `**` renders as two literal asterisks, and every later `**` in the paragraph
// re-pairs against the wrong partner.
//
// This has to be fixed at the SOURCE level, not on the mdast: re-splitting the
// link node after the fact cannot restore the code-span pairing, because the
// pairing is decided while micromark tokenizes the whole paragraph. So force
// the boundary before parsing by re-emitting the URL head as an angle autolink
// `<url>`, which has an explicit end and renders identically.
//
// The cut is EVIDENCE-BASED, not character-based — see cjkCutIndex and
// strongDelimCutIndex. CJK punctuation reaches real URLs raw
// (`…/wiki/苹果（公司）`), so cutting on the character alone would break links
// that render correctly today.
//
// Which regions are off-limits is read off remark's OWN parse (see
// autolinkLiteralSpans) rather than a hand-rolled scanner: only a real GFM
// autolink-literal node is ever touched, so code, existing links, raw HTML and
// math are excluded by construction instead of by a mask that has to re-derive
// every CommonMark block and inline rule correctly.
//
// Scope: only `http(s)://` runs. Scheme-less `www.` literals have the same flaw
// but cannot be closed with `<…>` (angle autolinks require a scheme).

// Punctuation classes. CJK punctuation is NOT by itself proof that a URL ended:
// real page titles contain it, and they reach the URL raw —
// `https://zh.wikipedia.org/wiki/苹果（公司）`, `https://zh.wikipedia.org/wiki/我，机器人`,
// `https://ja.wikipedia.org/wiki/モーニング娘。`. Cutting on the character alone
// would break links that render correctly today, so a cut needs EVIDENCE.
const CJK_PUNCT_RE =
  /[\u00b7\u2018\u2019\u201c\u201d\u2026\u3000-\u303f\u30fb\uff01-\uff0f\uff1a-\uff20\uff3b-\uff40\uff5b-\uff65]/
const CJK_OPEN_BRACKETS = '\u3008\u300a\u300c\u300e\u3010\u3014\u3016\u3018\u301a\uff08\uff3b\uff5b\uff5f\uff62'
const CJK_CLOSE_BRACKETS = '\u3009\u300b\u300d\u300f\u3011\u3015\u3017\u3019\u301b\uff09\uff3d\uff5d\uff60\uff63'
// Sentence-ending CJK punctuation. These are NEVER treated as a URL boundary,
// because real page titles end in them and reach the URL raw —
// `…/wiki/モーニング娘。`, `…/wiki/魔法先生ネギま！`, `…/wiki/そして誰もいなくなった…`.
// A separator like `，` or `、` does not end a title, so it stays eligible.
const CJK_SENTENCE_ENDERS = '\u3002\uff0e\uff01\uff1f\u2026\uff61'
// The one character that makes markdown do something AND cannot appear in a
// raw-written URL. RFC 3986 excludes the backtick, so browsers percent-encode
// it — while `*`, `[` and `]` are all legal and common in query strings
// (`?q=foo，*test`, `?filter[name]=x`), so they are NOT evidence. The backtick
// is also the character whose loss does the real damage: the run eats an opening
// code-span delimiter and every later backtick pairing in the paragraph shifts.
const MD_ACTIVE_RE = /`/

// Strong-emphasis delimiters. A single `*` or `_` is NOT included: both are legal
// in a URL and common in query strings (`?q=foo，*test`, `?a=b_c`), so an unpaired
// one before the URL is too weak to act on. A DOUBLED delimiter carries the
// structural evidence instead — see strongDelimCutIndex.
const STRONG_DELIMS = ['**', '__']
const STRONG_DELIM_RE = /\*\*|__/

// CommonMark's character classes for delimiter flanking. Unicode-aware on
// purpose: the text this runs on is CJK prose, where the neighbour of a `**` is
// routinely a fullwidth punctuation mark (`：`, `）`) that ASCII classes miss —
// and misclassifying a neighbour flips whether a run can open emphasis.
const UNICODE_WS_RE = /[\s\p{Zs}]/u
const UNICODE_PUNCT_RE = /[\p{P}\p{S}]/u
/**
 * East Asian WIDE or FULLWIDTH characters — ideographs, kana, hangul, and the
 * fullwidth forms that carry CJK punctuation (`：（），。`). Ambiguous-width marks
 * (`·`, `…`, curly quotes) are deliberately absent: the renderer's amendment
 * treats those as ordinary punctuation, so this class must too.
 */
const CJK_WIDE_RE =
  /[\u1100-\u115f\u2e80-\ua4cf\ua960-\ua97f\uac00-\ud7a3\uf900-\ufaff\ufe10-\ufe19\ufe30-\ufe6f\uff00-\uff60\uffe0-\uffe6]|[\u{20000}-\u{3fffd}]/u

// Where a bare URL may START, and the run GFM's tokenizer would take from there
// (everything up to ASCII whitespace or `<`). Only needed for a SECOND URL
// inside one autolink node's own run.
const URL_START_RE = /https?:\/\//g
const URL_RUN_AT_RE = /^https?:\/\/[^\s<]*/

// GFM only autolinks a host containing a dot, and neither of the last two
// labels may contain `_`. Wrapping a run GFM would NOT have linked would CREATE
// a link the author never wrote, so the head has to clear the same bar.
const AUTOLINKABLE_HOST_RE = /^https?:\/\/([A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)+)(?::\d+)?(?:[/?#]|$)/

/**
 * The renderer's own remark grammar, as a bare parser. `MarkdownRenderer.tsx`
 * passes the one it renders with (`AUTOLINK_PARSER`), so the spans read here are
 * exactly the links the renderer will produce.
 */
export type MarkdownParser = { parse(content: string): unknown }

// Node types whose source text is not prose. A bracket inside one of them is
// part of a URL, a code sample, a tag or a formula — never the `（` that wraps a
// following URL — so they are excluded from the bracket-balance prefix.
const NON_PROSE_TYPES = new Set([
  'inlineCode',
  'code',
  'link',
  'linkReference',
  'image',
  'imageReference',
  'html',
  'math',
  'inlineMath',
  'definition',
  'footnoteDefinition',
])

/**
 * Source offsets of every GFM autolink LITERAL in `content` — a bare
 * `http(s)://…` run that remark turned into a link on its own — plus a mask
 * marking every character that belongs to a non-prose node.
 *
 * Excludes `<https://…>` angle autolinks and `[text](url)` links from the
 * literal list, which already carry explicit boundaries: both can also satisfy
 * `text === url`, so the test is on the source text at the node's start, not on
 * the node shape alone.
 */
function autolinkLiteralSpans(content: string, parser: MarkdownParser): {
  literals: Array<[number, number]>
  nonProse: Uint8Array
} {
  let tree: MdastNode
  try {
    tree = parser.parse(content) as unknown as MdastNode
  } catch {
    // A parse failure must not take the message down with it — the unfixed
    // render is strictly better than no render.
    return { literals: [], nonProse: new Uint8Array(content.length) }
  }
  const literals: Array<[number, number]> = []
  const nonProse = new Uint8Array(content.length)
  const visit = (node: MdastNode): void => {
    const start = node.position?.start.offset
    const end = node.position?.end.offset
    const positioned = typeof start === 'number' && typeof end === 'number'
    if (node.type === 'link' && positioned) {
      const text =
        node.children?.length === 1 && node.children[0].type === 'text' ? node.children[0].value : undefined
      if (text !== undefined && text === node.url && /^https?:\/\//.test(content.slice(start, end))) {
        // Deliberately NOT masked here. A greedy literal run can hold several
        // URLs with real prose between them (`（…/1）和【…/2】` is ONE run), and
        // that prose is where the second URL's bracket context lives. The caller
        // masks each URL's own characters as it consumes them instead.
        literals.push([start as number, end as number])
        return
      }
    }
    if (positioned && NON_PROSE_TYPES.has(node.type)) nonProse.fill(1, start, end)
    for (const child of node.children ?? []) visit(child)
  }
  visit(tree)
  return { literals, nonProse }
}

/**
 * Index in `run` where the URL demonstrably stops, or -1 when there is no
 * evidence that it does. `prefix` is the source text before the URL on its own
 * line — it decides whether a closing bracket had an opener to close.
 *
 * Two things count as evidence:
 *
 *  1. A CJK closing bracket that closes an opener SURROUNDING the URL — one left
 *     unclosed in `prefix` and not opened inside the run. This is GFM's own ASCII
 *     paren-balancing rule, generalised. `（https://x.com/a）` and
 *     `（见 https://x.com/a）` cut; `https://x.com/苹果（公司）` does not (the
 *     opener is inside the URL), and neither does `https://x.com/search?q=foo）`
 *     (nothing to close, so the bracket is plausibly part of the query).
 *  2. A SEPARATOR-class CJK punctuation mark IMMEDIATELY followed by a BACKTICK.
 *     That is the destructive case (the run eats an opening code-span delimiter
 *     and shifts every later pairing) and the backtick is the one character that
 *     cannot appear in a raw-written URL. `…/2137，`96ed647b`）` cuts.
 *
 *     Sentence-enders (`。．！？…｡`) are EXCLUDED from this rule: real page titles
 *     end in them and reach the URL raw, so `…/wiki/モーニング娘。`紹介`` must not
 *     cut. Separators like `，`、`、`、`；`、`：` do not end titles, so they stay
 *     eligible. `…/wiki/我，机器人`简介`` is still safe for a different reason —
 *     its comma is followed by more title, not by the backtick.
 *
 * Deliberately NOT covered: `…/pull/1，然后` — a bare CJK sentence continuing
 * off a URL with no space and no markup. It is character-for-character
 * indistinguishable from a legitimate `…/wiki/我，机器人`, so it keeps today's
 * behaviour rather than risking a correct link.
 */
function cjkCutIndex(run: string, prefix: string): number {
  // Openers left unclosed before the URL, per bracket type: only those have
  // something for a closer inside the run to close.
  const pending = new Map<string, number>()
  for (const ch of prefix) {
    const open = CJK_OPEN_BRACKETS.indexOf(ch)
    if (open >= 0) {
      pending.set(ch, (pending.get(ch) ?? 0) + 1)
      continue
    }
    const close = CJK_CLOSE_BRACKETS.indexOf(ch)
    if (close >= 0) {
      const opener = CJK_OPEN_BRACKETS[close]
      const n = pending.get(opener) ?? 0
      if (n > 0) pending.set(opener, n - 1)
    }
  }
  let depth = 0
  for (let i = 1; i < run.length; i++) {
    const ch = run[i]
    if (CJK_OPEN_BRACKETS.includes(ch)) {
      depth++
      continue
    }
    const close = CJK_CLOSE_BRACKETS.indexOf(ch)
    if (close >= 0) {
      if (depth > 0) {
        depth--
        continue
      }
      if ((pending.get(CJK_OPEN_BRACKETS[close]) ?? 0) > 0) return i
      // No opener to close — the bracket is plausibly part of the URL itself.
      continue
    }
    if (depth > 0 || !CJK_PUNCT_RE.test(ch)) continue
    // Sentence-enders are never a boundary — a real title can end in one. This
    // also means a mixed run like `。，` cuts at the `，`, leaving the `。` inside
    // the URL, because the loop reaches the separator on a later iteration.
    if (CJK_SENTENCE_ENDERS.includes(ch)) continue
    // Walk the contiguous punctuation run — `、，` before a backtick is one
    // boundary, not two — and require the evidence to sit directly after it.
    let end = i
    while (end < run.length && CJK_PUNCT_RE.test(run[end])) end++
    if (end < run.length && MD_ACTIVE_RE.test(run[end])) return i
  }
  return -1
}

/**
 * Whether a delimiter run with `before`/`after` as its neighbours can OPEN
 * and/or CLOSE emphasis, per CommonMark's flanking rules. Callers pass `' '` for
 * start/end of line, which the spec treats as whitespace.
 *
 * This is the load-bearing distinction: a textual count of `**`/`__` cannot tell
 * a real delimiter from a run GFM renders literally. An intraword `__`
 * (`report__final.pdf`) can neither open nor close, and a `**` with whitespace on
 * both sides (`a ** b`) is not flanking at all — treating either as a delimiter
 * truncates a URL that renders correctly today.
 */
function flankingFor(
  before: string,
  after: string,
  ch: string,
): { canOpen: boolean; canClose: boolean } {
  const wsBefore = UNICODE_WS_RE.test(before)
  const wsAfter = UNICODE_WS_RE.test(after)
  // The CJK-friendly amendment that `remark-cjk-friendly` implements — and this
  // pass must measure the SAME grammar the renderer runs — classifies a wide or
  // fullwidth character as CJK rather than as punctuation, so CJK punctuation no
  // longer blocks emphasis, and admits a CJK neighbour where CommonMark admits
  // only whitespace or punctuation. Without this, `**中文。**` reads as two
  // openers here while the renderer pairs it as one closed strong.
  const cjkBefore = CJK_WIDE_RE.test(before)
  const cjkAfter = CJK_WIDE_RE.test(after)
  const punctBefore = UNICODE_PUNCT_RE.test(before) && !cjkBefore
  const punctAfter = UNICODE_PUNCT_RE.test(after) && !cjkAfter
  const leftFlanking = !wsAfter && (!punctAfter || wsBefore || punctBefore || cjkBefore)
  const rightFlanking = !wsBefore && (!punctBefore || wsAfter || punctAfter || cjkAfter)
  // `_` additionally cannot do intraword emphasis; `*` can. That extra condition tests
  // punct-or-whitespace in the RAW sense, where CJK punctuation counts — the amendment
  // above excludes wide characters from the punctuation class used for FLANKING only.
  // Reusing the amended class here would leave a fullwidth `：` reading as neither
  // punctuation nor whitespace, so `：__url__` would look intraword and open nothing.
  if (ch === '_') {
    const rawBefore = wsBefore || UNICODE_PUNCT_RE.test(before)
    const rawAfter = wsAfter || UNICODE_PUNCT_RE.test(after)
    return {
      canOpen: leftFlanking && (!rightFlanking || rawBefore),
      canClose: rightFlanking && (!leftFlanking || rawAfter),
    }
  }
  return { canOpen: leftFlanking, canClose: rightFlanking }
}

/** Flanking for the run at `[start, end)` of `line`. */
function delimFlanking(
  line: string,
  start: number,
  end: number,
  ch: string,
): { canOpen: boolean; canClose: boolean } {
  return flankingFor(
    start > 0 ? line[start - 1] : ' ',
    end < line.length ? line[end] : ' ',
    ch,
  )
}

/**
 * Whether the character at `at` is backslash-escaped, i.e. preceded by an ODD
 * number of backslashes. `\**` is a literal asterisk followed by a lone `*` and
 * cannot be a strong delimiter, while `\\**` escapes the backslash itself and
 * leaves the `**` intact. Parity is what tells those apart.
 */
function isEscapedAt(line: string, at: number): boolean {
  let n = 0
  while (at - n - 1 >= 0 && line[at - n - 1] === '\\') n++
  return n % 2 === 1
}

/**
 * Whether the delimiter run `line[at, end)` sits between two ordinary word
 * characters — neither side whitespace, punctuation, nor CJK. Start of line and
 * end of line count as whitespace, so they are never intraword.
 */
function isIntrawordAt(line: string, at: number, end: number): boolean {
  const before = at > 0 ? line[at - 1] : ' '
  const after = end < line.length ? line[end] : ' '
  const plain = (c: string) =>
    !UNICODE_WS_RE.test(c) && !UNICODE_PUNCT_RE.test(c) && !CJK_WIDE_RE.test(c)
  return plain(before) && plain(after)
}

/**
 * Whether `line[0, upTo)` leaves a strong-emphasis opener OPEN — i.e. the author
 * was still inside a `**`/`__` when the URL started.
 *
 * CommonMark consumes delimiter CHARACTERS, not whole runs, so this counts
 * characters: an unambiguous opener adds its length, an unambiguous closer takes
 * back up to that many, and a strong opener is open when at least two characters
 * are still unmatched. Counting runs instead would call `**foo*` a pending strong
 * opener, when the lone `*` has in fact eaten one of the two and CommonMark renders
 * `*<em>foo</em>` — no strong opener survives to wrap the URL.
 *
 * A run that could be either an opener or a closer (`a**b`) makes the whole line
 * inconclusive, because a count that guesses can be wrong in both directions. A run
 * GFM would render literally — an intraword `__`, or a `**` with whitespace on both
 * sides — is not flanking at all and contributes nothing, which is why a lone `*`
 * used as prose (`2 * 3 = 6`) does not disturb the count.
 */
function hasPendingStrongOpener(line: string, upTo: number, delim: string): boolean {
  const ch = delim[0]
  let open = 0
  let i = 0
  while (i < upTo) {
    if (line[i] !== ch) {
      i++
      continue
    }
    let end = i
    while (end < line.length && line[end] === ch) end++
    if (end > upTo) break
    // An escaped first character is a literal, so the delimiter run effectively
    // starts one character later: `\**` carries no delimiter at all, `\***` carries
    // one. Flanking is then measured from that later start, whose left neighbour is
    // the literal asterisk — punctuation, which is what it renders as.
    const from = isEscapedAt(line, i) ? i + 1 : i
    if (end > from) {
      const { canOpen, canClose } = delimFlanking(line, from, end, ch)
      // An INTRAWORD run — a word character on both sides, no whitespace, no
      // punctuation, no CJK — is the shape every counter-example to this rule has
      // used (`report__final.pdf`, `?q=foo**-bar`). CommonMark lets `*` pair there,
      // but the renderer leaves such a line literal, so claiming to know the pairing
      // is how a working URL gets truncated. Treat the line as inconclusive.
      if (canOpen && canClose && isIntrawordAt(line, from, end)) return false
      // A run that can do both otherwise is the ordinary shape in CJK prose (`：**`,
      // `。**`). The renderer resolves it the way a delimiter stack does: close an
      // opener when one is waiting, otherwise open.
      if (canClose && open >= delim.length) open -= Math.min(end - from, open)
      else if (canOpen) open += end - from
    }
    i = end
  }
  return open >= delim.length
}

/**
 * Index in `run` of a strong-emphasis delimiter the author wrote to CLOSE an
 * opener that sits before the URL, or -1 when there is none.
 *
 * This evidence is structural rather than lexical: it does not claim to know
 * where the URL ended, it observes that the current reading is one no author
 * writes — a delimiter PAIR wrapping the URL, whose closing half GFM has
 * swallowed into the href. Leaving it there costs the emphasis its delimiter, so
 * the opener degrades to two literal asterisks and every later `**` in the
 * paragraph re-pairs against the wrong partner.
 *
 * BOTH ends must be real, and both are judged against the text the author wrote:
 * the prefix must leave an opener open (hasPendingStrongOpener) AND the candidate
 * inside the run must itself be a legitimate closer. Checking only the opener is
 * not enough — `__See https://example.com/a__b for details__` opens a real `__`
 * and then hits an INTRAWORD `__` in the path, which closes nothing, so cutting
 * there truncates a correct link and the emphasis stays open anyway.
 *
 * The candidate must also be followed by CJK PUNCTUATION — the same class the two
 * rules above already act on. Flanking cannot carry this: in `?q=foo**-bar` the
 * `**` has a word character before it and punctuation after it, which is exactly
 * the shape of a real closer before punctuation, and a candidate followed by a
 * fullwidth mark is right-flanking by construction (a run holds no whitespace, and
 * the mark itself is punctuation), so a closer check on it can never refuse
 * anything. The requirement is deliberately narrower than "any non-ASCII": an
 * ideograph is legal mid-path (`?q=a**中文`), so treating one as a boundary would
 * truncate a working URL.
 *
 * Consequences worth knowing, both accepted:
 *  - An all-ASCII paragraph is never cut, and neither is `**url**已合并` (an
 *    ideograph, not punctuation, follows the delimiter). This pass carries `Cjk` in
 *    its name; the shape it is for is `**url**（…` / `**url**，…`.
 *  - A URL whose path genuinely carries a fullwidth mark straight after a `**`
 *    (`…/wiki/苹果**（公司）`) would be cut short. That is the SAME residual risk
 *    rules 1 and 2 already accept, on the same character class.
 *
 * A trailing delimiter never reaches here: GFM trims a trailing `*`/`_` off the
 * autolink literal, so `**https://x.com/a**` — which renders correctly today —
 * yields a node whose source stops at `a`, with no delimiter inside the run.
 */
function strongDelimCutIndex(run: string, prefix: string, suffix: string): number {
  // Flanking is decided by a delimiter's NEIGHBOURS, so the prefix alone is not
  // enough context: the character after a prefix-terminal `**` is the URL's
  // first character.
  const line = prefix + run
  let best = -1
  for (const delim of STRONG_DELIMS) {
    if (!hasPendingStrongOpener(line, prefix.length, delim)) continue
    // If the prose after the URL still has a delimiter available to close that
    // opener, the author's pair spans the URL and the `**` inside it is part of the
    // URL. Cutting there would truncate the href AND orphan the real closer.
    if (hasUnmatchedCloserAfter(suffix, delim)) continue
    for (let at = run.indexOf(delim); at > 0; at = run.indexOf(delim, at + 1)) {
      // An escaped delimiter closes nothing, so it is no evidence of a boundary.
      // Skipping rather than adjusting is enough here: the next iteration starts one
      // character later, which is exactly the run `\***` leaves behind.
      if (isEscapedAt(line, prefix.length + at)) continue
      const after = at + delim.length < run.length ? run[at + delim.length] : ' '
      if (!CJK_PUNCT_RE.test(after)) continue
      if (best < 0 || at < best) best = at
      break
    }
  }
  return best
}

/**
 * The earliest boundary any evidence rule can prove, or -1. Rules are
 * independent: each one alone is enough, and the shortest URL among them is the
 * conservative choice.
 */
function earliestCut(run: string, prefix: string, suffix: string): number {
  const cuts = [cjkCutIndex(run, prefix), strongDelimCutIndex(run, prefix, suffix)].filter(
    (i) => i > 0,
  )
  return cuts.length > 0 ? Math.min(...cuts) : -1
}

function isAutolinkableHost(head: string): boolean {
  const m = AUTOLINKABLE_HOST_RE.exec(head)
  if (!m) return false
  // GFM: `_` is not allowed in either of the last two domain labels.
  return m[1].split('.').slice(-2).every((label) => !label.includes('_'))
}

/**
 * Drop the trailing characters GFM strips from an autolink literal but an angle
 * autolink would keep, so `…/1.，`b`` links `…/1` and leaves `.` as prose.
 */
function trimGfmAutolinkTail(s: string): string {
  let out = s
  for (let guard = 0; guard < s.length; guard++) {
    const next = out.replace(/[?!.,:*_~]+$/, '')
    if (next.endsWith(')')) {
      const open = (next.match(/\(/g) ?? []).length
      const close = (next.match(/\)/g) ?? []).length
      // GFM keeps a `)` that closes a `(` from inside the URL itself.
      if (close > open) {
        out = next.slice(0, -1)
        continue
      }
    }
    if (next === out) return out
    out = next
  }
  return out
}

/**
 * The PROSE text before `at` on its own line, with every non-prose character
 * blanked out. Only this text can supply the opener a closing bracket inside the
 * URL closes — a `（` sitting in an earlier URL's query string, a code sample or
 * an HTML attribute is not bracket context for the URL that follows.
 *
 * Line-scoped on purpose: a paragraph-wide scan would be less conservative, and
 * a cut is the risky direction.
 */
function prosePrefix(content: string, nonProse: Uint8Array, at: number): string {
  const lineStart = content.lastIndexOf('\n', at - 1) + 1
  let out = ''
  for (let i = lineStart; i < at; i++) out += nonProse[i] ? ' ' : content[i]
  return out
}

/** `prosePrefix`'s mirror: the prose from `at` to the end of that line. */
function proseSuffix(content: string, nonProse: Uint8Array, at: number): string {
  let lineEnd = content.indexOf('\n', at)
  if (lineEnd < 0) lineEnd = content.length
  let out = ''
  for (let i = at; i < lineEnd; i++) out += nonProse[i] ? ' ' : content[i]
  return out
}

/**
 * Whether the prose AFTER the URL still offers a delimiter that could close the
 * opener waiting from before it — i.e. the author's pair is `**prose … prose**`
 * and the `**` inside the URL is part of the URL.
 *
 * Parity is the whole point, and it is what makes this usable where a plain
 * "is there another `**` later" test is not: in
 * `已建好：**url**（revision 1），说明见 **文档**。` the two trailing delimiters pair
 * with EACH OTHER, so none is left over for the opener, and the boundary inside
 * the run really is the only reading that closes it. In `**See url**（x） for
 * details**` the single trailing delimiter has no partner, so it is the closer and
 * the run's `**` belongs to the URL.
 */
function hasUnmatchedCloserAfter(suffix: string, delim: string): boolean {
  const ch = delim[0]
  let open = 0
  let i = 0
  while (i < suffix.length) {
    if (suffix[i] !== ch) {
      i++
      continue
    }
    let end = i
    while (end < suffix.length && suffix[end] === ch) end++
    const from = isEscapedAt(suffix, i) ? i + 1 : i
    if (end - from >= delim.length) {
      const { canOpen, canClose } = delimFlanking(suffix, from, end, ch)
      // Nothing local is waiting, so a closer here can only be closing the opener
      // that sits before the URL.
      if (canClose && open < delim.length) return true
      if (canClose) open -= Math.min(end - from, open)
      else if (canOpen) open += end - from
    }
    i = end
  }
  return false
}

/**
 * Close a bare `http(s)://` run whose boundary is provable — CJK punctuation
 * that could not be part of the URL, or a strong-emphasis delimiter swallowed
 * out of the surrounding markup — by re-emitting its head as an angle autolink.
 * Returns `content` unchanged when there is no such evidence.
 *
 * NOT safe to run when `data-sourcepos` is in play: it inserts two characters
 * per fixed URL, which shifts every later column on that line and would
 * mis-anchor an inline comment. Callers gate on that (see MarkdownBlock).
 */
export function closeCjkAutolinkBoundaries(content: string, parser: MarkdownParser): string {
  if (!content.includes('://')) return content
  if (!CJK_PUNCT_RE.test(content) && !STRONG_DELIM_RE.test(content)) return content
  const { literals, nonProse } = autolinkLiteralSpans(content, parser)
  const inserts: Array<[number, string]> = []
  for (const [start, end] of literals) {
    // Everything of this node already accounted for. A `https://` nested in the
    // URL's own path (`?u=https://…`) must not be cut separately — that would
    // corrupt the outer URL and emit out-of-order inserts.
    let consumedTo = start
    URL_START_RE.lastIndex = 0
    let m: RegExpExecArray | null
    while ((m = URL_START_RE.exec(content.slice(start, end))) !== null) {
      const at = start + m.index
      if (at < consumedTo) continue
      const run = URL_RUN_AT_RE.exec(content.slice(at, end))?.[0] ?? ''
      const cut = earliestCut(
        run,
        prosePrefix(content, nonProse, at),
        proseSuffix(content, nonProse, at + run.length),
      )
      if (cut < 0) {
        // The whole run is one URL — mask it, so a bracket in its query string
        // cannot pose as prose context for a later URL.
        nonProse.fill(1, at, at + run.length)
        consumedTo = at + run.length
        continue
      }
      const head = trimGfmAutolinkTail(run.slice(0, cut))
      if (!isAutolinkableHost(head)) {
        nonProse.fill(1, at, at + run.length)
        consumedTo = at + run.length
        continue
      }
      inserts.push([at, '<'], [at + head.length, '>'])
      // Resume right after the head: a second URL inside the same autolink node
      // (`（https://a/1）和【https://b/2】` is ONE run) still needs its own
      // boundary. Only the head just consumed is masked — the text between the
      // two URLs is real prose, and it is where the next bracket's opener lives.
      nonProse.fill(1, at, at + head.length)
      consumedTo = at + head.length
    }
  }
  if (inserts.length === 0) return content
  let out = ''
  let pos = 0
  for (const [at, ch] of inserts) {
    out += content.slice(pos, at) + ch
    pos = at
  }
  return out + content.slice(pos)
}

/**
 * A `[text](https?://…?…)` span whose destination carries RAW spaces or tabs.
 *
 * CommonMark refuses whitespace inside an unbracketed link destination, so the
 * whole span fails to parse as a link: the label renders as literal
 * `[text](`-prefixed prose and GFM autolinks just the head of the URL — the
 * href truncates at the first space (in practice the first unencoded query
 * param value), which is how an agent-emitted pre-filled URL becomes
 * unclickable.
 *
 * Three deliberate bounds, each the conservative direction:
 *  - The head must carry a `?`, and the run's LAST chunk must contain a
 *    `&name=` param start (see QUERY_CONTINUATION_RE below). An unencoded
 *    QUERY STRING is the shape this pass exists for, and only a new param
 *    opening in the final chunk proves the query spans every space to the
 *    run's end. Without that proof — `[docs](https://x.com/a for the full
 *    list)`, or `…?ref=1 for the full list` — the tail is PROSE after a
 *    truncated link, and absorbing it into the href would delete visible
 *    words and mint a dead URL, worse than the truncation it replaces. The
 *    cost is that a spaced value in a SINGLE-param URL (`?title=a b`) is not
 *    rescued: with no second param there is no evidence, and the issue's
 *    reported shape carries several `&`-separated params.
 *  - The label admits no brackets (`[^\][\n]`). A label that fails to close
 *    makes every later `[` restart the scan over the same characters, which
 *    is quadratic on `[`-heavy input — and a streaming message re-runs this
 *    on every reparse. Excluding `[` makes each start position fail in O(1),
 *    so the scan is linear; a nested-bracket label was never rescued before
 *    and still is not.
 *  - The chunks are `[^\s()]+`: a `(` or `)` inside the destination is
 *    CommonMark's OTHER refusal (unbalanced parens), where the span's true
 *    extent is genuinely ambiguous, so those spans are left alone.
 *
 * An uppercase scheme (`HTTPS://…`) is NOT rescued, and deliberately so: GFM
 * autolinks the uppercase head (schemes are case-insensitive there), and the
 * parse gate below sees that node as non-prose and skips the span. Reaching
 * it would mean loosening the gate that protects every accepted span, for a
 * casing agents do not emit.
 */
const BROKEN_LINK_DEST_RE =
  /(\[[^\][\n]*\]\([ \t]*)(https?:\/\/[^\s()?]*\?[^\s()]*(?:[ \t]+[^\s()]+)+)[ \t]*\)/g

/** A trailing `"…"` / `'…'` chunk at the end of a refused destination run.
 *  Genuinely ambiguous: it is the author's TITLE in `[a](url x "t")` but QUERY
 *  TEXT in `[a](https://x?q=crash when "Save As")`, and encoding or splitting
 *  either reading corrupts the other. Same verdict as parens: no rescue. */
const TRAILING_TITLE_RE = /[ \t]("[^"\n]*"|'[^'\n]*')$/

/** Evidence that the run's FINAL chunk is still query string: it contains a
 *  `&name=` param start (`&labels=bug` in `…?title=a b&labels=bug`). Only
 *  that proves the whitespace before it belongs to a query VALUE — a last
 *  chunk of plain words (`…?ref=1 for the full list`) is prose after a
 *  truncated link, not a spaced value. */
const QUERY_CONTINUATION_RE = /&[A-Za-z0-9_.~-]+=[^\s()]*$/

/**
 * Percent-encode raw whitespace inside a `[text](url)` destination that
 * CommonMark REFUSED, so the link the author unambiguously delimited parses
 * with its full URL.
 *
 * The author's own `](…)` delimiters prove the destination's extent, which is
 * what makes this safe where the bare-URL case is not: a bare
 * `https://… ?title=a b&c=d` run gives no evidence of where the URL ends, so
 * it keeps GFM's stop-at-whitespace behaviour (the same call every other
 * renderer makes).
 *
 * Gated on remark's OWN parse, exactly like `fixCjkAutolinkBoundaries`: a span
 * is rewritten only when every character of it is PROSE in the parse — inline
 * code, fenced/indented code, raw HTML, math, and (critically) every span that
 * ALREADY parsed as a link are all off-limits by construction. That last
 * exclusion is what protects the legal space-carrying forms — `<…>`-bracketed
 * destinations and `[a](url "title")` titles — without this function having to
 * re-derive CommonMark's grammar: if remark accepted it, it is not broken, and
 * it is never touched.
 *
 * Scheme-confined to `http(s)://` by the regex, so no rewrite can widen the
 * scheme surface — a `javascript:` destination never matches, and encoding
 * spaces cannot mint a new scheme. Same-line only (`[^\][\n]` / `[ \t]`): a
 * destination interrupted by a newline may be a paragraph boundary, and a cut
 * is the risky direction.
 *
 * Image spans (`![alt](url a b)`) are IN scope: the leading `!` sits outside
 * the match, the rescue makes the image parse, and a well-formed remote image
 * already fetches on render — no boundary moves. A destination whose run ends
 * in a quoted chunk (`[a](url x "t")`) is DECLINED: that chunk is the
 * author's title in one reading and query text (`?title=Crash when "Save
 * As"`) in the other, and either guess corrupts the other reading. An empty
 * label (`[](url a b)`) is skipped: the rescued anchor would have no
 * accessible name and nothing visible to click.
 *
 * NOT safe when `data-sourcepos` is in play: `%20` is three characters where
 * the space was one, which shifts every later column on the line. The caller
 * gates on that (see MarkdownBlock), mirroring `fixCjkAutolinkBoundaries`.
 */
export function encodeRefusedLinkDestinations(content: string, parser: MarkdownParser): string {
  if (!content.includes('](') || !content.includes('://')) return content
  BROKEN_LINK_DEST_RE.lastIndex = 0
  if (!BROKEN_LINK_DEST_RE.test(content)) return content
  const { nonProse } = autolinkLiteralSpans(content, parser)
  let out = ''
  let pos = 0
  BROKEN_LINK_DEST_RE.lastIndex = 0
  let m: RegExpExecArray | null
  while ((m = BROKEN_LINK_DEST_RE.exec(content)) !== null) {
    const start = m.index
    const end = start + m[0].length
    // An escaped `[` is a literal bracket the author wrote as prose; encoding
    // inside it would visibly rewrite their text, not repair a link. The
    // CLOSER gets the same check: `[a\](…)` is a literal `]` to CommonMark,
    // so no link was ever delimited there either.
    if (isEscapedAt(content, start)) continue
    if (isEscapedAt(content, start + m[1].lastIndexOf(']'))) continue
    // `[](url …)` would rescue an anchor with no accessible name and nothing
    // visible to click — leave the refused span as the prose it renders as.
    if (m[1].startsWith('[]')) continue
    // Any masked character means remark already owns this span — it parsed as
    // a real link (a legal title form), or it sits inside code/HTML/math.
    let masked = false
    for (let i = start; i < end; i++) {
      if (nonProse[i]) { masked = true; break }
    }
    if (masked) continue
    // A trailing quoted chunk is undecidable: the author's title in
    // `[a](url x "t")`, but query TEXT in `?title=Crash when "Save As"` —
    // treating it as a title would truncate that query out of the href.
    // Decline the span entirely, the same verdict parens get.
    if (TRAILING_TITLE_RE.test(m[2])) continue
    // The final chunk must PROVE it is still query string (`&name=…`): a
    // last chunk of plain words is prose after a truncated link, and
    // absorbing prose deletes visible words and mints a dead URL.
    const chunks = m[2].split(/[ \t]+/)
    if (!QUERY_CONTINUATION_RE.test(chunks[chunks.length - 1])) continue
    const destStart = start + m[1].length
    out += content.slice(pos, destStart)
    out += m[2].replace(/[ \t]/g, (ch) => (ch === ' ' ? '%20' : '%09'))
    pos = destStart + m[2].length
  }
  if (pos === 0) return content
  return out + content.slice(pos)
}
