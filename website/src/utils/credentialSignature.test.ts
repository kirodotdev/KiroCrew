/**
 * Signed-link signatures are redacted on display, but the word `signature` in
 * ordinary prose or source code is not. The frontend mirrors the backend's
 * `URL_SIGNING_VALUE`: the AWS-specific names (`X-Amz-Signature`,
 * `X-Amz-Security-Token`) are taken in any position, while a bare `Signature` is
 * taken only as a URL query parameter or as a 64-hex SigV4 value. The AWS access
 * key ID is Identifier tier and stays visible. These cases pin the narrowing so a
 * future edit cannot widen it back into mangling displayed scripts.
 */
import { describe, expect, it } from 'vitest'

import { sanitizeCredentials } from './sanitize'
import { redactSecrets } from './errorReport'

const KEY_ID = 'AKIA' + 'IOSFODNN7EXAMPLE'
const SIG64 = '0123456789abcdef'.repeat(4)

describe('signing-value redaction leaves ordinary text alone', () => {
  const kept = [
    'def verify(signature: bytes) -> None: ...',
    'signature = inspect.signature(callback)',
    'const signature: string = sign(body)',
    '<p>Signature: Jane Doe</p>',
    `the digest sha256=${SIG64} is fine`,
  ]
  for (const text of kept) {
    it(`keeps: ${text.slice(0, 32)}`, () => {
      expect(sanitizeCredentials(text)).toBe(text)
      expect(redactSecrets(text)).toBe(text)
    })
  }
})

describe('signing-value redaction removes a signed link signature', () => {
  const presigned =
    `https://b.s3.amazonaws.com/o?X-Amz-Algorithm=AWS4-HMAC-SHA256` +
    `&X-Amz-Credential=${KEY_ID}%2Fx&X-Amz-SignedHeaders=host&X-Amz-Signature=${SIG64}`

  it('drops the signature but keeps the key ID', () => {
    const out = sanitizeCredentials(presigned)
    expect(out).not.toContain(SIG64)
    expect(out).toContain(KEY_ID)
  })

  it('errorReport keeps the signature parameter name and drops its value', () => {
    // errorReport's own generic `credential=`-label matcher masks the
    // X-Amz-Credential value independently; this case pins only the signature
    // branch — the name is preserved and the signing value is gone.
    const out = redactSecrets(presigned)
    expect(out).not.toContain(SIG64)
    expect(out).toContain('X-Amz-Signature')
  })

  it('drops a bare Signature query parameter', () => {
    expect(sanitizeCredentials('?Signature=abcdefg123')).not.toContain('abcdefg123')
  })

  it('drops a session token', () => {
    const tok = 'IQoJb3Jp' + 'Q2hp'.repeat(12)
    expect(sanitizeCredentials(`X-Amz-Security-Token=${tok}`)).not.toContain(tok)
  })

  it('drops a 64-hex Signature value outside a query', () => {
    expect(sanitizeCredentials(`Signature=${SIG64}`)).not.toContain(SIG64)
  })

  it('drops a signature under a percent-encoded name in both scrubbers', () => {
    const sigV2 = `https://b.s3.amazonaws.com/o?AWSAccessKeyId=${KEY_ID}&Expires=1&S%69gnature=abcDEF123%2Bx`
    for (const out of [sanitizeCredentials(sigV2), redactSecrets(sigV2)]) {
      expect(out).not.toContain('abcDEF123')
      expect(out).toContain('S%69gnature=')
    }
    const token = `X-Amz-S%65curity-Token=${'Q2hp'.repeat(12)}`
    expect(sanitizeCredentials(token)).not.toContain('Q2hpQ2hp')
    expect(sanitizeCredentials('?p%61ge=2&q=x')).toBe('?p%61ge=2&q=x')
  })
})

describe('a signing value behind a percent-encoded separator is removed', () => {
  const token = 'IQoJb3Jp' + 'Z2luX2VjEExampleSessionTokenValue0123456789'
  for (const name of ['X-Amz-Security-Token', 'X-Amz-Signature', 'Signature']) {
    it(`drops ${name}%3D<value>`, () => {
      const text = `error: https://b/o%3F${name}%3D${token}%26next%3D1`
      expect(sanitizeCredentials(text)).not.toContain(token)
      expect(redactSecrets(text)).not.toContain(token)
    })
  }
})

describe('the shared signing scanner reads every signing name', () => {
  const value = 'Xk9mP2qR7sT4vW1yZ3bN6cF8gH0jL5dA9eQ2rU7iO4p'
  it('drops an Azure SAS sig in an error report', () => {
    const text = `GET https://a.blob.core.windows.net/c/f?sv=2021&sig=${value} failed`
    expect(redactSecrets(text)).not.toContain(value)
    expect(sanitizeCredentials(text)).not.toContain(value)
  })
  it('keeps a sig keyword outside a URL query', () => {
    const text = 'def f(sig=None): pass'
    expect(redactSecrets(text)).toBe(text)
  })
  it('reads a signing value after an ordinary encoded parameter', () => {
    const text = `https://b/o%3Ffoo%3Dbar%26X-Amz-Security-Token%3D${value}%26n%3D1`
    expect(redactSecrets(text)).not.toContain(value)
  })
})

describe('a link inside another parameter value', () => {
  it('reads the inner link as its own parameters', () => {
    const text = `url=https://b.s3.amazonaws.com/o?X-Amz-S%69gnature=${SIG64}&x=1`
    for (const out of [redactSecrets(text), sanitizeCredentials(text)]) {
      expect(out).not.toContain(SIG64)
      expect(out).toContain('url=https://b.s3.amazonaws.com/o?')
    }
  })
})

describe('a signing value already redacted', () => {
  it('is not redacted a second time', () => {
    const text = `https://b.s3.amazonaws.com/o?X-Amz-Signature=${SIG64}&x=1`
    expect(redactSecrets(text)).toBe('https://b.s3.amazonaws.com/o?X-Amz-Signature=[redacted]&x=1')
    expect(sanitizeCredentials(text)).toBe(
      'https://b.s3.amazonaws.com/o?X-Amz-Signature=[REDACTED]&x=1',
    )
  })
})
