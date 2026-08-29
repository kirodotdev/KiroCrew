import { describe, it, expect, beforeEach } from 'vitest'
import { gcOrphanedStorage, recordSessionStorageOwner, MAX_ABSENT_SESSIONS } from '../utils/storageGc'

/**
 * A channel-scoped session key carries its own delimiter (`slack:<ts>`), and the live map
 * is keyed by the full key. Resolving a storage key's owner by its first colon-separated
 * field collapses every such session onto one id, which both starves the capacity budget
 * and makes a genuinely live session read as absent.
 */
describe('storage gc resolves a delimited session key whole', () => {
  beforeEach(() => {
    window.localStorage.clear()
  })

  it('counts each channel-scoped session against the budget instead of collapsing them', () => {
    const total = MAX_ABSENT_SESSIONS + 6
    for (let i = 0; i < total; i++) {
      window.localStorage.setItem(`vc_heights_slack:17000000${String(i).padStart(2, '0')}`, '{}')
    }

    const removed = gcOrphanedStorage([])

    expect(removed).toBe(total - MAX_ABSENT_SESSIONS)
  })

  it('protects a live channel-scoped session from capacity eviction', () => {
    const liveKey = 'slack:1700000042'
    window.localStorage.setItem(`vc_heights_${liveKey}`, '{}')
    for (let i = 0; i < MAX_ABSENT_SESSIONS + 6; i++) {
      window.localStorage.setItem(`vc_heights_dead-${i}`, '{}')
    }

    gcOrphanedStorage([{ key: liveKey, created: '2026-01-01T00:00:00Z' }])

    expect(window.localStorage.getItem(`vc_heights_${liveKey}`)).not.toBeNull()
  })

  it('stamps the ledger under the full key so a delimited session is collectable at all', () => {
    const liveKey = 'slack:1700000099'
    const stateKey = `mc-panel-tabs:${liveKey}`
    window.localStorage.setItem(stateKey, 'a')
    gcOrphanedStorage([{ key: liveKey, created: '2026-01-01T00:00:00Z' }])
    recordSessionStorageOwner(stateKey)

    gcOrphanedStorage([{ key: liveKey, created: '2026-06-06T00:00:00Z' }])

    expect(window.localStorage.getItem(stateKey)).toBeNull()
  })
})
