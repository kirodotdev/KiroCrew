/** `sanitizeExfiltrationUrls` is the browser-side mirror of the backend's
 *  per-URL exfil classifier (`_exfil_url_warning` in security.py). Every PATTERN
 *  signal — heavy percent-encoding, hard credential markers, base64 blob — runs
 *  for every URL. Only the aggregate query-LENGTH signal, which names no shape at
 *  all, is waived, and only for a GitHub issue-creation URL whose scheme, host,
 *  port, path and complete parameter-key set are all accounted for.
 *
 *  These tests pin three directions: a long prefilled link whose spaces are `%20`
 *  renders unchanged; every span of the validated shape is load-bearing — change
 *  any one of them and the length signal is back in force; and every pattern
 *  signal survives inside the validated shape, including the base64 signal's
 *  over-match on a `+`-encoded prose body, which stays redacted on purpose.
 */
import { describe, it, expect } from 'vitest'
import { sanitizeExfiltrationUrls } from '../utils/sanitize'

// A benign, prefilled GitHub issue query >=200 chars carrying no credential
// marker, no 20+ consecutive percent-octets and no 40+ char base64 run, so the
// aggregate-length signal is the only one that can flag it. No literal spaces:
// URL_RE's path group stops at whitespace, so this uses the `%20` spelling, whose
// `%` also breaks up any long run in `[A-Za-z0-9+/=]`. The `+` spelling of the
// same link does NOT reach the carve-out — it trips the base64 signal first, and
// the last test in this file pins that as deliberate.
const LONG_BENIGN_QUERY =
  'title=' + 'Bug%20report%20'.repeat(8) +
  '&body=' + 'Steps%20to%20reproduce%20and%20expected%20behavior%20go%20here.%20'.repeat(4) +
  '&labels=bug,triage,needs-repro'
const ISSUE_URL = `https://github.com/kirodotdev/KiroCrew/issues/new?${LONG_BENIGN_QUERY}`

/** Asserts the whole input survives untouched. */
function expectKept(url: string): void {
  const text = `see ${url} for details`
  expect(sanitizeExfiltrationUrls(text)).toBe(text)
}

/** Asserts the URL was replaced by the redaction placeholder. */
function expectRedacted(url: string): void {
  const out = sanitizeExfiltrationUrls(`see ${url} for details`)
  expect(out).not.toContain(url)
}

describe('sanitizeExfiltrationUrls: the reported false positive', () => {
  it('keeps a long prefilled GitHub issue link', () => {
    expect(LONG_BENIGN_QUERY.length).toBeGreaterThanOrEqual(200)
    expectKept(ISSUE_URL)
  })

  it('keeps it when the host case differs', () => {
    // RFC 4343: DNS host case is not significant, so `GitHub.com` is the same
    // destination and must be validated the same way.
    expectKept(`https://GitHub.com/kirodotdev/KiroCrew/issues/new?${LONG_BENIGN_QUERY}`)
  })

  it('keeps a short query at any host, as before the narrowing', () => {
    expectKept('https://example.com/page?ref=chat&tab=1')
  })

  it('keeps it for a dot-leading repository name', () => {
    // `.github` is an ordinary repository, so refusing a leading dot outright
    // (while still refusing `..`) would carve out less than the shape allows.
    expectKept(`https://github.com/kirodotdev/.github/issues/new?${LONG_BENIGN_QUERY}`)
  })
})

describe('sanitizeExfiltrationUrls: every span of the validated shape is load-bearing', () => {
  it('redacts an unvalidated path at the same host', () => {
    expectRedacted(`https://github.com/kirodotdev/KiroCrew/settings?${LONG_BENIGN_QUERY}`)
  })

  it('redacts a deeper path that merely ends in issues/new', () => {
    expectRedacted(`https://github.com/a/b/c/issues/new?${LONG_BENIGN_QUERY}`)
  })

  it('redacts an unknown query parameter smuggled alongside the known ones', () => {
    expectRedacted(`https://github.com/o/r/issues/new?${LONG_BENIGN_QUERY}&leak=${'x'.repeat(8)}`)
  })

  it('redacts a parameter whose key is empty', () => {
    expectRedacted(`https://github.com/o/r/issues/new?${LONG_BENIGN_QUERY}&=payload`)
  })

  it('redacts an owner/repo segment spelled as a `..` traversal', () => {
    // A browser normalises `..` away before sending, so this names a path
    // github.com never served — an unaccounted-for component like any other.
    expectRedacted(`https://github.com/../../issues/new?${LONG_BENIGN_QUERY}`)
  })

  it('redacts an explicit port on the exempt host', () => {
    expectRedacted(`https://github.com:8080/o/r/issues/new?${LONG_BENIGN_QUERY}`)
  })

  it('redacts the plaintext-http spelling of the exempt shape', () => {
    expectRedacted(`http://github.com/o/r/issues/new?${LONG_BENIGN_QUERY}`)
  })

  it('does NOT treat a suffix look-alike host as the exempt host', () => {
    const url = `https://github.com.evil.example/o/r/issues/new?${LONG_BENIGN_QUERY}`
    const out = sanitizeExfiltrationUrls(`leak: ${url}`)
    expect(out).not.toContain(url)
    expect(out).toContain('github.com.evil.example')
  })

  it('still redacts an arbitrary host with a >=200-char query', () => {
    const url = `https://evil.example/collect?${LONG_BENIGN_QUERY}`
    const out = sanitizeExfiltrationUrls(`leak: ${url}`)
    expect(out).not.toContain(url)
    expect(out).toContain('evil.example')
  })
})

describe('sanitizeExfiltrationUrls: no pattern signal is waived', () => {
  // The carve-out waives the aggregate-length signal alone. Each case below puts
  // a pattern signal inside the fully validated exempt shape and asserts it still
  // redacts, which is what bounds the narrowing: an unbounded payload cannot ride
  // through on a validated destination.
  const validPrefix = 'https://github.com/kirodotdev/KiroCrew/issues/new?body='

  it('redacts a base64 blob inside the validated shape', () => {
    expectRedacted(`${validPrefix}${'A'.repeat(48)}`)
  })

  it('redacts a base64 blob that keeps the query under the length threshold', () => {
    const url = `${validPrefix}${'A'.repeat(48)}`
    expect(url.slice(url.indexOf('?') + 1).length).toBeLessThan(200)
    expectRedacted(url)
  })

  it('redacts 20+ consecutive percent-octets inside the validated shape', () => {
    expectRedacted(`${validPrefix}${'%41'.repeat(21)}`)
  })

  it('redacts an AWS access key id inside the validated shape', () => {
    expectRedacted(`${validPrefix}AKIAIOSFODNN7EXAMPLE`)
  })

  it('redacts an ssh public-key marker inside the validated shape', () => {
    expectRedacted(`${validPrefix}ssh-ed25519%20AAAA`)
  })

  it('redacts a private-key header inside the validated shape', () => {
    // Form-encoded spaces. The marker patterns admit `+`, `%` or literal
    // whitespace between their words but the frontend never percent-DECODES, so
    // a `%20`-separated spelling is out of reach here where it is not in the
    // backend — a divergence the carve-out neither creates nor widens.
    expectRedacted(`${validPrefix}BEGIN+OPENSSH+PRIVATE+KEY`)
  })

  it('redacts a Slack token inside the validated shape', () => {
    expectRedacted(`${validPrefix}xoxb-123456789012-abcdefghijkl`)
  })

  it('keeps a short benign percent-run inside the validated shape', () => {
    // Counterpart to the case above: a few percent-octets are ordinary encoding,
    // so the 20+ CONSECUTIVE threshold is what separates the two.
    expectKept(`${validPrefix}${'%41'.repeat(5)}`)
  })
})

describe('sanitizeExfiltrationUrls: the `+`-encoded prose body is still redacted', () => {
  // `+` is the form-encoded spelling of a space — what `URLSearchParams` emits —
  // and it is inside EXFIL_B64_RE's class, so ~7 words of unpunctuated prose in a
  // `+`-encoded `body=` are one 40+ char run and the URL is redacted before
  // isPrefilledIssueUrl() is ever consulted. This is DELIBERATE, not an oversight:
  // the two ways to stop it — dropping `+` from the class, or splitting the query
  // on `+` before testing — both let an attacker `+`-chunk a 40+ char secret
  // straight past the signal, and a chunking bypass costs more than a placeholder
  // on a prose link. So the carve-out covers the `%20` spelling of a prefilled
  // issue link and not the `+` spelling, and these tests pin that boundary so it
  // is a documented limit rather than a surprise.
  const PLUS_PROSE_URL =
    'https://github.com/kirodotdev/KiroCrew/issues/new?title=Dashboard+chat+drops+long+links' +
    '&body=The+dashboard+chat+redaction+fires+on+ordinary+prefilled+issue+links+and+replaces' +
    '+them+with+a+placeholder&labels=bug'

  it('redacts it even though the query is UNDER the length threshold', () => {
    // Proof the base64 signal, not the length signal, is what fires: waiving
    // length could not have changed this verdict.
    const query = PLUS_PROSE_URL.slice(PLUS_PROSE_URL.indexOf('?') + 1)
    expect(query.length).toBeLessThan(200)
    expectRedacted(PLUS_PROSE_URL)
  })

  it('keeps the same link once its spaces are `%20`, which breaks the run', () => {
    // The other side of the boundary, and the shape a realistic prefilled link
    // has once any parameter carries a percent-escape (see the `%20`/`%2C` mix in
    // MarkdownRenderer.longUrlLinkify's fixture).
    expectKept(ISSUE_URL)
  })
})
