import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { MAX_ABSENT_SESSIONS, __resetSessionIdentities, gcOrphanedStorage, noteLiveSessionInstances, recordSessionStorageOwner } from '../utils/storageGc'

/** BLOCKING F2 -- capacity eviction must not reach live session state.
 *
 *  Two causes, both required. The id derivation truncated at the first delimiter, so a
 *  channel-scoped key (itself `slack:<ts>`) resolved to `slack`, matched no live session and
 *  was classified ABSENT. Absent non-derived keys were then capacity-evicted, permanently
 *  deleting the panel, activity and preview state of a session that was live. Non-derived
 *  state is now deleted only on a stamp mismatch -- positive proof of supersession. */

const CREATED = '2026-01-01T00:00:00Z'
const slot = (key: string, created = CREATED) => ({ key, created })

beforeEach(() => {
  localStorage.clear()
  noteLiveSessionInstances([])
})

afterEach(() => {
  __resetSessionIdentities()
  localStorage.clear()
})

describe('non-derived state is deleted only on supersession proof', () => {
  /** THE REGRESSION: an unlisted session far past any budget keeps its real state, because
   *  absence is not proof -- it may be live in another tab. */
  it('keeps unlisted non-derived state however many sessions are absent', () => {
    for (let i = 0; i < MAX_ABSENT_SESSIONS + 40; i++) {
      const key = `mc-panel-tabs:chat-${i}`
      noteLiveSessionInstances([slot(`chat-${i}`)])
      localStorage.setItem(key, 'panel state')
      recordSessionStorageOwner(key)
    }

    gcOrphanedStorage([])

    for (let i = 0; i < MAX_ABSENT_SESSIONS + 40; i++) {
      expect(localStorage.getItem(`mc-panel-tabs:chat-${i}`)).toBe('panel state')
    }
  })

  /** Control: proof still deletes. A listed key whose stamp differs IS superseded, so the
   *  fix withholds deletion for want of proof rather than abandoning collection. */
  it('deletes non-derived state when the stamp proves supersession', () => {
    const key = 'mc-panel-tabs:chat-1'
    noteLiveSessionInstances([slot('chat-1')])
    localStorage.setItem(key, 'panel state')
    recordSessionStorageOwner(key)

    gcOrphanedStorage([slot('chat-1', '2026-06-06T00:00:00Z')])

    expect(localStorage.getItem(key)).toBeNull()
  })

  /** Derived caches keep their capacity budget -- they are rebuildable, so the ceiling that
   *  protects the origin quota still applies to them. */
  it('still capacity-evicts derived caches', () => {
    for (let i = 0; i < MAX_ABSENT_SESSIONS + 4; i++) {
      const key = `vc_heights_chat-${i}`
      noteLiveSessionInstances([slot(`chat-${i}`)])
      localStorage.setItem(key, 'height cache')
      recordSessionStorageOwner(key)
      const entryKey = `mc-storage-gc-owner:${key}`
      const entry = JSON.parse(localStorage.getItem(entryKey) ?? 'null') as { seenAt: number }
      entry.seenAt = 1000 + i
      localStorage.setItem(entryKey, JSON.stringify(entry))
    }

    gcOrphanedStorage([])

    expect(localStorage.getItem('vc_heights_chat-0')).toBeNull()
    expect(localStorage.getItem(`vc_heights_chat-${MAX_ABSENT_SESSIONS + 3}`)).toBe('height cache')
  })
})
