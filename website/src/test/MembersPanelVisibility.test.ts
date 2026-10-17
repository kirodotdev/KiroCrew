/**
 * Crew Members page — hiding and showing the side panel.
 *
 * The page seats the panel two ways (`panelSitsBeside`): a column beside the
 * thread on a wide window, an overlay drawer on a narrow one. Both are
 * dismissable, and each placement keeps its own flag — the docked column a
 * persisted preference, the overlay a per-visit one. `panelChrome` is the whole
 * decision, so the cases live here rather than behind a mounted page.
 *
 * The second half composes that decision with the shared mount rules
 * (`shouldMountSidePanel` / `isSidePanelHidden`), which is where hiding a docked
 * column that holds a live app or browser tab has to stay a HIDE: unmounting
 * one destroys an iframe drawing or a WebContentsView page with nothing to
 * restore from.
 */
import { describe, it, expect } from 'vitest'
import { panelChrome, panelSitsBeside } from '../pages/members/MembersPage'
import { isSidePanelHidden, shouldMountSidePanel } from '../pages/chat/sidePanelMount'

/** Wide enough for a docked column, per panelSitsBeside's own arithmetic. */
const WIDE = { winW: 1800, rosterW: 260, isMobile: false }
/** Too narrow for a column, so the panel becomes the overlay drawer. */
const NARROW = { winW: 900, rosterW: 260, isMobile: false }

const mount = (panelVisible: boolean, extra: { hasLiveAppTab?: boolean; hasBrowserTab?: boolean } = {}) => ({
  activityOpen: panelVisible,
  hasLiveAppTab: false,
  hasBrowserTab: false,
  searchOpen: false,
  ...extra,
})

describe('members page panel placement', () => {
  it('seats the panel beside the thread only when the window affords a column', () => {
    expect(panelSitsBeside(WIDE)).toBe(true)
    expect(panelSitsBeside(NARROW)).toBe(false)
    expect(panelSitsBeside({ ...WIDE, isMobile: true })).toBe(false)
  })
})

describe('members page panel visibility', () => {
  it('hides the docked column when the stored choice says hidden', () => {
    const { panelVisible } = panelChrome({ beside: true, dockedOpen: false, overlayOpen: false })
    expect(panelVisible).toBe(false)
  })

  it('shows the docked column when the stored choice says shown', () => {
    const { panelVisible } = panelChrome({ beside: true, dockedOpen: true, overlayOpen: false })
    expect(panelVisible).toBe(true)
  })

  it('ignores the overlay flag while docked, so a dismissed drawer cannot hide the column', () => {
    expect(panelChrome({ beside: true, dockedOpen: true, overlayOpen: false }).panelVisible).toBe(true)
    expect(panelChrome({ beside: true, dockedOpen: false, overlayOpen: true }).panelVisible).toBe(false)
  })

  it('ignores the docked flag as an overlay, so a hidden column cannot suppress the drawer', () => {
    expect(panelChrome({ beside: false, dockedOpen: false, overlayOpen: true }).panelVisible).toBe(true)
    expect(panelChrome({ beside: false, dockedOpen: true, overlayOpen: false }).panelVisible).toBe(false)
  })
})

describe('members page panel opener', () => {
  it('withdraws the opener while the docked column is open, leaving the close to the panel strip', () => {
    expect(panelChrome({ beside: true, dockedOpen: true, overlayOpen: false }).showOpener).toBe(false)
  })

  it('offers the opener once the docked column is hidden', () => {
    expect(panelChrome({ beside: true, dockedOpen: false, overlayOpen: false }).showOpener).toBe(true)
  })

  it('keeps the opener in both overlay states, the drawer having no strip to reopen from', () => {
    expect(panelChrome({ beside: false, dockedOpen: true, overlayOpen: true }).showOpener).toBe(true)
    expect(panelChrome({ beside: false, dockedOpen: true, overlayOpen: false }).showOpener).toBe(true)
  })
})

describe('members page panel mount continuity', () => {
  it('unmounts a hidden docked column when no tab owns a body, keeping the width exit', () => {
    const { panelVisible } = panelChrome({ beside: true, dockedOpen: false, overlayOpen: false })
    expect(shouldMountSidePanel(mount(panelVisible))).toBe(false)
  })

  it('keeps a hidden docked column MOUNTED while an app tab is live, and hides it instead', () => {
    const { panelVisible } = panelChrome({ beside: true, dockedOpen: false, overlayOpen: false })
    const input = mount(panelVisible, { hasLiveAppTab: true })
    expect(shouldMountSidePanel(input)).toBe(true)
    expect(isSidePanelHidden(input)).toBe(true)
  })

  it('keeps a hidden docked column MOUNTED while a browser tab is live, and hides it instead', () => {
    const { panelVisible } = panelChrome({ beside: true, dockedOpen: false, overlayOpen: false })
    const input = mount(panelVisible, { hasBrowserTab: true })
    expect(shouldMountSidePanel(input)).toBe(true)
    expect(isSidePanelHidden(input)).toBe(true)
  })

  it('leaves a shown docked column mounted and unhidden', () => {
    const { panelVisible } = panelChrome({ beside: true, dockedOpen: true, overlayOpen: false })
    const input = mount(panelVisible, { hasLiveAppTab: true })
    expect(shouldMountSidePanel(input)).toBe(true)
    expect(isSidePanelHidden(input)).toBe(false)
  })
})
