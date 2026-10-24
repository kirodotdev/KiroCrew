import { useEffect, useRef, useState } from 'react'
import { useLocation } from 'react-router-dom'
import { useAppDispatch, useAppSelector } from '../store'
import { ackNotification } from '../store/notificationsSlice'
import type { Notification } from '../types'
import { isChatPath } from './notificationBanner'
import { isWindowAway } from './windowAway'

/**
 * The unread cron run notes whose chat is the one on screen (#23564).
 *
 * A cron note links its run to the job's persistent session through `slot`
 * (`cron-<job id>`, stamped by `cron_delivery.py` only while that session is
 * shown in chat). When that session is the active chat in a focused window,
 * the run's output is in the transcript the user is reading, so the bell entry
 * says nothing new. Only `cron` notes qualify: an approval or a crewmate's
 * question is something to act on, not a duplicate of the transcript.
 */
export function cronNotesSeenInChat(
  items: readonly Notification[],
  ctx: { pathname: string; activeSlot: string | null | undefined; windowAway: boolean },
): string[] {
  if (ctx.windowAway || !ctx.activeSlot || !isChatPath(ctx.pathname)) return []
  return items
    .filter(n => n.kind === 'cron' && !n.acked && !!n.slot && n.slot === ctx.activeSlot)
    .map(n => n.ts)
}

/**
 * Mark a cron job's bell notes read when its chat is opened, and when a run
 * lands while that chat is on screen.
 *
 * Each note is marked at most once per page load. That keeps two things true:
 * a note the user deliberately marks unread again while the chat is open stays
 * unread, and a mark-read the server refuses (rolled back to unread by the
 * slice) is not retried in a loop. Returning to the window re-checks, so a run
 * that landed while the user was away is marked when they come back to it.
 */
export function useMarkOpenCronChatRead(): void {
  const dispatch = useAppDispatch()
  const { pathname } = useLocation()
  const activeSlot = useAppSelector(s => s.chat.activeSlot)
  const items = useAppSelector(s => s.notifications.items)
  const attempted = useRef(new Set<string>())
  const [focusTick, setFocusTick] = useState(0)

  useEffect(() => {
    const bump = () => setFocusTick(t => t + 1)
    window.addEventListener('focus', bump)
    document.addEventListener('visibilitychange', bump)
    return () => {
      window.removeEventListener('focus', bump)
      document.removeEventListener('visibilitychange', bump)
    }
  }, [])

  useEffect(() => {
    const seen = cronNotesSeenInChat(items, { pathname, activeSlot, windowAway: isWindowAway() })
    for (const ts of seen) {
      if (attempted.current.has(ts)) continue
      attempted.current.add(ts)
      void dispatch(ackNotification(ts))
    }
  }, [dispatch, items, pathname, activeSlot, focusTick])
}
