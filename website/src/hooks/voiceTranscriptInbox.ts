/**
 * Module-scoped hand-off for a batch transcription that outlives the component
 * which started it.
 *
 * Navigating away from Chat unmounts the voice hook while the `/api/stt`
 * request is still in flight. The request is a plain fetch, so it completes
 * regardless — only its DELIVERY needs somewhere to land, because the callback
 * it was going to invoke belongs to a page that no longer exists. Progress and
 * results are routed here instead: to the hook instance that is mounted at the
 * time if there is one, otherwise held until the next instance subscribes.
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
 * pending request lives here, and only until the tab unloads.
 *
 * Two invariants, both resting on the same fact — the controls that start a voice
 * session live only in Chat, and the caller refuses to start one while a
 * transcription is in flight:
 *   - at most one request is ever waiting, so a single result slot is enough;
 *   - at most one subscriber is ever live, so `sink` is a slot rather than a set.
 * Route exclusivity is what upholds the second one today: `ChatPage` also mounts
 * embedded (`ArtifactChatPanel`, the app-sdk `ChatPanel`), and if two instances
 * ever co-mounted the later would silently detach the earlier.
 */

/** A transcription request in flight. */
export interface PendingTranscription {
  id: number
  /** Slot that owned the recording, so the busy state shows on the right session. */
  sessionId: string | null
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
}

export interface TranscriptSink {
  begin: (request: PendingTranscription) => void
  settle: (result: TranscriptResult) => void
}

let sink: TranscriptSink | null = null
/** Result waiting for a subscriber. A newer one replaces it: the invariant allows
 *  only one, and the newer utterance is the one a user still wants. */
let pending: TranscriptResult | null = null
let inFlight: PendingTranscription | null = null
let nextId = 0

/**
 * Register the delivery target. An already-running request is replayed as a
 * `begin` so a returning instance restores the busy indicator, and a result that
 * settled while no instance existed is handed over.
 *
 * The hand-over is deferred to a microtask rather than applied inline. A
 * subscriber mounts inside a page whose own prefill and draft effects commit in
 * the same pass, and a transcript applied before those have run is written
 * against composer state they are about to replace — so it lands in a
 * half-settled slot and can be overwritten by the very next persist. A microtask
 * runs after the whole effect flush, which is when the composer knows which slot
 * it holds.
 *
 * It delivers to whichever sink is current when it runs, not to the one that
 * scheduled it: StrictMode subscribes, tears down and resubscribes within that
 * window, so keying on the scheduling subscription would strand the text on
 * every mount in development.
 */
export function subscribeTranscripts(next: TranscriptSink): () => void {
  sink = next
  if (inFlight) next.begin(inFlight)
  const handover = pending
  pending = null
  if (handover) {
    queueMicrotask(() => {
      const target = sink
      // Nothing mounted any more: keep holding the text for the next instance
      // rather than delivering into a sink that is gone. A result that arrived
      // in the meantime is newer and wins.
      if (!target) { pending = pending ?? handover; return }
      target.settle(handover)
    })
  }
  return () => {
    // Only if still ours: a newer subscriber has already taken the slot and
    // clearing it here would leave that live instance unreachable.
    if (sink === next) sink = null
  }
}

/** Announce a started transcription and return its identity. */
export function beginTranscription(sessionId: string | null): PendingTranscription {
  inFlight = { id: ++nextId, sessionId }
  sink?.begin(inFlight)
  return inFlight
}

/** Deliver a request's outcome to the live subscriber, or hold it for the next. */
export function settleTranscription(result: TranscriptResult): void {
  // Guarded so a straggler cannot clear a NEWER request's in-flight record and
  // leave a returning instance showing idle while that one is still running.
  if (inFlight?.id === result.id) inFlight = null
  if (sink) { sink.settle(result); return }
  pending = result
}
