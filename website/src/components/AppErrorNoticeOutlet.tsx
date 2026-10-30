import { useEffect, useState } from 'react'
import { subscribeAppErrorNotice, type AppErrorNoticeEvent } from '../lib/appNotice'
import ErrorNotice from './ErrorNotice'

const OVERLAY = 'fixed left-safe-offset-3 right-safe-offset-3 top-safe-offset-3 z-50 md:left-auto md:right-safe-offset-6 md:w-[28rem]'

/** Shows the newest app notice in popout and embed frames, which mount no
 *  notification banner. A later notice replaces it, and dismissing clears it. */
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
