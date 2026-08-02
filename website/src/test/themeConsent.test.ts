import { describe, it, expect, beforeEach, vi, afterEach } from 'vitest'
import {
  CONSENT_PREFIX,
  grantConsent,
  revokeConsent,
  getStoredConsent,
  hasConsent,
  hasAnyConsent,
} from '../utils/themeConsent'

describe('themeConsent util', () => {
  beforeEach(() => {
    localStorage.clear()
  })

  it('grantConsent persists under the shared prefix and getStoredConsent reads it back', () => {
    grantConsent('bikini-bottom', 'sha-abc')
    expect(localStorage.getItem(CONSENT_PREFIX + 'bikini-bottom')).toBe('sha-abc')
    expect(getStoredConsent('bikini-bottom')).toBe('sha-abc')
  })

  it('getStoredConsent returns null when nothing is stored', () => {
    expect(getStoredConsent('nope')).toBeNull()
  })

  it('grant/revoke are no-ops for an empty slug', () => {
    grantConsent('', 'sha-abc')
    expect(localStorage.getItem(CONSENT_PREFIX)).toBeNull()
    // revoke must not throw on empty slug
    expect(() => revokeConsent('')).not.toThrow()
  })

  describe('hasConsent (STRICT — used by the layer with the descriptor)', () => {
    it('true only when the stored token exactly equals the expected token', () => {
      grantConsent('bikini-bottom', 'sha-abc')
      expect(hasConsent('bikini-bottom', 'sha-abc')).toBe(true)
    })

    it('false when the stored token differs (persona changed → re-prompt)', () => {
      grantConsent('bikini-bottom', 'sha-OLD')
      expect(hasConsent('bikini-bottom', 'sha-NEW')).toBe(false)
    })

    it('false when nothing is stored', () => {
      expect(hasConsent('bikini-bottom', 'sha-abc')).toBe(false)
    })

    it('rejects a legacy "1" grant against any real token', () => {
      localStorage.setItem(CONSENT_PREFIX + 'bikini-bottom', '1')
      expect(hasConsent('bikini-bottom', 'consented-v2')).toBe(false)
      expect(hasConsent('bikini-bottom', 'sha-abc')).toBe(false)
    })
  })

  describe('hasAnyConsent (RELAXED — used by the chat wire flag)', () => {
    it('true when any non-legacy token is stored (sha256)', () => {
      grantConsent('bikini-bottom', 'sha-abc')
      expect(hasAnyConsent('bikini-bottom')).toBe(true)
    })

    it('true for the audio-only sentinel token', () => {
      grantConsent('bikini-bottom', 'consented-v2')
      expect(hasAnyConsent('bikini-bottom')).toBe(true)
    })

    it('false for a legacy "1" grant (must re-prompt exactly once)', () => {
      localStorage.setItem(CONSENT_PREFIX + 'bikini-bottom', '1')
      expect(hasAnyConsent('bikini-bottom')).toBe(false)
    })

    it('false for an empty-string token', () => {
      localStorage.setItem(CONSENT_PREFIX + 'bikini-bottom', '')
      expect(hasAnyConsent('bikini-bottom')).toBe(false)
    })

    it('false when nothing is stored', () => {
      expect(hasAnyConsent('bikini-bottom')).toBe(false)
    })
  })

  describe('revokeConsent', () => {
    it('removes a stored grant so both checks go false', () => {
      grantConsent('bikini-bottom', 'sha-abc')
      expect(hasAnyConsent('bikini-bottom')).toBe(true)
      revokeConsent('bikini-bottom')
      expect(getStoredConsent('bikini-bottom')).toBeNull()
      expect(hasConsent('bikini-bottom', 'sha-abc')).toBe(false)
      expect(hasAnyConsent('bikini-bottom')).toBe(false)
    })
  })
})

// ── Regression pin (content-binding) ──
// The chat wire transmits the RAW stored grant as `theme_consent_sha` (the
// sha256 the user granted); the backend verifies it against sha256 of the
// persona it reads. No boolean `theme_consent` is sent — gating is
// content-bound server-side, closing the reinstall-swap hole where a relaxed
// boolean would stay true after persona.md changed.
describe('sendChat theme_consent_sha wire token', () => {
  let fetchMock: ReturnType<typeof vi.fn>

  beforeEach(() => {
    localStorage.clear()
    fetchMock = vi.fn(() => Promise.resolve(new Response('{}', { status: 200 })))
    vi.stubGlobal('fetch', fetchMock)
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  async function sentBody(colorTheme: string): Promise<Record<string, unknown>> {
    const { api } = await import('../api/client')
    await api.sendChat('hello', 'slot-1', colorTheme)
    expect(fetchMock).toHaveBeenCalled()
    const init = fetchMock.mock.calls[0][1] as RequestInit
    return JSON.parse(init.body as string)
  }

  it('sends theme_consent_sha === the stored grant once consent is granted for the installed pack', async () => {
    // Layer writes a sha256-keyed grant on explicit consent…
    grantConsent('bikini-bottom', 'sha-abc')
    // …and the dropdown value for an installed pack is `custom-<slug>`.
    const body = await sentBody('custom-bikini-bottom')
    expect(body.color_theme).toBe('custom-bikini-bottom')
    expect(body.theme_consent_sha).toBe('sha-abc')
    // Legacy boolean is intentionally gone.
    expect('theme_consent' in body).toBe(false)
  })

  it('omits theme_consent_sha when no grant is stored', async () => {
    const body = await sentBody('custom-bikini-bottom')
    expect(body.color_theme).toBe('custom-bikini-bottom')
    expect('theme_consent_sha' in body).toBe(false)
  })

  it('omits theme_consent_sha for a legacy "1" grant (re-prompt semantics)', async () => {
    localStorage.setItem(CONSENT_PREFIX + 'bikini-bottom', '1')
    const body = await sentBody('custom-bikini-bottom')
    expect('theme_consent_sha' in body).toBe(false)
  })

  it('omits theme_consent_sha for an empty-string grant', async () => {
    localStorage.setItem(CONSENT_PREFIX + 'bikini-bottom', '')
    const body = await sentBody('custom-bikini-bottom')
    expect('theme_consent_sha' in body).toBe(false)
  })

  it('omits theme_consent_sha for a built-in (non-custom) theme, still passing color_theme', async () => {
    // Even if some grant is stored under the raw name, a built-in theme value
    // never carries the `custom-` prefix, so no token is transmitted.
    localStorage.setItem(CONSENT_PREFIX + 'emerald', 'sha-abc')
    const body = await sentBody('emerald')
    expect(body.color_theme).toBe('emerald')
    expect('theme_consent_sha' in body).toBe(false)
  })

  it('omits theme fields entirely when no colorTheme is supplied', async () => {
    const { api } = await import('../api/client')
    await api.sendChat('hello', 'slot-1')
    const init = fetchMock.mock.calls[0][1] as RequestInit
    const body = JSON.parse(init.body as string)
    expect('color_theme' in body).toBe(false)
    expect('theme_consent_sha' in body).toBe(false)
    expect('theme_consent' in body).toBe(false)
  })
})
