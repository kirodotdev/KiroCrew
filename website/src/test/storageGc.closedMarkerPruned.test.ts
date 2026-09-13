import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { __resetSessionIdentities, gcOrphanedStorage, noteLiveSessionInstances, noteSessionClosed, setSessionScopedItem } from '../utils/storageGc'

const ID = 'chat-13-1300'
const KEY = `mc-panel-tabs:${ID}`
const STAMP = '2026-01-01T00:00:00Z'

const markers = (): string[] => {
  const out: string[] = []
  for (let i = 0; i < localStorage.length; i++) {
    const k = localStorage.key(i)
    if (k && k.startsWith('mc-storage-gc-closed:')) out.push(k)
  }
  return out
}

afterEach(() => {
  __resetSessionIdentities()
  localStorage.clear()
})

describe('close proofs do not outlive the gateway process that minted them', () => {
  beforeEach(() => localStorage.clear())

  it('drops a proof from a retired epoch', () => {
    noteLiveSessionInstances([{ key: ID, created: STAMP }], { generation: 4, epoch: 'gw-old' })
    setSessionScopedItem(localStorage, KEY, 'state')
    noteSessionClosed(ID)
    expect(markers().length).toBe(1)

    gcOrphanedStorage([], { generation: 2, epoch: 'gw-new' })

    expect(markers()).toEqual([])
  })

  it('keeps a proof its own epoch can still use', () => {
    noteLiveSessionInstances([{ key: ID, created: STAMP }], { generation: 4, epoch: 'gw-same' })
    setSessionScopedItem(localStorage, KEY, 'state')
    noteSessionClosed(ID)

    gcOrphanedStorage([], { generation: 4, epoch: 'gw-same' })

    expect(markers().length).toBe(1)
  })
})
