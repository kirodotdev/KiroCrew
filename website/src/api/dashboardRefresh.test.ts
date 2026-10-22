import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { QueryClient, QueryObserver } from '@tanstack/react-query'
import { refreshDashboardReads } from './dashboardRefresh'

/** One open dashboard on *client*: every read it asks the gateway for lands only when the test says. */
function watch(client: QueryClient, slug: string, landings: Array<() => void>) {
  const observer = new QueryObserver(client, {
    queryKey: ['member-dashboard', slug, slug, 'en', 'live'],
    queryFn: () =>
      new Promise<number>((resolve) => {
        landings.push(() => resolve(landings.length))
      }),
  })
  return observer.subscribe(() => {})
}

/**
 * A real client with one OPEN dashboard per slug, so the cancel-or-join choice under
 * test is react-query's own and not a mock's. Every read the gateway would be asked
 * for is recorded in `reads`.
 */
function open(...slugs: string[]) {
  const client = new QueryClient({ defaultOptions: { queries: { staleTime: Infinity, retry: false } } })
  const reads: Record<string, Array<() => void>> = {}
  const stops: Array<() => void> = []
  for (const slug of slugs) {
    reads[slug] = []
    stops.push(watch(client, slug, reads[slug]))
  }
  return { client, reads, stop: () => stops.forEach((s) => s()) }
}

const flush = (ms = 0) => vi.advanceTimersByTimeAsync(ms)
const frame = (client: QueryClient, slug: string) => refreshDashboardReads(client, ['member-dashboard', slug])

let current: ReturnType<typeof open> | null = null
const mount = (...slugs: string[]) => {
  current = open(...slugs)
  return current
}

beforeEach(() => {
  vi.useFakeTimers()
})
afterEach(() => {
  current?.stop()
  current = null
  vi.useRealTimers()
})

describe('refreshDashboardReads: one read at a time per dashboard', () => {
  it('starts a read at once when none is out', async () => {
    const { client, reads } = mount('atlas')
    reads.atlas[0]() // the tab's own first read lands
    await flush()

    frame(client, 'atlas')
    await flush()

    expect(reads.atlas).toHaveLength(2)
  })

  it('starts no second read while one is out, and reads once more as soon as it lands', async () => {
    const { client, reads } = mount('atlas')
    reads.atlas[0]()
    await flush()

    frame(client, 'atlas')
    await flush(1000) // the read takes a second
    for (let i = 0; i < 20; i++) frame(client, 'atlas')
    await flush()
    // Twenty frames, still one request out.
    expect(reads.atlas).toHaveLength(2)

    reads.atlas[1]() // lands; frames arrived meanwhile, so the page it brought is already behind
    await flush()
    expect(reads.atlas).toHaveLength(3)
  })

  it('ends on the newest state: a frame during the last read still gets its read', async () => {
    const { client, reads } = mount('atlas')
    reads.atlas[0]()
    await flush()

    frame(client, 'atlas')
    await flush(50)
    frame(client, 'atlas') // the LAST frame of the burst lands mid-read
    reads.atlas[1]()
    await flush()

    expect(reads.atlas).toHaveLength(3)
  })

  it('reads again behind each read that frames landed behind, and goes idle once none did', async () => {
    const { client, reads } = mount('atlas')
    reads.atlas[0]()
    await flush()

    frame(client, 'atlas')
    for (let i = 0; i < 3; i++) frame(client, 'atlas')
    reads.atlas[1]() // 3 frames behind it
    await flush()
    expect(reads.atlas).toHaveLength(3)

    for (let i = 0; i < 3; i++) frame(client, 'atlas') // 3 more behind the second read
    reads.atlas[2]()
    await flush()
    expect(reads.atlas).toHaveLength(4)

    reads.atlas[3]() // nothing arrived behind this one
    await flush(10_000)
    // Nothing is left owing: the dashboard is idle again, not looping.
    expect(reads.atlas).toHaveLength(4)
  })

  it('is idle again after a read nothing arrived behind, and the next frame starts a read at once', async () => {
    const { client, reads } = mount('atlas')
    reads.atlas[0]()
    await flush()

    frame(client, 'atlas')
    await flush(500)
    reads.atlas[1]()
    await flush()
    frame(client, 'atlas')
    await flush()

    expect(reads.atlas).toHaveLength(3)
  })

  it('keeps one mate\'s reads apart from another\'s', async () => {
    const { client, reads } = mount('atlas', 'nyx')
    reads.atlas[0]()
    reads.nyx[0]()
    await flush()

    frame(client, 'atlas')
    await flush()
    // atlas has a read out; a frame for nyx still starts nyx's at once.
    frame(client, 'nyx')
    await flush()

    expect(reads.atlas).toHaveLength(2)
    expect(reads.nyx).toHaveLength(2)
  })

  it('never doubles a fetch it did not start: it waits for it and reads once more', async () => {
    const { client, reads } = mount('atlas')
    reads.atlas[0]()
    await flush()

    // The fallback poll (or a mount read) goes out first; it began before the write
    // the frame announces, so the page must not end on its answer.
    void client.refetchQueries({ queryKey: ['member-dashboard', 'atlas'] })
    await flush()
    expect(reads.atlas).toHaveLength(2)

    frame(client, 'atlas')
    frame(client, 'atlas')
    await flush()
    expect(reads.atlas).toHaveLength(2) // still the one request: nothing was cancelled or doubled

    reads.atlas[1]() // the poll lands...
    await flush()
    expect(reads.atlas).toHaveLength(3) // ...and the frames' read follows it, once

    reads.atlas[2]()
    await flush(10_000)
    expect(reads.atlas).toHaveLength(3)
  })

  it('marks the cached read stale with a plain invalidation when no dashboard is open', async () => {
    const client = new QueryClient()
    const invalidate = vi.spyOn(client, 'invalidateQueries')

    frame(client, 'atlas')
    frame(client, 'atlas')
    await flush()

    // Every frame is its own plain invalidation: with nothing open there is no read to
    // hold back, so no state is kept and nothing is deferred.
    expect(invalidate.mock.calls).toEqual([
      [{ queryKey: ['member-dashboard', 'atlas'] }],
      [{ queryKey: ['member-dashboard', 'atlas'] }],
    ])
  })

  it('forgets a dashboard that closed mid-read, so the next one opens on a clean slate', async () => {
    const { client, reads, stop } = mount('atlas')
    reads.atlas[0]()
    await flush()
    frame(client, 'atlas') // a read goes out...
    await flush(50)
    frame(client, 'atlas') // ...a frame lands behind it...
    stop() // ...and the tab closes before the read lands
    reads.atlas[1]()
    await flush(10_000)
    expect(reads.atlas).toHaveLength(2) // nothing was owed to a tab nobody has open

    const reopened = watch(client, 'atlas', reads.atlas)
    await flush()
    // The tab missed a frame while it was closed, so it reads once on opening.
    expect(reads.atlas).toHaveLength(3)
    reads.atlas[2]()
    await flush()

    frame(client, 'atlas')
    await flush()
    // Nothing is waiting from the old tab: the first frame of the new one reads at once.
    expect(reads.atlas).toHaveLength(4)
    reopened()
  })
})
