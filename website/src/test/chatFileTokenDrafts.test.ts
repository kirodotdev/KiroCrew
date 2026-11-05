import { describe, it, expect, beforeEach } from 'vitest'
import { FILE_TOKEN_DRAFTS_KEY, NO_SLOT_TOKEN_KEY, loadFileTokenDrafts, saveFileTokenDrafts } from '../utils/chatFileTokenDrafts'

describe('chatFileTokenDrafts', () => {
  beforeEach(() => { sessionStorage.clear() })

  it('roundtrips per-slot alias maps through sessionStorage', () => {
    const drafts = {
      'chat-1-100': { '/repo/report': ['@report'], '/repo/report,': ['@report,'] },
      'chat-2-200': { '/repo/src/main.ts': ['@src/main.ts', '@main.ts'] },
    }
    saveFileTokenDrafts(drafts)
    expect(loadFileTokenDrafts()).toEqual(drafts)
    expect(sessionStorage.getItem(FILE_TOKEN_DRAFTS_KEY)).toBe(JSON.stringify(drafts))
  })

  it('returns {} on missing, corrupt, or non-object storage', () => {
    expect(loadFileTokenDrafts()).toEqual({})
    sessionStorage.setItem(FILE_TOKEN_DRAFTS_KEY, 'not json')
    expect(loadFileTokenDrafts()).toEqual({})
    sessionStorage.setItem(FILE_TOKEN_DRAFTS_KEY, '[]')
    expect(loadFileTokenDrafts()).toEqual({})
  })

  it('drops malformed aliases, paths and slots', () => {
    sessionStorage.setItem(FILE_TOKEN_DRAFTS_KEY, JSON.stringify({
      good: { '/repo/a': ['@a', 42, 'no-at', '@', null, '@b'], '/repo/x': 'not-an-array' },
      'all-bad': { '/repo/y': ['plain'] },
      'not-a-map': ['@a'],
      empty: {},
    }))
    expect(loadFileTokenDrafts()).toEqual({ good: { '/repo/a': ['@a', '@b'] } })
  })

  it('never persists the reserved no-slot bucket, and never loads it', () => {
    // The new-chat (slotless) composer records its aliases under the reserved
    // bucket, which must stay in-memory only (#14571): an abandoned new-chat
    // draft's aliases must not survive a reload and revive a file later.
    saveFileTokenDrafts({
      'chat-1-100': { '/repo/a': ['@a'] },
      [NO_SLOT_TOKEN_KEY]: { '/repo/noslot': ['@noslot'] },
    })
    const stored = JSON.parse(sessionStorage.getItem(FILE_TOKEN_DRAFTS_KEY)!)
    expect(stored).toEqual({ 'chat-1-100': { '/repo/a': ['@a'] } })
    expect(NO_SLOT_TOKEN_KEY in stored).toBe(false)

    // A reserved bucket that somehow reaches storage is dropped on load too.
    sessionStorage.setItem(FILE_TOKEN_DRAFTS_KEY, JSON.stringify({
      'chat-1-100': { '/repo/a': ['@a'] },
      [NO_SLOT_TOKEN_KEY]: { '/repo/noslot': ['@noslot'] },
    }))
    const loaded = loadFileTokenDrafts()
    expect(loaded).toEqual({ 'chat-1-100': { '/repo/a': ['@a'] } })
    expect(NO_SLOT_TOKEN_KEY in loaded).toBe(false)
  })
})
