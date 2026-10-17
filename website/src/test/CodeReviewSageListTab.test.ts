import { describe, expect, it } from 'vitest'

import { coerceListTab } from '../apps/code-review-sage/lib/persist'

describe('coerceListTab', () => {
  it('keeps every tab the rail renders, so a reload lands where the user left', () => {
    for (const tab of ['pulls', 'reviews', 'queue']) expect(coerceListTab(tab)).toBe(tab)
  })

  it('falls back to pull requests for a tab that no longer exists', () => {
    expect(coerceListTab('gone')).toBe('pulls')
  })
})
