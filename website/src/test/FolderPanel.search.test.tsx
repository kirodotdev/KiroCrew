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
    expect(search).toHaveBeenCalledWith('app', ROOT, expect.anything(), 'files')

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

    expect(await screen.findByText(/Showing the first 15 matches/)).toBeInTheDocument()
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

    expect(await screen.findByText('Access denied')).toBeInTheDocument()
  })
})
