import { useLayoutEffect, useRef } from 'react'
import { act, fireEvent, screen } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import { api } from '../api/client'

vi.mock('../api/client', async importOriginal => {
  const mod = await importOriginal<typeof import('../api/client')>()
  return { ...mod, api: { ...mod.api, fileSearch: vi.fn() } }
})

import FilePickerMenu from '../components/FilePickerMenu'

const fileSearch = vi.mocked(api.fileSearch)

const DEBOUNCE_MS = 200

/**
 * Fires every due timer from a layout effect, which runs after the commit and
 * before any passive effect. That is the window a timer armed for an earlier
 * query can fire in on a busy main thread: the newer query is already
 * rendered and committed, but the debounce effect's cleanup that would clear
 * the older timer has not run yet.
 */
function TimerPumpAtCommit({ armed }: { armed: boolean }) {
  useLayoutEffect(() => {
    if (armed) vi.advanceTimersByTime(DEBOUNCE_MS)
  })
  return null
}

type HostProps = { query: string; open: boolean; project: string; pumpAtCommit: boolean; onSelect: () => void }

function Host({ query, open, project, pumpAtCommit, onSelect }: HostProps) {
  const anchorRef = useRef<HTMLDivElement>(null)
  return (
    <>
      <div ref={anchorRef} />
      <FilePickerMenu query={query} anchorRef={anchorRef} open={open} project={project} onSelect={onSelect} onClose={() => {}} />
      <TimerPumpAtCommit armed={pumpAtCommit} />
    </>
  )
}

/**
 * Let the resolved mock searches land without letting a debounce timer come
 * due: react-query delivers results on a zero-delay timer, so only that much
 * of the fake clock moves.
 */
async function settleSearches() {
  for (let tick = 0; tick < 3; tick++) {
    await act(async () => {
      await Promise.allSettled(fileSearch.mock.results.map(r => r.value))
      vi.advanceTimersByTime(0)
    })
  }
}

beforeEach(() => {
  vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })
  fileSearch.mockReset()
  // The same rows for every root, as the dashboard's search returns them: only
  // the response's `root` tells which project the rows were relativized for.
  fileSearch.mockImplementation((_query: string, project?: string) => Promise.resolve({
    root: project ?? '',
    results: [{ path: '/repo/src/main.ts', name: 'main.ts', size: 10, mtime: 1_700_000_000, kind: 'file' }],
  }))
})

afterEach(() => {
  vi.useRealTimers()
})

describe('FilePickerMenu debounce overtaken by a newer query', () => {
  it('a stale empty-query timer firing after the re-rooted query commits does not cancel its search', async () => {
    const onSelect = vi.fn()
    const props: HostProps = { query: 'mai', open: true, project: '/repo', pumpAtCommit: false, onSelect }
    const { rerender } = renderWithProviders(<Host {...props} />)
    await act(async () => { vi.advanceTimersByTime(DEBOUNCE_MS) })
    await settleSearches()
    expect(fileSearch).toHaveBeenCalledWith('mai', '/repo', expect.anything())

    // A pick closes the menu and clears its query, arming a debounce timer for ''.
    rerender(<Host {...props} query="" open={false} />)
    // The composer moves to a slot rooted at the subdirectory and `@mai` is typed
    // again before that timer is due; it fires right after this commit.
    rerender(<Host {...props} query="mai" open project="/repo/src" pumpAtCommit />)
    await settleSearches()

    expect(fileSearch).toHaveBeenCalledWith('mai', '/repo/src', expect.anything())
    expect(screen.getByRole('listbox')).not.toHaveAttribute('aria-busy', 'true')
    fireEvent.mouseDown(screen.getByText('main.ts'))
    expect(onSelect).toHaveBeenCalledWith({ path: '/repo/src/main.ts', relativePath: 'main.ts', kind: 'file' })
  })
})
