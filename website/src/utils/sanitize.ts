/** Output redaction — mirrors backend security.py patterns for frontend display. */

// ── Credential patterns (matches redact_credentials in security.py) ──
// Exported for the test that holds `CredentialScan` equal to these patterns.
export const CRED_PATTERNS: readonly RegExp[] = [
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
  // The false positive is closed by a structural test instead, as in the backend:
  // `CredentialScan` counts a hit only when its first segment decodes to a JSON
  // object (`isJsonObjectSegment`), so `honeyJar.atlassian.net` stays intact.
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

// ── Plaintext credentials: one scan with a reader per credential kind ──
//
// The scan visits every start in the text once. Where a kind's exact prefix begins,
// that kind's reader consumes the characters its `CRED_PATTERNS` entry takes and
// returns where the credential ends, or -1 when the text there is not one. Starts
// inside an earlier credential are read too, so a second credential written straight
// after the first is read from its own prefix even when the first one's body class
// already took its leading characters (a Slack token's body takes a JWT header up to
// its first `.`). Overlapping spans then become one placeholder.
//
// `CRED_PATTERNS` stays the spelling the backend parity test pins and the base64 check
// below runs. `sanitizeCredentialsGlued.test.ts` runs `CRED_PATTERNS` itself, one
// `replace` per pattern, and checks this scan redacts exactly the same on text with no
// glued credentials, so a change to a pattern fails that test until its reader follows.
// The intended differences are the JWT header check below, which those patterns, and
// the base64 check that runs them, lack (it over-redacts, the safe direction), and a key
// assignment whose value is already a backend tag, which is left alone.

function isDigit(c: number): boolean { return c >= 48 && c <= 57 }
function isUpper(c: number): boolean { return c >= 65 && c <= 90 }
function isLower(c: number): boolean { return c >= 97 && c <= 122 }
// `[A-Z0-9]`, the AWS key id body.
function isKeyIdChar(c: number): boolean { return isUpper(c) || isDigit(c) }
// `[0-9a-zA-Z-]`, the Slack token body.
function isSlackChar(c: number): boolean { return isUpper(c) || isLower(c) || isDigit(c) || c === 45 }
// `[A-Za-z0-9_-]`, one JWT segment.
function isSegmentChar(c: number): boolean { return isSlackChar(c) || c === 95 }
// `\S`. `trim` strips exactly the characters `\s` matches.
function isNonSpace(c: number): boolean { return String.fromCharCode(c).trim() !== '' }

/** Whether a JWT's first segment base64url-decodes to a JSON object, as every JWS/JWE
 *  header does and a hostname label does not: the backend's `jwt_header` check. A
 *  parse that fails on anything but syntax (a stack overflow) counts as one. */
function isJsonObjectSegment(segment: string): boolean {
  let decoded: string
  try {
    decoded = atob(segment.replace(/-/g, '+').replace(/_/g, '/'))
  } catch {
    return false
  }
  try {
    const value: unknown = JSON.parse(decoded)
    return typeof value === 'object' && value !== null && !Array.isArray(value)
  } catch (e) {
    return !(e instanceof SyntaxError)
  }
}

// The key-assignment names, lower case. Their patterns carry the `i` flag, which
// without `u` matches ASCII letters in either case and every other character exactly.
const KEY_NAMES = [
  'secretaccesskey', 'aws_secret_access_key',
  'sessiontoken', 'aws_session_token',
  'accesskeyid', 'aws_access_key_id',
]
const PEM_KEY_TYPES = ['RSA', 'DSA', 'EC', 'OPENSSH']

/** Where `ch` next occurs at or after a position, remembered so that queries from
 *  rising positions search each stretch of the text once. -1 when it does not. */
class NextIndex {
  private from = -1
  private at = -1

  constructor(private readonly text: string, private readonly ch: string) {}

  find(from: number): number {
    if (this.from === -1 || from < this.from || (this.at !== -1 && from > this.at)) {
      this.from = from
      this.at = this.text.indexOf(this.ch, from)
    }
    return this.at
  }
}

class CredentialScan {
  // The end of the body run each kind read last. A later start inside that run reads
  // the same run to the same end, so its credential lies inside the one already
  // recorded (or fails the same length check) and is not read again.
  private slackRun = -1
  private jwtRun = -1
  private valueFrom = -1
  private valueRun = -1
  private readonly colon: NextIndex
  private readonly at: NextIndex

  constructor(private readonly text: string) {
    this.colon = new NextIndex(text, ':')
    this.at = new NextIndex(text, '@')
  }

  /** Where the furthest credential starting at `pos` ends, or -1. */
  endAt(pos: number): number {
    return Math.max(
      this.keyId(pos), this.keyAssignment(pos), this.pemHeader(pos),
      this.slack(pos), this.jwt(pos), this.userinfo(pos),
    )
  }

  private runEnd(i: number, inRun: (c: number) => boolean): number {
    const t = this.text
    while (i < t.length && inRun(t.charCodeAt(i))) i++
    return i
  }

  private skipSpace(i: number): number {
    const t = this.text
    while (i < t.length && !isNonSpace(t.charCodeAt(i))) i++
    return i
  }

  private startsWithName(pos: number, name: string): boolean {
    const t = this.text
    if (pos + name.length > t.length) return false
    for (let k = 0; k < name.length; k++) {
      let c = t.charCodeAt(pos + k)
      if (isUpper(c)) c += 32
      if (c !== name.charCodeAt(k)) return false
    }
    return true
  }

  // `(?:AKIA|ASIA)[A-Z0-9]{16}`
  private keyId(pos: number): number {
    const t = this.text
    if (!t.startsWith('AKIA', pos) && !t.startsWith('ASIA', pos)) return -1
    const end = pos + 20
    if (end > t.length) return -1
    for (let i = pos + 4; i < end; i++) if (!isKeyIdChar(t.charCodeAt(i))) return -1
    return end
  }

  // `(?:SecretAccessKey|aws_secret_access_key)\s*[:=]\s*\S+` with the `i` flag, and
  // the same shape for the session token and the access key id.
  private keyAssignment(pos: number): number {
    const name = KEY_NAMES.find((n) => this.startsWithName(pos, n))
    if (name === undefined) return -1
    const sep = this.skipSpace(pos + name.length)
    if (this.text[sep] !== ':' && this.text[sep] !== '=') return -1
    const value = this.skipSpace(sep + 1)
    // The backend keeps an AWS key's label and writes its tag in place of the value. A
    // value that is already that tag, after an opening quote that may be JSON-escaped,
    // holds no secret, so it is left as written.
    let open = this.text[value] === '\\' ? value + 1 : value
    if (this.text[open] === '"' || this.text[open] === "'") open++
    if (this.text.startsWith('[REDACTED: credential]', open)
      || this.text.startsWith('[REDACTED: encoded credential]', open)) return -1
    if (value >= this.valueFrom && value < this.valueRun) return -1
    const end = this.runEnd(value, isNonSpace)
    this.valueFrom = value
    this.valueRun = end
    return end > value ? end : -1
  }

  // `BEGIN\s(?:RSA|DSA|EC|OPENSSH)\sPRIVATE\sKEY`
  private pemHeader(pos: number): number {
    const t = this.text
    if (!t.startsWith('BEGIN', pos)) return -1
    let i = pos + 5
    if (i >= t.length || isNonSpace(t.charCodeAt(i))) return -1
    i++
    const type = PEM_KEY_TYPES.find((k) => t.startsWith(k, i))
    if (type === undefined) return -1
    i += type.length
    if (i >= t.length || isNonSpace(t.charCodeAt(i))) return -1
    i++
    if (!t.startsWith('PRIVATE', i)) return -1
    i += 7
    if (i >= t.length || isNonSpace(t.charCodeAt(i))) return -1
    i++
    return t.startsWith('KEY', i) ? i + 3 : -1
  }

  // `xox[bpas]-[0-9a-zA-Z-]{10,}`
  private slack(pos: number): number {
    const t = this.text
    if (!t.startsWith('xox', pos) || t[pos + 4] !== '-') return -1
    const kind = t[pos + 3]
    if (kind !== 'b' && kind !== 'p' && kind !== 'a' && kind !== 's') return -1
    const body = pos + 5
    if (body < this.slackRun) return -1
    this.slackRun = this.runEnd(body, isSlackChar)
    return this.slackRun - body >= 10 ? this.slackRun : -1
  }

  // `eyJ[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]*){2,4}` (a JWS, or a compact JWE whose key
  // segment may be empty) and `eyJ[A-Za-z0-9_-]{96,}\.[A-Za-z0-9_-]{43}(?![A-Za-z0-9_-])`
  // (the two-segment dashboard link token). The segment class has no `.`, so the
  // first segment always runs to the end of its run. A JWS/JWE hit counts only when
  // its header is a JSON object or holds a second `eyJ`, as in the backend.
  private jwt(pos: number): number {
    const t = this.text
    if (!t.startsWith('eyJ', pos) || pos < this.jwtRun) return -1
    const first = this.runEnd(pos + 3, isSegmentChar)
    this.jwtRun = first
    if (first === pos + 3) return -1
    let end = -1
    let i = first
    let dots = 0
    while (dots < 4 && t[i] === '.') {
      i = this.runEnd(i + 1, isSegmentChar)
      dots++
    }
    if (dots >= 2) {
      const header = t.slice(pos, first)
      if (header.indexOf('eyJ', 1) !== -1 || isJsonObjectSegment(header)) end = i
    }
    if (first - (pos + 3) >= 96 && t[first] === '.') {
      const sig = this.runEnd(first + 1, isSegmentChar)
      if (sig - (first + 1) === 43) end = Math.max(end, sig)
    }
    return end
  }

  // `https?:\/\/[^:]+:[^@]+@`: the user runs to the first `:` after `//`, the
  // password to the first `@` after that, and each holds at least one character.
  private userinfo(pos: number): number {
    const t = this.text
    if (!t.startsWith('http', pos)) return -1
    const scheme = t[pos + 4] === 's' ? pos + 5 : pos + 4
    if (!t.startsWith('://', scheme)) return -1
    const user = scheme + 3
    const colon = this.colon.find(user)
    if (colon <= user) return -1
    const at = this.at.find(colon + 1)
    return at > colon + 1 ? at + 1 : -1
  }
}

export function sanitizeCredentials(text: string): string {
  const scan = new CredentialScan(text)
  let out = ''
  let last = 0
  let start = -1
  let end = -1
  for (let pos = 0; pos < text.length; pos++) {
    const credentialEnd = scan.endAt(pos)
    if (credentialEnd === -1) continue
    if (pos < end) {
      end = Math.max(end, credentialEnd)
      continue
    }
    // Spans that touch without overlapping stay two placeholders, as two separate
    // replacements left them.
    if (start !== -1) {
      out += text.slice(last, start) + '[REDACTED]'
      last = end
    }
    start = pos
    end = credentialEnd
  }
  if (start !== -1) {
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

/** The sanitizer for model-authored text shown outside the transcript: masks
 *  credential values, and leaves every URL as written. */
export function sanitizeLlmOutput(text: string): string {
  return sanitizeCredentials(text)
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
