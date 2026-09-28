/**
 * A retired approval asks for nothing, so no unread surface may count it,
 * whatever its ack flag says. `retireApprovalRow` marks the row read through
 * the ordinary ack, and when that POST is refused `ackNotification.rejected`
 * puts the flag back to unread. The row's own dot already ignores the flag, so
 * a surface reading `acked` alone would light a badge (or announce a banner)
 * that no highlighted row explains. Every unread surface reads
 * `selectUnreadNotes` from the slice instead.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, act, waitFor } from '@testing-library/react'
import { createElement } from 'react'
import { Provider } from 'react-redux'
import { createTestStore } from './helpers'
import { useNativeNotification } from '../hooks/useNativeNotification'
import { addNotification, retireApprovalRow, selectUnreadNotes } from '../store/notificationsSlice'
import { selectAllSurfacesAttention, selectSurfaceBadgeCount } from '../surfaces/registry'
import '../surfaces/builtins'
import type { RootState } from '../store'
import type { Notification as AppNotification } from '../types'

const mockAck = vi.fn()
vi.mock('../api/client', () => ({
  api: {
    ackNotification: (...args: unknown[]) => mockAck(...args),
  },
}))

class FakeNotification {
  static permission = 'granted'
  static requestPermission = vi.fn()
  static instances: string[] = []
  constructor(title: string) { FakeNotification.instances.push(title) }
}

const approval: AppNotification = {
  kind: 'approval', ts: '1.0', title: 'Tool approval', body: 'Bash', approval_id: 'ap-1', acked: false,
}

/** Retire the approval with its ack refused, and wait for the rollback. */
async function retireWithRefusedAck(store: ReturnType<typeof createTestStore>) {
  mockAck.mockRejectedValueOnce(new Error('network down'))
  act(() => { store.dispatch(retireApprovalRow('1.0', 'gone')) })
  // The rollback has run: the flag is back to unread.
  await waitFor(() => { expect(store.getState().notifications.items[0].acked).toBe(false) })
}

describe('a retired approval stays out of every unread count when its ack is refused', () => {
  beforeEach(() => {
    mockAck.mockReset()
    mockAck.mockResolvedValue({})
    FakeNotification.instances = []
    vi.stubGlobal('Notification', FakeNotification)
    Object.defineProperty(document, 'hidden', { configurable: true, get: () => true })
  })
  afterEach(() => {
    vi.unstubAllGlobals()
    delete (document as { hidden?: boolean }).hidden
  })

  it('the shared selector and the tab-title / surface badge leave it out', async () => {
    const store = createTestStore()
    act(() => { store.dispatch(addNotification(approval)) })
    const state = () => store.getState() as unknown as RootState
    expect(selectAllSurfacesAttention(state())).toBe(1)
    await retireWithRefusedAck(store)
    expect(selectUnreadNotes(state())).toEqual([])
    expect(selectAllSurfacesAttention(state())).toBe(0)
    expect(selectSurfaceBadgeCount('notifications')(state())).toBe(0)
  })

  it('the native banner is not fired a second time for it', async () => {
    const store = createTestStore()
    renderHook(() => useNativeNotification('Kiro Crew', '/avatar.png'), {
      wrapper: ({ children }) => createElement(Provider, { store }, children),
    })
    act(() => { store.dispatch(addNotification(approval)) })
    expect(FakeNotification.instances).toEqual(['Tool approval'])
    await retireWithRefusedAck(store)
    expect(FakeNotification.instances).toEqual(['Tool approval'])
  })
})
