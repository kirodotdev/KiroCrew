import { describe, it, expect, beforeEach, afterEach } from 'vitest'
import { __resetSessionIdentities, gcOrphanedStorage, noteLiveSessionInstances, recordSessionStorageOwner } from '../utils/storageGc'

const A_KEY = 'mc-panel-tabs:chat-a-1'
const B_KEY = 'mc-panel-tabs:chat-b-1'
const OLD = '2026-01-01T00:00:00Z'
const NEW = '2026-06-06T00:00:00Z'

describe('a tab stamping its own key cannot erase another tab entry', () => {
  const realGet = window.localStorage.getItem.bind(window.localStorage)

  beforeEach(() => {
    window.localStorage.clear()
  })

  afterEach(() => {
    window.localStorage.getItem = realGet
  })

  it('keeps the other tab proof, so the sweep still collects its superseded key', () => {
    window.localStorage.setItem(A_KEY, 'tab-a-state')
    window.localStorage.setItem(B_KEY, 'tab-b-state')

    noteLiveSessionInstances([{ key: 'chat-b-1', created: OLD }])
    recordSessionStorageOwner(B_KEY)

    const frozen = new Set<string>()
    for (let i = 0; i < window.localStorage.length; i++) {
      const k = window.localStorage.key(i)
      if (k && k.includes('gc-owner')) frozen.add(k)
    }
    window.localStorage.getItem = (k: string) => (frozen.has(k) ? null : realGet(k))
    noteLiveSessionInstances([{ key: 'chat-a-1', created: OLD }])
    recordSessionStorageOwner(A_KEY)
    window.localStorage.getItem = realGet

    gcOrphanedStorage([{ key: 'chat-b-1', created: NEW }])

    expect(window.localStorage.getItem(B_KEY)).toBeNull()
  })
})
