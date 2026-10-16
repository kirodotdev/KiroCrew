import { useEffect, useState } from 'react'
import { subscribeAppErrorNotice, type AppErrorNoticeEvent } from '../lib/appNotice'
import ErrorNotice from './ErrorNotice'

/** Pinned like `ImportSessionOutcomeNotice`, but in the top band, clear of the
 *  composer. */
const OVERLAY = 'fixed left-safe-offset-3 right-safe-offset-3 top-safe-offset-3 z-50 md:left-auto md:right-safe-offset-6 md:w-[28rem]'

/**
 * Where an app notice lands in a popout or embed frame: those frames mount no
 * notification banner, so without this a hook failure (a session reload
 * refused) would only reach this frame's unrendered notification feed. The
 * newest notice replaces the previous one and stays until dismissed.
 */
export default function AppErrorNoticeOutlet() {
  const [notice, setNotice] = useState<AppErrorNoticeEvent | null>(null)
  useEffect(() => subscribeAppErrorNotice(setNotice), [])
  if (!notice) return null
  return (
    <div className={`${OVERLAY} rounded-lg bg-bg shadow-lg`}>
      {/* Keyed by arrival so a repeated failure remounts the alert and is
          announced again. No hand-off: a popout chat's composer may hold an
          unsent draft that opening the agent would replace. */}
      <ErrorNotice key={notice.id} message={notice.message} onDismiss={() => setNotice(null)} />
    </div>
  )
}
