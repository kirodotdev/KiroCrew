/** Every record is the only copy of a prompt the composer already cleared, and it belongs to the
 *  tab that saved it: kept across a reload, invisible to any other tab. */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { DRAFT_TTL_MS } from './draftConstants'
import {
  __resetSendRecoveryLoadForTests, forgetRestoredSendRecoveries, forgetSendRecovery, loadSendRecoveries, markSendRecoveryRestored,
  parkSendRecovery, SEND_RECOVERY_KEY,
} from './sendRecoveryStore'

const texts = (slot: string): string[] => loadSendRecoveries(slot).map(r => (r.sent as { text: string }).text)
const park = (slot: string, sendId: string, text: string, surface: 'page' | 'pane' = 'pane') =>
  parkSendRecovery(slot, { sendId, surface, sent: { text, files: [] } })

describe('the send recovery store', () => {
  beforeEach(() => {
    localStorage.clear(); sessionStorage.clear()
    vi.useFakeTimers({ toFake: ['Date'] })
    vi.setSystemTime(new Date('2026-01-01T00:00:00Z'))
  })
  afterEach(() => { vi.restoreAllMocks(); vi.useRealTimers() })

  it('keeps one record per send, so a second send on the slot cannot overwrite the first', () => {
    park('slot-two', 's1', 'first')
    vi.setSystemTime(new Date('2026-01-01T00:00:01Z'))
    park('slot-two', 's2', 'second')
    expect(texts('slot-two')).toEqual(['first', 'second'])
  })

  it('forgets only the send it names', () => {
    park('slot-forget', 's1', 'first')
    park('slot-forget', 's2', 'second')
    forgetSendRecovery('slot-forget', 's1')
    expect(texts('slot-forget')).toEqual(['second'])
  })

  it('does not read another slot whose key shares a prefix', () => {
    park('slot-a', 's1', 'mine')
    park('slot-ab', 's2', 'not mine')
    expect(texts('slot-a')).toEqual(['mine'])
  })

  it('lives in this tab\u2019s sessionStorage only, so a sibling tab is never handed it', () => {
    park('slot-tab', 's1', 'only here')
    expect(localStorage.length, 'nothing in storage another tab shares').toBe(0)
    sessionStorage.clear()
    expect(texts('slot-tab')).toEqual([])
  })

  it('drops a value not shaped like a record instead of handing it back', () => {
    sessionStorage.setItem(SEND_RECOVERY_KEY, JSON.stringify({ 'slot-bad|s1': { sendId: 's1', surface: 'nowhere', sent: {}, ts: Date.now() } }))
    expect(loadSendRecoveries('slot-bad')).toEqual([])
    sessionStorage.setItem(SEND_RECOVERY_KEY, 'not json')
    expect(loadSendRecoveries('slot-bad')).toEqual([])
  })

  it('reports false when the tab refuses the write, and keeps the records already saved', () => {
    park('slot-full', 's1', 'already saved')
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new DOMException('full', 'QuotaExceededError') })
    expect(park('slot-full', 's2', 'refused')).toBe(false)
    vi.restoreAllMocks()
    expect(texts('slot-full')).toEqual(['already saved'])
  })

  it('expires a record past the drafts TTL', () => {
    park('slot-old', 's1', 'stale')
    vi.setSystemTime(new Date(Date.now() + DRAFT_TTL_MS + 1))
    expect(loadSendRecoveries('slot-old')).toEqual([])
  })

  it('drops the copy a window opened by another one inherits, and keeps an opened window\u2019s own on its reload', () => {
    park('slot-popout', 's1', 'still live in the opener')
    vi.stubGlobal('opener', {})
    try {
      __resetSendRecoveryLoadForTests()
      expect(texts('slot-popout'), 'a fresh pop-out does not take the opener\u2019s record').toEqual([])
      park('slot-popout', 's2', 'parked by the pop-out')
      __resetSendRecoveryLoadForTests()
      vi.spyOn(performance, 'getEntriesByType').mockImplementation(((t: string) =>
        t === 'navigation' ? [{ type: 'reload' }] : []) as typeof performance.getEntriesByType)
      expect(texts('slot-popout'), 'its own record survives its reload').toEqual(['parked by the pop-out'])
    } finally {
      vi.unstubAllGlobals()
      __resetSendRecoveryLoadForTests()
    }
  })

  it('marks a record handed back, and forgets only the handed-back ones of that surface', () => {
    park('slot-back', 's1', 'handed back')
    park('slot-back', 's2', 'still waiting')
    park('slot-back', 's3', 'page send', 'page')
    markSendRecoveryRestored('slot-back', 's1')
    markSendRecoveryRestored('slot-back', 's3')
    expect(loadSendRecoveries('slot-back').map(r => [r.sendId, r.restored])).toEqual([['s1', true], ['s2', false], ['s3', true]])
    forgetRestoredSendRecoveries('slot-back', 'pane')
    expect(loadSendRecoveries('slot-back').map(r => r.sendId)).toEqual(['s2', 's3'])
  })
})
