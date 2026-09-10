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
    withLanguages(['zh-tw'], () => expect(detectBrowserLanguage()).toBe('zh-TW'))
  })

  it('matches zh-TW exactly, not through the zh fallback', () => {
    // The whole point of shipping 繁體中文: a Taiwan browser must land on the
    // Traditional catalog. Before it existed this tag fell through the
    // primary-subtag branch to zh-CN, which served Simplified script to a
    // reader who asked for Traditional (#2571).
    withLanguages(['zh-TW'], () => expect(detectBrowserLanguage()).toBe('zh-TW'))
  })

  it('falls back to a primary-subtag match', () => {
    // A zh-preferring browser must get Chinese, not English, even when the
    // exact regional tag isn't one we ship.
    withLanguages(['zh'], () => expect(detectBrowserLanguage()).toBe('zh-CN'))
    withLanguages(['zh-Hans'], () => expect(detectBrowserLanguage()).toBe('zh-CN'))
    // Deliberately unchanged: `zh-Hant` names a script, not a region, and this
    // change ships Taiwan only. Redirecting every Traditional-script tag is a
    // separate decision — see #1130.
    withLanguages(['zh-Hant'], () => expect(detectBrowserLanguage()).toBe('zh-CN'))
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

  it('restores an explicit zh-TW choice verbatim', () => {
    // Registration is only half the fix: a stored code that `isRestorableLanguage`
    // rejects is silently downgraded to auto-detect, so a Taiwan user who picked
    // 繁體中文 on an English machine would get English back on every reload.
    withLanguages(['en-US'], () => expect(resolveLanguage('zh-TW')).toBe('zh-TW'))
    withLanguages(['zh-CN'], () => expect(resolveLanguage('zh-TW')).toBe('zh-TW'))
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
    // `zh-Hant`/`zh-HK` only match a Chinese catalog loosely; `en` is exact and
    // explicitly ranked. (`zh-TW` used to belong here and no longer does: it is
    // an exact match now that 繁體中文 ships, which is the fix, not a regression.)
    withLanguages(['zh-Hant', 'en-US'], () => expect(detectBrowserLanguage()).toBe('en'))
    withLanguages(['zh-HK', 'en'], () => expect(detectBrowserLanguage()).toBe('en'))
  })

  it('lets an exact zh-TW outrank a lower-ranked English', () => {
    // The mirror of the case above, and the reason the loose/confident split
    // exists at all: a Taiwan reader who also reads English ranked Traditional
    // first, so they get Traditional.
    withLanguages(['zh-TW', 'en'], () => expect(detectBrowserLanguage()).toBe('zh-TW'))
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
    withLanguages(['zh-Hant'], () => expect(detectBrowserLanguage()).toBe('zh-CN'))
    withLanguages(['tlh-US', 'zh-Hant'], () => expect(detectBrowserLanguage()).toBe('zh-CN'))
  })

  it('keeps zh-Hant / zh-HK / zh-MO on the loose branch', () => {
    // With two Chinese catalogs registered, `matchConfident` finds TWO
    // candidates for primary subtag `zh` and declines — so a script-only or
    // other-region tag still resolves through `matchTag`, which returns the
    // first registered `zh-*` code. Pinned because the natural follow-up
    // ("send everything Traditional to zh-TW") is a separate product decision
    // about Hong Kong and Macau vocabulary, not a side effect of this PR.
    withLanguages(['zh-Hant'], () => expect(detectBrowserLanguage()).toBe('zh-CN'))
    withLanguages(['zh-HK'], () => expect(detectBrowserLanguage()).toBe('zh-CN'))
    withLanguages(['zh-MO'], () => expect(detectBrowserLanguage()).toBe('zh-CN'))
  })

  it('treats a regional variant of a region-less catalog as CONFIDENT', () => {
    // `fr-FR`/`pt-BR`/`es-MX` name a language we ship whose catalog carries no
    // region of its own, so there is no sibling catalog to confuse them with —
    // they must win outright over an earlier tag's loose script fallback.
    withLanguages(['fr-FR', 'zh-Hant'], () => expect(detectBrowserLanguage()).toBe('fr'))
    withLanguages(['pt-BR'], () => expect(detectBrowserLanguage()).toBe('pt'))
    withLanguages(['es-MX', 'zh-Hant'], () => expect(detectBrowserLanguage()).toBe('es'))
    withLanguages(['ja-JP', 'zh-Hant'], () => expect(detectBrowserLanguage()).toBe('ja'))
    withLanguages(['ko-KR', 'zh-Hant'], () => expect(detectBrowserLanguage()).toBe('ko'))
  })

  it('does not treat zh-CN or zh-TW as a confident match for the other', () => {
    // Two catalogs share the `zh` primary subtag, so neither can absorb the
    // other's region: an exact tag wins, and everything else stays loose. This
    // is the assertion that would fail if someone "simplified" matchConfident
    // by dropping its single-candidate condition.
    withLanguages(['zh-CN', 'zh-TW'], () => expect(detectBrowserLanguage()).toBe('zh-CN'))
    withLanguages(['zh-TW', 'zh-CN'], () => expect(detectBrowserLanguage()).toBe('zh-TW'))
  })

  it('takes the highest-ranked loose match when several match loosely', () => {
    // The leading tag must be a language we do NOT ship, or it wins outright and
    // this stops testing loose-match ranking at all — and the remaining tags must
    // be loose ones, which is why `zh-TW` is no longer usable here.
    withLanguages(['tlh-US', 'zh-Hant', 'zh-MO'],
      () => expect(detectBrowserLanguage()).toBe('zh-CN'))
  })
})
