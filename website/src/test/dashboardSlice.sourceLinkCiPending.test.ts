import { describe, it, expect, vi } from 'vitest'
import reducer, { patchSlotSourceLinks, sseSlots } from '../store/dashboardSlice'
import type { ChatSlot } from '../types'

vi.mock('../api/client', () => ({
  api: { chatSlots: vi.fn(), chatMode: vi.fn() },
}))

/**
 * A `source_status` delta patches the sidebar chip's `ciPending` marker together
 * with `ci`: they describe one rollup, so a delta carrying `ci` without the
 * marker means the run has finished and the in-flight dot must go.
 */
const URL = 'https://example.test/pr/1'

const withLink = (link: Record<string, unknown>) =>
  reducer(reducer(undefined, { type: '@@INIT' }), sseSlots([
    { key: 'a', title: 'a', messages: 1, running: false, source_links: [{ url: URL, provider: 'github', number: 1, ...link }] },
  ] as unknown as ChatSlot[]))

const linkOf = (state: ReturnType<typeof withLink>) => state.slots[0].source_links?.[0]

describe('patchSlotSourceLinks — ciPending', () => {
  it('sets the marker that rides with a failed rollup', () => {
    const next = reducer(withLink({ ci: 'running' }), patchSlotSourceLinks({ url: URL, ci: 'failed', ciPending: '1' }))
    expect(linkOf(next)).toMatchObject({ ci: 'failed', ciPending: '1' })
  })

  it('clears the marker when a CI delta arrives without it', () => {
    const next = reducer(withLink({ ci: 'failed', ciPending: '1' }), patchSlotSourceLinks({ url: URL, ci: 'failed' }))
    expect(linkOf(next)?.ciPending).toBeUndefined()
  })

  it('leaves the marker alone on a lifecycle-only delta', () => {
    const next = reducer(withLink({ ci: 'failed', ciPending: '1' }), patchSlotSourceLinks({ url: URL, state: 'open' }))
    expect(linkOf(next)?.ciPending).toBe('1')
  })
})
