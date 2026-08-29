/**
 * A side/embedded draft must survive its cross-window claim EXPIRING.
 *
 * The claim carries a TTL so a crashed window stops blocking closes forever. That cannot
 * distinguish a dead window from a FROZEN one, so a background window holding an unsent
 * draft aged out of the guard and a close elsewhere destroyed text held only in React
 * state. The remedy is persistence: the guard's persisted fallback never expires.
 *
 * Driven through the expiry path deliberately — the clock is advanced past the TTL with no
 * refresh — because asserting on the refresh timer would pass on the unfixed code.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { renderHook } from '@testing-library/react'

import { useSlotDraftPersistence } from '../hooks/useSlotDraftPersistence'
import { slotHasUnsentWork } from '../utils/slotComposerRegistry'
import { publishSlotDirty, __resetSlotDirtyForTests, CLAIM_TTL_MS } from '../utils/slotDirtyBeacon'
import { __resetForTests as resetSideDrafts, loadSideDrafts } from '../utils/sideComposerDrafts'
import { DRAFT_SAVE_DEBOUNCE_MS } from '../utils/draftConstants'

const SLOT = 'chat-side-frozen'

describe('a side composer draft outlives its expiring claim', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-01-01T00:00:00Z'))
    __resetSlotDirtyForTests()
    resetSideDrafts()
    localStorage.clear()
  })

  afterEach(() => {
    vi.useRealTimers()
    __resetSlotDirtyForTests()
    resetSideDrafts()
    localStorage.clear()
  })

  it('is still discoverable after the claim ages past its TTL', () => {
    // The window publishes a claim and persists, exactly as the mounted host does.
    publishSlotDirty('composer-side-1', SLOT, true)
    renderHook(() => useSlotDraftPersistence(SLOT, 'half-written question in a side panel'))
    vi.advanceTimersByTime(DRAFT_SAVE_DEBOUNCE_MS + 10)

    // Control: while the claim is live the guard answers true either way, so this
    // assertion alone could not detect the defect.
    expect(slotHasUnsentWork(SLOT)).toBe(true)

    // Now the window freezes: no refresh runs, and the clock passes the TTL.
    vi.setSystemTime(new Date(Date.now() + CLAIM_TTL_MS + 5_000))

    // The claim is gone — proving the test really is on the expiry path.
    expect(loadSideDrafts()[SLOT]).toBeTruthy()
    expect(slotHasUnsentWork(SLOT)).toBe(true)
  })

  it('negative control: an EMPTY side composer does not block the close', () => {
    // Without this, the assertion above would pass for a guard that answers true always.
    renderHook(() => useSlotDraftPersistence(SLOT, '   '))
    vi.advanceTimersByTime(DRAFT_SAVE_DEBOUNCE_MS + 10)

    expect(loadSideDrafts()[SLOT]).toBeUndefined()
    expect(slotHasUnsentWork(SLOT)).toBe(false)
  })

  it('a MOUNTED composer that stops refreshing still holds its slot', () => {
    // The freeze case, which is what the persistence exists for: no unmount runs, so the
    // debounced write is the record that outlives the claim.
    const { unmount } = renderHook(() => useSlotDraftPersistence(SLOT, 'typed then frozen'))
    vi.advanceTimersByTime(DRAFT_SAVE_DEBOUNCE_MS + 10)

    expect(loadSideDrafts()[SLOT]).toHaveLength(1)

    // And dismissing the panel RELEASES it — a persisted copy with no surface showing it
    // would block this slot's close for the store's whole TTL.
    unmount()
    expect(loadSideDrafts()[SLOT]).toBeUndefined()
  })
})
