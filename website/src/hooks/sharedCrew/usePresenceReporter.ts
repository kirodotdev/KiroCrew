/**
 * Report this tab's presence in the open session: viewing while the tab is
 * visible, typing for a few seconds after a keystroke, idle while hidden.
 *
 * The report is an activity enum and a session key, nothing else: no draft text,
 * no keystroke and no principal ever leave the tab. Sends are spaced at least
 * `MIN_SPACING_MS` apart to stay under the server's per-connection rate limit,
 * except the frame that ends a reported typing, which the server exempts and
 * which therefore goes at once. A keep-alive repeats the current activity
 * before the server's presence expiry.
 *
 * The same reports drive the session's typing lease. While keystrokes keep
 * coming, `typing` is repeated once per spacing, so the lease is renewed and a
 * trailing report lands after the last keystroke; the host ends the lease after
 * its idle window. A draft that goes empty (cleared, or sent) is reported at
 * once with the content-free `draft: 'empty'` flag, which releases the lease.
 */
import { useCallback, useEffect, useRef } from 'react'
import type { Activity } from '../../api/sharedCrew'
import { sendActivity } from './sharedCrewSocket'

/** Server accepts one activity frame per 2s per connection; stay above it. */
export const MIN_SPACING_MS = 2_500
/** Repeat the current activity well inside the 30s server expiry. */
export const KEEPALIVE_MS = 10_000
/** How long after the last keystroke the tab still reads as typing. */
export const TYPING_HOLD_MS = 5_000

const visible = () => typeof document === 'undefined' || document.visibilityState !== 'hidden'

export function usePresenceReporter(sessionKey: string | null | undefined, enabled: boolean) {
  const current = useRef<Activity>('viewing')
  const lastSent = useRef(0)
  const pending = useRef<ReturnType<typeof setTimeout> | null>(null)
  const typingTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const keyRef = useRef(sessionKey ?? '')
  keyRef.current = sessionKey ?? ''

  /** The activity and session of the last frame that left this tab. */
  const sent = useRef<{ key: string; activity: Activity } | null>(null)
  /** Whether this tab has drafted since its last empty-draft report, so only a
   *  tab that may hold the typing lease reports releasing it. */
  const drafting = useRef(false)

  /** Report that this tab's draft is empty: releases its typing lease at once. */
  const sendCleared = useCallback(() => {
    if (!drafting.current) return
    drafting.current = false
    if (pending.current) { clearTimeout(pending.current); pending.current = null }
    current.current = 'viewing'
    if (!keyRef.current) return
    if (sendActivity(keyRef.current, 'viewing', { draftEmpty: true })) {
      lastSent.current = Date.now()
      sent.current = { key: keyRef.current, activity: 'viewing' }
    }
  }, [])

  const flush = useCallback(() => {
    pending.current = null
    if (!keyRef.current) return
    if (sendActivity(keyRef.current, current.current)) {
      lastSent.current = Date.now()
      sent.current = { key: keyRef.current, activity: current.current }
    }
  }, [])

  const report = useCallback((activity: Activity, force = false) => {
    if (!enabled) return
    const changed = activity !== current.current
    current.current = activity
    if (!changed && !force) return
    // Ending a typing this tab already reported goes at once: the server
    // exempts that one step down from its spacing, so collaborators stop
    // reading "typing" the moment it ends.
    const endsTyping = activity !== 'typing' && sent.current?.activity === 'typing' && sent.current.key === keyRef.current
    const wait = endsTyping ? 0 : MIN_SPACING_MS - (Date.now() - lastSent.current)
    if (wait <= 0) {
      if (pending.current) { clearTimeout(pending.current); pending.current = null }
      flush()
    } else if (!pending.current) {
      pending.current = setTimeout(flush, wait)
    }
  }, [enabled, flush])

  useEffect(() => {
    if (!enabled || !sessionKey) return
    report(visible() ? 'viewing' : 'idle', true)
    const keepAlive = setInterval(() => report(current.current, true), KEEPALIVE_MS)
    const onVisibility = () => report(visible() ? 'viewing' : 'idle')
    document.addEventListener('visibilitychange', onVisibility)
    return () => {
      clearInterval(keepAlive)
      document.removeEventListener('visibilitychange', onVisibility)
      if (pending.current) { clearTimeout(pending.current); pending.current = null }
      if (typingTimer.current) { clearTimeout(typingTimer.current); typingTimer.current = null }
    }
  }, [enabled, sessionKey, report])

  /** Call on composer input. Carries no text by design: only whether the draft
   *  is now empty, which releases the typing lease instead of renewing it. */
  const noteTyping = useCallback((draftEmpty?: boolean) => {
    if (!enabled || !keyRef.current) return
    if (draftEmpty === true) {
      if (typingTimer.current) { clearTimeout(typingTimer.current); typingTimer.current = null }
      sendCleared()
      return
    }
    // Forced: while keystrokes continue, `typing` repeats once per spacing, so
    // the lease stays renewed and its idle window starts at the last keystroke.
    drafting.current = true
    report('typing', true)
    if (typingTimer.current) clearTimeout(typingTimer.current)
    typingTimer.current = setTimeout(() => {
      typingTimer.current = null
      // A tab hidden during the hold stays idle, not viewing.
      report(visible() ? 'viewing' : 'idle')
    }, TYPING_HOLD_MS)
  }, [enabled, report, sendCleared])

  /** Call when the composer's draft is sent: typing ends now, not after the
   *  hold or the send spacing, so collaborators never read "typing" beside
   *  the turn it produced, and the typing lease is released. */
  const noteSent = useCallback((sentSessionKey: string | null) => {
    if (!enabled || !sentSessionKey || sentSessionKey !== keyRef.current) return
    if (typingTimer.current) { clearTimeout(typingTimer.current); typingTimer.current = null }
    sendCleared()
  }, [enabled, sendCleared])

  return { noteTyping, noteSent }
}
