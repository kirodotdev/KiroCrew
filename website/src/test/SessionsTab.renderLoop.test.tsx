// The System page froze the whole app: the renderer spun at ~80% CPU in a
// TanStack Table setState loop, and because that loop never yields, the fetch
// that would have ended it could never land — so the window latched shut.
//
// Two defects had to meet. `data?.sessions ?? []` minted a NEW array on every
// render whenever the payload carried no `sessions` field (the first fetch, or an
// error payload such as a 403), which changed the identity of the `data` handed to
// `useReactTable`; and the table set `autoResetExpanded: false` while leaving
// `autoResetPageIndex` at its default `true`. TanStack read the new identity as
// "data changed", queued `resetPageIndex`, and — with no `onPaginationChange`
// supplied — routed it through its own `makeStateUpdater('pagination')` into
// `table.setState`. That re-render minted the next array.
//
// These tests count row-tree RECOMPUTES and renders rather than asserting config,
// because the config is the mechanism and the churn is the symptom.
//
// Honest limitation: the setState LOOP itself does not reproduce under jsdom. Its
// `queued` guard means it cannot re-enter within one flush, so it needs each flush
// to schedule the next, which the test harness's batching settles instead. What is
// verified here is the CAUSE — that the row tree stops being rebuilt on every
// render — plus the loading-state fix. That the `autoResetPageIndex` option gates
// the `resetPageIndex` queue at all is proven from table-core 8.21.3's own source,
// not from this file.
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, cleanup, waitFor, fireEvent } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { useRef, type MutableRefObject } from 'react'
import SessionsTab from '../pages/system/SessionsTab'
import type { PlaneState } from '../pages/SystemPage'
import { api } from '../api/client'

/** Counts `buildTree` invocations, i.e. how often the row-tree memo missed. */
const buildTreeCalls = { n: 0 }

vi.mock('../pages/system/sessionRows', async importOriginal => {
  const actual = await importOriginal<typeof import('../pages/system/sessionRows')>()
  return {
    ...actual,
    buildTree: (...args: Parameters<typeof actual.buildTree>) => {
      buildTreeCalls.n++
      return actual.buildTree(...args)
    },
  }
})

/**
 * Captures every `options` object passed to `useReactTable` so tests can assert
 * on resolved table configuration. The wrapper is transparent: the table still
 * works normally, and existing tests are unaffected.
 */
const capturedTableOptions: Array<Record<string, unknown>> = []

vi.mock('@tanstack/react-table', async importOriginal => {
  const actual = await importOriginal<typeof import('@tanstack/react-table')>()
  return {
    ...actual,
    useReactTable: (options: Record<string, unknown>) => {
      capturedTableOptions.push(options)
      return actual.useReactTable(options as never)
    },
  }
})

/** A render that never settles is the bug; anything near this is a loop. */
const RENDER_CEILING = 25

let renders = 0

function Harness() {
  renders++
  const planeStateRef = useRef<PlaneState>({}) as MutableRefObject<PlaneState>
  return <SessionsTab planeStateRef={planeStateRef} />
}

function mount() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <MemoryRouter initialEntries={['/developer?tab=system']}>
      <QueryClientProvider client={qc}>
        <Harness />
      </QueryClientProvider>
    </MemoryRouter>,
  )
}

describe('SessionsTab render stability', () => {
  beforeEach(() => { renders = 0; buildTreeCalls.n = 0; capturedTableOptions.length = 0 })
  afterEach(() => { cleanup(); vi.restoreAllMocks() })

  it('settles while the first fetch is still in flight', async () => {
    // Never resolves: holds the component in the exact window that ignited the loop.
    vi.spyOn(api, 'sessionsMemory').mockImplementation(() => new Promise(() => {}) as never)

    mount()
    await new Promise(r => setTimeout(r, 600))

    expect(renders).toBeLessThan(RENDER_CEILING)
  })

  it('settles when the payload carries no sessions field', async () => {
    // The shape the Mac reproduction actually hit: an error body, so `data.sessions`
    // is undefined even though the query resolved.
    vi.spyOn(api, 'sessionsMemory').mockResolvedValue({ error: 'token expired' } as never)

    mount()
    await new Promise(r => setTimeout(r, 600))

    expect(renders).toBeLessThan(RENDER_CEILING)
  })

  it('shows a skeleton while loading rather than claiming there are none', async () => {
    vi.spyOn(api, 'sessionsMemory').mockImplementation(() => new Promise(() => {}) as never)

    mount()

    // The false claim must NOT be on screen during the first fetch.
    await waitFor(() => expect(screen.queryByText('No active sessions')).toBeNull())
  })

  it('settles when a state change re-renders while the payload has no sessions', async () => {
    // The shape the Mac reproduction hit, plus the kick the loop needs. A single
    // mount is not enough: TanStack's auto-reset only REGISTERS on its first call,
    // so the loop cannot start until something re-renders the table once. In the
    // wild that kick is a 5s poll or any interaction; here it is typing a filter.
    vi.spyOn(api, 'sessionsMemory').mockResolvedValue({ error: 'token expired' } as never)

    mount()
    await waitFor(() => expect(screen.getByPlaceholderText(/Filter sessions/)).toBeTruthy())
    renders = 0
    fireEvent.change(screen.getByPlaceholderText(/Filter sessions/), { target: { value: 'a' } })
    await new Promise(r => setTimeout(r, 800))

    expect(renders).toBeLessThan(RENDER_CEILING)
  })

  it('does not rebuild the row tree on every render when the payload has no sessions', async () => {
    // The mutation-sensitive assertion for the identity half, and the one that
    // actually fails if `?? []` comes back. `rows` is `useMemo(..., [sessions,
    // tasks])`; if those fallbacks are fresh arrays each render the memo can never
    // hit, so `buildTree` runs again on every single render — and it is that churn
    // in `data` identity that feeds TanStack's auto-reset. Counting recomputes
    // measures the cause directly, instead of relying on the downstream loop
    // reproducing under jsdom (it does not — see the file header).
    vi.spyOn(api, 'sessionsMemory').mockResolvedValue({ error: 'token expired' } as never)

    mount()
    await waitFor(() => expect(screen.getByPlaceholderText(/Filter sessions/)).toBeTruthy())
    const before = buildTreeCalls.n
    // Three renders that change nothing about the (absent) row data.
    for (const value of ['a', 'ab', 'abc']) {
      fireEvent.change(screen.getByPlaceholderText(/Filter sessions/), { target: { value } })
      await new Promise(r => setTimeout(r, 30))
    }
    // With stable fallbacks the memo holds across all three; with `?? []` it misses
    // every time and this grows with the render count.
    expect(buildTreeCalls.n - before).toBe(0)
  })

  it('says it cannot tell — not "none" — when the endpoint fails', async () => {
    // The other half of the false-claim window, and the one the reporting
    // scenario actually hits: a non-2xx resolves the query with no data, so
    // `isPending` is already false. Without an error branch the empty state
    // renders and a broken page is indistinguishable from an idle machine.
    vi.spyOn(api, 'sessionsMemory').mockRejectedValue(new Error('403 token expired'))

    mount()

    await waitFor(() => expect(screen.getByTestId('sessions-error-title')).toBeTruthy())
    expect(screen.queryByText('No active sessions')).toBeNull()
    // And the footer must not assert concrete zeros beside "cannot tell".
    expect(screen.queryByText('Sessions')).toBeNull()
  })

  it('keeps the table and shows a stale notice when a background poll fails', async () => {
    // react-query keeps the last payload while flipping status to `error`, so an
    // unguarded `isError` branch would unmount a table the user is mid-read on
    // every time one 5s poll blips. Stale rows plus "can't refresh" beat correct
    // rows replaced by a panel.
    const payload = {
      sessions: [{
        key: 'dashboard:chat-1-1', title: 'Live session', slot_key: 'chat-1-1', untitled: false,
        agent: 'kirocrew', channel: 'dashboard', pid: 4242, owns_runtime: true, prompts: 3,
        rss_mb: 512, peak_mb: 600, cpu_cores: 0.4, procs: 2, mcp: 1, uptime_s: 300,
        shared: false, credits: 1, turns: 2,
      }],
      tasks: [],
      totals: { rss_mb: 512, host_mb: 16000, procs: 2, sessions: 1, tasks: 0 },
      unattributed: null,
      history: [],
    }
    const spy = vi.spyOn(api, 'sessionsMemory')
    spy.mockResolvedValueOnce(payload as never)
    mount()
    await waitFor(() => expect(screen.getByText('Live session')).toBeTruthy())

    // Now make every later poll fail and drive one.
    spy.mockRejectedValue(new Error('boom'))
    fireEvent.change(screen.getByPlaceholderText(/Filter sessions/), { target: { value: '' } })
    await waitFor(() => expect(screen.getByTestId('sessions-stale')).toBeTruthy(), { timeout: 8000 })

    // The rows the user was reading are still there; the full-panel error is not.
    expect(screen.getByText('Live session')).toBeTruthy()
    expect(screen.queryByTestId('sessions-error-title')).toBeNull()
  }, 15000)

  it('still says there are none once an empty payload really arrives', async () => {
    vi.spyOn(api, 'sessionsMemory').mockResolvedValue({
      sessions: [], tasks: [], totals: { rss_mb: 0, host_mb: 1000, procs: 0, sessions: 0, tasks: 0 },
      unattributed: null, history: [],
    } as never)

    mount()

    await waitFor(() => expect(screen.getByText('No active sessions')).toBeTruthy())
    expect(renders).toBeLessThan(RENDER_CEILING)
  })

  it('disables autoResetPageIndex to prevent the pagination setState loop', async () => {
    // This table has no paginator (no `getPaginationRowModel`) and no
    // `onPaginationChange`, so TanStack's default `autoResetPageIndex: true`
    // routes every data-identity change through `makeStateUpdater('pagination')`
    // into `table.setState` — triggering a re-render that, with unstable `data`,
    // becomes an infinite loop. The ONLY thing breaking that cycle is the explicit
    // `autoResetPageIndex: false` in the table options.
    //
    // This is a configuration-level lock: it asserts the option is passed rather
    // than reproducing the loop (which requires real RAF scheduling that jsdom
    // cannot provide — see file header). It is the direct complement of the
    // `buildTree` churn tests above: those prove data identity is stable, this
    // proves the table will not self-destruct even if a future refactor
    // accidentally reintroduces an identity change.
    vi.spyOn(api, 'sessionsMemory').mockResolvedValue({
      sessions: [], tasks: [], totals: { rss_mb: 0, host_mb: 1000, procs: 0, sessions: 0, tasks: 0 },
      unattributed: null, history: [],
    } as never)

    mount()
    await waitFor(() => expect(capturedTableOptions.length).toBeGreaterThan(0))

    // Every call to useReactTable from this component must carry the guard.
    // (There is only one table, but renders call the hook repeatedly.)
    const withoutGuard = capturedTableOptions.filter(
      opts => opts.autoResetPageIndex !== false,
    )
    expect(withoutGuard).toHaveLength(0)
  })
})
