/**
 * Mark a cron run's bell notes read when the user has seen the run (#23564).
 *
 * A scheduled job's run lands in its own session and posts a `cron` note to the
 * bell. Leaving that note unread once the user had looked at the run made the
 * bell claim something still needed attention.
 *
 * What counts as "seen" is two events, never a reading of state:
 *
 *  1. The user OPENS the cron chat by a gesture (a sidebar row, a tab, the
 *     palette, the note's own "go to chat": every switch marked `gesture`;
 *     a restore, a URL, a late `?sid=` recovery or a forwarded nav intent is
 *     not a gesture),
 *     and its transcript then shows in a mounted chat pane. Every unread cron
 *     note of that chat is read, once, at that moment.
 *  2. A cron note arrives LIVE (`MC_LIVE_NOTIFICATION_EVENT`, never the boot
 *     fetch or a reconnect replay) while a mounted chat pane shows that
 *     chat's transcript. That one note is read.
 *
 * Both also need the user to be looking: a chat route, and the pane-visible
 * signal of `paneVisibility` (a visible, focused window and nothing over the
 * pane: no modal dialog, no `data-covers-pane` drawer, and none of the covers
 * the host passes as `hidden`, i.e. a remote Crew, a crew window or the bell's
 * detail card). If any of those fails at the moment of the event, the note
 * stays unread; nothing is retried later.
 *
 * So a reload or a restored chat reads nothing, a chat still loading or whose
 * load failed reads nothing (no pane shows it), an open the user left for
 * another page or another chat before its transcript showed reads nothing,
 * also when that pane is restored later, a split pane that was closed
 * reads nothing (it is no longer mounted), a note that arrives in a background
 * tab stays unread, and a user's "Mark unread" is never undone, since neither
 * event fires for it.
 *
 * Only `cron` notes are read this way. Every other kind (an approval, a
 * crewmate's question) still needs an explicit action. Mark-read only: the
 * cron "Acknowledge" button (`CronAckBar`) also tells the job not to repeat the
 * result in later runs, and that stays a deliberate press.
 */
import { useEffect, useRef, useSyncExternalStore } from 'react'
import { useStore } from 'react-redux'
import { useLocation } from 'react-router-dom'
import { useAppDispatch, useAppSelector, type RootState } from '../store'
import { ackNotification } from '../store/notificationsSlice'
import type { Notification } from '../types'
import { isChatPath } from './notificationBanner'
import { MC_LIVE_NOTIFICATION_EVENT, type McLiveNotificationDetail } from './notificationEvent'
import { paneVisibleNow } from './paneVisibility'

/** How long a gesture open waits for the cover its own gesture is taking away
 *  (a drawer, the bell sheet sliding out: 0.45s at most), and how often it asks. */
export const OPEN_SETTLE_MS = 1200
export const OPEN_SETTLE_POLL_MS = 100

/** The `ts` of every unread cron note for *slot*. */
export function unreadCronNotesFor(notes: readonly Notification[], slot: string | null | undefined): string[] {
  if (!slot) return []
  return notes.filter(n => n.kind === 'cron' && !n.acked && n.slot === slot).map(n => n.ts)
}

// Chats whose transcript a mounted pane is showing in this document, one
// registrant per pane: two split panes can show one chat.
type Registrant = { element?: () => Element | null }
const shownTranscripts = new Map<string, Set<Registrant>>()
const shownListeners = new Set<() => void>()
let shownVersion = 0

function bumpShown() {
  shownVersion += 1
  shownListeners.forEach(l => l())
}

/** Whether a mounted chat pane in this document shows *slot*'s transcript. */
export function transcriptShownFor(slot: string | null | undefined): boolean {
  return !!slot && (shownTranscripts.get(slot)?.size ?? 0) > 0
}

/**
 * The transcript elements of the panes that registered *slot*, read now, for
 * the on-screen backstop (`paneVisibleNow`). `undefined` when a registrant gave
 * no element to probe, which only a test harness does: the panes always do.
 */
function transcriptElementsFor(slot: string): Element[] | undefined {
  const elements: Element[] = []
  for (const r of shownTranscripts.get(slot) ?? []) {
    if (!r.element) return undefined
    const el = r.element()
    if (el) elements.push(el)
  }
  return elements
}

/**
 * Called by every chat pane: *shown* is true while the pane is mounted with
 * *slot*'s transcript on it (loaded, at least one row). The single chat pane
 * (`ChatTranscriptView`) and each split pane (`ChatPane`) both call it, so a
 * pane that closes, or a chat that has not loaded, does not count.
 *
 * *element* returns the pane's transcript element at the moment of an event,
 * so the hook can confirm it is actually on screen (`paneVisibleNow`): that is
 * what catches a cover nobody marked.
 */
export function useMarkTranscriptShown(slot: string | null | undefined, shown: boolean, element?: () => Element | null): void {
  // Read through a ref so a new getter each render does not re-register.
  const elementRef = useRef(element)
  elementRef.current = element
  const probed = element !== undefined
  useEffect(() => {
    if (!slot || !shown) return
    const registrant: Registrant = probed ? { element: () => elementRef.current?.() ?? null } : {}
    const set = shownTranscripts.get(slot) ?? new Set<Registrant>()
    set.add(registrant)
    shownTranscripts.set(slot, set)
    bumpShown()
    return () => {
      set.delete(registrant)
      if (set.size === 0 && shownTranscripts.get(slot) === set) shownTranscripts.delete(slot)
      bumpShown()
    }
  }, [slot, shown, probed])
}

function subscribeShown(listener: () => void) {
  shownListeners.add(listener)
  return () => { shownListeners.delete(listener) }
}

export function useAckCronNotesInView({ hidden = false }: { hidden?: boolean } = {}): void {
  const dispatch = useAppDispatch()
  const reduxStore = useStore<RootState>()
  const { pathname } = useLocation()
  const shownTick = useSyncExternalStore(subscribeShown, () => shownVersion, () => 0)
  const userOpen = useAppSelector(s => s.chat.userOpen)
  const activeSlot = useAppSelector(s => s.chat.activeSlot)

  // Read at event time by the listener below, which outlives a render.
  const looking = useRef({ hidden, pathname })
  looking.current = { hidden, pathname }
  // The pane-visible signal (see `paneVisibility`): the covers this hook's host
  // knows (`hidden`), a chat route, the window and document-level covers, and
  // the backstop that the chat's own transcript element is on screen.
  // *slot*'s transcript elements feed the on-screen backstop.
  const userLooking = (slot: string) => !looking.current.hidden && isChatPath(looking.current.pathname) && paneVisibleNow(transcriptElementsFor(slot))

  // Event 1. An open that lands before its pane shows the transcript waits for
  // it; the first moment the transcript is shown decides, and a later open
  // replaces it. The open in the store at mount is history (it is null on a
  // fresh load), so it never counts.
  //
  // A waiting open belongs to the navigation that made it. Leaving the chat
  // route, or moving to another chat, before its transcript shows drops it:
  // otherwise a pane restored later (back from the bell, back to that chat by
  // any other way) would read the note again, after a "Mark unread" made in
  // between. An open whose request has not fulfilled yet is not recorded, so
  // this cannot drop it; that leave-and-return window is #24080.
  //
  // The open is the user's own gesture, and it is often the very gesture that
  // takes a cover away: a tap on a session row in the phone drawer opens the
  // chat AND closes the drawer, whose scrim stays hit-testable through its
  // slide-out. So once the transcript shows, a check that fails only on
  // visibility keeps the open pending and asks again every
  // OPEN_SETTLE_POLL_MS, for at most OPEN_SETTLE_MS after it first showed.
  // Leaving the chat in that window still drops it at once, and past it the
  // open is dropped for good: a cover that outlasts the window was not the
  // gesture's own, and the open never revives later.
  const consumedSeq = useRef(userOpen?.seq ?? 0)
  const settle = useRef<{ seq: number; until: number } | null>(null)
  useEffect(() => {
    if (!userOpen || userOpen.seq <= consumedSeq.current) return
    if (!isChatPath(pathname) || activeSlot !== userOpen.key) {
      consumedSeq.current = userOpen.seq
      return
    }
    if (!transcriptShownFor(userOpen.key)) return
    const open = userOpen
    if (settle.current?.seq !== open.seq) settle.current = { seq: open.seq, until: Date.now() + OPEN_SETTLE_MS }
    const until = settle.current.until
    let timer: ReturnType<typeof setTimeout> | null = null
    const attempt = () => {
      timer = null
      if (open.seq <= consumedSeq.current) return
      if (userLooking(open.key)) {
        consumedSeq.current = open.seq
        const notes = reduxStore.getState().notifications.items
        for (const ts of unreadCronNotesFor(notes, open.key)) void dispatch(ackNotification(ts))
        return
      }
      if (Date.now() >= until) { consumedSeq.current = open.seq; return }
      timer = setTimeout(attempt, OPEN_SETTLE_POLL_MS)
    }
    attempt()
    return () => { if (timer !== null) clearTimeout(timer) }
    // `shownTick` re-runs this when a pane starts showing the opened chat;
    // `pathname` and `activeSlot` when the user leaves it first.
  }, [dispatch, reduxStore, userOpen, shownTick, pathname, activeSlot])

  // Event 2. A live arrival for a chat a mounted pane is showing.
  useEffect(() => {
    const onLive = (e: Event) => {
      const note = (e as CustomEvent<McLiveNotificationDetail>).detail?.note
      if (!note || note.kind !== 'cron' || note.acked || !note.slot || !transcriptShownFor(note.slot)) return
      if (!userLooking(note.slot)) return
      // The store holds the arrival already (useWebSocket dispatches it first);
      // a note gone from it was cleared or deleted, so there is nothing to read.
      if (!reduxStore.getState().notifications.items.some(n => n.ts === note.ts && !n.acked)) return
      void dispatch(ackNotification(note.ts))
    }
    window.addEventListener(MC_LIVE_NOTIFICATION_EVENT, onLive)
    return () => window.removeEventListener(MC_LIVE_NOTIFICATION_EVENT, onLive)
  }, [dispatch, reduxStore])
}
