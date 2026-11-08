// A level change must reach every log viewer, not just the one it was made in.
// The dashboard's QueryClient never treats a read as stale (`staleTime:
// Infinity`), so the level read is fetched once and then served from the cache.
// The viewer kept an accepted change in its own state, so the Logs page after a
// remount, or a second viewer (the Activity panel's Logs tab, the Developer
// panel), showed the old level checked and filtered lines by it while the
// backend logged at the new one. Mock conventions follow
// `LogsPage.criticalLevel.test.tsx`.
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, act, within } from '@testing-library/react'
import { defaultScheduler, notifyManager, QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'

type Line = { level: string; msg: string }

const { logLevel, setLogLevel, sub } = vi.hoisted(() => ({
  logLevel: vi.fn(),
  setLogLevel: vi.fn(),
  sub: { onLog: [] as ((l: Line) => void)[] },
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
      subscribeLogs: (fn: ((l: Line) => void) | null) => { if (fn) sub.onLog.push(fn) },
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

// The production defaults that matter here: a cached read is never refetched.
const client = () =>
  new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })

const checked = (container: HTMLElement) =>
  within(within(container).getByRole('radiogroup')).getByRole('radio', { checked: true }).textContent

beforeEach(() => {
  logLevel.mockReset().mockResolvedValue({ level: 'INFO' })
  setLogLevel.mockReset().mockResolvedValue({ ok: true })
  sub.onLog = []
  // Cache notifications on microtasks, so the `act` around each step flushes
  // them and every assertion reads the settled render.
  notifyManager.setScheduler(queueMicrotask)
})
afterEach(() => { notifyManager.setScheduler(defaultScheduler) })

describe('LogViewer level across viewers', () => {
  it('shows the accepted level after a remount, and filters by it', async () => {
    const qc = client()
    const first = render(<QueryClientProvider client={qc}><LogViewer /></QueryClientProvider>)
    await act(async () => {})
    await act(async () => { fireEvent.click(screen.getByRole('radio', { name: 'Debug' })) })
    expect(setLogLevel).toHaveBeenCalledWith('DEBUG')
    first.unmount()

    const again = render(<QueryClientProvider client={qc}><LogViewer /></QueryClientProvider>)
    await act(async () => {})
    expect(checked(again.container)).toBe('Debug')
    act(() => { sub.onLog.at(-1)?.({ level: 'DEBUG', msg: 'zz-debug-line' }) })
    expect(screen.getByText('zz-debug-line')).toBeInTheDocument()
  })

  it('moves a second open viewer to the accepted level too', async () => {
    const qc = client()
    const page = render(<QueryClientProvider client={qc}><LogViewer /></QueryClientProvider>)
    const panel = render(<QueryClientProvider client={qc}><LogViewer compact /></QueryClientProvider>)
    await act(async () => {})
    await act(async () => {
      fireEvent.click(within(page.container).getByRole('radio', { name: 'Warning' }))
    })
    expect(checked(page.container)).toBe('Warning')
    expect(checked(panel.container)).toBe('Warning')
  })

  it('keeps the accepted level when the first level read answers late', async () => {
    // The read is still out when the change is accepted; its older answer must
    // not land on top of the new level.
    let answerRead: () => void = () => {}
    logLevel.mockImplementation(() => new Promise(resolve => {
      answerRead = () => resolve({ level: 'INFO' })
    }))
    const qc = client()
    const page = render(<QueryClientProvider client={qc}><LogViewer /></QueryClientProvider>)
    await act(async () => {
      fireEvent.click(within(page.container).getByRole('radio', { name: 'Debug' }))
    })
    expect(setLogLevel).toHaveBeenCalledWith('DEBUG')
    await act(async () => { answerRead() })
    expect(checked(page.container)).toBe('Debug')
    expect(qc.getQueryData(['logs', 'level'])).toEqual({ level: 'DEBUG' })
  })

  it('leaves every viewer on the old level when the backend refuses', async () => {
    setLogLevel.mockResolvedValue({ ok: false })
    const qc = client()
    const page = render(<QueryClientProvider client={qc}><LogViewer /></QueryClientProvider>)
    await act(async () => {})
    await act(async () => {
      fireEvent.click(within(page.container).getByRole('radio', { name: 'Error' }))
    })
    expect(checked(page.container)).toBe('Info')
    expect(qc.getQueryData(['logs', 'level'])).toEqual({ level: 'INFO' })
  })
})
