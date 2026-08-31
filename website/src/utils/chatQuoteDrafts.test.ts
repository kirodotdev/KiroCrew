import { beforeEach, describe, expect, it } from 'vitest'
import {
  QUOTE_DRAFTS_KEY,
  __resetQuoteDraftsForTests,
  loadQuoteDrafts,
  mergeQuoteRefs,
  saveQuoteDrafts,
  setQuoteDraft,
} from './chatQuoteDrafts'
import type { QuoteRef } from './quoteRefs'

const quote = (key: string, text: string): QuoteRef => ({
  key,
  role: 'Assistant',
  time: '10:22',
  text,
  mid: `mid-${key}`,
  ts: `ts-${key}`,
})

beforeEach(() => {
  sessionStorage.clear()
  __resetQuoteDraftsForTests()
})

describe('chatQuoteDrafts', () => {
  it('stores isolated quote snapshots per slot and deletes empty drafts', () => {
    const drafts: Record<string, QuoteRef[]> = {}
    const original = quote('a', 'slot A context')
    setQuoteDraft(drafts, 'slot-a', [original])
    original.text = 'mutated after staging'
    setQuoteDraft(drafts, 'slot-b', [quote('b', 'slot B context')])
    saveQuoteDrafts(drafts)

    expect(loadQuoteDrafts()).toEqual({
      'slot-a': [quote('a', 'slot A context')],
      'slot-b': [quote('b', 'slot B context')],
    })
    setQuoteDraft(drafts, 'slot-a', [])
    saveQuoteDrafts(drafts)
    expect(JSON.parse(sessionStorage.getItem(QUOTE_DRAFTS_KEY) || '{}')['slot-a']).toBeUndefined()
  })

  it('drops malformed records without weakening valid source metadata', () => {
    sessionStorage.setItem(QUOTE_DRAFTS_KEY, JSON.stringify({
      slot: [
        null,
        { key: '', role: 'Assistant', time: '', text: 'missing key' },
        { key: 'empty', role: 'Assistant', time: '', text: '   ' },
        { key: 'valid', role: 'Assistant', time: '10:22', text: 'keep', mid: 'm1', ts: 't1', code: true },
        { key: 'valid', role: 'Assistant', time: 'later', text: 'duplicate' },
      ],
    }))

    expect(loadQuoteDrafts()).toEqual({
      slot: [{ key: 'valid', role: 'Assistant', time: '10:22', text: 'keep', mid: 'm1', ts: 't1', code: true }],
    })
  })

  it('keeps newer staged quotes first when restoring a failed request', () => {
    expect(mergeQuoteRefs(
      [quote('new', 'newer'), quote('same', 'new version')],
      [quote('old', 'failed request'), quote('same', 'old version')],
    ).map(item => item.text)).toEqual(['newer', 'new version', 'failed request'])
  })
})
