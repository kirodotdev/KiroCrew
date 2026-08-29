import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { MAX_ABSENT_SESSIONS, __resetSessionIdentities, gcOrphanedStorage, noteLiveSessionInstances, recordSessionStorageOwner } from '../utils/storageGc'

const STATE = 'mc-panel-tabs:'
const HEIGHTS = 'vc_heights_'
const CREATED = '2026-01-01T00:00:00Z'

const writeAt = (prefix: string, id: string, at: number): void => {
  noteLiveSessionInstances([{ key: id, created: CREATED }])
  localStorage.setItem(`${prefix}${id}`, 'state')
  const realNow = Date.now
  Date.now = () => at
  try {
    recordSessionStorageOwner(`${prefix}${id}`)
  } finally {
    Date.now = realNow
  }
}

const survivors = (prefix: string): number => {
  let n = 0
  for (let i = 0; i < localStorage.length; i++) {
    const k = localStorage.key(i)
    if (k && k.startsWith(prefix)) n++
  }
  return n
}

afterEach(() => {
  __resetSessionIdentities()
  localStorage.clear()
})

describe('a session absent from the boot list keeps state that cannot be rebuilt', () => {
  beforeEach(() => localStorage.clear())

  it('keeps every non-derived key however cold and however many', () => {
    const ancient = 1_000
    const many = MAX_ABSENT_SESSIONS * 3
    for (let i = 0; i < many; i++) writeAt(STATE, `chat-${i}-x`, ancient + i)

    const removed = gcOrphanedStorage([])

    expect(removed).toBe(0)
    expect(survivors(STATE)).toBe(many)
  })

  it('still caps derived caches, which rebuild themselves', () => {
    const many = MAX_ABSENT_SESSIONS + 4
    for (let i = 0; i < many; i++) writeAt(HEIGHTS, `chat-${i}-y`, 1_000 + i)

    gcOrphanedStorage([])

    expect(survivors(HEIGHTS)).toBe(MAX_ABSENT_SESSIONS)
  })
})
