/**
 * FolderPanel's recursive, files-only search.
 *
 * The behaviours pinned here are the ones a refactor can silently break without
 * failing anything else: that search goes to `/api/file-search` scoped to the
 * CURRENT directory with `kinds=files` (a filter over `browseFiles` could only
 * ever match the level already on screen), that a directory hit from an older
 * gateway is still not rendered, that navigating or re-targeting clears the
 * query, and that the truncation note appears only on a full page.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import FolderPanel from '../pages/chat/FolderPanel'
import { api } from '../api/client'
import { ApiError } from '../api/apiError'
import { ERROR_HANDOFF_KEY, __resetErrorJournalForTests, recordError } from '../utils/errorReport'

const ROOT = '/proj'

function listing() {
  return {
    path: ROOT,
    parent: '/',
    dirs: [{ name: 'src', path: `${ROOT}/src` }],
    files: [{ name: 'README.md', path: `${ROOT}/README.md` }],
  }
}

function hit(rel: string) {
  const path = `${ROOT}/${rel}`
  return { path, name: rel.split('/').pop() as string, size: 10, mtime: 1, kind: 'file' as const }
}

function renderPanel(props: Partial<Parameters<typeof FolderPanel>[0]> = {}) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <FolderPanel path={ROOT} onClose={() => {}} {...props} />
    </QueryClientProvider>,
  )
}

beforeEach(() => {
  vi.useFakeTimers({ shouldAdvanceTime: true })
  vi.spyOn(api, 'browseFiles').mockResolvedValue(listing() as never)
  vi.spyOn(api, 'revealPath').mockResolvedValue(undefined as never)
})

afterEach(() => {
  vi.useRealTimers()
  vi.restoreAllMocks()
})

async function type(text: string) {
  const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime })
  await user.type(screen.getByLabelText('Search files'), text)
  return user
}

describe('FolderPanel search', () => {
  it('searches the current directory recursively, files only', async () => {
    const search = vi.spyOn(api, 'fileSearch').mockResolvedValue({
      results: [hit('src/deep/nested/App.tsx')], root: ROOT,
    } as never)

    renderPanel()
    await screen.findByText('README.md')
    await type('app')

    await waitFor(() => expect(search).toHaveBeenCalled())
    // Scoped to cwd, and files-only, both server-side.
    expect(search).toHaveBeenCalledWith('app', ROOT, expect.anything(), 'files', 15)

    // The subfolder is shown, so a hit outside the current level is locatable.
    const row = await screen.findByTitle(`${ROOT}/src/deep/nested/App.tsx`)
    expect(within(row).getByText('App.tsx')).toBeInTheDocument()
    expect(within(row).getByText('src/deep/nested')).toBeInTheDocument()
  })

  it('does not dispatch a request for a one-character query', async () => {
    const search = vi.spyOn(api, 'fileSearch').mockResolvedValue({ results: [], root: ROOT } as never)

    renderPanel()
    await screen.findByText('README.md')
    await type('a')

    await vi.advanceTimersByTimeAsync(500)
    expect(search).not.toHaveBeenCalled()
    // The listing stays put rather than being replaced by an empty result set.
    expect(screen.getByText('README.md')).toBeInTheDocument()
  })

  it('hides a directory hit from a gateway that ignores kinds=files', async () => {
    vi.spyOn(api, 'fileSearch').mockResolvedValue({
      results: [hit('src/App.tsx'), { path: `${ROOT}/apps`, name: 'apps', size: 0, mtime: 1, kind: 'dir' }],
      root: ROOT,
    } as never)

    renderPanel()
    await screen.findByText('README.md')
    await type('app')

    await screen.findByText('App.tsx')
    expect(screen.queryByText('apps')).not.toBeInTheDocument()
  })

  it('opens a hit by its absolute path', async () => {
    vi.spyOn(api, 'fileSearch').mockResolvedValue({
      results: [hit('src/App.tsx')], root: ROOT,
    } as never)
    const onFileOpen = vi.fn()

    renderPanel({ onFileOpen })
    await screen.findByText('README.md')
    const user = await type('app')

    await user.click(await screen.findByTitle(`${ROOT}/src/App.tsx`))
    expect(onFileOpen).toHaveBeenCalledWith(`${ROOT}/src/App.tsx`)
  })

  it('clears the query when the tab is re-targeted at another directory', async () => {
    vi.spyOn(api, 'fileSearch').mockResolvedValue({
      results: [hit('src/App.tsx')], root: ROOT,
    } as never)

    const { rerender } = renderPanel()
    await screen.findByText('README.md')
    await type('app')
    await screen.findByText('App.tsx')

    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    rerender(
      <QueryClientProvider client={client}>
        <FolderPanel path="/other" onClose={() => {}} />
      </QueryClientProvider>,
    )

    // A query typed for the previous directory must not survive the re-target.
    await waitFor(() => expect(screen.getByLabelText('Search files')).toHaveValue(''))
    expect(screen.queryByText('App.tsx')).not.toBeInTheDocument()
  })

  it('notes truncation only when the page is full', async () => {
    const full = Array.from({ length: 15 }, (_, i) => hit(`src/f${i}.ts`))
    vi.spyOn(api, 'fileSearch').mockResolvedValue({ results: full, root: ROOT } as never)

    renderPanel()
    await screen.findByText('README.md')
    await type('f')  // one char: no request
    await type('s')  // now two

    // The notice is the expand control: a real <button> whose accessible name
    // is the notice text itself (#5639), not an inert <div>.
    expect(await screen.findByRole('button', { name: /Showing the first 15 matches/ })).toBeInTheDocument()
  })

  it('expands to the next tier when the notice is activated', async () => {
    // The server honours `limit`: 15 -> a full page, 30 -> 25 matches (no longer
    // truncated). Match 16+ must become reachable after activating the control.
    const all = Array.from({ length: 25 }, (_, i) => hit(`src/f${String(i).padStart(2, '0')}.ts`))
    const search = vi.spyOn(api, 'fileSearch').mockImplementation(
      (_q, _p, _s, _k, limit) => Promise.resolve({ results: all.slice(0, limit ?? 15), root: ROOT } as never),
    )

    renderPanel()
    await screen.findByText('README.md')
    await type('fs')

    const expand = await screen.findByRole('button', { name: /Showing the first 15 matches/ })
    expect(screen.queryByTitle(`${ROOT}/src/f15.ts`)).not.toBeInTheDocument()

    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime })
    await user.click(expand)

    // The next tier is requested from the SERVER (the cap is server-side
    // truncation, not a client render ceiling)...
    await waitFor(() => expect(search).toHaveBeenCalledWith('fs', ROOT, expect.anything(), 'files', 30))
    // ...and a match past the old cap is now reachable.
    expect(await screen.findByTitle(`${ROOT}/src/f15.ts`)).toBeInTheDocument()
    expect(screen.getByTitle(`${ROOT}/src/f24.ts`)).toBeInTheDocument()
    // 25 < 30: the page is no longer truncated, so no notice and no control.
    await waitFor(() => expect(screen.queryByText(/Showing the first/)).not.toBeInTheDocument())
  })

  it('keeps the control mounted and focusable while the wider page loads', async () => {
    // Regression (UX review on #5830): unmounting the notice mid-fetch made the
    // list read as complete ("no notice" is this panel's untruncated state) and
    // dropped keyboard focus to <body> on every activation. Inertness is
    // aria-disabled + an in-handler guard, NOT `disabled`, which blurs the
    // focused element in real browsers.
    const all = Array.from({ length: 25 }, (_, i) => hit(`src/f${String(i).padStart(2, '0')}.ts`))
    let releaseWider: (() => void) | undefined
    vi.spyOn(api, 'fileSearch').mockImplementation((_q, _p, _s, _k, limit) => {
      if ((limit ?? 15) > 15) {
        return new Promise(resolve => {
          releaseWider = () => resolve({ results: all.slice(0, limit), root: ROOT } as never)
        })
      }
      return Promise.resolve({ results: all.slice(0, 15), root: ROOT } as never)
    })

    renderPanel()
    await screen.findByText('README.md')
    await type('fs')

    const expand = await screen.findByRole('button', { name: /Showing the first 15 matches/ })
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime })
    await user.click(expand)

    // Mid-fetch: still mounted, inert, label still honest about the 15 rows on
    // screen, and focus still on the control.
    const pending = screen.getByRole('button', { name: /Showing the first 15 matches/ })
    expect(pending).toHaveAttribute('aria-disabled', 'true')
    expect(pending).toHaveFocus()
    expect(screen.getByTitle(`${ROOT}/src/f00.ts`)).toBeInTheDocument()
    // A second activation while in flight is guarded: no extra request.
    const callsBefore = (api.fileSearch as ReturnType<typeof vi.fn>).mock.calls.length
    await user.click(pending)
    expect((api.fileSearch as ReturnType<typeof vi.fn>).mock.calls.length).toBe(callsBefore)

    releaseWider!()
    // 25 < 30: once the wider page lands the set is untruncated, so the notice goes.
    await waitFor(() => expect(screen.queryByText(/Showing the first/)).not.toBeInTheDocument())
    expect(screen.getByTitle(`${ROOT}/src/f24.ts`)).toBeInTheDocument()
  })

  it('renders the notice as plain text once the server ceiling is reached', async () => {
    // Every tier comes back full: 15 -> 30 -> 60 (the server clamp). At 60 a
    // button could not fetch more, so the notice must degrade to text rather
    // than recreate the inert-affordance bug.
    const all = Array.from({ length: 60 }, (_, i) => hit(`src/f${String(i).padStart(2, '0')}.ts`))
    vi.spyOn(api, 'fileSearch').mockImplementation(
      (_q, _p, _s, _k, limit) => Promise.resolve({ results: all.slice(0, limit ?? 15), root: ROOT } as never),
    )

    renderPanel()
    await screen.findByText('README.md')
    await type('fs')

    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime })
    await user.click(await screen.findByRole('button', { name: /Showing the first 15 matches/ }))
    await user.click(await screen.findByRole('button', { name: /Showing the first 30 matches/ }))

    // Ceiling tier: honest count, but no button.
    expect(await screen.findByText(/Showing the first 60 matches/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Showing the first/ })).not.toBeInTheDocument()
  })

  it('resets to the default tier when the query changes', async () => {
    const all = Array.from({ length: 40 }, (_, i) => hit(`src/f${String(i).padStart(2, '0')}.ts`))
    const search = vi.spyOn(api, 'fileSearch').mockImplementation(
      (_q, _p, _s, _k, limit) => Promise.resolve({ results: all.slice(0, limit ?? 15), root: ROOT } as never),
    )

    renderPanel()
    await screen.findByText('README.md')
    await type('fs')

    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime })
    await user.click(await screen.findByRole('button', { name: /Showing the first 15 matches/ }))
    await waitFor(() => expect(search).toHaveBeenCalledWith('fs', ROOT, expect.anything(), 'files', 30))

    // Typing more is a NEW search: it must start back at the default tier, not
    // inherit the expansion of the set the user was previously looking at.
    await type('x')
    await waitFor(() => expect(search).toHaveBeenCalledWith('fsx', ROOT, expect.anything(), 'files', 15))
  })

  it('says so when nothing matches', async () => {
    vi.spyOn(api, 'fileSearch').mockResolvedValue({ results: [], root: ROOT } as never)

    renderPanel()
    await screen.findByText('README.md')
    await type('zzz')

    expect(await screen.findByText('No files match')).toBeInTheDocument()
  })

  it('surfaces a refused search instead of showing an empty list', async () => {
    vi.spyOn(api, 'fileSearch').mockRejectedValue(new Error('Access denied'))

    renderPanel()
    await screen.findByText('README.md')
    await type('app')

    expect(await screen.findByText('Search failed')).toBeInTheDocument()
    expect(screen.queryByText('Access denied')).not.toBeInTheDocument()
  })
})

/**
 * Every failure notice is keyed on the CAUSE, never on the rejection's own message.
 *
 * A deadline rejects with a message that is truthy, so the `message || t(...)` shape
 * these notices used to carry rendered that English string in every locale. Each case
 * asserts the raw text is ABSENT as well as the translated string present: presence
 * alone would still pass with both on screen.
 */
describe('FolderPanel failure copy is cause-keyed', () => {
  function deadline(): Error {
    const e = new Error('deadline exceeded')
    e.name = 'TimeoutError'
    return e
  }

  it('names a timed-out search without leaking the deadline text', async () => {
    vi.spyOn(api, 'fileSearch').mockRejectedValue(deadline())

    renderPanel()
    await screen.findByText('README.md')
    await type('app')

    expect(await screen.findByText('Search timed out')).toBeInTheDocument()
    expect(screen.queryByText('deadline exceeded')).not.toBeInTheDocument()
  })

  it('names a timed-out listing without leaking the deadline text', async () => {
    vi.spyOn(api, 'browseFiles').mockRejectedValue(deadline())

    renderPanel()

    expect(await screen.findByText('Folder listing timed out')).toBeInTheDocument()
    expect(screen.queryByText('deadline exceeded')).not.toBeInTheDocument()
  })

  it('distinguishes a refusal from a timeout on the machine code', async () => {
    vi.spyOn(api, 'fileSearch').mockRejectedValue(
      new ApiError(403, 'Access denied', JSON.stringify({ code: 'access_denied' })),
    )

    renderPanel()
    await screen.findByText('README.md')
    await type('app')

    expect(await screen.findByText('No access to this folder')).toBeInTheDocument()
    expect(screen.queryByText('Search timed out')).not.toBeInTheDocument()
  })

  it('reports an expired session as a generic failure, not as a refused folder', async () => {
    vi.spyOn(api, 'fileSearch').mockRejectedValue(
      new ApiError(403, 'Access denied', JSON.stringify({ code: 'access_denied' }), true),
    )

    renderPanel()
    await screen.findByText('README.md')
    await type('app')

    expect(await screen.findByText('Search failed')).toBeInTheDocument()
    expect(screen.queryByText('No access to this folder')).not.toBeInTheDocument()
  })
})

/**
 * The journal is keyed on the RAW server sentence, so translating the notice moved the
 * displayed text off that key and `Ask the agent` fell back to a bare message — the
 * endpoint, status and backend code never reached the agent. These pin the hand-off
 * payload rather than the copy, so a future `report=` removal fails here and not only
 * in review.
 */
describe('FolderPanel hands the agent the structured report, not the translated copy', () => {
  function journalled403() {
    recordError({
      source: 'api',
      message: 'Access denied',
      status: 403,
      code: 'access_denied',
      endpoint: '/api/file-search',
    })
    return new ApiError(403, 'Access denied', JSON.stringify({ code: 'access_denied' }))
  }

  function handoffPrompt(): string {
    return sessionStorage.getItem(ERROR_HANDOFF_KEY) ?? ''
  }

  beforeEach(() => {
    __resetErrorJournalForTests()
    sessionStorage.clear()
  })

  it('carries endpoint, status and code from a refused search', async () => {
    vi.spyOn(api, 'fileSearch').mockRejectedValue(journalled403())

    renderPanel()
    await screen.findByText('README.md')
    const user = await type('app')
    await screen.findByText('No access to this folder')
    await user.click(screen.getByRole('button', { name: /ask the agent/i }))

    const prompt = handoffPrompt()
    expect(prompt).toContain('/api/file-search')
    expect(prompt).toContain('HTTP 403')
    expect(prompt).toContain('access_denied')
  })

  it('carries them from a refused listing too', async () => {
    vi.spyOn(api, 'browseFiles').mockRejectedValue(journalled403())

    renderPanel()
    await screen.findByText('No access to this folder')
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime })
    await user.click(screen.getByRole('button', { name: /ask the agent/i }))

    const prompt = handoffPrompt()
    expect(prompt).toContain('HTTP 403')
    expect(prompt).toContain('access_denied')
  })
})
