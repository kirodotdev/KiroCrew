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

describe('sanitizeExfiltrationUrls: no shape waives redaction of model-authored text', () => {
  it('redacts a long prefilled GitHub issue link to this project own tracker', () => {
    // This block used to assert the opposite. Two waivers were tried (#7824 on
    // shape, then pinned to this repository) and both are exfiltration primitives:
    // what this function sanitizes is MODEL-AUTHORED text, so injected content can
    // steer the model into emitting a prefill URL whose `body` carries encoded
    // private context. The user submits it and the issue is PUBLIC, so pinning the
    // repository changes who reads the payload from the attacker to everyone,
    // including the attacker. The feature is served by the backend's structured
    // `github_issue_url` field instead, which no redactor scans.
    expect(LONG_BENIGN_QUERY.length).toBeGreaterThanOrEqual(200)
    expectRedacted(ISSUE_URL)
  })

  it('redacts it when the host case differs', () => {
    // RFC 4343 leaves DNS host case insignificant; with no waiver it changes nothing.
    expectRedacted(`https://GitHub.com/kirodotdev/KiroCrew/issues/new?${LONG_BENIGN_QUERY}`)
  })

  it('keeps a short query at any host, so only the length signal moved', () => {
    expectKept('https://example.com/page?ref=chat&tab=1')
  })

  it('redacts a prefill link to an attacker-owned repository', () => {
    expectRedacted(`https://github.com/attacker/exfil-sink/issues/new?${LONG_BENIGN_QUERY}`)
  })

  it('redacts the other long-query host the same report names', () => {
    // #7820 reports monitorportal.amazon.com alongside the prefill link. Both stay
    // redacted: narrowing this heuristic is a per-host decision on its own merits,
    // not a per-shape escape hatch.
    expectRedacted(
      'https://monitorportal.amazon.com/metrics?namespace=AWS/SageMaker' +
        '&metricName=Invocations&dimensions=EndpointName%3Dmy-endpoint' +
        '&startTime=2026-09-01T00%3A00%3A00Z&period=300&stat=Sum&region=us-west-2' +
        '&accountId=123456789012&view=timeSeries&label=long+enough+to+pass+two+hundred',
    )
  })
})

describe('sanitizeExfiltrationUrls: the length signal applies to every host', () => {
  // These used to prove each span of a validated exempt SHAPE was load-bearing.
  // With no exempt shape they prove the simpler and stronger property: a >=200-char
  // query is redacted wherever it appears, with no path, port or scheme exception.
  it('redacts a long query at this project own repository', () => {
    expectRedacted(`https://github.com/kirodotdev/KiroCrew/settings?${LONG_BENIGN_QUERY}`)
  })

  it('redacts an explicit port on github.com', () => {
    expectRedacted(`https://github.com:8080/o/r/issues/new?${LONG_BENIGN_QUERY}`)
  })

  it('redacts the plaintext-http spelling', () => {
    expectRedacted(`http://github.com/o/r/issues/new?${LONG_BENIGN_QUERY}`)
  })

  it('does NOT treat a suffix look-alike host as github.com', () => {
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
  // These predate the carve-out and outlive it. Each puts a pattern signal in a
  // prefill-shaped URL to this project's own tracker and asserts it still redacts,
  // independent of the length signal.
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

describe('sanitizeExfiltrationUrls: both spellings of a prose body are redacted', () => {
  // The two cases below are the same logical prefilled issue link spelled two ways,
  // and they are redacted by DIFFERENT signals — which is why the pair is worth
  // keeping now that nothing is waived.
  //
  // `+` is the form-encoded spelling of a space — what `URLSearchParams` emits —
  // and it is inside EXFIL_B64_RE's class, so ~7 words of unpunctuated prose in a
  // `+`-encoded `body=` are one 40+ char run. That fires regardless of length, and
  // it is DELIBERATE: the two ways to stop it — dropping `+` from the class, or
  // splitting the query on `+` before testing — both let an attacker `+`-chunk a
  // 40+ char secret straight past the signal.
  //
  // The `%20` spelling breaks that run, so it reaches the aggregate-length signal
  // instead and is redacted there. It used to be KEPT, by the withdrawn
  // `isPrefilledIssueUrl` waiver; a validated shape no longer earns an exception,
  // because the shape of a URL says nothing about who authored it.
  const PLUS_PROSE_URL =
    'https://github.com/kirodotdev/KiroCrew/issues/new?title=Dashboard+chat+drops+long+links' +
    '&body=The+dashboard+chat+redaction+fires+on+ordinary+prefilled+issue+links+and+replaces' +
    '+them+with+a+placeholder&labels=bug'

  it('redacts the `+` spelling even though the query is UNDER the length threshold', () => {
    // Proof the base64 signal, not the length signal, is what fires here.
    const query = PLUS_PROSE_URL.slice(PLUS_PROSE_URL.indexOf('?') + 1)
    expect(query.length).toBeLessThan(200)
    expectRedacted(PLUS_PROSE_URL)
  })

  it('redacts the `%20` spelling on length once the base64 run is broken', () => {
    // The other side of the boundary: no 40+ char run in `[A-Za-z0-9+/=]`, so this
    // one is caught by aggregate query length alone. Asserting both properties
    // keeps it from passing for the wrong reason if the fixture ever changes.
    const query = ISSUE_URL.slice(ISSUE_URL.indexOf('?') + 1)
    expect(query.length).toBeGreaterThanOrEqual(200)
    expect(/[A-Za-z0-9+/=]{40,}/.test(query)).toBe(false)
    expectRedacted(ISSUE_URL)
  })
})
