/** `sanitizeCredentials` on two credentials written back to back.
 *
 *  The patterns run one after another, so when the first credential's body class
 *  also takes the second one's leading characters, the second pattern no longer
 *  matches what the first left and most of the second credential stays on screen.
 *  Every value here is synthetic.
 */
import { describe, it, expect } from 'vitest'
import { CRED_PATTERNS, sanitizeCredentials } from '../utils/sanitize'

// Every credential here is assembled at run time from fragments, so no source line
// holds a whole credential-shaped literal for the content scanners to flag.
const EYJ = ['ey', 'J'].join('')
const XOXB = ['xox', 'b-'].join('')
const SLACK = `${XOXB}1234567890-abcdefghij`
const AKIA = ['AKIA', 'IOSFODNN7EXAMPLE'].join('') // AWS's documented example key id
const ASIA = ['ASIA', 'IOSFODNN7EXAMPLE'].join('')
const PEM_HEADER = ['-----BEGIN', 'KEY-----'].join(' RSA PRIVATE ')
const OPENSSH_HEADER = ['BEGIN', 'KEY'].join(' OPENSSH PRIVATE ')
const JWS_HEADER = `${EYJ}hbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9`
const JWS_PAYLOAD = `${EYJ}zdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvaG4gRG9lIn0`
const JWS_SIG = 'dQw4w9WgXcQdQw4w9WgXcQdQw4w9WgXcQdQw4w9WgXc'
const JWS = `${JWS_HEADER}.${JWS_PAYLOAD}.${JWS_SIG}`
// A compact JWE: four dots, the most the JWT pattern takes, so a token glued after
// it is cut at its first dot.
const JWE = [
  `${EYJ}hbGciOiJkaXIiLCJlbmMiOiJBMjU2R0NNIn0`, '', '48V1_ALb6US04U3b',
  '5eym8TW_c8SuK0ltJ3rpYIzOeDQz7TALvtu6UG9oMo4vpzs9tX_EFShS8iB7j6ji', 'XFBoMYUZodetZdvTiFvSkQ',
].join('.')
const LINK_PAYLOAD =
  `${EYJ}zdWIiOiJsb2NhbC1hcHAiLCJleHAiOjE3ODU0MTc2MDYsInNlc3Npb25fZXhwIjoxNzg1NDg5MzA2` +
  'LCJpYXQiOjE3ODU0MTczMDYsIm5vbmNlIjoiOTM5YzE3MGQ5ZjBiNmEyMiIsImdlbiI6MH0'
const LINK_SIG = 'gVhM4aKLA8dyFH-oZlQx6SpYSNPkXA07kpDhWd6UhZI'
const LINK_TOKEN = `${LINK_PAYLOAD}.${LINK_SIG}`
const PASSWORD = 'Sx9fakePassw0rdValue'
const USERINFO_URL = `https://deploy:${PASSWORD}@git.example/r.git`

/** One `replace` per pattern, with no glued-credential scan: text with no glued
 *  credentials must come out of this and `sanitizeCredentials` exactly the same. */
function sequentialReplace(text: string): string {
  return CRED_PATTERNS.reduce((out, re) => out.replace(re, '[REDACTED]'), text)
}

it('has a reader for every pattern in CRED_PATTERNS', () => {
  // `CredentialScan` mirrors these nine patterns one for one. A pattern added
  // there needs a reader and a case in KINDS before this count moves.
  expect(CRED_PATTERNS).toHaveLength(9)
})

const KEY_VALUE = 'Sx9fakeKeyValue'
const KEY_ASSIGNMENT = `aws_secret_access_key=${KEY_VALUE}`
const SLACK_BODY = SLACK.slice(5)
const AKIA_BODY = AKIA.slice(4)

/** One credential of each kind, with the parts of it that must never stay on screen. */
const KINDS: { name: string, text: string, secret: string[] }[] = [
  { name: 'AWS key id', text: AKIA, secret: [AKIA_BODY] },
  { name: 'key assignment', text: KEY_ASSIGNMENT, secret: [KEY_VALUE] },
  { name: 'private key header', text: PEM_HEADER, secret: ['PRIVATE'] },
  { name: 'Slack token', text: SLACK, secret: [SLACK_BODY] },
  { name: 'JWS', text: JWS, secret: [JWS_PAYLOAD, JWS_SIG] },
  { name: 'JWE', text: JWE, secret: ['5eym8TW', 'XFBoMYUZodetZdvTiFvSkQ'] },
  { name: 'link token', text: LINK_TOKEN, secret: [LINK_SIG, 'eyJzdWIi'] },
  { name: 'URL userinfo', text: USERINFO_URL, secret: [PASSWORD] },
]

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

  for (const a of KINDS) {
    for (const b of KINDS) {
      it(`redacts a ${b.name} glued after a ${a.name}`, () => {
        const out = sanitizeCredentials(`log: ${a.text}${b.text} end`)

        expect(out).toContain('[REDACTED]')
        expect(out.startsWith('log: ')).toBe(true)
        expect(out.endsWith(' end')).toBe(true)
        // A link token ends where its 43-character signature does, and a glued
        // character from the next credential makes that signature longer, so a link
        // token followed by anything is not one, here or in the backend.
        expectNoneOf(out, a.name === 'link token' ? b.secret : [...a.secret, ...b.secret])
      })
    }
  }

  it('redacts a URL password whose span holds many nested credentials, up to its `@`', () => {
    // Every nested Slack token ends inside the userinfo span, so the host after the
    // `@` is not a credential and stays.
    const text = `https://u:Sx9prefix${`${XOXB}aaaaaaaaaa`.repeat(9)}@host.invalid y`

    expect(sanitizeCredentials(text)).toBe('[REDACTED]host.invalid y')
  })

  it('redacts a run of nested Slack tokens only up to where the body class ends', () => {
    // `_` is outside the Slack body class, so the text from it on is not a credential.
    const text = `x ${XOXB}${`${XOXB}aaaaaaaaaa`.repeat(8)}_tail y`

    expect(sanitizeCredentials(text)).toBe('x [REDACTED]_tail y')
  })

  it('reads a long run of starts in time linear in its length', () => {
    // Each of these reads to the end of the run from every start in it when a reader
    // re-reads a run it has already read; at this length that does not finish within
    // the test timeout.
    const n = 40_000
    const texts = [
      `x ${JWS_HEADER}.a.${'eyJ'.repeat(n)} y`,
      `x SessionToken=''${'eyJ'.repeat(n)} y`,
      `x SessionToken=''${'https://h:p'.repeat(n)} y`,
      `x aws_session_token=${'https://h:p'.repeat(n)} y`,
      `x ${XOXB.repeat(n)}aaaaaaaaaa y`,
      `x ${'SessionToken='.repeat(n)}v y`,
      `x ${'AKIA'.repeat(n)} y`,
    ]
    for (const text of texts) expect(sanitizeCredentials(text)).toBe('x [REDACTED] y')
    // No `@` anywhere: every scheme in the run fails, and none of them reads to the end.
    const noAt = `x ${'https://h:p'.repeat(n)} y`
    expect(sanitizeCredentials(noAt)).toBe(noAt)
  })
})

describe('sanitizeCredentials: each credential kind on its own', () => {
  it.each([
    ['an AWS key id', `id ${AKIA} ok`, 'id [REDACTED] ok'],
    ['a session key id', `id ${ASIA} ok`, 'id [REDACTED] ok'],
    ['a secret key assignment', `k ${KEY_ASSIGNMENT} ok`, 'k [REDACTED] ok'],
    ['a key assignment in upper case', 'k AWS_SECRET_ACCESS_KEY = abc ok', 'k [REDACTED] ok'],
    ['a session token assignment', 'k SessionToken: abc ok', 'k [REDACTED] ok'],
    ['an access key id assignment', 'k aws_access_key_id=abc ok', 'k [REDACTED] ok'],
    ['a private key header', `${PEM_HEADER}\nbody`, '-----[REDACTED]-----\nbody'],
    ['an OpenSSH key header', OPENSSH_HEADER, '[REDACTED]'],
    ['a Slack token', `t ${SLACK} ok`, 't [REDACTED] ok'],
    ['a Slack token with the shortest body', `t ${XOXB}1234567890 ok`, 't [REDACTED] ok'],
    ['a JWT with empty later segments', `t ${JWS_HEADER}.. ok`, 't [REDACTED] ok'],
    ['a link token with the shortest payload', `t ${EYJ}${'a'.repeat(96)}.${'b'.repeat(43)} ok`, 't [REDACTED] ok'],
    ['a JWS', `t ${JWS} ok`, 't [REDACTED] ok'],
    ['a JWE with an empty key segment', `t ${JWE} ok`, 't [REDACTED] ok'],
    ['a link token', `t ${LINK_TOKEN} ok`, 't [REDACTED] ok'],
    ['URL userinfo', `t ${USERINFO_URL} ok`, 't [REDACTED]git.example/r.git ok'],
    ['plain-http URL userinfo', `http://u:${PASSWORD}@h ok`, '[REDACTED]h ok'],
  ])('redacts %s', (_name, text, expected) => {
    expect(sanitizeCredentials(text)).toBe(expected)
    expect(sanitizeCredentials(text)).toBe(sequentialReplace(text))
  })
})

describe('sanitizeCredentials: text that only starts like a credential', () => {
  const LOOKALIKES = [
    'AKIA1234 is too short, and so is ASIAABC',
    'akiaiosfodnn7example is lower case',
    'BEGIN RSA PUBLIC KEY and BEGIN  RSA PRIVATE KEY',
    'xoxz-1234567890abc and xoxb-123456789 and xoxb_1234567890',
    'eyJ alone, and eyJabc.def with one dot',
    'SessionToken is unset',
    'the value after SessionToken= ',
    'aws_session_token:',
    'http://host:8080/path',
    'https://user@host',
    'https://:p@h',
    'https://u:@h',
    'httpx://u:p@h',
  ]

  it('leaves it untouched', () => {
    for (const text of LOOKALIKES) {
      expect(sanitizeCredentials(text)).toBe(text)
      expect(sequentialReplace(text)).toBe(text)
    }
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
