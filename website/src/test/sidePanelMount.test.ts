/**
 * Pins the panel mount decision. The invariant that matters: while an MCP App
 * tab is live, closing the panel must NOT unmount the subtree — the app's
 * null-origin iframe cannot survive a remount, so an unmount loses the drawing.
 */
import { describe, it, expect } from 'vitest'
import { join } from 'node:path'
import { readSource } from './readSource'
import { shouldMountSidePanel, isSidePanelHidden, SIDE_PANEL_DOCK_TRANSITION, SIDE_PANEL_MOTION_EASE, SIDE_PANEL_MOTION_MS, sidePanelDimTransition } from '../pages/chat/sidePanelMount'

const S = (activityOpen: boolean, hasLiveAppTab: boolean, searchOpen = false, hasBrowserTab = false) =>
  ({ activityOpen, hasLiveAppTab, hasBrowserTab, searchOpen })

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

  /**
   * A hidden panel still has a SELECTED tab, so tab selection alone is not
   * "the user can see this". The tab bodies bind document-level keys (Escape
   * closes the file, Cmd/Ctrl+S writes it to disk), and a closed panel kept
   * mounted for a live app/browser tab would otherwise answer both from an
   * editor that is nowhere on screen.
   *
   * A source assertion because the composition is one expression in JSX, and
   * rendering the whole panel to reach it would test the harness more than the
   * wiring. What must not regress is that `panelHidden` participates at all.
   */
  it('composes tab-body visibility from BOTH tab selection and panel visibility', () => {
    const src = readSource(join(__dirname, '..', 'pages', 'chat', 'SidePanel.tsx'))
    expect(src).toContain('active={isActive && !panelHidden}')
    // ...and the flag has to reach the panel from its host, or the guard above
    // is dead code that always reads undefined.
    const host = readSource(join(__dirname, '..', 'pages', 'ChatPage.tsx'))
    expect(host.match(/panelHidden=\{isSidePanelHidden\(/g) ?? []).toHaveLength(2)
  })

  /**
   * The cross-slot keep-mounted list needs the SAME composition. It renders a
   * contributed `panelTabs` body, whose `entry` module runs in the dashboard's own
   * origin and typically polls and binds global handlers — so selection alone would
   * leave a collapsed panel's app live. Its sibling one branch up already reads
   * `isActive && !panelHidden`; these two must not drift apart.
   *
   * `display` deliberately stays on `shown` ALONE: the body has to keep its box while
   * the panel is merely collapsed, or the `AppHost` would remount and lose its state.
   */
  it('composes the cross-slot app-tab body from BOTH selection and panel visibility', () => {
    const src = readSource(join(__dirname, '..', 'pages', 'chat', 'SidePanel.tsx'))
    expect(src).toMatch(/<AppPanelTabBody kind=\{t\.kind\} active=\{shown && !panelHidden\}/)
    // The visibility style must NOT pick up the same flag, or a collapsed panel
    // remounts the host instead of merely hiding it.
    expect(src).toContain("display: shown ? 'block' : 'none'")
    expect(src).not.toContain("display: shown && !panelHidden")
  })

  /**
   * Every tab body in the keep-mounted branch that binds a document-level key
   * needs the same signal, not just the file one. `ArtifactPanel` binds Escape
   * and would otherwise close an artifact that is off screen. `CliPanel` and
   * `FolderPanel` scope their handlers to their own elements, so they cannot.
   */
  it('passes the same visibility signal to the artifact body', () => {
    const src = readSource(join(__dirname, '..', 'pages', 'chat', 'SidePanel.tsx'))
    expect(src).toMatch(/<ArtifactPanel\s+embedded\s+active=\{active\}/)
    const panel = readSource(join(__dirname, '..', 'components', 'ArtifactPanel.tsx'))
    // The guard, and the flag in the effect's deps so a switch re-binds it. The
    // prop is aliased to `visible` there because a local `active` in that file
    // already names the fullscreen icon.
    expect(panel).toContain('active: visible = true')
    expect(panel).toContain('if (!visible) return')
    expect(panel).toContain('}, [visible, fullscreen, onClose])')
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

  describe('a live browser tab', () => {
    // The Browser tab hosts an Electron WebContentsView that useNativeBrowser
    // destroys on unmount (api.close). Closing the panel must hide, not unmount,
    // or the loaded page (and its scroll/history) is lost — same shape as the
    // app-tab invariant above.
    it('STAYS MOUNTED and hidden on close', () => {
      expect(shouldMountSidePanel(S(false, false, false, true))).toBe(true)
      expect(isSidePanelHidden(S(false, false, false, true))).toBe(true)
    })

    it('survives the find pane claiming the dock', () => {
      expect(shouldMountSidePanel(S(true, false, true, true))).toBe(true)
      expect(isSidePanelHidden(S(true, false, true, true))).toBe(true)
    })

    it('is shown while the panel is open and unobstructed', () => {
      expect(isSidePanelHidden(S(true, false, false, true))).toBe(false)
    })
  })
})

/**
 * Opening the panel and switching to a chat with a different remembered width
 * move the SAME edge, so they have to look like one motion. They cannot share a
 * code path — one is a framer tween on the dock wrapper, the other a CSS
 * transition on the panel — so they share constants, and these cases are what
 * stops a later edit to one from silently desynchronising the other.
 */
describe('side panel motion timing is shared, not copied', () => {
  it('expresses the same duration and curve through both mechanisms', () => {
    // framer takes seconds and a bezier array…
    expect(SIDE_PANEL_DOCK_TRANSITION.duration).toBe(SIDE_PANEL_MOTION_MS / 1000)
    expect(SIDE_PANEL_DOCK_TRANSITION.ease).toEqual(SIDE_PANEL_MOTION_EASE)
    // …CSS takes ms and a cubic-bezier() function. Same numbers either way.
    expect(sidePanelDimTransition('width')).toEqual({ transition: 'width 180ms cubic-bezier(0.2, 0, 0, 1)' })
    expect(sidePanelDimTransition('height')).toEqual({ transition: 'height 180ms cubic-bezier(0.2, 0, 0, 1)' })
  })

  it('derives the CSS curve from the constant rather than restating it', () => {
    const src = readSource(join(__dirname, '..', 'pages', 'chat', 'sidePanelMount.ts'))
    // One bezier literal in the module — the exported constant. A second one
    // would mean the CSS builder had been given its own copy to drift from.
    expect(src.match(/0\.2, 0, 0, 1/g)).toHaveLength(1)
  })

  it('is the shared constant that the dock wrapper actually uses', () => {
    const host = readSource(join(__dirname, '..', 'pages', 'ChatPage.tsx'))
    expect(host).toContain('transition={SIDE_PANEL_DOCK_TRANSITION}')
  })
})
