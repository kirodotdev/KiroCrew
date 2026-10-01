/**
 * Threads on one conversation's messages, for whichever surface is showing it.
 *
 * Threads used to arrive only on the Crewmates page, because that page was the
 * only one that built `ThreadHooks` and passed them down. A thread is now an
 * ordinary chat session anchored to a message, which is true of every chat
 * surface — so the wiring lives here, and a host adds threads in one line
 * (`const threads = useThreads(slot, …)`) instead of copying four pieces of
 * state.
 *
 * What it owns:
 *
 *  - the per-message anchor read (`GET /api/chat/threads?slot=`), which feeds
 *    every bubble's footer;
 *  - which thread is open on this surface;
 *  - opening one, which is where the two anchor cases are decided. A message
 *    that already carries an anchor opens with no request at all. A message
 *    that carries none — including a reply that is still STREAMING and has no
 *    `mid` yet — is handed to the backend, which mints the thread's session and
 *    resolves the anchor (for a streaming reply: to the user message that
 *    started the turn, NOTES D4).
 *
 * It deliberately does NOT own the thread's messages. Those are the transcript
 * rows of the thread's own slot, read by the ordinary chat surface.
 */
import { useCallback, useMemo, useRef, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { ApiError } from '../../api/apiError'
import { parseErrorCode, parseErrorField } from '../../utils/errorReport'
import {
  ANCHOR_IN_FLIGHT,
  threadsApi,
  threadQueryKey,
  threadsQueryKey,
  type ThreadSummary,
} from '../../api/threads'
import type { ThreadHooks } from '../../app-sdk/messageRenderers'

/**
 * Refusals that mean "not yet", rather than "not ever".
 *
 * Both are the same underlying fact: a transcript row is written AFTER its turn,
 * so a message that exists on screen may not exist on disk yet, and a slot whose
 * turn is running can move under a creation that is already in flight. Opening a
 * thread on the reply being written right now is exactly when both are likeliest.
 * Measured against a real gateway: on a fresh chat's first turn the open is
 * refused `transcript_missing`, and occasionally `caller_memory_changed`; a moment
 * later, or on any message already on disk, the same call succeeds.
 *
 * So these get their own sentence, which says to try again — a flat "couldn't open
 * a thread" would read as a broken feature at the one moment it is only early.
 */
const TRANSIENT_OPEN_REFUSALS = new Set([
  // The anchored row is not on disk yet. The route answers this code for BOTH of
  // its store outcomes (`missing` and `unflushed`), so there is no second
  // spelling to carry here -- `unflushed` never reaches the wire.
  'transcript_missing',
  // An unmapped admission outcome. Its own sentence ends "Try again", so the
  // route itself classes it as retryable rather than final.
  'thread_open_failed',
  // Surfaced verbatim from `create_session`: the parent slot moved under a
  // creation already in flight.
  'caller_memory_changed',
])

/** The i18n key for a failed open, or '' when nothing failed. */
export function threadOpenErrorKey(code: string): string {
  if (!code) return ''
  return TRANSIENT_OPEN_REFUSALS.has(code)
    ? 'pages.chat.thread.err_open_not_ready'
    : 'pages.chat.thread.err_open_failed'
}

/** The thread this surface is showing. `threadSlot` is absent while it is still
 *  being resolved, and stays absent for a version 1 thread (read-only fold). */
export interface OpenThread {
  mid: string
  threadSlot?: string
}

export interface ThreadsController {
  /** Hands to `ChatPane`/`ChatMessageList` so every row draws its footer and action. */
  hooks: ThreadHooks | undefined
  /** The open thread, or null. */
  open: OpenThread | null
  /** A thread is being minted right now (the anchor did not exist yet). */
  opening: boolean
  /** The backend's refusal code for a failed open, or '' when none failed. The
   *  host turns it into a sentence — see `threadOpenErrorKey`. */
  openError: string
  /** The anchor read failed — the footers are missing, the chat is fine. */
  /** The anchor read SUCCEEDED, so `summaryOf` can be trusted. A deep link must
   *  wait for this: acting on an address before the anchors are known reads every
   *  message as anchorless and asks the backend to mint a thread that already
   *  exists. A failed read is not readiness -- it knows no anchors either. */
  summaryReady: boolean
  summaryFailed: boolean
  summaryRetrying: boolean
  retrySummary: () => void
  /** Open the thread on a message. `undefined` = the row is still streaming.
   *
   *  `read` means the caller is pointing at a thread it can SEE and wants that
   *  one: a footer, or a close card's back-link. Without it the call is "start a
   *  thread here", which on a free message mints one. The difference only shows
   *  on an ENDED thread, where the two intents want opposite things. */
  openThread: (mid: string | undefined, opts?: { read?: boolean }) => void
  /** Show the thread whose session is `threadSlot`, found by its anchor. The
   *  close card's back-link: it knows the slot it named, not the anchored mid.
   *  False when no anchor claims that slot any more -- a later thread on the same
   *  message took it over -- which is the host's cue to open it as a full page. */
  openThreadSlot: (threadSlot: string) => boolean
  /** Dismiss the drawer. A VIEW action: it reaches no thread and ends nothing. */
  close: () => void
  /** End the thread on `mid`: close its anchor and post the summary card in the
   *  parent. The drawer is dismissed once that lands, so a failure leaves the
   *  thread open and on screen instead of hiding a close that did not happen. */
  endThread: (mid: string) => void
  /** An end is in flight. */
  ending: boolean
  /** The end was refused, as an i18n key, or '' when nothing failed. */
  endError: string
  summaryOf: (mid: string) => ThreadSummary | undefined
}

export function useThreads(
  slot: string | undefined,
  opts: { enabled?: boolean; crewmateName: string; title?: string },
): ThreadsController {
  const qc = useQueryClient()
  const enabled = opts.enabled !== false && !!slot
  const [open, setOpen] = useState<OpenThread | null>(null)
  const [opening, setOpening] = useState(false)
  const [openError, setOpenError] = useState('')
  // Read inside the open's own callbacks, where `slot` is the value captured when
  // the request went out rather than the surface showing now.
  const slotRef = useRef(slot)
  slotRef.current = slot
  // Which open is the live one. The slot check alone lets a SECOND open on the
  // SAME chat be overtaken by the first one's answer: both were asked for by this
  // surface, so both pass it, and the slower reply installs the older thread as
  // the drawer while the reader is already typing into the newer one. Two clicks
  // inside one mint round trip is ordinary impatience -- the message list stays
  // live for its whole duration -- so each open takes a number and only the
  // newest one may write state.
  const openSeqRef = useRef(0)
  // Read inside a close's own callbacks, where `open` is the value captured when the
  // request went out rather than the drawer showing now.
  const openRef = useRef(open)
  openRef.current = open
  const [ending, setEnding] = useState(false)
  const [endError, setEndError] = useState('')

  // Every piece of state above belongs to ONE parent slot, so the controller drops
  // all of it when its own slot changes. Both hosts do clear it -- the chat page
  // through the `?thread=` the session switch deletes, the Crewmates page on
  // `confirmedSlot` -- but that makes the invariant a thing each host has to
  // remember for it, and a host that forgets leaves a drawer open over the wrong
  // conversation with a composer that writes into the thread it came from. The
  // controller owns the invariant here, where its own key is what moved.
  //
  // During render rather than in an effect: an effect would let one frame paint the
  // previous parent's drawer over the new one. React re-renders on this without
  // committing, and each setter bails out when its value is already correct, so a
  // render where the slot did not change costs nothing.
  const lastSlotRef = useRef(slot)
  if (lastSlotRef.current !== slot) {
    lastSlotRef.current = slot
    // Invalidates a mint in flight too: `live()` asks whether its own `seq` is
    // still the newest, and this makes it not.
    openSeqRef.current += 1
    setOpen(null)
    setOpening(false)
    setOpenError('')
    setEnding(false)
    setEndError('')
  }

  /**
   * Show a thread NOW, and invalidate any mint still in flight.
   *
   * Every immediate open goes through here, and the sequence bump is the whole
   * reason: a pending mint's `live()` asks only whether its own `seq` is still
   * the newest one, so an arm that installed the drawer without advancing it
   * left the mint free to overwrite the drawer when it resolved -- and the
   * reader's next message would be typed into the thread they had just
   * navigated away from. One click on another thread inside one mint round trip
   * is ordinary timing, not a race a reader has to avoid.
   *
   * A helper rather than a bump at each call site, because forgetting the bump
   * is exactly the defect: a new arm gets it by construction.
   */
  const showThread = useCallback((next: OpenThread) => {
    openSeqRef.current += 1
    setOpen(next)
  }, [])

  const query = useQuery({
    queryKey: threadsQueryKey(slot || ''),
    queryFn: () => threadsApi.summary(slot as string),
    enabled,
    staleTime: 30_000,
  })
  const anchors = query.data?.threads
  const summaryOf = useCallback((mid: string) => anchors?.[mid], [anchors])

  const close = useCallback(() => {
    setOpen(null)
    setOpenError('')
    setEndError('')
  }, [])

  const endThread = useCallback(
    (mid: string) => {
      if (!slot || !mid) return
      // The thread on screen, named to the backend. A drawer can outlive the thread
      // it shows -- another tab or an agent can end this message's thread and open
      // another on it -- and the mid alone says which MESSAGE, never which thread,
      // so an unnamed close would end whichever one is there when it lands.
      // Only a SESSION anchor has a slot to name. A version 1 fold has nothing to
      // end, so the narrowing is the rule rather than a type appeasement.
      const anchor = anchors?.[mid]
      const fromIndex = anchor?.kind === 'session' ? anchor.thread_slot : ''
      const showing = open?.mid === mid ? open.threadSlot : fromIndex
      if (!showing) return
      setEndError('')
      setEnding(true)
      // The close is a round trip, and the reader can switch chats or open another
      // thread inside it. Dismissing the drawer on the answer alone would dismiss
      // whatever is open WHEN it lands, so every state write below is scoped: the
      // same surface, and still the same thread. The query invalidations are not --
      // they name the slot they were asked for, and a refreshed anchor read is
      // correct for that chat whether or not the reader is still looking at it.
      const endedFor = slot
      const endedThread = showing
      const stillMine = () =>
        endedFor === slotRef.current && (!openRef.current || openRef.current.threadSlot === endedThread)
      threadsApi
        .close(slot, mid, showing)
        .then(() => {
          // The anchor read feeds every footer, so it is refreshed before the
          // drawer goes: the row the user is about to look at should already say
          // Ended rather than settle into it a moment later.
          void qc.invalidateQueries({ queryKey: threadsQueryKey(endedFor) })
          void qc.invalidateQueries({ queryKey: threadQueryKey(endedFor, mid) })
          if (stillMine()) setOpen(null)
        })
        .catch((err: unknown) => {
          // `already_closed` is not a failure: another tab or the thread's own
          // agent ended it between the render and the click. The outcome the user
          // asked for holds, so the drawer closes and the footers are refreshed.
          if (err instanceof ApiError && parseErrorCode(err.body) === 'already_closed') {
            void qc.invalidateQueries({ queryKey: threadsQueryKey(endedFor) })
            if (stillMine()) setOpen(null)
            return
          }
          if (stillMine()) setEndError('pages.chat.thread.err_end_failed')
        })
        .finally(() => { if (stillMine()) setEnding(false) })
    },
    [slot, qc, open, anchors],
  )

  const openThread = useCallback((mid: string | undefined, openOpts?: { read?: boolean }) => {
    if (!slot) return
    setOpenError('')
    if (mid) {
      const anchor = anchors?.[mid]
      // A thread still OPEN here: show it, with no request at all.
      //
      // An ENDED one depends on which intent asked. A footer saying "Thread --
      // Ended" and a close card naming that thread are both a pointer at a thread
      // the reader can see, so pressing one means "let me read it" and it opens,
      // ended and readable, with its composer saying what typing there does. The
      // row's own **Reply in thread** on a message whose thread has ended means
      // the other thing -- closing releases the message, so that starts a new
      // thread and falls through to the request below.
      if (anchor?.kind === 'session' && (!anchor.closed_at || openOpts?.read)) {
        showThread({ mid, threadSlot: anchor.thread_slot })
        return
      }
      // Only version 1 replies. Shown as the read-only fold rather than turned
      // into a session, because a session minted for them would assert a history
      // it never had (NOTES D3). Starting a real thread on the same message is
      // the fold's own action: it arrives back here with the fold already open,
      // and falls through to the request below.
      if (anchor?.kind === 'legacy' && open?.mid !== mid) {
        showThread({ mid })
        return
      }
    }
    // No anchor yet, or no id to anchor to. The backend mints the thread's
    // session and answers with both. This never refuses because the parent's
    // turn is running: opening creates a sibling session and never addresses
    // the running turn (NOTES D5).
    setOpening(true)
    // The slot this open belongs to, captured now. A mint is a round trip, and the
    // reader can switch chats inside it: applying the answer blind would install
    // chat A's thread as chat B's open drawer, and B's next message would be typed
    // into A's transcript. Every arm below goes through `live()`, which asks both
    // questions -- right chat, newest open -- because either one alone admits a
    // stale answer.
    const openedFor = slot
    const seq = ++openSeqRef.current
    // Both conditions, every arm: the right chat AND the newest open on it.
    const live = () => openedFor === slotRef.current && seq === openSeqRef.current
    threadsApi
      .open(slot, mid ?? ANCHOR_IN_FLIGHT, opts.title ? { title: opts.title } : undefined)
      .then((opened) => {
        if (!live()) return
        setOpen({ mid: opened.anchor.mid, threadSlot: opened.thread_slot })
        void qc.invalidateQueries({ queryKey: threadsQueryKey(openedFor) })
      })
      .catch((err: unknown) => {
        // `already_open` is not a failure: another tab (or an agent) opened this
        // thread between the anchor read and the click, and the refusal names
        // the slot. Show it instead of reporting a problem the user has none of.
        if (err instanceof ApiError) {
          // `ApiError.body` is the RAW response text, so the field is read
          // through the shared parser rather than indexed as an object.
          const slotKey = parseErrorField(err.body, 'thread_slot')
          if (slotKey && mid && live()) { setOpen({ mid, threadSlot: slotKey }); void qc.invalidateQueries({ queryKey: threadsQueryKey(openedFor) }); return }
        }
        // One plain sentence, and nothing opens. There is no draft to lose here
        // (a thread's composer belongs to the thread's own slot, which does not
        // exist yet), so a failed open leaves the chat exactly as it was. The
        // backend's own code is reported for the journal, not shown: every
        // refusal of an open means the same thing to the reader.
        if (!live()) return
        setOpenError(err instanceof ApiError ? (parseErrorCode(err.body) || 'failed') : 'failed')
      })
      // `opening` is cleared only by the newest open: an overtaken one settling
      // would otherwise report the surface idle while a mint is still in flight.
      .finally(() => { if (live()) setOpening(false) })
  }, [slot, anchors, open?.mid, opts.title, qc, showThread])

  // The close card names a thread SLOT; the drawer is keyed by the anchored mid.
  // One anchor holds any one thread slot, so the reverse lookup is exact.
  //
  // It can also MISS, and that case is ordinary rather than broken: closing frees
  // the message, so a later thread on it takes the anchor over and every older
  // card's slot then matches nothing. The answer is false, not a no-op -- the
  // thread is still a real session, and the host opens it as a full page instead.
  // Guessing a mid, or leaving the control inert, both lose a conversation the card
  // is pointing straight at.
  const openThreadSlot = useCallback((threadSlot: string): boolean => {
    if (!threadSlot) return false
    const hit = Object.entries(anchors ?? {}).find(
      ([, a]) => a.kind === 'session' && a.thread_slot === threadSlot,
    )
    if (!hit) return false
    setOpenError('')
    showThread({ mid: hit[0], threadSlot })
    return true
  }, [anchors, showThread])

  const hooks = useMemo<ThreadHooks | undefined>(
    () => (enabled && slot
      ? { summaryOf, onOpen: openThread, onOpenSlot: openThreadSlot, crewmateName: opts.crewmateName }
      : undefined),
    [enabled, slot, summaryOf, openThread, openThreadSlot, opts.crewmateName],
  )

  return {
    hooks,
    open,
    opening,
    openError,
    // SUCCESS only, never `isError`. A deep link waits on this, and with a failed
    // read counted as ready the anchors are unknown while the address is acted on:
    // every message reads as anchorless, so `openThread(mid, {read: true})` asks the
    // backend to mint a thread on a message that already has one -- and on an ENDED
    // thread that answers a reload with a brand-new conversation, leaving the ended
    // one unreachable from its own URL. A failed read holds the deep link instead,
    // and the reader is not stranded: `summaryFailed` shows the failure beside the
    // chat with a Retry, which is the path back.
    summaryReady: !enabled || query.isSuccess,
    summaryFailed: query.isError,
    summaryRetrying: query.isFetching,
    retrySummary: () => { void query.refetch() },
    openThread,
    openThreadSlot,
    close,
    endThread,
    ending,
    endError,
    summaryOf,
  }
}
