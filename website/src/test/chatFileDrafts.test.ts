import { describe, it, expect, beforeEach } from 'vitest'
import { FILE_DRAFTS_KEY, loadFileDrafts, saveFileDrafts, setFileDraft } from '../utils/chatFileDrafts'

describe('chatFileDrafts', () => {
  beforeEach(() => { sessionStorage.clear() })

  it('roundtrips file drafts through sessionStorage', () => {
    const drafts = {
      'chat-1-100': ['/tmp/a.png', '/tmp/b.png'],
      'chat-2-200': ['/tmp/report.pdf'],
    }
    saveFileDrafts(drafts)
    expect(loadFileDrafts()).toEqual(drafts)
    // Raw key matches contract (so migration / debugging is possible)
    expect(sessionStorage.getItem(FILE_DRAFTS_KEY)).toBe(JSON.stringify(drafts))
  })

  it('returns {} on missing, corrupt, or non-object storage', () => {
    expect(loadFileDrafts()).toEqual({})
    sessionStorage.setItem(FILE_DRAFTS_KEY, 'not json')
    expect(loadFileDrafts()).toEqual({})
    sessionStorage.setItem(FILE_DRAFTS_KEY, '[]')
    expect(loadFileDrafts()).toEqual({})
    sessionStorage.setItem(FILE_DRAFTS_KEY, 'null')
    expect(loadFileDrafts()).toEqual({})
  })

  it('filters out non-array and non-string-element values (corruption guard)', () => {
    sessionStorage.setItem(FILE_DRAFTS_KEY, JSON.stringify({
      'good': ['/tmp/a.png'],
      'string-value': 'not-an-array',
      'number-value': 42,
      'null-value': null,
      'mixed-array': ['/tmp/b.png', 42, null, '/tmp/c.png'],
      'empty-array': [],
    }))
    expect(loadFileDrafts()).toEqual({
      'good': ['/tmp/a.png'],
      'mixed-array': ['/tmp/b.png', '/tmp/c.png'],
      // empty-array is dropped (no point persisting empty slots)
    })
  })

  it('setFileDraft stores non-empty and deletes empty', () => {
    const d: Record<string, string[]> = { 'chat-1-100': ['/tmp/old.png'] }
    setFileDraft(d, 'chat-1-100', ['/tmp/new1.png', '/tmp/new2.png'])
    expect(d).toEqual({ 'chat-1-100': ['/tmp/new1.png', '/tmp/new2.png'] })
    setFileDraft(d, 'chat-1-100', [])
    expect(d).toEqual({})
  })

  it('setFileDraft stores a defensive copy so caller mutations do not leak', () => {
    const d: Record<string, string[]> = {}
    const live: string[] = ['/tmp/a.png']
    setFileDraft(d, 'chat-1', live)
    // Caller keeps appending to the live array — stored draft must not track it
    live.push('/tmp/b.png')
    expect(d['chat-1']).toEqual(['/tmp/a.png'])
  })

  it('saveFileDrafts swallows QuotaExceededError without throwing', () => {
    const orig = Storage.prototype.setItem
    Storage.prototype.setItem = () => { throw new Error('QuotaExceeded') }
    try {
      expect(() => saveFileDrafts({ 'a': ['/tmp/x.png'] })).not.toThrow()
    } finally {
      Storage.prototype.setItem = orig
    }
  })

  it('uses sessionStorage not localStorage (tab-close clears)', () => {
    saveFileDrafts({ 'chat-1': ['/tmp/a.png'] })
    // Must land in sessionStorage
    expect(sessionStorage.getItem(FILE_DRAFTS_KEY)).toBeTruthy()
    // Must NOT land in localStorage
    expect(localStorage.getItem(FILE_DRAFTS_KEY)).toBeNull()
  })

  it('round-trips survive a simulated slot-switch cycle', () => {
    // Save slot A's pending files
    const d: Record<string, string[]> = {}
    setFileDraft(d, 'slot-a', ['/tmp/screenshot-1.png'])
    saveFileDrafts(d)
    // Simulate fresh load from storage (new slot selected)
    const reloaded = loadFileDrafts()
    expect(reloaded['slot-a']).toEqual(['/tmp/screenshot-1.png'])
    // Add slot B's pending files
    setFileDraft(reloaded, 'slot-b', ['/tmp/screenshot-2.png'])
    saveFileDrafts(reloaded)
    // Both should be present and isolated
    const final = loadFileDrafts()
    expect(final).toEqual({
      'slot-a': ['/tmp/screenshot-1.png'],
      'slot-b': ['/tmp/screenshot-2.png'],
    })
  })
})
