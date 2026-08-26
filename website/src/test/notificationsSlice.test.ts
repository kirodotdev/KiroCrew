import { describe, it, expect, vi } from 'vitest'
import reducer, {
  addNotification,
  ackNotificationByTs,
  unackNotificationByTs,
  removeNotificationByTs,
  clearAllNotifications,
  fetchNotifications,
  clearNotifications,
  deleteNotification,
  ackNotification,
  unackNotification,
  ackAllNotifications,
  NOTIFICATIONS_RING_CAP,
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

    it('a wildcard ack echo does not stamp, so a fetch still corrects a row it should not have marked', () => {
      // Ack-all is applied server-side; a new notification arrives while that is
      // in flight; then the `"*"` broadcast lands and marks everything read
      // locally, including the row the backend never acked. The value is
      // pre-existing behaviour, but it must stay CORRECTABLE — stamping it would
      // pin it against the fetch that carries the server's truth.
      let state = reducer({ items: [n1] }, ackNotificationByTs('*'))
      state = reducer(state, addNotification(n2))
      state = reducer(state, ackNotificationByTs('*'))
      expect(state.items.find(n => n.ts === '2')?.acked).toBe(true)
      expect(state.ackSeqByTs ?? {}).toEqual({})
      const corrected = reducer(
        { ...state, clearSeq: 0 },
        fetchNotifications.fulfilled(
          { items: [{ ...n1, acked: true }, n2], seq: 0, ackSeq: state.ackSeq },
          '',
        ),
      )
      expect(corrected.items.find(n => n.ts === '2')?.acked).toBeUndefined()
    })

    it('a named ack echo still stamps, so it keeps its protection', () => {
      const state = reducer({ items: [n1], clearSeq: 0 }, ackNotificationByTs('1'))
      expect(Object.keys(state.ackSeqByTs ?? {})).toEqual(['1'])
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
      const state = reducer({ items: [n1], clearSeq: 0 }, fetchNotifications.fulfilled({ items: [n2], seq: 0 }, ''))
      expect(state.items).toEqual([n2])
    })

    it('fetchNotifications.fulfilled is dropped when a clear landed mid-flight', () => {
      // The fetch started at generation 0; a clear bumped it to 1 while the
      // request was in flight, so this payload predates the clear. Applying it
      // would resurrect the rows and the bell badge with them.
      const state = reducer({ items: [], clearSeq: 1 }, fetchNotifications.fulfilled({ items: [n1, n2], seq: 0 }, ''))
      expect(state.items).toEqual([])
    })

    it('a fetch started after the clear still applies', () => {
      const state = reducer({ items: [], clearSeq: 1 }, fetchNotifications.fulfilled({ items: [n1], seq: 1 }, ''))
      expect(state.items).toEqual([n1])
    })

    it('fetchNotifications.fulfilled keeps an ack that landed while it was in flight', () => {
      // The fetch snapshotted ackSeq 0; the user acked mid-flight, so the
      // payload's unacked copy of that item predates the ack. Applying it
      // verbatim would resurrect the row as unread.
      const acked = reducer(
        { items: [n1, n2], clearSeq: 0 },
        ackNotificationByTs('1'),
      )
      const state = reducer(
        acked,
        fetchNotifications.fulfilled({ items: [n1, n2], seq: 0, ackSeq: 0 }, ''),
      )
      expect(state.items[0].acked).toBe(true)
      expect(state.items[1].acked).toBeUndefined()
    })

    it('two fetches resolving out of order do not revert an ack made between them', () => {
      // Fetch A starts before the ack (snapshot 0), the user acks, fetch B
      // starts after it (snapshot 1) and resolves FIRST with the server's acked
      // copy; then the older A resolves last carrying the pre-ack copy. Only
      // the per-item stamp distinguishes them.
      let state = reducer({ items: [n1], clearSeq: 0 }, ackNotification.pending('', '1'))
      expect(state.items[0].acked).toBe(true)
      state = reducer(state, fetchNotifications.fulfilled({ items: [{ ...n1, acked: true }], seq: 0, ackSeq: 1 }, 'B'))
      expect(state.items[0].acked).toBe(true)
      state = reducer(state, fetchNotifications.fulfilled({ items: [n1], seq: 0, ackSeq: 0 }, 'A'))
      expect(state.items[0].acked).toBe(true)
    })

    it('the ack echo re-stamps, so a fetch started after the ack cannot revert it', () => {
      // The backend broadcasts an ack to every socket with no originator
      // exclusion, so the acking view gets its own echo. A fetch issued between
      // the flip and the echo is served from a server state that may predate
      // the write; the echo's stamp is what keeps it from winning.
      let state = reducer({ items: [n1], clearSeq: 0 }, ackNotification.pending('', '1'))
      const inFlightSnapshot = state.ackSeq
      state = reducer(state, ackNotificationByTs('1'))
      expect(state.ackSeq).toBeGreaterThan(inFlightSnapshot ?? 0)
      state = reducer(state, fetchNotifications.fulfilled({ items: [n1], seq: 0, ackSeq: inFlightSnapshot }, ''))
      expect(state.items[0].acked).toBe(true)
    })

    it('ackNotification.fulfilled re-stamps, extending protection past the confirmed write', () => {
      const pending = reducer({ items: [n1], clearSeq: 0 }, ackNotification.pending('', '1'))
      const confirmed = reducer(
        pending,
        ackNotification.fulfilled({ ts: '1', stamp: pending.ackSeqByTs?.['1'] }, '', '1'),
      )
      expect(confirmed.ackSeq).toBeGreaterThan(pending.ackSeq ?? 0)
      // A fetch that began before the confirmation still cannot unread the row.
      const state = reducer(
        confirmed,
        fetchNotifications.fulfilled({ items: [n1], seq: 0, ackSeq: pending.ackSeq }, ''),
      )
      expect(state.items[0].acked).toBe(true)
    })

    it('ackNotification.fulfilled re-asserts the ack a stale fetch clobbered mid-write', () => {
      // Reconnect flap: the flip happens, a fetch that began after it is served
      // from a state predating the write and installs unread, then the POST
      // confirms. The fetch merge does not stamp, so the item's stamp is still
      // the one this request started under — the rule permits the re-assert.
      const pending = reducer({ items: [n1], clearSeq: 0 }, ackNotification.pending('', '1'))
      const stamp = pending.ackSeqByTs?.['1']
      const clobbered = reducer(
        pending,
        fetchNotifications.fulfilled({ items: [n1], seq: 0, ackSeq: pending.ackSeq }, ''),
      )
      expect(clobbered.items[0].acked).toBeUndefined()
      const state = reducer(clobbered, ackNotification.fulfilled({ ts: '1', stamp }, '', '1'))
      expect(state.items[0].acked).toBe(true)
      // And the restored value now outranks any fetch still in flight from before.
      const late = reducer(
        state,
        fetchNotifications.fulfilled({ items: [n1], seq: 0, ackSeq: clobbered.ackSeq }, ''),
      )
      expect(late.items[0].acked).toBe(true)
    })

    it('a stale ack confirmation does NOT overwrite a newer unack from another tab', () => {
      // Tab A acks, its POST is slow; tab B unacks and that WS frame reaches this
      // view first. The confirmation is then stale evidence about a value that has
      // moved, and re-asserting read would contradict the backend it proves.
      const pending = reducer({ items: [n1], clearSeq: 0 }, ackNotification.pending('', '1'))
      const stamp = pending.ackSeqByTs?.['1']
      const moved = reducer(pending, unackNotificationByTs('1'))
      expect(moved.items[0].acked).toBe(false)
      const state = reducer(moved, ackNotification.fulfilled({ ts: '1', stamp }, '', '1'))
      expect(state.items[0].acked).toBe(false)
    })

    it('a stale unack confirmation does NOT overwrite a newer ack from another tab', () => {
      const seeded = { items: [{ ...n1, acked: true }], clearSeq: 0 }
      const pending = reducer(seeded, unackNotification.pending('', '1'))
      const stamp = pending.ackSeqByTs?.['1']
      const moved = reducer(pending, ackNotificationByTs('1'))
      const state = reducer(moved, unackNotification.fulfilled({ ts: '1', stamp }, '', '1'))
      expect(state.items[0].acked).toBe(true)
    })

    it('unackNotification.fulfilled re-asserts the unack the same way', () => {
      const seeded = { items: [{ ...n1, acked: true }], clearSeq: 0 }
      const pending = reducer(seeded, unackNotification.pending('', '1'))
      const stamp = pending.ackSeqByTs?.['1']
      const clobbered = reducer(
        pending,
        fetchNotifications.fulfilled({ items: [{ ...n1, acked: true }], seq: 0, ackSeq: pending.ackSeq }, ''),
      )
      expect(clobbered.items[0].acked).toBe(true)
      const state = reducer(clobbered, unackNotification.fulfilled({ ts: '1', stamp }, '', '1'))
      expect(state.items[0].acked).toBe(false)
    })

    it('ackAllNotifications.fulfilled re-asserts per item, sparing rows moved since', () => {
      const pending = reducer({ items: [n1, n2], clearSeq: 0 }, {
        type: ackAllNotifications.pending.type,
        meta: { arg: undefined, requestId: 'x', requestStatus: 'pending' as const },
      })
      const stamps = { ...(pending.ackSeqByTs ?? {}) }
      // A stale fetch unreads both, and another tab then unacks n2 specifically.
      let state = reducer(
        pending,
        fetchNotifications.fulfilled({ items: [n1, n2], seq: 0, ackSeq: pending.ackSeq }, ''),
      )
      state = reducer(state, unackNotificationByTs('2'))
      state = reducer(state, {
        type: ackAllNotifications.fulfilled.type,
        payload: { stamps },
        meta: { arg: undefined, requestId: 'x', requestStatus: 'fulfilled' as const },
      })
      // n1 had not moved, so its ack is restored; n2 moved, so it is left alone.
      expect(state.items.find(n => n.ts === '1')?.acked).toBe(true)
      expect(state.items.find(n => n.ts === '2')?.acked).toBe(false)
    })

    it('ack-all does not mark a notification that arrived during the request', () => {
      const pending = reducer({ items: [n1], clearSeq: 0 }, {
        type: ackAllNotifications.pending.type,
        meta: { arg: undefined, requestId: 'x', requestStatus: 'pending' as const },
      })
      const stamps = { ...(pending.ackSeqByTs ?? {}) }
      const arrived = reducer(pending, addNotification(n2))
      const state = reducer(arrived, {
        type: ackAllNotifications.fulfilled.type,
        payload: { stamps },
        meta: { arg: undefined, requestId: 'x', requestStatus: 'fulfilled' as const },
      })
      expect(state.items.find(n => n.ts === '2')?.acked).toBeUndefined()
    })

    it('a fetch started after the ack takes the server value, so a server-side unack converges', () => {
      // The request began after the local ack, so its copy of the item is
      // authoritative — otherwise an unack performed in another view could
      // never reach this one.
      const acked = reducer({ items: [n1], clearSeq: 0 }, ackNotificationByTs('1'))
      const state = reducer(
        acked,
        fetchNotifications.fulfilled({ items: [{ ...n1, acked: false }], seq: 0, ackSeq: acked.ackSeq }, ''),
      )
      expect(state.items[0].acked).toBe(false)
    })

    it('the fetch merge takes membership and ordering from the server', () => {
      const acked = reducer({ items: [n1, n2], clearSeq: 0 }, ackNotificationByTs('1'))
      const state = reducer(
        acked,
        fetchNotifications.fulfilled({ items: [n2], seq: 0, ackSeq: 0 }, ''),
      )
      // n1 is gone from the server's view, so the local ack does not resurrect it.
      expect(state.items.map(n => n.ts)).toEqual(['2'])
      // Its stamp is pruned with it, keeping the map bounded by the ring cap.
      expect(state.ackSeqByTs).toEqual({})
    })

    it('an unack that landed mid-flight also survives the response', () => {
      const seeded = { items: [{ ...n1, acked: true }], clearSeq: 0 }
      const unacked = reducer(seeded, unackNotificationByTs('1'))
      const state = reducer(
        unacked,
        fetchNotifications.fulfilled({ items: [{ ...n1, acked: true }], seq: 0, ackSeq: 0 }, ''),
      )
      expect(state.items[0].acked).toBe(false)
    })

    it('clearAllNotifications drops the ack stamps with the items', () => {
      const acked = reducer({ items: [n1], clearSeq: 0 }, ackNotificationByTs('1'))
      expect(Object.keys(acked.ackSeqByTs ?? {})).toEqual(['1'])
      const state = reducer(acked, clearAllNotifications())
      expect(state.ackSeqByTs).toEqual({})
    })

    it('removing an item drops its ack stamp', () => {
      const acked = reducer({ items: [n1, n2], clearSeq: 0 }, ackNotificationByTs('1'))
      const state = reducer(acked, removeNotificationByTs('1'))
      expect(state.ackSeqByTs).toEqual({})
    })

    it('deleteNotification.fulfilled drops the deleted item ack stamp', () => {
      const acked = reducer({ items: [n1, n2], clearSeq: 0 }, ackNotificationByTs('1'))
      const state = reducer(acked, deleteNotification.fulfilled('1', '', '1'))
      expect(state.ackSeqByTs).toEqual({})
    })

    it('a ring-cap eviction drops the evicted item ack stamps', () => {
      // The stamps must not outlive the rows the cap evicts, or a long-lived
      // tab on a stable socket accumulates one entry per acked item forever.
      let state = reducer(
        { items: [], clearSeq: 0 },
        addNotification({ kind: 'cron', title: 'first', body: '', ts: 'evicted' }),
      )
      state = reducer(state, ackNotificationByTs('evicted'))
      expect(Object.keys(state.ackSeqByTs ?? {})).toEqual(['evicted'])
      for (let i = 0; i < NOTIFICATIONS_RING_CAP; i++) {
        state = reducer(state, addNotification({ kind: 'cron', title: `n${i}`, body: '', ts: `fill-${i}` }))
      }
      expect(state.items).toHaveLength(NOTIFICATIONS_RING_CAP)
      expect(state.items.some(n => n.ts === 'evicted')).toBe(false)
      expect(state.ackSeqByTs).toEqual({})
    })

    it('clearNotifications.fulfilled empties items', () => {
      const state = reducer({ items: [n1, n2], clearSeq: 0 }, clearNotifications.fulfilled({ seq: 0 }, ''))
      expect(state.items).toEqual([])
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
