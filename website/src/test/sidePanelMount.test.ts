/**
 * Pins the panel mount decision. The invariant that matters: while an MCP App
 * tab is live, closing the panel must NOT unmount the subtree — the app's
 * null-origin iframe cannot survive a remount, so an unmount loses the drawing.
 */
import { describe, it, expect } from 'vitest'
import { shouldMountSidePanel, isSidePanelHidden } from '../pages/chat/sidePanelMount'

const S = (activityOpen: boolean, hasLiveAppTab: boolean, searchOpen = false) =>
  ({ activityOpen, hasLiveAppTab, searchOpen })

describe('side panel mount decision', () => {
  it('mounts while open, with or without an app tab', () => {
    expect(shouldMountSidePanel(S(true, false))).toBe(true)
    expect(shouldMountSidePanel(S(true, true))).toBe(true)
  })

  it('UNMOUNTS on close when no app tab is live (preserves the exit animation)', () => {
    expect(shouldMountSidePanel(S(false, false))).toBe(false)
  })

  it('STAYS MOUNTED on close while an app tab is live — the load-bearing case', () => {
    expect(shouldMountSidePanel(S(false, true))).toBe(true)
    // …and is hidden rather than shown, so closing still looks closed.
    expect(isSidePanelHidden(S(false, true))).toBe(true)
  })

  it('is never hidden while the user has the panel open and unobstructed', () => {
    expect(isSidePanelHidden(S(true, true))).toBe(false)
    expect(isSidePanelHidden(S(true, false))).toBe(false)
  })

  describe('find pane claims the dock', () => {
    it('UNMOUNTS the panel when no app tab is live (unchanged precedence — held pre-fix too)', () => {
      expect(shouldMountSidePanel(S(true, false, true))).toBe(false)
      expect(shouldMountSidePanel(S(false, false, true))).toBe(false)
    })

    it('KEEPS a live app tab mounted and hides it instead — regression guard', () => {
      // Was a real data-loss bug: `if (searchOpen) return false` ran before the
      // app-tab check, so pressing Ctrl+F unmounted the panel and destroyed the
      // drawing. Frame survival must not yield to a transient dock claim.
      expect(shouldMountSidePanel(S(true, true, true))).toBe(true)
      expect(isSidePanelHidden(S(true, true, true))).toBe(true)
      // …and the same while the panel was already closed.
      expect(shouldMountSidePanel(S(false, true, true))).toBe(true)
      expect(isSidePanelHidden(S(false, true, true))).toBe(true)
    })
  })

  it('mounts a live app tab in every combination — the invariant, stated exhaustively', () => {
    for (const activityOpen of [true, false]) {
      for (const searchOpen of [true, false]) {
        expect(shouldMountSidePanel(S(activityOpen, true, searchOpen))).toBe(true)
      }
    }
  })
})
