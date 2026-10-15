/**
 * What the gateway's "this dashboard moved" frames cost the gateway.
 *
 * A crewmate that is working sends these faster than one read takes. A read per
 * frame would pile up on the gateway, and the requests the page itself needs (the
 * document mint behind "Drawing the dashboard...") would wait behind the pile. This
 * pins the handler the socket routes those frames to, against a real query client
 * and real observers on the keys the Dashboard tab and a root session's page read
 * under: one read out at a time, and one more behind a burst.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { QueryClient, QueryObserver } from '@tanstack/react-query'
import { handleDashboardMoved } from '../hooks/useWebSocket'

const flush = (ms = 0) => vi.advanceTimersByTimeAsync(ms)

/** One open dashboard: every read it asks the gateway for lands only when the test says. */
function openDashboard(client: QueryClient, queryKey: string[]) {
  const landings: Array<() => void> = []
  const observer = new QueryObserver(client, {
    queryKey,
    queryFn: () =>
      new Promise<number>((resolve) => {
        landings.push(() => resolve(landings.length))
      }),
  })
  const stop = observer.subscribe(() => {})
  return { landings, stop }
}

let stops: Array<() => void> = []
beforeEach(() => {
  vi.useFakeTimers()
})
afterEach(() => {
  stops.forEach((s) => s())
  stops = []
  vi.useRealTimers()
})

describe('handleDashboardMoved', () => {
  it('starts one read for a burst of mate frames, and one more behind it', async () => {
    const client = new QueryClient({ defaultOptions: { queries: { staleTime: Infinity, retry: false } } })
    const tab = openDashboard(client, ['member-dashboard', 'oncall', 'oncall', 'en', 'live'])
    stops.push(tab.stop)
    tab.landings[0]() // the tab's own first read
    await flush()

    for (let i = 0; i < 5; i++) handleDashboardMoved(client, { slug: 'oncall', key: 'workstreams', seq: i })
    await flush(40)
    expect(tab.landings).toHaveLength(2) // five frames, one read out

    tab.landings[1]()
    await flush() // landed; frames arrived behind it, so one more follows
    expect(tab.landings).toHaveLength(3)

    tab.landings[2]()
    await flush(10_000)
    expect(tab.landings).toHaveLength(3) // and that one was the last
  })

  it('does the same for a root session\'s page, named by its slot', async () => {
    const client = new QueryClient({ defaultOptions: { queries: { staleTime: Infinity, retry: false } } })
    const page = openDashboard(client, ['session-dashboard', 'chat-7', 'en', 'live'])
    stops.push(page.stop)
    page.landings[0]()
    await flush()

    for (let i = 0; i < 5; i++) handleDashboardMoved(client, { slot: 'chat-7' })
    await flush()

    expect(page.landings).toHaveLength(2)
  })

  it('leaves another mate\'s open dashboard alone', async () => {
    const client = new QueryClient({ defaultOptions: { queries: { staleTime: Infinity, retry: false } } })
    const mine = openDashboard(client, ['member-dashboard', 'oncall', 'oncall', 'en', 'live'])
    const theirs = openDashboard(client, ['member-dashboard', 'release-captain', 'release-captain', 'en', 'live'])
    stops.push(mine.stop, theirs.stop)
    mine.landings[0]()
    theirs.landings[0]()
    await flush()

    handleDashboardMoved(client, { slug: 'release-captain' })
    await flush()

    expect(mine.landings).toHaveLength(1)
    expect(theirs.landings).toHaveLength(2)
  })
})
