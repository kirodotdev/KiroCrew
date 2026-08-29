import { describe, it, expect, afterEach } from 'vitest'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import {
  __resetSessionIdentities,
  noteLiveSessionInstances,
  sessionOwnerStamp,
  setSessionScopedItem,
} from '../utils/storageGc'
import { saveScrollAnchor } from '../hooks/virtualizer/ScrollAnchorCache'

const read = (rel: string): string => readFileSync(resolve(__dirname, rel), 'utf-8')
const ID = 'chat-6-600'
const KEY = `vc_anchor3_${ID}`
const DEPARTED = '2026-01-01T00:00:00Z'
const REPLACEMENT = '2026-09-09T00:00:00Z'

afterEach(() => {
  __resetSessionIdentities()
  localStorage.clear()
})

describe('a scroll anchor scheduled before a replacement cannot land on it', () => {
  it('drops the departed session anchor at flush time', () => {
    noteLiveSessionInstances([{ key: ID, created: DEPARTED }])
    const capturedWhenScheduled = sessionOwnerStamp(ID)

    noteLiveSessionInstances([{ key: ID, created: REPLACEMENT }])
    setSessionScopedItem(localStorage, KEY, 'the replacement own anchor')

    saveScrollAnchor(ID, { key: 'row-9', top: 42 }, capturedWhenScheduled)

    expect(localStorage.getItem(KEY)).toBe('the replacement own anchor')
  })

  it('still writes for the session that scheduled it', () => {
    noteLiveSessionInstances([{ key: ID, created: REPLACEMENT }])

    saveScrollAnchor(ID, { key: 'row-3', top: 7 }, sessionOwnerStamp(ID))

    expect(localStorage.getItem(KEY)).toContain('row-3')
  })

  it('captures the owner when the debounce is armed, not at flush', () => {
    const src = read('../hooks/virtualizer/useVirtualChat.ts')

    expect(src).toMatch(/const scheduledOwner = sessionOwnerStamp\(scheduledSession\)/)
    expect(src).toMatch(/saveScrollAnchor\(scheduledSession, a, scheduledOwner\)/)
  })
})
