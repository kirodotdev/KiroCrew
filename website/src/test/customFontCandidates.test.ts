import { describe, it, expect } from 'vitest'

import { CUSTOM_FONT_CANDIDATES, customFontCandidatesFor } from '../utils/customFontCandidates'

describe('custom font candidates', () => {
  it('offers the common Japanese, Chinese and Korean reading faces', () => {
    for (const family of [
      'Meiryo', 'Yu Gothic UI', 'Hiragino Sans', 'Hiragino Kaku Gothic ProN', 'Noto Sans JP',
      'Microsoft YaHei', 'PingFang SC', 'Microsoft JhengHei', 'PingFang TC',
      'Malgun Gothic', 'Apple SD Gothic Neo',
    ]) {
      expect(CUSTOM_FONT_CANDIDATES, family).toContain(family)
    }
  })

  it('lists each family once', () => {
    expect(new Set(CUSTOM_FONT_CANDIDATES).size).toBe(CUSTOM_FONT_CANDIDATES.length)
  })

  it.each([
    ['ja', 'Hiragino Sans', 'Meiryo'],
    ['ko', 'Apple SD Gothic Neo', 'Malgun Gothic'],
    ['zh-CN', 'PingFang SC', 'Microsoft YaHei'],
    ['zh-TW', 'PingFang TC', 'Microsoft JhengHei'],
  ])('leads with the %s UI language\'s own faces', (language, first, alsoEarly) => {
    const list = customFontCandidatesFor(language)
    expect(list[0]).toBe(first)
    expect(list.indexOf(alsoEarly)).toBeLessThan(list.indexOf('Arial'))
    expect([...list].sort()).toEqual([...CUSTOM_FONT_CANDIDATES].sort())
  })

  it('keeps the default order for other languages', () => {
    expect(customFontCandidatesFor('en')).toBe(CUSTOM_FONT_CANDIDATES)
    expect(customFontCandidatesFor('')).toBe(CUSTOM_FONT_CANDIDATES)
  })
})
