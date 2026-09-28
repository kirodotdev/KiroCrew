/**
 * The steer receipt policy, tested once (issue #9457).
 *
 * Before this, the five rulings (`refused`/`transport-error`, `response-late`,
 * `unknown`, `steered`, `queued`, plus the `dispatched && !steered` row) lived
 * in three hand-synced copies -- `ChatPage.steerMutation`, `ChatPane.doSteer`,
 * and `ChatPane.doSend`'s steer branch -- each pinned only by its own host
 * tests, so a copy could drift with CI green (the failure mode #2240 found for
 * the queue-card recipe). `applySteerReceipt` owns the rulings; these tests pin
 * the ruling table itself, independent of any host. Each host keeps its own
 * tests for its adapter (composer restore, error rows, bubble reducer).
 *
 * The adapter is a full spy set: every ruling asserts WHICH adapter calls fire
 * and which do not, so a future edit that (say) drops the `restore` on a
 * `response-late`, or fires `stashDemoted` on a `dispatched`, reddens here.
 */
import { describe, it, expect, vi } from 'vitest'
import { applySteerReceipt, type SteerReceiptAdapter } from '../chat-core/transport/steerReceipt'
import type { SendReceipt, SendReceiptStatus } from '../chat-core/transport/sendTurn'

/** A spy adapter. `echoReconciled` defaults to false (no echo landed yet). */
function makeAdapter(overrides: Partial<SteerReceiptAdapter> = {}) {
  const adapter: SteerReceiptAdapter & {
    echoReconciled: ReturnType<typeof vi.fn>
    restore: ReturnType<typeof vi.fn>
    reportFailure: ReturnType<typeof vi.fn>
    warnUnconfirmed: ReturnType<typeof vi.fn>
    resolveBubble: ReturnType<typeof vi.fn>
    stashDemoted: ReturnType<typeof vi.fn>
  } = {
    echoReconciled: vi.fn(() => false),
    restore: vi.fn(),
    reportFailure: vi.fn(),
    warnUnconfirmed: vi.fn(),
    resolveBubble: vi.fn(),
    stashDemoted: vi.fn(),
    ...overrides,
  }
  return adapter
}

const receipt = (
  status: SendReceiptStatus,
  body: SendReceipt['body'] = {},
  reason?: string,
): SendReceipt => ({ status, body, ...(reason !== undefined ? { reason } : {}) })

describe('applySteerReceipt -- refused', () => {
  it('drops the bubble, reports the reason, restores the composer exactly once', () => {
    const a = makeAdapter()
    applySteerReceipt(receipt('refused', {}, 'slot agent mismatch'), a)
    expect(a.resolveBubble).toHaveBeenCalledWith('drop')
    expect(a.reportFailure).toHaveBeenCalledWith('slot agent mismatch', 'refused')
    // restore is the ONLY restore path (reportFailure reports only), so a
    // refused restores exactly once and is told which ruling triggered it.
    // This pins the double-restore fix GPT 5.6 flagged at ChatPane.tsx:837.
    expect(a.restore).toHaveBeenCalledTimes(1)
    expect(a.restore).toHaveBeenCalledWith('refused')
    // A refusal is not indeterminate: no unconfirmed warning, no stash.
    expect(a.warnUnconfirmed).not.toHaveBeenCalled()
    expect(a.stashDemoted).not.toHaveBeenCalled()
  })

  it('does not consult the echo -- a refusal is definitive', () => {
    const a = makeAdapter({ echoReconciled: vi.fn(() => true) })
    applySteerReceipt(receipt('refused'), a)
    expect(a.echoReconciled).not.toHaveBeenCalled()
    expect(a.reportFailure).toHaveBeenCalledWith(undefined, 'refused')
  })
})

describe('applySteerReceipt -- transport-error', () => {
  it('is treated as refused when no echo reconciled', () => {
    const a = makeAdapter()
    applySteerReceipt(receipt('transport-error'), a)
    expect(a.resolveBubble).toHaveBeenCalledWith('drop')
    expect(a.reportFailure).toHaveBeenCalledWith(undefined, 'transport-error')
    expect(a.restore).toHaveBeenCalledTimes(1)
    expect(a.restore).toHaveBeenCalledWith('transport-error')
  })

  it('does NOTHING when a matching echo already reconciled the bubble', () => {
    const a = makeAdapter({ echoReconciled: vi.fn(() => true) })
    applySteerReceipt(receipt('transport-error'), a)
    expect(a.echoReconciled).toHaveBeenCalledTimes(1)
    expect(a.resolveBubble).not.toHaveBeenCalled()
    expect(a.reportFailure).not.toHaveBeenCalled()
    expect(a.restore).not.toHaveBeenCalled()
  })
})

describe('applySteerReceipt -- response-late', () => {
  it('drops the bubble, restores once (told the status), and warns when unconfirmed', () => {
    const a = makeAdapter()
    applySteerReceipt(receipt('response-late'), a)
    expect(a.resolveBubble).toHaveBeenCalledWith('drop')
    expect(a.restore).toHaveBeenCalledTimes(1)
    expect(a.restore).toHaveBeenCalledWith('response-late')
    expect(a.warnUnconfirmed).toHaveBeenCalledTimes(1)
    // Indeterminate, not failed: no error row.
    expect(a.reportFailure).not.toHaveBeenCalled()
  })

  it('does NOTHING when a matching echo already reconciled the bubble', () => {
    const a = makeAdapter({ echoReconciled: vi.fn(() => true) })
    applySteerReceipt(receipt('response-late'), a)
    expect(a.resolveBubble).not.toHaveBeenCalled()
    expect(a.restore).not.toHaveBeenCalled()
    expect(a.warnUnconfirmed).not.toHaveBeenCalled()
  })
})

describe('applySteerReceipt -- unknown', () => {
  it('leaves the bubble alone: an unreadable body confirms nothing', () => {
    const a = makeAdapter()
    applySteerReceipt(receipt('unknown'), a)
    expect(a.resolveBubble).not.toHaveBeenCalled()
    expect(a.restore).not.toHaveBeenCalled()
    expect(a.reportFailure).not.toHaveBeenCalled()
    expect(a.warnUnconfirmed).not.toHaveBeenCalled()
    expect(a.stashDemoted).not.toHaveBeenCalled()
    // `unknown` is not an indeterminate-transport status, so the echo is not
    // consulted (matching the old code, which did not either).
    expect(a.echoReconciled).not.toHaveBeenCalled()
  })
})

describe('applySteerReceipt -- steered', () => {
  it('leaves the bubble alone on a dispatched+steered receipt: the echo owns the row', () => {
    const a = makeAdapter()
    applySteerReceipt(receipt('dispatched', { steered: true }), a)
    expect(a.resolveBubble).not.toHaveBeenCalled()
    expect(a.stashDemoted).not.toHaveBeenCalled()
  })

  it('short-circuits steered ahead of the turn demotion: no resolveBubble at all', () => {
    const a = makeAdapter()
    applySteerReceipt(receipt('dispatched', { steered: true }), a)
    // steered means the steer_push echo owns the row, so the 'turn' demotion
    // (which doSend maps to confirmOptimisticSend) must not fire.
    expect(a.resolveBubble).not.toHaveBeenCalled()
  })
})

describe('applySteerReceipt -- queued (demotion)', () => {
  it('stashes the pre-send state by queue_id, then drops the bubble', () => {
    const a = makeAdapter()
    applySteerReceipt(receipt('queued', { queue_id: 'q-42' }), a)
    expect(a.stashDemoted).toHaveBeenCalledWith('q-42')
    expect(a.resolveBubble).toHaveBeenCalledWith('drop')
  })

  it('does not stash when the body carries no queue_id', () => {
    const a = makeAdapter()
    applySteerReceipt(receipt('queued', {}), a)
    expect(a.stashDemoted).not.toHaveBeenCalled()
    // The demotion still resolves the bubble.
    expect(a.resolveBubble).toHaveBeenCalledWith('drop')
  })

  it('does not stash on an empty-string queue_id', () => {
    const a = makeAdapter()
    applySteerReceipt(receipt('queued', { queue_id: '' }), a)
    expect(a.stashDemoted).not.toHaveBeenCalled()
  })

  it('a doSend-style adapter (no-op drop arm) still stashes on queued', () => {
    // The doSend copy makes the 'drop' arm a no-op (it mints no steer bubble);
    // the helper still calls resolveBubble('drop') after the stash, which that
    // adapter ignores. The stash must still happen.
    const a = makeAdapter({ resolveBubble: vi.fn() })
    applySteerReceipt(receipt('queued', { queue_id: 'q-7' }), a)
    expect(a.stashDemoted).toHaveBeenCalledWith('q-7')
    expect(a.resolveBubble).toHaveBeenCalledWith('drop')
  })
})

describe('applySteerReceipt -- dispatched && !steered', () => {
  it('demotes the bubble to a plain user row via the turn arm', () => {
    const a = makeAdapter()
    applySteerReceipt(receipt('dispatched', {}), a)
    expect(a.resolveBubble).toHaveBeenCalledWith('turn')
    expect(a.stashDemoted).not.toHaveBeenCalled()
  })

  it('fires the turn arm exactly once (this is where doSend confirms its row)', () => {
    const a = makeAdapter()
    applySteerReceipt(receipt('dispatched', { mid: 'm-1' }), a)
    expect(a.resolveBubble).toHaveBeenCalledTimes(1)
    expect(a.resolveBubble).toHaveBeenCalledWith('turn')
  })
})
