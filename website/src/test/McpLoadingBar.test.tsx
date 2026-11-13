import { describe, it, expect, afterEach } from 'vitest'
import { render, screen, act, within, fireEvent } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { Provider } from 'react-redux'
import { configureStore } from '@reduxjs/toolkit'
import McpLoadingBar from '../pages/chat/McpLoadingBar'
import dashboardReducer, { sseMcpReportUpdate } from '../store/dashboardSlice'
import { mcpSessionLoading } from '../lib/mcpSessionReport'
import type { ChatSlot, McpSessionReport } from '../types'
import { i18next } from '../i18n/all'

const report = (over: Partial<McpSessionReport> = {}): McpSessionReport => ({
  configured: [],
  ready: [],
  failed: [],
  awaiting_auth: [],
  failures: {},
  ...over,
})

const slot = (key: string, mcp_report: McpSessionReport | null): ChatSlot =>
  ({ key, messages: 0, running: false, mcp_report }) as ChatSlot

function renderBar(slots: ChatSlot[], activeKey: string | null = 'slot-1') {
  const store = configureStore({
    reducer: { dashboard: dashboardReducer },
    preloadedState: { dashboard: { slots } } as never,
  })
  const view = (key: string | null) => (
    <Provider store={store}>
      <McpLoadingBar slot={key} />
    </Provider>
  )
  const utils = render(view(activeKey))
  return { store, show: (key: string | null) => utils.rerender(view(key)), ...utils }
}

const pill = () => screen.queryByTestId('mcp-loading-pill')
const count = () => screen.getByTestId('mcp-loading-count').textContent

afterEach(async () => {
  await act(async () => { await i18next.changeLanguage('en') })
})

describe('McpLoadingBar: when it shows', () => {
  it('renders nothing when no slot is active', () => {
    renderBar([slot('slot-1', report({ ready: ['a'], states: { connecting: ['b'] } }))], null)
    expect(pill()).toBeNull()
  })

  it('renders nothing when the session has published no report', () => {
    renderBar([slot('slot-1', null)])
    expect(pill()).toBeNull()
  })

  it('renders nothing for a report that does not say which servers are connecting', () => {
    // A gateway or an agent backend that sends no `states`: a configured server
    // nobody has heard from is unreported, and that is not shown as loading.
    renderBar([slot('slot-1', report({ configured: ['a', 'b'], ready: ['a'] }))])
    expect(pill()).toBeNull()
  })

  it('renders nothing once every server has connected', () => {
    renderBar([slot('slot-1', report({ ready: ['a', 'b'], states: {} }))])
    expect(pill()).toBeNull()
  })

  it('renders nothing when the only servers not ready are not loading', () => {
    renderBar([slot('slot-1', report({
      ready: ['a'],
      states: { failed: ['b'], needs_sign_in: ['c'], disabled: ['d'], registry_dropped: ['e'] },
    }))])
    expect(pill()).toBeNull()
  })
})

describe('McpLoadingBar: the count', () => {
  it('says how many are ready of how many', () => {
    renderBar([slot('slot-1', report({ ready: ['a', 'b'], states: { connecting: ['c', 'd', 'e'] } }))])
    expect(count()).toBe('2 of 5 MCP servers ready')
  })

  it('leaves failed, sign-in, switched-off and left-out servers out of both numbers', () => {
    renderBar([slot('slot-1', report({
      ready: ['a'],
      states: { connecting: ['b'], failed: ['c'], needs_sign_in: ['d'], disabled: ['e'], registry_dropped: ['f'] },
    }))])
    expect(count()).toBe('1 of 2 MCP servers ready')
  })

  it('follows the live report, then goes away by itself', () => {
    const { store } = renderBar([slot('slot-1', report({ ready: ['a'], states: { connecting: ['b', 'c'] } }))])
    expect(count()).toBe('1 of 3 MCP servers ready')
    act(() => {
      store.dispatch(sseMcpReportUpdate({ slot: 'slot-1', mcp_report: report({ ready: ['a', 'b'], states: { connecting: ['c'] } }) }))
    })
    expect(count()).toBe('2 of 3 MCP servers ready')
    act(() => {
      store.dispatch(sseMcpReportUpdate({ slot: 'slot-1', mcp_report: report({ ready: ['a', 'b', 'c'], states: {} }) }))
    })
    expect(pill()).toBeNull()
  })

  it('goes away when a server it waited for fails instead of connecting', () => {
    const { store } = renderBar([slot('slot-1', report({ ready: ['a'], states: { connecting: ['b'] } }))])
    act(() => {
      store.dispatch(sseMcpReportUpdate({ slot: 'slot-1', mcp_report: report({ ready: ['a'], failed: ['b'], states: { failed: ['b'] } }) }))
    })
    expect(pill()).toBeNull()
  })

  it('goes away when the session is reset and its report is cleared', () => {
    const { store } = renderBar([slot('slot-1', report({ states: { connecting: ['b'] } }))])
    act(() => { store.dispatch(sseMcpReportUpdate({ slot: 'slot-1', mcp_report: null })) })
    expect(pill()).toBeNull()
  })

  it('renders the count as one catalog sentence', async () => {
    // en-XA brackets every catalog value, so a count glued from fragments would not be one bracketed run.
    await act(async () => { await i18next.changeLanguage('en-XA') })
    renderBar([slot('slot-1', report({ ready: ['a'], states: { connecting: ['b', 'c'] } }))])
    expect(count()).toMatch(/^\[1 [^[\]]* 3 [^[\]]*\]$/)
  })

  it('formats the numbers in the app language', async () => {
    await act(async () => { await i18next.changeLanguage('bn') })
    renderBar([slot('slot-1', report({ ready: ['a'], states: { connecting: ['b', 'c'] } }))])
    expect(count()).toContain('১')
    expect(count()).toContain('৩')
    expect(count()).not.toMatch(/[0-9]/)
  })
})

describe('McpLoadingBar: the list', () => {
  const mixed = report({
    ready: ['up'],
    states: { connecting: ['slow'], failed: ['broken'], needs_sign_in: ['locked'], disabled: ['off'], registry_dropped: ['dropped'] },
  })

  it('is closed until asked for, and opens from the keyboard', async () => {
    const user = userEvent.setup()
    renderBar([slot('slot-1', mixed)])
    expect(pill()).toHaveAttribute('aria-expanded', 'false')
    expect(screen.queryByTestId('mcp-loading-list')).toBeNull()
    await user.tab()
    expect(pill()).toHaveFocus()
    await user.keyboard('{Enter}')
    expect(pill()).toHaveAttribute('aria-expanded', 'true')
    const controlled = document.getElementById(pill()!.getAttribute('aria-controls')!)
    expect(controlled).toContainElement(screen.getByTestId('mcp-loading-list'))
  })

  const rowsOf = (testId: string) =>
    within(screen.getByTestId(testId)).getAllByTestId('mcp-loading-row').map(r => [r.textContent, r.dataset.state])

  it('lists the servers it counts first, the connecting ones on top', async () => {
    const user = userEvent.setup()
    renderBar([slot('slot-1', mixed)])
    await user.click(pill()!)
    expect(rowsOf('mcp-loading-list')).toEqual([
      ['slowConnecting', 'connecting'],
      ['upStarted in this session', 'connected'],
    ])
  })

  it('lists the servers it does not count apart, each as what it is', async () => {
    const user = userEvent.setup()
    renderBar([slot('slot-1', mixed)])
    await user.click(pill()!)
    expect(screen.getByTestId('mcp-loading-uncounted-label').textContent).toBe('Not counted: 4')
    expect(screen.getByTestId('mcp-loading-uncounted')).toHaveAccessibleName('Not counted: 4')
    expect(rowsOf('mcp-loading-uncounted')).toEqual([
      ['lockedWaiting for authorization', 'needs_sign_in'],
      ['brokenFailed to start in this session', 'failed'],
      ['offSwitched off', 'disabled'],
      ['droppedLeft out by policy', 'registry_dropped'],
    ])
  })

  it('draws no second group when every server is counted', async () => {
    const user = userEvent.setup()
    renderBar([slot('slot-1', report({ ready: ['a'], states: { connecting: ['b'] } }))])
    await user.click(pill()!)
    expect(screen.queryByTestId('mcp-loading-uncounted-label')).toBeNull()
    expect(screen.queryByTestId('mcp-loading-uncounted')).toBeNull()
  })

  it('counts exactly the rows of its first group: "ready of total" is connected rows of all its rows', async () => {
    // The rule a reader checks with their eyes: the header's two numbers are
    // the first group's connected rows and all of its rows, whatever else the
    // session has. Servers outside the count are in the second group only.
    const user = userEvent.setup()
    const { store } = renderBar([slot('slot-1', mixed)])
    await user.click(pill()!)
    const check = () => {
      const first = rowsOf('mcp-loading-list')
      const connected = first.filter(([, state]) => state === 'connected').length
      expect(count()).toBe(`${connected} of ${first.length} MCP servers ready`)
      expect(first.every(([, state]) => state === 'connected' || state === 'connecting')).toBe(true)
      const second = screen.queryByTestId('mcp-loading-uncounted') ? rowsOf('mcp-loading-uncounted') : []
      expect(second.some(([, state]) => state === 'connected' || state === 'connecting')).toBe(false)
      return { first: first.length, connected, second: second.length }
    }
    expect(check()).toEqual({ first: 2, connected: 1, second: 4 })
    // One connects, one that was connecting fails instead: both numbers follow the rows.
    act(() => {
      store.dispatch(sseMcpReportUpdate({
        slot: 'slot-1',
        mcp_report: report({
          ready: ['up', 'slow'],
          states: { connecting: ['later', 'never'], failed: ['broken'] },
        }),
      }))
    })
    expect(check()).toEqual({ first: 4, connected: 2, second: 1 })
    act(() => {
      store.dispatch(sseMcpReportUpdate({
        slot: 'slot-1',
        mcp_report: report({ ready: ['up', 'slow', 'later'], states: { connecting: [], failed: ['broken', 'never'] } }),
      }))
    })
    expect(check()).toEqual({ first: 3, connected: 3, second: 2 })
  })

  it('says so when the report does not track every server, without a number', async () => {
    const user = userEvent.setup()
    renderBar([slot('slot-1', report({ states: { connecting: ['slow'] }, states_omitted: 4 }))])
    await user.click(pill()!)
    expect(screen.getByTestId('mcp-loading-untracked').textContent).toBe('Some servers are not tracked')
  })

  it('stays open after the last server connects, and goes once closed', async () => {
    const user = userEvent.setup()
    const { store } = renderBar([slot('slot-1', report({ ready: ['a'], states: { connecting: ['b'] } }))])
    await user.click(pill()!)
    act(() => {
      store.dispatch(sseMcpReportUpdate({ slot: 'slot-1', mcp_report: report({ ready: ['a', 'b'], states: {} }) }))
    })
    expect(count()).toBe('2 of 2 MCP servers ready')
    expect(screen.getAllByTestId('mcp-loading-row')).toHaveLength(2)
    expect(screen.queryByTestId('mcp-loading-spinner')).toBeNull()
    await user.click(pill()!)
    expect(pill()).toBeNull()
  })

  it('goes away, even open, when a reset clears the report it was listing', async () => {
    // Nothing is left to list, so an open pill must not stay as "0 of 0".
    const user = userEvent.setup()
    const { store } = renderBar([slot('slot-1', report({ ready: ['a'], states: { connecting: ['b'] } }))])
    await user.click(pill()!)
    act(() => { store.dispatch(sseMcpReportUpdate({ slot: 'slot-1', mcp_report: null })) })
    expect(pill()).toBeNull()
  })

  it('starts closed for the next session in the slot after a reset', async () => {
    const user = userEvent.setup()
    const { store } = renderBar([slot('slot-1', report({ ready: ['a'], states: { connecting: ['b'] } }))])
    await user.click(pill()!)
    act(() => { store.dispatch(sseMcpReportUpdate({ slot: 'slot-1', mcp_report: null })) })
    // The next session's servers are all up at its first report: nothing is loading.
    act(() => {
      store.dispatch(sseMcpReportUpdate({ slot: 'slot-1', mcp_report: report({ ready: ['a', 'b'], states: {} }) }))
    })
    expect(pill()).toBeNull()
    act(() => {
      store.dispatch(sseMcpReportUpdate({ slot: 'slot-1', mcp_report: report({ ready: ['a'], states: { connecting: ['b'] } }) }))
    })
    expect(pill()!.getAttribute('aria-expanded')).toBe('false')
  })

  it('fades its bottom edge only while rows sit below the fold', async () => {
    // jsdom lays nothing out, so the scroller's metrics are set by hand and a
    // scroll event asks for the same re-measure a real scroll does.
    const user = userEvent.setup()
    renderBar([slot('slot-1', mixed)])
    await user.click(pill()!)
    const scroller = screen.getByTestId('mcp-loading-scroller')
    expect(scroller.className).not.toContain('mask-image')
    const metrics = { scrollHeight: 500, clientHeight: 256, scrollTop: 0 }
    for (const key of ['scrollHeight', 'clientHeight', 'scrollTop'] as const) {
      Object.defineProperty(scroller, key, { configurable: true, get: () => metrics[key] })
    }
    fireEvent.scroll(scroller)
    expect(scroller.className).toContain('mask-image')
    metrics.scrollTop = 244 // scrolled to the end: nothing is left below
    fireEvent.scroll(scroller)
    expect(scroller.className).not.toContain('mask-image')
  })

  it('starts closed in another session', async () => {
    const user = userEvent.setup()
    const { show } = renderBar([
      slot('slot-1', report({ states: { connecting: ['b'] } })),
      slot('slot-2', report({ states: { connecting: ['c'] } })),
    ])
    await user.click(pill()!)
    show('slot-2')
    expect(pill()).toHaveAttribute('aria-expanded', 'false')
  })
})

describe('McpLoadingBar: motion and announcements', () => {
  it('spins only where motion is welcome, and says the same thing in words', () => {
    renderBar([slot('slot-1', report({ ready: ['a'], states: { connecting: ['b'] } }))])
    const spinner = screen.getByTestId('mcp-loading-spinner')
    expect(spinner).toHaveClass('motion-safe:animate-spin')
    expect(spinner).not.toHaveClass('animate-spin')
    expect(count()).toBe('1 of 2 MCP servers ready')
  })

  it('announces once that servers are connecting, never each count', () => {
    const { store } = renderBar([slot('slot-1', report({ ready: ['a'], states: { connecting: ['b', 'c'] } }))])
    const status = screen.getByTestId('mcp-loading-status')
    expect(status).toHaveAttribute('role', 'status')
    const said = status.textContent
    expect(said).not.toMatch(/\d/)
    // The count is the button's own text, outside every live region.
    expect(within(status).queryByTestId('mcp-loading-count')).toBeNull()
    expect(screen.getByTestId('mcp-loading-count').closest('[role="status"], [aria-live]')).toBeNull()
    act(() => {
      store.dispatch(sseMcpReportUpdate({ slot: 'slot-1', mcp_report: report({ ready: ['a', 'b'], states: { connecting: ['c'] } }) }))
    })
    expect(screen.getByTestId('mcp-loading-status')).toBe(status)
    expect(status.textContent).toBe(said)
  })
})

describe('mcpSessionLoading', () => {
  it('reads nothing as connecting from a report without states', () => {
    expect(mcpSessionLoading(null).connecting).toEqual([])
    expect(mcpSessionLoading(report({ configured: ['a'], ready: [] })).connecting).toEqual([])
  })

  it('puts a server in one row only, the not-ready state winning over ready', () => {
    // A server that reconnects is in `connecting` again; a stale `ready` entry
    // for it must not count it twice.
    const got = mcpSessionLoading(report({ ready: ['a', 'b'], states: { connecting: ['b', 'b'], failed: ['b', 'c'] } }))
    expect(got.connecting).toEqual(['b'])
    expect(got.ready).toEqual(['a'])
    expect(got.counted).toEqual([
      { name: 'b', state: 'connecting' },
      { name: 'a', state: 'connected' },
    ])
    expect(got.uncounted).toEqual([{ name: 'c', state: 'failed' }])
  })

  it('ignores wire data that is not a list of names', () => {
    const bad = { connecting: 'slow', failed: [7, null, '', 'x'] } as unknown as McpSessionReport['states']
    const got = mcpSessionLoading(report({ states: bad, states_omitted: -3 }))
    expect(got.connecting).toEqual([])
    expect(got.counted).toEqual([])
    expect(got.uncounted).toEqual([{ name: 'x', state: 'failed' }])
    expect(got.partial).toBe(false)
  })
})
