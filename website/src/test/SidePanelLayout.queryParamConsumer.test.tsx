/**
 * The `basePath` path-navigation seam is OPT-IN. SidePanelLayout is shared by
 * Developer / Capabilities / Schedule / Webhooks, and only Settings passes
 * basePath — every other consumer must keep the `?tab=` query-param contract
 * byte-for-byte: reads come from `?tab=`, writes go to `?tab=` (never a path
 * segment), the desktop "first tab = no param" convention holds, and path
 * segments under the page's own route mean nothing.
 *
 * These tests mount at a Developer-style route (`/developer`, no basePath) and
 * assert the ACTUAL URL after every interaction via a location probe — the
 * regression this pins is a seam refactor that starts writing
 * `/developer/<tab>` paths (404s under the page's non-splat route) or starts
 * reading segments it was never given authority over.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, screen, cleanup, fireEvent } from '@testing-library/react'
import { MemoryRouter, useLocation, useNavigationType } from 'react-router-dom'
import SidePanelLayout, { type SidePanelTab } from '../components/SidePanelLayout'
import { SUBNAV_PUSH_STATE } from '../components/subNavParams'

let mobile = false
vi.mock('../hooks/useIsMobile', () => ({ useIsMobile: () => mobile }))

const TABS: SidePanelTab[] = [
  { key: 'overview', label: 'Overview', icon: null },
  { key: 'security', label: 'Security', icon: null, group: 'System', hostsSubNav: true },
  { key: 'about', label: 'About', icon: null, group: 'System' },
]

function LocationProbe() {
  const loc = useLocation()
  const navType = useNavigationType()
  const marker = (loc.state as Record<string, unknown> | null)?.[SUBNAV_PUSH_STATE]
  return (
    <>
      <div data-testid="loc">{loc.pathname + loc.search}</div>
      <div data-testid="nav">{`${navType}|${marker ? 'marked' : 'unmarked'}`}</div>
    </>
  )
}

function renderAt(url: string, rememberKey = 'test-query-consumer') {
  return render(
    <MemoryRouter initialEntries={[url]}>
      <SidePanelLayout title="Developer" tabs={TABS} rememberKey={rememberKey}>
        {tab => <div data-testid="pane">{tab}</div>}
      </SidePanelLayout>
      <LocationProbe />
    </MemoryRouter>,
  )
}

const loc = () => screen.getByTestId('loc').textContent
const nav = () => screen.getByTestId('nav').textContent

describe('query-param consumer (no basePath) — the path seam is opt-in', () => {
  beforeEach(() => { mobile = false; sessionStorage.clear() })
  afterEach(() => { vi.restoreAllMocks(); cleanup() })

  it('reads the active tab from ?tab= on desktop', () => {
    renderAt('/developer?tab=about')
    expect(screen.getByTestId('pane').textContent).toBe('about')
    expect(loc()).toBe('/developer?tab=about')
  })

  it('writes tab selection as ?tab=, never a path segment', () => {
    renderAt('/developer')
    fireEvent.click(screen.getByRole('button', { name: 'About' }))
    expect(screen.getByTestId('pane').textContent).toBe('about')
    // The pathname must be untouched: a seam leak writing /developer/about
    // would 404 under the page's non-splat route.
    expect(loc()).toBe('/developer?tab=about')
  })

  it('keeps the desktop "first tab = no param" convention', () => {
    renderAt('/developer?tab=about')
    fireEvent.click(screen.getByRole('button', { name: 'Overview' }))
    expect(screen.getByTestId('pane').textContent).toBe('overview')
    expect(loc()).toBe('/developer')
  })

  it('preserves unrelated query params across a tab switch', () => {
    renderAt('/developer?foo=1')
    fireEvent.click(screen.getByRole('button', { name: 'About' }))
    expect(loc()).toContain('foo=1')
    expect(loc()).toContain('tab=about')
    expect(loc()).toMatch(/^\/developer\?/)
  })

  it('restores the remembered tab by writing ?tab=, not a path', () => {
    sessionStorage.setItem('kirocrew:sidepanel-tab:test-query-consumer', 'about')
    renderAt('/developer')
    expect(screen.getByTestId('pane').textContent).toBe('about')
    expect(loc()).toBe('/developer?tab=about')
  })

  it('ignores path segments entirely — only basePath consumers read them', () => {
    // A stray deeper path under the page's route (mangled link) must not be
    // interpreted as a tab selection.
    renderAt('/developer/security')
    expect(screen.getByTestId('pane').textContent).toBe('overview')
  })

  it('mobile drill-in writes ?tab= and back deletes it — pathname untouched', () => {
    mobile = true
    renderAt('/developer')
    fireEvent.click(screen.getByRole('button', { name: 'Security' }))
    expect(screen.getByTestId('pane').textContent).toBe('security')
    expect(loc()).toBe('/developer?tab=security')
    fireEvent.click(screen.getByRole('button', { name: /Developer/ }))
    expect(screen.queryByTestId('pane')).toBeNull()
    expect(loc()).toBe('/developer')
  })

  it('mobile drill-in PUSHES with the SUBNAV_PUSH_STATE marker and back POPS it', () => {
    // The history mechanics are part of the legacy contract, not just the
    // URL: the drill-in must be a real pushed entry carrying the marker (so
    // the platform back gesture pops one level), and the back control must
    // navigate(-1) — a replace-write would leave [root, root] twins and the
    // next back-swipe would visibly do nothing. Asserted here because a
    // basePath-seam refactor scoping the marker to path mode would pass
    // every URL-only assertion in this file.
    mobile = true
    renderAt('/developer')
    fireEvent.click(screen.getByRole('button', { name: 'Security' }))
    expect(nav()).toBe('PUSH|marked')
    fireEvent.click(screen.getByRole('button', { name: /Developer/ }))
    expect(nav()).toBe('POP|unmarked')
    expect(loc()).toBe('/developer')
  })
})
