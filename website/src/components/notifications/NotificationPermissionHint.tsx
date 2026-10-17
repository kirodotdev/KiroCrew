import { useState } from 'react'
import { BellRing } from 'lucide-react'

import { i18nT } from '../../i18n/t'
import ErrorNotice from '../ErrorNotice'
import { useNotificationPermission } from '../../hooks/useNotificationPermission'
import { loadPermissionHintDismissed, savePermissionHintDismissed } from '../../hooks/notificationBanner'
import { MAC_ACTION_BTN_CLASS } from './notifMeta'

/**
 * One slim row inside the bell popover's controls card nudging the user to
 * allow OS notifications. Shown only while the browser has never been asked
 * (`permission === 'default'`), there is at least one notification to be
 * alerted about, and the user has not said "Not now".
 *
 * "Allow" is the user gesture the prompt needs. Whatever the browser answers —
 * granted, denied, or the prompt closed without a verdict — the row retires:
 * a second nudge after a first answer is nagging, and a user who wants it
 * later has the Settings › Notifications row. "Not now" retires it for good
 * the same way, persisted so it never returns on reload.
 */
export default function NotificationPermissionHint({ hasNotes }: { hasNotes: boolean }) {
  const { permission, request } = useNotificationPermission()
  const [dismissed, setDismissed] = useState(loadPermissionHintDismissed)
  const [saveFailed, setSaveFailed] = useState(false)

  if (permission !== 'default' || !hasNotes || dismissed) return null

  // The row goes only once the dismissal is on disk: hiding it on a failed
  // write would bring it straight back on the next load, so the row stays
  // with a notice and the buttons remain the retry.
  const retire = () => {
    if (savePermissionHintDismissed()) { setSaveFailed(false); setDismissed(true) }
    else setSaveFailed(true)
  }

  return (
    <div data-testid="notification-permission-hint" className="mb-1.5 px-1 py-1 rounded-lg bg-accent-subtle">
    <div className="flex items-center gap-2">
      <BellRing className="lucide-inline shrink-0 text-accent" />
      <div className="flex-1 min-w-0 text-[12px] text-text truncate">{i18nT('components.notifications.notificationPermissionHint.get_alerted_when_you_re_away')}</div>
      <button
        type="button"
        className={`${MAC_ACTION_BTN_CLASS} text-accent`}
        onClick={() => { retire(); void request() }}
      >{i18nT('components.notifications.notificationPermissionHint.allow')}</button>
      <button
        type="button"
        className={`${MAC_ACTION_BTN_CLASS} text-muted`}
        onClick={retire}
      >{i18nT('components.notifications.notificationPermissionHint.not_now')}</button>
    </div>
    {saveFailed && (
      /* No hand-off: the bell popover floats over whatever page the user is
         on, which may hold an unsaved draft the hand-off's navigation to the
         chat would destroy. Allow / Not now on the row are the retry. */
      <ErrorNotice
        variant="inline"
        testId="notification-permission-hint-save-failed"
        message={i18nT('components.notifications.notificationPermissionHint.save_failed')}
      />
    )}
    </div>
  )
}
