/** Output redaction — mirrors backend security.py patterns for frontend display. */

// ── Credential patterns (matches redact_credentials in security.py) ──
const CRED_PATTERNS: RegExp[] = [
  /(?:AKIA|ASIA)[A-Z0-9]{16}/g,
  /(?:SecretAccessKey|aws_secret_access_key)\s*[:=]\s*\S+/gi,
  /(?:SessionToken|aws_session_token)\s*[:=]\s*\S+/gi,
  /(?:AccessKeyId|aws_access_key_id)\s*[:=]\s*\S+/gi,
  /BEGIN\s(?:RSA|DSA|EC|OPENSSH)\sPRIVATE\sKEY/g,
  /xox[bpas]-[0-9a-zA-Z-]{10,}/g,
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
const EXFIL_PATTERNS = new RegExp(
  '(?:' +
    '[A-Za-z0-9+/=]{40,}' +                          // base64-like blob
    '|%[0-9A-Fa-f]{2}(?:%[0-9A-Fa-f]{2}){20,}' +    // heavy URL-encoding
    '|(?:AKIA|ASIA)[A-Z0-9]{16}' +                   // AWS access key ID
    '|(?:ssh-rsa|ssh-ed25519)[\\s+%]' +               // SSH public key
    '|BEGIN[\\s+%](?:RSA|DSA|EC|OPENSSH)[\\s+%]PRIVATE[\\s+%]KEY' + // private key header
    '|xox[bpas]-[0-9a-zA-Z-]+' +                     // Slack token
  ')',
  'i',
)

export function sanitizeExfiltrationUrls(text: string): string {
  let out = text
  URL_RE.lastIndex = 0
  for (const m of text.matchAll(URL_RE)) {
    const domain = m[1]
    const pathAndQuery = m[3] || ''
    const qmark = pathAndQuery.indexOf('?')
    if (qmark === -1) continue
    const query = pathAndQuery.slice(qmark + 1)
    if (query.length >= EXFIL_QUERY_MIN_LEN || EXFIL_PATTERNS.test(query)) {
      out = out.replace(m[0], `[REDACTED: suspicious URL to ${domain}]`)
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
