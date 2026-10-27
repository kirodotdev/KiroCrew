/**
 * The Back/Forward arrows' history menu (#17453): right-click (or long-press)
 * an arrow for a named list of the destinations behind or ahead of it, and
 * jump straight to one.
 *
 * Real `<BrowserRouter>` over jsdom's history, like NavHistoryArrows.test.tsx,
 * because the menu is keyed by react-router's `history.state.idx`. The shell's
 * naming effect is modelled by `Named`, which names each entry it lands on the
 * way `appShell.tsx` does.
 */
import React from 'react'
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, screen, cleanup, fireEvent, waitFor, act } from '@testing-library/react'
import { BrowserRouter, Routes, Route, useLocation, useNavigate } from 'react-router-dom'
import { NavHistoryArrows, RouteHistoryTracker } from '../components/NavHistoryArrows'
import {
  NavigationLeaveGuardProvider,
  NavigationBackGuard,
  useRegisterNavigationLeaveGuard,
  usePublishNavigationStake,
} from '../components/NavigationLeaveGuard'
import { _resetRouteHistoryPositionForTest } from '../lib/routeHistoryPosition'
import {
  _resetRouteHistoryLabelsForTest,
  _simulateLabelsReloadForTest,
  labelCurrentRouteEntry,
  routeHistoryMenuEntries,
} from '../lib/routeHistoryLabels'

const NAMES: Record<string, string> = { '/': 'Home', '/a': 'Alpha', '/b': 'Beta', '/c': 'Gamma', '/drafty': 'Drafty' }

/** Names every entry it lands on, like the shell's tab-title effect. `/nameless`
 *  has no name, like a route that matches no rail row. */
function Named() {
  const location = useLocation()
  React.useEffect(() => {
    labelCurrentRouteEntry(NAMES[location.pathname] ?? '')
  }, [location.key, location.pathname])
  return null
}

function GoTo({ to }: { to: string }) {
  const navigate = useNavigate()
  return <button onClick={() => navigate(to)}>{`go ${to}`}</button>
}

const routes = ['/', '/a', '/b', '/c', '/nameless']

function Shell({ children }: { children?: React.ReactNode }) {
  return (
    <BrowserRouter>
      <RouteHistoryTracker />
      <Named />
      <NavHistoryArrows />
      {routes.map(r => <GoTo key={r} to={r} />)}
      {children}
      <Routes>
        {routes.map(r => <Route key={r} path={r} element={<div data-testid="page">{r}</div>} />)}
      </Routes>
    </BrowserRouter>
  )
}

const backBtn = () => screen.getByRole('button', { name: 'Back' })
const forwardBtn = () => screen.getByRole('button', { name: 'Forward' })
const page = () => screen.getByTestId('page').textContent
const items = () => screen.getAllByRole('menuitem').map(b => b.textContent)

async function go(to: string) {
  fireEvent.click(screen.getByRole('button', { name: `go ${to}` }))
  await waitFor(() => expect(page()).toBe(to))
}

beforeEach(() => {
  window.history.replaceState(null, '', '/')
  _resetRouteHistoryPositionForTest()
  _resetRouteHistoryLabelsForTest()
})
afterEach(() => {
  cleanup()
  vi.useRealTimers()
})

describe('NavHistoryArrows history menu', () => {
  it('right-click Back lists earlier destinations by name, nearest first', async () => {
    render(<Shell />)
    await go('/a')
    await go('/b')
    await go('/c')
    fireEvent.contextMenu(backBtn())
    expect(screen.getByRole('menu', { name: 'Back history' })).toBeInTheDocument()
    expect(items()).toEqual(['Beta', 'Alpha', 'Home'])
  })

  it('picking an entry jumps straight there, and Forward then lists the way back', async () => {
    render(<Shell />)
    await go('/a')
    await go('/b')
    await go('/c')
    fireEvent.contextMenu(backBtn())
    fireEvent.click(screen.getByRole('menuitem', { name: 'Alpha' }))
    await waitFor(() => expect(page()).toBe('/a'))
    expect(screen.queryByRole('menu')).not.toBeInTheDocument()

    fireEvent.contextMenu(forwardBtn())
    expect(screen.getByRole('menu', { name: 'Forward history' })).toBeInTheDocument()
    expect(items()).toEqual(['Beta', 'Gamma'])
    fireEvent.click(screen.getByRole('menuitem', { name: 'Gamma' }))
    await waitFor(() => expect(page()).toBe('/c'))
  })

  it('shows at most ten entries', async () => {
    render(<Shell />)
    for (let i = 0; i < 7; i++) {
      await go('/a')
      await go('/b')
    }
    fireEvent.contextMenu(backBtn())
    expect(items()).toHaveLength(10)
  })

  it('leaves out an entry with no name, but still jumps the right number of steps', async () => {
    render(<Shell />)
    await go('/a')
    await go('/nameless')
    await go('/b')
    fireEvent.contextMenu(backBtn())
    expect(items()).toEqual(['Alpha', 'Home'])
    fireEvent.click(screen.getByRole('menuitem', { name: 'Alpha' }))
    await waitFor(() => expect(page()).toBe('/a'))
  })

  it('never lists a Forward branch a push truncated', async () => {
    render(<Shell />)
    await go('/a')
    await go('/b')
    fireEvent.contextMenu(backBtn())
    fireEvent.click(screen.getByRole('menuitem', { name: 'Home' }))
    await waitFor(() => expect(page()).toBe('/'))
    await go('/c')
    expect(routeHistoryMenuEntries(1)).toEqual([])
    fireEvent.contextMenu(backBtn())
    expect(items()).toEqual(['Home'])
  })

  it('Escape closes the menu and hands focus back to the arrow', async () => {
    render(<Shell />)
    await go('/a')
    fireEvent.contextMenu(backBtn())
    expect(screen.getByRole('menu')).toBeInTheDocument()
    fireEvent.keyDown(document.activeElement ?? document.body, { key: 'Escape' })
    await waitFor(() => expect(screen.queryByRole('menu')).not.toBeInTheDocument())
    await waitFor(() => expect(document.activeElement).toBe(backBtn()))
    expect(page()).toBe('/a')
  })

  it('after a right-click menu closes, the next bare click still steps', async () => {
    render(<Shell />)
    await go('/a')
    await go('/b')
    fireEvent.contextMenu(backBtn())
    fireEvent.keyDown(document.activeElement ?? document.body, { key: 'Escape' })
    await waitFor(() => expect(screen.queryByRole('menu')).not.toBeInTheDocument())
    // A click with no pointerdown or keydown first, as assistive tech sends.
    fireEvent.click(backBtn())
    await waitFor(() => expect(page()).toBe('/a'))
  })

  it('a plain click still steps one entry and opens no menu', async () => {
    render(<Shell />)
    await go('/a')
    await go('/b')
    fireEvent.click(backBtn())
    await waitFor(() => expect(page()).toBe('/a'))
    expect(screen.queryByRole('menu')).not.toBeInTheDocument()
  })

  it('a touch long-press opens the menu, and its release does not also step', async () => {
    render(<Shell />)
    await go('/a')
    await go('/b')
    // Held before the menu opens: an open modal menu hides the rest of the
    // page from the accessibility tree.
    const back = backBtn()
    vi.useFakeTimers()
    fireEvent.pointerDown(back, { pointerType: 'touch' })
    // The shared context menu's own long-press threshold.
    act(() => { vi.advanceTimersByTime(800) })
    expect(items()).toEqual(['Alpha', 'Home'])
    fireEvent.pointerUp(back, { pointerType: 'touch' })
    fireEvent.click(back)
    vi.useRealTimers()
    expect(page()).toBe('/b')
    expect(screen.getByRole('menu')).toBeInTheDocument()
  })

  it('a short touch is a plain step', async () => {
    render(<Shell />)
    await go('/a')
    vi.useFakeTimers()
    fireEvent.pointerDown(backBtn(), { pointerType: 'touch' })
    act(() => { vi.advanceTimersByTime(100) })
    fireEvent.pointerUp(backBtn(), { pointerType: 'touch' })
    vi.useRealTimers()
    fireEvent.click(backBtn())
    await waitFor(() => expect(page()).toBe('/'))
    expect(screen.queryByRole('menu')).not.toBeInTheDocument()
  })

  it('an open menu closes when another way of navigating moves the stack', async () => {
    render(<Shell />)
    await go('/a')
    await go('/b')
    fireEvent.contextMenu(backBtn())
    expect(screen.getByRole('menu')).toBeInTheDocument()
    act(() => { window.history.back() })
    await waitFor(() => expect(page()).toBe('/a'))
    expect(screen.queryByRole('menu')).not.toBeInTheDocument()
  })

  it('a guard duplicate drops the name a truncated entry had at its index', async () => {
    render(<Shell />)
    await go('/a')
    await go('/b')
    await go('/c')
    // Gamma is named at idx 3. Model the guard's duplicate reusing idx 3 (the
    // shell names the entry on screen before anything else prunes it).
    window.history.replaceState({ idx: 3, usr: { __navigationLeaveTrap: true }, key: 'trap' }, '', '/b')
    labelCurrentRouteEntry('Gamma')
    window.history.pushState({ idx: 4, usr: null, key: 'next' }, '', '/c')
    expect(routeHistoryMenuEntries(-1).map(e => e.label)).toEqual(['Beta', 'Alpha', 'Home'])
  })

  it.each(['navigate', 'back_forward'])('names are dropped on a %s arrival', async (type) => {
    render(<Shell />)
    await go('/a')
    await go('/b')
    const spy = vi.spyOn(performance, 'getEntriesByType').mockReturnValue([{ type } as unknown as PerformanceEntry])
    try {
      _simulateLabelsReloadForTest()
    } finally {
      spy.mockRestore()
    }
    expect(routeHistoryMenuEntries(-1)).toEqual([])
  })

  it('names survive a reload of the same tab', async () => {
    render(<Shell />)
    await go('/a')
    await go('/b')
    _simulateLabelsReloadForTest()
    expect(routeHistoryMenuEntries(-1).map(e => e.label)).toEqual(['Alpha', 'Home'])
  })
})

describe('history menu and the draft guard', () => {
  function DraftyPage({ guard }: { guard: () => boolean }) {
    useRegisterNavigationLeaveGuard(guard)
    usePublishNavigationStake(true)
    return <div data-testid="page">/drafty</div>
  }

  it('a jump past the trap asks first, and a veto keeps the draft', async () => {
    const guard = vi.fn(() => false)
    render(
      <NavigationLeaveGuardProvider>
        <BrowserRouter>
          <NavigationBackGuard />
          <RouteHistoryTracker />
          <Named />
          <NavHistoryArrows />
          <GoTo to="/a" />
          <GoTo to="/drafty" />
          <Routes>
            <Route path="/" element={<div data-testid="page">/</div>} />
            <Route path="/a" element={<div data-testid="page">/a</div>} />
            <Route path="/drafty" element={<DraftyPage guard={guard} />} />
          </Routes>
        </BrowserRouter>
      </NavigationLeaveGuardProvider>,
    )
    await go('/a')
    await go('/drafty')
    // The guard's duplicate is never listed.
    fireEvent.contextMenu(backBtn())
    expect(items()).toEqual(['Alpha', 'Home'])
    fireEvent.click(screen.getByRole('menuitem', { name: 'Home' }))
    expect(guard).toHaveBeenCalledTimes(1)
    expect(page()).toBe('/drafty')

    guard.mockReturnValue(true)
    fireEvent.contextMenu(backBtn())
    fireEvent.click(screen.getByRole('menuitem', { name: 'Home' }))
    await waitFor(() => expect(page()).toBe('/'))
    expect(guard).toHaveBeenCalledTimes(2)
  })
})
