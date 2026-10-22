import { beforeEach, describe, expect, it } from 'vitest'
import { claimDashboardOnLoad, resetDashboardOnLoad } from './dashboardOnLoad'

describe('claimDashboardOnLoad', () => {
  beforeEach(() => resetDashboardOnLoad())

  it('claims a mate once per page load', () => {
    expect(claimDashboardOnLoad('oncall', 'member-oncall')).toBe(true)
    // A later render or an in-app return keeps the tab the person picked.
    expect(claimDashboardOnLoad('oncall', 'member-oncall')).toBe(false)
  })

  it('does not claim before the slot confirms, then claims once on the confirmed slot', () => {
    expect(claimDashboardOnLoad('oncall', '')).toBe(false)
    expect(claimDashboardOnLoad('oncall', 'member-oncall')).toBe(true)
  })

  it('a second thread of the same mate does not force Dashboard again', () => {
    expect(claimDashboardOnLoad('oncall', 'member-oncall')).toBe(true)
    expect(claimDashboardOnLoad('oncall', 'member-oncall-2')).toBe(false)
  })

  it('claims each mate on its own', () => {
    expect(claimDashboardOnLoad('oncall', 's1')).toBe(true)
    expect(claimDashboardOnLoad('writer', 's2')).toBe(true)
  })

  it('claims again after a reload', () => {
    claimDashboardOnLoad('oncall', 's1')
    resetDashboardOnLoad()
    expect(claimDashboardOnLoad('oncall', 's1')).toBe(true)
  })
})
