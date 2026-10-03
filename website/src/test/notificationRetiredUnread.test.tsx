/**
 * An approval that retires while nobody is looking stays unread on every
 * surface. An expiry while the reader was away (or one a fresh tab's reconcile
 * finds at boot) means the job was denied, and clearing the bell, dock and
 * tab-title badges on that alone would hide it. The row keeps a quiet dot for
 * as long as it counts, and it is read the way any note is: opened, marked
 * read, or dismissed. Every unread surface reads `selectUnreadNotes`, so they
 * agree.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { createElement } from 'react'
import { Provider } from 'react-redux'
import { createTestStore } from './helpers'
import { useNativeNotification } from '../hooks/useNativeNotification'
import { ackNotificationByTs, addNotification, retireApprovalRow, selectUnreadNotes } from '../store/notificationsSlice'
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

describe('an approval that retires unseen stays unread until the reader sees it', () => {
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

  it('the shared selector and the tab-title / surface badge keep counting it, with no ack sent', () => {
    const store = createTestStore()
    act(() => { store.dispatch(addNotification(approval)) })
    const state = () => store.getState() as unknown as RootState
    expect(selectAllSurfacesAttention(state())).toBe(1)
    act(() => { store.dispatch(retireApprovalRow('1.0', 'gone')) })
    expect(mockAck).not.toHaveBeenCalled()
    expect(selectUnreadNotes(state()).map(n => n.ts)).toEqual(['1.0'])
    expect(selectAllSurfacesAttention(state())).toBe(1)
    expect(selectSurfaceBadgeCount('notifications')(state())).toBe(1)
    // Read the ordinary way, it leaves every count.
    act(() => { store.dispatch(ackNotificationByTs('1.0')) })
    expect(selectUnreadNotes(state())).toEqual([])
    expect(selectAllSurfacesAttention(state())).toBe(0)
    expect(selectSurfaceBadgeCount('notifications')(state())).toBe(0)
  })

  it('the native banner is not fired a second time for it', () => {
    const store = createTestStore()
    renderHook(() => useNativeNotification('Kiro Crew', '/avatar.png'), {
      wrapper: ({ children }) => createElement(Provider, { store }, children),
    })
    act(() => { store.dispatch(addNotification(approval)) })
    expect(FakeNotification.instances).toEqual(['Tool approval'])
    act(() => { store.dispatch(retireApprovalRow('1.0', 'gone')) })
    expect(FakeNotification.instances).toEqual(['Tool approval'])
  })
})
