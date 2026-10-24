import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, act } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { configureStore } from '@reduxjs/toolkit'
import type { ReactNode } from 'react'
import chatReducer, { setActiveSlot, sseSubagentSpawn } from '../store/chatSlice'
import dashboardReducer from '../store/dashboardSlice'
import notificationsReducer from '../store/notificationsSlice'
import { useSecondClock, __resetSecondClockForTests } from '../hooks/useSecondClock'

vi.mock('../api/client', () => ({
  api: { spawnList: vi.fn().mockResolvedValue({ agents: [] }) },
}))

// Glass is rendered once per SubagentProgressBar body render, so counting its
// renders counts the bar's own renders without instrumenting the component.
const glassRenders = vi.hoisted(() => ({ n: 0 }))
vi.mock('../components/Glass', () => ({
  Glass: ({ children }: { children: ReactNode }) => {
    glassRenders.n += 1
    return <div>{children}</div>
  },
}))

import SubagentProgressBar from '../pages/chat/SubagentProgressBar'

const T0 = new Date('2026-01-01T00:00:00Z').getTime()

function setHidden(hidden: boolean) {
  Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => (hidden ? 'hidden' : 'visible') })
  document.dispatchEvent(new Event('visibilitychange'))
}

function Seconds({ since, onRender }: { since: number; onRender?: () => void }) {
  const secs = useSecondClock(now => Math.floor((now - since) / 1000))
  onRender?.()
  return <span data-testid="secs">{secs}</span>
}

function Past({ at, onRender }: { at: number; onRender: () => void }) {
  const past = useSecondClock(now => now >= at)
  onRender()
  return <span data-testid="past">{String(past)}</span>
}

describe('useSecondClock', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    vi.setSystemTime(T0)
    __resetSecondClockForTests()
    setHidden(false)
    glassRenders.n = 0
  })
  afterEach(() => {
    vi.useRealTimers()
    setHidden(false)
    __resetSecondClockForTests()
  })

  it('drives every subscriber from one interval and clears it on the last unmount', () => {
    const setSpy = vi.spyOn(globalThis, 'setInterval')
    const clearSpy = vi.spyOn(globalThis, 'clearInterval')
    const view = render(<><Seconds since={T0} /><Seconds since={T0} /><Seconds since={T0} /></>)
    expect(setSpy).toHaveBeenCalledTimes(1)
    act(() => { vi.advanceTimersByTime(3000) })
    expect(screen.getAllByTestId('secs').map(e => e.textContent)).toEqual(['3', '3', '3'])
    view.unmount()
    expect(clearSpy).toHaveBeenCalledTimes(1)
    expect(vi.getTimerCount()).toBe(0)
    setSpy.mockRestore()
    clearSpy.mockRestore()
  })

  it('re-renders a boolean subscriber only when the value flips', () => {
    const onRender = vi.fn()
    render(<Past at={T0 + 5000} onRender={onRender} />)
    const initial = onRender.mock.calls.length
    act(() => { vi.advanceTimersByTime(4000) })
    expect(onRender).toHaveBeenCalledTimes(initial)
    expect(screen.getByTestId('past')).toHaveTextContent('false')
    act(() => { vi.advanceTimersByTime(3000) })
    expect(onRender).toHaveBeenCalledTimes(initial + 1)
    expect(screen.getByTestId('past')).toHaveTextContent('true')
  })

  it('stops ticking while the document is hidden and catches up when shown', () => {
    const onRender = vi.fn()
    render(<Seconds since={T0} onRender={onRender} />)
    act(() => { setHidden(true) })
    expect(vi.getTimerCount()).toBe(0)
    const before = onRender.mock.calls.length
    act(() => { vi.advanceTimersByTime(10_000) })
    expect(onRender).toHaveBeenCalledTimes(before)
    act(() => { setHidden(false) })
    expect(screen.getByTestId('secs')).toHaveTextContent('10')
    expect(vi.getTimerCount()).toBe(1)
  })

  it('ticks the subagent row figure without re-rendering the progress bar body', () => {
    const store = configureStore({ reducer: { chat: chatReducer, dashboard: dashboardReducer, notifications: notificationsReducer } })
    store.dispatch(setActiveSlot('s'))
    store.dispatch(sseSubagentSpawn({ slot: 's', id: 'a1', task: 'task a1', agent: 'agent-a1' }))
    render(
      <QueryClientProvider client={new QueryClient()}>
        <Provider store={store}><SubagentProgressBar slot="s" /></Provider>
      </QueryClientProvider>,
    )
    const bodyRenders = glassRenders.n
    const row = screen.getByTestId('subagent-row')
    // One act per second: a single act around all five would batch the ticks
    // into one commit and hide a per-second body render.
    for (let i = 0; i < 5; i++) act(() => { vi.advanceTimersByTime(1000) })
    expect(row).toHaveTextContent('5s')
    expect(glassRenders.n).toBe(bodyRenders)
  })
})
