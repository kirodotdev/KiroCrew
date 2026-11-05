import { useEffect, useRef, useState } from 'react'
import StatusNotice from '../../components/StatusNotice'
import { useConnected } from '../../hooks/useConnected'
import { i18nT } from '../../i18n/t'

/** How often the band re-checks the read's age. Only the age rule needs a clock;
 *  a drop, a reconnect and a fresh read each re-render the band on their own. */
export const BEHIND_CLOCK_MS = 10_000

/**
 * The page-wide "values may be behind" band an open Dashboard tab draws above its
 * frame. Drawn by the HOST, not the page: a page's own script cannot hide it, and
 * an author who forgot to draw one cannot leave a stopped page looking current.
 *
 * Shown in two cases, and cleared by a fresh read of the page:
 *
 * 1. THE SOCKET DROPPED while this tab was open, and nothing fresh has arrived
 *    since. Every push sent in that time is lost, so the values on screen are only
 *    as current as the last one that landed. Shown for as long as the socket is
 *    down too, even if a GET got through: nothing pushes into the page until the
 *    socket is back. A tab that MOUNTED disconnected is not banded for that alone:
 *    the read it just made is as fresh as anything, and the age rule below still
 *    catches it if the socket never comes.
 * 2. THE LAST FRESH READ IS OLDER THAN `staleAfterMs`. The fallback read runs on a
 *    fixed interval, so two missed intervals means something stopped it (a frozen
 *    tab, a background tab whose interval paused, a page evicted from the live
 *    feed, a read that keeps failing).
 *
 * A pushed patch does NOT count as fresh: the page may refuse it (a version gap
 * after a drop) and keep showing older values, and the host cannot see that. The
 * fallback read runs every interval while the tab is in front, and a reconnect
 * re-reads at once, so the read time alone is a sound clock.
 *
 * `hidden` suppresses the drawing only. The band stays mounted under it, so a drop
 * seen while another notice stood in for it is still remembered when that notice
 * goes away.
 */
export default function CrewDashboardBehindBand({ readAt, staleAfterMs, hidden = false }: {
  /** When the dashboard query last succeeded (`dataUpdatedAt`). 0 = never. */
  readAt: number
  /** The age past which a read counts as behind. */
  staleAfterMs: number
  /** Another notice already says the values are behind: track, but draw nothing. */
  hidden?: boolean
}) {
  const connected = useConnected()
  const [now, setNow] = useState(() => Date.now())
  const [droppedAt, setDroppedAt] = useState<number | null>(null)
  /** When the socket came back after the drop. Only a read that lands AFTER this
   *  clears the band: a read that got through while the socket was down is older
   *  than the frames it missed between that read and the reconnect. */
  const [backAt, setBackAt] = useState<number | null>(null)
  const wasConnected = useRef(connected)

  useEffect(() => {
    if (wasConnected.current && !connected) {
      setDroppedAt(Date.now())
      setBackAt(null)
    } else if (!wasConnected.current && connected) {
      setBackAt(Date.now())
    }
    wasConnected.current = connected
  }, [connected])

  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), BEHIND_CLOCK_MS)
    return () => clearInterval(id)
  }, [])

  const freshAt = readAt
  const dropped = droppedAt !== null && (!connected || backAt === null || freshAt <= backAt)
  // `now` lags a fresh read by up to one clock step, so the age is negative right
  // after one lands: the band clears at once instead of on the next tick.
  const overdue = freshAt > 0 && now - freshAt > staleAfterMs
  if (hidden || (!dropped && !overdue)) return null
  return (
    <div className="shrink-0 border-b border-border p-2" data-testid="crew-dashboard-behind-band" data-reason={dropped ? 'disconnected' : 'old'}>
      <StatusNotice
        tone="warn"
        message={i18nT(dropped ? 'pages.membersPage.dashboard_values_behind_offline' : 'pages.membersPage.dashboard_values_behind_old')}
        testId="crew-dashboard-behind"
      />
    </div>
  )
}
