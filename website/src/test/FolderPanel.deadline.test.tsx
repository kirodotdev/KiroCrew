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
 *  `outside` mounts beside the panel, standing in for the rest of the page. */
function renderPanel(outside?: React.ReactNode) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: retryPolicy, retryDelay: retryDelayPolicy } },
  })
  return render(
    <QueryClientProvider client={client}>
      <FolderPanel path={ROOT} onClose={() => {}} />
      {outside}
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

  it('restores focus on a recovered retry even with an EARLIER failed search still cached', async () => {
    // A prefix check counted a PREVIOUS query's cached failure, so the recovered retry
    // unmounted its own button without handing focus anywhere.
    const fs = vi.spyOn(api, 'fileSearch')
    fs.mockImplementation(wedgedGateway() as never)
    renderPanel()
    await search('zz')
    expect(await screen.findByText(/^Search timed out/)).toBeInTheDocument()

    // Appending leaves the FIRST query's failure cached under a sibling key, and that
    // sibling keeps failing after the refetch — so a prefix check still sees an error.
    await search('z')
    await waitFor(() => expect(fs.mock.calls.some(c => c[0] === 'zzz')).toBe(true))
    // Let the ACTIVE query reach its deadline: the scenario is a user looking at a failed
    // search, not one clicking mid-flight.
    expect(await screen.findByText(/^Search timed out/)).toBeInTheDocument()
    const refresh = screen.getByLabelText('Refresh')
    fs.mockImplementation(((q: string, cwd?: string, signal?: AbortSignal) =>
      (q === 'zz'
        ? wedgedGateway()(q, cwd, signal)
        : Promise.resolve({ matches: [] }))) as never)
    refresh.focus()
    fireEvent.click(refresh)
    await waitFor(() => expect(document.activeElement)
      .toBe(screen.getByPlaceholderText('Search files')))
  })

  it('leaves focus alone when the user moved on while the retry was in flight', async () => {
    // The await behind Refresh runs for up to the search deadline. A user who pressed it and then
    // clicked into the chat composer has put the caret where they want it, so the recovered retry
    // hands focus to the search box only while THIS panel still holds it -- never yanks it back.
    const fs = vi.spyOn(api, 'fileSearch')
    fs.mockImplementation(wedgedGateway() as never)
    renderPanel(<textarea aria-label="composer" />)
    await search('zz')
    expect(await screen.findByText(/^Search timed out/)).toBeInTheDocument()

    let release: (v: unknown) => void = () => {}
    fs.mockImplementation((() => new Promise(r => { release = r })) as never)
    const refresh = screen.getByLabelText('Refresh')
    refresh.focus()
    fireEvent.click(refresh)
    expect(await screen.findByText('Searching…')).toBeInTheDocument()

    const composer = screen.getByLabelText('composer')
    composer.focus()
    expect(document.activeElement).toBe(composer)

    release({ results: [], root: ROOT })
    expect(await screen.findByText('No files match')).toBeInTheDocument()
    // The spinner stops in the OUTER finally, after the focus hand-off has already run.
    await waitFor(() => expect(refresh.querySelector('svg')).not.toHaveClass('animate-spin'))
    expect(document.activeElement).toBe(composer)
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
