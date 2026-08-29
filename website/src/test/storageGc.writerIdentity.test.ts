import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { __resetSessionIdentities, noteLiveSessionInstances, sessionOwnerStamp, setSessionScopedItem } from '../utils/storageGc'

const KEY = 'mc-panel-tabs:chat-7-700'
const ID = 'chat-7-700'
const FIRST = '2026-02-02T00:00:00Z'
const REPLACEMENT = '2026-07-07T00:00:00Z'

const asInstance = (created: string): void => {
  noteLiveSessionInstances([{ key: ID, created }])
}

afterEach(() => {
  __resetSessionIdentities()
  localStorage.clear()
})

describe('a writer that outlives a same-key replacement cannot borrow its identity', () => {
  beforeEach(() => localStorage.clear())

  it('rejects a delayed write whose captured owner is no longer current', () => {
    asInstance(FIRST)
    const captured = sessionOwnerStamp(ID)

    asInstance(REPLACEMENT)
    setSessionScopedItem(localStorage, KEY, 'replacement state')
    setSessionScopedItem(localStorage, KEY, 'state from the previous instance', captured)

    expect(localStorage.getItem(KEY)).toBe('replacement state')
  })

  it('lands the same write when the capturing instance is still the owner', () => {
    asInstance(REPLACEMENT)
    const captured = sessionOwnerStamp(ID)
    setSessionScopedItem(localStorage, KEY, 'replacement state')

    setSessionScopedItem(localStorage, KEY, 'its own later flush', captured)

    expect(localStorage.getItem(KEY)).toBe('its own later flush')
  })

  it('is not reachable by the stamp ledger alone, which is why identity is threaded', () => {
    asInstance(FIRST)
    asInstance(REPLACEMENT)
    setSessionScopedItem(localStorage, KEY, 'replacement state')

    setSessionScopedItem(localStorage, KEY, 'state from the previous instance')

    expect(localStorage.getItem(KEY)).toBe('state from the previous instance')
  })
})
