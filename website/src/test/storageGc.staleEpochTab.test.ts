import { describe, it, expect, beforeEach, afterEach } from 'vitest'
import {
  __resetSessionIdentities,
  noteLiveSessionInstances,
  setSessionScopedItem,
} from '../utils/storageGc'

const ID = 'chat-8-800'
const KEY = `mc-panel-tabs:${ID}`
const OLD_SESSION = '2026-02-02T00:00:00Z'
const REPLACEMENT = '2026-08-08T00:00:00Z'
const LATER_CLOCK = '2026-08-08T00:00:00Z'
const CORRECTED_CLOCK = '2026-03-03T00:00:00Z'

afterEach(() => {
  __resetSessionIdentities()
  localStorage.clear()
})

describe('a tab left open across a restart cannot overwrite the replacement', () => {
  beforeEach(() => localStorage.clear())

  it('refuses its write once the replacement has stamped the current epoch', () => {
    noteLiveSessionInstances([{ key: ID, created: REPLACEMENT }],
      { generation: 3, epoch: 'gw-new' })
    setSessionScopedItem(localStorage, KEY, "the replacement's own panel layout")

    __resetSessionIdentities()
    noteLiveSessionInstances([{ key: ID, created: OLD_SESSION }],
      { generation: 41, epoch: 'gw-old' })
    localStorage.setItem('mc-storage-gc-order',
      JSON.stringify({ generation: 3, epoch: 'gw-new' }))

    setSessionScopedItem(localStorage, KEY, 'state from the departed session')

    expect(localStorage.getItem(KEY)).toBe("the replacement's own panel layout")
  })

  it('still lets the current epoch write after a backward clock correction', () => {
    noteLiveSessionInstances([{ key: ID, created: LATER_CLOCK }],
      { generation: 900, epoch: 'gw-old' })
    setSessionScopedItem(localStorage, KEY, 'before the restart')

    noteLiveSessionInstances([{ key: ID, created: CORRECTED_CLOCK }],
      { generation: 2, epoch: 'gw-new' })
    setSessionScopedItem(localStorage, KEY, 'after the restart')

    expect(localStorage.getItem(KEY)).toBe('after the restart')
  })

})
