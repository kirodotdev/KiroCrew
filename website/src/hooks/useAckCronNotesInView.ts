/**
 * Mark a cron run's bell notes read once its chat is on screen (#23564).
 *
 * A scheduled job's run lands in its own session and posts a `cron` note to the
 * bell. Opening that session's chat shows the run, so the note has done its
 * job; leaving it unread made the bell claim something still needed attention.
 *
 * "On screen" is the same test the live banner uses to stay quiet
 * (`targetsCurrentView`): a chat route showing the note's slot, in a focused,
 * visible window, with the local Crew in front. Only `cron` notes are read
 * this way. Every other kind (an approval, a crewmate's question) still needs
 * an explicit action, so seeing its session is not an answer to it.
 *
 * Mark-read only. The cron "Acknowledge" button (`CronAckBar`) also tells the
 * job not to repeat the result in later runs; that stays a deliberate press.
 */
import { useEffect, useRef, useState } from 'react'
import { useLocation } from 'react-router-dom'
import { useAppDispatch, useAppSelector } from '../store'
import { ackNotification } from '../store/notificationsSlice'
import type { Notification } from '../types'
import { isChatPath } from './notificationBanner'
import { isWindowAway } from './windowAway'

/** The `ts` of every unread cron note for *slot*. */
export function unreadCronNotesFor(notes: readonly Notification[], slot: string | null | undefined): string[] {
  if (!slot) return []
  return notes.filter(n => n.kind === 'cron' && !n.acked && n.slot === slot).map(n => n.ts)
}

/** `!isWindowAway()`, kept current across focus and visibility changes. */
function useWindowPresent(): boolean {
  const [present, setPresent] = useState(() => !isWindowAway())
  useEffect(() => {
    const sync = () => setPresent(!isWindowAway())
    window.addEventListener('focus', sync)
    window.addEventListener('blur', sync)
    document.addEventListener('visibilitychange', sync)
    sync()
    return () => {
      window.removeEventListener('focus', sync)
      window.removeEventListener('blur', sync)
      document.removeEventListener('visibilitychange', sync)
    }
  }, [])
  return present
}

export function useAckCronNotesInView({ hidden = false }: { hidden?: boolean } = {}): void {
  const dispatch = useAppDispatch()
  const { pathname } = useLocation()
  const activeSlot = useAppSelector(s => s.chat.activeSlot)
  const notes = useAppSelector(s => s.notifications.items)
  const present = useWindowPresent()
  // One request per note: the optimistic flip makes a second pass see it read,
  // but a refused ack rolls it back to unread, and asking again on every render
  // after that would loop against a server that keeps saying no.
  const asked = useRef(new Set<string>())

  useEffect(() => {
    if (hidden || !present || !isChatPath(pathname)) return
    for (const ts of unreadCronNotesFor(notes, activeSlot)) {
      if (asked.current.has(ts)) continue
      asked.current.add(ts)
      void dispatch(ackNotification(ts))
    }
  }, [dispatch, hidden, present, pathname, activeSlot, notes])
}
