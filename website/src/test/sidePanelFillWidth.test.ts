/**
 * Tests for sidePanelFillWidth — the activity panel's BESIDE-vs-FILL decision.
 *
 * The gate reads the width left for the CHAT (viewport minus the nav rail track
 * minus the session sidebar), not the raw viewport. Hiding either piece of
 * chrome can therefore promote fill -> beside at an unchanged window width,
 * which is the whole point of the rule.
 */
import { describe, it, expect } from 'vitest'
import { SIDE_PANEL_MIN_W, CHAT_PANE_MIN_W, sidePanelFillWidth, sidePanelEffectiveWidth } from '../pages/chat/SidePanel'

const THRESHOLD = SIDE_PANEL_MIN_W + CHAT_PANE_MIN_W // 640
const RAIL_EXPANDED = 236
const RAIL_COLLAPSED = 74
const SIDEBAR = 260

const fill = (o: Partial<Parameters<typeof sidePanelFillWidth>[0]> = {}) =>
  sidePanelFillWidth({ winW: 1400, railW: RAIL_EXPANDED, sidebarW: SIDEBAR, isMobile: false, ...o })

describe('sidePanelFillWidth', () => {
  it('sits beside on a normal desktop window with all chrome shown', () => {
    // 1400 - 236 - 260 = 904 >= 640
    expect(fill()).toBeUndefined()
  })

  it('fills when the chat remainder cannot seat panel + chat minimum', () => {
    // 800 - 236 - 260 = 304 < 640
    expect(fill({ winW: 800 })).toBe(SIDE_PANEL_MIN_W)
  })

  it('promotes fill -> beside when the rail is collapsed, at the same window width', () => {
    // 900 - 236 - 260 = 404 -> fill;  900 - 74 - 260 = 566 -> still fill
    expect(fill({ winW: 900 })).toBe(404)
    expect(fill({ winW: 900, railW: RAIL_COLLAPSED })).toBe(566)
    // 980 - 74 - 260 = 646 >= 640 -> beside, where the expanded rail would not be
    expect(fill({ winW: 980, railW: RAIL_COLLAPSED })).toBeUndefined()
    expect(fill({ winW: 980 })).toBe(484)
  })

  it('promotes fill -> beside when the session sidebar is hidden', () => {
    // 768 - 74 - 0 = 694 >= 640 -> beside (the user's stated case: both hidden)
    expect(fill({ winW: 768, railW: RAIL_COLLAPSED, sidebarW: 0 })).toBeUndefined()
    // same window, sidebar shown: 768 - 74 - 260 = 434 -> fill
    expect(fill({ winW: 768, railW: RAIL_COLLAPSED })).toBe(434)
  })

  it('is exact at the boundary', () => {
    const winW = THRESHOLD + RAIL_EXPANDED + SIDEBAR // remainder == 640
    expect(fill({ winW })).toBeUndefined()
    expect(fill({ winW: winW - 1 })).toBe(THRESHOLD - 1)
  })

  it('never returns a fill width below the panel minimum', () => {
    // Absurdly cramped: the panel keeps its floor and overflows instead of
    // collapsing to an unusable sliver.
    expect(fill({ winW: 400 })).toBe(SIDE_PANEL_MIN_W)
    expect(fill({ winW: 300, railW: 0, sidebarW: 0 })).toBe(SIDE_PANEL_MIN_W)
  })

  it('always fills on mobile regardless of the remainder', () => {
    // A 700px phone-class viewport clears 640 on paper; mobile still fills,
    // because SidePanel renders full-width there and the drawer is fixed.
    expect(fill({ winW: 700, railW: 0, sidebarW: 0, isMobile: true })).toBe(700)
    expect(fill({ winW: 390, railW: 0, sidebarW: 0, isMobile: true })).toBe(390)
    // …but never below the panel floor.
    expect(fill({ winW: 280, railW: 0, sidebarW: 0, isMobile: true })).toBe(SIDE_PANEL_MIN_W)
  })
})

/**
 * Branch ORDER regression. The mobile arm used to be checked before fillWidth,
 * so a mobile frame got `width: '100%'` and the computed fill width was thrown
 * away. That percentage cannot resolve inside the inline render path's
 * shrink-to-fit `width: auto` wrapper, so the panel rendered at its own
 * max-content width instead of filling the screen.
 */
describe('sidePanelEffectiveWidth', () => {
  const base = { isMobile: false, expanded: false, width: 460, maxW: 800 }

  it('prefers the explicit fill width over the mobile percentage', () => {
    expect(sidePanelEffectiveWidth({ ...base, isMobile: true, fillWidth: 390 })).toBe(390)
  })

  it('prefers the explicit fill width over the persisted width on desktop', () => {
    expect(sidePanelEffectiveWidth({ ...base, fillWidth: 520 })).toBe(520)
  })

  it('falls back to the percentage on a mobile frame with no fill width', () => {
    expect(sidePanelEffectiveWidth({ ...base, isMobile: true })).toBe('100%')
  })

  it('clamps the persisted width to the responsive maximum in beside mode', () => {
    expect(sidePanelEffectiveWidth({ ...base, width: 460, maxW: 800 })).toBe(460)
    expect(sidePanelEffectiveWidth({ ...base, width: 900, maxW: 800 })).toBe(800)
    expect(sidePanelEffectiveWidth({ ...base, width: 460, maxW: 100 })).toBe(SIDE_PANEL_MIN_W)
  })

  it('takes the maximum in preview-focus (expanded) mode', () => {
    expect(sidePanelEffectiveWidth({ ...base, expanded: true, maxW: 800 })).toBe(800)
    expect(sidePanelEffectiveWidth({ ...base, expanded: true, maxW: 100 })).toBe(SIDE_PANEL_MIN_W)
  })
})
