import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { __resetForTests, loadSideDrafts, writeSideDraft } from '../utils/sideComposerDrafts'

describe('a side-draft write reports whether storage actually holds the text', () => {
  beforeEach(() => __resetForTests())
  afterEach(() => vi.restoreAllMocks())

  it('reports true when the write lands, and the draft is findable', () => {
    expect(writeSideDraft('composer-a', 'slot-1', 'typed')).toBe(true)
    expect(loadSideDrafts()['slot-1']).toEqual(['composer-a'])
  })

  it('reports FALSE when storage refuses the write', () => {
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new DOMException('quota', 'QuotaExceededError')
    })
    expect(writeSideDraft('composer-b', 'slot-1', 'typed')).toBe(false)
  })

  it('reports true for empty text, where storage agrees with the composer', () => {
    expect(writeSideDraft('composer-c', 'slot-1', '   ')).toBe(true)
    expect(loadSideDrafts()['slot-1']).toBeUndefined()
  })
})
