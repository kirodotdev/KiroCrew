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
import { render, screen, waitFor, fireEvent } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { api } from '../api/client'
import { retryPolicy, retryDelayPolicy } from '../api/queryClient'

/* Shrink the real deadline by wrapping the MODULE, keeping the production
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

/** Renders with the SHIPPED retry policy, not the usual `retry: false`.
 *  Returns the client too, so a test can start a read the panel's own controls did not. */
function renderPanel() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: retryPolicy, retryDelay: retryDelayPolicy } },
  })
  const rendered = render(
    <QueryClientProvider client={client}>
      <FolderPanel path={ROOT} onClose={() => {}} />
    </QueryClientProvider>,
  )
  return { ...rendered, client }
}

/**
 * A listing read the Refresh button did NOT start -- the shape of a window-focus or
 * invalidation refetch of `['browse-files', cwd]` -- held open the way a wedged gateway holds it.
 * Resolves once the panel has re-rendered against it (the icon spins for any listing read, so
 * that is the observable), and returns the release.
 */
async function holdBackgroundListingRead(client: QueryClient): Promise<(v: unknown) => void> {
  let release: (v: unknown) => void = () => {}
  vi.spyOn(api, 'browseFiles').mockImplementation((() => new Promise(r => { release = r })) as never)
  void client.refetchQueries({ queryKey: ['browse-files', ROOT], exact: true })
  await waitFor(() => expect(client.getQueryState(['browse-files', ROOT])?.fetchStatus).toBe('fetching'))
  await waitFor(() => expect(screen.getByLabelText('Refresh').querySelector('svg')).toHaveClass('animate-spin'))
  return (v: unknown) => release(v)
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
  it('lets an honestly slow walk finish, so Retry on a timeout can actually succeed', async () => {
    // Scaled 1:100 against the real constant, so a walk standing in for ~9s of honest
    // tree-walking fits a 15s budget and is cut off by a 5s one.
    seen.shrinkTo = FILE_SEARCH_TIMEOUT_MS / 100
    const WALK_MS = 90
    const search$ = vi.spyOn(api, 'fileSearch')
    search$.mockImplementation(((_q: string, _cwd?: string, signal?: AbortSignal) =>
      withDeadline(FILE_SEARCH_TIMEOUT_MS, signal, s =>
        new Promise((resolve, reject) => {
          const t = setTimeout(() => resolve({ results: [] }), WALK_MS)
          s.addEventListener('abort', () => { clearTimeout(t); reject(s.reason) }, { once: true })
        }))) as never)

    renderPanel()
    await search('zz')

    await waitFor(() => expect(search$).toHaveBeenCalled())
    await waitFor(
      () => expect(screen.queryByText(/^Search timed out/)).not.toBeInTheDocument(),
      { timeout: 2_000 },
    )
    expect(await screen.findByText('No files match')).toBeInTheDocument()
    expect(seen.ms.at(-1)).toBe(FILE_SEARCH_TIMEOUT_MS)
  })

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
    expect(await screen.findByText(/^Search timed out/)).toBeInTheDocument()
    expect(screen.queryByText('Searching…')).not.toBeInTheDocument()
  })

  it('names the timeout apart from a gateway failure, not one shared copy', async () => {
    // A slow-but-healthy walk and a gateway that answered with an error need different
    // remedies, so the deadline branch gets its own key rather than "Search failed".
    vi.spyOn(api, 'fileSearch').mockImplementation(wedgedGateway() as never)
    renderPanel()
    await search('zz')
    expect(await screen.findByText(/^Search timed out/)).toBeInTheDocument()
    expect(screen.queryByText(/^Search failed/)).not.toBeInTheDocument()
  })

  it('renders catalog copy for a timeout, never the untranslated reason', async () => {
    // The deadline rejects with a diagnostic DOMException message; rendering
    // `error.message` here would put an untranslated string on screen.
    vi.spyOn(api, 'fileSearch').mockImplementation(wedgedGateway() as never)
    renderPanel()
    await search('zz')
    expect(await screen.findByText(/^Search timed out/)).toBeInTheDocument()
    expect(screen.queryByText(/deadline exceeded/)).not.toBeInTheDocument()
  })

  it('names a listing timeout apart from a gateway failure, as the search branch does', async () => {
    // The panel already said "Search timed out" for a bounded search while the listing two
    // rows above collapsed the same deadline into the generic failure copy.
    const timeout = Object.assign(new Error('deadline exceeded'), { name: 'TimeoutError' })
    vi.spyOn(api, 'browseFiles').mockRejectedValue(timeout as never)
    renderPanel()
    expect(await screen.findByText(/^Folder listing timed out/)).toBeInTheDocument()
    expect(screen.queryByText(/^Unable to list folder/)).not.toBeInTheDocument()
  })

  it('offers no Retry beside a refused search, as the @-menu does', async () => {
    // Re-asking a refused read returns the same answer. This panel now has no Retry at all —
    // the header Refresh is its one recovery control — so a refusal must not reintroduce one.
    const { ApiError } = await import('../api/apiError')
    vi.spyOn(api, 'fileSearch').mockRejectedValue(
      new ApiError(403, 'denied', JSON.stringify({ code: 'access_denied' })) as never)
    renderPanel()
    await search('zz')
    expect(await screen.findByText('No access to this folder')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /^Retry: / })).not.toBeInTheDocument()
  })

  it('names a listing 400 as a missing folder, offering no remedy it cannot honour', async () => {
    // A path that is no longer a directory answers `not_a_directory`. Without that code in the
    // shared map it degraded to a RETRYABLE failure and promised a Refresh that re-fails.
    const { ApiError } = await import('../api/apiError')
    vi.spyOn(api, 'browseFiles').mockRejectedValue(
      new ApiError(400, 'Not a directory', JSON.stringify({ error: 'Not a directory', code: 'not_a_directory' })) as never)
    renderPanel()
    expect(await screen.findByText('Folder not found')).toBeInTheDocument()
    expect(screen.queryByText(/Refresh to retry/)).not.toBeInTheDocument()
  })

  it('leaves an UNNAMED 400 from the listing generic, rather than guessing from the status', async () => {
    // A bare `status === 400` sniff would call ANY 400 a missing folder, so a future 400 from
    // this endpoint would inherit the wrong copy silently. The cause is keyed on `code` alone.
    const { ApiError } = await import('../api/apiError')
    vi.spyOn(api, 'browseFiles').mockRejectedValue(
      new ApiError(400, 'Bad request', JSON.stringify({ error: 'Bad request' })) as never)
    renderPanel()
    expect(await screen.findByText(/^Unable to list folder/)).toBeInTheDocument()
    expect(screen.queryByText('Folder not found')).not.toBeInTheDocument()
  })

  it('still calls a listing failure carrying no status retryable', async () => {
    // The control for the cases above: without it, a mapping that answered `root_missing` for
    // EVERY listing failure would pass those assertions while destroying the generic arm.
    vi.spyOn(api, 'browseFiles').mockRejectedValue(new Error('socket hang up') as never)
    renderPanel()
    expect(await screen.findByText(/^Unable to list folder/)).toBeInTheDocument()
    expect(screen.getByText(/Refresh to retry/)).toBeInTheDocument()
  })

  it('shows the retry in progress, never a stale or generic failure, while a Refresh retry is in flight', async () => {
    // A retry of a never-succeeded query re-enters pending and drops `error`, so the timed-out
    // notice gives way to the "Searching…" status until the retry lands. That is the honest
    // state: the old copy names Refresh as the remedy, and holding it over a retry already under
    // way would invite the second press the header's busy state exists to refuse. What must never
    // appear mid-flight is the generic failure copy. (An earlier form of this test asserted the
    // timed-out copy synchronously on the click, ahead of react-query's deferred re-render, and so
    // passed against a panel that had already replaced it.)
    const search$ = vi.spyOn(api, 'fileSearch')
    search$.mockImplementation(wedgedGateway() as never)
    renderPanel()
    await search('zz')
    await screen.findByText(/^Search timed out/)

    let release: (v: unknown) => void = () => {}
    search$.mockImplementation((() => new Promise(r => { release = r })) as never)
    const refresh = screen.getByLabelText('Refresh')
    fireEvent.click(refresh)

    expect(await screen.findByText('Searching…')).toBeInTheDocument()
    expect(screen.queryByText(/^Search timed out/)).not.toBeInTheDocument()
    expect(screen.queryByText(/^Search failed/)).not.toBeInTheDocument()
    // The header carries the in-flight state for the whole retry, not just the listing's part.
    expect(refresh.querySelector('svg')).toHaveClass('animate-spin')

    release({ results: [], root: ROOT })
    expect(await screen.findByText('No files match')).toBeInTheDocument()
    await waitFor(() => expect(refresh.querySelector('svg')).not.toHaveClass('animate-spin'))
  })

  it('lets Refresh re-run a timed-out search while a listing read it did NOT start is in flight', async () => {
    // THE DEFECT: the press guard read the listing's own `isFetching`, which also fires for reads
    // this button never started -- a window-focus or invalidation refetch of the listing. On a
    // wedged gateway that read stays out for its whole deadline, so a press on Refresh beside
    // "Search timed out -- Refresh to retry" returned early: the search never re-ran, and the
    // spinning icon made it look exactly like a retry under way.
    const fs = vi.spyOn(api, 'fileSearch').mockImplementation(wedgedGateway() as never)
    const { client } = renderPanel()
    await screen.findByText('README.md')
    await search('zz')
    expect(await screen.findByText(/^Search timed out/)).toBeInTheDocument()

    const releaseListing = await holdBackgroundListingRead(client)
    let releaseSearch: (v: unknown) => void = () => {}
    fs.mockImplementation((() => new Promise(r => { releaseSearch = r })) as never)
    const calls = fs.mock.calls.length
    const refresh = screen.getByLabelText('Refresh')
    fireEvent.click(refresh)

    // The press has to reach the search: a background listing read is not this button's batch.
    await waitFor(() => expect(fs.mock.calls.length).toBeGreaterThan(calls))
    expect(fs).toHaveBeenLastCalledWith('zz', ROOT, expect.any(AbortSignal), 'files', 15)

    releaseListing(listing())
    releaseSearch({ results: [], root: ROOT })
    expect(await screen.findByText('No files match')).toBeInTheDocument()
    await waitFor(() => expect(refresh.querySelector('svg')).not.toHaveClass('animate-spin'))
  })

  it('does not tell assistive tech Refresh is inert over a listing read it did not start', async () => {
    // Same root as the test above, on the a11y side: `aria-disabled` followed the spinner's
    // state, so a screen reader was told the control was inert during a read the press could
    // (after the fix, does) proceed past. The spinner may show the listing read -- navigation and
    // the first load always have -- but "inert" is a statement about the press, not the icon.
    vi.spyOn(api, 'fileSearch').mockImplementation(wedgedGateway() as never)
    const { client } = renderPanel()
    await screen.findByText('README.md')
    await search('zz')
    expect(await screen.findByText(/^Search timed out/)).toBeInTheDocument()

    const releaseListing = await holdBackgroundListingRead(client)
    const refresh = screen.getByLabelText('Refresh')
    expect(refresh.querySelector('svg')).toHaveClass('animate-spin')
    expect(refresh).toHaveAttribute('aria-disabled', 'false')

    releaseListing(listing())
    await waitFor(() => expect(refresh.querySelector('svg')).not.toHaveClass('animate-spin'))
  })

  it('still refuses a second press while a PRESS-started retry is in flight', async () => {
    // The control for the two tests above: narrowing the guard to the press's own bracket must
    // keep the defects it was added for fixed. A second press during a press-started batch
    // dispatches nothing -- no `refetchQueries` restart of the running walk, and so no first
    // `finally` clearing the flag under a second press's work.
    const fs = vi.spyOn(api, 'fileSearch').mockImplementation(wedgedGateway() as never)
    const browse = vi.spyOn(api, 'browseFiles')
    renderPanel()
    await screen.findByText('README.md')
    await search('zz')
    expect(await screen.findByText(/^Search timed out/)).toBeInTheDocument()

    let release: (v: unknown) => void = () => {}
    fs.mockImplementation((() => new Promise(r => { release = r })) as never)
    const refresh = screen.getByLabelText('Refresh')
    fireEvent.click(refresh)
    expect(await screen.findByText('Searching…')).toBeInTheDocument()
    const searchCalls = fs.mock.calls.length
    const browseCalls = browse.mock.calls.length
    expect(refresh).toHaveAttribute('aria-disabled', 'true')

    fireEvent.click(refresh)
    await new Promise(r => setTimeout(r, 50))
    expect(fs.mock.calls.length).toBe(searchCalls)
    expect(browse.mock.calls.length).toBe(browseCalls)

    release({ results: [], root: ROOT })
    expect(await screen.findByText('No files match')).toBeInTheDocument()
    await waitFor(() => expect(refresh.querySelector('svg')).not.toHaveClass('animate-spin'))
    expect(refresh).toHaveAttribute('aria-disabled', 'false')
  })

  it('leaves the listing notice to the header Refresh, with no adjacent button', async () => {
    // The listing Retry only ever called the header Refresh this panel already renders, so it
    // was one action spelled twice; the header stays mounted beside the notice.
    const timeout = Object.assign(new Error('deadline exceeded'), { name: 'TimeoutError' })
    const browse = vi.spyOn(api, 'browseFiles').mockRejectedValue(timeout as never)
    renderPanel()
    expect(await screen.findByText(/^Folder listing timed out/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /^Retry: Folder listing/ })).not.toBeInTheDocument()

    const refresh = screen.getByLabelText('Refresh')
    expect(refresh).toBeEnabled()
    browse.mockResolvedValue(listing() as never)
    const before = browse.mock.calls.length
    fireEvent.click(refresh)
    await waitFor(() => expect(browse.mock.calls.length).toBeGreaterThan(before))
  })

  it('re-reads the listing from Refresh after a NON-timeout failure too', async () => {
    // The `failed` arm names Refresh as its remedy exactly as the timeout arm does, so the
    // control has to re-ask for that cause as well.
    const browse = vi.spyOn(api, 'browseFiles').mockRejectedValue(new Error('Failed to fetch') as never)
    renderPanel()
    expect(await screen.findByText(/^Unable to list folder/)).toBeInTheDocument()
    browse.mockResolvedValue(listing() as never)
    fireEvent.click(screen.getByLabelText('Refresh'))
    await waitFor(() => expect(browse).toHaveBeenCalledTimes(2))
    expect(browse).toHaveBeenLastCalledWith(ROOT, expect.any(AbortSignal))
  })

  it('routes a listing timeout to catalog copy, never the raw deadline message', async () => {
    // The listing branch rendered the error's own message, so bounding the listing
    // surfaced English jargon in all 12 catalogs. Pins it as the search branch is.
    vi.spyOn(api, 'browseFiles').mockRejectedValue(new Error('deadline exceeded') as never)
    renderPanel()
    expect(await screen.findByText(/^Unable to list folder/)).toBeInTheDocument()
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
    expect(await screen.findByText(/^Search failed/)).toBeInTheDocument()
    expect(screen.queryByText(/Failed to fetch/)).not.toBeInTheDocument()
  })

  it('retries the failed search from the header Refresh, not just the listing', async () => {
    // Refresh sits beside the failure copy, so refetching only the listing behind it
    // leaves the obvious retry doing nothing about the thing that actually failed.
    const spy = vi.spyOn(api, 'fileSearch').mockImplementation(wedgedGateway() as never)
    renderPanel()
    await search('zz')
    expect(await screen.findByText(/^Search timed out/)).toBeInTheDocument()
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

  it('has the Refresh label width reserved BEFORE the timeout notice names it', async () => {
    // The notice names the header Refresh the instant it lands. Growing the control at that
    // moment shifts it under a cursor already reaching for it, so the width is taken while the
    // box holds the query -- ahead of the deadline -- and the notice only reveals the label.
    vi.spyOn(api, 'fileSearch').mockImplementation(wedgedGateway() as never)
    renderPanel()
    await screen.findByText('README.md')
    await search('zz')

    const refresh = screen.getByRole('button', { name: 'Refresh' })
    expect(refresh).not.toHaveClass('w-[26px]')
    expect(refresh.querySelector('span')).toHaveClass('invisible')
    const reserved = refresh.className

    expect(await screen.findByText(/^Search timed out/)).toBeInTheDocument()
    // Same box, same classes: the notice landing moved nothing but the label's visibility.
    expect(refresh.className).toBe(reserved)
    expect(refresh.querySelector('span')).not.toHaveClass('invisible')
  })
})
