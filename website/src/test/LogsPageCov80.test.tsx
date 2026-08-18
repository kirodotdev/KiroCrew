// LogViewer (and the LogsPage shell around it): level switching, the search /
// matches-only / newest-first / wrap / tail toggles, the ring-buffer cap, the
// match highlighter, and the follow behaviour that pins to whichever end is
// "latest" for the current direction.
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, act } from '@testing-library/react'
import type { ReactNode } from 'react'

type Line = { level: string; msg: string }

const { logLevel, setLogLevel, sub, virtuoso } = vi.hoisted(() => ({
  logLevel: vi.fn(),
  setLogLevel: vi.fn(),
  sub: { onLog: null as ((l: Line) => void) | null, calls: [] as unknown[] },
  virtuoso: {
    scrollToIndex: vi.fn(),
    props: null as Record<string, unknown> | null,
  },
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
      subscribeLogs: (fn: ((l: Line) => void) | null) => { sub.calls.push(fn); sub.onLog = fn },
      subscribeSubagents: () => {},
      forceReconnect: () => {},
    }),
  }
})

vi.mock('react-virtuoso', async () => {
  const React = await import('react')
  return {
    Virtuoso: React.forwardRef((p: Record<string, unknown>, ref: React.Ref<unknown>) => {
      virtuoso.props = p
      React.useImperativeHandle(ref, () => ({ scrollToIndex: virtuoso.scrollToIndex }))
      const data = (p.data ?? []) as Line[]
      const itemContent = p.itemContent as (i: number, l: Line) => ReactNode
      return <div data-testid="virtuoso">{data.map((d, i) => <div key={i}>{itemContent(i, d)}</div>)}</div>
    }),
  }
})

import LogsPage, { LogViewer } from '../pages/LogsPage'

/** Feed lines through the websocket subscription the viewer registered. */
function emit(...lines: Line[]) {
  act(() => { lines.forEach(l => sub.onLog?.(l)) })
}

beforeEach(() => {
  logLevel.mockReset().mockResolvedValue({ level: 'INFO' })
  setLogLevel.mockReset().mockResolvedValue({ ok: true })
  sub.onLog = null
  sub.calls.length = 0
  virtuoso.scrollToIndex.mockClear()
  virtuoso.props = null
})

describe('LogViewer level control', () => {
  it('adopts the backend level on mount', async () => {
    render(<LogViewer />)
    await act(async () => {})
    expect(logLevel).toHaveBeenCalled()
    expect(screen.getByText('Info').className).toContain('bg-info')
  })

  it('switches level when the backend accepts it', async () => {
    render(<LogViewer />)
    await act(async () => {})
    await act(async () => { fireEvent.click(screen.getByText('Warning')) })
    expect(setLogLevel).toHaveBeenCalledWith('WARNING')
    expect(screen.getByText('Warning').className).toContain('bg-warn')
  })

  it('keeps the old level when the backend refuses', async () => {
    setLogLevel.mockResolvedValue({ ok: false })
    render(<LogViewer />)
    await act(async () => {})
    await act(async () => { fireEvent.click(screen.getByText('Error')) })
    expect(screen.getByText('Info').className).toContain('bg-info')
    expect(screen.getByText('Error').className).toContain('bg-transparent')
  })

  it('styles each active level with its own tone', async () => {
    render(<LogViewer />)
    await act(async () => {})
    for (const [label, cls] of [['Debug', 'bg-muted'], ['Error', 'bg-danger']] as const) {
      await act(async () => { fireEvent.click(screen.getByText(label)) })
      expect(screen.getByText(label).className).toContain(cls)
    }
  })

  it('hides lines below the selected level', async () => {
    render(<LogViewer />)
    await act(async () => {})
    emit({ level: 'DEBUG', msg: 'zz-dbg' }, { level: 'ERROR', msg: 'zz-err' })
    expect(screen.queryByText('zz-dbg')).not.toBeInTheDocument()
    expect(screen.getByText('zz-err')).toBeInTheDocument()
    await act(async () => { fireEvent.click(screen.getByText('Debug')) })
    expect(screen.getByText('zz-dbg')).toBeInTheDocument()
  })

  it('colours each level row', async () => {
    render(<LogViewer />)
    await act(async () => {})
    await act(async () => { fireEvent.click(screen.getByText('Debug')) })
    emit(
      { level: 'DEBUG', msg: 'zz-d' },
      { level: 'INFO', msg: 'zz-i' },
      { level: 'WARNING', msg: 'zz-w' },
      { level: 'ERROR', msg: 'zz-e' },
    )
    expect(screen.getByText('zz-d').className).toContain('text-muted')
    expect(screen.getByText('zz-i').className).toContain('text-text')
    expect(screen.getByText('zz-w').className).toContain('text-warn')
    expect(screen.getByText('zz-e').className).toContain('text-danger')
  })

  it('caps the retained buffer', async () => {
    render(<LogViewer />)
    await act(async () => {})
    emit(...Array.from({ length: 505 }, (_, i) => ({ level: 'INFO', msg: `zz-${i}` })))
    expect(screen.queryByText('zz-0')).not.toBeInTheDocument()
    expect(screen.getByText('zz-504')).toBeInTheDocument()
    expect(screen.getAllByTestId('log-line')).toHaveLength(500)
  })

  it('unsubscribes on unmount', async () => {
    const { unmount } = render(<LogViewer />)
    await act(async () => {})
    unmount()
    expect(sub.calls.at(-1)).toBeNull()
  })
})

describe('LogViewer search', () => {
  async function withLines() {
    render(<LogViewer />)
    await act(async () => {})
    emit(
      { level: 'INFO', msg: 'alpha needle tail needle end' },
      { level: 'INFO', msg: 'beta plain' },
    )
    return screen.getByLabelText(/filter logs/i)
  }

  it('marks every occurrence of the query in a matching line', async () => {
    const input = await withLines()
    fireEvent.change(input, { target: { value: 'needle' } })
    const marks = screen.getAllByText('needle')
    expect(marks).toHaveLength(2)
    expect(marks[0].tagName).toBe('MARK')
    expect(screen.getByText(/2 matches|matches/)).toBeInTheDocument()
  })

  it('narrows to matches only, and clearing the query resets that', async () => {
    const input = await withLines()
    fireEvent.change(input, { target: { value: 'needle' } })
    fireEvent.click(screen.getByText('Matches only'))
    expect(screen.getAllByTestId('log-line')).toHaveLength(1)
    fireEvent.change(input, { target: { value: '' } })
    expect(screen.getAllByTestId('log-line')).toHaveLength(2)
    fireEvent.change(input, { target: { value: 'needle' } })
    // matchesOnly was reset by the clear, so both lines are back.
    expect(screen.getAllByTestId('log-line')).toHaveLength(2)
  })

  it('leaves a non-matching line unhighlighted', async () => {
    const input = await withLines()
    fireEvent.change(input, { target: { value: 'needle' } })
    expect(screen.getByText('beta plain').querySelector('mark')).toBeNull()
  })
})

describe('LogViewer view toggles', () => {
  async function mounted() {
    render(<LogViewer />)
    await act(async () => {})
    emit({ level: 'INFO', msg: 'zz-first' }, { level: 'INFO', msg: 'zz-second' })
  }

  it('reverses the order for latest-first', async () => {
    await mounted()
    fireEvent.click(screen.getByText('Latest: last'))
    const rows = screen.getAllByTestId('log-line').map(r => r.textContent)
    expect(rows).toEqual(['zz-second', 'zz-first'])
    expect(screen.getByText('Latest: first')).toBeInTheDocument()
  })

  it('reverses the order for latest-first while filtering too', async () => {
    await mounted()
    fireEvent.change(screen.getByLabelText(/filter logs/i), { target: { value: 'zz' } })
    fireEvent.click(screen.getByText('Latest: last'))
    expect(screen.getAllByTestId('log-line')[0].textContent).toBe('zz-second')
  })

  it('toggles line wrapping', async () => {
    await mounted()
    expect(screen.getByText('zz-first').className).toContain('whitespace-pre-wrap')
    fireEvent.click(screen.getByText('Wrap: on'))
    expect(screen.getByText('zz-first').className).toContain('whitespace-pre')
    expect(screen.getByText('Wrap: off')).toBeInTheDocument()
  })

  it('re-pins to the latest line when tail is turned back on', async () => {
    await mounted()
    fireEvent.click(screen.getByText('Tail: on'))
    expect(screen.getByText('Tail: off')).toBeInTheDocument()
    virtuoso.scrollToIndex.mockClear()
    fireEvent.click(screen.getByText('Tail: off'))
    expect(virtuoso.scrollToIndex).toHaveBeenCalledWith({ index: 1, behavior: 'smooth' })
  })

  it('pins to the top while following in latest-first order', async () => {
    await mounted()
    fireEvent.click(screen.getByText('Latest: last'))
    virtuoso.scrollToIndex.mockClear()
    emit({ level: 'INFO', msg: 'zz-third' })
    expect(virtuoso.scrollToIndex).toHaveBeenCalledWith({ index: 0 })
  })

  it('stops yanking the view when the user scrolls away from the top', async () => {
    await mounted()
    fireEvent.click(screen.getByText('Latest: last'))
    act(() => { (virtuoso.props?.atTopStateChange as (v: boolean) => void)(false) })
    virtuoso.scrollToIndex.mockClear()
    emit({ level: 'INFO', msg: 'zz-fourth' })
    expect(virtuoso.scrollToIndex).not.toHaveBeenCalled()
  })

  it('follows the bottom only in latest-last order', async () => {
    await mounted()
    expect(virtuoso.props?.followOutput).toBe('smooth')
    fireEvent.click(screen.getByText('Latest: last'))
    expect(virtuoso.props?.followOutput).toBe(false)
  })
})

describe('LogViewer density', () => {
  it('uses tighter controls in compact mode', async () => {
    const { container } = render(<LogViewer compact />)
    await act(async () => {})
    emit({ level: 'INFO', msg: 'zz-compact' })
    expect(screen.getByText('zz-compact').className).toContain('text-[12px]')
    expect(container.querySelector('.px-6')).toBeNull()
  })

  it('uses page padding and larger controls otherwise', async () => {
    const { container } = render(<LogViewer />)
    await act(async () => {})
    emit({ level: 'INFO', msg: 'zz-roomy' })
    expect(screen.getByText('zz-roomy').className).toContain('text-[13px]')
    // The gutter is narrow-first now: 8px unprefixed, widened at `md`.
    expect(container.innerHTML).toContain('md:px-6')
  })
})

describe('LogsPage', () => {
  it('frames the viewer with the page header', async () => {
    render(<LogsPage />)
    await act(async () => {})
    expect(screen.getByText('Live Logs')).toBeInTheDocument()
    expect(screen.getByTestId('virtuoso')).toBeInTheDocument()
  })
})
