import { describe, it, expect, beforeEach } from 'vitest'
import { FILE_TOKEN_DRAFTS_KEY, loadFileTokenDrafts, saveFileTokenDrafts } from '../utils/chatFileTokenDrafts'

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
})
