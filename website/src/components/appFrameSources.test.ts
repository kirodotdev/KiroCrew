import { describe, expect, it } from 'vitest'
import { policyAdmitsFrame, refusedDeclaredOrigin } from './appFrameSources'

const declared = ['https://video.example.com']
const ORIGIN = 'https://video.example.com'
const admitting = `default-src 'self'; frame-src 'self' blob: ${ORIGIN}; object-src 'none'`
const refusing = "default-src 'self'; frame-src 'self' blob:; object-src 'none'"

describe('refusedDeclaredOrigin', () => {
  it('returns the declared origin a frame-src refusal names', () => {
    expect(refusedDeclaredOrigin(
      { effectiveDirective: 'frame-src', blockedURI: 'https://video.example.com/embed/x?y=1', originalPolicy: refusing },
      declared,
    )).toBe(ORIGIN)
  })

  it('ignores a refusal by a policy that already admits the origin (a redirect to an undeclared host)', () => {
    expect(refusedDeclaredOrigin(
      { effectiveDirective: 'frame-src', blockedURI: 'https://video.example.com/embed', originalPolicy: admitting },
      declared,
    )).toBeNull()
  })

  it.each([
    ['another directive', { effectiveDirective: 'img-src', blockedURI: 'https://video.example.com/a.png', originalPolicy: refusing }],
    ['an undeclared origin', { effectiveDirective: 'frame-src', blockedURI: 'https://other.example.com/', originalPolicy: refusing }],
    ['a different port', { effectiveDirective: 'frame-src', blockedURI: 'https://video.example.com:8443/', originalPolicy: refusing }],
    ['a non-URL value', { effectiveDirective: 'frame-src', blockedURI: 'inline', originalPolicy: refusing }],
  ])('ignores %s', (_label, e) => {
    expect(refusedDeclaredOrigin(e, declared)).toBeNull()
  })
})

describe('policyAdmitsFrame', () => {
  it('reads frame-src only, exact origin', () => {
    expect(policyAdmitsFrame(admitting, ORIGIN)).toBe(true)
    expect(policyAdmitsFrame(refusing, ORIGIN)).toBe(false)
    expect(policyAdmitsFrame(`img-src ${ORIGIN}; frame-src 'self'`, ORIGIN)).toBe(false)
    expect(policyAdmitsFrame(`frame-src https://video.example.com.evil.test`, ORIGIN)).toBe(false)
    expect(policyAdmitsFrame(null, ORIGIN)).toBe(false)
  })

  it('splits sources on runs of spaces and tabs', () => {
    expect(policyAdmitsFrame(`frame-src  'self'\t ${ORIGIN}  `, ORIGIN)).toBe(true)
    expect(policyAdmitsFrame(`frame-src\t${ORIGIN}`, ORIGIN)).toBe(true)
    expect(policyAdmitsFrame(`frame-src  'self'\tblob:`, ORIGIN)).toBe(false)
  })
})
