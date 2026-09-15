import { describe, expect, it } from 'vitest'
import { coerceDashboardTab, coerceSortKey } from '../apps/issue-radar/lib/format'
import { DASHBOARD_TABS, SORT_KEYS } from '../apps/issue-radar/lib/types'

// Issue Radar persists the active sort key and dashboard tab to localStorage.
// When an option is removed from the app, previously-persisted values must not
// survive a reload: a retired sort key would leave the list unsorted with no
// rail option highlighted, and a retired dashboard tab would render an empty
// main area. These guards pin that fallback behaviour.
describe('issue-radar persisted UI state coercion', () => {
  it('keeps every supported sort key', () => {
    for (const key of SORT_KEYS) expect(coerceSortKey(key)).toBe(key)
  })

  it('falls back to number for retired or malformed sort keys', () => {
    // 'ranking' is a retired sort key (the AI-ordered sort).
    for (const bad of ['ranking', '', 'Number', undefined, null, 7, {}]) {
      expect(coerceSortKey(bad)).toBe('number')
    }
  })

  it('keeps every registered dashboard tab', () => {
    for (const tab of DASHBOARD_TABS) expect(coerceDashboardTab(tab)).toBe(tab)
  })

  it('falls back to overview for retired or malformed dashboard tabs', () => {
    // ranking / insights / duplicates are retired placeholder dashboard tabs.
    for (const bad of ['ranking', 'insights', 'duplicates', '', undefined, null, 3, []]) {
      expect(coerceDashboardTab(bad)).toBe('overview')
    }
  })
})
