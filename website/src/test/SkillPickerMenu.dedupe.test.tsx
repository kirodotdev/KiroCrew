import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, waitFor, fireEvent } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { useRef } from 'react'

/* ── Mock api/client BEFORE the component imports ── */
const mockApi = vi.hoisted(() => ({ skills: vi.fn() }))
vi.mock('../api/client', () => ({ api: mockApi }))

import SkillPickerMenu from '../components/SkillPickerMenu'
import { skillsCacheStaleTime } from '../lib/skillsCache'

const SLOT = 'dashboard:chat-1'
const KEY = ['skills', SLOT, null, null]

function Harness({ client, query = '', open = true, onSelect = vi.fn(), onClose = vi.fn() }: {
  client: QueryClient; query?: string; open?: boolean
  onSelect?: (i: { leaf: string; key: string }) => void; onClose?: () => void
}) {
  const ref = useRef<HTMLDivElement>(null)
  return (
    <QueryClientProvider client={client}>
      <div>
        <div ref={ref} data-testid="anchor">anchor</div>
        <SkillPickerMenu query={query} anchorRef={ref} open={open}
          onSelect={onSelect} onClose={onClose} slotKey={SLOT} />
      </div>
    </QueryClientProvider>
  )
}

/** What the bounded client does to a wedged gateway: reject with TimeoutError —
 *  but on the TEST's clock, not a wall-clock timer. A timer-based deadline
 *  raced the menu's mount: once it fired first the prefetch was already
 *  settled, nothing was in flight to dedupe onto, and staleTime 0 (see
 *  skillsCacheStaleTime(undefined)) made the menu fetch again, so the
 *  one-call assertion failed on a slow runner rather than on a real
 *  regression. `expire()` fires the same rejection at a chosen point. */
function wedgedGateway() {
  let expire = () => {}
  const impl = () => new Promise((_resolve, reject) => {
    expire = () => reject(new DOMException('deadline exceeded', 'TimeoutError'))
  })
  return { impl, expire: () => expire() }
}

beforeEach(() => { vi.clearAllMocks() })
afterEach(() => { vi.restoreAllMocks() })

describe('SkillPickerMenu — deduping onto ChatInput\'s in-flight prefetch', () => {
  /** ChatInput's focus prefetch, reproduced: same query key, same staleTime, and
   *  the same signal pass-through. */
  const startPrefetch = (client: QueryClient) =>
    client.prefetchQuery({
      queryKey: KEY,
      queryFn: ({ signal }) => mockApi.skills(SLOT, undefined, signal),
      staleTime: skillsCacheStaleTime(undefined),
    })

  it('reuses the in-flight promise — ONE fetch — and still settles, releasing Enter', async () => {
    // Dedupe on the shared key means the menu never runs its own queryFn, which
    // is why the deadline is bound in the client: the winner fetches for both.
    const gateway = wedgedGateway()
    mockApi.skills.mockImplementation(gateway.impl)
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false, retryDelay: 0 } } })
    void startPrefetch(qc)
    await waitFor(() => expect(mockApi.skills).toHaveBeenCalled())
    const onSelect = vi.fn()
    render(<Harness client={qc} query="grill" onSelect={onSelect} />)
    // Mounted and subscribed to the SAME key while the prefetch is still in
    // flight — the only window in which dedupe is even the question. Asserting
    // it here is what makes the one-call count below a claim about dedupe
    // instead of a claim about which of two timers won.
    expect(await screen.findByText(/Loading skills…/)).toBeInTheDocument()
    expect(mockApi.skills).toHaveBeenCalledTimes(1)
    // Now let the shared deadline expire: one rejection settles both readers.
    gateway.expire()
    await waitFor(() => expect(screen.queryByText(/Loading skills…/)).not.toBeInTheDocument())
    await waitFor(() => expect(fireEvent.keyDown(document, { key: 'Enter' })).toBe(true))
    expect(onSelect).not.toHaveBeenCalled()
    // Still one call after settling: the menu rode the prefetch's promise
    // rather than running its own bounded fetch alongside it.
    expect(mockApi.skills).toHaveBeenCalledTimes(1)
  })

  it('every initiator hands api.skills a signal, so none of them can be the unbounded one', async () => {
    mockApi.skills.mockImplementation(() => new Promise(() => {}))
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false, retryDelay: 0 } } })
    void startPrefetch(qc)
    await waitFor(() => expect(mockApi.skills).toHaveBeenCalled())
    expect(mockApi.skills.mock.calls[0][2]).toBeInstanceOf(AbortSignal)
    // And the menu's own queryFn, when IT wins the race, does the same.
    mockApi.skills.mockClear()
    const qc2 = new QueryClient({ defaultOptions: { queries: { retry: false, retryDelay: 0 } } })
    render(<Harness client={qc2} query="grill" />)
    await waitFor(() => expect(mockApi.skills).toHaveBeenCalled())
    expect(mockApi.skills.mock.calls[0][2]).toBeInstanceOf(AbortSignal)
  })
})

describe('SkillPickerMenu — a failed load is not an empty catalog', () => {
  it('names the failure instead of claiming there are no matching skills', async () => {
    mockApi.skills.mockRejectedValue(new Error('boom'))
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false, retryDelay: 0 } } })
    render(<Harness client={qc} query="grill" />)
    expect(await screen.findByText(/Couldn't load skills — Enter sends the message/))
      .toBeInTheDocument()
    // The false claim must be gone, not merely accompanied.
    expect(screen.queryByText(/No matching skills/)).not.toBeInTheDocument()
  })

  it('still releases Enter on the error path — that half was already correct', async () => {
    mockApi.skills.mockRejectedValue(new Error('boom'))
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false, retryDelay: 0 } } })
    const onSelect = vi.fn(); const onClose = vi.fn()
    render(<Harness client={qc} query="grill" onSelect={onSelect} onClose={onClose} />)
    await screen.findByText(/Couldn't load skills/)
    await waitFor(() => expect(fireEvent.keyDown(document, { key: 'Enter' })).toBe(true))
    expect(onClose).toHaveBeenCalled()
    expect(onSelect).not.toHaveBeenCalled()
  })

  it('a genuinely empty catalog still says "No matching skills"', async () => {
    // Negative control on the new branch: it must key on the ERROR, not fire for
    // every empty list, or it would replace a true statement with a false one.
    mockApi.skills.mockResolvedValue([])
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false, retryDelay: 0 } } })
    render(<Harness client={qc} query="grill" />)
    expect(await screen.findByText(/No matching skills/)).toBeInTheDocument()
    expect(screen.queryByText(/Couldn't load skills/)).not.toBeInTheDocument()
  })
})
