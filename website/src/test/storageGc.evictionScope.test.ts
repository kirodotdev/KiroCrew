import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { MAX_ABSENT_SESSIONS, __resetSessionIdentities, gcOrphanedStorage, noteLiveSessionInstances, recordSessionStorageOwner } from '../utils/storageGc'

/** THE BLOCKING FINDING -- capacity eviction must not delete a live
 *  cross-tab session's state.
 *
 *  A boot snapshot omits a session another tab has just resumed, so an "absent" session can
 *  be fully LIVE and no tab can prove otherwise. Evicting its keys under the capacity
 *  budget therefore destroyed live shared state. The budget now reaches ONLY purely derived
 *  caches -- heights and reading anchors, which the virtualizer re-measures from the DOM --
 *  and never the session's own panel/activity/preview state. */

const HEIGHTS = 'vc_heights_'
const ANCHOR = 'vc_anchor3_'
const TABS = 'mc-panel-tabs:'
const ACTIVITY = 'mc-activity-open:'
const PREVIEW = 'mc-webpreview-url:'
const slot = (key: string, created?: string) => ({ key, created })

const writeAt = (key: string, id: string, at: number): void => {
  noteLiveSessionInstances([slot(id, '2026-01-01T00:00:00Z')])
  localStorage.setItem(key, '{}')
  const real = Date.now
  Date.now = () => at
  try {
    recordSessionStorageOwner(key)
  } finally {
    Date.now = real
  }
}

afterEach(() => {
  __resetSessionIdentities()
  localStorage.clear()
})

describe('capacity eviction never reaches real session state', () => {
  beforeEach(() => localStorage.clear())

  /** THE REGRESSION: an unlisted-but-live session keeps everything that is not derivable. */
  it('spares panel, activity and preview state of an over-budget absent session', () => {
    const over = MAX_ABSENT_SESSIONS + 4
    // The coldest session of all owns one key of every family.
    for (const p of [HEIGHTS, ANCHOR, TABS, ACTIVITY, PREVIEW]) writeAt(`${p}chat-cold`, 'chat-cold', 1)
    for (let i = 0; i < over; i++) writeAt(`${HEIGHTS}chat-${i}`, `chat-${i}`, 10_000 + i)

    gcOrphanedStorage([])

    // Its derived caches may go; its own state may not.
    expect(localStorage.getItem(`${TABS}chat-cold`)).toBe('{}')
    expect(localStorage.getItem(`${ACTIVITY}chat-cold`)).toBe('{}')
    expect(localStorage.getItem(`${PREVIEW}chat-cold`)).toBe('{}')
  })

  it('still bounds the derived caches, which are the growth', () => {
    const over = MAX_ABSENT_SESSIONS + 5
    for (let i = 0; i < over; i++) writeAt(`${HEIGHTS}chat-${i}`, `chat-${i}`, 1_000 + i)

    const removed = gcOrphanedStorage([])

    expect(removed).toBe(5)
    expect(localStorage.getItem(`${HEIGHTS}chat-0`)).toBeNull()
    expect(localStorage.getItem(`${HEIGHTS}chat-${over - 1}`)).toBe('{}')
  })

  /** A session with no derived keys cannot be pushed over the budget by its own state, so
   *  it never becomes a candidate and never consumes the budget either. */
  it('does not count a state-only absent session against the budget', () => {
    for (let i = 0; i < MAX_ABSENT_SESSIONS + 3; i++) writeAt(`${TABS}chat-${i}`, `chat-${i}`, 1_000 + i)

    expect(gcOrphanedStorage([])).toBe(0)
    expect(localStorage.getItem(`${TABS}chat-0`)).toBe('{}')
  })

  it('never evicts a LISTED session, however cold', () => {
    const over = MAX_ABSENT_SESSIONS + 3
    writeAt(`${HEIGHTS}chat-live`, 'chat-live', 1)
    for (let i = 0; i < over; i++) writeAt(`${HEIGHTS}chat-${i}`, `chat-${i}`, 10_000 + i)

    gcOrphanedStorage([slot('chat-live', '2026-01-01T00:00:00Z')])

    expect(localStorage.getItem(`${HEIGHTS}chat-live`)).toBe('{}')
  })
})
