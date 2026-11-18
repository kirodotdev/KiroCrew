import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createElement, type ReactNode } from 'react'
import { Provider } from 'react-redux'
import { act, renderHook } from '@testing-library/react'
import { api } from '../api/client'
import { useChatPaneDraft } from './useChatPaneDraft'
import { mergePaneSends, parsePaneSent, useChatPaneSend, type PaneSentRestore } from './useChatPaneSend'
import { useDeliveryWarningStrip } from './useDeliveryWarningStrip'
import { store } from '../store'
import { PANE_DRAFTS_KEY, __resetPaneDraftsForTests, readPaneDraft } from '../utils/chatPaneDrafts'
import { __resetDeliveryWarningsForTests } from '../utils/deliveryWarning'
import { SEND_RECOVERY_KEY, loadSendRecoveries, parkSendRecovery } from '../utils/sendRecoveryStore'

const receipt = vi.hoisted(() => ({ status: 'dispatched' as 'dispatched' | 'refused' | 'response-late' | 'unknown', body: {}, inFlight: null as Promise<void> | null }))
vi.mock('../chat-core/transport/sendTurn', async real => ({ ...await real<object>(), sendTurn: async () => { await receipt.inFlight; return receipt } }))
type PaneDraft = ReturnType<typeof useChatPaneDraft>

/** The pane's live composer with its send path and delivery strip on top, as ChatPane wires them. */
const mountPaneSend = (slot: string) => renderHook(({ at }) => {
  const draft = useChatPaneDraft(at)
  useDeliveryWarningStrip<PaneSentRestore>({ slot: at, surface: 'pane', currentSlot: () => at, restore: (to, sent, recovered) => draft.restoreIntoComposer(sent.text, sent.files, sent.pastes, to, recovered), applied: draft.isApplied, merge: mergePaneSends, ready: true, parse: parsePaneSent })
  return { ...draft, send: useChatPaneSend({ slotKey: at, dispatch: store.dispatch, draft, composerRef: { current: null }, busy: false, running: false, paneSlot: undefined, scrollToBottom: () => {}, paneAgentName: '', pendingQuestion: null, markCrewmateSent: () => {} }) }
}, { initialProps: { at: slot }, wrapper: ({ children }: { children: ReactNode }) => createElement(Provider, { store, children }) })

/** A pane mounted on a slot an earlier page load left a record for, once its strip handed the text back. */
const mountRecovered = async (sendId: string) => {
  parkSendRecovery('pane-a', { sendId, surface: 'pane', sent: { text: 'handed back' } })
  vi.spyOn(api, 'chatSlotPage').mockResolvedValueOnce({ key: 'pane-a', running: false, total: 0, next_before: 0, messages: [], has_more: false } as never)
  const pane = mountPaneSend('pane-a')
  await act(async () => { await new Promise(r => setTimeout(r, 0)) })
  expect(pane.result.current.input).toBe('handed back')
  return pane
}

/** Hand a recovered send back into `pane`'s live composer, the way the delivery strip does. */
const handBack = (pane: { current: PaneDraft }, slot: string, sendId: string): void => {
  parkSendRecovery(slot, { sendId, surface: 'pane', sent: { text: 'handed back' } })
  let held: boolean | undefined
  act(() => { held = pane.current.restoreIntoComposer('handed back', [], [], slot, { ids: [sendId], applied: false }) })
  if (held !== false) throw new Error('a live hand-back is not stored until the pane parks it')
}

const refuseWritesTo = (refused: string): void => { const write = Storage.prototype.setItem; vi.spyOn(Storage.prototype, 'setItem').mockImplementation(function (this: Storage, key: string, value: string) { if (key === refused) throw new DOMException('quota', 'QuotaExceededError'); write.call(this, key, value) }) }

describe('a recovered send handed back into a pane composer', () => {
  beforeEach(() => { sessionStorage.clear(); __resetPaneDraftsForTests(); __resetDeliveryWarningsForTests(); receipt.inFlight = null })
  afterEach(() => { vi.restoreAllMocks() })

  it.each(['dispatched', 'unknown', 'refused'] as const)('keeps its record while only in the live composer, and a %s pane send of that composer retires it unless the text comes back', async status => {
    receipt.status = status
    const { result } = mountPaneSend('pane-a')
    handBack(result, 'pane-a', 'p-live')
    expect(loadSendRecoveries('pane-a').map(r => r.sendId)).toEqual(['p-live'])
    await act(async () => { await result.current.send.doSend() })
    expect([result.current.input, loadSendRecoveries('pane-a').map(r => r.sendId), result.current.isApplied('pane-a', 'p-live')]).toEqual(status === 'refused' ? ['handed back', ['p-live'], true] : ['', [], false])
  })

  it.each(['dispatched', 'unknown', 'refused'] as const)('retires its record on a %s pane steer of that composer unless the text comes back', async status => {
    receipt.status = status
    const { result } = mountPaneSend('pane-a')
    handBack(result, 'pane-a', 'p-steer')
    await act(async () => { await result.current.send.doSteer() })
    expect([result.current.input, loadSendRecoveries('pane-a').map(r => r.sendId)]).toEqual(status === 'refused' ? ['handed back', ['p-steer']] : ['', []])
  })

  it('keeps its record when the resend of it cannot store the warning that would replace it, through the pagehide park of the emptied composer', async () => {
    // The only copy of this text is the record: it reached the composer from there and the resend cleared it. The reply never comes, so the pending bubble keeps the text off screen, and the warning's own record is refused by quota — retiring the original here would leave the words nowhere.
    receipt.status = 'response-late'
    const { result } = await mountRecovered('p-late')
    refuseWritesTo(SEND_RECOVERY_KEY)
    await act(async () => { await result.current.send.doSend() })
    vi.restoreAllMocks()
    act(() => { window.dispatchEvent(new Event('pagehide')) })
    expect(loadSendRecoveries('pane-a').map(r => r.sendId)).toEqual(['p-late'])
  })

  it('keeps its record while the resend of it is in flight, through the park of the emptied composer on a rebind', async () => {
    let land = () => {}
    receipt.inFlight = new Promise<void>(resolve => { land = resolve })
    receipt.status = 'dispatched'
    const { result, rerender } = await mountRecovered('p-flight')
    let sent: Promise<unknown> = Promise.resolve()
    act(() => { sent = result.current.send.doSend() })
    rerender({ at: 'pane-b' })
    expect(loadSendRecoveries('pane-a').map(r => r.sendId)).toEqual(['p-flight'])
    await act(async () => { land(); await sent })
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
    refuseWritesTo(PANE_DRAFTS_KEY)
    act(() => { window.dispatchEvent(new Event('pagehide')) })
    expect(loadSendRecoveries('pane-a').map(r => r.sendId)).toEqual(['p-refused'])
  })

  it('retires its record when the pane is rebound and parks the slot it leaves', () => {
    parkSendRecovery('pane-b', { sendId: 'p-other', surface: 'pane', sent: { text: 'held elsewhere' } })
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

  it('retires its record only once a later pane on the same slot takes the text the refused park left in memory and stores it', () => {
    const first = renderHook(() => useChatPaneDraft('pane-a'))
    handBack(first.result, 'pane-a', 'p-gone')
    refuseWritesTo(PANE_DRAFTS_KEY)
    first.unmount()
    vi.restoreAllMocks()
    expect(loadSendRecoveries('pane-a').map(r => r.sendId)).toEqual(['p-gone'])
    const second = renderHook(() => useChatPaneDraft('pane-a'))
    second.unmount()
    expect([readPaneDraft('pane-a').text, loadSendRecoveries('pane-a')]).toEqual(['handed back', []])
  })

  it('keeps its record through a refused park on a rebind, and retires it once the pane comes back and stores that slot', () => {
    const { result, rerender } = renderHook(({ slot }) => useChatPaneDraft(slot), { initialProps: { slot: 'pane-a' } })
    handBack(result, 'pane-a', 'p-lost')
    refuseWritesTo(PANE_DRAFTS_KEY)
    rerender({ slot: 'pane-b' })
    vi.restoreAllMocks()
    expect(loadSendRecoveries('pane-a').map(r => r.sendId)).toEqual(['p-lost'])
    rerender({ slot: 'pane-a' })
    act(() => { window.dispatchEvent(new Event('pagehide')) })
    expect([readPaneDraft('pane-a').text, loadSendRecoveries('pane-a')]).toEqual(['handed back', []])
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
    let held: boolean | undefined
    act(() => { held = result.current.restoreIntoComposer('handed back', [], [], 'pane-gone') })
    expect(held).toBe(true)
  })

  it('reports a hand-back taken by another pane showing that slot as not durable and held by no instance here', () => {
    const { result } = renderHook(() => useChatPaneDraft('pane-a'))
    renderHook(() => useChatPaneDraft('pane-b'))
    let held: boolean | undefined
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
      let held: boolean | undefined
      act(() => { held = second.result.current.restoreIntoComposer('handed back', [], [], 'pane-a', { ids: ['p-applied'], applied: true }) })
      expect(second.result.current.input).toBe(parkedText)
      expect(held).toBe(false)
    })

    it('is reported as parked and durable when the slot is off screen', () => {
      const { result } = renderHook(() => useChatPaneDraft('pane-a'))
      act(() => { result.current.restoreIntoComposer('handed back', [], [], 'pane-gone', { ids: ['p-off'], applied: false }) })
      expect(result.current.isApplied('pane-gone', 'p-off')).toBe(true)
      let held: boolean | undefined
      act(() => { held = result.current.restoreIntoComposer('handed back', [], [], 'pane-gone', { ids: ['p-off'], applied: true }) })
      expect(held).toBe(true)
      expect(readPaneDraft('pane-gone').text).toBe('handed back')
    })

    it('drops its applied ids and retires their record once the composer is cleared, so a later recovery appends normally', () => {
      const first = renderHook(() => useChatPaneDraft('pane-a'))
      handBack(first.result, 'pane-a', 'p-old')
      act(() => { first.result.current.setInput('') })
      first.unmount()
      expect(loadSendRecoveries('pane-a')).toEqual([])

      const second = renderHook(() => useChatPaneDraft('pane-a'))
      expect(second.result.current.isApplied('pane-a', 'p-old')).toBe(false)
      act(() => { second.result.current.restoreIntoComposer('second', [], [], 'pane-a', { ids: ['p-new'], applied: false }) })
      expect(second.result.current.input).toBe('second')
    })
  })
})
