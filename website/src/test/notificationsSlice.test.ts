import { describe, it, expect, vi } from 'vitest'
import reducer, {
  addNotification,
  ackNotificationByTs,
  clearAllNotifications,
  fetchNotifications,
  clearNotifications,
  deleteNotification,
  ackNotification,
  unackNotification,
  ackAllNotifications,
} from '../store/notificationsSlice'
import type { Notification } from '../types'

vi.mock('../api/client', () => ({
  api: {
    notifications: vi.fn(),
    clearNotifications: vi.fn(),
    deleteNotification: vi.fn(),
    ackNotification: vi.fn().mockResolvedValue({}),
    unackNotification: vi.fn().mockResolvedValue({}),
    ackAllNotifications: vi.fn().mockResolvedValue({}),
  },
}))

const n1: Notification = { kind: 'cron', title: 'Job done', body: 'output', ts: '1' }
const n2: Notification = { kind: 'approval', title: 'Approve?', body: 'tool X', ts: '2' }
const fetched = (items: Notification[], seq = 0) => ({ items, seq })

describe('notificationsSlice', () => {
  describe('reducers', () => {
    it('addNotification appends to items', () => {
      const state = reducer({ items: [n1] }, addNotification(n2))
      expect(state.items).toHaveLength(2)
      expect(state.items[1].ts).toBe('2')
    })

    it('ackNotificationByTs marks as acked', () => {
      const state = reducer({ items: [n1, n2] }, ackNotificationByTs('1'))
      expect(state.items[0].acked).toBe(true)
      expect(state.items[1].acked).toBeUndefined()
    })

    it('clearAllNotifications empties items (WS notifications_clear sync)', () => {
      const state = reducer({ items: [n1, n2] }, clearAllNotifications())
      expect(state.items).toEqual([])
    })

    it('clearAllNotifications on an empty list is a no-op, not an error', () => {
      const state = reducer({ items: [] }, clearAllNotifications())
      expect(state.items).toEqual([])
    })
  })

  describe('extraReducers', () => {
    it('fetchNotifications.fulfilled replaces items', () => {
      let state = reducer({ items: [n1] }, fetchNotifications.pending('fetch', undefined))
      state = reducer(state, fetchNotifications.fulfilled(fetched([n2]), 'fetch'))
      expect(state.items).toEqual([n2])
    })

    it('keeps the newest fetch response when requests overlap', () => {
      let state = reducer(undefined, addNotification(n1))
      state = reducer(state, fetchNotifications.pending('older', undefined))
      state = reducer(state, fetchNotifications.pending('newer', undefined))

      state = reducer(state, fetchNotifications.fulfilled(fetched([n2]), 'newer'))
      expect(state.items).toEqual([n2])

      state = reducer(state, fetchNotifications.fulfilled(fetched([n1]), 'older'))
      expect(state.items).toEqual([n2])
    })

    it('keeps an acknowledgement while its request is unsettled', () => {
      let state = reducer(undefined, addNotification(n1))
      state = reducer(state, ackNotification.pending('ack', '1'))
      state = reducer(state, fetchNotifications.pending('stale', undefined))
      state = reducer(state, fetchNotifications.fulfilled(fetched([{ ...n1, acked: false }]), 'stale'))

      expect(state.items[0]?.acked).toBe(true)
      state = reducer(state, ackNotification.fulfilled('1', 'ack', '1'))
      expect(state.items[0]?.acked).toBe(true)
    })

    it('does not let a server-sent acknowledgement get overwritten by an earlier fetch', () => {
      let state = reducer(undefined, addNotification(n1))
      state = reducer(state, fetchNotifications.pending('stale', undefined))
      state = reducer(state, ackNotificationByTs('1'))
      state = reducer(state, fetchNotifications.fulfilled(fetched([{ ...n1, acked: false }]), 'stale'))

      expect(state.items[0]?.acked).toBe(true)
    })

    it('does not resurrect a deleted notification from an earlier fetch', () => {
      let state = reducer(undefined, addNotification(n1))
      state = reducer(state, addNotification(n2))
      state = reducer(state, fetchNotifications.pending('stale', undefined))
      state = reducer(state, deleteNotification.fulfilled('1', 'delete', '1'))
      state = reducer(state, fetchNotifications.fulfilled(fetched([n1, n2]), 'stale'))

      expect(state.items).toEqual([n2])
    })

    it('does not restore notifications after a clear', () => {
      let state = reducer(undefined, addNotification(n1))
      state = reducer(state, fetchNotifications.pending('stale', undefined))
      state = reducer(state, clearNotifications.fulfilled({ seq: 0 }, 'clear'))
      state = reducer(state, fetchNotifications.fulfilled(fetched([n1]), 'stale'))

      expect(state.items).toEqual([])
    })

    it('clearNotifications.fulfilled empties items', () => {
      const state = reducer({ items: [n1, n2], clearSeq: 0 }, clearNotifications.fulfilled({ seq: 0 }, ''))
      expect(state.items).toEqual([])
    })

    it('a fetch started after the clear still applies', () => {
      let state = reducer({ items: [], clearSeq: 1 }, fetchNotifications.pending('after-clear', undefined))
      state = reducer(state, fetchNotifications.fulfilled(fetched([n1], 1), 'after-clear'))
      expect(state.items).toEqual([n1])
    })

    it('clearNotifications.fulfilled does not re-empty after the WS frame applied the clear', () => {
      // Clear click → WS notifications_clear empties and bumps to 1 → a note
      // delivered during the backend rewrite is added → HTTP 200 lands last.
      // The trailing fulfilment must leave that note alone: the backend still
      // holds it, so wiping it here would lose a live notification.
      const state = reducer({ items: [n2], clearSeq: 1 }, clearNotifications.fulfilled({ seq: 0 }, ''))
      expect(state.items).toEqual([n2])
    })

    it('deleteNotification.fulfilled removes by ts', () => {
      const state = reducer({ items: [n1, n2] }, deleteNotification.fulfilled('1', '', '1'))
      expect(state.items).toHaveLength(1)
      expect(state.items[0].ts).toBe('2')
    })

    it('ackNotification.pending optimistically acks', () => {
      const action = { type: ackNotification.pending.type, meta: { arg: '1', requestId: 'x', requestStatus: 'pending' as const } }
      const state = reducer({ items: [n1, n2] }, action)
      expect(state.items[0].acked).toBe(true)
      expect(state.items[1].acked).toBeUndefined()
    })

    it('unackNotification.pending optimistically unacks', () => {
      const acked = { ...n1, acked: true }
      const action = { type: unackNotification.pending.type, meta: { arg: '1', requestId: 'x', requestStatus: 'pending' as const } }
      const state = reducer({ items: [acked, n2] }, action)
      expect(state.items[0].acked).toBe(false)
    })

    it('ackAllNotifications.pending acks all', () => {
      const action = { type: ackAllNotifications.pending.type, meta: { arg: undefined, requestId: 'x', requestStatus: 'pending' as const } }
      const state = reducer({ items: [n1, n2] }, action)
      expect(state.items.every(n => n.acked)).toBe(true)
    })
  })
})
