import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { __resetSessionIdentities, noteLiveSessionInstances, sessionOwnerStamp, setSessionScopedItem } from '../utils/storageGc'
import { safeSetItem } from '../utils/safeStorage'

const ID = 'chat-9-900'
const KEY = `mc-panel-tabs:${ID}`
const DEPARTED = '2026-03-03T00:00:00Z'
const REPLACEMENT = '2026-09-09T00:00:00Z'

const asInstance = (created: string): void => {
  noteLiveSessionInstances([{ key: ID, created }])
}

afterEach(() => {
  __resetSessionIdentities()
  localStorage.clear()
})

describe('a panel-tab write scheduled before a replacement cannot land on it', () => {
  beforeEach(() => localStorage.clear())

  it('drops the departed instance bucket at flush time', () => {
    asInstance(DEPARTED)
    const ownerAtScheduleTime = sessionOwnerStamp(ID)

    asInstance(REPLACEMENT)
    setSessionScopedItem(localStorage, KEY, 'replacement layout')

    safeSetItem(KEY, 'departed instance layout', ownerAtScheduleTime)

    expect(localStorage.getItem(KEY)).toBe('replacement layout')
  })

  it('still writes when the scheduling instance is the one that flushes', () => {
    asInstance(REPLACEMENT)
    const owner = sessionOwnerStamp(ID)

    safeSetItem(KEY, 'its own layout', owner)

    expect(localStorage.getItem(KEY)).toBe('its own layout')
  })

  it('is not reachable by the staleness ledger alone', () => {
    asInstance(DEPARTED)
    asInstance(REPLACEMENT)
    setSessionScopedItem(localStorage, KEY, 'replacement layout')

    safeSetItem(KEY, 'departed instance layout')

    expect(localStorage.getItem(KEY)).toBe('departed instance layout')
  })
})

describe('the debounced writer captures its owner when the write is scheduled', () => {
  it('passes that captured owner through the flush, not a fresh lookup', () => {
    const src = readFileSync(resolve(__dirname, '../hooks/usePanelTabs.ts'), 'utf-8')

    expect(src).toMatch(/dirtySlots\.set\(slot, sessionOwnerStamp\(slot\)\)/)
    expect(src).toMatch(/safeSetItem\(KEY_PREFIX \+ slot, serializeBucket\(b\), owner\)/)
  })
})
