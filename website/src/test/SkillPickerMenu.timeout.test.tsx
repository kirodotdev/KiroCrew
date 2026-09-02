import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, waitFor, fireEvent } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { useRef } from 'react'

/* ── Mock api/client BEFORE the component imports ── */
const mockApi = vi.hoisted(() => ({ skills: vi.fn() }))
vi.mock('../api/client', () => ({ api: mockApi }))

import SkillPickerMenu from '../components/SkillPickerMenu'
import { retryPolicy } from '../api/queryClient'

/* The deadline lives in the client layer and is pinned there. What the MENU owes
 * is passing react-query's signal through, and rendering a failure as a failure. */

function Harness({ query = '', open = true, onSelect = vi.fn(), onClose = vi.fn(), client }: {
  query?: string; open?: boolean; onSelect?: (i: { leaf: string; key: string }) => void
  onClose?: () => void; client?: QueryClient
}) {
  const ref = useRef<HTMLDivElement>(null)
  const qc = client ?? new QueryClient({ defaultOptions: { queries: { retry: false, retryDelay: 0 } } })
  return (
    <QueryClientProvider client={qc}>
      <div>
        <div ref={ref} data-testid="anchor">anchor</div>
        <SkillPickerMenu query={query} anchorRef={ref} open={open} onSelect={onSelect} onClose={onClose} />
      </div>
    </QueryClientProvider>
  )
}

/** What the bounded client does to a wedged gateway: reject with TimeoutError. */
const timesOut = () => () =>
  new Promise((_resolve, reject) =>
    setTimeout(() => reject(new DOMException('deadline exceeded', 'TimeoutError')), 5))

beforeEach(() => { vi.clearAllMocks() })
afterEach(() => { vi.restoreAllMocks() })

describe('SkillPickerMenu — a timed-out skills fetch', () => {
  it('runs the fetch ONCE under the production retry policy, not twice', async () => {
    // Under the app's own default policy, not a `retry: false` client: the query
    // must override that default for a deadline, or the wait doubles to ~31s.
    mockApi.skills.mockImplementation(timesOut())
    const qc = new QueryClient({
      defaultOptions: { queries: { retry: retryPolicy, retryDelay: 0 } },
    })
    render(<Harness client={qc} query="grill" />)
    await waitFor(() => expect(screen.queryByText(/Loading skills…/)).not.toBeInTheDocument())
    expect(mockApi.skills).toHaveBeenCalledTimes(1)
  })

  it('passes react-query\'s AbortSignal to api.skills, so the client can bound and cancel it', async () => {
    mockApi.skills.mockImplementation(() => new Promise(() => {}))
    render(<Harness />)
    await waitFor(() => expect(mockApi.skills).toHaveBeenCalled())
    expect(mockApi.skills.mock.calls[0][2]).toBeInstanceOf(AbortSignal)
  })

  it('leaves "Loading skills…" once the fetch settles', async () => {
    // THE DEFECT: `loading = isLoading && open` and isLoading clears only on
    // settle, so an unbounded fetch held the spinner up like a hang.
    mockApi.skills.mockImplementation(timesOut())
    render(<Harness query="grill" />)
    expect(await screen.findByText(/Loading skills…/)).toBeInTheDocument()
    await waitFor(() => expect(screen.queryByText(/Loading skills…/)).not.toBeInTheDocument())
  })

  it('names the failure and releases Enter, so the composer is not deadlocked', async () => {
    // releaseKeysWhenEmpty admits `isError`, so a TimeoutError reaches the same
    // settled state and hands Enter back rather than swallowing it forever.
    mockApi.skills.mockImplementation(timesOut())
    const onSelect = vi.fn()
    render(<Harness query="grill" onSelect={onSelect} />)
    expect(await screen.findByText(/Couldn't load skills — Enter sends the message/)).toBeInTheDocument()
    await waitFor(() => expect(fireEvent.keyDown(document, { key: 'Enter' })).toBe(true))
    expect(onSelect).not.toHaveBeenCalled()
  })
})
