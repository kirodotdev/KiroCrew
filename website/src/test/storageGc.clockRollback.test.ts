import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { __resetSessionIdentities, noteLiveSessionInstances, setSessionScopedItem } from '../utils/storageGc'

const KEY = 'mc-panel-tabs:chat-9-900'
const ID = 'chat-9-900'
const LATER_CLOCK = '2026-06-06T00:00:00Z'
const CORRECTED_CLOCK = '2026-01-01T00:00:00Z'
const EPOCH = 'gw-epoch-1'

afterEach(() => {
  __resetSessionIdentities()
  localStorage.clear()
})

describe('ordering survives a backward clock correction', () => {
  beforeEach(() => localStorage.clear())

  it('lets a replacement own its writes even when its stamp reads earlier', () => {
    noteLiveSessionInstances([{ key: ID, created: LATER_CLOCK }], { generation: 5, epoch: EPOCH })
    setSessionScopedItem(localStorage, KEY, 'original state')

    noteLiveSessionInstances([{ key: ID, created: CORRECTED_CLOCK }], { generation: 9, epoch: EPOCH })
    setSessionScopedItem(localStorage, KEY, 'replacement state')

    expect(localStorage.getItem(KEY)).toBe('replacement state')
  })

  it('still refuses a writer the ledger proves has been replaced', () => {
    noteLiveSessionInstances([{ key: ID, created: CORRECTED_CLOCK }], { generation: 9, epoch: EPOCH })
    setSessionScopedItem(localStorage, KEY, 'replacement state')

    noteLiveSessionInstances([{ key: ID, created: LATER_CLOCK }], { generation: 5, epoch: EPOCH })
    setSessionScopedItem(localStorage, KEY, 'stale state')

    expect(localStorage.getItem(KEY)).toBe('replacement state')
  })

  it('does not compare generations minted by different gateway processes', () => {
    noteLiveSessionInstances([{ key: ID, created: LATER_CLOCK }], { generation: 900, epoch: 'gw-old' })
    setSessionScopedItem(localStorage, KEY, 'before the restart')

    noteLiveSessionInstances([{ key: ID, created: CORRECTED_CLOCK }], { generation: 2, epoch: 'gw-new' })
    setSessionScopedItem(localStorage, KEY, 'after the restart')

    expect(localStorage.getItem(KEY)).toBe('after the restart')
  })
})
