import { describe, it, expect } from 'vitest'
import { shouldReplaceSessionUrl } from '../utils/sessionUrlHistory'

describe('shouldReplaceSessionUrl', () => {
  it('pushes a real session switch on desktop, so Back retraces sessions', () => {
    expect(shouldReplaceSessionUrl({ isSessionSwitch: true, isMobile: false })).toBe(false)
  })

  it('NEVER pushes on mobile, even for a real session switch', () => {
    // The regression this pins: mobile Back is a platform edge-swipe and the
    // only back affordance the narrow layout has. Pushing per session tap made
    // it retrace sessions while skipping every layer the user had actually
    // opened (drawer, side panel, file viewer — all component state), so Back
    // landed in an unrelated earlier chat. If this flips back to false, that
    // behaviour returns and nothing else in the suite would notice.
    expect(shouldReplaceSessionUrl({ isSessionSwitch: true, isMobile: true })).toBe(true)
  })

  it('replaces a non-switch write on either layout', () => {
    // Initial activation and same-session path normalisation are not
    // navigations the user performed, so neither may leave an entry.
    expect(shouldReplaceSessionUrl({ isSessionSwitch: false, isMobile: false })).toBe(true)
    expect(shouldReplaceSessionUrl({ isSessionSwitch: false, isMobile: true })).toBe(true)
  })
})
