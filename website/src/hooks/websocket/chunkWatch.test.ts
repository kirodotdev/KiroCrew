/**
 * The chunk-watch controller: which slots count as rendering a live stream,
 * and when `chunk_watch` is sent (on every open, then on each change only).
 */
import { describe, it, expect } from 'vitest'
import { QueryClient, QueryObserver } from '@tanstack/react-query'
import { catchupSlots, chunkWatchSlots, createChunkWatch, MAX_CHUNK_WATCH_SLOTS, type ChunkWatchSources } from './chunkWatch'

function sources(qc: QueryClient, over: Partial<ChunkWatchSources> = {}): ChunkWatchSources & { active: string | null; apps: string[]; isHidden: boolean } {
  const src = {
    queryClient: qc,
    active: null as string | null,
    apps: [] as string[],
    isHidden: false,
    activeSlot: () => src.active,
    appSlots: () => src.apps,
    hidden: () => src.isHidden,
    ...over,
  }
  return src
}

/** Mount a pane observer on `slot`'s messages query; returns its unmount. */
function mountPane(qc: QueryClient, slot: string): () => void {
  const observer = new QueryObserver(qc, {
    queryKey: ['slot-messages', slot, 50],
    queryFn: () => new Promise<never>(() => {}),
  })
  return observer.subscribe(() => {})
}

describe('chunk watch controller', () => {
  it('puts the active slot first, then app slots sorted, and only the active slot while hidden', () => {
    const qc = new QueryClient()
    const src = sources(qc)
    src.active = 'b'
    src.apps = ['z', 'b', 'a']
    expect(chunkWatchSlots(src)).toEqual(['b', 'a', 'z'])
    src.isHidden = true
    expect(chunkWatchSlots(src)).toEqual(['b'])
    src.active = null
    expect(chunkWatchSlots(src)).toEqual([])
  })

  it('caps the set at the gateway limit, keeping the active slot', () => {
    const qc = new QueryClient()
    const src = sources(qc)
    src.apps = Array.from({ length: MAX_CHUNK_WATCH_SLOTS + 5 }, (_, i) => `s${String(i).padStart(3, '0')}`)
    src.active = 'zz-active'
    const slots = chunkWatchSlots(src)
    expect(slots).toHaveLength(MAX_CHUNK_WATCH_SLOTS)
    expect(slots[0]).toBe('zz-active')
  })

  it('sends on every open and only when the set changes', () => {
    const qc = new QueryClient()
    const src = sources(qc)
    const sent: unknown[] = []
    const watch = createChunkWatch(src, (payload) => { sent.push(payload) })
    src.active = 'a'
    watch.onOpen()
    expect(sent).toEqual([{ type: 'chunk_watch', slots: ['a'] }])
    watch.sync()
    expect(sent).toHaveLength(1)
    src.active = 'b'
    watch.sync()
    expect(sent.at(-1)).toEqual({ type: 'chunk_watch', slots: ['b'] })
    // A new socket starts unwatched, so the same set is sent again.
    watch.onOpen()
    expect(sent).toHaveLength(3)
    watch.dispose()
  })

  it('follows pane observers through the query cache', () => {
    const qc = new QueryClient()
    const src = sources(qc)
    const sent: Array<{ slots: string[] }> = []
    const watch = createChunkWatch(src, (payload) => { sent.push(payload as { slots: string[] }) })
    watch.onOpen()
    const off = mountPane(qc, 'pane-1')
    expect(sent.at(-1)?.slots).toEqual(['pane-1'])
    off()
    expect(sent.at(-1)?.slots).toEqual([])
    watch.dispose()
  })

  it('reads only string slots from an ack', () => {
    expect(catchupSlots({ slots: ['a'], catchup: ['a', 3, '', 'b'] })).toEqual(['a', 'b'])
    expect(catchupSlots(null)).toEqual([])
    expect(catchupSlots({ catchup: 'a' })).toEqual([])
  })
})
