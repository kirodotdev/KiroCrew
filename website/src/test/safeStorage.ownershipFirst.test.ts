import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { safeSetItem } from '../utils/safeStorage'
import { __resetSessionIdentities, noteLiveSessionInstances, setSessionScopedItem } from '../utils/storageGc'

const KEY = 'mc-panel-tabs:chat-6-600'
const ID = 'chat-6-600'
const OLD = '2026-01-01T00:00:00Z'
const NEW = '2026-06-06T00:00:00Z'

afterEach(() => {
  __resetSessionIdentities()
  localStorage.clear()
})

describe('safeSetItem validates ownership before the value lands', () => {
  beforeEach(() => localStorage.clear())

  it('drops a stale tab write instead of overwriting the replacement state', () => {
    noteLiveSessionInstances([{ key: ID, created: NEW }])
    setSessionScopedItem(localStorage, KEY, 'replacement state')

    noteLiveSessionInstances([{ key: ID, created: OLD }])
    safeSetItem(KEY, 'stale state')

    expect(localStorage.getItem(KEY)).toBe('replacement state')
  })

  it('still writes for the owning instance', () => {
    noteLiveSessionInstances([{ key: ID, created: NEW }])

    expect(safeSetItem(KEY, 'mine')).toBe(true)
    expect(localStorage.getItem(KEY)).toBe('mine')
  })

  it('still writes a key that is not session-scoped', () => {
    expect(safeSetItem('mc-sidebar-pinned', 'true')).toBe(true)
    expect(localStorage.getItem('mc-sidebar-pinned')).toBe('true')
  })
})
