/**
 * The hook against the real store: the strip's visibility, its exit, and the notice for a
 * removed send that turns out to have landed.
 */
import { afterEach, beforeEach, describe, it, expect, vi } from 'vitest'
import { act, renderHook } from '@testing-library/react'
import { api } from '../api/client'
import type { ChatMessage } from '../types'
import type { ReactNode } from 'react'
import { Provider } from 'react-redux'
import { store } from '../store'
import { appendSlotMessage, confirmOptimisticSend, selectPendingTurn, selectSlotMessages, selectSlotRunEpoch, sseChatMessage, startLocalTurn } from '../store/chatSlice'
import { useDeliveryWarningStrip } from './useDeliveryWarningStrip'
import { __resetDeliveryWarningsForTests, createDraftOwner, draftSaved, pruneDeliveryWarnings, removedSendsFor, warnDeliveryUnconfirmed, type DeliveryWarningSurface, type DraftOwner } from '../utils/deliveryWarning'
import { loadSendRecoveries, parkSendRecovery } from '../utils/sendRecoveryStore'
import { i18nT } from '../i18n/t'
import { RECOVERY_HISTORY_MAX_PAGES } from '../utils/recoveredSendHistory'

const wrapper = ({ children }: { children: ReactNode }) => <Provider store={store}>{children}</Provider>
const textOf = (v: unknown): string | null => v && typeof v === 'object' && typeof (v as { text?: unknown }).text === 'string' ? (v as { text: string }).text : null

function pendingRow(slot: string, sendId: string) {
  store.dispatch(appendSlotMessage({
    slot,
    message: { role: 'user', content: 'did this land?', cls: 'msg msg-u', meta: { optimistic: true, sendId } },
  }))
}

function mount(slot: string, surface: DeliveryWarningSurface = 'page') {
  const restored: [string, string][] = []
  const hook = renderHook(() => useDeliveryWarningStrip<string>({
    slot,
    surface,
    currentSlot: () => slot,
    restore: (at, sent) => { restored.push([at, sent]); return true },
    merge: (a, b) => `${a}|${b}`,
    ready: true,
    parse: textOf,
  }), { wrapper })
  return { hook, restored }
}

const userRows = (slot: string, sendId: string) =>
  selectSlotMessages(store.getState(), slot).filter(m => m.role === 'user' && m.meta?.sendId === sendId)

describe('useDeliveryWarningStrip', () => {
  it('shows while the row is pending, and its exit hands the send to the surface restore and drops the row', () => {
    const slot = 'hook-slot-a'
    pendingRow(slot, 'h1')
    warnDeliveryUnconfirmed(slot, { sendId: 'h1', surface: 'page', epoch: selectSlotRunEpoch(store.getState(), slot), sent: 'see @a.txt' })
    const m = mount(slot)
    expect(m.hook.result.current.show).toBe(true)

    act(() => { m.hook.result.current.onDismiss() })

    expect(m.restored).toEqual([[slot, 'see @a.txt']])
    expect(userRows(slot, 'h1')).toHaveLength(0)
    expect(removedSendsFor(slot)).toEqual(['h1'])
    expect(m.hook.result.current.show).toBe(false)
  })

  it('restores every pending send in one call, oldest first, and skips one that settled', () => {
    const slot = 'hook-slot-f'
    for (const id of ['h6', 'h7', 'h8']) pendingRow(slot, id)
    warnDeliveryUnconfirmed(slot, { sendId: 'h6', surface: 'page', epoch: selectSlotRunEpoch(store.getState(), slot), sent: 'first' })
    warnDeliveryUnconfirmed(slot, { sendId: 'h7', surface: 'page', epoch: selectSlotRunEpoch(store.getState(), slot), sent: 'second' })
    warnDeliveryUnconfirmed(slot, { sendId: 'h8', surface: 'page', epoch: selectSlotRunEpoch(store.getState(), slot), sent: 'third' })
    const m = mount(slot)
    store.dispatch(confirmOptimisticSend({ slot, sendId: 'h7' }))

    act(() => { m.hook.result.current.onDismiss() })

    expect(m.restored).toEqual([[slot, 'first|third']])
    expect(userRows(slot, 'h6')).toHaveLength(0)
    expect(userRows(slot, 'h7')).toHaveLength(1)
    expect(userRows(slot, 'h8')).toHaveLength(0)
    expect(removedSendsFor(slot).sort()).toEqual(['h6', 'h8'])
  })

  it('treats a send confirmed by a separate row as delivered, so its stale optimistic copy is not handed back', () => {
    const slot = 'hook-slot-g'
    pendingRow(slot, 'h9')
    warnDeliveryUnconfirmed(slot, { sendId: 'h9', surface: 'page', epoch: selectSlotRunEpoch(store.getState(), slot), sent: 'late echo' })
    // A second row for the same send, confirmed: the backward scan confirms the
    // newest copy, so the older optimistic one is left standing beside it.
    store.dispatch(appendSlotMessage({ slot, message: { role: 'user', content: 'did this land?', cls: 'msg msg-u', meta: { sendId: 'h9' } } }))
    store.dispatch(confirmOptimisticSend({ slot, sendId: 'h9', mid: 'm9' }))
    expect(userRows(slot, 'h9').map(r => !!r.meta?.optimistic)).toEqual([true, false])
    const m = mount(slot)
    expect(m.hook.result.current.show).toBe(false)

    act(() => { m.hook.result.current.onDismiss() })

    expect(m.restored).toEqual([])
    expect(removedSendsFor(slot)).toEqual([])
  })

  it('stays hidden on the surface that did not raise the warning', () => {
    const slot = 'hook-slot-e'
    pendingRow(slot, 'h5')
    warnDeliveryUnconfirmed(slot, { sendId: 'h5', surface: 'page', epoch: selectSlotRunEpoch(store.getState(), slot), sent: 'main page send' })
    const m = mount(slot, 'pane')
    expect(m.hook.result.current.show).toBe(false)
    act(() => { m.hook.result.current.onDismiss() })
    expect(m.restored).toEqual([])
    expect(userRows(slot, 'h5')).toHaveLength(1)
  })

  it('ends the local turn the removed send started', () => {
    const slot = 'hook-slot-d'
    pendingRow(slot, 'h4')
    store.dispatch(startLocalTurn(slot))
    warnDeliveryUnconfirmed(slot, { sendId: 'h4', surface: 'page', epoch: selectSlotRunEpoch(store.getState(), slot), sent: 'gone' })
    const m = mount(slot)
    expect(selectPendingTurn(store.getState(), slot)).toBe(true)
    act(() => { m.hook.result.current.onDismiss() })
    expect(selectPendingTurn(store.getState(), slot)).toBe(false)
  })

  it('leaves a newer turn running when an older warned send is removed', () => {
    const slot = 'hook-slot-h'
    pendingRow(slot, 'h10')
    store.dispatch(startLocalTurn(slot))
    warnDeliveryUnconfirmed(slot, { sendId: 'h10', surface: 'page', epoch: selectSlotRunEpoch(store.getState(), slot), sent: 'older' })
    store.dispatch(startLocalTurn(slot))
    const m = mount(slot)
    act(() => { m.hook.result.current.onDismiss() })
    expect(m.restored).toEqual([[slot, 'older']])
    expect(userRows(slot, 'h10')).toHaveLength(0)
    expect(selectPendingTurn(store.getState(), slot)).toBe(true)
  })

  it('a disabled host neither shows the strip nor removes anything', () => {
    const slot = 'hook-slot-i'
    pendingRow(slot, 'h11')
    warnDeliveryUnconfirmed(slot, { sendId: 'h11', surface: 'pane', epoch: selectSlotRunEpoch(store.getState(), slot), sent: 'keep me' })
    const restored: string[] = []
    const { result } = renderHook(() => useDeliveryWarningStrip<string>({
      slot, surface: 'pane', currentSlot: () => slot, restore: (_at, sent) => { restored.push(sent) }, merge: a => a, disabled: true, ready: true, parse: textOf,
    }), { wrapper })
    expect(result.current.show).toBe(false)
    act(() => { result.current.onDismiss() })
    expect(restored).toEqual([])
    expect(userRows(slot, 'h11')).toHaveLength(1)
  })

  it('posts the delivered-after-all notice once when a removed send is accepted', () => {
    const slot = 'hook-slot-b'
    pendingRow(slot, 'h2')
    warnDeliveryUnconfirmed(slot, { sendId: 'h2', surface: 'page', epoch: selectSlotRunEpoch(store.getState(), slot), sent: 'again?' })
    const m = mount(slot)
    act(() => { m.hook.result.current.onDismiss() })

    pendingRow(slot, 'h2')
    act(() => { store.dispatch(confirmOptimisticSend({ slot, sendId: 'h2' })) })

    const notice = '\u26A0\uFE0F ' + i18nT('pages.chatPage.delivery_removed_delivered')
    const notices = () => selectSlotMessages(store.getState(), slot).filter(x => x.role === 'notice' && x.content === notice)
    expect(notices()).toHaveLength(1)
    expect(removedSendsFor(slot)).toEqual([])
    m.hook.rerender()
    expect(notices()).toHaveLength(1)
  })

  describe('a send an earlier page load was still warning about', () => {
    const recoveredNotice = '\u26A0\uFE0F ' + i18nT('pages.chatPage.delivery_recovered_after_reload')
    const notices = (slot: string, text: string) => selectSlotMessages(store.getState(), slot).filter(x => x.role === 'notice' && x.content === text)
    const serverHas = (pages: Array<{ messages: ChatMessage[]; has_more: boolean; next_before?: number }>) => {
      const spy = vi.spyOn(api, 'chatSlotDetail')
      for (const p of pages) spy.mockResolvedValueOnce({ key: 'k', running: false, total: 0, next_before: 0, ...p } as never)
      return spy
    }
    const NOW = '2026-01-01T12:00:00.000Z'
    const userRow = (sendId: string): ChatMessage => ({ role: 'user', content: 'sent', cls: 'msg msg-u', ts: NOW, meta: { sendId } })
    const mountReady = (slot: string, ready: boolean, surface: DeliveryWarningSurface = 'page', held: boolean | DraftOwner = true, applied?: (at: string, sendId: string) => boolean) => {
      const restored: [string, string][] = []
      const hook = renderHook(({ isReady }: { isReady: boolean }) => useDeliveryWarningStrip<string>({
        slot, surface, currentSlot: () => slot, restore: (at, sent) => { restored.push([at, sent]); return held },
        merge: (a, b) => `${a}|${b}`, ready: isReady, parse: textOf, applied,
      }), { wrapper, initialProps: { isReady: ready } })
      return { hook, restored }
    }
    const settled = () => act(async () => { await new Promise(r => setTimeout(r, 0)) })

    beforeEach(() => {
      vi.useFakeTimers({ toFake: ['Date'] })
      vi.setSystemTime(new Date(NOW))
    })
    afterEach(() => { vi.restoreAllMocks(); vi.useRealTimers() })

    it('comes back into the composer with a notice once the transcript loads, and only once', async () => {
      const slot = 'reload-slot-a'
      serverHas([{ messages: [], has_more: false }])
      parkSendRecovery(slot, { sendId: 'r1', surface: 'page', sent: { text: 'never confirmed' } })
      parkSendRecovery(slot, { sendId: 'r2', surface: 'page', sent: { text: 'nor this' } })
      const m = mountReady(slot, false)
      await settled()
      expect(m.restored, 'not before the server rows can be checked').toEqual([])

      m.hook.rerender({ isReady: true })
      await settled()
      expect(m.restored).toEqual([[slot, 'never confirmed|nor this']])
      expect(notices(slot, recoveredNotice)).toHaveLength(1)
      expect(loadSendRecoveries(slot), 'the composer\u2019s draft holds the text now').toEqual([])
      mountReady(slot, true)
      await settled()
      expect(notices(slot, recoveredNotice), 'a second mount finds nothing left').toHaveLength(1)
    })

    it('keeps the record while the handed-back text is only in composer memory, until that slot\u2019s draft is saved', async () => {
      const slot = 'reload-slot-await'
      const owner = createDraftOwner()
      serverHas([{ messages: [], has_more: false }])
      parkSendRecovery(slot, { sendId: 'r-await', surface: 'pane', sent: { text: 'not on disk yet' } })
      const m = mountReady(slot, true, 'pane', owner)
      await settled()
      expect(m.restored).toEqual([[slot, 'not on disk yet']])
      expect(loadSendRecoveries(slot).map(r => r.sendId)).toEqual(['r-await'])

      draftSaved('page', owner, s => s === slot)
      draftSaved('pane', owner, s => s === 'another-slot')
      expect(loadSendRecoveries(slot).map(r => r.sendId)).toEqual(['r-await'])
      draftSaved('pane', owner, s => s === slot)
      expect(loadSendRecoveries(slot)).toEqual([])
    })

    it('keeps the record without awaiting any instance when the text went to a composer that holds no token here', async () => {
      const slot = 'reload-slot-no-owner'
      serverHas([{ messages: [], has_more: false }])
      parkSendRecovery(slot, { sendId: 'r-none', surface: 'pane', sent: { text: 'another pane took it' } })
      const m = mountReady(slot, true, 'pane', false)
      await settled()
      expect(m.restored).toEqual([[slot, 'another pane took it']])
      draftSaved('pane', createDraftOwner(), s => s === slot)
      expect(loadSendRecoveries(slot).map(r => r.sendId)).toEqual(['r-none'])
    })

    it('is retired without a word when the loaded transcript already has it', async () => {
      const slot = 'reload-slot-b'
      serverHas([{ messages: [], has_more: false }])
      parkSendRecovery(slot, { sendId: 'r3', surface: 'page', sent: { text: 'it did arrive' } })
      store.dispatch(sseChatMessage({ slot, role: 'user', content: 'it did arrive', meta: { sendId: 'r3', mid: 'm3' } }))

      const m = mountReady(slot, true)
      await settled()
      expect(m.restored).toEqual([])
      expect(notices(slot, recoveredNotice)).toHaveLength(0)
      expect(loadSendRecoveries(slot)).toEqual([])
    })

    it('is retired without a word when the server history holds it beyond the loaded window', async () => {
      const slot = 'reload-slot-h'
      const detail = serverHas([
        { messages: [{ role: 'assistant', content: 'later output', cls: 'msg msg-a', ts: NOW }], has_more: true, next_before: 40 },
        { messages: [userRow('r9')], has_more: true, next_before: 20 },
      ])
      parkSendRecovery(slot, { sendId: 'r9', surface: 'page', sent: { text: 'already delivered' } })

      const m = mountReady(slot, true)
      await settled()
      expect(detail.mock.calls.map(c => c[2]), 'read back one page past the loaded one').toEqual([undefined, 40])
      expect(m.restored).toEqual([])
      expect(notices(slot, recoveredNotice)).toHaveLength(0)
      expect(loadSendRecoveries(slot)).toEqual([])
    })

    it('is retired without a word when the browser clock runs ahead of the gateway and the row is pages back', async () => {
      const slot = 'reload-slot-skew'
      const old = (n: number): ChatMessage => ({ role: 'assistant', content: 'later', cls: 'msg', ts: '2026-01-01T11:00:00.000Z', meta: { mid: `x${n}` } })
      serverHas([
        { messages: [old(1)], has_more: true, next_before: 50 },
        { messages: [{ ...userRow('r-skew'), ts: '2026-01-01T10:59:00.000Z' }], has_more: false },
      ])
      parkSendRecovery(slot, { sendId: 'r-skew', surface: 'page', sent: { text: 'already delivered' } })
      const m = mountReady(slot, true)
      await settled()

      expect(m.restored).toEqual([])
      expect(notices(slot, recoveredNotice)).toHaveLength(0)
      expect(loadSendRecoveries(slot)).toEqual([])
    })

    it('hands nothing back and keeps the record when the page cap is reached first', async () => {
      const slot = 'reload-slot-cap'
      const detail = serverHas(Array.from({ length: RECOVERY_HISTORY_MAX_PAGES }, (_, i) =>
        ({ messages: [], has_more: true, next_before: 1000 - i })))
      parkSendRecovery(slot, { sendId: 'r-cap', surface: 'page', sent: { text: 'unproven' } })
      const m = mountReady(slot, true)
      await settled()

      expect(detail).toHaveBeenCalledTimes(RECOVERY_HISTORY_MAX_PAGES)
      expect(m.restored).toEqual([])
      expect(loadSendRecoveries(slot).map(r => r.sendId)).toEqual(['r-cap'])
    })

    it('is retired without a word when a queue drain merged it into another send\u2019s row', async () => {
      const slot = 'reload-slot-m'
      const merged: ChatMessage = { role: 'user', content: 'a\n\nb', cls: 'msg msg-u', ts: NOW, meta: { sendId: 'r21', sendIds: ['r20', 'r21'] } }
      serverHas([{ messages: [merged], has_more: false }])
      parkSendRecovery(slot, { sendId: 'r20', surface: 'page', sent: { text: 'first of two' } })

      const m = mountReady(slot, true)
      await settled()
      expect(m.restored).toEqual([])
      expect(loadSendRecoveries(slot)).toEqual([])
    })

    it('is retired without a word when the loaded transcript holds it inside a merged row', async () => {
      const slot = 'reload-slot-u'
      serverHas([{ messages: [], has_more: false }])
      parkSendRecovery(slot, { sendId: 'r30', surface: 'page', sent: { text: 'folded into another row' } })
      store.dispatch(sseChatMessage({ slot, role: 'user', content: 'a\n\nb', meta: { sendId: 'r31', sendIds: ['r30', 'r31'], mid: 'm31' } }))

      const m = mountReady(slot, true)
      await settled()
      expect(m.restored).toEqual([])
      expect(loadSendRecoveries(slot)).toEqual([])
    })

    it('hands nothing back and keeps the record when the history cannot be read', async () => {
      const slot = 'reload-slot-i'
      vi.spyOn(api, 'chatSlotDetail').mockRejectedValue(new Error('offline'))
      parkSendRecovery(slot, { sendId: 'r10', surface: 'page', sent: { text: 'undecided' } })

      const m = mountReady(slot, true)
      await settled()
      expect(m.restored).toEqual([])
      expect(loadSendRecoveries(slot).map(r => r.sendId)).toEqual(['r10'])
    })

    it('is not handed back again on a later load once its text came back', async () => {
      const slot = 'reload-slot-j'
      serverHas([{ messages: [], has_more: false }, { messages: [], has_more: false }])
      parkSendRecovery(slot, { sendId: 'r11', surface: 'pane', sent: { text: 'only in the composer' } })
      const first = mountReady(slot, true, 'pane')
      await settled()
      expect(first.restored).toEqual([[slot, 'only in the composer']])

      __resetDeliveryWarningsForTests()
      const again = mountReady(slot, true, 'pane')
      await settled()
      expect(again.restored, 'an edited draft is not appended to a second time').toEqual([])
      expect(notices(slot, recoveredNotice)).toHaveLength(1)
    })

    it('is retired without a word when its echo lands, or its draft is sent, while the history is read', async () => {
      const slot = 'reload-slot-t4'
      parkSendRecovery(slot, { sendId: 'r28d', surface: 'pane', sent: { text: 'echoed mid-check' } })
      parkSendRecovery(slot, { sendId: 'r28e', surface: 'pane', sent: { text: 'sent mid-check' } })
      let answer: (v: unknown) => void = () => {}
      vi.spyOn(api, 'chatSlotDetail').mockImplementationOnce(() => new Promise(r => { answer = r }) as never)
      const m = mountReady(slot, true, 'pane', true, vi.fn().mockReturnValueOnce(false).mockReturnValueOnce(true).mockReturnValue(false))
      act(() => { store.dispatch(sseChatMessage({ slot, role: 'user', content: 'echoed mid-check', meta: { sendId: 'r28d', mid: 'm28d' } })) })
      await act(async () => { answer({ key: slot, running: false, total: 0, next_before: 0, messages: [], has_more: false }) })
      await settled()

      expect(m.restored).toEqual([])
      expect(notices(slot, '\u26A0\uFE0F ' + i18nT('pages.chatPage.delivery_recovered_delivered'))).toHaveLength(0)
      expect(loadSendRecoveries(slot)).toEqual([])
    })

    it('retires a delivered send silently when it was never handed back, and says so when a stored draft holds its text', async () => {
      const slot = 'reload-slot-q'
      const delivered = () => notices(slot, '\u26A0\uFE0F ' + i18nT('pages.chatPage.delivery_recovered_delivered'))
      serverHas([{ messages: [userRow('r25')], has_more: false }, { messages: [userRow('r26')], has_more: false }])
      parkSendRecovery(slot, { sendId: 'r25', surface: 'page', sent: { text: 'never restored' } })
      mountReady(slot, true, 'page')
      await settled()
      expect(delivered()).toHaveLength(0)
      expect(loadSendRecoveries(slot)).toEqual([])
      parkSendRecovery(slot, { sendId: 'r26', surface: 'page', sent: { text: 'restored, then delivered' } })
      mountReady(slot, true, 'page', true, (_, id) => id === 'r26')
      await settled()
      expect(delivered()).toHaveLength(1)
      expect(loadSendRecoveries(slot)).toEqual([])
    })

    it('says it was delivered after all when its receipt lands after the text came back', async () => {
      const slot = 'reload-slot-c'
      serverHas([{ messages: [], has_more: false }])
      parkSendRecovery(slot, { sendId: 'r4', surface: 'page', sent: { text: 'late' } })
      const m = mountReady(slot, true)
      await settled()
      expect(m.restored).toEqual([[slot, 'late']])

      act(() => {
        store.dispatch(sseChatMessage({ slot, role: 'user', content: 'late', meta: { sendId: 'r4', mid: 'm4' } }))
      })

      const delivered = '\u26A0\uFE0F ' + i18nT('pages.chatPage.delivery_recovered_delivered')
      expect(notices(slot, delivered)).toHaveLength(1)
      expect(notices(slot, '\u26A0\uFE0F ' + i18nT('pages.chatPage.delivery_removed_delivered'))).toHaveLength(0)
    })

    it('is left for the surface that raised it', async () => {
      const slot = 'reload-slot-d'
      serverHas([{ messages: [], has_more: false }])
      parkSendRecovery(slot, { sendId: 'r5', surface: 'pane', sent: { text: 'pane send' } })
      const page = mountReady(slot, true, 'page')
      await settled()
      expect(page.restored).toEqual([])
      const pane = mountReady(slot, true, 'pane')
      await settled()
      expect(pane.restored).toEqual([[slot, 'pane send']])
    })

    it('is not handed back by a disabled host, which keeps it for one that can say so', async () => {
      const slot = 'reload-slot-e'
      parkSendRecovery(slot, { sendId: 'r6', surface: 'pane', sent: { text: 'crewmate pane' } })
      const restored: string[] = []
      renderHook(() => useDeliveryWarningStrip<string>({
        slot, surface: 'pane', currentSlot: () => slot, restore: (_at, sent) => { restored.push(sent) },
        merge: a => a, disabled: true, ready: true, parse: textOf,
      }), { wrapper })
      await settled()
      expect(restored).toEqual([])
      expect(loadSendRecoveries(slot)).toHaveLength(1)
    })

    it('is persisted when a warning is raised and retired when it is removed', () => {
      const slot = 'reload-slot-f'
      pendingRow(slot, 'r7')
      warnDeliveryUnconfirmed(slot, { sendId: 'r7', surface: 'page', epoch: selectSlotRunEpoch(store.getState(), slot), sent: { text: 'in doubt' } })
      expect(loadSendRecoveries(slot).map(r => r.sendId)).toEqual(['r7'])
      const m = mountReady(slot, true)
      expect(m.restored, 'a live warning is the strip\u2019s, not a recovery').toEqual([])

      act(() => { m.hook.result.current.onDismiss() })
      expect(loadSendRecoveries(slot)).toEqual([])
    })

    it('is retired when its warning is pruned', () => {
      const slot = 'reload-slot-g'
      pendingRow(slot, 'r8')
      warnDeliveryUnconfirmed(slot, { sendId: 'r8', surface: 'page', epoch: selectSlotRunEpoch(store.getState(), slot), sent: { text: 'confirmed soon' } })
      expect(loadSendRecoveries(slot)).toHaveLength(1)
      pruneDeliveryWarnings(slot, () => false)
      expect(loadSendRecoveries(slot)).toEqual([])
    })
  })

  it('does nothing on dismiss when the click-time slot is gone', () => {
    const slot = 'hook-slot-c'
    pendingRow(slot, 'h3')
    warnDeliveryUnconfirmed(slot, { sendId: 'h3', surface: 'page', epoch: selectSlotRunEpoch(store.getState(), slot), sent: 'kept' })
    const { result } = renderHook(() => useDeliveryWarningStrip<string>({
      slot, surface: 'page', currentSlot: () => null, restore: () => {}, merge: a => a, ready: true, parse: textOf,
    }), { wrapper })
    act(() => { result.current.onDismiss() })
    expect(userRows(slot, 'h3')).toHaveLength(1)
  })
})
