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
import { MemoryRouter } from 'react-router-dom'
import SidePanelLayout, { type SidePanelTab } from '../components/SidePanelLayout'

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
