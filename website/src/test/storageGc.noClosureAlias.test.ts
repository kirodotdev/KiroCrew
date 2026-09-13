import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { __resetSessionIdentities, gcOrphanedStorage, noteLiveSessionInstances, recordSessionStorageOwner } from '../utils/storageGc'

const KEY = 'mc-panel-tabs:chat-8-800'
const ID = 'chat-8-800'
const STAMP = '2026-01-01T00:00:00Z'
const LEGACY_MARKER = 'mc-storage-gc-closed:' + ID

const ownAs = (created: string): void => {
  noteLiveSessionInstances([{ key: ID, created }])
  recordSessionStorageOwner(KEY)
}

afterEach(() => {
  __resetSessionIdentities()
  localStorage.clear()
})

describe('closure evidence cannot authorise deleting a same-stamp resumed session', () => {
  beforeEach(() => localStorage.clear())

  it('ignores a closure marker naming the very stamp that still owns the key', () => {
    localStorage.setItem(KEY, 'live state')
    ownAs(STAMP)
    localStorage.setItem(LEGACY_MARKER, STAMP)

    expect(gcOrphanedStorage([])).toBe(0)
    expect(localStorage.getItem(KEY)).toBe('live state')
  })

  it('leaves no closure marker of its own behind', () => {
    localStorage.setItem(KEY, 'live state')
    ownAs(STAMP)
    gcOrphanedStorage([])

    const markers: string[] = []
    for (let i = 0; i < localStorage.length; i++) {
      const k = localStorage.key(i)
      if (k && k.includes('gc-closed')) markers.push(k)
    }

    expect(markers).toEqual([])
  })

  it('still collects on supersession proof, which names an instance', () => {
    localStorage.setItem(KEY, 'old state')
    ownAs(STAMP)

    expect(gcOrphanedStorage([{ key: ID, created: '2026-06-06T00:00:00Z' }])).toBe(1)
    expect(localStorage.getItem(KEY)).toBeNull()
  })
})
