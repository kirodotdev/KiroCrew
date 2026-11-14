import { beforeEach, describe, expect, it, vi } from 'vitest'
import { policyAdmitsFrame, refusedDeclaredOrigin, reloadIfFrameAdmitted } from './appFrameSources'

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
})

describe('reloadIfFrameAdmitted', () => {
  beforeEach(() => sessionStorage.clear())

  it('does not reload when the served policy still refuses the origin (untrusted app)', async () => {
    const reload = vi.fn()
    for (const now of [1_000_000, 1_100_000, 1_200_000]) {
      expect(await reloadIfFrameAdmitted('player', ORIGIN, { policy: async () => refusing, reload, now })).toBe(false)
    }
    expect(reload).not.toHaveBeenCalled()
  })

  it('reloads when the served policy now admits it, at most once per 30 s', async () => {
    const reload = vi.fn()
    const policy = async () => admitting
    expect(await reloadIfFrameAdmitted('player', ORIGIN, { policy, reload, now: 1_000_000 })).toBe(true)
    expect(await reloadIfFrameAdmitted('player', ORIGIN, { policy, reload, now: 1_010_000 })).toBe(false)
    expect(await reloadIfFrameAdmitted('player', ORIGIN, { policy, reload, now: 1_031_000 })).toBe(true)
    expect(reload).toHaveBeenCalledTimes(2)
  })

  it('does not reload when the policy cannot be read', async () => {
    const reload = vi.fn()
    const policy = async (): Promise<string | null> => { throw new Error('offline') }
    expect(await reloadIfFrameAdmitted('player', ORIGIN, { policy, reload, now: 1 })).toBe(false)
    expect(reload).not.toHaveBeenCalled()
  })
})
