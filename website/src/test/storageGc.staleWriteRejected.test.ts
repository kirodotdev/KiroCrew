import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { __resetSessionIdentities, noteLiveSessionInstances, recordSessionStorageOwner, setSessionScopedItem } from '../utils/storageGc'

const KEY = 'mc-panel-tabs:chat-5-500'
const ID = 'chat-5-500'
const OLD = '2026-01-01T00:00:00Z'
const NEW = '2026-06-06T00:00:00Z'

const asInstance = (created: string): void => {
  noteLiveSessionInstances([{ key: ID, created }])
}

afterEach(() => {
  __resetSessionIdentities()
  localStorage.clear()
})

describe('a stale instance cannot overwrite the replacement session state', () => {
  beforeEach(() => localStorage.clear())

  it('drops the value write, not just the owner stamp', () => {
    asInstance(NEW)
    setSessionScopedItem(localStorage, KEY, 'replacement state')

    asInstance(OLD)
    setSessionScopedItem(localStorage, KEY, 'stale state')

    expect(localStorage.getItem(KEY)).toBe('replacement state')
  })

  it('still lets the owning instance write', () => {
    asInstance(NEW)
    setSessionScopedItem(localStorage, KEY, 'first')
    setSessionScopedItem(localStorage, KEY, 'second')

    expect(localStorage.getItem(KEY)).toBe('second')
  })

  it('still lets a NEWER instance take the key over', () => {
    asInstance(OLD)
    setSessionScopedItem(localStorage, KEY, 'old state')

    asInstance(NEW)
    setSessionScopedItem(localStorage, KEY, 'newer state')

    expect(localStorage.getItem(KEY)).toBe('newer state')
  })

  it('writes when the instance is unknown, which stamps nothing and collects nothing', () => {
    noteLiveSessionInstances([])
    setSessionScopedItem(localStorage, KEY, 'unstamped')

    expect(localStorage.getItem(KEY)).toBe('unstamped')
  })

  it('keeps refusing the stale owner stamp itself', () => {
    asInstance(NEW)
    setSessionScopedItem(localStorage, KEY, 'replacement state')
    asInstance(OLD)
    recordSessionStorageOwner(KEY)

    const raw = localStorage.getItem('mc-storage-gc-owner:' + KEY)
    expect(raw).not.toBeNull()
    expect((JSON.parse(raw as string) as { stamp: string }).stamp).toBe(NEW)
  })
})
