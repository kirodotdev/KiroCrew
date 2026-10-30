/**
 * The Stop button's press protocol, shared by the dashboard's ChatPane and the
 * app SDK's ChatEmbed. Store-free: the host hands in how to send the stop
 * (`sendStop`), so ChatPane keeps the Redux `requestStop` thunk and the embed
 * keeps its scoped app API.
 *
 * The protocol (ChatPane's, #9547):
 *
 * - The first press is the cooperative soft cancel; a press while the slot is
 *   `soft_pending` (or a stalled `killing`) escalates to the hard kill, and a
 *   double-tap inside the arming window is ignored. `handleStopPress` is the
 *   decision.
 * - The server's `soft_pending` lags the press (a WS round-trip, or a poll), so
 *   the host's own soft press is held as an OPTIMISTIC `soft_pending` until the
 *   server reports a phase of its own or the turn ends. Without it a second
 *   press inside the lag reads as a fresh soft press, and the backend escalates
 *   any second press to a queue-clearing hard kill, silently.
 * - A press that never got an answer (`lost`) may have armed the cancel, so
 *   the pending state stays and the retry is a deliberate force (GPT round 8).
 *   A definite refusal (`refused`) armed nothing: the stamp and the pending
 *   state reset, so the retry is a fresh soft press.
 * - Everything is about ONE slot: a re-point resets it, and a reply for the
 *   slot the host used to show is dropped (GPT round 9).
 *
 * Two opt-in extensions, both off unless the host asks:
 *
 * - `turnScoped`: everything is also about ONE turn. A host that learns the
 *   run state from a poll can miss the end of a stopped turn entirely (the
 *   queue drain hands the floor straight to the next one), so `turnKey`
 *   changing starts over (keeping only the last press's arming stamp, so a
 *   double-tap across the handoff is still one press), and only the latest press of the current turn may
 *   act on its reply: a late failure for the previous turn's press must not
 *   clear the new turn's arming stamp, or a double-tap would force-stop it.
 * - A `declined` outcome (a soft press the backend refused because automatic
 *   compaction holds the session, which arms its reset hatch for the next
 *   press) drops the optimistic `soft_pending` and arms `stopDeclined` for the
 *   backend's window. `serverStopDeclined` (the backend's own marker) arms the
 *   same warning. The press after a decline is sent as a plain stop: the
 *   backend's marker is what turns it into the reset.
 *
 * ChatPane passes neither, so its behaviour is the protocol above unchanged.
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import { handleStopPress, isEscalationState } from '../../utils/stopDebounce'
import { STOP_DECLINED_HINT_MS } from '../../hooks/useStopDeclinedHint'
import { i18nT } from '../../i18n/t'

export type StopPhase = 'idle' | 'soft_pending' | 'killing'

/** What became of one stop request, as the host's transport saw it. */
export type StopOutcome =
  | { kind: 'ok' }
  /** Declined while automatic compaction holds the session; nothing stops. */
  | { kind: 'declined' }
  /** The backend answered and said no: nothing was armed. */
  | ({ kind: 'refused' } & StopRefusal)
  /** No answer: the request may have reached the backend and armed the cancel. */
  | { kind: 'lost' }

/** What the transport read off a refused stop. `error` is the backend's own
 *  text; `status`, `code` and `authRequired` say which known reason it is. */
export interface StopRefusal {
  error: string
  status?: number
  code?: string
  authRequired?: boolean
  /** An access proxy answered with its own sign-in page: `error` is already
   *  the proxy remedy, and the gateway's sign-in cannot clear it. */
  edgeChallenge?: boolean
}

/**
 * The words a refused stop shows (#20398). A known reason gets a plain sentence
 * that says what to do, and its backend text is dropped: it adds nothing the
 * sentence does not say. Any other refusal keeps the backend text as
 * `detail`, a secondary line, because then it is the only clue to the cause.
 */
export function stopRefusalCopy(r: StopRefusal): { message: string; detail: string } {
  if (r.edgeChallenge) return { message: r.error, detail: '' }
  if (r.authRequired) return { message: i18nT('components.chatPane.stop_failed_signed_out'), detail: '' }
  if (r.code === 'slot_not_found' || r.status === 404) {
    return { message: i18nT('components.chatPane.stop_failed_not_found'), detail: '' }
  }
  if (typeof r.status === 'number' && r.status >= 500) {
    return { message: i18nT('components.chatPane.stop_failed_server'), detail: r.error }
  }
  return { message: i18nT('components.chatPane.stop_failed'), detail: r.error }
}

export interface UseStopPressOptions {
  slotKey: string
  /** A turn (or the slot's work) is live, so Stop is meaningful. */
  busy: boolean
  serverStopState?: StopPhase | null
  sendStop: (force: boolean) => Promise<StopOutcome>
  /** Scope the press state to one turn as well as one slot (see above). */
  turnScoped?: boolean
  /** The server's turn identity; undefined when the read carries none. */
  turnKey?: string
  /** The server's own compaction-decline marker. */
  serverStopDeclined?: boolean
}

export function useStopPress({
  slotKey, busy, serverStopState, sendStop, turnScoped = false, turnKey, serverStopDeclined = false,
}: UseStopPressOptions) {
  const softStopAtRef = useRef(0)
  const [optimistic, setOptimistic] = useState(false)
  const [error, setError] = useState('')
  // The backend's own text under the plain message, for an unknown reason only.
  const [errorDetail, setErrorDetail] = useState('')
  // When the last press was declined for a compaction (0 when it was not).
  const [declinedAt, setDeclinedAt] = useState(0)
  // Which press, and which turn of which slot, a reply is about.
  const pressSeqRef = useRef(0)
  const turnEpochRef = useRef(0)
  const slotRef = useRef(slotKey)

  // `keepStamp`: a new turn keeps the last press's time. The press's catch-up
  // read is often the one that reports the queued successor, so clearing the
  // stamp there would let the second tap of a double-click land as a force
  // stop on the successor and drop the queue. A new slot clears it.
  const startOver = useCallback((keepStamp = false) => {
    turnEpochRef.current += 1
    if (!keepStamp) softStopAtRef.current = 0
    setOptimistic(false)
    setDeclinedAt(0)
    setError('')
  }, [])

  useEffect(() => {
    slotRef.current = slotKey
    startOver()
  }, [slotKey, startOver])

  // turnScoped: a new turn starts with no press of its own. A read with no
  // identity (an admission-only reservation) neither changes the turn nor
  // forgets the last one, so a press made during admission survives the
  // numbering of the turn it was aimed at.
  const turnRef = useRef<{ slot: string; turn?: string }>({ slot: slotKey, turn: turnKey })
  useEffect(() => {
    if (!turnScoped || turnKey === undefined) return
    const prev = turnRef.current
    turnRef.current = { slot: slotKey, turn: turnKey }
    if (prev.slot !== slotKey || prev.turn === undefined || prev.turn === turnKey) return
    startOver(true)
  }, [turnScoped, slotKey, turnKey, startOver])

  useEffect(() => {
    if (!optimistic) return
    if (!busy || isEscalationState(serverStopState)) setOptimistic(false)
  }, [optimistic, busy, serverStopState])

  // turnScoped: a finished turn retires its failure notice and its decline,
  // and a reply that lands after it is about a turn that is over.
  useEffect(() => {
    if (!turnScoped || busy) return
    turnEpochRef.current += 1
    setError('')
    setDeclinedAt(0)
  }, [turnScoped, busy])

  // A stop the server reports under way supersedes the decline.
  useEffect(() => { if (isEscalationState(serverStopState)) setDeclinedAt(0) }, [serverStopState])
  // The backend's marker expires after its window; so does the warning.
  useEffect(() => {
    if (!declinedAt) return
    const timer = setTimeout(() => setDeclinedAt(0), STOP_DECLINED_HINT_MS)
    return () => clearTimeout(timer)
  }, [declinedAt])

  const stopState: StopPhase =
    optimistic && !isEscalationState(serverStopState) ? 'soft_pending' : (serverStopState ?? 'idle')
  // The host's own optimistic soft_pending does not retire it: while the press
  // after a decline is unanswered it may still be the reset.
  const stopDeclined = busy && !isEscalationState(serverStopState) && (declinedAt > 0 || serverStopDeclined)

  const stop = useCallback((force: boolean) => {
    setError('')
    const sentFor = slotKey
    const seq = ++pressSeqRef.current
    const epoch = turnEpochRef.current
    const stale = () =>
      slotRef.current !== sentFor || (turnScoped && (seq !== pressSeqRef.current || epoch !== turnEpochRef.current))
    void sendStop(force).then((outcome) => {
      if (stale()) return
      switch (outcome.kind) {
        case 'ok':
          // The press landed (a stop, or the hatch's reset): the decline is spent.
          setDeclinedAt(0)
          return
        case 'declined':
          // Nothing is stopping, and the next press is the reset. The stamp
          // keeps the declined press's time, so a double-tap cannot spend the
          // hatch inside the arming window.
          setOptimistic(false)
          setDeclinedAt(Date.now())
          return
        case 'lost':
          setError(i18nT('components.chatPane.stop_failed_network'))
          setErrorDetail('')
          return
        case 'refused': {
          softStopAtRef.current = 0
          setOptimistic(false)
          const copy = stopRefusalCopy(outcome)
          setError(copy.message)
          setErrorDetail(copy.detail)
        }
      }
    })
  }, [slotKey, sendStop, turnScoped])

  const onStop = useCallback(() => {
    const soft = () => { setOptimistic(true); stop(false) }
    handleStopPress(
      // After a decline the press is the hatch: debounced like a second press,
      // but sent as a plain stop.
      isEscalationState(stopState) || stopDeclined,
      Date.now(),
      softStopAtRef,
      soft,
      () => {
        if (!stopDeclined) { stop(true); return }
        softStopAtRef.current = Date.now()
        soft()
      },
    )
  }, [stop, stopState, stopDeclined])

  const dismissStopError = useCallback(() => setError(''), [])

  return { stopState, onStop, stopError: error, stopErrorDetail: error ? errorDetail : '', dismissStopError, stopDeclined }
}
