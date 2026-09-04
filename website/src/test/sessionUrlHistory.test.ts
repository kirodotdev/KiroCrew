import { describe, it, expect } from 'vitest'
import { shouldReplaceSessionUrl, popMaySwitchSession } from '../utils/sessionUrlHistory'

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

describe('popMaySwitchSession', () => {
  it('honours a POP on desktop, where a session switch pushed the entry', () => {
    expect(popMaySwitchSession({ isMobile: false, entryPushedBySwitch: false })).toBe(true)
  })

  it('never honours an unrecorded POP entry on mobile', () => {
    // The two predicates are one decision read from both ends, and #8207 is what
    // their disagreement cost: the write side stopped pushing session entries on
    // mobile while the read side kept treating any POP `?sid=` as a session the
    // user retraced. The only mobile entry carrying a foreign sid is the one
    // under the sessions drawer's duplicate, so honouring a POP there walked the
    // pane back into the conversation the user had just left.
    expect(popMaySwitchSession({ isMobile: true, entryPushedBySwitch: false })).toBe(false)
  })

  it('honours a pushed entry on mobile, because the layout is not its provenance', () => {
    // A window narrowed after the push, or an iPad Mini rotated into portrait.
    // The layout is read now; the entry was written earlier, possibly on the
    // other side of the breakpoint. Declining here would not merely make Back
    // inert — the reader REPAIRS the sid it declines, so it would overwrite a
    // real history target and destroy it.
    expect(popMaySwitchSession({ isMobile: true, entryPushedBySwitch: true })).toBe(true)
  })

  it('agrees with the write side: what mobile never pushes, mobile never pops', () => {
    // Stated as the coupling rather than two constants, so flipping either one
    // alone fails here instead of silently reopening the gap between them.
    const isMobile = true
    expect(shouldReplaceSessionUrl({ isSessionSwitch: true, isMobile })).toBe(true)
    expect(popMaySwitchSession({ isMobile, entryPushedBySwitch: false })).toBe(false)
  })
})
