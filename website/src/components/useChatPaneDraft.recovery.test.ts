import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, renderHook } from '@testing-library/react'
import { useChatPaneDraft } from './useChatPaneDraft'
import { useChatPaneSend } from './useChatPaneSend'
import { store } from '../store'
import { PANE_DRAFTS_KEY, __resetPaneDraftsForTests, readPaneDraft } from '../utils/chatPaneDrafts'
import { __resetDeliveryWarningsForTests, awaitDraftSave, createDraftOwner, type DraftOwner } from '../utils/deliveryWarning'
import { loadSendRecoveries, parkSendRecovery } from '../utils/sendRecoveryStore'

const receipt = vi.hoisted(() => ({ status: 'dispatched' as 'dispatched' | 'refused', body: {} }))
vi.mock('../chat-core/transport/sendTurn', async real => ({ ...await real<object>(), sendTurn: () => Promise.resolve(receipt) }))
type PaneDraft = ReturnType<typeof useChatPaneDraft>

/** Hand a recovered send back into `pane`'s live composer, the way the delivery strip does. */
const handBack = (pane: { current: PaneDraft }, slot: string, sendId: string): DraftOwner => {
  parkSendRecovery(slot, { sendId, surface: 'pane', sent: { text: 'handed back' } })
  let held: boolean | DraftOwner = false
  act(() => { held = pane.current.restoreIntoComposer('handed back', [], [], slot, { ids: [sendId], applied: false }) })
  if (typeof held !== 'object') throw new Error('a live hand-back must return this instance\u2019s owner')
  awaitDraftSave(slot, 'pane', [sendId], held)
  return held
}

const refusePaneDraftWrites = () => {
  const write = Storage.prototype.setItem
  vi.spyOn(Storage.prototype, 'setItem').mockImplementation(function (this: Storage, key: string, value: string) {
    if (key === PANE_DRAFTS_KEY) throw new DOMException('quota', 'QuotaExceededError')
    write.call(this, key, value)
  })
}

describe('a recovered send handed back into a pane composer', () => {
  beforeEach(() => {
    sessionStorage.clear()
    __resetPaneDraftsForTests()
    __resetDeliveryWarningsForTests()
  })
  afterEach(() => { vi.restoreAllMocks() })

  it.each(['dispatched', 'refused'] as const)('keeps its record while only in the live composer, and a %s pane send of that composer retires it unless the text comes back', async status => {
    receipt.status = status
    const { result } = renderHook(() => { const draft = useChatPaneDraft('pane-a'); return { ...draft, send: useChatPaneSend({ slotKey: 'pane-a', dispatch: store.dispatch, draft, composerRef: { current: null }, busy: false, running: false, paneSlot: undefined, scrollToBottom: () => {}, paneAgentName: '', pendingQuestion: null, markCrewmateSent: () => {} }) } })
    handBack(result, 'pane-a', 'p-live')
    expect(loadSendRecoveries('pane-a').map(r => r.sendId)).toEqual(['p-live'])
    await act(async () => { await result.current.send.doSend() })
    expect([result.current.input, loadSendRecoveries('pane-a').map(r => r.sendId), result.current.isApplied('pane-a', 'p-live')]).toEqual(status === 'refused' ? ['handed back', ['p-live'], true] : ['', [], false])
  })

  it('retires its record once a pagehide park puts the composer in storage', () => {
    const { result } = renderHook(() => useChatPaneDraft('pane-a'))
    handBack(result, 'pane-a', 'p-hide')
    act(() => { window.dispatchEvent(new Event('pagehide')) })
    expect(loadSendRecoveries('pane-a')).toEqual([])
  })

  it('keeps its record when storage refuses the park, so a crash still has a copy', () => {
    const { result } = renderHook(() => useChatPaneDraft('pane-a'))
    handBack(result, 'pane-a', 'p-refused')
    refusePaneDraftWrites()
    act(() => { window.dispatchEvent(new Event('pagehide')) })
    expect(loadSendRecoveries('pane-a').map(r => r.sendId)).toEqual(['p-refused'])
  })

  it('retires its record when the pane is rebound and parks the slot it leaves', () => {
    parkSendRecovery('pane-b', { sendId: 'p-other', surface: 'pane', sent: { text: 'held elsewhere' } })
    awaitDraftSave('pane-b', 'pane', ['p-other'], createDraftOwner())
    const { result, rerender } = renderHook(({ slot }) => useChatPaneDraft(slot), { initialProps: { slot: 'pane-a' } })
    handBack(result, 'pane-a', 'p-rebind')
    rerender({ slot: 'pane-b' })
    expect(loadSendRecoveries('pane-a')).toEqual([])
    expect(loadSendRecoveries('pane-b').map(r => r.sendId)).toEqual(['p-other'])
  })

  it('retires its record when the pane unmounts and parks its composer', () => {
    const { result, unmount } = renderHook(() => useChatPaneDraft('pane-a'))
    handBack(result, 'pane-a', 'p-unmount')
    unmount()
    expect(loadSendRecoveries('pane-a')).toEqual([])
  })

  it('keeps its record when a later pane on the same slot parks after the pane holding the text unmounted on a refused park', () => {
    const first = renderHook(() => useChatPaneDraft('pane-a'))
    handBack(first.result, 'pane-a', 'p-gone')
    refusePaneDraftWrites()
    first.unmount()
    vi.restoreAllMocks()
    const second = renderHook(() => useChatPaneDraft('pane-a'))
    second.unmount()
    expect(loadSendRecoveries('pane-a').map(r => r.sendId)).toEqual(['p-gone'])
  })

  it('keeps its record when the pane holding the text is rebound on a refused park and comes back to park that slot', () => {
    const { result, rerender } = renderHook(({ slot }) => useChatPaneDraft(slot), { initialProps: { slot: 'pane-a' } })
    handBack(result, 'pane-a', 'p-lost')
    refusePaneDraftWrites()
    rerender({ slot: 'pane-b' })
    vi.restoreAllMocks()
    rerender({ slot: 'pane-a' })
    act(() => { window.dispatchEvent(new Event('pagehide')) })
    expect(loadSendRecoveries('pane-a').map(r => r.sendId)).toEqual(['p-lost'])
  })

  it('keeps its record when another pane on the same slot parks while the pane holding the text is still mounted', () => {
    const holder = renderHook(() => useChatPaneDraft('pane-a'))
    handBack(holder.result, 'pane-a', 'p-held')
    const other = renderHook(() => useChatPaneDraft('pane-a'))
    other.unmount()
    expect(loadSendRecoveries('pane-a').map(r => r.sendId)).toEqual(['p-held'])
  })

  it('reports a hand-back parked for a slot no pane shows as durable', () => {
    const { result } = renderHook(() => useChatPaneDraft('pane-a'))
    let held: boolean | DraftOwner | undefined
    act(() => { held = result.current.restoreIntoComposer('handed back', [], [], 'pane-gone') })
    expect(held).toBe(true)
  })

  it('reports a hand-back taken by another pane showing that slot as not durable and held by no instance here', () => {
    const { result } = renderHook(() => useChatPaneDraft('pane-a'))
    renderHook(() => useChatPaneDraft('pane-b'))
    let held: boolean | DraftOwner | undefined
    act(() => { held = result.current.restoreIntoComposer('handed back', [], [], 'pane-b') })
    expect(held).toBe(false)
  })

  describe('a send whose text already reached the parked draft', () => {
    it('is not appended again after the pane remounts and finds the text parked with its applied id', () => {
      const first = renderHook(() => useChatPaneDraft('pane-a'))
      act(() => { first.result.current.setInput('typed since') })
      act(() => { first.result.current.restoreIntoComposer('handed back', [], [], 'pane-a', { ids: ['p-applied'], applied: false }) })
      first.unmount()

      const second = renderHook(() => useChatPaneDraft('pane-a'))
      const parkedText = 'typed since\n\nhanded back'
      expect(second.result.current.input).toBe(parkedText)
      expect(second.result.current.isApplied('pane-a', 'p-applied')).toBe(true)
      let held: boolean | DraftOwner | undefined
      act(() => { held = second.result.current.restoreIntoComposer('handed back', [], [], 'pane-a', { ids: ['p-applied'], applied: true }) })
      expect(second.result.current.input).toBe(parkedText)
      expect(typeof held).toBe('object')
    })

    it('is reported as parked and durable when the slot is off screen', () => {
      const { result } = renderHook(() => useChatPaneDraft('pane-a'))
      act(() => { result.current.restoreIntoComposer('handed back', [], [], 'pane-gone', { ids: ['p-off'], applied: false }) })
      expect(result.current.isApplied('pane-gone', 'p-off')).toBe(true)
      let held: boolean | DraftOwner | undefined
      act(() => { held = result.current.restoreIntoComposer('handed back', [], [], 'pane-gone', { ids: ['p-off'], applied: true }) })
      expect(held).toBe(true)
      expect(readPaneDraft('pane-gone').text).toBe('handed back')
    })

    it('drops its applied ids once the composer is cleared, so a later recovery appends normally', () => {
      const first = renderHook(() => useChatPaneDraft('pane-a'))
      act(() => { first.result.current.restoreIntoComposer('first', [], [], 'pane-a', { ids: ['p-old'], applied: false }) })
      act(() => { first.result.current.setInput('') })
      first.unmount()

      const second = renderHook(() => useChatPaneDraft('pane-a'))
      expect(second.result.current.isApplied('pane-a', 'p-old')).toBe(false)
      act(() => { second.result.current.restoreIntoComposer('second', [], [], 'pane-a', { ids: ['p-new'], applied: false }) })
      expect(second.result.current.input).toBe('second')
    })
  })
})
