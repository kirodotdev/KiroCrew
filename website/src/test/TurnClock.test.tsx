import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, act, fireEvent } from '@testing-library/react'
import { Provider } from 'react-redux'
import { configureStore } from '@reduxjs/toolkit'
import TurnClock from '../pages/chat/TurnClock'
import TaskProgressBar from '../pages/chat/TaskProgressBar'
import dashboardReducer from '../store/dashboardSlice'
import type { ChatSlot, TodoList } from '../types'
import { __resetSecondClockForTests } from '../hooks/useSecondClock'
import { CHAT_COMPLETE_NOTIFY_KEY, CHAT_COMPLETE_MIN_SECONDS_KEY } from '../hooks/chatCompleteNotify'
import { CHAT_COMPLETE_OFFER_DISMISSED_KEY, CHAT_COMPLETE_OFFER_MIN_SECONDS } from '../hooks/chatCompleteOffer'
import { noteServerNow, resetServerClock } from '../utils/serverClock'
import '../i18n/all'

vi.mock('../lib/embedded', () => ({ isEmbeddedPane: vi.fn(() => false) }))
import { isEmbeddedPane } from '../lib/embedded'

// Epoch seconds the run and the step started; the clock reads now - these.
const T0_SECS = 1_800_000_000

const slot = (over: Partial<ChatSlot>): ChatSlot =>
  ({ key: 'slot-1', messages: 1, running: true, run_started_ts: T0_SECS, ...over }) as ChatSlot

function renderWith(ui: React.ReactElement, slots: ChatSlot[]) {
  const store = configureStore({ reducer: { dashboard: dashboardReducer }, preloadedState: { dashboard: { slots } } as never })
  return render(<Provider store={store}>{ui}</Provider>)
}

function advance(secs: number) {
  act(() => { vi.advanceTimersByTime(secs * 1000) })
}

const grantable = { permission: 'default', requestPermission: vi.fn().mockResolvedValue('default') }
const originalReferrer = document.referrer

describe('TurnClock', () => {
  beforeEach(() => {
    vi.useFakeTimers({ shouldAdvanceTime: false })
    vi.setSystemTime(T0_SECS * 1000)
    __resetSecondClockForTests()
    resetServerClock()
    localStorage.clear()
    grantable.requestPermission.mockClear()
    vi.mocked(isEmbeddedPane).mockReturnValue(false)
    Object.defineProperty(document, 'referrer', { configurable: true, value: originalReferrer })
    vi.stubGlobal('Notification', grantable)
  })
  afterEach(() => {
    vi.useRealTimers()
    vi.unstubAllGlobals()
    vi.mocked(isEmbeddedPane).mockReturnValue(false)
    Object.defineProperty(document, 'referrer', { configurable: true, value: originalReferrer })
    __resetSecondClockForTests()
    resetServerClock()
    localStorage.clear()
  })

  it("counts on the gateway's clock, so a browser running behind it shows no skew", () => {
    // The gateway's clock is 30 s ahead of this browser's: the run began 30 s ago.
    noteServerNow(T0_SECS + 30)
    renderWith(<TurnClock slot="slot-1" />, [slot({})])
    expect(screen.getByTestId('turn-clock').textContent).toBe('Running for 30s')
  })

  it('hides the offer when another window of this browser dismisses it', () => {
    renderWith(<TurnClock slot="slot-1" />, [slot({ run_started_ts: T0_SECS - 120 })])
    expect(screen.getByTestId('turn-clock-notify')).toBeTruthy()
    localStorage.setItem(CHAT_COMPLETE_OFFER_DISMISSED_KEY, '1')
    act(() => { window.dispatchEvent(new StorageEvent('storage', { key: CHAT_COMPLETE_OFFER_DISMISSED_KEY })) })
    expect(screen.queryByTestId('turn-clock-notify')).toBeNull()
  })

  it('counts the running turn up from the gateway stamp, every second', () => {
    renderWith(<TurnClock slot="slot-1" />, [slot({})])
    expect(screen.getByTestId('turn-clock').textContent).toBe('Running for 0s')
    advance(65)
    expect(screen.getByTestId('turn-clock').textContent).toBe('Running for 1m 5s')
  })

  it('reads a stamp from before this view opened, as a reload would', () => {
    renderWith(<TurnClock slot="slot-1" />, [slot({ run_started_ts: T0_SECS - 600 })])
    expect(screen.getByTestId('turn-clock').textContent).toBe('Running for 10m 0s')
  })

  it('renders nothing while no turn runs, or with no stamp', () => {
    const { unmount } = renderWith(<TurnClock slot="slot-1" />, [slot({ running: false })])
    expect(screen.queryByTestId('turn-clock')).toBeNull()
    unmount()
    renderWith(<TurnClock slot="slot-1" />, [slot({ run_started_ts: null })])
    expect(screen.queryByTestId('turn-clock')).toBeNull()
  })

  it('offers the turn-done toast once the turn passes the threshold, and turns it on', () => {
    renderWith(<TurnClock slot="slot-1" />, [slot({})])
    advance(CHAT_COMPLETE_OFFER_MIN_SECONDS - 1)
    expect(screen.queryByTestId('turn-clock-notify')).toBeNull()
    advance(1)
    fireEvent.click(screen.getByTestId('turn-clock-notify'))
    expect(localStorage.getItem(CHAT_COMPLETE_NOTIFY_KEY)).toBe('1')
    expect(grantable.requestPermission).toHaveBeenCalled()
    expect(screen.queryByTestId('turn-clock-notify')).toBeNull()
    expect(screen.getByTestId('turn-clock')).toBeTruthy()
  })

  it('offers in an embedded pane with a relay target despite denied local permission', () => {
    vi.mocked(isEmbeddedPane).mockReturnValue(true)
    Object.defineProperty(document, 'referrer', { configurable: true, value: 'http://127.0.0.1:8787/' })
    const requestPermission = vi.fn()
    vi.stubGlobal('Notification', { permission: 'denied', requestPermission })

    renderWith(<TurnClock slot="slot-1" />, [slot({ run_started_ts: T0_SECS - 120 })])
    fireEvent.click(screen.getByTestId('turn-clock-notify'))

    expect(localStorage.getItem(CHAT_COMPLETE_NOTIFY_KEY)).toBe('1')
    expect(requestPermission).not.toHaveBeenCalled()
    expect(screen.queryByTestId('turn-clock-notify')).toBeNull()
  })

  it('waits for a longer minimum-turn-length setting, which the toast itself obeys', () => {
    localStorage.setItem(CHAT_COMPLETE_MIN_SECONDS_KEY, '300')
    renderWith(<TurnClock slot="slot-1" />, [slot({})])
    advance(CHAT_COMPLETE_OFFER_MIN_SECONDS)
    expect(screen.queryByTestId('turn-clock-notify')).toBeNull()
    advance(300 - CHAT_COMPLETE_OFFER_MIN_SECONDS)
    expect(screen.getByTestId('turn-clock-notify')).toBeTruthy()
  })

  it('stops offering once dismissed, in later turns too', () => {
    const { unmount } = renderWith(<TurnClock slot="slot-1" />, [slot({ run_started_ts: T0_SECS - 120 })])
    fireEvent.click(screen.getByTestId('turn-clock-dismiss'))
    expect(screen.queryByTestId('turn-clock-notify')).toBeNull()
    expect(localStorage.getItem(CHAT_COMPLETE_OFFER_DISMISSED_KEY)).toBe('1')
    expect(localStorage.getItem(CHAT_COMPLETE_NOTIFY_KEY)).toBeNull()
    unmount()
    renderWith(<TurnClock slot="slot-1" />, [slot({ run_started_ts: T0_SECS - 120 })])
    expect(screen.queryByTestId('turn-clock-notify')).toBeNull()
  })

  it('makes no offer while the toast is on', () => {
    localStorage.setItem(CHAT_COMPLETE_NOTIFY_KEY, '1')
    renderWith(<TurnClock slot="slot-1" />, [slot({ run_started_ts: T0_SECS - 120 })])
    expect(screen.queryByTestId('turn-clock-notify')).toBeNull()
  })

  it('hides the offer in a standalone dashboard where notifications are denied', () => {
    vi.stubGlobal('Notification', { permission: 'denied', requestPermission: vi.fn() })
    renderWith(<TurnClock slot="slot-1" />, [slot({ run_started_ts: T0_SECS - 120 })])
    expect(screen.queryByTestId('turn-clock-notify')).toBeNull()
  })
})

const todo = (stepStartedAt?: number): TodoList => ({
  description: 'Ship',
  tasks: [{ id: '1', text: 'build', completed: true }, { id: '2', text: 'test', completed: false }],
  completed: 1,
  total: 2,
  current: 'test',
  ...(stepStartedAt === undefined ? {} : { step_started_at: stepStartedAt }),
})

describe('TaskProgressBar step clock', () => {
  beforeEach(() => {
    vi.useFakeTimers({ shouldAdvanceTime: false })
    vi.setSystemTime(T0_SECS * 1000)
    __resetSecondClockForTests()
    resetServerClock()
  })
  afterEach(() => {
    vi.useRealTimers()
    __resetSecondClockForTests()
    resetServerClock()
  })

  it("counts the step on the gateway's clock", () => {
    noteServerNow(T0_SECS - 60) // the gateway's clock runs a minute behind
    renderWith(<TaskProgressBar slot="slot-1" />, [slot({ todo: todo(T0_SECS - 720) })])
    expect(screen.getByTestId('todo-step-elapsed').textContent).toBe('running 11m 0s')
  })

  it('shows how long the current step has run while the turn runs', () => {
    renderWith(<TaskProgressBar slot="slot-1" />, [slot({ todo: todo(T0_SECS - 720) })])
    expect(screen.getByTestId('todo-step-elapsed').textContent).toBe('running 12m 0s')
    advance(3)
    expect(screen.getByTestId('todo-step-elapsed').textContent).toBe('running 12m 3s')
  })

  it('puts the count and step timer below the current task in a narrow pane', () => {
    renderWith(<TaskProgressBar slot="slot-1" />, [slot({ todo: todo(T0_SECS - 60) })])

    expect(screen.getByTestId('todo-progress').className).toContain('@container/task-progress')
    const pill = screen.getByTestId('todo-pill')
    expect(pill.className).toContain('grid-cols-[auto_auto_minmax(0,1fr)_auto]')
    expect(pill.className).toContain('@[28rem]/task-progress:flex')
    expect(screen.getByTestId('todo-current')).toHaveClass('col-start-3', 'row-start-1')
    expect(screen.getByRole('progressbar')).toHaveClass('col-start-4', 'row-start-1')
    expect(screen.getByTestId('todo-count')).toHaveClass('col-start-3', 'row-start-2')
    expect(screen.getByTestId('todo-step-elapsed')).toHaveClass('col-start-4', 'row-start-2')
  })

  it('shows no step clock between turns, or before an agent snapshot stamped one', () => {
    const { unmount } = renderWith(<TaskProgressBar slot="slot-1" />, [slot({ running: false, todo: todo(T0_SECS - 60) })])
    expect(screen.queryByTestId('todo-step-elapsed')).toBeNull()
    unmount()
    renderWith(<TaskProgressBar slot="slot-1" />, [slot({ todo: todo() })])
    expect(screen.queryByTestId('todo-step-elapsed')).toBeNull()
  })
})
