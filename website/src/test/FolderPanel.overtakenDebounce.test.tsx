/**
 * FolderPanel's search debounce when a newer query overtakes a pending timer.
 *
 * A timer armed for an earlier query can fire after a newer query has committed
 * but before the debounce effect's cleanup clears it. Publishing that overtaken
 * query would search for text the box no longer holds and move the search key
 * off the newer query's in-flight search.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { act, fireEvent, render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Profiler } from 'react'
import FolderPanel from '../pages/chat/FolderPanel'
import { api } from '../api/client'

const ROOT = '/proj'
const DEBOUNCE_MS = 200

let pumpOnNextCommit = false

/**
 * Fires every due timer from the panel's next commit. `onRender` runs in the
 * commit's layout phase, before any passive effect, which is the window an
 * overtaken timer can fire in on a busy main thread. A discrete change event
 * commits on its own, so a sibling component's layout effect would land in a
 * later commit, after the cleanup had already cleared the timer.
 */
function pumpTimersAtCommit() {
  if (!pumpOnNextCommit) return
  pumpOnNextCommit = false
  vi.advanceTimersByTime(DEBOUNCE_MS)
}

/** Let resolved mock requests land; react-query delivers them on a zero-delay timer. */
async function settle() {
  for (let tick = 0; tick < 4; tick++) {
    await act(async () => {
      await Promise.resolve()
      vi.advanceTimersByTime(0)
    })
  }
}

beforeEach(() => {
  pumpOnNextCommit = false
  vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })
  vi.spyOn(api, 'browseFiles').mockResolvedValue({
    path: ROOT,
    parent: '/',
    dirs: [],
    files: [{ name: 'README.md', path: `${ROOT}/README.md` }],
  } as never)
})

afterEach(() => {
  vi.useRealTimers()
  vi.restoreAllMocks()
})

describe('FolderPanel search debounce overtaken by a newer query', () => {
  it('an overtaken timer firing after the newer query commits searches nothing', async () => {
    const search = vi.spyOn(api, 'fileSearch').mockResolvedValue({ results: [], root: ROOT } as never)
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={client}>
        <Profiler id="folder-panel" onRender={pumpTimersAtCommit}>
          <FolderPanel path={ROOT} onClose={() => {}} />
        </Profiler>
      </QueryClientProvider>,
    )
    await settle()
    const box = screen.getByLabelText('Search files')

    fireEvent.change(box, { target: { value: 'app' } })
    await act(async () => { vi.advanceTimersByTime(DEBOUNCE_MS) })
    await settle()
    expect(search).toHaveBeenCalledWith('app', ROOT, expect.anything(), 'files', 15)

    // 'apx' arms a timer; the box is back at 'app' before it is due, and the
    // timer fires inside that commit, before the effect cleanup that would clear it.
    fireEvent.change(box, { target: { value: 'apx' } })
    pumpOnNextCommit = true
    fireEvent.change(box, { target: { value: 'app' } })
    expect(pumpOnNextCommit).toBe(false)
    await act(async () => { vi.advanceTimersByTime(DEBOUNCE_MS) })
    await settle()

    expect(search.mock.calls.map(call => call[0])).not.toContain('apx')
  })
})
