import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { __resetSessionIdentities, gcOrphanedStorage, noteLiveSessionInstances, setSessionScopedItem } from '../utils/storageGc'

const KEY = 'mc-panel-tabs:chat-11-1100'
const ID = 'chat-11-1100'
const EPOCH = 'gw-epoch-7'
const EARLY = '2026-01-01T00:00:00Z'
const LATE = '2026-06-06T00:00:00Z'

afterEach(() => {
  __resetSessionIdentities()
  localStorage.clear()
})

describe('the boot sweep orders by the server generation, not by the clock', () => {
  beforeEach(() => localStorage.clear())

  it('keeps state when the listing is an OLDER frame that merely reads later', () => {
    noteLiveSessionInstances([{ key: ID, created: EARLY }], { generation: 9, epoch: EPOCH })
    setSessionScopedItem(localStorage, KEY, 'live state')

    expect(gcOrphanedStorage([{ key: ID, created: LATE }], { generation: 4, epoch: EPOCH })).toBe(0)
    expect(localStorage.getItem(KEY)).toBe('live state')
  })

  it('never deletes a session that is simply listed again in a later frame', () => {
    noteLiveSessionInstances([{ key: ID, created: EARLY }], { generation: 4, epoch: EPOCH })
    setSessionScopedItem(localStorage, KEY, 'live state')

    expect(gcOrphanedStorage([{ key: ID, created: EARLY }], { generation: 40, epoch: EPOCH })).toBe(0)
    expect(localStorage.getItem(KEY)).toBe('live state')
  })

  it('still collects a genuine replacement, proven by a later frame', () => {
    noteLiveSessionInstances([{ key: ID, created: EARLY }], { generation: 4, epoch: EPOCH })
    setSessionScopedItem(localStorage, KEY, 'old state')

    expect(gcOrphanedStorage([{ key: ID, created: LATE }], { generation: 9, epoch: EPOCH })).toBe(1)
    expect(localStorage.getItem(KEY)).toBeNull()
  })

  it('leaves the write path able to order after a sweep has run', () => {
    noteLiveSessionInstances([{ key: ID, created: LATE }], { generation: 9, epoch: EPOCH })
    setSessionScopedItem(localStorage, KEY, 'replacement state')
    gcOrphanedStorage([{ key: ID, created: LATE }], { generation: 9, epoch: EPOCH })

    noteLiveSessionInstances([{ key: ID, created: EARLY }], { generation: 4, epoch: EPOCH })
    setSessionScopedItem(localStorage, KEY, 'stale state')

    expect(localStorage.getItem(KEY)).toBe('replacement state')
  })
})
