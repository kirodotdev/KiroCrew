/**
 * Tab memory for SidePanelLayout-backed pages.
 *
 * The tab lives in `?tab=`, and every plain entry point (sidebar link, the
 * ⌘-shortcut, `navigate('/settings')`) drops that param — which used to snap
 * the page back to its first tab on every return visit. These tests pin the
 * memory that fixes it, and the three ways it must NOT fire:
 *   - an explicit `?tab=` (deep link / command palette) always wins,
 *   - deliberately picking the first tab is remembered as the first tab,
 *   - a remembered tab that no longer exists in the roster is ignored.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { MemoryRouter, useLocation, useNavigate } from 'react-router-dom'
import SidePanelLayout, { type SidePanelTab } from '../components/SidePanelLayout'

// SidePanelLayout → useIsMobile reads window.matchMedia at module load.
if (!window.matchMedia) {
  window.matchMedia = ((query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addEventListener: () => {},
    removeEventListener: () => {},
    addListener: () => {},
    removeListener: () => {},
    dispatchEvent: () => false,
  })) as unknown as typeof window.matchMedia
}

const TABS: SidePanelTab[] = [
  { key: 'overview', label: 'Overview', icon: null },
  { key: 'security', label: 'Security', icon: null },
  { key: 'about', label: 'About', icon: null },
]

const KEY = 'kirocrew:sidepanel-tab:test-page'

/** Surfaces the live query string so a test can assert URL/pane agreement. */
function UrlProbe() {
  const { search } = useLocation()
  return <div data-testid="search">{search}</div>
}

/** Re-navigates to the SAME route with no query — what ⌘+, and the sidebar
 *  entry both do — without unmounting the layout. */
function ReNavigate({ to }: { to: string }) {
  const navigate = useNavigate()
  return <button onClick={() => navigate(to)}>re-navigate</button>
}

function renderPage(opts: { url?: string; rememberKey?: string; tabs?: SidePanelTab[]; seen?: string[] } = {}) {
  return render(
    <MemoryRouter initialEntries={[opts.url ?? '/page']}>
      <SidePanelLayout
        title="Test"
        tabs={opts.tabs ?? TABS}
        rememberKey={'rememberKey' in opts ? opts.rememberKey : 'test-page'}
      >
        {tab => {
          opts.seen?.push(tab)
          return <div data-testid="active">{tab}</div>
        }}
      </SidePanelLayout>
    </MemoryRouter>,
  )
}

describe('SidePanelLayout tab memory', () => {
  beforeEach(() => {
    sessionStorage.clear()
    vi.restoreAllMocks()
  })

  it('remembers the tab you clicked and restores it on a param-less return', () => {
    const { unmount } = renderPage()
    fireEvent.click(screen.getByText('Security'))
    expect(screen.getByTestId('active')).toHaveTextContent('security')
    unmount()

    // Navigating back via the sidebar / shortcut — no ?tab= in the URL.
    renderPage()
    expect(screen.getByTestId('active')).toHaveTextContent('security')
  })

  it('remembers a tab reached by deep link, not just by click', () => {
    const { unmount } = renderPage({ url: '/page?tab=about' })
    expect(screen.getByTestId('active')).toHaveTextContent('about')
    unmount()

    renderPage()
    expect(screen.getByTestId('active')).toHaveTextContent('about')
  })

  it('paints the remembered pane on the FIRST render, never flashing the first tab', () => {
    // Restoring from an effect would render `overview` for a frame before
    // swapping to `security` — a visible flash, and a wasted mount of a pane
    // that fetches. The restore is seeded during the first render instead, so
    // the first-tab pane must never be handed to the children callback at all.
    sessionStorage.setItem(KEY, 'security')
    const seen: string[] = []
    renderPage({ seen })
    expect(seen[0]).toBe('security')
    expect(seen).not.toContain('overview')
    expect(screen.getByTestId('active')).toHaveTextContent('security')
  })

  it('keeps an explicitly-picked tab through an in-place re-navigation, and does not clobber the memory', () => {
    // The destructive sequence: pick a tab in-session, then hit ⌘+, while still
    // on the page. The param drops with the layout alive, so a one-shot restore
    // fell through to the first tab AND the persist effect then overwrote the
    // stored tab with `overview` — losing the preference this feature exists to
    // keep.
    sessionStorage.setItem(KEY, 'security')
    render(
      <MemoryRouter initialEntries={['/page']}>
        <SidePanelLayout title="Test" tabs={TABS} rememberKey="test-page">
          {tab => (
            <div>
              <div data-testid="active">{tab}</div>
              <UrlProbe />
              <ReNavigate to="/page" />
            </div>
          )}
        </SidePanelLayout>
      </MemoryRouter>,
    )
    // An explicit pick clears any one-shot restore state.
    fireEvent.click(screen.getByText('About'))
    expect(screen.getByTestId('active')).toHaveTextContent('about')
    expect(sessionStorage.getItem(KEY)).toBe('about')

    fireEvent.click(screen.getByText('re-navigate'))

    expect(screen.getByTestId('active')).toHaveTextContent('about')
    expect(screen.getByTestId('search')).toHaveTextContent('?tab=about')
    expect(sessionStorage.getItem(KEY)).toBe('about')
  })

  it('keeps a deep-linked tab through an in-place re-navigation', () => {
    // Same defect reached the other way: arriving via ?tab= leaves no restore
    // state at all, so a later param drop had nothing to fall back to.
    render(
      <MemoryRouter initialEntries={['/page?tab=about']}>
        <SidePanelLayout title="Test" tabs={TABS} rememberKey="test-page">
          {tab => (
            <div>
              <div data-testid="active">{tab}</div>
              <UrlProbe />
              <ReNavigate to="/page" />
            </div>
          )}
        </SidePanelLayout>
      </MemoryRouter>,
    )
    expect(screen.getByTestId('active')).toHaveTextContent('about')

    fireEvent.click(screen.getByText('re-navigate'))

    expect(screen.getByTestId('active')).toHaveTextContent('about')
    expect(sessionStorage.getItem(KEY)).toBe('about')
  })

  it('re-syncs the URL when the same route is re-navigated without a param', () => {
    // ⌘+, runs navigate('/settings') and the sidebar entry is the same route,
    // so the layout does NOT unmount — the param just disappears underneath it.
    // A mount-only sync effect could never re-add it, leaving the URL and the
    // shown pane persistently divergent.
    sessionStorage.setItem(KEY, 'security')
    render(
      <MemoryRouter initialEntries={['/page']}>
        <SidePanelLayout title="Test" tabs={TABS} rememberKey="test-page">
          {tab => (
            <div>
              <div data-testid="active">{tab}</div>
              <UrlProbe />
              <ReNavigate to="/page" />
            </div>
          )}
        </SidePanelLayout>
      </MemoryRouter>,
    )
    expect(screen.getByTestId('active')).toHaveTextContent('security')
    expect(screen.getByTestId('search')).toHaveTextContent('?tab=security')

    fireEvent.click(screen.getByText('re-navigate'))

    expect(screen.getByTestId('active')).toHaveTextContent('security')
    expect(screen.getByTestId('search')).toHaveTextContent('?tab=security')
  })

  it('lets an explicit ?tab= win over the remembered tab', () => {
    sessionStorage.setItem(KEY, 'security')
    renderPage({ url: '/page?tab=about' })
    expect(screen.getByTestId('active')).toHaveTextContent('about')
  })

  it('honours deliberately returning to the first tab', () => {
    const { unmount } = renderPage()
    fireEvent.click(screen.getByText('Security'))
    // Picking the first tab deletes the param — the memory must record the
    // first tab, not bounce back to Security on the next visit.
    fireEvent.click(screen.getByText('Overview'))
    expect(screen.getByTestId('active')).toHaveTextContent('overview')
    unmount()

    renderPage()
    expect(screen.getByTestId('active')).toHaveTextContent('overview')
  })

  it('does not bounce away when the user picks the first tab in-session', () => {
    sessionStorage.setItem(KEY, 'security')
    renderPage()
    expect(screen.getByTestId('active')).toHaveTextContent('security')
    fireEvent.click(screen.getByText('Overview'))
    expect(screen.getByTestId('active')).toHaveTextContent('overview')
  })

  it('ignores a remembered tab missing from this roster', () => {
    // e.g. the embedded pane hides Instances, or a tab was removed by an update.
    sessionStorage.setItem(KEY, 'instances')
    renderPage()
    expect(screen.getByTestId('active')).toHaveTextContent('overview')
  })

  it('stays stateless without a rememberKey', () => {
    const { unmount } = renderPage({ rememberKey: undefined })
    fireEvent.click(screen.getByText('Security'))
    expect(sessionStorage.length).toBe(0)
    unmount()

    renderPage({ rememberKey: undefined })
    expect(screen.getByTestId('active')).toHaveTextContent('overview')
  })

  it('survives sessionStorage access throwing', () => {
    // safeStorage warns in dev on a failed write — expected here, keep it quiet.
    vi.spyOn(console, 'warn').mockImplementation(() => {})
    const getItem = vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
      throw new DOMException('denied', 'SecurityError')
    })
    const setItem = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new DOMException('denied', 'SecurityError')
    })
    renderPage()
    expect(screen.getByTestId('active')).toHaveTextContent('overview')
    fireEvent.click(screen.getByText('Security'))
    expect(screen.getByTestId('active')).toHaveTextContent('security')
    getItem.mockRestore()
    setItem.mockRestore()
  })
})
