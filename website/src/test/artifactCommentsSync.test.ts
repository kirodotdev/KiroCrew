import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'

/**
 * Tests for the cross-window artifact-comment sync channel. Uses a stub
 * BroadcastChannel and a fresh module instance per test (the module holds a
 * lazy singleton channel).
 */
class StubChannel {
  static instances: StubChannel[] = []
  onmessage: ((e: { data: { slug: string } }) => void) | null = null
  posted: Array<{ slug: string }> = []
  constructor(public name: string) { StubChannel.instances.push(this) }
  postMessage(msg: { slug: string }) { this.posted.push(msg) }
  close() { /* noop */ }
}

beforeEach(() => {
  StubChannel.instances = []
  vi.stubGlobal('BroadcastChannel', StubChannel as unknown as typeof BroadcastChannel)
  vi.resetModules()
})

afterEach(() => {
  vi.unstubAllGlobals()
})

async function loadModule() {
  return await import('../utils/artifactCommentsSync')
}

describe('artifactCommentsSync', () => {
  it('announce posts the slug on the shared channel', async () => {
    const { announceCommentsChanged, ARTIFACT_COMMENTS_SYNC_CHANNEL } = await loadModule()
    announceCommentsChanged('cr-queue')
    expect(StubChannel.instances[0].name).toBe(ARTIFACT_COMMENTS_SYNC_CHANNEL)
    expect(StubChannel.instances[0].posted).toEqual([{ slug: 'cr-queue' }])
  })

  it('delivers inbound announcements only to listeners of that slug', async () => {
    const { onCommentsChanged } = await loadModule()
    const hitA = vi.fn()
    const hitB = vi.fn()
    onCommentsChanged('artifact-a', hitA)
    onCommentsChanged('artifact-b', hitB)
    StubChannel.instances[0].onmessage?.({ data: { slug: 'artifact-a' } })
    expect(hitA).toHaveBeenCalledTimes(1)
    expect(hitB).not.toHaveBeenCalled()
  })

  it('cleanup unsubscribes the listener', async () => {
    const { onCommentsChanged } = await loadModule()
    const hit = vi.fn()
    const cleanup = onCommentsChanged('artifact-a', hit)
    cleanup()
    StubChannel.instances[0].onmessage?.({ data: { slug: 'artifact-a' } })
    expect(hit).not.toHaveBeenCalled()
  })

  it('ignores malformed messages without a slug', async () => {
    const { onCommentsChanged } = await loadModule()
    const hit = vi.fn()
    onCommentsChanged('artifact-a', hit)
    StubChannel.instances[0].onmessage?.({ data: {} as { slug: string } })
    expect(hit).not.toHaveBeenCalled()
  })
})
