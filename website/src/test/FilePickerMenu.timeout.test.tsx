import { useRef } from 'react'
import { screen, fireEvent, waitFor } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import { api, ApiError } from '../api/client'

vi.mock('../api/client', async importOriginal => {
  const mod = await importOriginal<typeof import('../api/client')>()
  return { ...mod, api: { ...mod.api, fileSearch: vi.fn() } }
})

/**
 * Shrink the real 5s deadline by wrapping the MODULE rather than stubbing a
 * timer or `AbortSignal.timeout`. That keeps the production composition under
 * test and changes only the duration, so these tests exercise the same
 * `withDeadline` the component ships with.
 *
 * Stubbing a global instead is how a test in this family passes for the wrong
 * reason: a detached `AbortSignal.timeout` reference throws `TypeError` inside
 * the stub (happy-dom reads an internal window slot off `this`), react-query
 * catches it, and the component lands in the very settled-error state the
 * assertions are looking for — with the fix effectively absent.
 */
const seen = vi.hoisted(() => ({ ms: [] as number[], shrinkTo: 20 }))
vi.mock('../lib/withDeadline', async () => {
  const real = await vi.importActual<typeof import('../lib/withDeadline')>('../lib/withDeadline')
  return {
    withDeadline: (ms: number, outer: AbortSignal | undefined,
                   attempt: (s: AbortSignal) => Promise<unknown>) => {
      seen.ms.push(ms)
      return real.withDeadline(seen.shrinkTo, outer, attempt)
    },
  }
})

import FilePickerMenu from '../components/FilePickerMenu'
import { FILE_SEARCH_TIMEOUT_MS } from '../api/client'
import { withDeadline } from '../lib/withDeadline'
import { retryPolicy, retryDelayPolicy } from '../api/queryClient'

const fileSearch = vi.mocked(api.fileSearch)

/**
 * A wedged gateway behind the SAME deadline the real `api.fileSearch` binds, so
 * the mock stands in for the bounded client rather than for a bare fetch.
 *
 * The inner promise settles ONLY when its signal aborts, and given no signal
 * NEVER settles at all. That second half is the pre-fix behaviour exactly, which
 * is what makes these assertions a real negative control rather than a
 * tautology — drop the deadline binding and the fetch stays pending.
 */
const wedgedGateway = () =>
  (_q: string, _project?: string, signal?: AbortSignal) =>
    withDeadline(FILE_SEARCH_TIMEOUT_MS, signal, s =>
      new Promise((_resolve, reject) => {
        if (s.aborted) return reject(s.reason)
        s.addEventListener('abort', () => reject(s.reason), { once: true })
      }))

/** The picker positions against a live anchor, so give it a real element. */
function Host(props: Omit<React.ComponentProps<typeof FilePickerMenu>, 'anchorRef'>) {
  const ref = useRef<HTMLDivElement>(null)
  return (
    <>
      <div ref={ref} data-testid="zzq-anchor" />
      <FilePickerMenu {...props} anchorRef={ref} />
    </>
  )
}

function mount(props: Partial<React.ComponentProps<typeof FilePickerMenu>> = {}) {
  const onSelect = vi.fn()
  const onClose = vi.fn()
  const view = renderWithProviders(
    <Host query="zz" open onSelect={onSelect} onClose={onClose} {...props} />,
  )
  return { onSelect, onClose, ...view }
}

/**
 * Mount with the SHIPPED retry policy instead of the test helper's `retry: false`.
 *
 * Load-bearing: the shared render helper disables retries by default, so a test
 * rendered through it cannot observe the production behaviour at all and would
 * be a vacuous gate. The real `retryPolicy` refuses to retry a deadline, and this
 * query adds no override, so the bound is what settles it.
 */
function mountWithShippedRetry(props: Partial<React.ComponentProps<typeof FilePickerMenu>> = {}) {
  const onSelect = vi.fn()
  const onClose = vi.fn()
  const view = renderWithProviders(
    <Host query="zz" open onSelect={onSelect} onClose={onClose} {...props} />,
    { queryDefaults: { retry: retryPolicy, retryDelay: retryDelayPolicy } },
  )
  return { onSelect, onClose, ...view }
}

beforeEach(() => {
  vi.clearAllMocks()
  seen.ms = []
  seen.shrinkTo = 20
})
afterEach(() => { vi.restoreAllMocks() })

describe('FilePickerMenu — bounded /api/file-search fetch', () => {
  it('still hands api.fileSearch an AbortSignal (the deadline must not drop it)', async () => {
    // The wrapper substitutes its OWN signal for react-query's; the call must
    // keep receiving one in slot 3 so unmount/cancel still aborts the request.
    fileSearch.mockImplementation(wedgedGateway() as never)
    mount()
    await waitFor(() => expect(fileSearch).toHaveBeenCalled())
    expect(fileSearch.mock.calls[0][2]).toBeInstanceOf(AbortSignal)
  })

  it('asks for a deadline of FILE_SEARCH_TIMEOUT_MS, bounded either side', async () => {
    // Pinned on the REQUESTED value, not elapsed wall-clock, so the constant
    // cannot drift silently without this failing.
    fileSearch.mockImplementation(wedgedGateway() as never)
    mount()
    await waitFor(() => expect(seen.ms.length).toBeGreaterThan(0))
    expect(seen.ms).toContain(FILE_SEARCH_TIMEOUT_MS)
    // Comfortably clear of the 200ms debounce in front of the query...
    expect(FILE_SEARCH_TIMEOUT_MS).toBeGreaterThan(200 * 10)
    // ...and strictly shorter than the once-per-open $-menu's 15s, because this
    // one fires per keystroke and its answer expires as the user keeps typing.
    expect(FILE_SEARCH_TIMEOUT_MS).toBeLessThan(15_000)
  })

  it('clears "Searching…" when the deadline fires on a response that never arrives', async () => {
    // THE DEFECT: unbounded, the query stayed pending and the menu showed
    // "Searching…" forever — indistinguishable from a hang.
    fileSearch.mockImplementation(wedgedGateway() as never)
    // 250ms (not the 20ms the others use) so the pending state is observable
    // before the deadline fires, making both halves deterministic.
    seen.shrinkTo = 250
    const onSelect = vi.fn()
    const onClose = vi.fn()
    const { rerender } = mount({ onSelect, onClose })
    // The rerender is load-bearing: anchorRef.current is null on first render,
    // so the menu returns null and paints nothing until something re-renders it.
    rerender(<Host query="zz" open onSelect={onSelect} onClose={onClose} />)
    // Matched by substring: the held/sends suffix is the composer's copy, not this
    // test's subject, which is that the pending state CLEARS when the deadline fires.
    expect(await screen.findByText(/Searching…/)).toBeInTheDocument()
    await waitFor(() => expect(screen.queryByText(/Searching…/)).not.toBeInTheDocument())
  })

  it('settles into the released-Enter empty state, so the composer is not deadlocked', async () => {
    // releaseKeysWhenEmpty admits `isError`, so a TimeoutError reaches the same
    // settled state and hands Enter back instead of swallowing it forever.
    seen.shrinkTo = 20
    fileSearch.mockImplementation(wedgedGateway() as never)
    const { onSelect, onClose } = mount()
    expect(await screen.findByText(/File search timed out — Enter sends the message/)).toBeInTheDocument()
    await waitFor(() => expect(fireEvent.keyDown(document, { key: 'Enter' })).toBe(true))
    expect(onClose).toHaveBeenCalled()
    expect(onSelect).not.toHaveBeenCalled()
  })

  it('does not retry under the SHIPPED retry policy, so the bound is the one advertised', async () => {
    // Neither policy retries a deadline, so the advertised bound is the real one:
    // no second attempt extends the window Enter stays swallowed.
    seen.shrinkTo = 20
    fileSearch.mockImplementation(wedgedGateway() as never)
    const { onClose } = mountWithShippedRetry()
    await waitFor(() => expect(fileSearch).toHaveBeenCalledTimes(1))

    // Past the 1s retry backoff: a second attempt would have been made by now.
    await new Promise(r => setTimeout(r, 1_400))
    expect(fileSearch).toHaveBeenCalledTimes(1)

    // And the query is settled, so the release gate is armed rather than
    // swallowing Enter across a retry window.
    expect(fireEvent.keyDown(document, { key: 'Enter' })).toBe(true)
    expect(onClose).toHaveBeenCalled()
  })

  it('retries a transient 429 under the SHIPPED policy, so a throttle is not a failed search', async () => {
    // Inverted deliberately: it previously pinned the `retry: false` the GPT lane
    // blocked. Non-vacuous -- with the override still present, attempts stays at 1.
    let attempts = 0
    fileSearch.mockImplementation((() => {
      attempts += 1
      return Promise.reject(new ApiError(429, 'Rate exceeded'))
    }) as never)
    mountWithShippedRetry()

    // Past the 1s + jitter the shared ladder waits before attempt two.
    await waitFor(() => expect(attempts).toBeGreaterThan(1), { timeout: 4_000 })
  })

  it('says the search FAILED rather than reporting no matches (a timeout is not an absence)', async () => {
    // Routing a wedged gateway into the settled-empty copy tells a user the file
    // they are looking for does not exist. That is a false negative, not a hint.
    seen.shrinkTo = 20
    fileSearch.mockImplementation(wedgedGateway() as never)
    mount()
    expect(await screen.findByText(/File search timed out — Enter sends the message/))
      .toBeInTheDocument()
    expect(screen.queryByText(/No matching files/)).not.toBeInTheDocument()
  })

  it('names Ctrl+Enter in the failure copy when that is the send binding', async () => {
    seen.shrinkTo = 20
    fileSearch.mockImplementation(wedgedGateway() as never)
    mount({ sendOnEnter: 'ctrl-enter' })
    expect(await screen.findByText(/File search timed out — Ctrl\+Enter sends the message/))
      .toBeInTheDocument()
  })

  it('announces the empty and failed copy to a screen reader (role="alert")', async () => {
    // The copy exists to prevent a silent-send surprise when Enter's meaning
    // flips, so a visually-only announcement leaves that user with the surprise.
    seen.shrinkTo = 20
    fileSearch.mockImplementation(wedgedGateway() as never)
    mount()
    const status = await screen.findByRole('alert')
    expect(status).toHaveTextContent(/File search timed out — Enter sends the message/)
  })

  it('colours the failure branch differently from the empty branch', async () => {
    // The failed/empty distinction this change exists to draw must not be
    // legible only by reading the words.
    seen.shrinkTo = 20
    fileSearch.mockImplementation(wedgedGateway() as never)
    mount()
    const failed = await screen.findByRole('alert')
    expect(failed.className).toContain('text-danger')
    expect(failed.className).not.toContain('text-muted')
    // The empty branch keeps role="status"; only a failure is an alert.
    expect(screen.queryByRole('status')).toBeNull()
  })

  it('keeps the settled-empty branch muted, and still announces its copy', async () => {
    fileSearch.mockResolvedValue({ results: [], root: '/root' } as never)
    mount()
    const empty = await screen.findByRole('status')
    expect(empty).toHaveTextContent(/No matching files/)
    expect(empty.className).toContain('text-muted')
    expect(empty.className).not.toContain('text-danger')
  })

  it('names the timeout apart from a gateway failure, not one shared copy', async () => {
    // A plain rejection must still read "failed": the distinction is the point, not a rename.
    seen.shrinkTo = 20
    fileSearch.mockImplementation(wedgedGateway() as never)
    mountWithShippedRetry()
    expect(await screen.findByText(/File search timed out/)).toBeInTheDocument()
    expect(screen.queryByText(/File search failed/)).not.toBeInTheDocument()
  })

  it('shows the failure over CACHED rows when a later fetch fails', async () => {
    // React Query keeps the last successful `data` for a key when a refetch of that
    // same key fails, so isError is true while results.length is still > 0.
    fileSearch.mockResolvedValueOnce({
      results: [{ path: '/p/kept.ts', name: 'kept.ts', size: 1, mtime: 1 }],
      root: '/p',
    } as never)
    const { queryClient } = mount({ query: 'zz' })
    expect(await screen.findByText('kept.ts')).toBeInTheDocument()

    fileSearch.mockRejectedValue(new ApiError(500, 'boom'))
    await queryClient.refetchQueries({ queryKey: ['file-search'] })

    expect(await screen.findByRole('alert')).toHaveTextContent(/File search failed/)
    expect(screen.queryByText('kept.ts')).toBeNull()
  })

  it('takes the cached rows out of keyboard reach too, not just out of the render', async () => {
    // Hiding the rows is not enough: while they stayed in the collection the hook
    // walks, Arrow+Enter activated a row with nothing on screen to show which.
    fileSearch.mockResolvedValueOnce({
      results: [{ path: '/p/kept.ts', name: 'kept.ts', size: 1, mtime: 1 }],
      root: '/p',
    } as never)
    const { queryClient, onSelect } = mount({ query: 'zz' })
    expect(await screen.findByText('kept.ts')).toBeInTheDocument()

    fileSearch.mockRejectedValue(new ApiError(500, 'boom'))
    await queryClient.refetchQueries({ queryKey: ['file-search'] })
    await screen.findByRole('alert')

    fireEvent.keyDown(document, { key: 'ArrowDown' })
    fireEvent.keyDown(document, { key: 'Enter' })
    expect(onSelect).not.toHaveBeenCalled()
  })
})
