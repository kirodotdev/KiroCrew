import { useEffect, useState } from 'react'
import Glass from '../Glass'
import { useLocalCrewHidden } from '../overlayRoot'
import { useAppDispatch } from '../../store'
import { setActiveId } from '../../store/instancesSlice'
import { i18nT } from '../../i18n/t'
import { BANNER_AUTO_HIDE_MS } from '../../hooks/notificationBanner'
import { noticeRow, noticeTitle } from './copy'
import { openMilestonesPanel, postMilestoneEvent, useMilestones } from './useMilestones'

/** Notice ids this page load has already shown, so a re-read before the
 *  server records `notice_shown` cannot show the same one twice. */
const shownThisLoad = new Set<string>()

/** Test seam: forget what this load has shown. */
export function __resetMilestoneNotices(): void { shownThisLoad.clear() }

interface Notice { text: string; focusId: string | null }

/**
 * The quiet milestone notice (RFC 4.7): one line in the same corner, card and
 * timing as the file-menu result toast, rendered in the notification banner's
 * region. One milestone reads "Milestone: {title}"; several found in one read
 * merge into "{n} new milestones". Clicking opens the panel. Each id is shown
 * once, then `notice_shown` is posted so the server stops sending it. Text
 * comes only from the fixed copy map: an id with no text here is skipped.
 */
export default function MilestoneNoticeToast() {
  const { data } = useMilestones()
  const [notice, setNotice] = useState<Notice | null>(null)
  // Held while the pointer is over it OR keyboard focus is inside it, as the
  // file-menu toast it matches: a keyboard user who tabs to the notice must not
  // have it vanish under them on the timer.
  const [hovered, setHovered] = useState(false)
  const [focused, setFocused] = useState(false)
  const held = hovered || focused
  // A card that leaves the page fires no blur or pointer-leave. So when it is
  // hidden while held (a temporary chat turns milestones off), drop the hold,
  // or the notice would come back on return and never time out.
  const shown = notice !== null && data?.enabled === true
  useEffect(() => { if (!shown) { setHovered(false); setFocused(false) } }, [shown])
  // The notice shows over every Crew, but the panel belongs to the local one:
  // a click over a remote Crew brings the local Crew forward first.
  const crewHidden = useLocalCrewHidden()
  const dispatch = useAppDispatch()

  useEffect(() => {
    if (!data?.enabled || !Array.isArray(data.notices)) return
    const fresh = data.notices.filter(id => typeof id === 'string' && !shownThisLoad.has(id))
    // Only a notice this bundle can show is acknowledged: one with no text
    // here (`turns-sent:100`, a row this build does not know) stays on the
    // server for a build that can show it, instead of being marked seen unseen.
    const known = fresh.filter(id => noticeTitle(id) !== null)
    if (known.length === 0) return
    for (const id of known) {
      shownThisLoad.add(id)
      postMilestoneEvent(id, 'notice_shown')
    }
    setNotice(known.length === 1
      ? { text: i18nT('milestones.notice_one', { title: noticeTitle(known[0]) }), focusId: noticeRow(known[0]) }
      : { text: i18nT('milestones.notice_count', { count: known.length }), focusId: null })
  }, [data])

  useEffect(() => {
    if (!notice || held) return
    const t = window.setTimeout(() => setNotice(null), BANNER_AUTO_HIDE_MS)
    return () => window.clearTimeout(t)
  }, [notice, held])

  // A session where milestones are off (temporary, incognito) shows none, even one already up.
  if (!notice || !data?.enabled) return null
  return (
    <Glass
      variant="panel"
      radius={16}
      role="status"
      aria-live="polite"
      data-testid="milestone-notice"
      className="notif-material glass-shadow pointer-events-auto mt-2 block rounded-2xl text-[13px]"
      onPointerEnter={() => setHovered(true)}
      onPointerLeave={() => setHovered(false)}
      onFocusCapture={() => setFocused(true)}
      onBlurCapture={event => { if (!event.currentTarget.contains(event.relatedTarget as Node | null)) setFocused(false) }}
    >
      <button
        type="button"
        data-milestones-trigger=""
        className="w-full text-left px-3 py-2.5 text-text bg-transparent border-none cursor-pointer"
        onClick={() => { const focusId = notice.focusId; setNotice(null); setHovered(false); setFocused(false); if (crewHidden) dispatch(setActiveId(null)); openMilestonesPanel(focusId) }}
      >
        {notice.text}
      </button>
    </Glass>
  )
}
