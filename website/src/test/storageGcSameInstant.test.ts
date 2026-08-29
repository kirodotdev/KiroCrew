/** Two spellings of one instant are the SAME writer, in the generation branch too. */

import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { __resetSessionIdentities, gcOrphanedStorage, noteLiveSessionInstances } from '../utils/storageGc'

const ID = 'chat-31'
const KEY = `mc-panel-tabs:${ID}`
const EPOCH = 'gw-epoch-3'
const AS_Z = '2026-03-04T05:06:07Z'
const AS_OFFSET = '2026-03-04T05:06:07+00:00'

const ledgerEntry = (stamp: string, generation: number): void => {
  localStorage.setItem(
    `mc-storage-gc-owner:${KEY}`,
    JSON.stringify({ stamp, seenAt: Date.now(), generation, epoch: EPOCH }),
  )
}

afterEach(() => {
  __resetSessionIdentities()
  localStorage.clear()
})

describe('the generation branch does not mistake a spelling for a replacement', () => {
  beforeEach(() => localStorage.clear())

  it('keeps state whose recorded stamp is the same instant spelled differently', () => {
    localStorage.setItem(KEY, '{"tab":"files"}')
    ledgerEntry(AS_OFFSET, 4)
    noteLiveSessionInstances([{ key: ID, created: AS_Z }], { generation: 9, epoch: EPOCH })

    gcOrphanedStorage([{ key: ID, created: AS_Z }], { generation: 9, epoch: EPOCH })

    expect(localStorage.getItem(KEY)).toBe('{"tab":"files"}')
  })

  it('still deletes state a genuinely later instance superseded', () => {
    localStorage.setItem(KEY, '{"tab":"stale"}')
    ledgerEntry('2026-01-01T00:00:00Z', 4)
    noteLiveSessionInstances([{ key: ID, created: AS_Z }], { generation: 9, epoch: EPOCH })

    gcOrphanedStorage([{ key: ID, created: AS_Z }], { generation: 9, epoch: EPOCH })

    expect(localStorage.getItem(KEY)).toBeNull()
  })
})
