/**
 * The hook against the real store: the strip's visibility, its exit, and the notice for a
 * removed send that turns out to have landed.
 */
import { describe, it, expect } from 'vitest'
import { act, renderHook } from '@testing-library/react'
import type { ReactNode } from 'react'
import { Provider } from 'react-redux'
import { store } from '../store'
import { appendSlotMessage, confirmOptimisticSend, selectSlotMessages, selectSlotRunEpoch, sseChatMessage, startLocalTurn } from '../store/chatSlice'
import { useDeliveryWarningStrip } from './useDeliveryWarningStrip'
import { removedSendsFor, warnDeliveryUnconfirmed, type DeliveryWarningSurface } from '../utils/deliveryWarning'
import { i18nT } from '../i18n/t'

const wrapper = ({ children }: { children: ReactNode }) => <Provider store={store}>{children}</Provider>

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
    restore: (at, sent) => { restored.push([at, sent]) },
    merge: (a, b) => `${a}|${b}`,
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
    for (let i = 0; i < 55; i++) {
      store.dispatch(appendSlotMessage({ slot, message: { role: 'assistant', content: `row ${i}`, cls: 'msg msg-a' } }))
    }
    store.dispatch(sseChatMessage({ slot, role: 'user', content: 'did this land?', meta: { sendId: 'h9', mid: 'm9' } }))
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
    expect(store.getState().chat.pendingTurnSlot).toBe(slot)
    act(() => { m.hook.result.current.onDismiss() })
    expect(store.getState().chat.pendingTurnSlot).toBeNull()
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
    expect(store.getState().chat.pendingTurnSlot).toBe(slot)
  })

  it('a disabled host neither shows the strip nor removes anything', () => {
    const slot = 'hook-slot-i'
    pendingRow(slot, 'h11')
    warnDeliveryUnconfirmed(slot, { sendId: 'h11', surface: 'pane', epoch: selectSlotRunEpoch(store.getState(), slot), sent: 'keep me' })
    const restored: string[] = []
    const { result } = renderHook(() => useDeliveryWarningStrip<string>({
      slot, surface: 'pane', currentSlot: () => slot, restore: (_at, sent) => { restored.push(sent) }, merge: a => a, disabled: true,
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

  it('does nothing on dismiss when the click-time slot is gone', () => {
    const slot = 'hook-slot-c'
    pendingRow(slot, 'h3')
    warnDeliveryUnconfirmed(slot, { sendId: 'h3', surface: 'page', epoch: selectSlotRunEpoch(store.getState(), slot), sent: 'kept' })
    const { result } = renderHook(() => useDeliveryWarningStrip<string>({
      slot, surface: 'page', currentSlot: () => null, restore: () => {}, merge: a => a,
    }), { wrapper })
    act(() => { result.current.onDismiss() })
    expect(userRows(slot, 'h3')).toHaveLength(1)
  })
})
