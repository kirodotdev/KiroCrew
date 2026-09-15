import { describe, it, expect, afterEach, vi } from 'vitest'

import {
  detectBrowserLanguage,
  resolveLanguage,
  readStoredLanguage,
  LANG_STORAGE_KEY,
} from './detect'

/** Replace navigator.languages for one assertion. */
function withLanguages(tags: string[], fn: () => void) {
  const spy = vi.spyOn(navigator, 'languages', 'get').mockReturnValue(tags)
  try {
    fn()
  } finally {
    spy.mockRestore()
  }
}

afterEach(() => {
  localStorage.clear()
})

describe('detectBrowserLanguage', () => {
  it('matches an exact supported tag', () => {
    withLanguages(['zh-CN', 'en'], () => expect(detectBrowserLanguage()).toBe('zh-CN'))
  })

  it('matches case-insensitively (browsers may report zh-cn)', () => {
    withLanguages(['zh-cn'], () => expect(detectBrowserLanguage()).toBe('zh-CN'))
  })

  it('falls back to a primary-subtag match', () => {
    // A zh-preferring browser must get Chinese, not English, even when the
    // exact regional tag isn't one we ship.
    withLanguages(['zh'], () => expect(detectBrowserLanguage()).toBe('zh-CN'))
    withLanguages(['zh-Hans'], () => expect(detectBrowserLanguage()).toBe('zh-CN'))
    withLanguages(['zh-TW'], () => expect(detectBrowserLanguage()).toBe('zh-CN'))
  })

  it('honours preference order', () => {
    withLanguages(['en-GB', 'zh-CN'], () => expect(detectBrowserLanguage()).toBe('en'))
    withLanguages(['zh-CN', 'en-GB'], () => expect(detectBrowserLanguage()).toBe('zh-CN'))
  })

  it('returns null when nothing matches', () => {
    // `tlh` and the private-use `qaa` range are deliberately not product locales,
    // so adding a real-world language cannot silently invert this assertion.
    withLanguages(['tlh-US', 'qaa'], () => expect(detectBrowserLanguage()).toBeNull())
  })

  it('ignores blank tags', () => {
    withLanguages(['', 'zh-CN'], () => expect(detectBrowserLanguage()).toBe('zh-CN'))
  })
})

describe('resolveLanguage', () => {
  it('prefers an explicit stored choice over the browser', () => {
    // The key anti-regression: a user who picks English on a Chinese machine
    // must not be re-detected back to Chinese on the next load.
    withLanguages(['zh-CN'], () => expect(resolveLanguage('en')).toBe('en'))
  })

  it('detects when the stored value is the auto sentinel', () => {
    withLanguages(['zh-CN'], () => expect(resolveLanguage('')).toBe('zh-CN'))
  })

  it('detects when there is no stored value', () => {
    withLanguages(['zh-CN'], () => {
      expect(resolveLanguage(null)).toBe('zh-CN')
      expect(resolveLanguage(undefined)).toBe('zh-CN')
    })
  })

  it('ignores an unsupported stored value and falls back to detection', () => {
    // e.g. config carried over from an install that shipped more languages.
    withLanguages(['zh-CN'], () => expect(resolveLanguage('tlh')).toBe('zh-CN'))
  })

  it('falls back to en when neither stored nor browser matches', () => {
    withLanguages(['tlh-US'], () => expect(resolveLanguage('')).toBe('en'))
  })
})

describe('readStoredLanguage', () => {
  it('returns a stored supported language', () => {
    localStorage.setItem(LANG_STORAGE_KEY, 'zh-CN')
    expect(readStoredLanguage()).toBe('zh-CN')
  })

  it('returns the auto sentinel when unset', () => {
    expect(readStoredLanguage()).toBe('')
  })

  it('rejects an unsupported stored value', () => {
    localStorage.setItem(LANG_STORAGE_KEY, 'tlh')
    expect(readStoredLanguage()).toBe('')
  })

  it('survives storage being blocked', () => {
    const spy = vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
      throw new Error('SecurityError: storage blocked')
    })
    try {
      expect(readStoredLanguage()).toBe('')
    } finally {
      spy.mockRestore()
    }
  })
})

describe('detectBrowserLanguage — exact vs loose precedence', () => {
  /**
   * Regression: a single "first match wins" pass let an earlier tag's LOOSE
   * primary-subtag fallback outrank a later tag's EXACT match, so a
   * Traditional-Chinese reader who also reads English was served Simplified
   * script. Over-correcting (all exact matches beat all loose ones) breaks the
   * mirror case, where a user who ranked English first gets Chinese.
   */
  it('prefers a later EXACT match over an earlier loose one', () => {
    // zh-TW only matches zh-CN loosely; `en` is exact and explicitly ranked.
    withLanguages(['zh-TW', 'en'], () => expect(detectBrowserLanguage()).toBe('en'))
    withLanguages(['zh-Hant', 'en-US'], () => expect(detectBrowserLanguage()).toBe('en'))
    withLanguages(['zh-HK', 'en'], () => expect(detectBrowserLanguage()).toBe('en'))
  })

  it('still honours the highest-ranked tag when both match exactly', () => {
    withLanguages(['zh-CN', 'en'], () => expect(detectBrowserLanguage()).toBe('zh-CN'))
    withLanguages(['en', 'zh-CN'], () => expect(detectBrowserLanguage()).toBe('en'))
  })

  it('does not let a later exact match beat an earlier exact match', () => {
    // `en-GB` is exact for `en`, so ranking it first must win over zh-CN.
    withLanguages(['en-GB', 'zh-CN'], () => expect(detectBrowserLanguage()).toBe('en'))
  })

  it('uses the loose match when nothing matches exactly', () => {
    withLanguages(['zh-TW'], () => expect(detectBrowserLanguage()).toBe('zh-CN'))
    withLanguages(['tlh-US', 'zh-Hant'], () => expect(detectBrowserLanguage()).toBe('zh-CN'))
  })

  it('treats a regional variant of a region-less catalog as CONFIDENT', () => {
    // `fr-FR`/`pt-BR`/`es-MX` name a language we ship whose catalog carries no
    // region of its own, so there is no sibling catalog to confuse them with —
    // they must win outright over an earlier tag's loose script fallback.
    withLanguages(['fr-FR', 'zh-Hant'], () => expect(detectBrowserLanguage()).toBe('fr'))
    withLanguages(['pt-BR'], () => expect(detectBrowserLanguage()).toBe('pt'))
    withLanguages(['es-MX', 'zh-TW'], () => expect(detectBrowserLanguage()).toBe('es'))
    withLanguages(['ja-JP', 'zh-TW'], () => expect(detectBrowserLanguage()).toBe('ja'))
    withLanguages(['ko-KR', 'zh-TW'], () => expect(detectBrowserLanguage()).toBe('ko'))
  })

  it('takes the highest-ranked loose match when several match loosely', () => {
    // The leading tag must be a language we do NOT ship, or it wins outright and
    // this stops testing loose-match ranking at all.
    withLanguages(['tlh-US', 'zh-TW', 'zh-MO'], () => expect(detectBrowserLanguage()).toBe('zh-CN'))
  })
})
