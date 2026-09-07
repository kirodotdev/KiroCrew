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
  let out = text
  URL_RE.lastIndex = 0
  for (const m of text.matchAll(URL_RE)) {
    const domain = m[1]
    const pathAndQuery = m[3] || ''
    const qmark = pathAndQuery.indexOf('?')
    if (qmark === -1) continue
    const query = pathAndQuery.slice(qmark + 1)
    const redact =
      EXFIL_PERCENT_RE.test(query) ||
      EXFIL_CREDENTIAL_RE.test(query) ||
      EXFIL_B64_RE.test(query) ||
      query.length >= EXFIL_QUERY_MIN_LEN
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
