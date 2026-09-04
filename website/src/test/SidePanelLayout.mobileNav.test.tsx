/**
 * The mobile branch is an iOS-style TWO-LEVEL navigation, replacing the old
 * horizontally scrolling pill strip (which, measured at 390px, hid fifteen of
 * nineteen Settings tabs past the clipped edge):
 *
 *   - ROOT (no ?tab=): the page title over a grouped vertical list of every
 *     tab — the whole map is visible, nothing to discover by scrolling
 *     sideways.
 *   - DETAIL (?tab=<key>): a sticky accent back bar carrying the page title
 *     ("‹ Settings") over the tab's own header and pane.
 *
 * The URL is the level: mobile always writes ?tab= explicitly (the desktop
 * convention of "first tab = no param" would make the first tab unreachable,
 * since param-less IS the root list there), and back deletes it.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, screen, cleanup, fireEvent } from '@testing-library/react'
import { MemoryRouter, useLocation, useNavigationType } from 'react-router-dom'
import SidePanelLayout, { type SidePanelTab } from '../components/SidePanelLayout'
import { SUBNAV_PUSH_STATE } from '../components/subNavParams'

let mobile = true
vi.mock('../hooks/useIsMobile', () => ({ useIsMobile: () => mobile }))

const TABS: SidePanelTab[] = [
  { key: 'overview', label: 'Overview', icon: null },
  // hostsSubNav: chrome-yield on ?sub=/legacy params is opt-in per tab —
  // the yield tests below drill into THIS tab.
  { key: 'security', label: 'Security', icon: null, group: 'System', hostsSubNav: true },
  { key: 'about', label: 'About', icon: null, group: 'System' },
]

function renderAt(search: string) {
  return render(
    <MemoryRouter initialEntries={[`/settings${search}`]}>
      <SidePanelLayout title="Settings" tabs={TABS} rememberKey="test-ios-nav">
        {tab => <div data-testid="pane">{tab}</div>}
      </SidePanelLayout>
    </MemoryRouter>,
  )
}

describe('mobile iOS-style two-level navigation', () => {
  beforeEach(() => { mobile = true; sessionStorage.clear() })
  afterEach(() => { vi.restoreAllMocks(); cleanup() })

  it('shows the root list — every tab, no pane — when the URL carries no tab', () => {
    renderAt('')
    for (const t of TABS) expect(screen.getByRole('button', { name: t.label })).toBeTruthy()
    expect(screen.queryByTestId('pane')).toBeNull()
    // Group headers render for grouped tabs.
    expect(screen.getByText('System')).toBeTruthy()
  })

  it('drills into a tab on tap and shows a back bar named after the page', () => {
    renderAt('')
    fireEvent.click(screen.getByRole('button', { name: 'Security' }))
    expect(screen.getByTestId('pane').textContent).toBe('security')
    expect(screen.getByRole('button', { name: /Settings/ })).toBeTruthy()
  })

  it('writes the param explicitly even for the FIRST tab — param-less is the root', () => {
    renderAt('')
    fireEvent.click(screen.getByRole('button', { name: 'Overview' }))
    // If the desktop "first tab deletes the param" convention leaked in here,
    // this click would bounce straight back to the root list.
    expect(screen.getByTestId('pane').textContent).toBe('overview')
  })

  it('returns to the root list on back', () => {
    renderAt('?tab=security')
    expect(screen.getByTestId('pane').textContent).toBe('security')
    fireEvent.click(screen.getByRole('button', { name: /Settings/ }))
    expect(screen.queryByTestId('pane')).toBeNull()
    expect(screen.getByRole('button', { name: 'Security' })).toBeTruthy()
  })

  it('opens a deep link directly in the detail view', () => {
    renderAt('?tab=about')
    expect(screen.getByTestId('pane').textContent).toBe('about')
  })

  it('does NOT auto-drill into the remembered tab — mobile always opens at root', () => {
    // iOS Settings opens at its root every time; a phone visit that teleports
    // into last week's tab reads as being lost, not resumed.
    sessionStorage.setItem('kirocrew:sidepanel-tab:test-ios-nav', 'security')
    renderAt('')
    expect(screen.queryByTestId('pane')).toBeNull()
    expect(screen.getByRole('button', { name: 'Security' })).toBeTruthy()
  })

  it('keeps the root visit from overwriting the remembered desktop tab', () => {
    sessionStorage.setItem('kirocrew:sidepanel-tab:test-ios-nav', 'security')
    renderAt('')
    expect(sessionStorage.getItem('kirocrew:sidepanel-tab:test-ios-nav')).toBe('security')
  })

  it('yields the whole level to the SubNav when a second level is drilled in', () => {
    // iOS push-stack: one back button per level. With ?sub= present the pane's
    // SubNav owns navigation, so THIS level's "‹ Settings" bar and big title
    // both step aside — two stacked back bars is the misread a stack prevents.
    renderAt('?tab=security&sub=rules')
    expect(screen.getByTestId('pane').textContent).toBe('security')
    expect(screen.queryByRole('button', { name: /Settings/ })).toBeNull()
  })

  it('yields on a LEGACY-alias deep link too — old bookmarks carry ?channel=/?section=', () => {
    // The registry deep links and pre-unification bookmarks write the aliases;
    // a level test that reads only the canonical name re-stacks the two back
    // bars on exactly those links (the primary search-result flow).
    renderAt('?tab=security&section=rules')
    expect(screen.getByTestId('pane').textContent).toBe('security')
    expect(screen.queryByRole('button', { name: /Settings/ })).toBeNull()
  })

  it('does NOT yield chrome on a tab that hosts no SubNav — selection params are not global reserved words', () => {
    // A stray ?section= (another page's param, a mangled link) on a tab
    // without hostsSubNav must not strip the back bar and title: there is no
    // SubNav back bar to replace them, and yielding would strand the pane
    // with zero navigation affordance.
    renderAt('?tab=overview&section=whatever')
    expect(screen.getByTestId('pane').textContent).toBe('overview')
    expect(screen.getByRole('button', { name: /Settings/ })).toBeInTheDocument()
  })

  it('leaves the desktop rail alone — remembered tab still restores there', () => {
    mobile = false
    sessionStorage.setItem('kirocrew:sidepanel-tab:test-ios-nav', 'about')
    renderAt('')
    expect(screen.getByTestId('pane').textContent).toBe('about')
    // Desktop renders the persistent rail, not the mobile root list — the
    // list's role=list/listitem structure must be absent.
    expect(screen.queryAllByRole('listitem')).toHaveLength(0)
  })
})

/**
 * Path model (basePath): the same iOS push-stack contract, expressed as URL
 * PATH segments instead of query params. `${basePath}` with no segments is
 * the root list, `${basePath}/<tab>` is the drilled-in detail, and a second
 * segment (`${basePath}/<tab>/<sub>`) is a SubNav's deeper level. The
 * history mechanics must hold exactly:
 *
 *   - drill-in PUSHES a real history entry carrying the SUBNAV_PUSH_STATE
 *     marker (so the platform back gesture pops like an iOS stack),
 *   - the back control POPS that entry via navigate(-1) — a replace-write
 *     would leave [root, root] twins and the next back-swipe would visibly
 *     do nothing,
 *   - a COLD deep link (an entry this stack did not mint) goes back by
 *     REPLACE, because popping would exit the app,
 *   - exactly one back bar per level: the chrome-yield level test is path
 *     DEPTH here (segment[1] present), not the ?sub= query params.
 */
function NavProbe() {
  const location = useLocation()
  const navType = useNavigationType()
  const pushed = !!(location.state as Record<string, unknown> | null)?.[SUBNAV_PUSH_STATE]
  return (
    <div
      data-testid="nav-probe"
      data-pathname={location.pathname}
      data-navtype={navType}
      data-push-marker={String(pushed)}
    />
  )
}

function renderPath(initialPath: string) {
  return render(
    <MemoryRouter initialEntries={[initialPath]}>
      <NavProbe />
      <SidePanelLayout title="Settings" tabs={TABS} rememberKey="test-path-nav" basePath="/settings">
        {tab => <div data-testid="pane">{tab}</div>}
      </SidePanelLayout>
    </MemoryRouter>,
  )
}

const probe = () => screen.getByTestId('nav-probe')

describe('mobile push/pop symmetry — path model (basePath)', () => {
  beforeEach(() => { mobile = true; sessionStorage.clear() })
  afterEach(() => { vi.restoreAllMocks(); cleanup() })

  it('drill-in PUSHES a history entry carrying the SUBNAV_PUSH_STATE marker', () => {
    renderPath('/settings')
    fireEvent.click(screen.getByRole('button', { name: 'Security' }))
    expect(screen.getByTestId('pane').textContent).toBe('security')
    expect(probe().dataset.pathname).toBe('/settings/security')
    // A real entry, not a rewrite of the root — the platform back gesture
    // must have something to pop.
    expect(probe().dataset.navtype).toBe('PUSH')
    // The marker is what lets the back control tell "we pushed this" from a
    // cold deep link; without it back degrades to the replace path and the
    // pushed root entry becomes an inert duplicate.
    expect(probe().dataset['pushMarker']).toBe('true')
  })

  it('writes the segment explicitly even for the FIRST tab — the bare basePath is the root', () => {
    renderPath('/settings')
    fireEvent.click(screen.getByRole('button', { name: 'Overview' }))
    // Desktop's "first tab = bare basePath" convention leaking in here would
    // bounce this click straight back to the root list.
    expect(screen.getByTestId('pane').textContent).toBe('overview')
    expect(probe().dataset.pathname).toBe('/settings/overview')
  })

  it('back POPS the pushed entry via navigate(-1) — not a replace-write', () => {
    renderPath('/settings')
    fireEvent.click(screen.getByRole('button', { name: 'Security' }))
    fireEvent.click(screen.getByRole('button', { name: /Settings/ }))
    // POP proves navigate(-1): a replace-write back to the root would report
    // REPLACE and leave [root, root] twins in history, so the next platform
    // back-swipe would visibly do nothing.
    expect(probe().dataset.navtype).toBe('POP')
    expect(probe().dataset.pathname).toBe('/settings')
    expect(screen.queryByTestId('pane')).toBeNull()
    expect(screen.getByRole('button', { name: 'Security' })).toBeTruthy()
  })

  it('cold deep link goes back by REPLACE — popping an entry we did not mint would exit the app', () => {
    renderPath('/settings/security')
    expect(screen.getByTestId('pane').textContent).toBe('security')
    fireEvent.click(screen.getByRole('button', { name: /Settings/ }))
    expect(probe().dataset.navtype).toBe('REPLACE')
    expect(probe().dataset.pathname).toBe('/settings')
    expect(screen.queryByTestId('pane')).toBeNull()
  })

  it('yields the whole level on path DEPTH — a second segment means the SubNav owns navigation', () => {
    // One back button per level: with `${basePath}/<tab>/<sub>` the pane's
    // SubNav shows its own back bar, so this level's "‹ Settings" bar and
    // big title both step aside.
    renderPath('/settings/security/rules')
    expect(screen.getByTestId('pane').textContent).toBe('security')
    expect(screen.queryByRole('button', { name: /Settings/ })).toBeNull()
  })

  it('a trailing slash (empty second segment) is NOT a drill-in — the back bar stays', () => {
    // `/settings/channels/` parses to an empty filler segment. Treating it as
    // drilled would hide the outer back bar while the SubNav shows its list
    // with no inner bar — a mobile pane with zero navigation affordance.
    renderPath('/settings/security/')
    expect(screen.getByTestId('pane').textContent).toBe('security')
    expect(screen.getByRole('button', { name: /Settings/ })).toBeInTheDocument()
  })

  it('does NOT yield on a second segment under a tab that hosts no SubNav', () => {
    // A stray deep segment (mangled link) on a non-hostsSubNav tab must not
    // strip the back bar: there is no SubNav bar to replace it, and yielding
    // would strand the pane with zero navigation affordance.
    renderPath('/settings/overview/whatever')
    expect(screen.getByTestId('pane').textContent).toBe('overview')
    expect(screen.getByRole('button', { name: /Settings/ })).toBeInTheDocument()
  })

  it('ignores legacy ?sub=/alias QUERY params for the level test — path depth is the only signal', () => {
    // In path mode old bookmarks are translated to paths upstream
    // (SettingsPage's legacy remap), not honoured here: a stray ?section=
    // riding on a depth-1 path must not strip this level's chrome.
    renderPath('/settings/security?section=rules')
    expect(screen.getByTestId('pane').textContent).toBe('security')
    expect(screen.getByRole('button', { name: /Settings/ })).toBeInTheDocument()
  })
})
