import { describe, expect, it } from 'vitest'
import { truncateTabTitle } from './sessionTabs'

describe('truncateTabTitle with astral characters', () => {
  it('drops an emoji the 24-unit cap would cut in half instead of keeping its high surrogate', () => {
    // 23 ASCII units, then an emoji occupying UTF-16 units 23-24.
    const title = 'Release notes for v2.1 🎉 shipped'
    expect(title.indexOf('🎉')).toBe(23)
    expect(truncateTabTitle(title)).toBe('Release notes for v2.1 …')
  })

  it('keeps an emoji that ends exactly at the cap', () => {
    const title = `${'a'.repeat(22)}🎉 and more`
    expect(truncateTabTitle(title)).toBe(`${'a'.repeat(22)}🎉…`)
  })

  it('leaves ASCII titles at 24 units plus the ellipsis', () => {
    expect(truncateTabTitle('y'.repeat(30))).toBe(`${'y'.repeat(24)}…`)
  })
})
