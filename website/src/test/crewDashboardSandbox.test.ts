import { describe, it, expect } from 'vitest'

import {
  CREW_DASHBOARD_SANDBOX,
  CREW_DASHBOARD_SANDBOX_STATIC,
  crewDashboardSandbox,
} from '../pages/members/CrewDynamicDashboard'

/**
 * The grant set a crewmate's dashboard page is mounted with.
 *
 * An agent-written page is licensed to run script by the RFC amendment's fourth
 * reason: the owner agent already reaches the network, so a page navigating its
 * own frame opens no path that agent lacked. Under an admin ceiling that DENIES
 * the agent egress that reason is false, and nothing else in the frame stops
 * navigation -- a closed `connect-src` does not. The server decides and answers
 * `scripts: false`; this is the host honouring it.
 */
describe('the mate dashboard sandbox', () => {
  it('grants allow-scripts and nothing else where the page may run', () => {
    expect(crewDashboardSandbox(true)).toBe('allow-scripts')
    expect(crewDashboardSandbox(true)).toBe(CREW_DASHBOARD_SANDBOX)
  })

  it('grants NOTHING where the ceiling denies the owner agent egress', () => {
    // An empty sandbox attribute is every restriction applied, which is what
    // makes the page static: no script, so no self-navigation.
    expect(crewDashboardSandbox(false)).toBe('')
    expect(crewDashboardSandbox(false)).toBe(CREW_DASHBOARD_SANDBOX_STATIC)
  })

  it('never grants same-origin or top-navigation in either state', () => {
    // The two grants that would undo the opaque origin or let the frame move the
    // tab. Asserted for BOTH values so a future edit to either cannot add one.
    for (const value of [crewDashboardSandbox(true), crewDashboardSandbox(false)]) {
      expect(value).not.toContain('allow-same-origin')
      expect(value).not.toContain('allow-top-navigation')
      expect(value).not.toContain('allow-popups')
      expect(value).not.toContain('allow-forms')
      expect(value).not.toContain('allow-modals')
    }
  })

  it('is decided only by the flag, so the page cannot widen its own grants', () => {
    // The argument is a boolean the SERVER sent. There is no path from page
    // content to this value, which is the property worth pinning: a page that
    // could influence it would be a page that could grant itself script.
    expect(crewDashboardSandbox(Boolean('anything truthy'))).toBe('allow-scripts')
    expect(crewDashboardSandbox(Boolean(''))).toBe('')
  })
})
