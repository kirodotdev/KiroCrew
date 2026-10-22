/**
 * The desktop shell's relayed swipe: on macOS, Logitech's Options software hands
 * a mouse's Back/Forward buttons over as swipes, and the shell relays each one
 * as a route-history step. These pin that the dashboard takes it the way it
 * takes the top-bar arrows: the same step, the same no-ops, the same draft ask.
 *
 * A real `<BrowserRouter>` over jsdom's history, like NavHistoryArrows.test:
 * jsdom applies `back()` / `forward()` in a task, so assertions after a step
 * are awaited.
 */
import React from 'react'
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, screen, cleanup, fireEvent, waitFor } from '@testing-library/react'
import { BrowserRouter, Routes, Route, useNavigate } from 'react-router-dom'
import { RouteHistoryTracker } from '../components/NavHistoryArrows'
import {
  NavigationLeaveGuardProvider,
  NavigationBackGuard,
  useRegisterNavigationLeaveGuard,
  usePublishNavigationStake,
} from '../components/NavigationLeaveGuard'
import { _resetRouteHistoryPositionForTest } from '../lib/routeHistoryPosition'
import { useNativeHistoryStep } from '../shell/platform/nativeHistoryStep'

type Relay = (delta: unknown) => void

let relay: Relay | null = null
const unsubscribe = vi.fn()
const onHistoryStep = vi.fn((cb: Relay) => {
  relay = cb
  return unsubscribe
})

function GoTo({ to }: { to: string }) {
  const navigate = useNavigate()
  return <button onClick={() => navigate(to)}>{`go ${to}`}</button>
}

function NativeStep({ narrow }: { narrow: boolean }) {
  useNativeHistoryStep(narrow)
  return null
}

/** A page holding work: registers the veto and publishes the stake, like a
 *  dirty prompt editor. The guard mock IS the confirm. */
function DraftyPage({ guard }: { guard: () => boolean }) {
  useRegisterNavigationLeaveGuard(guard)
  usePublishNavigationStake(true)
  return <div data-testid="page">drafty</div>
}

function Shell({ narrow = false, guard = () => true }: { narrow?: boolean; guard?: () => boolean }) {
  return (
    <NavigationLeaveGuardProvider>
      <BrowserRouter>
        <NavigationBackGuard />
        <RouteHistoryTracker />
        <NativeStep narrow={narrow} />
        <GoTo to="/schedule" />
        <GoTo to="/drafty" />
        <GoTo to="/other" />
        <Routes>
          <Route path="/" element={<div data-testid="page">home</div>} />
          <Route path="/schedule" element={<div data-testid="page">schedule</div>} />
          <Route path="/drafty" element={<DraftyPage guard={guard} />} />
          <Route path="/other" element={<div data-testid="page">other</div>} />
        </Routes>
      </BrowserRouter>
    </NavigationLeaveGuardProvider>
  )
}

const page = () => screen.getByTestId('page').textContent
const go = async (to: string, expected: string) => {
  fireEvent.click(screen.getByRole('button', { name: `go ${to}` }))
  await waitFor(() => expect(page()).toBe(expected))
}
const step = (delta: unknown) => {
  expect(relay).not.toBeNull()
  relay!(delta)
}

beforeEach(() => {
  window.history.replaceState(null, '', '/')
  _resetRouteHistoryPositionForTest()
  relay = null
  unsubscribe.mockClear()
  onHistoryStep.mockClear()
  ;(window as Window & { electronAPI?: unknown }).electronAPI = { onHistoryStep }
})
afterEach(() => {
  cleanup()
  delete (window as Window & { electronAPI?: unknown }).electronAPI
})

describe('useNativeHistoryStep', () => {
  it('a relayed Back returns to the previous page and Forward comes back', async () => {
    render(<Shell />)
    await go('/schedule', 'schedule')
    step(-1)
    await waitFor(() => expect(page()).toBe('home'))
    step(1)
    await waitFor(() => expect(page()).toBe('schedule'))
  })

  it('does nothing with nowhere to go, like a disabled arrow', async () => {
    render(<Shell />)
    const historyGo = vi.spyOn(window.history, 'go')
    step(-1)
    step(1)
    // A Back from the app's first entry would leave the app; neither is issued.
    expect(historyGo).not.toHaveBeenCalled()
    expect(page()).toBe('home')
    historyGo.mockRestore()
  })

  it('ignores anything but a one-entry step', async () => {
    render(<Shell />)
    await go('/schedule', 'schedule')
    step(-1)
    await waitFor(() => expect(page()).toBe('home'))
    // Standing mid-stack, so both directions are open: junk must still move nothing.
    const historyGo = vi.spyOn(window.history, 'go')
    for (const junk of [2, -2, 0, '-1', '1', null, undefined, Number.NaN]) step(junk)
    expect(historyGo).not.toHaveBeenCalled()
    expect(page()).toBe('home')
    historyGo.mockRestore()
  })

  it('asks about a dirty draft before stepping away, and a veto keeps it', async () => {
    const guard = vi.fn(() => false)
    render(<Shell guard={guard} />)
    // A Forward branch above the dirty page keeps the Back trap from arming,
    // so only the step's own up-front ask stands between the draft and the pop.
    await go('/drafty', 'drafty')
    await go('/other', 'other')
    window.history.back()
    await waitFor(() => expect(page()).toBe('drafty'))

    step(-1)
    expect(guard).toHaveBeenCalledTimes(1)
    expect(page()).toBe('drafty')

    guard.mockReturnValue(true)
    step(-1)
    await waitFor(() => expect(page()).toBe('home'))
  })

  it('stays unsubscribed on a narrow viewport and lets go when it narrows', () => {
    const { rerender } = render(<Shell narrow />)
    expect(onHistoryStep).not.toHaveBeenCalled()

    rerender(<Shell />)
    expect(onHistoryStep).toHaveBeenCalledTimes(1)
    rerender(<Shell narrow />)
    expect(unsubscribe).toHaveBeenCalledTimes(1)
  })

  it('is inert outside the desktop app', async () => {
    delete (window as Window & { electronAPI?: unknown }).electronAPI
    render(<Shell />)
    await go('/schedule', 'schedule')
    expect(relay).toBeNull()
    expect(page()).toBe('schedule')
  })
})
