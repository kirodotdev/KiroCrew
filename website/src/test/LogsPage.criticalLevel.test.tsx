// A log line whose level the chips do not list (Python's CRITICAL, or any
// custom level) used to be hidden at every filter: the level filter compared
// `LEVELS.indexOf(line.level)`, which is -1 for such a line. CRITICAL is the
// most severe level the backend streams, so it must show at every filter.
// Mock conventions follow `LogsPageCov80.test.tsx`.
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render as rtlRender, screen, fireEvent, act } from '@testing-library/react'
import { defaultScheduler, notifyManager, QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactElement, ReactNode } from 'react'

type Line = { level: string; msg: string }

function render(ui: ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return rtlRender(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>)
}

const { logLevel, setLogLevel, sub } = vi.hoisted(() => ({
  logLevel: vi.fn(),
  setLogLevel: vi.fn(),
  sub: { onLog: null as ((l: Line) => void) | null },
}))

vi.mock('../api/client', () => ({
  api: {
    logLevel: (...a: unknown[]) => logLevel(...a),
    setLogLevel: (...a: unknown[]) => setLogLevel(...a),
  },
}))

vi.mock('../App', async () => {
  const React = await import('react')
  return {
    WsContext: React.createContext({
      subscribeLogs: (fn: ((l: Line) => void) | null) => { sub.onLog = fn },
      subscribeSubagents: () => {},
      forceReconnect: () => {},
    }),
  }
})

// Render every row: jsdom has no layout for the real virtual list to measure.
vi.mock('react-virtuoso', async () => {
  const React = await import('react')
  return {
    Virtuoso: React.forwardRef((p: Record<string, unknown>, ref: React.Ref<unknown>) => {
      React.useImperativeHandle(ref, () => ({ scrollToIndex: () => {} }))
      const data = (p.data ?? []) as Line[]
      const itemContent = p.itemContent as (i: number, l: Line) => ReactNode
      return <div>{data.map((d, i) => <div key={i}>{itemContent(i, d)}</div>)}</div>
    }),
  }
})

import { LogViewer } from '../pages/LogsPage'

function emit(...lines: Line[]) {
  act(() => { lines.forEach(l => sub.onLog?.(l)) })
}

beforeEach(() => {
  logLevel.mockReset().mockResolvedValue({ level: 'INFO' })
  setLogLevel.mockReset().mockResolvedValue({ ok: true })
  sub.onLog = null
  // The accepted level reaches the filter through the query cache; on
  // microtasks the `act` around a click flushes its notification.
  notifyManager.setScheduler(queueMicrotask)
})
afterEach(() => { notifyManager.setScheduler(defaultScheduler) })

describe('LogViewer level filter', () => {
  it('shows CRITICAL and unlisted-level lines at every level, in the error colour for CRITICAL', async () => {
    render(<LogViewer />)
    await act(async () => {})
    emit(
      { level: 'INFO', msg: 'info line' },
      { level: 'CRITICAL', msg: 'critical line' },
      { level: 'NOTICE', msg: 'custom level line' },
    )

    expect(screen.getByText('critical line')).toBeInTheDocument()
    expect(screen.getByText('critical line')).toHaveClass('text-danger')
    expect(screen.getByText('custom level line')).toBeInTheDocument()

    // The strictest chip still hides INFO but keeps the more severe line.
    await act(async () => { fireEvent.click(screen.getByText('Error')) })
    expect(setLogLevel).toHaveBeenCalledWith('ERROR')
    expect(screen.queryByText('info line')).not.toBeInTheDocument()
    expect(screen.getByText('critical line')).toBeInTheDocument()
    expect(screen.getByText('custom level line')).toBeInTheDocument()
  })
})
