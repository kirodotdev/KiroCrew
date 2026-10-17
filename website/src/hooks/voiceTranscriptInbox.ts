/**
 * Module-scoped hand-off for a batch transcription that outlives the component
 * which started it.
 *
 * Navigating away from Chat unmounts the voice hook while the `/api/stt`
 * request is still in flight. The request is a plain fetch, so it completes
 * regardless — only its DELIVERY needs somewhere to land, because the callback
 * it was going to invoke belongs to a page that no longer exists. Progress and
 * results are routed here instead: to the hook instances that are mounted at the
 * time if there are any, otherwise held until the next instance subscribes.
 * That is what carries a transcript across a trip to Settings.
 *
 * Every request carries an id, and a subscriber is told when one BEGINS as well
 * as when it settles. Both exist for the same reason: an instance must be able
 * to tell "the request I am displaying as busy" from "some other request", so a
 * late settlement can never blank the state of a session that has started since.
 *
 * Deliberately NOT a mic owner and NOT a persistence layer: the microphone, the
 * MediaRecorder and the streaming socket still stop on unmount, so leaving Chat
 * never leaves a recording running without any UI to see or stop it. Only the
 * pending request lives here, plus the audio of the latest failed one (see
 * `failed`), and only until the tab unloads.
 *
 * The caller refuses to start a session while a transcription is in flight, so
 * at most one request is ever in flight — but several RESULTS can be waiting for
 * a composer (see `held`): a settled, unowned result is not in flight any more,
 * and the next dictation may settle unowned too.
 *
 * Subscribers are a SET, not a slot (chat-core P3-b). Every composer that can
 * dictate mounts its own voice hook — the main chat, each split pane, each Crew
 * Members DM — and they co-mount routinely (the session grid keeps ChatPage's
 * hook alive under N panes). `begin` fans out so every instance reads the same
 * global "a transcription is in flight" fact; that is what disables the mic on
 * every other composer while one is busy. `settle` releases busy on every
 * instance but delivers the TEXT once: to the instance whose composer owns the
 * session (`owns`), and only when none claims it, to every instance — the legacy
 * single-subscriber shape, which each host's own routing (ChatPage: append to
 * that slot's persisted draft; a pane: drop) then decides.
 */

/** A transcription request in flight. */
export interface PendingTranscription {
  id: number
  /** Slot that owned the recording, so the busy state shows on the right session. */
  sessionId: string | null
}

/**
 * The audio of a request that failed, kept so the user can try it again, save
 * it, or let it go instead of losing what they said.
 */
export interface FailedCapture {
  blob: Blob
  /** File extension of the recorder's container (`webm`, `ogg`, `mp4`). */
  ext: string
  /**
   * True when posting the same audio again can succeed: the request never
   * reached the transcriber, or the transcriber failed on its side. False when
   * the gateway refused the audio itself (too large, too long), where the same
   * bytes get the same answer and only saving the recording helps.
   */
  retryable: boolean
}

/** Terminal outcome of one request. Exactly one is delivered per `beginTranscription`. */
export interface TranscriptResult {
  /** Matches the `PendingTranscription` this settles. */
  id: number
  /** Transcribed text. Absent when the request failed or returned nothing. */
  text?: string
  /** Already-localized failure message, surfaced as the hook's `error`. */
  error?: string
  sessionId: string | null
  /**
   * The audio of a failed request. Settling a result that carries one makes it
   * the inbox's single held capture (see `failedCapture`).
   */
  capture?: FailedCapture
}

export interface TranscriptSink {
  begin: (request: PendingTranscription) => void
  /**
   * `deliver` is false when another subscriber claimed the transcript: release
   * this instance's busy state for the request, surface an error if any, but do
   * NOT hand the text to the host — it would land in two composers.
   */
  settle: (result: TranscriptResult, deliver: boolean) => void
  /**
   * True when this subscriber's composer is the one on screen for `sessionId`.
   * Optional: a subscriber without it never claims and only receives the text on
   * the unclaimed fallback path.
   */
  owns?: (sessionId: string | null) => boolean
  /**
   * True when this subscriber can take a transcript for a session it does NOT
   * show — ChatPage appends it to that slot's persisted draft. A subscriber
   * without this (a pane: its composer is its slot's, it has no draft store)
   * would drop an unowned transcript, so unowned text is never handed to it;
   * when nobody can take it the inbox keeps it until an owner appears.
   */
  acceptsUnowned?: boolean
  /**
   * The inbox has let go of the capture of failed request `id`: it was discarded,
   * taken for a retry, or dropped for a new recording. A subscriber offering
   * Retry, Save or Discard for it withdraws the offer and its notice.
   */
  release?: (id: number) => void
}

const sinks = new Set<TranscriptSink>()
/**
 * Results waiting for a composer, in settle order. A LIST, not a slot: with
 * composers that switch slots, two dictations can settle unowned back to back
 * (dictate in pane A, switch it to B before `/api/stt` answers, dictate again,
 * switch again) and a single slot would let the second overwrite the first —
 * dictated text lost for good. Each entry waits for its own session's composer
 * and lands independently. Bounded so a tab that never returns to a session
 * cannot grow it without limit; past the cap the OLDEST goes, which is the one
 * least likely to still be wanted.
 */
const held: TranscriptResult[] = []
const HELD_MAX = 16
let inFlight: PendingTranscription | null = null
let nextId = 0
/**
 * The one failed capture the tab holds, if any, with the failure it belongs to.
 * One, not a list: it exists so the notice in front of the user can act on it,
 * and only the latest failure has a notice. It lives in module memory and
 * nowhere else, so the audio is gone when the tab unloads; before that it is
 * dropped on Discard, on a retry (which carries it again only if that attempt
 * fails too) and when a new recording starts capturing. Kept whole so a
 * composer that mounts later (a trip to Settings and back) is shown the same
 * notice and can still reach the audio.
 */
let failed: TranscriptResult & { capture: FailedCapture } | null = null

function dropFailed(): void {
  if (!failed) return
  const { id } = failed
  failed = null
  // A failure that settled with no composer mounted still waits in `held`; it
  // must not bring the audio back when it is finally delivered.
  const i = held.findIndex(r => r.id === id)
  if (i >= 0) held[i] = { ...held[i], capture: undefined }
  for (const s of sinks) s.release?.(id)
}

function hold(result: TranscriptResult): void {
  held.push(result)
  if (held.length > HELD_MAX) held.splice(0, held.length - HELD_MAX)
}

/** Try every held result against the live subscribers; keep what still has no home. */
function flushHeld(releaseBusy: boolean): void {
  if (!held.length || !sinks.size) return
  const batch = held.splice(0, held.length)
  for (const r of batch) if (!dispatchSettle(r, releaseBusy)) hold(r)
}

/**
 * Deliver one settled result to the live subscribers with single-owner text
 * delivery. Returns false when the text could not be placed anywhere: no
 * subscriber owns the session and none accepts unowned text. The caller then
 * keeps the result, so a pane that switched slots during the `/api/stt`
 * round-trip does not lose the utterance — it lands when a composer for that
 * session is next on screen (a re-subscribe, or `redeliverPending`).
 */
function dispatchSettle(result: TranscriptResult, releaseBusy: boolean): boolean {
  const live = [...sinks]
  const owners = live.filter(s => s.owns?.(result.sessionId) === true)
  // A claimed transcript goes to exactly one composer. No shipped layout mounts
  // two composers for one slot (split view unmounts ChatPage's composer; Crew
  // DM panes render without ChatPage), so `owners` has at most one entry today.
  // The last-subscribed tie-break is the defensive choice for a layout that
  // does; if one arrives, carry the starting instance on the request and
  // prefer it here instead of mount order.
  const owner = owners.length ? owners[owners.length - 1] : null
  // Unowned text likewise lands in at most one place: two co-mounted hosts that
  // both keep a draft store (two ChatPage instances) would otherwise each
  // append the same transcript to the same persisted draft. Same tie-break as
  // the owner path.
  const takers = live.filter(s => s.acceptsUnowned)
  const taker = takers.length ? takers[takers.length - 1] : null
  const targets = owner ? new Set([owner]) : new Set(taker ? [taker] : [])
  // An error has no composer to land in; every subscriber may surface it.
  const isError = !result.text
  for (const s of live) {
    const deliver = isError || targets.has(s)
    if (deliver || releaseBusy) s.settle(result, deliver)
  }
  return isError || targets.size > 0
}

/**
 * Register a delivery target. An already-running request is replayed as a
 * `begin` so a returning instance restores the busy indicator, a result that
 * settled while no instance existed is handed over, and a failure whose audio
 * is still held is shown again.
 *
 * The hand-over is deferred to a microtask rather than applied inline. A
 * subscriber mounts inside a page whose own prefill and draft effects commit in
 * the same pass, and a transcript applied before those have run is written
 * against composer state they are about to replace — so it lands in a
 * half-settled slot and can be overwritten by the very next persist. A microtask
 * runs after the whole effect flush, which is when the composer knows which slot
 * it holds.
 *
 * It delivers to whichever sinks are current when it runs, not to the one that
 * scheduled it: StrictMode subscribes, tears down and resubscribes within that
 * window, so keying on the scheduling subscription would strand the text on
 * every mount in development.
 */
export function subscribeTranscripts(next: TranscriptSink): () => void {
  sinks.add(next)
  if (inFlight) next.begin(inFlight)
  // Nothing mounted any more by the time this runs: the results simply stay
  // held for the next instance rather than being delivered into sinks that are
  // gone.
  if (held.length) queueMicrotask(() => flushHeld(true))
  // A failure that was already delivered, and whose audio is still held, is
  // shown again to the newcomer: its notice is the only way to that audio.
  // Deferred like the hand-over above, and only to this subscriber while it is
  // still live; a failure still in `held` reaches it through the flush instead.
  if (failed && !held.some(r => r.id === failed!.id)) {
    const replay = failed
    queueMicrotask(() => { if (failed === replay && sinks.has(next)) next.settle(replay, true) })
  }
  return () => { sinks.delete(next) }
}

/**
 * A subscriber's on-screen session changed (a pane switched slots, the main
 * chat switched sessions): if a transcript is waiting for an owner, try again.
 * Subscribing is the other time this runs; this covers an instance that stays
 * mounted while what it shows moves.
 */
export function redeliverPending(): void {
  // Busy state was already released when each result first settled.
  flushHeld(false)
}

/** Announce a started transcription and return its identity. */
export function beginTranscription(sessionId: string | null): PendingTranscription {
  inFlight = { id: ++nextId, sessionId }
  for (const s of sinks) s.begin(inFlight)
  return inFlight
}

/** Deliver a request's outcome to the live subscribers, or hold it for the next. */
export function settleTranscription(result: TranscriptResult): void {
  // Guarded so a straggler cannot clear a NEWER request's in-flight record and
  // leave a returning instance showing idle while that one is still running.
  if (inFlight?.id === result.id) inFlight = null
  // Held before delivery, so a subscriber reading `failedCapture` while it
  // settles finds the audio it is about to offer.
  if (result.capture) {
    dropFailed()
    failed = { ...result, capture: result.capture }
  }
  if (sinks.size && dispatchSettle(result, true)) return
  hold(result)
}

/** The capture failed request `id` left behind, while the inbox still holds it. */
export function failedCapture(id: number): FailedCapture | null {
  return failed?.id === id ? failed.capture : null
}

/**
 * Let the held capture go. With `id`, only while it is still that request's: a
 * notice acting on a failure that has since been replaced must not drop the
 * newer one.
 */
export function discardFailedCapture(id?: number): void {
  if (id !== undefined && failed?.id !== id) return
  dropFailed()
}

/**
 * Hand back the held capture of request `id` for one more attempt, and stop
 * holding it: a retry that fails settles with the capture again, one that
 * succeeds leaves nothing behind. Null while another transcription is in
 * flight, which keeps the one-request-at-a-time rule the mic controls rely on.
 */
export function takeFailedCapture(id: number): { capture: FailedCapture; sessionId: string | null } | null {
  if (inFlight || failed?.id !== id) return null
  const { capture, sessionId } = failed
  dropFailed()
  return { capture, sessionId }
}

/** Test seam: how many results are waiting for a composer. */
export function _heldCount(): number { return held.length }

/** Test seam: number of live subscribers. */
export function _subscriberCount(): number { return sinks.size }
