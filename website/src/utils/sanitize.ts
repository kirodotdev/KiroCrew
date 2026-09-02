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

export function sanitizeCredentials(text: string): string {
  let out = text
  // Plaintext credential patterns
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

// ── Exfiltration URL detection (matches redact_exfiltration_urls in security.py) ──
const URL_RE = /https?:\/\/([a-zA-Z0-9._-]+\.[a-zA-Z]{2,})(:\d+)?(\/[^\s)"'>]*)?/g
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
const EXFIL_B64_RE = /[A-Za-z0-9+/=]{40,}/i

// Aggregate query LENGTH is the one signal that names no shape at all: it fires on
// any richly-parameterised URL, which is why prefilled issue links —
// `…/issues/new?title=…&body=<a paragraph of prose>&labels=…` — were rendered as a
// `[REDACTED: suspicious URL]` placeholder. It is the only check the carve-out
// below waives, and it is waived only for a URL whose every component is
// accounted for. A query that ALSO trips a pattern signal is still redacted, so a
// `+`-spelled prose body stays a placeholder even inside the validated shape.
//
// The carve-out validates the payload's SHAPE rather than trusting a destination.
// That is nearer the backend's Slack app-create link check than its
// companion-owned host-exemption tier, but it is not the same move: the backend
// narrows the payload to its one caller-controlled span and keeps that span under
// every heuristic, which is unavailable here because every GitHub prefill
// parameter value is caller-controlled and there is no constant template to
// subtract. So one SIGNAL is waived here where one SPAN is there.
// `github.com` cannot earn destination trust either:
// it is a public multi-tenant WRITE sink, so a prefilled issue submitted there
// lands in whichever repository the URL names, including an attacker's own. What
// is trustworthy is not the host but this exact shape, whose every span is either
// a fixed literal or a parameter GitHub itself defines.
const EXFIL_ISSUE_SCHEME = 'https://'
const EXFIL_ISSUE_HOST = 'github.com'
// Each of the two segments is dot-SEPARATED rather than dot-permissive, so
// neither may end with a dot or contain `..`: a traversal spelling names a path
// GitHub never served (a browser normalises it away before sending), so it is an
// unaccounted-for component like any other. A single LEADING dot stays legal —
// `.github` is an ordinary repository name.
//
// A RegExp literal spliced by `.source`, not a pattern string: the escaping then
// reads at one level (`\.`, the character, rather than `\\.`, two characters that
// happen to compile to it), which is what makes a dot-SEPARATED class auditable
// against a dot-permissive one at a glance.
const EXFIL_ISSUE_SEGMENT = /\.?[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*/
const EXFIL_ISSUE_PATH_RE = new RegExp(
  `^/${EXFIL_ISSUE_SEGMENT.source}/${EXFIL_ISSUE_SEGMENT.source}/issues/new$`,
)
// GitHub's documented issue-prefill parameters. A query carrying ANY other key is
// refused whole rather than having the unknown key judged on its own: an extra
// parameter is the obvious smuggling shape, and a key that is empty or cased
// differently is one GitHub would not prefill from either.
const EXFIL_ISSUE_PARAMS = new Set([
  'assignee',
  'assignees',
  'body',
  'labels',
  'milestone',
  'projects',
  'template',
  'title',
])

/**
 * True only for a GitHub issue-creation URL whose scheme, host, port, path and
 * complete parameter-key set are all accounted for. Anything unaccounted-for
 * fails closed, leaving the length check in force.
 */
function isPrefilledIssueUrl(
  url: string,
  host: string,
  port: string,
  path: string,
  query: string,
): boolean {
  if (url.slice(0, EXFIL_ISSUE_SCHEME.length).toLowerCase() !== EXFIL_ISSUE_SCHEME) return false
  // Host is compared lowercased (RFC 4343 leaves DNS case insignificant) and
  // EXACTLY, never by suffix, so `github.com.evil.example` is not the same host.
  // An explicit port is refused outright: `github.com:8080` is not a destination
  // GitHub serves, so it is somebody redirecting the name somewhere else.
  if (host !== EXFIL_ISSUE_HOST || port) return false
  if (!EXFIL_ISSUE_PATH_RE.test(path)) return false
  return query.split('&').every((pair) => {
    const eq = pair.indexOf('=')
    // `eq > 0` also rejects a pair with no `=` at all and one whose key is empty:
    // neither presents a key that can be checked, so it counts as unknown.
    return eq > 0 && EXFIL_ISSUE_PARAMS.has(pair.slice(0, eq))
  })
}

export function sanitizeExfiltrationUrls(text: string): string {
  let out = text
  URL_RE.lastIndex = 0
  for (const m of text.matchAll(URL_RE)) {
    const domain = m[1]
    const host = domain.toLowerCase()
    const pathAndQuery = m[3] || ''
    const qmark = pathAndQuery.indexOf('?')
    if (qmark === -1) continue
    const query = pathAndQuery.slice(qmark + 1)
    let redact =
      EXFIL_PERCENT_RE.test(query) ||
      EXFIL_CREDENTIAL_RE.test(query) ||
      EXFIL_B64_RE.test(query)
    if (!redact && query.length >= EXFIL_QUERY_MIN_LEN) {
      redact = !isPrefilledIssueUrl(m[0], host, m[2] || '', pathAndQuery.slice(0, qmark), query)
    }
    if (redact) {
      out = out.replace(m[0], i18nT('utils.sanitize.redacted_suspicious_url', { domain }))
    }
  }
  return out
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
