import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, waitFor, fireEvent } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { useRef } from 'react'

/* Mock api/client BEFORE the component imports. */
const mockApi = vi.hoisted(() => ({ slashCommands: vi.fn() }))
vi.mock('../api/client', () => ({ api: mockApi }))

import SlashCommandMenu from '../components/SlashCommandMenu'
import { retryPolicy } from '../api/queryClient'

/* The deadline itself lives in the client layer and is pinned there. What the
 * MENU owes is passing react-query's signal through, and rendering a failure as
 * a failure rather than as a confident "no matching commands". */

function Harness({ input, onSelect = vi.fn(), onClose = vi.fn(), sendOnEnter, client }: {
  input: string; onSelect?: (c: string) => void; onClose?: () => void
  sendOnEnter?: 'enter' | 'ctrl-enter' | 'enter-ctrl-newline'; client?: QueryClient
}) {
  const ref = useRef<HTMLDivElement>(null)
  const qc = client ?? new QueryClient({ defaultOptions: { queries: { retry: false, retryDelay: 0 } } })
  return (
    <QueryClientProvider client={qc}>
      <div>
        <div ref={ref} data-testid="anchor">anchor</div>
        <SlashCommandMenu input={input} anchorRef={ref} onSelect={onSelect} onClose={onClose} sendOnEnter={sendOnEnter} />
      </div>
    </QueryClientProvider>
  )
}

/** What the bounded client does to a wedged gateway: reject with TimeoutError.
 *  A plain `Error` with that name, matching lib/withDeadline's own reason. */
const timesOut = () => () =>
  new Promise((_resolve, reject) => setTimeout(() => {
    const e = new Error('deadline exceeded')
    e.name = 'TimeoutError'
    reject(e)
  }, 5))

beforeEach(() => { vi.clearAllMocks() })
afterEach(() => { vi.restoreAllMocks() })

describe('SlashCommandMenu — a timed-out commands fetch', () => {
  it('passes react-query\'s AbortSignal to api.slashCommands, so the client can bound and cancel it', async () => {
    mockApi.slashCommands.mockImplementation(() => new Promise(() => {}))
    render(<Harness input="/" />)
    await waitFor(() => expect(mockApi.slashCommands).toHaveBeenCalled())
    expect(mockApi.slashCommands.mock.calls[0][0]).toBeInstanceOf(AbortSignal)
  })

  it('names the failure and releases Enter, so the composer is not deadlocked', async () => {
    // THE DEFECT: the release gate reads `!isFetching`, which an unbounded fetch
    // never clears — so the menu rendered NOTHING while Enter stayed swallowed.
    mockApi.slashCommands.mockImplementation(timesOut())
    const onSelect = vi.fn()
    const onClose = vi.fn()
    render(<Harness input="/xyz" onSelect={onSelect} onClose={onClose} />)
    expect(await screen.findByText(/Couldn't load commands — Enter sends the message/)).toBeInTheDocument()
    // fireEvent returns false when preventDefault was called; the composer's own
    // Enter-to-send only fires when the keystroke is NOT prevented.
    expect(fireEvent.keyDown(document, { key: 'Enter' })).toBe(true)
    expect(onClose).toHaveBeenCalled()
    expect(onSelect).not.toHaveBeenCalled()
  })

  it('does not claim "no matching commands" when the list never loaded', async () => {
    // The two states both release Enter, but they are not the same claim: a
    // zero-match says the live list was read, and here it never was.
    mockApi.slashCommands.mockImplementation(timesOut())
    render(<Harness input="/xyz" />)
    await screen.findByText(/Couldn't load commands/)
    expect(screen.queryByText(/No matching commands/)).not.toBeInTheDocument()
  })

  it('in ctrl-enter send mode, the failure copy names Ctrl+Enter', async () => {
    // Bare Enter is a newline there, so promising "Enter sends" would be false.
    mockApi.slashCommands.mockImplementation(timesOut())
    render(<Harness input="/xyz" sendOnEnter="ctrl-enter" />)
    expect(await screen.findByText(/Couldn't load commands — Ctrl\+Enter sends the message/)).toBeInTheDocument()
    expect(screen.queryByText(/— Enter sends the message/)).not.toBeInTheDocument()
  })

  it('runs the fetch ONCE under the production retry policy, not twice', async () => {
    // Under the app's own default policy rather than a `retry: false` client:
    // retrying a deadline would double the very wait it exists to bound.
    mockApi.slashCommands.mockImplementation(timesOut())
    const qc = new QueryClient({ defaultOptions: { queries: { retry: retryPolicy, retryDelay: 0 } } })
    render(<Harness input="/xyz" client={qc} />)
    await screen.findByText(/Couldn't load commands/)
    expect(mockApi.slashCommands).toHaveBeenCalledTimes(1)
  })

  it('still offers the offline fallback rows when the fetch times out', async () => {
    // Negative control on the failure copy: it belongs to the ZERO-ROW state
    // only. A bare slash still lists the fallback, which remains usable.
    mockApi.slashCommands.mockImplementation(timesOut())
    render(<Harness input="/" />)
    expect(await screen.findByText('/compact')).toBeInTheDocument()
    expect(screen.queryByText(/Couldn't load commands/)).not.toBeInTheDocument()
  })

  it('shows the cached rows, not the in-flight row, while a refetch hangs over a populated cache', async () => {
    // A populated cache plus an in-flight refetch is still isFetching, so the
    // loading row must be gated on there being nothing to show, not on fetching.
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false, retryDelay: 0 } } })
    qc.setQueryData(['slash-commands'], [{ name: '/compact', description: 'cached' }])
    mockApi.slashCommands.mockImplementation(() => new Promise(() => {}))
    render(<Harness input="/compact" client={qc} />)
    expect(await screen.findByText('/compact')).toBeInTheDocument()
    await waitFor(() => expect(mockApi.slashCommands).toHaveBeenCalled())
    expect(screen.queryByText(/Loading commands…/)).not.toBeInTheDocument()
  })
})
