/**
 * The route-history position store + the top-bar Back/Forward arrows (#8258).
 *
 * Uses a real `<BrowserRouter>` over jsdom's history rather than MemoryRouter,
 * because the subject is react-router's `history.state.idx` bookkeeping and the
 * platform stack — a memory history writes neither. jsdom applies `back()` /
 * `forward()` in a task, so every assertion after one is awaited.
 */
import React from 'react'
import { describe, it, expect, beforeEach, afterEach } from 'vitest'
import { render, screen, cleanup, fireEvent, waitFor } from '@testing-library/react'
import { BrowserRouter, Routes, Route, useNavigate } from 'react-router-dom'
import { NavHistoryArrows, RouteHistoryTracker } from '../components/NavHistoryArrows'
import {
  NavigationLeaveGuardProvider,
  NavigationBackGuard,
  useRegisterNavigationLeaveGuard,
  usePublishNavigationStake,
} from '../components/NavigationLeaveGuard'
import {
  _resetRouteHistoryPositionForTest,
  _simulateReloadForTest,
  canGoBack,
  canGoForward,
  getRouteHistoryPosition,
  recordRouteNavigation,
  routerEntry,
  subscribeRouteHistoryPosition,
} from '../lib/routeHistoryPosition'

function GoTo({ to }: { to: string }) {
  const navigate = useNavigate()
  return <button onClick={() => navigate(to)}>{`go ${to}`}</button>
}

const renderShell = () =>
  render(
    <BrowserRouter>
      <RouteHistoryTracker />
      <NavHistoryArrows />
      <GoTo to="/chat" />
      <GoTo to="/settings" />
      <Routes>
        <Route path="/" element={<div data-testid="page">home</div>} />
        <Route path="/chat" element={<div data-testid="page">chat</div>} />
        <Route path="/settings" element={<div data-testid="page">settings</div>} />
      </Routes>
    </BrowserRouter>,
  )

const backBtn = () => screen.getByRole('button', { name: 'Back' })
const forwardBtn = () => screen.getByRole('button', { name: 'Forward' })
const page = () => screen.getByTestId('page').textContent

beforeEach(() => {
  // A fresh document position: BrowserRouter re-stamps idx 0 on this entry at
  // mount. The stack itself cannot be truncated from a test, so each case
  // starts from a replace-written root, the same ritual backNavLeaveGuard uses.
  window.history.replaceState(null, '', '/')
  _resetRouteHistoryPositionForTest()
})
afterEach(cleanup)

describe('NavHistoryArrows', () => {
  it('renders both arrows disabled on a fresh document', () => {
    renderShell()
    expect(backBtn()).toBeDisabled()
    expect(forwardBtn()).toBeDisabled()
  })

  it('enables Back after a push, and walking back re-enables Forward', async () => {
    renderShell()
    fireEvent.click(screen.getByRole('button', { name: 'go /chat' }))
    await waitFor(() => expect(page()).toBe('chat'))
    expect(backBtn()).toBeEnabled()
    expect(forwardBtn()).toBeDisabled()

    fireEvent.click(backBtn())
    await waitFor(() => expect(page()).toBe('home'))
    expect(backBtn()).toBeDisabled()
    expect(forwardBtn()).toBeEnabled()

    fireEvent.click(forwardBtn())
    await waitFor(() => expect(page()).toBe('chat'))
    expect(backBtn()).toBeEnabled()
    expect(forwardBtn()).toBeDisabled()
  })

  it('a push from mid-stack truncates the Forward branch', async () => {
    renderShell()
    fireEvent.click(screen.getByRole('button', { name: 'go /chat' }))
    await waitFor(() => expect(page()).toBe('chat'))
    fireEvent.click(backBtn())
    await waitFor(() => expect(forwardBtn()).toBeEnabled())

    // Standing on home with /chat ahead: a new push destroys that branch.
    fireEvent.click(screen.getByRole('button', { name: 'go /settings' }))
    await waitFor(() => expect(page()).toBe('settings'))
    expect(forwardBtn()).toBeDisabled()
    expect(backBtn()).toBeEnabled()
  })

  it('labels both arrows with their chords for assistive tech', () => {
    renderShell()
    // jsdom reports a non-Mac platform, so the meta chord renders as Control.
    expect(backBtn()).toHaveAttribute('aria-keyshortcuts', 'Control+ArrowLeft')
    expect(forwardBtn()).toHaveAttribute('aria-keyshortcuts', 'Control+ArrowRight')
    // The tooltip carries the display chord, so sighted users can discover it.
    expect(backBtn()).toHaveAttribute('title', 'Back \u00b7 Ctrl+\u2190')
  })

  it('advertises no chord while shortcuts are globally disabled', () => {
    // The keydown handler bails on the same toggle, so an advertisement here
    // would teach a keypress that does nothing (the useNavShortcutHint rule).
    localStorage.setItem('mc-keyboard-shortcuts', '0')
    renderShell()
    expect(backBtn()).not.toHaveAttribute('aria-keyshortcuts')
    expect(forwardBtn()).not.toHaveAttribute('aria-keyshortcuts')
    expect(backBtn()).toHaveAttribute('title', 'Back')
    localStorage.removeItem('mc-keyboard-shortcuts')
  })

  it('follows a user rebind and an unbind — never the factory default', () => {
    // A rebound Back advertises the NEW chord to assistive tech, and since the
    // catalog tooltip can only spell the factory chord, drops to the bare
    // label rather than teaching a chord that no longer fires. An unbound
    // Forward advertises nothing.
    localStorage.setItem('mc-shortcut-overrides', JSON.stringify({
      'history-back': { key: 'b', mod: true, shift: true },
      'history-forward': null,
    }))
    try {
      renderShell()
      expect(backBtn()).toHaveAttribute('aria-keyshortcuts', 'Control+Shift+B')
      expect(backBtn()).toHaveAttribute('title', 'Back')
      expect(forwardBtn()).not.toHaveAttribute('aria-keyshortcuts')
      expect(forwardBtn()).toHaveAttribute('title', 'Forward')
    } finally {
      localStorage.removeItem('mc-shortcut-overrides')
    }
  })
})

describe('NavHistoryArrows — draft guard interplay', () => {
  /** A page holding work: registers the veto and publishes the stake, like a
   *  dirty prompt editor. The guard mock IS the confirm — its call count is the
   *  number of times the user was asked. */
  function DraftyPage({ guard }: { guard: () => boolean }) {
    useRegisterNavigationLeaveGuard(guard)
    usePublishNavigationStake(true)
    return <div data-testid="page">drafty</div>
  }

  const renderGuardedShell = (guard: () => boolean) =>
    render(
      <NavigationLeaveGuardProvider>
        <BrowserRouter>
          <NavigationBackGuard />
          <RouteHistoryTracker />
          <NavHistoryArrows />
          <GoTo to="/drafty" />
          <GoTo to="/other" />
          <Routes>
            <Route path="/" element={<div data-testid="page">home</div>} />
            <Route path="/drafty" element={<DraftyPage guard={guard} />} />
            <Route path="/other" element={<div data-testid="page">other</div>} />
          </Routes>
        </BrowserRouter>
      </NavigationLeaveGuardProvider>,
    )

  it('asks before the pop when the Back trap could not arm, and a veto keeps the draft', async () => {
    // Build the gap the trap documents: a Forward branch above the dirty page.
    // The guard refuses to push a duplicate there (a push would truncate the
    // user's branch), so WITHOUT the up-front ask this pop would silently
    // unmount the draft — the exact GPT-review scenario.
    const guard = vi.fn(() => false)
    renderGuardedShell(guard)
    fireEvent.click(screen.getByRole('button', { name: 'go /drafty' }))
    await waitFor(() => expect(page()).toBe('drafty'))
    fireEvent.click(screen.getByRole('button', { name: 'go /other' }))
    await waitFor(() => expect(page()).toBe('other'))
    window.history.back()
    await waitFor(() => expect(page()).toBe('drafty'))

    fireEvent.click(backBtn())
    expect(guard).toHaveBeenCalledTimes(1)
    // Vetoed synchronously: no navigation was issued at all.
    expect(page()).toBe('drafty')

    // Allowed: the same click carries the pop out.
    guard.mockReturnValue(true)
    fireEvent.click(backBtn())
    await waitFor(() => expect(page()).toBe('home'))
  })

  it('asks exactly once when the trap IS armed — the trap prompts, the arrow does not', async () => {
    const guard = vi.fn(() => false)
    renderGuardedShell(guard)
    // A real PUSH first, so the guard can calibrate and arm (see its contract).
    fireEvent.click(screen.getByRole('button', { name: 'go /drafty' }))
    await waitFor(() => expect(page()).toBe('drafty'))
    // The stake published on mount lets the guard push its duplicate; the click
    // then pops that duplicate and the guard asks inside the popstate.
    fireEvent.click(backBtn())
    await waitFor(() => expect(guard).toHaveBeenCalledTimes(1))
    // Vetoed: still on the draft, at the same address.
    expect(page()).toBe('drafty')
  })

  it('never offers the trap duplicate as a Forward destination', async () => {
    // The GPT round-8 blocker: the trap is a real stack entry one above the
    // dirty page. If it raised the watermark, an accepted Back then Forward
    // twice would land ON the trap, where the guard's carry-through no-ops at
    // the stack top and leaks its self-move flag — the next native Back would
    // then skip the draft ask. So on the page beneath an armed trap, Forward
    // must read as absent, and on the trap itself the position is the page's.
    const guard = vi.fn(() => true)
    renderGuardedShell(guard)
    fireEvent.click(screen.getByRole('button', { name: 'go /drafty' }))
    await waitFor(() => expect(page()).toBe('drafty'))
    // Trap armed (stake published on mount): we are standing ON the trap now,
    // one above the page. Forward must be disabled, Back enabled (home below).
    await waitFor(() => expect(routerEntry().idx).toBe(2))
    expect(forwardBtn()).toBeDisabled()
    expect(backBtn()).toBeEnabled()
    // Accept a Back: the trap is consumed and the guard carries the pop through
    // to home. From home, the only entry above that is a DESTINATION is the
    // drafty page itself — never the trap beyond it.
    fireEvent.click(backBtn())
    await waitFor(() => expect(page()).toBe('home'))
    expect(forwardBtn()).toBeEnabled()
    fireEvent.click(forwardBtn())
    await waitFor(() => expect(page()).toBe('drafty'))
    // Re-armed trap or not, Forward from the drafty page never reaches a trap.
    await waitFor(() => expect(forwardBtn()).toBeDisabled())
  })
})

describe('routeHistoryPosition store', () => {
  it('keeps snapshot identity stable across no-change reports', () => {
    window.history.replaceState({ idx: 0 }, '', '/')
    recordRouteNavigation('POP')
    const first = getRouteHistoryPosition()
    recordRouteNavigation('POP')
    expect(getRouteHistoryPosition()).toBe(first)
  })

  it('notifies subscribers only when a value flips', () => {
    window.history.replaceState({ idx: 0 }, '', '/')
    let calls = 0
    const unsub = subscribeRouteHistoryPosition(() => { calls += 1 })
    recordRouteNavigation('POP')
    expect(calls).toBe(0)
    window.history.replaceState({ idx: 1 }, '', '/chat')
    recordRouteNavigation('PUSH')
    expect(calls).toBe(1)
    expect(getRouteHistoryPosition()).toEqual({ canGoBack: true, canGoForward: false })
    unsub()
  })

  it('claims nothing when the entry carries no router index', () => {
    window.history.replaceState(null, '', '/')
    recordRouteNavigation('POP')
    expect(getRouteHistoryPosition()).toEqual({ canGoBack: false, canGoForward: false })
    expect(canGoBack()).toBe(false)
    expect(canGoForward()).toBe(false)
  })

  it('treats a NaN idx as no bookkeeping — the wiped-state regression', () => {
    // A raw `history.replaceState({}, …)` (the ChatPage token/prefill strippers
    // before their fix) erases react-router's idx; the router then computes
    // every later idx as NaN. NaN passes a typeof check but poisons every
    // comparison — the arrows must read it as "unknown", never as a number.
    window.history.replaceState({ idx: Number.NaN }, '', '/')
    recordRouteNavigation('PUSH')
    expect(getRouteHistoryPosition()).toEqual({ canGoBack: false, canGoForward: false })
    expect(canGoBack()).toBe(false)
    expect(canGoForward()).toBe(false)
  })

  it('canGoBack reads the platform live; canGoForward needs the watermark', () => {
    // idx present before any report: Back answers from the entry itself…
    window.history.replaceState({ idx: 2 }, '', '/chat')
    expect(canGoBack()).toBe(true)
    // …Forward stays on the conservative side until a report establishes it.
    expect(canGoForward()).toBe(false)
    recordRouteNavigation('POP')          // watermark = 2
    window.history.replaceState({ idx: 1 }, '', '/')
    recordRouteNavigation('POP')          // idx 1 < watermark 2
    expect(canGoForward()).toBe(true)
  })

  it('remembers the Forward watermark across a reload via sessionStorage (per tab)', () => {
    // Build a stack of three and walk back to the middle: watermark 2, idx 1.
    window.history.replaceState({ idx: 2 }, '', '/c')
    recordRouteNavigation('PUSH')
    window.history.replaceState({ idx: 1 }, '', '/b')
    recordRouteNavigation('POP')
    expect(getRouteHistoryPosition()).toEqual({ canGoBack: true, canGoForward: true })
    expect(window.sessionStorage.getItem('mc-route-history-max-idx')).toBe('2')

    // A reload restarts the module but keeps the tab: the first report after it
    // is a POP at the same idx. Without persistence this read "no Forward".
    _simulateReloadForTest()
    recordRouteNavigation('POP')
    expect(getRouteHistoryPosition()).toEqual({ canGoBack: true, canGoForward: true })
  })

  it('collapses the watermark on a back_forward arrival — and the collapse survives a reload', () => {
    // Return by browser Back after an external same-tab link: the external
    // PUSH truncated this app's Forward branch in ANOTHER document, so the
    // persisted watermark (2) may name entries that no longer exist — or the
    // external page itself. Forward must read "none"; Back is unaffected.
    window.history.replaceState({ idx: 2 }, '', '/c')
    recordRouteNavigation('PUSH')                       // persisted watermark = 2
    const original = performance.getEntriesByType
    const arriveBy = (type: string) => {
      performance.getEntriesByType = ((kind: string) =>
        kind === 'navigation' ? [{ type } as unknown as PerformanceEntry] : []) as typeof performance.getEntriesByType
    }
    try {
      arriveBy('back_forward')
      window.history.replaceState({ idx: 1 }, '', '/b')
      _simulateReloadForTest()
      recordRouteNavigation('POP')
      expect(getRouteHistoryPosition()).toEqual({ canGoBack: true, canGoForward: false })
      expect(canGoForward()).toBe(false)
      // The collapse is PERSISTED (watermark now 1, not 2), so a plain reload of
      // this document — which trusts the stored watermark — cannot resurrect the
      // stale branch and hand Forward to the external page.
      expect(window.sessionStorage.getItem('mc-route-history-max-idx')).toBe('1')
      arriveBy('reload')
      _simulateReloadForTest()
      recordRouteNavigation('POP')
      expect(getRouteHistoryPosition()).toEqual({ canGoBack: true, canGoForward: false })
      expect(canGoForward()).toBe(false)
      // Forward re-appears only for entries this document is then seen ON: a
      // Back to idx 0 leaves idx 1 — where we just were — as a real destination…
      window.history.replaceState({ idx: 0 }, '', '/a')
      recordRouteNavigation('POP')
      expect(getRouteHistoryPosition()).toEqual({ canGoBack: false, canGoForward: true })
      // …and an in-app PUSH re-establishes the frontier as before.
      window.history.replaceState({ idx: 1 }, '', '/d')
      recordRouteNavigation('PUSH')
      expect(getRouteHistoryPosition()).toEqual({ canGoBack: true, canGoForward: false })
    } finally {
      performance.getEntriesByType = original
    }
  })

  it('collapses the watermark after a BFCache restore (pageshow persisted) — persisted, and re-armed only by entries seen since', () => {
    // The back_forward case WITHOUT a document start: the browser revives the
    // frozen page, module state survives, navigation timing still describes the
    // original load — so the arrival-type check cannot see it. `pageshow` with
    // `persisted` is the only signal, and it must collapse the same way.
    window.history.replaceState({ idx: 2 }, '', '/c')
    recordRouteNavigation('PUSH')
    window.history.replaceState({ idx: 1 }, '', '/b')
    recordRouteNavigation('POP')
    expect(getRouteHistoryPosition().canGoForward).toBe(true)
    // The test DOM's PageTransitionEvent does not carry `persisted`, so build the
    // event the way a browser would present it: a pageshow whose `persisted` is
    // a real own property.
    const pageshow = (persisted: boolean) =>
      Object.defineProperty(new Event('pageshow'), 'persisted', { value: persisted })
    window.dispatchEvent(pageshow(true))
    expect(getRouteHistoryPosition()).toEqual({ canGoBack: true, canGoForward: false })
    expect(canGoForward()).toBe(false)
    // Persisted at the collapsed value, so a later reload cannot resurrect '2'.
    expect(window.sessionStorage.getItem('mc-route-history-max-idx')).toBe('1')
    // A non-persisted pageshow (ordinary first show) changes nothing further…
    window.dispatchEvent(pageshow(false))
    expect(getRouteHistoryPosition().canGoForward).toBe(false)
    // …and an in-app PUSH then Back re-establishes a Forward that is real.
    window.history.replaceState({ idx: 2 }, '', '/d')
    recordRouteNavigation('PUSH')
    window.history.replaceState({ idx: 1 }, '', '/b')
    recordRouteNavigation('POP')
    expect(getRouteHistoryPosition().canGoForward).toBe(true)
  })

  it('discards the persisted watermark on a fresh navigate arrival — the branch it described is gone', () => {
    // A typed URL, `location.href = …`, or a `_blank` popout cloning this tab's
    // sessionStorage all TRUNCATE the Forward branch while react-router restarts
    // at idx 0. Restoring the watermark there would enable a Forward that goes
    // nowhere (the round-10 Opus/GPT/Design finding). jsdom exposes no
    // navigation timing entry by default, so model the arrival type explicitly.
    window.history.replaceState({ idx: 2 }, '', '/c')
    recordRouteNavigation('PUSH')                       // persisted watermark = 2
    const original = performance.getEntriesByType
    performance.getEntriesByType = ((type: string) =>
      type === 'navigation' ? [{ type: 'navigate' } as unknown as PerformanceEntry] : []) as typeof performance.getEntriesByType
    try {
      window.history.replaceState({ idx: 0 }, '', '/')   // fresh document, router restarts
      _simulateReloadForTest()                            // module init on the new document
      recordRouteNavigation('POP')
      expect(getRouteHistoryPosition()).toEqual({ canGoBack: false, canGoForward: false })
      expect(canGoForward()).toBe(false)
      // …and the stale value is cleared, so a later reload of THIS document
      // cannot resurrect it either.
      expect(window.sessionStorage.getItem('mc-route-history-max-idx')).toBe('0')
    } finally {
      performance.getEntriesByType = original
    }
  })
})
