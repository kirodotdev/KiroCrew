/** `sanitizeCredentials` on two credentials written back to back.
 *
 *  The patterns run one after another, so when the first credential's body class
 *  also takes the second one's leading characters, the second pattern no longer
 *  matches what the first left and most of the second credential stays on screen.
 *  Every value here is synthetic.
 */
import { describe, it, expect, vi } from 'vitest'
import { sanitizeCredentials } from '../utils/sanitize'

const SLACK = 'xoxb-1234567890-abcdefghij'
// Both assembled at run time so the content scanners do not flag the literals.
const AKIA = ['AKIA', 'IOSFODNN7EXAMPLE'].join('') // AWS's documented example key id
const PEM_HEADER = ['-----BEGIN', 'KEY-----'].join(' RSA PRIVATE ')
const JWS_HEADER = 'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9'
const JWS_PAYLOAD = 'eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvaG4gRG9lIn0'
const JWS_SIG = 'dQw4w9WgXcQdQw4w9WgXcQdQw4w9WgXcQdQw4w9WgXc'
const JWS = `${JWS_HEADER}.${JWS_PAYLOAD}.${JWS_SIG}`
// A compact JWE: four dots, the most the JWT pattern takes, so a token glued after
// it is cut at its first dot.
const JWE =
  'eyJhbGciOiJkaXIiLCJlbmMiOiJBMjU2R0NNIn0..48V1_ALb6US04U3b' +
  '.5eym8TW_c8SuK0ltJ3rpYIzOeDQz7TALvtu6UG9oMo4vpzs9tX_EFShS8iB7j6ji.XFBoMYUZodetZdvTiFvSkQ'
const LINK_PAYLOAD =
  'eyJzdWIiOiJsb2NhbC1hcHAiLCJleHAiOjE3ODU0MTc2MDYsInNlc3Npb25fZXhwIjoxNzg1NDg5MzA2' +
  'LCJpYXQiOjE3ODU0MTczMDYsIm5vbmNlIjoiOTM5YzE3MGQ5ZjBiNmEyMiIsImdlbiI6MH0'
const LINK_SIG = 'gVhM4aKLA8dyFH-oZlQx6SpYSNPkXA07kpDhWd6UhZI'
const LINK_TOKEN = `${LINK_PAYLOAD}.${LINK_SIG}`
const PASSWORD = 'Sx9fakePassw0rdValue'
const USERINFO_URL = `https://deploy:${PASSWORD}@git.example/r.git`

/** One `replace` per pattern, with no glued-credential scan: text with no glued
 *  credentials must come out of this and `sanitizeCredentials` exactly the same. */
function sequentialReplace(text: string): string {
  const patterns = [
    /(?:AKIA|ASIA)[A-Z0-9]{16}/g,
    /(?:SecretAccessKey|aws_secret_access_key)\s*[:=]\s*\S+/gi,
    /(?:SessionToken|aws_session_token)\s*[:=]\s*\S+/gi,
    /(?:AccessKeyId|aws_access_key_id)\s*[:=]\s*\S+/gi,
    /BEGIN\s(?:RSA|DSA|EC|OPENSSH)\sPRIVATE\sKEY/g,
    /xox[bpas]-[0-9a-zA-Z-]{10,}/g,
    /eyJ[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]*){2,4}/g,
    /eyJ[A-Za-z0-9_-]{96,}\.[A-Za-z0-9_-]{43}(?![A-Za-z0-9_-])/g,
    /https?:\/\/[^:]+:[^@]+@/g,
  ]
  return patterns.reduce((out, re) => out.replace(re, '[REDACTED]'), text)
}

/** `sanitizeCredentials(text)`, plus how many times an anchored (sticky) pattern
 *  whose source starts with `sourcePrefix` was tested: the glued probes are the only
 *  sticky matches it runs, so this counts probes without timing anything. */
function countProbes(
  text: string, sourcePrefix: string,
): { out: string, probes: number, freeScans: number } {
  const exec = RegExp.prototype.exec
  let probes = 0
  // Unanchored searches of the same patterns over the input: one of them retries at
  // every start inside the engine, which no call count can see, so there must be none.
  let freeScans = 0
  const spy = vi.spyOn(RegExp.prototype, 'exec').mockImplementation(function (
    this: RegExp, s: string,
  ) {
    if (this.source.startsWith(sourcePrefix)) {
      if (this.sticky) probes++
      else if (s === text) freeScans++
    }
    return exec.call(this, s)
  })
  try {
    const out = sanitizeCredentials(text)
    return { out, probes, freeScans }
  } finally {
    spy.mockRestore()
  }
}

function expectNoneOf(out: string, parts: string[]): void {
  for (const part of parts) expect(out).not.toContain(part)
}

describe('sanitizeCredentials: glued credentials', () => {
  it('redacts a JWT glued after a Slack token', () => {
    // The Slack body class takes the JWT header up to its first `.`, which left
    // `.payload.signature` with no `eyJ` start in front of it.
    const out = sanitizeCredentials(`log: ${SLACK}${JWS} end`)

    expect(out).toBe('log: [REDACTED] end')
    expectNoneOf(out, [JWS_HEADER, JWS_PAYLOAD, JWS_SIG])
  })

  it('redacts a JWS glued after a JWE', () => {
    // The JWT pattern takes four dots at most, so the JWE match ends inside the
    // JWS header and its payload and signature were left.
    const out = sanitizeCredentials(`log: ${JWE}${JWS} end`)

    expect(out).toBe('log: [REDACTED] end')
    expectNoneOf(out, [JWS_PAYLOAD, JWS_SIG, 'XFBoMYUZodetZdvTiFvSkQ'])
  })

  it('redacts a link token glued after a JWE', () => {
    const out = sanitizeCredentials(`log: ${JWE}${LINK_TOKEN} end`)

    expect(out).toBe('log: [REDACTED] end')
    expectNoneOf(out, [LINK_SIG, 'eyJzdWIi'])
  })

  it('redacts URL userinfo glued after a JWT', () => {
    // The JWT signature class takes `https`, so the userinfo pattern lost its scheme
    // and the password stayed.
    const out = sanitizeCredentials(`push ${JWS}${USERINFO_URL} failed`)

    expect(out).toBe('push [REDACTED]git.example/r.git failed')
    expectNoneOf(out, [PASSWORD, JWS_SIG])
  })

  it('redacts URL userinfo glued after a Slack token', () => {
    const out = sanitizeCredentials(`push ${SLACK}${USERINFO_URL} failed`)

    expect(out).toBe('push [REDACTED]git.example/r.git failed')
    expect(out).not.toContain(PASSWORD)
  })

  it('redacts a chain of three glued credentials', () => {
    // Each extension can reach a further glued start, so the scan runs to the span's
    // new end.
    const out = sanitizeCredentials(`x ${SLACK}${JWS}${USERINFO_URL} y`)

    expect(out).toBe('x [REDACTED]git.example/r.git y')
    expectNoneOf(out, [JWS_PAYLOAD, JWS_SIG, PASSWORD])
  })

  it('tests the JWT patterns once per segment run', () => {
    // Every `eyJ` in the run fails the JWT patterns only after reading to the run's
    // end, so testing each of them reads the run once per `eyJ`: quadratic.
    const text = `x eyJfake.a.${'eyJ'.repeat(5_000)} y`
    const { out, probes } = countProbes(text, 'eyJ')

    expect(out).toBe('x [REDACTED] y')
    // A fixed count however long the run: each JWT pattern's own scan tests the
    // first `eyJ` of each of the two segment runs, and the span's glued probes test
    // the nested run once per JWT pattern (the first pattern's scan stops at its
    // match, so it tests only one run itself).
    expect(probes).toBe(5)
  })

  it('scans a JWT pattern over a long run of starts once, outside any other span', () => {
    // Every pattern reads the original text, so the JWT patterns' own scan meets the
    // whole run even when another pattern's span already covers it.
    const text = `x SessionToken=''${'eyJ'.repeat(5_000)} y`
    const { out, probes, freeScans } = countProbes(text, 'eyJ')

    expect(out).toBe('x [REDACTED] y')
    expect(freeScans).toBe(0)
    expect(probes).toBeLessThanOrEqual(4)
  })

  it('scans the userinfo pattern over URL starts with no `@` without testing them', () => {
    const text = `x SessionToken=''${'https://h:p'.repeat(5_000)} y`
    const { out, probes, freeScans } = countProbes(text, 'https?')

    expect(out).toBe('x [REDACTED] y')
    expect(freeScans).toBe(0)
    expect(probes).toBe(0)
  })

  it('does not test a URL start that has no `@` after it', () => {
    // With no `@` left, the userinfo pattern fails only after reading to the end of
    // the text, so testing every scheme in the run reads it once per scheme.
    const text = `x aws_session_token=${'https://h:p'.repeat(5_000)} y`
    const { out, probes } = countProbes(text, 'https?')

    expect(out).toBe('x [REDACTED] y')
    expect(probes).toBe(0)
  })

  it('keeps a URL password redacted when a span inside it runs out of probes', () => {
    // The budget's fail-closed run takes the URL's `@`; the userinfo match is still
    // read from the original text, so the scheme and the password are redacted too.
    const text = `https://u:Sx9prefix${'xoxb-aaaaaaaaaa'.repeat(9)}@host.invalid y`
    const out = sanitizeCredentials(text)

    expect(out).toBe('[REDACTED] y')
    expect(out).not.toContain('Sx9prefix')
  })

  it('redacts the rest of the run once a span holds too many nested starts', () => {
    // Nested matches that extend nothing are counted; past the budget a start not
    // yet tested may still run past the span, so the run is redacted to its end.
    const text = `x xoxb-${'xoxb-aaaaaaaaaa'.repeat(8)}_tail y`

    expect(sanitizeCredentials(text)).toBe('x [REDACTED] y')
  })
})

describe('sanitizeCredentials: text with no glued credentials', () => {
  const CORPUS = [
    `log: ${SLACK} ${JWS} end`,
    `${AKIA}${JWS}`,
    `${AKIA}${AKIA}`,
    `aws_secret_access_key=${AKIA}xyz next`,
    `clone ${USERINFO_URL} and ${LINK_TOKEN} then ${JWE}`,
    `${PEM_HEADER} ${SLACK}`,
    'see https://example.com/docs and xoxo, the http header',
    'eyJsonSerializer.deserializeFromStringValue(x)',
    'obj.eyJsonReader.readValueFromInputStream(x) keyJson.get(raw)',
    'AKIA alone is a prefix, xoxb-short is too short',
    'plain prose with no credential in it at all.',
  ]

  it('sanitizes exactly as one replace per pattern does', () => {
    for (const text of CORPUS) {
      expect(sanitizeCredentials(text)).toBe(sequentialReplace(text))
    }
  })

  it('keeps two credentials written back to back without overlap as two placeholders', () => {
    // A fixed-length AKIA id takes none of the next credential's characters.
    expect(sanitizeCredentials(`${AKIA}${JWS}`)).toBe('[REDACTED][REDACTED]')
  })

  it('leaves ordinary text untouched', () => {
    for (const text of CORPUS.slice(6)) {
      expect(sanitizeCredentials(text)).toBe(text)
    }
  })
})
