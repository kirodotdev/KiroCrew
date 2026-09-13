/**
 * One composer's write must not destroy another's.
 *
 * Both stores live in `localStorage`, shared by every same-origin window, and each of these
 * asserts on a SECOND composer's record after the FIRST one writes — the only way to see the
 * loss, because a single-composer test passes on the unfixed code: the record that gets
 * discarded is the one it never created. The beacon now holds each claim under its own key,
 * so a racing publish has no shared value to merge and overwrite; the side-draft store keys
 * each draft by its composer, so an unmount clears only the entry it owns.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { renderHook } from '@testing-library/react'

import { useSlotDraftPersistence } from '../hooks/useSlotDraftPersistence'
import { slotHasUnsentWork } from '../utils/slotComposerRegistry'
import {
  publishSlotDirty,
  anyWindowSlotDirty,
  __resetSlotDirtyForTests,
  SLOT_DIRTY_KEY_PREFIX,
  CLAIM_TTL_MS,
} from '../utils/slotDirtyBeacon'
import {
  __resetForTests as resetSideDrafts,
  loadSideDrafts,
  writeSideDraft,
} from '../utils/sideComposerDrafts'
import { DRAFT_SAVE_DEBOUNCE_MS } from '../utils/draftConstants'

const SLOT = 'chat-shared-slot'
const OTHER_SLOT = 'chat-other-window-slot'
const OTHER = 'composer-other-window-1'
const MINE = 'composer-mine-1'

/**
 * Run *write* with storage READS answering as they did before *concurrentWrite* ran.
 *
 * That is the interleave: a write path has already read the store when another window's write
 * lands, so anything derived from that read is built on a value now out of date. Real writes
 * are left alone, so only the read is stale — exactly the window a shared cell exposes.
 */
function withStaleRead(concurrentWrite: () => void, write: () => void): void {
  const frozen = new Map<string, string>()
  for (let i = 0; i < localStorage.length; i += 1) {
    const key = localStorage.key(i)
    if (key !== null) frozen.set(key, localStorage.getItem(key) ?? '')
  }
  concurrentWrite()
  const realGetItem = Storage.prototype.getItem
  Storage.prototype.getItem = function stale(key: string): string | null {
    return frozen.has(key) ? (frozen.get(key) as string) : null
  }
  try {
    write()
  } finally {
    Storage.prototype.getItem = realGetItem
  }
}

describe('the dirty beacon cannot lose a concurrent windows claim', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-01-01T00:00:00Z'))
    __resetSlotDirtyForTests()
    localStorage.clear()
  })

  afterEach(() => {
    vi.useRealTimers()
    __resetSlotDirtyForTests()
    localStorage.clear()
  })

  it('keeps a claim written by another window during a racing publish', () => {
    // The other window's claim lands after this one has read the store but before it writes,
    // so a whole-map write built on that earlier read erases it and the gate reads clean.
    withStaleRead(
      () => publishSlotDirty(OTHER, OTHER_SLOT, true, true),
      () => publishSlotDirty(MINE, SLOT, true, true),
    )

    expect(anyWindowSlotDirty(OTHER_SLOT)).toBe(true)
    expect(anyWindowSlotDirty(SLOT)).toBe(true)
  })

  it('control: with no stale read, both claims survive', () => {
    // Establishes that the loss above comes from the interleave rather than from
    // publishing twice, so the test measures the race and not ordinary sequencing.
    publishSlotDirty(OTHER, OTHER_SLOT, true, true)
    publishSlotDirty(MINE, SLOT, true, true)

    expect(anyWindowSlotDirty(OTHER_SLOT)).toBe(true)
    expect(anyWindowSlotDirty(SLOT)).toBe(true)
  })

  it('control: expiry still governs the READ, so a stale claim answers false', () => {
    // Without this the isolation above could be a silent weakening of the TTL.
    publishSlotDirty(OTHER, SLOT, true, true)
    vi.setSystemTime(new Date(Date.now() + CLAIM_TTL_MS + 1_000))

    expect(anyWindowSlotDirty(SLOT)).toBe(false)
  })

  it('control: retraction still clears the claim it owns', () => {
    publishSlotDirty(MINE, SLOT, true, true)
    expect(anyWindowSlotDirty(SLOT)).toBe(true)

    publishSlotDirty(MINE, null, false, true)

    expect(anyWindowSlotDirty(SLOT)).toBe(false)
  })

  it('ignores an unreadable entry under the prefix instead of failing the read', () => {
    localStorage.setItem(`${SLOT_DIRTY_KEY_PREFIX}corrupt`, 'not json at all')
    publishSlotDirty(MINE, SLOT, true, true)

    expect(anyWindowSlotDirty(SLOT)).toBe(true)
  })
})

describe('a side composer unmount clears only its own draft', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-01-01T00:00:00Z'))
    resetSideDrafts()
    __resetSlotDirtyForTests()
    localStorage.clear()
  })

  afterEach(() => {
    vi.useRealTimers()
    resetSideDrafts()
    __resetSlotDirtyForTests()
    localStorage.clear()
  })

  it('keeps a draft another window persisted for a DIFFERENT slot', () => {
    // A separate window writes storage directly, as a separate process does. A whole-store
    // write built on the earlier read erases its entry, and that slot then reads clean.
    withStaleRead(
      () => writeSideDraft(OTHER, OTHER_SLOT, 'the other windows draft'),
      () => writeSideDraft(MINE, SLOT, 'this windows draft'),
    )

    expect(loadSideDrafts()[OTHER_SLOT]).toHaveLength(1)
    expect(loadSideDrafts()[SLOT]).toHaveLength(1)
    expect(slotHasUnsentWork(OTHER_SLOT)).toBe(true)
  })

  it('leaves a still-mounted sibling draft on the same slot intact', () => {
    // Two hosts bound to one slot — a side panel and an embedded composer can be on
    // screen together, and a popout reaches the same key from another window.
    const staying = renderHook(() => useSlotDraftPersistence(SLOT, 'the draft still on screen'))
    const leaving = renderHook(() => useSlotDraftPersistence(SLOT, 'the panel being dismissed'))
    vi.advanceTimersByTime(DRAFT_SAVE_DEBOUNCE_MS + 10)

    leaving.unmount()

    expect(loadSideDrafts()[SLOT]).toHaveLength(1)
    expect(slotHasUnsentWork(SLOT)).toBe(true)
    staying.unmount()
  })

  it('releases the slot once the LAST composer on it unmounts', () => {
    // The behaviour the per-composer keying must not lose: nothing on screen holds the
    // draft, so a persisted copy would block this slot's close for the store's whole TTL.
    const first = renderHook(() => useSlotDraftPersistence(SLOT, 'first draft'))
    const second = renderHook(() => useSlotDraftPersistence(SLOT, 'second draft'))
    vi.advanceTimersByTime(DRAFT_SAVE_DEBOUNCE_MS + 10)

    first.unmount()
    second.unmount()

    expect(loadSideDrafts()[SLOT]).toBeUndefined()
  })
})
