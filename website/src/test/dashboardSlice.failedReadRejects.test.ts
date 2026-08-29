import { describe, it, expect, vi } from 'vitest'
import { configureStore } from '@reduxjs/toolkit'

/** THE BLOCKING FINDING -- a FAILED slots read must not read as a
 *  successful empty answer.
 *
 *  `fetchSlotsIfApplied` returned `null` for a refused read AND for a failed one, so a
 *  failed GET resolved, React Query recorded success, and the user saw nothing at all.
 *  `null` now means REFUSED and nothing else; a failure REJECTS, so the boot query has an
 *  error to render and every retrying caller can tell the two apart. */

const { mockSlots } = vi.hoisted(() => ({ mockSlots: vi.fn() }))
vi.mock('../api/client', () => ({
  api: { chatSlots: mockSlots, deleteChatSlot: vi.fn(), deleteSession: vi.fn() },
}))

const dashboardSlice = await import('../store/dashboardSlice')
const { fetchSlotsIfApplied } = dashboardSlice

const store = () => configureStore({
  reducer: { dashboard: dashboardSlice.default },
  middleware: g => g({ serializableCheck: false, immutableCheck: false }),
})

describe('a failed slots read is distinguishable from a refused one', () => {
  it('REJECTS when the read fails', async () => {
    mockSlots.mockRejectedValue(new Error('network down'))
    const s = store()

    await expect(
      fetchSlotsIfApplied(s.dispatch as never, () => s.getState() as never),
    ).rejects.toThrow()
  })

  it('carries the failure reason so a notice can name it', async () => {
    mockSlots.mockRejectedValue(new Error('network down'))
    const s = store()

    await expect(
      fetchSlotsIfApplied(s.dispatch as never, () => s.getState() as never),
    ).rejects.toThrow(/network down/)
  })

  it('still RESOLVES with the list when the read applies', async () => {
    mockSlots.mockResolvedValue([{ key: 'chat-1', messages: 0, running: false }])
    const s = store()

    const got = await fetchSlotsIfApplied(s.dispatch as never, () => s.getState() as never)

    expect(got).toEqual([{ key: 'chat-1', messages: 0, running: false }])
  })
})

/** The boot query must RENDER that error, through `ErrorNotice` with the hand-off, rather
 *  than dropping it -- the anchor the lane cites is `errors-use-error-notice`. */
describe('the boot slots query surfaces its error', () => {
  const src = Object.values(
    import.meta.glob('../App.tsx', { query: '?raw', eager: true }) as Record<string, { default: string }>,
  )[0].default

  it('binds the query error rather than only its data', () => {
    expect(src).toMatch(/error:\s*bootSlotsError[\s\S]{0,400}?queryKey:\s*\['boot-slots-gc'\]/)
  })

  it('renders that error through ErrorNotice with askAgent', () => {
    const window = src.slice(src.indexOf('{showBootSlotsError && ('))
    expect(window.slice(0, 700)).toMatch(/<ErrorNotice[\s\S]{0,500}?askAgent/)
    expect(window.slice(0, 700)).toMatch(/testId="boot-slots-failed"/)
  })
})
