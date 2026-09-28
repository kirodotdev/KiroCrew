import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { QueryClient } from '@tanstack/react-query'
import { useArtifactLiveReload } from './useArtifactLiveReload'
import { setArtifactEditing, __resetArtifactEditing } from '../utils/artifactEditGuard'

/**
 * A file-backed artifact's content is read off `source_path` on every GET, but
 * the client fetches once per mount and the shared QueryClient never expires a
 * query on its own. An agent rewriting the backing file therefore left an open
 * artifact surface rendering pre-edit content. `useArtifactLiveReload` turns the
 * existing /api/file-watch SSE into the missing change signal — while refusing
 * to move the cache under an open edit buffer.
 */

/** Past the hook's 400ms debounce with margin. */
const PAST_DEBOUNCE_MS = 900

// Minimal controllable EventSource, same seam as useFileWatch.test.ts.
class MockEventSource {
  static instances: MockEventSource[] = []
  url: string
  closed = false
  onopen: (() => void) | null = null
  onmessage: ((ev: { data: string }) => void) | null = null
  onerror: (() => void) | null = null
  constructor(url: string) {
    this.url = url
    MockEventSource.instances.push(this)
  }
  close() {
    this.closed = true
  }
}

/** Fire one watch frame carrying the (deliberately ignored) new file content. */
function emitChange(content: string): void {
  act(() => {
    MockEventSource.instances[0].onmessage?.({ data: JSON.stringify({ content, mtime: 2 }) })
  })
}

/**
 * Let the debounce window elapse. Real timers on purpose: a fake clock has to be
 * uninstalled at a hook boundary, and the shared setup wraps the global timer
 * functions for its leak sweep. Every "not yet" assertion below is either taken
 * synchronously (zero elapsed) or after waiting LONGER than the window, so
 * nothing here depends on landing inside it.
 */
async function pastDebounce(): Promise<void> {
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, PAST_DEBOUNCE_MS))
  })
}

let queryClient: QueryClient
let invalidate: ReturnType<typeof vi.spyOn>

beforeEach(() => {
  vi.stubGlobal('EventSource', MockEventSource as unknown as typeof EventSource)
  queryClient = new QueryClient()
  invalidate = vi.spyOn(queryClient, 'invalidateQueries').mockResolvedValue(undefined)
})

afterEach(() => {
  MockEventSource.instances = []
  __resetArtifactEditing()
  vi.unstubAllGlobals()
})

// useQueryClient() reads from context; the hook is the only consumer here, so a
// direct module mock is lighter than wrapping every render in a provider.
vi.mock('@tanstack/react-query', async () => {
  const actual = await vi.importActual<typeof import('@tanstack/react-query')>(
    '@tanstack/react-query',
  )
  return { ...actual, useQueryClient: () => queryClient }
})

describe('useArtifactLiveReload', () => {
  it('watches the artifact source_path and refetches the artifact on a change', async () => {
    renderHook(() => useArtifactLiveReload('flow-report', '/repo/out/flow.html'))

    expect(MockEventSource.instances).toHaveLength(1)
    expect(MockEventSource.instances[0].url).toBe(
      '/api/file-watch?path=' + encodeURIComponent('/repo/out/flow.html'),
    )

    emitChange('<h1>rebaked</h1>')
    // Debounced: nothing has fired on the frame itself.
    expect(invalidate).not.toHaveBeenCalled()

    await pastDebounce()
    // invalidateQueries, not setQueryData: it refetches active observers
    // regardless of staleTime, which is what makes the surface repaint.
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ['artifact', 'flow-report'] })
  })

  it('coalesces a burst of writes into one refetch', async () => {
    renderHook(() => useArtifactLiveReload('flow-report', '/repo/out/flow.html'))

    emitChange('a')
    emitChange('ab')
    emitChange('abc')
    expect(invalidate).not.toHaveBeenCalled()

    await pastDebounce()
    expect(invalidate).toHaveBeenCalledTimes(1)
  })

  it('withholds the refetch while an edit buffer is open on that artifact', async () => {
    renderHook(() => useArtifactLiveReload('flow-report', '/repo/out/flow.html'))
    // Editor opened DURING the debounce window: the guard must be read when the
    // refetch would land, not when the frame arrived.
    emitChange('<h1>rebaked</h1>')
    setArtifactEditing('flow-report', true)

    await pastDebounce()
    // Nothing at all is invalidated — an external write emits no artifact event
    // and no comment, so there is nothing safe left to pull.
    expect(invalidate).not.toHaveBeenCalled()

    // Once the buffer closes, the next change reloads normally again.
    setArtifactEditing('flow-report', false)
    emitChange('<h1>rebaked twice</h1>')
    await pastDebounce()
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ['artifact', 'flow-report'] })
  })

  it('opens no stream for an artifact with no source_path', async () => {
    renderHook(() => useArtifactLiveReload('inline-widget', undefined))
    expect(MockEventSource.instances).toHaveLength(0)
    await pastDebounce()
    expect(invalidate).not.toHaveBeenCalled()
  })

  it('drops a pending refetch when the surface unmounts', async () => {
    const { unmount } = renderHook(() =>
      useArtifactLiveReload('flow-report', '/repo/out/flow.html'),
    )
    emitChange('<h1>rebaked</h1>')
    unmount()

    await pastDebounce()
    expect(invalidate).not.toHaveBeenCalled()
  })
})
