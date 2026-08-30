/**
 * FolderPanel's search shares `/api/file-search` with the @-mention picker, so it
 * shares the picker's hazard: without a deadline a wedged gateway never settles
 * and the panel shows "Searching…" indefinitely.
 *
 * These tests build their client with the SHIPPED retryPolicy rather than the
 * `retry: false` every other harness here uses, because a client with retries
 * disabled cannot observe a retry-policy defect at all and would be a vacuous
 * gate.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { api } from '../api/client'
import { retryPolicy, retryDelayPolicy } from '../api/queryClient'

/* Shrink the real 5s deadline by wrapping the MODULE, keeping the production
 * composition under test and changing only the duration. */
const seen = vi.hoisted(() => ({ ms: [] as number[], shrinkTo: 40 }))
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

import FolderPanel from '../pages/chat/FolderPanel'
import { FILE_SEARCH_TIMEOUT_MS } from '../api/client'
import { withDeadline } from '../lib/withDeadline'

const ROOT = '/proj'

function listing() {
  return { path: ROOT, parent: '/', dirs: [], files: [{ name: 'README.md', path: `${ROOT}/README.md` }] }
}

/**
 * A wedged gateway behind the SAME deadline the real `api.fileSearch` binds, so
 * the mock stands in for the bounded client rather than for a bare fetch.
 *
 * The inner promise settles ONLY when its signal aborts, and given no signal
 * NEVER settles. That second half is the pre-fix behaviour exactly, so these
 * assertions are a real control rather than a tautology.
 */
const wedgedGateway = () =>
  (_q: string, _cwd?: string, signal?: AbortSignal) =>
    withDeadline(FILE_SEARCH_TIMEOUT_MS, signal, s =>
      new Promise((_resolve, reject) => {
        if (s.aborted) return reject(s.reason)
        s.addEventListener('abort', () => reject(s.reason), { once: true })
      }))

/** Renders with the SHIPPED retry policy, not the usual `retry: false`. */
function renderPanel() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: retryPolicy, retryDelay: retryDelayPolicy } },
  })
  return render(
    <QueryClientProvider client={client}>
      <FolderPanel path={ROOT} onClose={() => {}} />
    </QueryClientProvider>,
  )
}

async function search(text: string) {
  const user = userEvent.setup()
  await user.type(screen.getByLabelText('Search files'), text)
}

beforeEach(() => {
  seen.ms = []
  seen.shrinkTo = 40
  vi.spyOn(api, 'browseFiles').mockResolvedValue(listing() as never)
  vi.spyOn(api, 'revealPath').mockResolvedValue(undefined as never)
})
afterEach(() => { vi.restoreAllMocks() })

describe('FolderPanel — bounded /api/file-search', () => {
  it('asks for the shared file-search deadline', async () => {
    vi.spyOn(api, 'fileSearch').mockImplementation(wedgedGateway() as never)
    renderPanel()
    await search('zz')
    await waitFor(() => expect(seen.ms.length).toBeGreaterThan(0))
    expect(seen.ms).toContain(FILE_SEARCH_TIMEOUT_MS)
  })

  it('settles a wedged search instead of showing "Searching…" forever', async () => {
    // THE DEFECT: unbounded, this query stayed pending and the panel spun with
    // no error surface and no way to tell a slow walk from a dead gateway.
    vi.spyOn(api, 'fileSearch').mockImplementation(wedgedGateway() as never)
    renderPanel()
    await search('zz')
    expect(await screen.findByText('Search timed out')).toBeInTheDocument()
    expect(screen.queryByText('Searching…')).not.toBeInTheDocument()
  })

  it('names the timeout apart from a gateway failure, not one shared copy', async () => {
    // A slow-but-healthy walk and a gateway that answered with an error need different
    // remedies, so the deadline branch gets its own key rather than "Search failed".
    vi.spyOn(api, 'fileSearch').mockImplementation(wedgedGateway() as never)
    renderPanel()
    await search('zz')
    expect(await screen.findByText('Search timed out')).toBeInTheDocument()
    expect(screen.queryByText('Search failed')).not.toBeInTheDocument()
  })

  it('renders catalog copy for a timeout, never the untranslated reason', async () => {
    // The deadline rejects with a diagnostic DOMException message; rendering
    // `error.message` here would put an untranslated string on screen.
    vi.spyOn(api, 'fileSearch').mockImplementation(wedgedGateway() as never)
    renderPanel()
    await search('zz')
    expect(await screen.findByText('Search timed out')).toBeInTheDocument()
    expect(screen.queryByText(/deadline exceeded/)).not.toBeInTheDocument()
  })

  it('names a listing timeout apart from a gateway failure, as the search branch does', async () => {
    // The panel already said "Search timed out" for a bounded search while the listing two
    // rows above collapsed the same deadline into the generic failure copy.
    const timeout = Object.assign(new Error('deadline exceeded'), { name: 'TimeoutError' })
    vi.spyOn(api, 'browseFiles').mockRejectedValue(timeout as never)
    renderPanel()
    expect(await screen.findByText('Folder listing timed out')).toBeInTheDocument()
    expect(screen.queryByText('Unable to list folder')).not.toBeInTheDocument()
  })

  it('routes a listing timeout to catalog copy, never the raw deadline message', async () => {
    // The listing branch rendered the error's own message, so bounding the listing
    // surfaced English jargon in all 12 catalogs. Pins it as the search branch is.
    vi.spyOn(api, 'browseFiles').mockRejectedValue(new Error('deadline exceeded') as never)
    renderPanel()
    expect(await screen.findByText('Unable to list folder')).toBeInTheDocument()
    expect(screen.queryByText(/deadline exceeded/)).not.toBeInTheDocument()
  })

  it('does not retry the timed-out search under the SHIPPED retry policy', async () => {
    // This query ships `retry: false`, so no retry is owed for any error, and the
    // harness's shared policy cannot grant one either.
    const spy = vi.spyOn(api, 'fileSearch').mockImplementation(wedgedGateway() as never)
    renderPanel()
    await search('zz')
    await waitFor(() => expect(spy).toHaveBeenCalledTimes(1))
    // Past the backoff a retry would have waited, had either policy allowed one.
    await new Promise(r => setTimeout(r, 1_400))
    expect(spy).toHaveBeenCalledTimes(1)
  })

  it('routes a NON-timeout failure to catalog copy too, never the raw exception text', async () => {
    // A network error's `.message` is untranslated engine text ("Failed to
    // fetch"), which is not UI copy in a twelve-language interface.
    vi.spyOn(api, 'fileSearch').mockRejectedValue(new Error('Failed to fetch') as never)
    renderPanel()
    await search('zz')
    expect(await screen.findByText('Search failed')).toBeInTheDocument()
    expect(screen.queryByText(/Failed to fetch/)).not.toBeInTheDocument()
  })

  it('retries the failed search from the header Refresh, not just the listing', async () => {
    // Refresh sits beside the failure copy, so refetching only the listing behind it
    // leaves the obvious retry doing nothing about the thing that actually failed.
    const spy = vi.spyOn(api, 'fileSearch').mockImplementation(wedgedGateway() as never)
    renderPanel()
    await search('zz')
    expect(await screen.findByText('Search timed out')).toBeInTheDocument()
    const calls = spy.mock.calls.length
    await userEvent.click(screen.getByLabelText('Refresh'))
    await waitFor(() => expect(spy.mock.calls.length).toBeGreaterThan(calls))
  })

  it('announces the failure through a live region', async () => {
    // Without an announced region a screen-reader user is told nothing at all;
    // ErrorNotice's role="alert" is the assertive form of that guarantee.
    vi.spyOn(api, 'fileSearch').mockImplementation(wedgedGateway() as never)
    renderPanel()
    await search('zz')
    await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent('Search timed out'))
  })
})
