/**
 * The browser-tab attention count must skip silenced / passive-priority notes.
 *
 * Backend contract (`kiro_crew/notifications/settings.py`, `ChannelSettings.apply()`):
 * muting a channel leaves the note in history but stamps it `silenced: true` and
 * forces `priority: "passive"`, "so every attention surface (badge count, sound,
 * native banner, feed styling) skips it". The coordinator holds itself to that —
 * `notification_coordinator.deliver()` only does `state._unread_count += 1` when
 * `note.get("priority") != "passive"`.
 *
 * `selectUnacknowledgedNotificationCount` (`surfaces/builtins.tsx`) is the
 * `unreadSelector` the Notifications surface registers, and it filtered on
 * `!acked` alone. It is summed by `selectAllSurfacesAttention`, which App.tsx
 * turns into `document.title = "(n) …"` — so a muted note put an attention
 * number in the tab title that no visible surface accounted for: the bell
 * sheet's own badge already excluded it (`App.tsx`, `!n.acked && n.priority !==
 * 'passive' && !n.silenced`), the native banner excluded it (#11300/#11304), and
 * the feed hides silenced rows behind the muted disclosure. Nothing the user
 * could see explained the count, and nothing they could click cleared it.
 *
 * The assertions sit ON the selectors, whose inputs are exactly the store rows —
 * no rendering, no window title, nothing else that could decide the outcome.
 */
import { describe, it, expect } from 'vitest'
import { createTestStore } from './helpers'
import { addNotification } from '../store/notificationsSlice'
import { selectAllSurfacesAttention, selectSurfaceBadgeCount } from '../surfaces/registry'
import type { Notification as AppNotification } from '../types'
// Side-effect import: registers the built-in surfaces (including the
// Notifications surface whose unreadSelector is under test) into the registry.
import '../surfaces/builtins'
import type { RootState } from '../store'

type Store = ReturnType<typeof createTestStore>

const note = (over: Partial<AppNotification>): AppNotification => ({
  kind: 'system',
  title: 'note',
  body: '',
  ts: String(Math.random()),
  ...over,
} as AppNotification)

const push = (store: Store, ...notes: AppNotification[]) => {
  for (const n of notes) store.dispatch(addNotification(n))
}

/** The tab-title sum and the surface's own badge, read off one store. */
const counts = (store: Store) => {
  const state = store.getState() as unknown as RootState
  return {
    attention: selectAllSurfacesAttention(state),
    badge: selectSurfaceBadgeCount('notifications')(state),
  }
}

describe('notification attention count skips silenced / passive notes', () => {
  it('ignores a note the backend muted (silenced + forced passive)', () => {
    const store = createTestStore()
    // Exactly what `ChannelSettings.apply()` writes for a muted channel.
    push(store, note({
      title: 'Host memory tight for 10 minutes',
      channel: 'system.resources',
      priority: 'passive',
      silenced: true,
    }))

    expect(counts(store)).toEqual({ attention: 0, badge: 0 })
  })

  it('ignores a passive-priority note that was never muted', () => {
    // A channel default or a producer-requested `passive` — e.g. a subagent
    // completion. `silenced` is absent, so a check on that flag alone misses it,
    // while the backend's own `_unread_count` already skips it on priority.
    const store = createTestStore()
    push(store, note({ title: 'Subagent finished', channel: 'chat.subagent', priority: 'passive' }))

    expect(counts(store)).toEqual({ attention: 0, badge: 0 })
  })

  it('ignores a silenced note whose priority the stamp did not reach', () => {
    // Defence in depth for a row persisted by an older backend, or rehydrated
    // from a notification log written before the priority forcing landed:
    // `silenced` is the user's decision and outranks whatever priority says.
    const store = createTestStore()
    push(store, note({ title: 'Heartbeat', channel: 'system.heartbeat', silenced: true }))

    expect(counts(store)).toEqual({ attention: 0, badge: 0 })
  })

  // Guard-the-guard: a filter that returned 0 for everything would pass all
  // three cases above. These pin that ordinary attention still counts, and that
  // the count is the number of qualifying rows rather than a boolean.
  it('still counts ordinary unacknowledged notes, and only those', () => {
    const store = createTestStore()
    push(
      store,
      note({ title: 'Approval needed', channel: 'system.approval', priority: 'critical' }),
      note({ title: 'Run finished', channel: 'cron.run' }),
      note({ title: 'Already read', channel: 'cron.run', acked: true }),
      note({ title: 'Muted', channel: 'system.heartbeat', priority: 'passive', silenced: true }),
    )

    expect(counts(store)).toEqual({ attention: 2, badge: 2 })
  })

  it('agrees with the bell sheet, which already applied the rule', () => {
    // App.tsx computes the bell/dock badge inline as
    // `!n.acked && n.priority !== 'passive' && !n.silenced`. The tab title and
    // the bell are two readouts of one number, so they must not disagree — that
    // divergence IS the defect, and asserting the two side by side pins it
    // without re-deriving either rule inside the test.
    const store = createTestStore()
    push(
      store,
      note({ title: 'Real', channel: 'cron.run' }),
      note({ title: 'Muted', channel: 'system.heartbeat', priority: 'passive', silenced: true }),
      note({ title: 'Passive', channel: 'chat.subagent', priority: 'passive' }),
    )

    const items = (store.getState() as unknown as RootState).notifications.items
    const bellBadge = items.filter(n => !n.acked && n.priority !== 'passive' && !n.silenced).length

    expect(counts(store).attention).toBe(bellBadge)
    expect(bellBadge).toBe(1)
  })
})
