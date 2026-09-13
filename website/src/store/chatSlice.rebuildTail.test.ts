/**
 * The rebuild TAIL — hydrate queued bubbles, dedup by mid, re-add retained sends —
 * is order-dependent, and the two sites that rebuild `state.messages` had drifted:
 * `switchSlot` ran hydrate→dedup→preserve while `refreshSlot` ran preserve→hydrate.
 * The order now lives in `finalizeRebuild`.
 *
 * These tests demonstrate the consolidation rather than asserting it: the first
 * drives ONE scenario through BOTH reducers and requires the same result, so a site
 * that goes back to hand-sequencing diverges here. The second is a ratchet on the
 * order having exactly one owner, in the repo's established per-file-count style.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { configureStore } from '@reduxjs/toolkit'
import { readFileSync } from 'node:fs'
import { resolve, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'
import chatReducer, {
  retainedSend, switchSlot, refreshSlot, setActiveSlot, appendMessage, appendQueuedMessage,
} from './chatSlice'
import { api } from '../api/client'

vi.mock('../api/client', () => ({ api: { chatSlotDetail: vi.fn() } }))

const SLOT = 'slot-tail'
const SEND_ID = 'send-tail-1'

const page = () => ({ messages: [], running: false, has_more: false, total: 0, queue: [{ content: '[REDACTED]', id: 'q-tail' }] })
const makeStore = () => configureStore({ reducer: { chat: chatReducer } })

/** One slot carrying a retained send AND a queued card standing for it. */
async function seeded() {
  const store = makeStore()
  store.dispatch(setActiveSlot(SLOT))
  store.dispatch(appendMessage({
    role: 'user', content: 'the tail row', cls: '', ts: '2026-08-29T17:54:03.900Z',
    meta: retainedSend({ sendId: SEND_ID, files: ['/tmp/tail.pdf'] }),
  }))
  store.dispatch(appendQueuedMessage({ slot: SLOT, content: '[REDACTED]', ts: '2026-08-29T17:55:02.000Z', queue_id: 'q-tail', sendId: SEND_ID }))
  return store
}

const shape = (store: ReturnType<typeof makeStore>) =>
  store.getState().chat.messages.map(m => ({
    role: m.role,
    raw: (m.meta?.rawSend as { text?: string } | undefined)?.text,
  }))

beforeEach(() => {
  ;(api.chatSlotDetail as ReturnType<typeof vi.fn>).mockResolvedValue(page())
})

describe('the rebuild tail is the same at every site that uses it', () => {
  it('gives switchSlot and refreshSlot the same result for one scenario', async () => {
    const viaSwitch = await seeded()
    await viaSwitch.dispatch(switchSlot(SLOT))

    const viaRefresh = await seeded()
    await viaRefresh.dispatch(refreshSlot(SLOT))

    // Not a snapshot of one site's output: the POINT is that the two agree, which is
    // what hand-sequencing the tail per site could not guarantee.
    expect(shape(viaRefresh)).toEqual(shape(viaSwitch))
    // And the scenario is discriminating -- the queued card carries the recovery text,
    // which only survives when hydrate runs with the pre-rebuild list as its prior.
    expect(shape(viaSwitch).some(r => r.role === 'queued' && r.raw === 'the tail row')).toBe(true)
  })
})

describe('rebuild-tail order ratchet', () => {
  const SRC = resolve(dirname(fileURLToPath(import.meta.url)), 'chatSlice.ts')
  const src = readFileSync(SRC, 'utf-8')

  it('keeps ONE owner for the tail order', () => {
    expect((src.match(/function finalizeRebuild\(/g) ?? []).length).toBe(1)
  })

  it('leaves the two transcript-rebuild sites going through the owner', () => {
    // Definition, the call inside `finalizeRebuild`, and TWO deliberate direct callers: `warmSlotCache`
    // (background merge, no mid-dedup) and `resumeFromHistory` (its page cannot re-add a pane send).
    expect((src.match(/preserveOptimisticSends\(/g) ?? []).length).toBe(4)
  })
})

describe('only a row\u2019s OWN confirmation retires its delivery doubt', () => {
  it('has no cross-row demotion loop, so a later send cannot settle an earlier one', () => {
    const src = readFileSync('src/store/chatSlice.ts', 'utf8')
    // A later turn's success proves nothing about an earlier send. Clearing the earlier row's warning
    // made a prompt that never left render as an ordinary delivered message, with no way back.
    expect((src.match(/demotePriorDoubt/g) || []).length,
      'a prior-row demotion loop makes an unsent prompt look delivered').toBe(0)
    expect((src.match(/delete \(prior\.meta as Record<string, unknown>\)\.deliveryUnknown/g) || []).length,
      'and no open-coded copy of it may return either').toBe(0)
    // Both confirm paths still clear the CONFIRMED row's own doubt, which is the identity-scoped rule.
    expect((src.match(/delete meta\.deliveryUnknown/g) || []).length,
      'the receipt path retires the doubt on the row it confirms').toBeGreaterThanOrEqual(1)
  })
})
