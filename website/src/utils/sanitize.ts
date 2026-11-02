/** Output redaction — mirrors backend security.py patterns for frontend display. */

import { i18nT } from '../i18n/t'

// ── Credential patterns (matches redact_credentials in security.py) ──
const CRED_PATTERNS: RegExp[] = [
  /(?:AKIA|ASIA)[A-Z0-9]{16}/g,
  /(?:SecretAccessKey|aws_secret_access_key)\s*[:=]\s*\S+/gi,
  /(?:SessionToken|aws_session_token)\s*[:=]\s*\S+/gi,
  /(?:AccessKeyId|aws_access_key_id)\s*[:=]\s*\S+/gi,
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
    // Check if decoded content contains credential patterns
    for (const re of CRED_PATTERNS) {
      re.lastIndex = 0
      if (re.test(decoded)) return decoded
    }
  } catch { /* not valid base64 */ }
  return ''
}

// Each pattern again, anchored at `lastIndex`, to test one start inside a match.
const CRED_PATTERNS_AT: RegExp[] = CRED_PATTERNS.map(
  (re) => new RegExp(re.source, re.flags.replace('g', '') + 'y'),
)

// Which patterns read far before they can fail: the two JWT patterns read a whole
// segment run, the userinfo pattern reads to the next `:` and `@`.
const IS_JWT_PATTERN = CRED_PATTERNS.map((re) => re.source.startsWith('eyJ'))
const IS_USERINFO_PATTERN = CRED_PATTERNS.map((re) => re.source.startsWith('https?'))

// Nested credential matches a span may hold before the rest of its non-space run is
// redacted unread, so a span full of nested starts costs a bounded number of reads.
const GLUED_PROBES = 8

// `trim` strips exactly the characters `\s` matches.
function isSpace(ch: string): boolean {
  return ch.trim() === ''
}

// A JWT segment character, `[A-Za-z0-9_-]` in the two JWT patterns.
function isSegmentChar(code: number): boolean {
  return (code >= 48 && code <= 57) || (code >= 65 && code <= 90) ||
    (code >= 97 && code <= 122) || code === 95 || code === 45
}

/** What one pass's glued probes already read. Probe positions only rise within a
 *  pass, and the pass's text does not change, so each cached index stays valid. */
interface ProbeCache {
  // End of the segment run whose first `eyJ` was probed.
  runEnd: number
  // The first `:` and `@` at or after the last position each was searched from,
  // or `text.length` when there is none.
  colon: number
  at: number
}

function newProbeCache(): ProbeCache {
  return { runEnd: -1, colon: -1, at: -1 }
}

/** Whether the JWT patterns need testing at `pos`. Every `eyJ` in one segment run
 *  has the same match end, because a JWT header must end where its run does, so
 *  only the first `eyJ` of a run is tested and a long run is read once. */
function jwtProbeNeeded(text: string, pos: number, cache: ProbeCache): boolean {
  if (!text.startsWith('eyJ', pos) || pos < cache.runEnd) return false
  let runEnd = pos + 3
  while (runEnd < text.length && isSegmentChar(text.charCodeAt(runEnd))) runEnd++
  cache.runEnd = runEnd
  return true
}

/** Whether the userinfo pattern needs testing at `pos`: only after `http://` or
 *  `https://` with a `:` later on and an `@` after that, so a probe never reads to
 *  the end of the text only to fail. */
function userinfoProbeNeeded(text: string, pos: number, cache: ProbeCache): boolean {
  if (!text.startsWith('http', pos)) return false
  const scheme = pos + (text[pos + 4] === 's' ? 5 : 4)
  if (!text.startsWith('://', scheme)) return false
  if (cache.colon < scheme + 3) {
    const i = text.indexOf(':', scheme + 3)
    cache.colon = i === -1 ? text.length : i
  }
  if (cache.colon === text.length) return false
  if (cache.at <= cache.colon) {
    const i = text.indexOf('@', cache.colon + 1)
    cache.at = i === -1 ? text.length : i
  }
  return cache.at < text.length
}

/** Where the span `text[start, end)` that pattern `from` matched ends once every
 *  credential glued onto it is included.
 *
 *  Two credentials written back to back with nothing between them overlap when the
 *  first one's body class also takes the second one's leading characters (a Slack
 *  token takes a JWT's header up to its first `.`). The second pattern then no longer
 *  matches what the first left, and most of the second credential stays on screen.
 *  So every start inside the span is tested against this pattern and the ones after
 *  it (the earlier ones already ran), and a match that runs past the span extends it.
 *  A credential that starts exactly at the span's end needs nothing: no pattern here
 *  guards its start, so the next scan finds it. */
function gluedEnd(
  text: string, start: number, end: number, from: number, cache: ProbeCache,
): number {
  let nested = 0
  for (let pos = start + 1; pos < end; pos++) {
    const jwt = jwtProbeNeeded(text, pos, cache)
    const userinfo = userinfoProbeNeeded(text, pos, cache)
    for (let i = from; i < CRED_PATTERNS_AT.length; i++) {
      if ((IS_JWT_PATTERN[i] && !jwt) || (IS_USERINFO_PATTERN[i] && !userinfo)) continue
      const re = CRED_PATTERNS_AT[i]
      re.lastIndex = pos
      const m = re.exec(text)
      if (!m) continue
      const mEnd = pos + m[0].length
      if (mEnd > end) {
        end = mEnd
        nested = 0
      } else if (++nested >= GLUED_PROBES) {
        // Out of probes: a credential may start at a position not tested yet, so
        // the rest of the non-space run is redacted rather than left to chance.
        while (end < text.length && !isSpace(text[end])) end++
        return end
      }
    }
  }
  return end
}

/** The next match of pattern `i` at or after `from`, or null.
 *
 *  The JWT and userinfo patterns can read far before they fail, and a plain search
 *  retries them at every start, so they are tested only at the starts the probe
 *  filters admit, one sticky match at a time. The other patterns fail within a few
 *  characters, so their own search is used. */
function nextMatch(
  text: string, i: number, from: number, cache: ProbeCache,
): RegExpExecArray | null {
  if (!IS_JWT_PATTERN[i] && !IS_USERINFO_PATTERN[i]) {
    const re = CRED_PATTERNS[i]
    re.lastIndex = from
    return re.exec(text)
  }
  const jwt = IS_JWT_PATTERN[i]
  const lead = jwt ? 'eyJ' : 'http'
  const re = CRED_PATTERNS_AT[i]
  for (let pos = text.indexOf(lead, from); pos !== -1; pos = text.indexOf(lead, pos + 1)) {
    if (!(jwt ? jwtProbeNeeded(text, pos, cache) : userinfoProbeNeeded(text, pos, cache))) continue
    re.lastIndex = pos
    const m = re.exec(text)
    if (m) return m
  }
  return null
}

export function sanitizeCredentials(text: string): string {
  // Plaintext credential patterns. Every pattern reads the original text, so one
  // pattern's span can never hide another credential's start or end (an extended
  // span that reaches past a URL's `@` still leaves the URL's own match whole);
  // overlapping spans are then merged and each merged span is redacted once.
  const spans: [number, number][] = []
  CRED_PATTERNS.forEach((_re, i) => {
    const cache = newProbeCache()
    let m: RegExpExecArray | null
    let from = 0
    while ((m = nextMatch(text, i, from, cache))) {
      from = gluedEnd(text, m.index, m.index + m[0].length, i, cache)
      spans.push([m.index, from])
    }
  })
  spans.sort((a, b) => a[0] - b[0] || b[1] - a[1])
  let out = ''
  let last = 0
  for (let k = 0; k < spans.length; k++) {
    const start = spans[k][0]
    let end = spans[k][1]
    // Spans that touch without overlapping stay two placeholders, as two separate
    // replacements left them.
    while (k + 1 < spans.length && spans[k + 1][0] < end) end = Math.max(end, spans[++k][1])
    out += text.slice(last, start) + '[REDACTED]'
    last = end
  }
  out += text.slice(last)
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

// A SHA-1 git object name (40 lowercase hex digits) is a 40+ char run in the
// class above, so a commit permalink's `?commit=<sha>` fired the blob signal.
// It is not a base64 blob: hex is a 16-symbol alphabet. Mirrors the backend's
// `_GIT_OBJECT_NAME_RE` / `_exfil_blob_hit`: the test is on the MAXIMAL match,
// so 41+ digits, mixed case, padding or a SHA glued to other base64 characters
// are a longer or wider run and still redact. 64 hex (SHA-256) is not carved
// out: it is the shape of the product's own secrets. Same spelling as the backend.
const EXFIL_GIT_OBJECT_NAME_RE = /^[0-9a-f]{40}$/

// The blob signal minus bare git object names. Global so every maximal run in
// the value is judged, with `lastIndex` reset per call.
const EXFIL_B64_SCAN_RE = new RegExp(EXFIL_B64_RE.source, 'gi')
function exfilBlobHit(value: string): boolean {
  EXFIL_B64_SCAN_RE.lastIndex = 0
  let m: RegExpExecArray | null
  while ((m = EXFIL_B64_SCAN_RE.exec(value))) {
    if (!EXFIL_GIT_OBJECT_NAME_RE.test(m[0])) return true
  }
  return false
}

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
    const hit = EXFIL_PERCENT_RE.test(past) || EXFIL_CREDENTIAL_RE.test(past) || exfilBlobHit(past)
    const pathAndQuery = url.slice(m[0].length - (m[3] || '').length)
    const qmark = pathAndQuery.indexOf('?')
    if (qmark === -1 && !hit) continue
    const query = pathAndQuery.slice(qmark + 1)
    const redact = hit ||
      EXFIL_PERCENT_RE.test(query) ||
      EXFIL_CREDENTIAL_RE.test(query) ||
      exfilBlobHit(query) ||
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
