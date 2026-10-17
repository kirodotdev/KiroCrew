/**
 * Fires a browser `Notification` whenever a new unacked notification lands in
 * the Redux store while the user is away from the window. Used by `App.tsx`
 * to surface macOS notification-center toasts.
 *
 * This is the ONLY place a feed note becomes an OS toast. The socket layer's
 * `approval` frame dispatches `addNotification`, which grows the count read
 * here, so the approval reaches the OS through this hook with the note's own
 * title/body and its `approval_id` as the collapse tag. A second constructor
 * on the approval path (with its own tag) meant two banners for one event,
 * because the OS collapses only equal tags.
 *
 * The toast is gated on `isWindowAway()`: while the window is visible AND
 * focused the in-app banner (`NotificationBanner`, gated by `shouldBannerNote`,
 * fed by the socket layer's live relay for both `notification` and `approval`
 * frames) and the bell badge already show the note, and an OS toast on top of
 * them says the same thing a third time. A note that arrives while the user is
 * watching advances the count without a toast and is NOT re-announced when
 * the window later loses focus: it was seen.
 *
 * A muted channel's notes arrive with `silenced: true` and `priority:
 * "passive"` (`ChannelSettings.apply()`, `kiro_crew/notifications/settings.py`)
 * -- the backend's stated contract is that every attention surface (badge
 * count, sound, native banner, feed styling) skips them. The in-app feed
 * (`NotificationFeed.tsx`) already reads `silenced` for its styling; this hook
 * must exclude the same notes from BOTH its unread count and its
 * latest-note pick, or a muted note still increments the count and fires the
 * native banner even though the in-app row correctly shows "muted".
 *
 * A shared hook so the regression tests in
 * `integration/AppNotification.integration.test.tsx` exercise *this* code —
 * if the effect regresses, tests and production break together.
 */
import { useEffect, useRef } from 'react'
import { useAppSelector } from '../store'
// Shared with the tab-title attention count rather than kept file-local: two
// attention surfaces that spell this rule separately can drift apart, and the
// backend states it once for all of them.
import { isSilencedNote } from '../store/notificationsSlice'
// A note's `body` is MARKDOWN by contract -- the detail panel renders it as
// markdown and producers write `**name** -- description`, `_italics_`,
// `**Triggers:**` (see `_pending_skill_notification`, dashboard/server.py).
// An OS notification body is plain text: macOS Notification Center paints the
// asterisks and underscores literally. Reuse the same flattener the in-app feed
// row uses so both previews read identically.
import { stripMd } from '../components/notifications/notifMeta'
// The same "away" predicate the in-app banner and the chat-complete toast
// read, so the three surfaces agree on when the user can see the app.
import { isWindowAway } from './windowAway'
// Posts the toast, or relays it to the parent frame when this dashboard is an
// embedded instance pane (where Notification.permission is denied by design).
import { nativeNotificationPermitted, postNativeNotification } from '../lib/nativeNotify'
import { i18nT } from '../i18n/t'

export function useNativeNotification(botName: string, avatar: string) {
  const notifCount = useAppSelector(
    (s) => s.notifications.items.filter((n) => !n.acked && !isSilencedNote(n)).length,
  )
  const latestNotif = useAppSelector((s) => {
    const unacked = s.notifications.items.filter((n) => !n.acked && !isSilencedNote(n))
    return unacked.length > 0 ? unacked[unacked.length - 1] : null
  })

  const prev = useRef(0)
  useEffect(() => {
    if (notifCount > prev.current) {
      // The visibility gate sits inside the permission branch on purpose:
      // the best-effort permission prompt below is about capability, not
      // attention, and must not depend on where the window is.
      if (nativeNotificationPermitted() && isWindowAway()) {
        const delta = notifCount - prev.current
        const title = latestNotif?.title || botName
        // stripMd only on the note's own markdown body; the generic fallback
        // and the title are plain text already (the feed renders the title
        // verbatim, never as markdown).
        const noteBody = latestNotif?.body ? stripMd(latestNotif.body) : ''
        const body =
          noteBody || i18nT('hooks.useNativeNotification.new_notifications', { count: delta })
        // Best-effort: Android Chrome throws "Illegal constructor" for a
        // page-context Notification even with permission granted; the helper
        // swallows it and the in-app notification center still shows the event.
        postNativeNotification(title, {
          body,
          icon: avatar,
          // Always silent: WebAudio (useNotificationSound) is the single
          // source of notification sound. Without this the OS toast plays
          // its own system chime on top of the WebAudio tone — a double
          // sound. Browsers that ignore `silent` are no worse than before.
          silent: true,
          tag:
            latestNotif?.approval_id ||
            latestNotif?.job_id ||
            latestNotif?.task_id ||
            'kirocrew-notif',
        })
      } else if (typeof Notification !== 'undefined' && Notification.permission === 'default') {
        // Best-effort only: browsers refuse a prompt with no user gesture
        // behind it, and this fires from an effect. The two places that ask
        // FROM a gesture are Settings › Notifications ("Allow system
        // notifications", `SystemNotificationsRow`) and the bell popover's
        // hint row (`NotificationPermissionHint`), both through
        // `useNotificationPermission().request`.
        Notification.requestPermission()
      }
    }
    prev.current = notifCount
  }, [notifCount, botName, avatar, latestNotif])
}
