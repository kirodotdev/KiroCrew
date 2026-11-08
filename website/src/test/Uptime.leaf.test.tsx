import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, act } from '@testing-library/react'
import { Provider } from 'react-redux'
import { configureStore } from '@reduxjs/toolkit'
import dashboardReducer, { sseStatus } from '../store/dashboardSlice'
import { Uptime } from '../components/Uptime'
import { __resetSecondClockForTests } from '../hooks/useSecondClock'
import type { StatusData } from '../types'

const T0 = new Date('2026-01-01T00:00:00Z').getTime()

function Host({ onRender }: { onRender: () => void }) {
  onRender()
  return <div data-testid="uptime"><Uptime /></div>
}

describe('Uptime leaf', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    vi.setSystemTime(T0)
    __resetSecondClockForTests()
  })
  afterEach(() => {
    vi.useRealTimers()
    __resetSecondClockForTests()
  })

  it('ticks its own text each second without re-rendering the host', () => {
    const store = configureStore({ reducer: { dashboard: dashboardReducer } })
    store.dispatch(sseStatus({ start_time: T0 / 1000 - 30 } as unknown as StatusData))
    const onRender = vi.fn()
    render(<Provider store={store}><Host onRender={onRender} /></Provider>)
    const hostRenders = onRender.mock.calls.length
    const first = screen.getByTestId('uptime').textContent
    // One act per second, so a per-second host render could not hide in one batch.
    for (let i = 0; i < 3; i++) act(() => { vi.advanceTimersByTime(1000) })
    expect(screen.getByTestId('uptime').textContent).not.toBe(first)
    expect(onRender).toHaveBeenCalledTimes(hostRenders)
  })

  it('shows a dash and starts no clock before the gateway reports a start time', () => {
    const store = configureStore({ reducer: { dashboard: dashboardReducer } })
    render(<Provider store={store}><Host onRender={() => {}} /></Provider>)
    expect(screen.getByTestId('uptime')).toHaveTextContent('\u2014')
    expect(vi.getTimerCount()).toBe(0)
  })
})
