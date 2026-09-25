/** Output redaction — mirrors backend security.py patterns for frontend display. */

import { i18nT } from '../i18n/t'

// ── Credential patterns (matches redact_credentials in security.py) ──
//
// The three AWS key-value spellings are KEY-ANCHORED: they begin at the key
// naming the secret. Like the backend, only the VALUE is replaced -- the key,
// separator and quotes stay, so a redacted line still says what was redacted --
// and a value that IS a registered redaction tag (or a run of them) filling it is
// left alone (`isRedactionTag`), so this mirror is a fixed point over the
// backend's own output (`key=[REDACTED: credential]`) instead of re-collapsing
// it to `[REDACTED] credential]`. Where the value begins and ends is read by
// `scanKeyedValue`, a port of the backend's `scan_keyed_value`: one explicit
// scanner, one token per step, with the opener (bare or escaped), the
// escaped-whitespace head, the tag run, the escape pair, the doubled quote and
// the line end as its rules. A regex stood here before and was patched by hand
// for every new encoding the backend learned, and the copies drifted: a doubled
// or escaped interior quote ended the mirror's value early and showed the second
// fragment of a concatenated secret. `test/sanitizeCredentials.fixture.test.ts`
// pins this port to the backend's generated fixture, so the two cannot drift
// again. Every other pattern IS the secret and is replaced whole.
const REDACTION_TAGS = ['[REDACTED]', '[REDACTED: credential]', '[REDACTED: encoded credential]']
const WHITESPACE_ESCAPE_LETTERS = new Set(['n', 'r', 't', 'f', 'v'])
const QUOTES = new Set(['"', "'"])
const UNQUOTED_TERMINATORS = new Set([',', '}'])
// Structural bytes where a value would start (see noValueOpensAt).
const NO_VALUE_OPENERS = new Set([',', '}', ']'])

// The PREFIX run the structural byte at `i` heads (the backend's `_prefix_run_end`):
// further structural bytes, a backslash paired with a structural byte or another
// backslash (the escaped encoding's inner `\\` among them), and the enclosing
// encoding's escaped whitespace between them, read whole before the value is
// judged: the base's value class admits `]` and a backslash, so `key=]]<secret>`
// is a value to it, and a judgement of the first byte alone left the secret after
// the pair standing. A backslash pair never heads a run: there it is the value's
// first byte, so the caller asks only at a structural byte.
function prefixRunEnd(text: string, i: number, enclosing: string): number {
  const n = text.length
  for (;;) {
    let j = i
    while (j < n) {
      const [kind, width] = innerToken(text, j, false, '', enclosing)
      if (kind === 'char' && width === 1 && NO_VALUE_OPENERS.has(text[j])) j += 1
      else if (
        kind === 'backslash' &&
        (width === 2 || NO_VALUE_OPENERS.has(text[j + 1]) || text[j + 1] === '\\')
      )
        j += 2
      else break
    }
    while (j < n) {
      const [kind, width] = innerToken(text, j, false, '', enclosing)
      if ((kind === 'space' || kind === 'break') && width === 2) {
        j += width
        continue
      }
      if (kind !== 'backslash' || j + width >= n) break
      const [letterKind, letterWidth] = innerToken(text, j + width, false, '', enclosing)
      if (letterKind !== 'char' || !WHITESPACE_ESCAPE_LETTERS.has(text[j + width])) break
      j += width + letterWidth
    }
    if (j === i) return i
    i = j
  }
}

// Whether NO value opens at the structural byte at `i`, where an unquoted value
// would start: past the prefix run it heads nothing value-like follows (whitespace,
// a line break, a quote, a close, or a bare backslash before one of those); a value
// byte after the run makes the run the value's head. A run reaching the text's end
// is decided by the caller before this is asked.
function noValueOpensAt(text: string, i: number, enclosing: string): boolean {
  const j = prefixRunEnd(text, i, enclosing)
  const [kind] = innerToken(text, j, false, '', enclosing)
  if (kind === 'backslash') return QUOTES.has(text[j + 1]) || isWhitespace(text[j + 1])
  return kind !== 'char'
}

// The quote state of a line's prefix, the backend's `_advance_line_state`: the
// kind of the string literal open at its end ('' for none), whether its last
// byte is a backslash still escaping the next one, the kind a quote just closed
// (a doubled quote reopens it as an escaped interior quote), and the last byte.
// A raw line break resets it; outside a literal a quote opens one only when the
// byte before it is not a word byte (an apostrophe inside a word is prose), and
// a backslash escapes the next byte; inside, a backslash escapes, the same kind
// closes, the other kind is a byte of the literal.
interface LineState {
  kind: string
  escaped: boolean
  closed: string
  last: string
  inner: string
}
const LINE_START: LineState = { kind: '', escaped: false, closed: '', last: '', inner: '' }

function isWordByte(c: string): boolean {
  return /[\p{L}\p{N}_]/u.test(c)
}

function advanceLineState(state: LineState, text: string, start: number, end: number): LineState {
  let { kind, escaped, closed, last, inner } = state
  for (let i = start; i < end; i++) {
    const c = text[i]
    if (c === '\r' || c === '\n') {
      kind = ''
      escaped = false
      closed = ''
      inner = ''
    } else if (escaped) {
      escaped = false
      if (kind && QUOTES.has(c)) {
        // An escaped quote inside a literal delimits the INNER literal of the
        // escaped encoding: it opens one, and the same escaped quote closes it.
        const delim = '\\' + c
        if (!inner) inner = delim
        else if (inner === delim) inner = ''
      }
    } else if (c === '\\') {
      escaped = true
      closed = ''
    } else if (kind) {
      if (c === kind) {
        kind = ''
        closed = c
        inner = ''
      }
    } else if (closed && c === closed) {
      kind = c
      closed = ''
    } else if (QUOTES.has(c) && !(last !== '' && isWordByte(last))) {
      kind = c
      closed = ''
    } else {
      closed = ''
    }
    last = c
  }
  return { kind, escaped, closed, last, inner }
}

// The backend's `_enclosing_at`: the literals enclosing `at`, innermost last --
// the outer literal's bare quote, then the inner literal's escaped delimiter
// when one is open -- read back along the line from its start.
function enclosingAt(text: string, at: number): string {
  const lineStart = Math.max(text.lastIndexOf('\n', at - 1), text.lastIndexOf('\r', at - 1)) + 1
  const state = advanceLineState(LINE_START, text, lineStart, at)
  return state.kind + state.inner
}

// The innermost enclosing delimiter as written, and the context left once it closes.
function innermost(enclosing: string): [string, string] {
  if (enclosing.length >= 3 && enclosing[enclosing.length - 2] === '\\') {
    return [enclosing.slice(-2), enclosing.slice(0, -2)]
  }
  return [enclosing.slice(-1), '']
}

// The backend's `_LABEL_QUOTE`: an optional quote closing a JSON key, bare or
// escaped as it reads inside an enclosing string literal. The key anchor ends at
// the separator; the opener belongs to the scanner.
const KEY_ANCHOR = new RegExp(
  `(?:SecretAccessKey|aws_secret_access_key|SessionToken|aws_session_token|AccessKeyId|aws_access_key_id)(?:\\\\?["'])?\\s*[:=]\\s*`,
  'gi',
)

interface KeyedValue {
  start: number
  end: number
  closes: boolean
  opener: string
}

function isWhitespace(c: string): boolean {
  return /\s/.test(c)
}

function tagRunEnd(text: string, i: number): number {
  for (;;) {
    const tag = REDACTION_TAGS.find((candidate) => text.startsWith(candidate, i))
    if (tag === undefined) return i
    i += tag.length
  }
}

// The inner token at `i` in the value's encoding -- what one byte of the value
// reads as. Bare encoding: every token is one byte. Escaped encoding (the pair is
// written inside an enclosing string literal, `key=\"<v>\"`): a backslash and
// the byte after it are ONE inner token, and a bare quote of the literal's own
// kind is the literal's end. A bare quote of the kind `enclosing` the pair (the
// literal the look-back found the key inside) is the enclosing literal's end in
// either encoding. Kinds: backslash, quote, break (a line break), close (the
// enclosing literal's end), space, char, partial.
/** The scanner's WORK, counted deterministically: one step per inner token read
 *  (`innerToken`, the scanner's only per-byte advance). A test bounds the steps a
 *  payload costs instead of reading a clock, which a loaded runner moves; the
 *  counter is reset by the test that reads it and is never read in production. */
export const scanWork = { steps: 0 }

function innerToken(
  text: string,
  i: number,
  escaped: boolean,
  literalQuote: string,
  enclosing: string,
): [string, number] {
  scanWork.steps++
  const c = text[i]
  if (c === '\\') {
    if (i + 1 >= text.length) return ['partial', 1]
    if (escaped || enclosing.startsWith('"') || enclosing.length >= 3) {
      // The pair is ONE token, read before any delimiter test: inside a
      // backslash-escaping literal (a `"` literal or an escaped inner one) every
      // byte is in the literal's encoding whatever quote opens the value; a bare
      // `'` literal has no escapes of its own.
      const nxt = text[i + 1]
      if (nxt === '\r' || nxt === '\n') return ['backslash', 1] // no encoding pairs a backslash with a raw line break
      if (nxt === '\\') return ['backslash', 2]
      if (QUOTES.has(nxt)) {
        if (enclosing.length >= 3 && nxt === enclosing[enclosing.length - 1]) return ['close', 2] // the inner literal's escaped delimiter
        return ['quote', 2]
      }
      if (nxt === 'n' || nxt === 'r') return ['break', 2]
      if (nxt === 't' || nxt === 'f' || nxt === 'v') return ['space', 2]
      return ['char', 2]
    }
    return ['backslash', 1]
  }
  if ((escaped && c === literalQuote) || (enclosing !== '' && c === enclosing[0])) return ['close', 1]
  if (c === '\r' || c === '\n') return ['break', 1]
  if (isWhitespace(c)) return ['space', 1]
  if (QUOTES.has(c)) return ['quote', 1]
  return ['char', 1]
}

// The backend's `scan_keyed_value`, byte for byte: the value of a key-anchored
// pair whose separator ends at `at`, inside the literal `enclosing` (read back
// along the line when not given). Every rule is a STOPPING rule, so an unknown
// spelling costs an over-redaction, never a byte left standing.
/** The quote OPENING a value at `at`, as written: bare, escaped, or none (the
 *  backend's `_opener_at`). */
function openerAt(text: string, at: number): string {
  const n = text.length
  if (at < n && QUOTES.has(text[at])) return text[at]
  if (at + 1 < n && text[at] === '\\' && QUOTES.has(text[at + 1])) return text.slice(at, at + 2)
  return ''
}

export function scanKeyedValue(text: string, at: number, enclosing?: string): KeyedValue {
  if (enclosing === undefined) enclosing = enclosingAt(text, at)
  const n = text.length
  let opener = openerAt(text, at)
  const [innermostDelim, outer] = innermost(enclosing)
  if (enclosing !== '' && opener === enclosing[0] && text[at + 1] === opener) {
    // A doubled quote of the enclosing literal's kind where the value would open
    // is that literal's escaped interior quote: the value's own quote, as
    // written, and the same doubled pair closes it.
    opener = opener + opener
  } else if (enclosing !== '' && (opener === innermostDelim || opener === enclosing[0])) {
    // The enclosing literal's own close stands where the value would open
    // (`{"template":"key=","keep":1}`, one level down the inner literal's `\"`,
    // and the outer literal's bare quote closes everything): the assignment
    // inside the literal is empty, and what follows the close is in the context
    // outside it.
    return scanKeyedValue(text, at + opener.length, opener === innermostDelim ? outer : '')
  }
  const escaped = opener.startsWith('\\')
  const literalQuote = opener ? opener[opener.length - 1] : ''
  const start = at + opener.length
  let i = start
  // The head: escaped whitespace, unbounded, consumed with the value.
  for (;;) {
    if (i >= n) break
    const [kind, width] = innerToken(text, i, escaped, literalQuote, enclosing)
    if ((kind === 'space' || kind === 'break') && width === 2) {
      // The enclosing encoding's escaped whitespace, a line break among it, is
      // whitespace to the anchor; read as the value's end it left the whole
      // value standing behind it.
      i += width
      continue
    }
    if (kind !== 'backslash' || i + width >= n) break
    const [letterKind, letterWidth] = innerToken(text, i + width, escaped, literalQuote, enclosing)
    if (letterKind !== 'char' || !WHITESPACE_ESCAPE_LETTERS.has(text[i + width])) break
    i += width + letterWidth
  }
  if (!opener) {
    while (i < n) {
      const run = tagRunEnd(text, i)
      if (run > i) {
        i = run
        continue
      }
      const [kind, width] = innerToken(text, i, false, '', enclosing)
      if (kind === 'partial') return { start, end: i, closes: true, opener: '' }
      if (kind === 'char' && i === start && NO_VALUE_OPENERS.has(text[i])) {
        // The token past the PREFIX run decides: the text's end (the next byte
        // would), whitespace, a quote or a close, and no value opens; a value
        // byte, and the run is the value's head, consumed with it.
        const run = prefixRunEnd(text, i, enclosing)
        if (run >= n || (text[run] === '\\' && run + 1 >= n)) {
          return { start, end: i, closes: true, opener: '' }
        }
        if (noValueOpensAt(text, i, enclosing)) return { start, end: i, closes: true, opener: '' }
        i = run
        continue
      }
      if (
        kind === 'space' ||
        kind === 'break' ||
        kind === 'close' ||
        kind === 'quote' ||
        (kind === 'char' && i > start && UNQUOTED_TERMINATORS.has(text[i]))
      ) {
        return { start, end: i, closes: true, opener: '' }
      }
      if (kind === 'backslash') {
        // A pair token is a byte of the value; a BARE backslash reads the byte
        // after it (a lone one at the text's end is `partial` above): a quote or
        // raw whitespace ends the value, any other pair is the value's, the
        // two-byte spelling `\n` of a decoded URL path among them.
        const nxt = text[i + 1]
        if (width === 1 && (QUOTES.has(nxt) || isWhitespace(nxt))) {
          return { start, end: i, closes: true, opener: '' }
        }
        i += 2
        continue
      }
      i += width
    }
    return { start, end: n, closes: true, opener: '' }
  }
  while (i < n) {
    const run = tagRunEnd(text, i)
    if (run > i) {
      i = run
      continue
    }
    const [kind, width] = innerToken(text, i, escaped, literalQuote, enclosing)
    if (kind === 'close' && width === 1 && text[i + 1] === text[i]) {
      // A doubled quote of the enclosing literal's kind is an escaped interior
      // quote of that literal (a YAML single-quoted scalar spells `''`). When the
      // value opened with that doubled pair it is the value's own quote: doubled
      // again it is the value's escaped interior quote, alone it is the close.
      if (opener === text[i] + text[i]) {
        if (text.slice(i + 2, i + 4) === opener) {
          i += 4
          continue
        }
        return { start, end: i, closes: true, opener }
      }
      i += 2
      continue
    }
    if (kind === 'partial' || kind === 'break' || kind === 'close') return { start, end: i, closes: false, opener }
    if (kind === 'backslash') {
      const j = i + width
      if (j >= n) return { start, end: i, closes: false, opener }
      const [nxtKind, nxtWidth] = innerToken(text, j, escaped, literalQuote, enclosing)
      if (nxtKind === 'break') {
        i = j // a backslash does not escape a line break, raw or inner
        continue
      }
      if (nxtKind === 'partial') return { start, end: j, closes: false, opener }
      if (nxtKind === 'close' && nxtWidth === 1) {
        // A bare quote of the enclosing kind after a backslash is the enclosing
        // literal's close in every encoding (lower depth after an inner backslash;
        // a `'` literal has no escapes at all).
        // Escapes pair up at the value's own depth: after the escaped encoding's
        // inner backslash (`\\`) a BARE quote is the enclosing literal's close,
        // never the escaped token; doubled, its escaped interior quote.
        if (text[j + 1] === text[j]) {
          i = j + 2
          continue
        }
        return { start, end: j, closes: false, opener }
      }
      i = j + nxtWidth // the escaped token is interior, an enclosing quote included
      continue
    }
    if (kind === 'quote' && text.slice(i, i + width) === opener) {
      const j = i + width
      if (j < n && text.slice(j, j + width) === opener) {
        // A doubled quote is an escaped interior quote, never the close.
        i = j + width
        continue
      }
      return { start, end: i, closes: true, opener }
    }
    // A quote of the other kind is a byte of the value: the literal that could
    // end the line here is the enclosing one, and its quote reads as `close`.
    i += width
  }
  return { start, end: n, closes: false, opener }
}

// Trust is byte identity of the ENTIRE value with a RUN of one or more of the
// tag literals above (this mirror's own, and the two the backend's
// `CREDENTIAL_REDACTION_TAGS` registers), never a shape and never a prefix:
// `[REDACTED<secret>` is a value and is redacted like any other, and so is
// `[REDACTED: credential]<secret>` -- and so is a run with bytes glued to its
// last `]`. No tag is a substring of another, so the parse of a run is unique.
function isRedactionTag(value: string): boolean {
  return value.length > 0 && tagRunEnd(value, 0) === value.length
}

// The key-anchored pass: every AWS key anchor, its value read by the scanner,
// replaced by the tag with the opener kept and -- when the quote never closed on
// its line -- the close WRITTEN as the backend writes it, so the output is a
// closed pair a re-screen leaves alone. Coverage is judged on the VALUE, as the
// backend's pass 1 judges it: a value covered whole by an earlier claim is
// skipped, and a value that straddles the claim's end is claimed from there.
// Skipping every anchor that BEGAN inside an earlier claim let one slip: an
// anchor whose trailing whitespace crosses a line break reads the next key's
// name as its value (`aws_access_key_id = \naws_secret_access_key = <v>`), and
// the next key's anchor, beginning inside that claim, was skipped with its
// value past it left in plaintext while the backend redacted it.
function redactKeyedValues(text: string): string {
  let out = ''
  let cursor = 0
  KEY_ANCHOR.lastIndex = 0
  let state = LINE_START
  let pos = 0
  // The last UNQUOTED scan, remembered (the backend's `_KeyedValueScans`): an
  // anchor that begins inside the run it read -- a key repeated as its own
  // value, `secretaccesskey=secretaccesskey=...` -- starts its own scan at a
  // token boundary of that run and reads the same tokens to the same end, so it
  // is answered from the memo. Scanning each nested anchor afresh read the rest
  // of the run once per anchor: a 50,000-character run of bare anchors, the
  // cron message cap, held the dashboard's main thread for seconds.
  let bare: KeyedValue | null = null
  for (const anchor of text.matchAll(KEY_ANCHOR)) {
    const at = anchor.index + anchor[0].length
    // The look-back for the literal enclosing the key, advanced from the last
    // anchor so a line of many anchors is read once (the backend's
    // `_KeyedValueScans`).
    state = advanceLineState(state, text, pos, at)
    pos = at
    let value: KeyedValue
    if (bare !== null && bare.start <= at && at < bare.end && openerAt(text, at) === '') {
      value = { start: at, end: bare.end, closes: true, opener: '' }
    } else {
      value = scanKeyedValue(text, at, state.kind + state.inner)
      if (value.opener === '') bare = value
    }
    if (value.end <= value.start) continue
    if (value.end <= cursor) continue
    let start = value.start
    let closes = value.closes
    if (start < cursor) {
      // Straddles the earlier claim's end: the part past it is this anchor's.
      start = cursor
      closes = true
    }
    const body = text.slice(start, value.end)
    if (isRedactionTag(body) && closes) continue
    const close = closes ? '' : value.opener
    out += text.slice(cursor, start) + '[REDACTED]' + close
    cursor = value.end
  }
  return out + text.slice(cursor)
}

const CRED_PATTERNS: RegExp[] = [
  /(?:AKIA|ASIA)[A-Z0-9]{16}/g,
  /BEGIN\s(?:RSA|DSA|EC|OPENSSH)\sPRIVATE\sKEY/g,
  /xox[bpas]-[0-9a-zA-Z-]{10,}/g,
  // JWS (3 segments) and compact JWE (5 segments). Post-header segments use `*`,
  // not `+`. A `dir`/`ECDH-ES` JWE has an EMPTY Encrypted Key segment
  // (`header..iv.ciphertext.tag`), so `*` is what makes it redact whole rather
  // than truncating and leaving the ciphertext and tag on screen.
  //
  // Byte-identical to the backend alternative, deliberately, and pinned as such
  // by `test/test_redaction_mirror_parity.py`. No left boundary is used here: a
  // left boundary would stop a two-dot identifier such as
  // `keyJson.parse.value` being redacted, but that trade is
  // the wrong one: the boundary MISSES a real token whenever a renderer
  // concatenates a label straight onto it (`compact=jwt<token>`,
  // `/session/jwe<token>`), which the backend redacts. It also does not prevent
  // the commonest false-positive form, a space-preceded identifier in a stack
  // trace (`at eyJsonSerializer.deserialize.value`), which matches either way.
  // So it would buy two avoided false positives and cost two leaks. A miss is a
  // leak; a false positive is mangled display text.
  //
  // The residual false positive is therefore shared with the backend rather than
  // unique to this mirror, which keeps it ONE defect to fix in one place. Closing
  // it needs a structural test, not a boundary: decode segment one as a JOSE
  // header and require `alg`/`enc`. That belongs in the backend first, with this
  // mirror following, so it is deliberately out of scope here.
  /eyJ[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]*){2,4}/g,
  // Two-segment dashboard link token (`base64url(payload).base64url(hmac_sig)`).
  // `security.py` carries the full derivation of both bounds and is the single
  // source for it; `test/test_redaction_mirror_parity.py` fails if this copy
  // drifts from it. The local invariant worth knowing here: the signature width
  // is PINNED (`{43}`, a property of the HMAC-SHA256 digest, not of the payload),
  // so a digest change fails a backend test loudly instead of silently disabling
  // redaction, and the payload bound is a generator-derived floor rather than a
  // guess because a guessed floor is beatable by a verbose enough identifier.
  //
  // ONE deliberate difference from the backend: its leading lookbehind boundary
  // is omitted here, and must stay omitted. Safari 16.3 and older cannot compile
  // a lookbehind. `vite.config.ts` declares no `build.target`, so the default
  // `'modules'` floor is `safari14`, and at that target esbuild rewrites the
  // literal into a `new RegExp(...)` call. That moves the failure from parse time
  // to run time, which does not help: this array is a module-level constant in
  // the eagerly loaded entry chunk, so the throw lands during module evaluation
  // and the dashboard renders blank rather than losing one feature. Verified by
  // execution, with `RegExp` patched to reject lookbehind: this shape throws on
  // import, a function-scoped one throws only when called.
  //
  // Removing a LEFT boundary cannot create a MISS, so the divergence is
  // one-directional: verified by execution, no input redacts in the backend and
  // not here. This mirror additionally catches a token a renderer concatenated
  // onto a label (`tok=jwt<token>`), which the backend's boundary makes it miss.
  //
  // The cost is the SAME mechanism, and it is not a benign extra replacement. A
  // lookbehind is zero-width, so the match still starts at an `eyJ`, but that
  // `eyJ` may be one INSIDE a preceding identifier: `keyJson<token>` matches from
  // index 1 and renders `k[REDACTED: credential]`, absorbing the identifier tail.
  // That is the same class of damage cited above as the reason the segment floor
  // was not relaxed to `{1,4}`. It needs an identifier containing `eyJ` glued with
  // no delimiter to 96+ identifier chars, a dot, then exactly 43 more. Not
  // reachable on the surfaces this function feeds: the longest `eyJ`-containing
  // identifier in the tree is 50 chars, the backend already redacts `agent`/`task`
  // before broadcast and truncates `tool` to 80 chars, and the output of this
  // function goes to in-memory store state only. The one surface rewritten before
  // persistence (file-diff chip bodies) is redacted in the BACKEND, so an
  // over-match here cannot reach disk.
  //
  // Ordering after the JWS alternative is defensive, not load-bearing for real
  // tokens: a conventional `{"alg":"HS256","typ":"JWT"}` header is only 33 chars
  // past `eyJ`, far below this alternative's first-segment floor, so it cannot
  // match a real JWS's `header.payload` at all. It becomes load-bearing only for
  // a JWS whose header clears that floor AND whose payload is exactly 43 chars,
  // because this pattern's right boundary is satisfied by a `.` and would leave
  // `.signature` rendered. That shape is covered by a test.
  /eyJ[A-Za-z0-9_-]{96,}\.[A-Za-z0-9_-]{43}(?![A-Za-z0-9_-])/g,
  /https?:\/\/[^:]+:[^@]+@/g,
]

// Base64 chunk: 40+ chars of base64 alphabet with optional trailing =
const B64_CHUNK = /[A-Za-z0-9+/]{40,}={0,2}/g

function decodeB64Safe(chunk: string): string {
  try {
    const decoded = atob(chunk)
    // A labelled pair in the decoded bytes: the keyed walk rewrites a live
    // value and leaves a tag run filling it alone, the same rule the plaintext
    // pass applies. The key-anchored spellings live in KEY_ANCHOR, not in
    // CRED_PATTERNS, so a check of CRED_PATTERNS alone let an encoded
    // `SessionToken=<v>` through reversible.
    if (redactKeyedValues(decoded) !== decoded) return decoded
    // Check if decoded content contains credential patterns
    for (const re of CRED_PATTERNS) {
      re.lastIndex = 0
      if (re.test(decoded)) return decoded
    }
  } catch { /* not valid base64 */ }
  return ''
}

export function sanitizeCredentials(text: string): string {
  // Key-anchored AWS pairs first: the value as the scanner reads it, key kept.
  let out = redactKeyedValues(text)
  // Plaintext credential patterns -- each IS the secret and is replaced whole.
  for (const re of CRED_PATTERNS) {
    re.lastIndex = 0
    out = out.replace(re, '[REDACTED]')
  }
  // Base64-encoded credentials
  B64_CHUNK.lastIndex = 0
  for (const m of text.matchAll(B64_CHUNK)) {
    if (decodeB64Safe(m[0])) {
      out = out.replace(m[0], '[REDACTED: encoded credential]')
    }
  }
  return out
}

// ── Exfiltration URL detection (mirrors redact_exfiltration_urls in security.py) ──
// Unlike the backend, a URL that stops at `)` is judged with the text after it (#8638),
// up to the next scheme or space (URL_TAIL_RE), then cut back to where its link ends.
const URL_RE = /https?:\/\/([a-zA-Z0-9._-]+\.[a-zA-Z]{2,})(:\d+)?(\/[^\s)"'>]*)?/g
const URL_TAIL_RE = /[^\s"'>]*?(?=https?:\/\/|[\s"'>]|$)/y
// Drop trailing `)` that have no `(` partner in the URL, plus punctuation after them.
function trimWrapperParen(url: string): string {
  let extra = url.split(')').length - url.split('(').length
  let keep = url.length
  for (let i = url.length - 1; i >= 0 && extra > 0; i--) {
    if (url[i] === ')') { extra--; keep = i } else if (!'.,;:!?'.includes(url[i])) break
  }
  return url.slice(0, keep)
}

// A markdown `](...)` target ends at its first `)` with no `(` partner, as CommonMark does.
function linkTarget(url: string): string {
  let depth = 0
  for (let i = 0; i < url.length; i++) {
    if (url[i - 1] === '\\') continue // a `\(` or `\)` escape stays in the target, as in CommonMark
    if (url[i] === '(') depth++
    else if (url[i] === ')' && --depth < 0) return url.slice(0, i)
  }
  return url
}

// True when the `]` at `i` closes a real `[label]` (CommonMark caps a label at 999 chars).
function closesLabel(text: string, i: number): boolean {
  let depth = 0
  for (let j = i - 1; j >= 0 && j >= i - 1000; j--) {
    if (text[j] === ']') depth++
    else if (text[j] === '[' && depth-- === 0) return true
  }
  return false
}
const EXFIL_QUERY_MIN_LEN = 200

// PATTERN signals: each names a shape rather than a size, and each runs for
// EVERY URL — no host and no carve-out escapes them — so this redactor still
// flags every pattern the undifferentiated check flagged. Non-global so `.test()`
// carries no sticky `.lastIndex` between calls.
//
// Heavy URL-encoding: 20+ CONSECUTIVE percent-encoded octets. Mirrors the
// backend's _EXFIL_PERCENT_RE.
const EXFIL_PERCENT_RE = /%[0-9A-Fa-f]{2}(?:%[0-9A-Fa-f]{2}){20,}/i

// Hard credential markers. Mirrors the backend's _HARD_CREDENTIAL_RE.
const EXFIL_CREDENTIAL_RE = new RegExp(
  '(?:' +
    '(?:AKIA|ASIA)[A-Z0-9]{16}' +                    // AWS access key ID
    '|(?:ssh-rsa|ssh-ed25519)[\\s+%]' +               // SSH public key
    '|BEGIN[\\s+%](?:RSA|DSA|EC|OPENSSH)[\\s+%]PRIVATE[\\s+%]KEY' + // private key header
    '|xox[bpas]-[0-9a-zA-Z-]+' +                     // Slack token
  ')',
  'i',
)

// Base64-like blob, 40+ chars — the shape an encoded payload has. Same spelling
// as the backend's `_EXFIL_PATTERNS` base64 branch, and it OVER-matches by
// design: `+` is the form-encoded spelling of a space, so ~7 words of
// unpunctuated prose in a `+`-encoded `body=` are one run in this class and are
// redacted. That is accepted rather than fixed, and this signal is deliberately
// NOT waivable, because both available narrowings — dropping `+` from the class,
// or splitting the query on `+` before testing — let an attacker `+`-chunk a 40+
// char secret straight past it. A false positive on prose costs a placeholder; a
// chunking bypass costs the payload.
//
// `=` is different: it counts only as trailing padding, never as a joiner, so a
// parameter name, its `=` and a short value (`trainingId=` plus a 32-char ID) do
// not fuse into one 40-char run. That opens no chunking channel `&`, `.`, `-` and
// `_` do not already provide, and the aggregate length signal still bounds the
// query. Padding still counts toward the 40 chars (38 plus `==`, 39 plus `=`),
// so a minimum-length encoded payload is caught. Same spelling as the backend.
const EXFIL_B64_RE = /[A-Za-z0-9+/]{40,}={0,2}|[A-Za-z0-9+/]{39}=|[A-Za-z0-9+/]{38}==/i

// Aggregate query LENGTH is the one signal that names no shape at all: it fires on
// any richly-parameterised URL, which is why prefilled issue links —
// `…/issues/new?title=…&body=<a paragraph of prose>&labels=…` — render as a
// `[REDACTED: suspicious URL]` placeholder.
//
// It is NOT waived for that shape, deliberately, and no future shape-based waiver
// belongs here either. `isPrefilledIssueUrl` used to waive it (#7824), first on
// shape alone and later pinned to this project's own tracker; both spellings are
// exfiltration primitives, because what this function sanitizes is MODEL-AUTHORED
// text. Injected content steers the model into emitting a prefill URL whose `body`
// carries percent-encoded private context, the waiver skips the length check, the
// link renders as the familiar "file an issue" affordance, the user submits it —
// and the issue is PUBLIC, so the attacker reads it. Pinning the repository does
// not help: this project's tracker is world-readable, which is the point of it.
//
// A URL's shape says nothing about who authored it, and a marker placed IN the
// text travels in the channel the injection already controls. Provenance has to
// come from a different channel, which the product already has: the backend's
// `diagnostics._issue_url` assembles the prefill link from STRUCTURED fields and
// the dashboard renders its own anchor from the `github_issue_url` JSON field,
// which no redactor scans (`ReportProblemModal`, `ReportProblemCard`). A link that
// never enters model prose never needs a waiver.
//
// If you are here to make a long legitimate URL render, narrow or replace this
// heuristic for EVERY host on its own merits (#7820 also reports
// monitorportal.amazon.com) — do not reintroduce a per-shape escape hatch.

export function sanitizeExfiltrationUrls(text: string): string {
  let out = ''
  let last = 0
  URL_RE.lastIndex = 0
  let m: RegExpExecArray | null
  while ((m = URL_RE.exec(text))) {
    const domain = m[1]
    const end = m.index + m[0].length
    URL_TAIL_RE.lastIndex = end
    const full = m[0] + (text[end] === ')' ? (URL_TAIL_RE.exec(text)?.[0] ?? '') : '')
    const inLink = text.slice(m.index - 2, m.index) === '](' && closesLabel(text, m.index - 2)
    // End at the first unpaired `)` when that already keeps the `?`, so glued prose after it
    // never votes or is spliced; otherwise the query lies past the `)`, so keep scanning it.
    const cut = linkTarget(full)
    const url = inLink || cut.includes('?') ? cut : trimWrapperParen(full)
    // A pattern signal past the cut still counts (glued prose can only trip the length one).
    const past = inLink ? '' : full.slice(url.length)
    const hit = EXFIL_PERCENT_RE.test(past) || EXFIL_CREDENTIAL_RE.test(past) || EXFIL_B64_RE.test(past)
    const pathAndQuery = url.slice(m[0].length - (m[3] || '').length)
    const qmark = pathAndQuery.indexOf('?')
    if (qmark === -1 && !hit) continue
    const query = pathAndQuery.slice(qmark + 1)
    const redact = hit ||
      EXFIL_PERCENT_RE.test(query) ||
      EXFIL_CREDENTIAL_RE.test(query) ||
      EXFIL_B64_RE.test(query) ||
      query.length >= EXFIL_QUERY_MIN_LEN
    if (redact) {
      out += text.slice(last, m.index) + i18nT('utils.sanitize.redacted_suspicious_url', { domain })
      last = m.index + (hit ? trimWrapperParen(full) : url).length
    }
  }
  return out + text.slice(last)
}

/** Combined sanitizer — runs both credential and exfiltration redaction. */
export function sanitizeLlmOutput(text: string): string {
  return sanitizeExfiltrationUrls(sanitizeCredentials(text))
}

/** True for the three object keys that, when used to index a plain object,
 *  mutate ``Object.prototype`` instead of the object (prototype pollution).
 *  Reducers that index a state map with an id sourced from SSE/LLM payloads
 *  must early-return on these before the assignment. The literal ``===`` form
 *  (not a Set/array membership test) is what CodeQL's
 *  ``js/prototype-polluting-assignment`` query recognizes as a sanitizer. */
export function isUnsafeKey(key: string): boolean {
  return key === '__proto__' || key === 'constructor' || key === 'prototype'
}
