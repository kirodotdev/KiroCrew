import { describe, it, expect, beforeEach, vi, afterEach } from 'vitest'
import {
  CONSENT_PREFIX,
  EXPERIENCE_PREFIX,
  THEME_EXPERIENCE_CHANGED_EVENT,
  experiencePack,
  getExperienceChoice,
  keepColorsOnly,
  readExperience,
  revokeConsent,
  saveExperienceChoice,
  settleExperience,
  subscribeExperience,
  getStoredConsent,
  hasConsent,
  hasAnyConsent,
} from '../utils/themeConsent'

const SLUG = 'bikini-bottom'
const RECORD_KEY = EXPERIENCE_PREFIX + SLUG
const GRANT_KEY = CONSENT_PREFIX + SLUG
const FULL = experiencePack({
  overlays: ['bubbles'],
  hasAudio: true,
  hasPersona: true,
  personaInfo: { sha256: 'sha-abc', text: 'boo!' },
})

describe('themeConsent util', () => {
  beforeEach(() => {
    localStorage.clear()
  })

  describe('experiencePack', () => {
    it('reads the shipped parts and binds the persona token to its sha256', () => {
      expect(FULL).toEqual({ sounds: true, overlays: true, persona: true, personaToken: 'sha-abc', personaText: 'boo!' })
    })

    it('counts a topbar as the overlays part and has no persona token without a persona', () => {
      expect(experiencePack({ topbar: { dark: true }, hasAudio: true })).toEqual({
        sounds: true, overlays: true, persona: false, personaToken: null, personaText: undefined,
      })
    })
  })

  describe('the choice record', () => {
    it('stores one JSON record per slug and reads it back', () => {
      saveExperienceChoice(SLUG, { sounds: true, overlays: false, persona: false }, 'sha-abc')
      expect(JSON.parse(localStorage.getItem(RECORD_KEY) as string)).toEqual({ sounds: true, overlays: false, persona: false })
      expect(getExperienceChoice(SLUG)).toEqual({ sounds: true, overlays: false, persona: false })
      expect(getExperienceChoice('other-pack')).toBeNull()
    })

    it('ignores a record that is not a JSON object, and fields that are not booleans', () => {
      localStorage.setItem(RECORD_KEY, '1')
      expect(getExperienceChoice(SLUG)).toBeNull()
      localStorage.setItem(RECORD_KEY, '{"sounds":"yes","overlays":true}')
      expect(getExperienceChoice(SLUG)).toEqual({ overlays: true })
    })

    it('writes the grant only when persona is on', () => {
      saveExperienceChoice(SLUG, { sounds: true, overlays: true, persona: false }, 'sha-abc')
      expect(localStorage.getItem(GRANT_KEY)).toBeNull()
      saveExperienceChoice(SLUG, { sounds: false, overlays: false, persona: true }, 'sha-abc')
      expect(localStorage.getItem(GRANT_KEY)).toBe('sha-abc')
    })

    it('revokes the grant when persona turns off, and writes none without a token', () => {
      saveExperienceChoice(SLUG, { persona: true }, 'sha-abc')
      saveExperienceChoice(SLUG, { persona: false, sounds: true }, 'sha-abc')
      expect(localStorage.getItem(GRANT_KEY)).toBeNull()
      saveExperienceChoice(SLUG, { persona: true }, null)
      expect(localStorage.getItem(GRANT_KEY)).toBeNull()
    })

    it('keepColorsOnly turns every part off and revokes the grant of that slug only', () => {
      saveExperienceChoice(SLUG, { persona: true }, 'sha-abc')
      saveExperienceChoice('other-pack', { persona: true }, 'sha-other')
      keepColorsOnly(SLUG)
      expect(getExperienceChoice(SLUG)).toEqual({ sounds: false, overlays: false, persona: false })
      expect(localStorage.getItem(GRANT_KEY)).toBeNull()
      expect(getStoredConsent('other-pack')).toBe('sha-other')
    })

    it('fires the change event with the slug after the record and grant are written', () => {
      const seen: Array<{ slug: string; grant: string | null; record: unknown }> = []
      const onChange = (e: Event) => {
        const slug = (e as CustomEvent<{ slug: string }>).detail.slug
        seen.push({ slug, grant: getStoredConsent(slug), record: getExperienceChoice(slug) })
      }
      window.addEventListener(THEME_EXPERIENCE_CHANGED_EVENT, onChange)
      try {
        saveExperienceChoice(SLUG, { persona: true }, 'sha-abc')
        keepColorsOnly(SLUG)
      } finally {
        window.removeEventListener(THEME_EXPERIENCE_CHANGED_EVENT, onChange)
      }
      expect(seen).toEqual([
        { slug: SLUG, grant: 'sha-abc', record: { sounds: false, overlays: false, persona: true } },
        { slug: SLUG, grant: null, record: { sounds: false, overlays: false, persona: false } },
      ])
    })
  })

  describe('when localStorage refuses the write', () => {
    const quota = () => new DOMException('full', 'QuotaExceededError')
    const refuse = (keys: string[], err: () => Error = quota) => {
      const real = Storage.prototype.setItem
      return vi.spyOn(Storage.prototype, 'setItem').mockImplementation(function (this: Storage, key: string, value: string) {
        if (keys.includes(key)) throw err()
        return real.call(this, key, value)
      })
    }

    afterEach(() => {
      vi.restoreAllMocks()
      // A stored save drops any choice kept for this tab, so it cannot leak into later cases.
      saveExperienceChoice(SLUG, {}, null)
    })

    it.each([
      ['a quota error', quota],
      ['storage being disabled', () => new Error('SecurityError')],
    ])('keeps "Keep colors only" for this tab on %s, so the dialog does not ask again', (_label, err) => {
      refuse([RECORD_KEY], err)
      const onChange = vi.fn()
      window.addEventListener(THEME_EXPERIENCE_CHANGED_EVENT, onChange)
      try {
        keepColorsOnly(SLUG)
      } finally {
        window.removeEventListener(THEME_EXPERIENCE_CHANGED_EVENT, onChange)
      }
      expect(localStorage.getItem(RECORD_KEY)).toBeNull()
      expect(onChange).toHaveBeenCalledTimes(1)
      expect(readExperience(SLUG, FULL)).toEqual({ answered: true, sounds: false, overlays: false, persona: false, ask: [] })
      expect(saveExperienceChoice(SLUG, { sounds: true }, null)).toBe(false)
    })

    it('keeps "Keep colors only" for this tab when reading localStorage itself throws', () => {
      const desc = Object.getOwnPropertyDescriptor(window, 'localStorage')
      Object.defineProperty(window, 'localStorage', {
        configurable: true,
        get() {
          throw new DOMException('denied', 'SecurityError')
        },
      })
      try {
        expect(() => keepColorsOnly(SLUG)).not.toThrow()
        expect(readExperience(SLUG, FULL)).toEqual({ answered: true, sounds: false, overlays: false, persona: false, ask: [] })
      } finally {
        if (desc) Object.defineProperty(window, 'localStorage', desc)
      }
    })

    it('drops the tab-local choice when another tab stores an answer', () => {
      const spy = refuse([RECORD_KEY])
      keepColorsOnly(SLUG)
      spy.mockRestore()
      const onChange = vi.fn()
      const unsubscribe = subscribeExperience(SLUG, onChange)
      try {
        localStorage.setItem(RECORD_KEY, JSON.stringify({ sounds: false, overlays: false, persona: true }))
        localStorage.setItem(GRANT_KEY, 'sha-abc')
        window.dispatchEvent(new StorageEvent('storage', { key: GRANT_KEY }))
      } finally {
        unsubscribe()
      }
      expect(onChange).toHaveBeenCalledTimes(1)
      expect(getExperienceChoice(SLUG)).toEqual({ sounds: false, overlays: false, persona: true })
      expect(readExperience(SLUG, FULL)).toMatchObject({ persona: true, ask: [] })
    })

    it('writes no grant when the choice cannot be stored, so a reload does not read all-on', () => {
      refuse([RECORD_KEY])
      expect(saveExperienceChoice(SLUG, { persona: true }, 'sha-abc')).toBe(false)
      expect(localStorage.getItem(GRANT_KEY)).toBeNull()
      expect(getExperienceChoice(SLUG)).toEqual({ sounds: false, overlays: false, persona: false })
      // A reload reads storage only: no record and no grant, so it asks again
      // instead of reading an orphan grant as all-on.
      expect(localStorage.getItem(RECORD_KEY)).toBeNull()
    })

    it('saves the persona as off with no grant when the grant cannot be written', () => {
      refuse([GRANT_KEY])
      expect(saveExperienceChoice(SLUG, { sounds: true, persona: true }, 'sha-abc')).toBe(false)
      expect(localStorage.getItem(GRANT_KEY)).toBeNull()
      expect(getExperienceChoice(SLUG)).toEqual({ sounds: true, overlays: false, persona: false })
      expect(readExperience(SLUG, FULL)).toMatchObject({ answered: true, sounds: true, persona: false, ask: [] })
    })

    it('drops the tab-local choice once a later save is stored', () => {
      const spy = refuse([RECORD_KEY])
      keepColorsOnly(SLUG)
      spy.mockRestore()
      expect(saveExperienceChoice(SLUG, { sounds: true, persona: true }, 'sha-abc')).toBe(true)
      expect(JSON.parse(localStorage.getItem(RECORD_KEY) as string)).toEqual({ sounds: true, overlays: false, persona: true })
      localStorage.setItem(RECORD_KEY, JSON.stringify({ sounds: false, overlays: true, persona: false }))
      // Storage is read again: nothing kept for the tab shadows it.
      expect(getExperienceChoice(SLUG)).toEqual({ sounds: false, overlays: true, persona: false })
    })
  })

  describe('readExperience', () => {
    it('asks about every shipped part when no record is stored', () => {
      expect(readExperience(SLUG, FULL)).toEqual({
        answered: false, sounds: false, overlays: false, persona: false, ask: ['sounds', 'overlays', 'persona'],
      })
    })

    it('shows the overlays of an overlays-only pack without asking', () => {
      const pack = experiencePack({ overlays: ['bubbles'] })
      expect(readExperience(SLUG, pack)).toMatchObject({ answered: false, overlays: true, ask: [] })
    })

    it('turns on each part the record lists, persona only with a matching grant', () => {
      saveExperienceChoice(SLUG, { sounds: true, overlays: false, persona: true }, 'sha-abc')
      expect(readExperience(SLUG, FULL)).toEqual({ answered: true, sounds: true, overlays: false, persona: true, ask: [] })
    })

    it('asks again about persona only when the persona sha changed', () => {
      saveExperienceChoice(SLUG, { sounds: true, overlays: true, persona: true }, 'sha-OLD')
      expect(readExperience(SLUG, FULL)).toEqual({ answered: true, sounds: true, overlays: true, persona: false, ask: ['persona'] })
    })

    it('keeps a part the record does not list off', () => {
      localStorage.setItem(RECORD_KEY, JSON.stringify({ overlays: true }))
      expect(readExperience(SLUG, FULL)).toEqual({ answered: true, sounds: false, overlays: true, persona: false, ask: [] })
    })
  })

  describe('settleExperience (migration and stale grants)', () => {
    it('turns an old valid grant into an all-on record and keeps the grant', () => {
      localStorage.setItem(GRANT_KEY, 'sha-abc')
      expect(readExperience(SLUG, FULL)).toMatchObject({ answered: true, sounds: true, overlays: true, persona: true })
      settleExperience(SLUG, FULL)
      expect(getExperienceChoice(SLUG)).toEqual({ sounds: true, overlays: true, persona: true })
      expect(localStorage.getItem(GRANT_KEY)).toBe('sha-abc')
    })

    it('migrates an old sentinel grant of a pack without a persona and drops the grant', () => {
      const audioOnly = experiencePack({ hasAudio: true })
      localStorage.setItem(GRANT_KEY, 'consented-v2')
      settleExperience(SLUG, audioOnly)
      expect(getExperienceChoice(SLUG)).toEqual({ sounds: true, overlays: false, persona: false })
      expect(localStorage.getItem(GRANT_KEY)).toBeNull()
    })

    it('revokes a grant whose sha no longer matches, and a legacy "1" grant', () => {
      saveExperienceChoice(SLUG, { persona: true }, 'sha-OLD')
      settleExperience(SLUG, FULL)
      expect(localStorage.getItem(GRANT_KEY)).toBeNull()
      expect(getExperienceChoice(SLUG)).toEqual({ sounds: false, overlays: false, persona: true })
      localStorage.clear()
      localStorage.setItem(GRANT_KEY, '1')
      settleExperience(SLUG, FULL)
      expect(localStorage.getItem(GRANT_KEY)).toBeNull()
      expect(getExperienceChoice(SLUG)).toBeNull()
    })
  })

  it('getStoredConsent returns null when nothing is stored', () => {
    expect(getStoredConsent('nope')).toBeNull()
  })

  it('writes are no-ops for an empty slug', () => {
    saveExperienceChoice('', { persona: true }, 'sha-abc')
    expect(localStorage.getItem(CONSENT_PREFIX)).toBeNull()
    expect(localStorage.getItem(EXPERIENCE_PREFIX)).toBeNull()
    expect(() => revokeConsent('')).not.toThrow()
  })

  describe('hasConsent (STRICT — used by the layer with the descriptor)', () => {
    it('true only when the stored token exactly equals the expected token', () => {
      saveExperienceChoice(SLUG, { persona: true }, 'sha-abc')
      expect(hasConsent(SLUG, 'sha-abc')).toBe(true)
    })

    it('false when the stored token differs (persona changed → re-prompt)', () => {
      saveExperienceChoice(SLUG, { persona: true }, 'sha-OLD')
      expect(hasConsent(SLUG, 'sha-NEW')).toBe(false)
    })

    it('false when nothing is stored', () => {
      expect(hasConsent(SLUG, 'sha-abc')).toBe(false)
    })

    it('rejects a legacy "1" grant against any real token', () => {
      localStorage.setItem(GRANT_KEY, '1')
      expect(hasConsent(SLUG, 'consented-v2')).toBe(false)
      expect(hasConsent(SLUG, 'sha-abc')).toBe(false)
    })
  })

  describe('hasAnyConsent (RELAXED)', () => {
    it('true when any non-legacy token is stored (sha256)', () => {
      saveExperienceChoice(SLUG, { persona: true }, 'sha-abc')
      expect(hasAnyConsent(SLUG)).toBe(true)
    })

    it('false for a legacy "1" grant (must re-prompt exactly once)', () => {
      localStorage.setItem(GRANT_KEY, '1')
      expect(hasAnyConsent(SLUG)).toBe(false)
    })

    it('false for an empty-string token', () => {
      localStorage.setItem(GRANT_KEY, '')
      expect(hasAnyConsent(SLUG)).toBe(false)
    })

    it('false when nothing is stored', () => {
      expect(hasAnyConsent(SLUG)).toBe(false)
    })
  })

  describe('revokeConsent', () => {
    it('removes a stored grant so both checks go false', () => {
      saveExperienceChoice(SLUG, { persona: true }, 'sha-abc')
      expect(hasAnyConsent(SLUG)).toBe(true)
      revokeConsent(SLUG)
      expect(getStoredConsent(SLUG)).toBeNull()
      expect(hasConsent(SLUG, 'sha-abc')).toBe(false)
      expect(hasAnyConsent(SLUG)).toBe(false)
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
    // The layer writes a sha256-keyed grant when the persona part is on…
    saveExperienceChoice('bikini-bottom', { persona: true }, 'sha-abc')
    // …and the dropdown value for an installed pack is `custom-<slug>`.
    const body = await sentBody('custom-bikini-bottom')
    expect(body.color_theme).toBe('custom-bikini-bottom')
    expect(body.theme_consent_sha).toBe('sha-abc')
    // Legacy boolean is intentionally gone.
    expect('theme_consent' in body).toBe(false)
  })

  it('omits theme_consent_sha after "Keep colors only" on a previously granted experience', async () => {
    saveExperienceChoice('bikini-bottom', { persona: true }, 'sha-abc')
    keepColorsOnly('bikini-bottom')
    const body = await sentBody('custom-bikini-bottom')
    expect(body.color_theme).toBe('custom-bikini-bottom')
    expect('theme_consent_sha' in body).toBe(false)
  })

  it('omits theme_consent_sha while sounds and overlays are on and persona is off', async () => {
    saveExperienceChoice('bikini-bottom', { sounds: true, overlays: true, persona: false }, 'sha-abc')
    const body = await sentBody('custom-bikini-bottom')
    expect('theme_consent_sha' in body).toBe(false)
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
