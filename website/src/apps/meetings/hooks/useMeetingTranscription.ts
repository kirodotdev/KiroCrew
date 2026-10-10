// Live transcription for a meeting, over KiroCrew's OWN streaming speech-to-text.
//
// Wire protocol — this conforms to `dashboard/stt_stream.py:api_ws_stt`, it does
// not invent one:
//   • connect to `/api/ws/stt`
//   • the server replies `{"type":"ready"}` once its transcription stream is up
//     (2-3s cold), so PCM is buffered locally until then and flushed
//   • the client sends 16 kHz Int16 PCM mono frames produced by
//     `/pcm-worklet.js` (the same worklet the dashboard's dictation uses)
//   • the server emits `{"type":"partial"|"final"|"error", text}`
//   • the client sends `{"type":"stop"}` and lets the SERVER close, so trailing
//     finals still arrive
//   • the server's `ready` may carry `max_duration_ms`, the wall-clock life of
//     one connection, and `final_timeout_ms`, how long a `stop`'s drain may
//     take; a lead before the cap (`rotationLeadMs`) the hook opens the NEXT
//     socket, switches the live audio to it when it is ready, and sends `stop`
//     on the old one so the server finishes the utterance in flight. Rotation
//     is silent. Without the fields the hook assumes `DEFAULT_MAX_DURATION_MS`
//     and `ROTATE_LEAD_MS`.
//
// A meeting outlives any one socket, so a socket is held as a LEASE: the server
// ends every connection at its cap, and a client that only reconnects AFTER
// that close has already lost the utterance the server was recognising, up to
// two minutes of speech per cap in a lively conversation.
//
// The upstream app had a second provider backed by a separately built local
// daemon; it is gone, so this is the single path.
//
// Every FINAL segment is POSTed to the meeting's dispatch endpoint, which is
// what feeds the agents. Partials only drive the live caption.

import { useCallback, useEffect, useRef, useState } from 'react'

import { MeetingsApiError, meetingsApi, type TranscriptSegment } from '../api'
import { reportIfMicDenied } from '../../../hooks/mic'
import { dictationSeparator, joinTranscript, transcriptTail } from '../../../lib/dictationText'
import { withBase } from '../../../lib/basePath'

/** Feature detection mirroring `useStreamingStt` — the dashboard's own hook. */
export const transcriptionSupported =
  typeof window !== 'undefined' &&
  typeof window.AudioContext !== 'undefined' &&
  typeof (window as unknown as { AudioWorkletNode?: unknown }).AudioWorkletNode !== 'undefined' &&
  typeof window.WebSocket !== 'undefined' &&
  typeof navigator !== 'undefined' &&
  typeof navigator.mediaDevices !== 'undefined' &&
  typeof navigator.mediaDevices.getUserMedia === 'function'

/** Cap the locally buffered pre-`ready` audio at ~8s (16 kHz mono Int16 = 32 KB/s). */
/** The stop frame the STT WebSocket protocol expects (a wire frame, not copy). */
const STOP_FRAME = JSON.stringify({ type: 'stop' })

const MAX_BUFFERED_BYTES = 8 * 32 * 1024
/** Reconnect when no server frame has arrived for this long while recording. */
const STALL_TIMEOUT_MS = 20_000
const WATCHDOG_INTERVAL_MS = 5_000
/** How long to wait for the server to close after `stop` before forcing it. */
const CLOSE_GRACE_MS = 8_000

/**
 * A server that does not advertise its cap is assumed to enforce the known one
 * (`_MAX_STREAM_DURATION_SECS` in `dashboard/stt_stream.py`).
 */
export const DEFAULT_MAX_DURATION_MS = 300_000
/** The least lead before the cap at which the next lease is opened, so it is ready in time. */
export const ROTATE_LEAD_MS = 30_000
/**
 * Time budgeted for a successor to open and report `ready`, measured at about
 * two seconds; the lead reserves it ahead of the server's drain bound.
 */
export const ROTATE_CONNECT_ALLOWANCE_MS = 5_000
/**
 * How long the successor's finals are held behind a stopped predecessor's
 * trailing final, for ordering.
 *
 * Past this the hold is released, so the notes are delayed by at most this and
 * never indefinitely — but the predecessor's socket stays OPEN: the server's own
 * drain budget (`stt.timeout_secs`) is longer, it may still be decoding that
 * final, and a client close would make it discard it. The server closes the
 * socket when it is done; only the next rotation or an unmount closes it first.
 */
export const DRAIN_GRACE_MS = 60_000
/** Timer delay ceiling: a larger value fires immediately in browsers. */
const MAX_TIMER_DELAY_MS = 2_147_483_647

/**
 * Retry schedule for a failed segment dispatch, in ms.
 *
 * A dispatch is the ONLY path a final segment reaches the agents, so a swallowed
 * rejection means the notes and tasks silently omit that stretch of the meeting —
 * the same "a queue is discarded without being drained" failure the backend
 * teardown paths were fixed for, reached from the client side. A transient
 * failure (a gateway restart, a momentary network drop) is exactly the case worth
 * retrying, and the segment is small.
 *
 * Bounded and short on purpose: transcription is a live stream, so a segment that
 * cannot land within a few seconds is better dropped than queued indefinitely
 * behind newer speech. The give-up is REPORTED (see below) rather than silent,
 * which is the part that was actually missing.
 */
const DISPATCH_RETRY_DELAYS_MS = [400, 1_200, 3_000]

/**
 * The longest `flush` waits for in-flight dispatches: the whole retry ladder
 * plus two seconds for the requests themselves. A dispatch with no response at
 * all (a wedged backend; `fetch` has no timeout of its own) must not hold the
 * caller's status change forever. It keeps running and reports its own give-up.
 */
export const FLUSH_TIMEOUT_MS = DISPATCH_RETRY_DELAYS_MS.reduce((sum, delay) => sum + delay, 0) + 2_000

/**
 * How much of the transcript the live caption may carry, in characters.
 *
 * The caption element is two lines tall, so this only has to be the right order
 * of magnitude — the hard bound on the rendered height is the `line-clamp-2` in
 * `BroadcastBar.tsx`. What this constant guarantees is that the text inside
 * those lines stays RECENT.
 */
export const CAPTION_WINDOW_CHARS = 240
/** The durable transcript owns history; the caption needs only a bounded tail. */
export const CAPTION_FINALS_LIMIT = 64

/**
 * The recent tail of the transcript, for the "Heard: …" caption.
 *
 * Trimming from the FRONT is the entire point. The finals array accumulates for
 * the whole meeting, and the caption used to receive all of it — which read as a
 * caption that froze on the meeting's opening sentence and never updated again,
 * because the element clipped its overflow with `text-overflow: ellipsis` and
 * that shows a string's HEAD. A live caption has to show the newest speech, so
 * the oldest is what gets dropped.
 *
 * Whole segments are kept wherever possible so the caption never begins
 * mid-sentence; only a single over-long segment is cut, and then at a word
 * boundary.
 */
export function captionWindow(finals: readonly string[], partial = ''): string {
  const segments = [...finals, partial].map((s) => s.trim()).filter(Boolean)
  if (segments.length === 0) return ''

  const kept: string[] = []
  let length = 0
  for (let i = segments.length - 1; i >= 0; i--) {
    const cost = segments[i].length + dictationSeparator(segments[i], kept[0] ?? '').length
    if (length + cost > CAPTION_WINDOW_CHARS) break
    kept.unshift(segments[i])
    length += cost
  }
  if (kept.length > 0) return joinTranscript(kept)

  // Even the newest segment alone overflows the window: keep its tail, cut at a
  // word boundary for spaced scripts, without throwing away a CJK prefix just
  // because an English word later in the caption has a space after it.
  return transcriptTail(segments[segments.length - 1], CAPTION_WINDOW_CHARS)
}

/**
 * One STT socket and the per-socket state the lease model needs.
 *
 * Three leases can exist at once: the ACTIVE one receives the live audio, a
 * DRAINING predecessor was sent `stop` and is still delivering its trailing
 * final, and a SUCCESSOR has been opened ahead of the cap and is waiting for
 * `ready`. The microphone, the AudioContext and the worklet are shared across
 * leases and are never touched by a rotation.
 */
interface Lease {
  ws: WebSocket
  ready: boolean
  /** PCM captured before `ready`, flushed in order once it arrives. */
  buffer: ArrayBuffer[]
  bufferedBytes: number
  /** `stop` was sent on this lease; its close is expected and silent. */
  stopping: boolean
  /** The drain grace passed: successors no longer hold their finals behind it. */
  holdExpired: boolean
  /**
   * How long this lease's drain may keep a successor silent: the server's
   * advertised drain bound, or `DRAIN_GRACE_MS` when it advertised none. The
   * successor's stall watchdog waits this out while this lease is draining.
   */
  drainBudgetMs: number
  rotateTimer: ReturnType<typeof setTimeout> | null
  closeTimer: ReturnType<typeof setTimeout> | null
  lastPartial: string
  /** Dispatches of this lease's finals not yet settled, which a drain's release waits for. */
  outstandingDispatches: Set<Promise<void>>
  /** Resolves on the first `ready` or `error` frame — the gate `start()` awaits. */
  settled: Promise<void>
  settle: () => void
}

function clearLeaseTimers(lease: Lease | null): void {
  if (!lease) return
  if (lease.rotateTimer) clearTimeout(lease.rotateTimer)
  if (lease.closeTimer) clearTimeout(lease.closeTimer)
  lease.rotateTimer = null
  lease.closeTimer = null
}

/** Count a dispatch against the lease whose final it carries, until it settles. */
function trackLeaseDispatch(lease: Lease, dispatch: Promise<void>): void {
  const outstanding = lease.outstandingDispatches
  outstanding.add(dispatch)
  void dispatch.finally(() => { outstanding.delete(dispatch) })
}

/** Resolves once `pending` settles, or after `ms` if it never does. */
function settledOrTimedOut(pending: Promise<unknown>, ms: number): Promise<void> {
  return new Promise(resolve => {
    const timer = setTimeout(resolve, ms)
    const done = () => { clearTimeout(timer); resolve() }
    pending.then(done, done)
  })
}

/** The advertised cap, range-checked, or the known default. */
function advertisedMaxDurationMs(value: unknown): number {
  return typeof value === 'number' && Number.isFinite(value) && value > 0 && value <= MAX_TIMER_DELAY_MS
    ? value
    : DEFAULT_MAX_DURATION_MS
}

/** The advertised drain bound, range-checked, or 0 when a server sends none. */
function advertisedFinalTimeoutMs(value: unknown): number {
  return typeof value === 'number' && Number.isFinite(value) && value > 0 && value <= MAX_TIMER_DELAY_MS
    ? value
    : 0
}

/**
 * How long before the cap the next lease is opened.
 *
 * The predecessor is asked to `stop` once the successor is ready, and the
 * server then needs its own drain bound to deliver the trailing final. So the
 * lead is at least `ROTATE_LEAD_MS`, and when the server advertises that bound
 * it is the bound plus the connect allowance, so the cap's lifetime deadline
 * cannot land in the middle of a drain the server itself allows. The derived
 * part never exceeds half the cap: a bound that would not fit still leaves the
 * lease most of its life.
 */
export function rotationLeadMs(maxDurationMs: number, finalTimeoutMs: number): number {
  const drain = Math.min(finalTimeoutMs + ROTATE_CONNECT_ALLOWANCE_MS, Math.floor(maxDurationMs / 2))
  return Math.max(ROTATE_LEAD_MS, drain)
}

interface Options {
  /** The meeting whose dispatch endpoint receives each final segment. */
  meetingId: string
  /** Called with the recent tail of the transcript (see `captionWindow`). */
  onCaption: (text: string) => void
  /**
   * Called once per committed final segment, BEFORE it is dispatched.
   *
   * Return `false` to suppress the dispatch. Speech-to-text emits overlapping
   * finals, so the caller's duplicate check is what stops the same sentence
   * reaching every listening agent twice (duplicated notes, duplicated
   * extracted tasks, duplicated agent turns).
   */
  /**
   * Called with each final segment and the time it arrived (`Date.now()`).
   * Returns the text to DISPATCH — which may be only the new suffix of a growing
   * final — or `false` to suppress it entirely. `void` keeps the caption-only
   * callers working without an opt-in.
   *
   * `at` is the ARRIVAL, not the call: a final held behind a draining lease is
   * judged when the hold is released, and a dedup window measured from that
   * moment would read two utterances heard seconds apart as one repeat.
   */
  onFinal?: (text: string, at: number) => string | boolean | void
  /** Called with the recognizer's in-flight text; never persisted. */
  onPartial?: (text: string) => void
  /** Called after the backend has durably accepted a final segment. */
  onCommitted?: (segment: TranscriptSegment) => void
  /** Called with a user-facing message when transcription cannot run. */
  onError?: (message: string) => void
}

export function useMeetingTranscription({
  meetingId,
  onCaption,
  onFinal,
  onPartial,
  onCommitted,
  onError,
}: Options) {
  const [active, setActive] = useState(false)
  /** The lease receiving the live audio. */
  const activeRef = useRef<Lease | null>(null)
  /** A predecessor that was sent `stop` and is delivering its trailing final. */
  const drainingRef = useRef<Lease | null>(null)
  /** The next lease, opened ahead of the cap and not yet `ready`. */
  const successorRef = useRef<Lease | null>(null)
  /**
   * Finals from the active lease, held while a predecessor drains.
   *
   * Order matters to the notes: the predecessor's trailing final is the END of
   * the utterance the switch interrupted, so anything the successor recognises
   * meanwhile waits behind it. Each keeps the time it arrived, which is what the
   * caller's dedup judges it by when it is released.
   */
  const heldDispatchesRef = useRef<{ text: string; at: number }[]>([])
  /** Every dispatch issued and not yet settled, so `flush` can wait for them. */
  /**
   * The dispatch issued last, while it is still in flight: the next one starts
   * behind it. `started` resolves, and `startedAt` is stamped, when its request
   * actually goes out, not when it was queued, so time spent waiting behind
   * earlier dispatches never counts against its own bound.
   */
  const dispatchTailRef = useRef<{
    dispatch: Promise<void>
    started: Promise<void>
    startedAt: number
  } | null>(null)
  const ctxRef = useRef<AudioContext | null>(null)
  const streamRef = useRef<MediaStream | null>(null)
  const watchdogRef = useRef<ReturnType<typeof setInterval> | null>(null)
  const lastFrameRef = useRef(0)
  const finalsRef = useRef<string[]>([])
  /** The USER stopped capture (as opposed to a lease being rotated out). */
  const stoppingRef = useRef(false)
  const dispatchBlockedRef = useRef(false)
  /** True from entering `start()` until the socket is live (or it gave up). */
  const startingRef = useRef(false)
  /** The rotation entry point, held in a ref so `openLease` can schedule it. */
  const rotateRef = useRef<() => void>(() => {})

  // Keep callback refs fresh so the long-lived socket handlers always invoke the
  // latest caller-supplied callbacks, not the ones captured at start().
  const onCaptionRef = useRef(onCaption)
  const onFinalRef = useRef(onFinal)
  const onPartialRef = useRef(onPartial)
  const onCommittedRef = useRef(onCommitted)
  const onErrorRef = useRef(onError)
  onCaptionRef.current = onCaption
  onFinalRef.current = onFinal
  onPartialRef.current = onPartial
  onCommittedRef.current = onCommitted
  onErrorRef.current = onError

  useEffect(() => {
    dispatchBlockedRef.current = false
  }, [meetingId])

  /**
   * Send one final segment to the agents, retrying a transient failure.
   *
   * Never rejects: a dispatch failure must not tear down the socket handler that
   * called it. But it must not be silent either — if every attempt fails the
   * segment is genuinely lost from the notes and tasks, so the caller's error
   * channel is told, which is what surfaces a toast instead of a quiet gap.
   *
   * Not cancelled on stop(): a segment captured before the user paused still
   * belongs in the transcript, and the endpoint is idempotent per segment.
   */
  const dispatchWithRetry = useCallback(
    async (text: string): Promise<void> => {
      if (dispatchBlockedRef.current) return
      for (let attempt = 0; ; attempt += 1) {
        try {
          const response = await meetingsApi.dispatch(meetingId, text)
          onCommittedRef.current?.(response.segment)
          return
        } catch (error) {
          if (
            error instanceof MeetingsApiError
            && error.status === 413
            && error.code === 'transcript_too_large'
          ) {
            dispatchBlockedRef.current = true
            onPartialRef.current?.('')
            onErrorRef.current?.('transcript_full')
            return
          }
          // Retry ONLY a failure the server explicitly reported. A
          // `MeetingsApiError` carries a status, which means a response arrived and
          // the request was rejected — safe to send again.
          //
          // A bare fetch rejection (connection reset, navigation, TLS drop) is
          // AMBIGUOUS: the dispatch endpoint broadcasts to every agent queue before
          // it responds, so the segment may already have been accepted and a retry
          // would duplicate it into all of them. Duplicated transcript is worse than
          // a reported gap — the notes silently repeat a passage and the task
          // extractor files the same action item twice, with nothing to indicate
          // why. So an ambiguous failure is reported, not retried.
          const reported = error instanceof MeetingsApiError
          if (!reported || attempt >= DISPATCH_RETRY_DELAYS_MS.length) {
            onErrorRef.current?.('dispatch')
            return
          }
          await new Promise(resolve =>
            setTimeout(resolve, DISPATCH_RETRY_DELAYS_MS[attempt]),
          )
        }
      }
    },
    [meetingId],
  )

  /**
   * Issue a dispatch in speech order: behind the one still in flight, if any,
   * so a segment the server rejected once, and is retrying, is not overtaken
   * by the next. A request holds the ones behind it for at most
   * `FLUSH_TIMEOUT_MS` after it went out, the bound the flush uses, because
   * `fetch` has no timeout of its own and one request that never answers must
   * not hold every later segment of the meeting. With nothing in flight the
   * request goes out at once.
   */
  const dispatchInOrder = useCallback((text: string): Promise<void> => {
    const tail = dispatchTailRef.current
    let markStarted!: () => void
    const started = new Promise<void>(resolve => { markStarted = resolve })
    const entry = { dispatch: Promise.resolve(), started, startedAt: 0 }
    const begin = () => {
      entry.startedAt = Date.now()
      markStarted()
      return dispatchWithRetry(text)
    }
    entry.dispatch = tail
      ? tail.started
        .then(() => settledOrTimedOut(tail.dispatch, tail.startedAt + FLUSH_TIMEOUT_MS - Date.now()))
        .then(begin)
      : begin()
    dispatchTailRef.current = entry
    void entry.dispatch.finally(() => {
      if (dispatchTailRef.current === entry) dispatchTailRef.current = null
    })
    return entry.dispatch
  }, [dispatchWithRetry])

  const clearWatchdog = useCallback(() => {
    if (watchdogRef.current) {
      clearInterval(watchdogRef.current)
      watchdogRef.current = null
    }
  }, [])

  /**
   * Dispatch, in order, the finals held while a predecessor drained.
   *
   * The caller's dedup runs HERE, not at arrival: dispatch order is speech
   * order, and a dedup judged against a final that has not been dispatched yet
   * would drop the wrong words. SYNCHRONOUS on purpose: by the time this runs
   * the hold is already gone, so a live final arriving during an awaited
   * release would be judged and dispatched ahead of the held ones still queued.
   * The requests themselves leave in that order too (`dispatchInOrder`).
   */
  const releaseHeld = useCallback(() => {
    const held = heldDispatchesRef.current
    heldDispatchesRef.current = []
    const live = activeRef.current
    for (const { text, at } of held) {
      const decision = onFinalRef.current?.(text, at)
      if (decision === false) continue
      const toDispatch = typeof decision === 'string' ? decision : text
      if (!toDispatch.trim()) continue
      const dispatch = dispatchInOrder(toDispatch)
      if (live) trackLeaseDispatch(live, dispatch)
    }
  }, [dispatchInOrder])

  /**
   * End a draining lease: on the server's close after `stop`, or when the next
   * rotation or an unmount needs it gone. Idempotent, because both can happen.
   */
  const finishDrain = useCallback((lease: Lease) => {
    if (lease.rotateTimer) clearTimeout(lease.rotateTimer)
    lease.rotateTimer = null
    try { lease.ws.close() } catch { /* already closed */ }
    const release = () => {
      clearLeaseTimers(lease)
      if (drainingRef.current === lease) {
        drainingRef.current = null
        // The successor's stall window was stretched to this drain; it gets a
        // fresh, normal one from here rather than one already spent waiting.
        if (activeRef.current) lastFrameRef.current = Date.now()
      }
      releaseHeld()
    }
    const pending = [...lease.outstandingDispatches]
    if (pending.length === 0) {
      release()
      return
    }
    // Every final this lease delivered precedes the successor's, and any of
    // their dispatches may still be inside its retry ladder when the server
    // closes the socket: the hold stays until all of them have settled, so a
    // segment retried once cannot land behind the successor's. The drain grace
    // and `flush()` keep bounding the wait, and a lease replaced or expired
    // meanwhile releases nothing from here.
    void Promise.allSettled(pending).then(() => {
      if (drainingRef.current !== lease) return
      release()
    })
  }, [releaseHeld])

  /**
   * The drain grace passed without the server's close: stop holding the live
   * lease's finals behind it, but leave the socket open — the server may still
   * be decoding the trailing final, and a client close would make it discard it.
   */
  const expireHold = useCallback((lease: Lease) => {
    lease.holdExpired = true
    if (lease.closeTimer) clearTimeout(lease.closeTimer)
    lease.closeTimer = null
    releaseHeld()
  }, [releaseHeld])

  /**
   * Tear capture down: the microphone, the worklet, the live lease and any
   * successor. A DRAINING predecessor is left to finish unless `abandonDrain`:
   * the server may still be decoding the utterance the rotation stopped it in,
   * and closing the socket now would discard that final — the one the rotation
   * exists to save. It ends on the server's close (or the next rotation), and
   * the finals held behind it are released then. Only an unmount abandons it.
   */
  const cleanup = useCallback((abandonDrain = false) => {
    clearWatchdog()
    // Close, THEN null the ref: a close handler that runs synchronously (the test
    // double does) still sees its own lease as current and reports the close the
    // way a live socket's would; a real socket closes on a later task either way.
    for (const ref of [successorRef, activeRef]) {
      clearLeaseTimers(ref.current)
      try { ref.current?.ws.close() } catch { /* already closing */ }
      ref.current = null
    }
    if (abandonDrain) {
      const draining = drainingRef.current
      drainingRef.current = null
      clearLeaseTimers(draining)
      try { draining?.ws.close() } catch { /* already closing */ }
      // A final already recognised belongs in the transcript, even now.
      releaseHeld()
    }
    try { streamRef.current?.getTracks().forEach(t => t.stop()) } catch { /* ignore */ }
    streamRef.current = null
    try { ctxRef.current?.close() } catch { /* ignore */ }
    ctxRef.current = null
    // Release the in-progress guard here as well as on the success path: `cleanup`
    // runs on EVERY teardown, including each of `start`'s own failure exits, so a
    // start that dies partway cannot leave the flag stuck and block every later
    // attempt for the rest of the meeting.
    startingRef.current = false
    onPartialRef.current?.('')
    setActive(false)
  }, [clearWatchdog, releaseHeld])

  // Never leave the microphone, or a draining socket, open when the page unmounts.
  useEffect(() => () => { cleanup(true) }, [cleanup])

  /**
   * Open one socket and bind its handlers to a lease object.
   *
   * Which lease a frame or a close belongs to is decided against the three refs
   * at the moment it lands, never against a single "current socket": a close is
   * delivered asynchronously, so the rotation and a stop()-then-start() both
   * create the NEW socket before the OLD one's close event arrives — and an
   * unguarded teardown there would tear down the new session (mic tracks stopped,
   * AudioContext closed, active=false) moments after it came up, with nothing to
   * restart it while the UI still showed Live.
   */
  const openLease = useCallback((): Lease => {
    const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
    const ws = new WebSocket(`${proto}//${window.location.host}/api/ws/stt`)
    ws.binaryType = 'arraybuffer'
    let settle: () => void = () => {}
    const settled = new Promise<void>(resolve => { settle = resolve })
    const lease: Lease = {
      ws,
      ready: false,
      buffer: [],
      bufferedBytes: 0,
      stopping: false,
      holdExpired: false,
      rotateTimer: null,
      closeTimer: null,
      lastPartial: '',
      drainBudgetMs: DRAIN_GRACE_MS,
      outstandingDispatches: new Set(),
      settled,
      settle: () => settle(),
    }
    const isActive = () => activeRef.current === lease

    ws.onmessage = ev => {
      if (typeof ev.data !== 'string') return
      if (isActive()) lastFrameRef.current = Date.now()
      let msg: {
        type?: string
        text?: string
        message?: string
        code?: string
        max_duration_ms?: unknown
        final_timeout_ms?: unknown
      }
      try {
        msg = JSON.parse(ev.data)
      } catch {
        return
      }
      if (msg.type === 'ready') {
        // A lease that is neither live nor the planned successor (dropped by a
        // stop, or already replaced) has nothing to become ready FOR.
        if (!isActive() && successorRef.current !== lease) {
          lease.settle()
          return
        }
        lease.ready = true
        if (ws.readyState === WebSocket.OPEN) {
          for (const chunk of lease.buffer) {
            try { ws.send(chunk) } catch { break }
          }
        }
        lease.buffer = []
        lease.bufferedBytes = 0
        if (successorRef.current === lease) {
          if (stoppingRef.current) {
            // The user stopped while this successor was still connecting: it
            // must not take the audio over. Without this, a `ready` landing
            // inside the stop grace replaced the active lease, the grace timer
            // then found a different lease and did nothing, and the microphone
            // stayed hot — dispatching speech into a meeting the user had ended.
            successorRef.current = null
            try { ws.close() } catch { /* already closing */ }
            lease.settle()
            return
          }
          // The switch: the live audio moves here, and the predecessor is asked
          // to finish so the server delivers the utterance it is in the middle
          // of instead of discarding it at the cap.
          const old = activeRef.current
          successorRef.current = null
          activeRef.current = lease
          // The stall watchdog times the LIVE lease, and this `ready` arrived
          // before the lease was live, so it was not counted above.
          lastFrameRef.current = Date.now()
          if (old) {
            clearLeaseTimers(old)
            old.stopping = true
            try { old.ws.send(STOP_FRAME) } catch { /* closing */ }
            drainingRef.current = old
            old.closeTimer = setTimeout(() => expireHold(old), DRAIN_GRACE_MS)
          }
        }
        // Every lease plans its own replacement from its own cap. Armed only
        // once the lease is live, so a lease dropped above leaves no timer.
        const maxDurationMs = advertisedMaxDurationMs(msg.max_duration_ms)
        const finalTimeoutMs = advertisedFinalTimeoutMs(msg.final_timeout_ms)
        if (finalTimeoutMs > 0) lease.drainBudgetMs = finalTimeoutMs
        const lead = rotationLeadMs(maxDurationMs, finalTimeoutMs)
        lease.rotateTimer = setTimeout(
          () => rotateRef.current(),
          Math.max(maxDurationMs - lead, 1_000),
        )
        lease.settle()
        return
      }
      if (msg.type === 'partial') {
        // A draining lease's partial precedes its final and is never shown.
        if (!isActive()) return
        const lastPartial = msg.text || ''
        lease.lastPartial = lastPartial
        onPartialRef.current?.(lastPartial)
        onCaptionRef.current(captionWindow(finalsRef.current, lastPartial))
        return
      }
      if (msg.type === 'final') {
        // Only the live lease and the predecessor it is draining own speech;
        // a lease dropped by a stop does not get to add to the transcript.
        if (!isActive() && drainingRef.current !== lease) return
        const at = Date.now()
        const text = (msg.text || '').trim()
        lease.lastPartial = ''
        // A final replaces the live row with what the LIVE lease hears now: its
        // own cleared partial, or, for a draining predecessor's final, the
        // successor's current partial — so the stale partial the predecessor left
        // on screen does not outlive the final that committed it.
        const livePartial = isActive() ? '' : (activeRef.current?.lastPartial ?? '')
        onPartialRef.current?.(livePartial)
        if (!text) return
        finalsRef.current.push(text)
        if (finalsRef.current.length > CAPTION_FINALS_LIMIT) {
          finalsRef.current.splice(0, finalsRef.current.length - CAPTION_FINALS_LIMIT)
        }
        onCaptionRef.current(captionWindow(finalsRef.current, livePartial))
        // Order matters to the notes: while a predecessor is still delivering
        // the utterance it was stopped in, this lease's finals wait behind it,
        // and the caller's dedup runs at release (`releaseHeld`), in that order.
        const draining = drainingRef.current
        if (isActive() && draining && !draining.holdExpired) {
          heldDispatchesRef.current.push({ text, at })
          return
        }
        // The caller's duplicate check gates the dispatch: an overlapping final
        // still belongs in the caption (above), but must not be sent to the
        // agents a second time.
        // The caller's dedup decides WHAT to dispatch, not just whether to. STT
        // emits a growing final (`"yes"` then `"yes please"`), so a boolean answer
        // could only suppress the whole thing and lose the added words; a string
        // lets it hand back just the new suffix.
        const decision = onFinalRef.current?.(text, at)
        if (decision === false) return
        const toDispatch = typeof decision === 'string' ? decision : text
        if (!toDispatch.trim()) return
        // This is the line that reaches the agents. It must not tear down the
        // stream on failure — but it must not be swallowed either, or a transient
        // request failure silently drops that stretch of the meeting from the
        // notes and tasks. Retried on a short bounded schedule, then reported.
        trackLeaseDispatch(lease, dispatchInOrder(toDispatch))
        return
      }
      if (msg.type === 'error') {
        lease.settle()
        if (isActive()) {
          onErrorRef.current?.(msg.message || 'error')
          return
        }
        // A draining predecessor that fails while finishing loses the utterance
        // the rotation exists to save, so it is reported like the live lease.
        // Its cap is not: `stop` was sent for it to finish inside the deadline,
        // and the active lease carries on either way. A refused successor and a
        // lease already replaced have nothing the user needs to hear.
        if (drainingRef.current === lease && msg.code !== 'stt_max_duration_exceeded') {
          onErrorRef.current?.(msg.message || 'error')
        }
      }
    }
    ws.onclose = () => {
      if (drainingRef.current === lease) {
        finishDrain(lease)
        return
      }
      if (successorRef.current === lease) {
        // Died before taking over: the predecessor keeps the audio, and its own
        // cap still reaches the reactive path below.
        successorRef.current = null
        return
      }
      // A close for a socket already replaced is nobody's business.
      if (!isActive()) return
      // A close we did not ask for, while recording, is a transport failure:
      // surface it rather than silently going quiet mid-meeting.
      if (!stoppingRef.current) onErrorRef.current?.('disconnected')
      cleanup()
    }
    ws.onerror = () => {
      // Only the lease carrying the audio has a failure worth telling the user
      // about. A successor that cannot open is dropped silently (see onclose),
      // and a socket closed while still CONNECTING — a real browser fires
      // `error` then `close` for that on a later task, after a teardown has
      // already nulled every ref — must not read as a connection failure.
      if (!isActive()) {
        if (successorRef.current === lease) successorRef.current = null
        try { ws.close() } catch { /* never opened */ }
        return
      }
      onErrorRef.current?.('connection')
    }
    return lease
  }, [cleanup, dispatchInOrder, expireHold, finishDrain])

  rotateRef.current = () => {
    // One rotation at a time, and none once the user has stopped.
    if (stoppingRef.current || successorRef.current || !activeRef.current) return
    const draining = drainingRef.current
    if (draining) {
      // A predecessor still open past its grace is a wedged recogniser; the
      // next rotation needs the slot more than it needs that final.
      if (!draining.holdExpired) return
      finishDrain(draining)
    }
    successorRef.current = openLease()
  }

  const start = useCallback(async () => {
    if (!transcriptionSupported) {
      onErrorRef.current?.('unsupported')
      return
    }
    if (activeRef.current) return
    // The active ref alone is not enough: it is only assigned once the socket is
    // created, and everything before that is awaited (getUserMedia, the
    // AudioWorklet module). Two calls landing in that window both proceed and
    // end up with two microphone streams and two sockets, whose finals are
    // dispatched twice.
    //
    // The watchdog is the path that reaches it: its `cleanup()` clears `active`,
    // which the session hook watches to restart a dropped socket (that is the fix
    // for a silent disconnect) — so the watchdog's own `start()` and the effect's
    // race. Guarding here rather than in the effect keeps the invariant with the
    // function that owns it, and covers any future caller too.
    if (startingRef.current) return
    startingRef.current = true
    stoppingRef.current = false
    finalsRef.current = []
    // `heldDispatchesRef` is deliberately NOT reset: a predecessor left draining
    // by a stop or a restart still releases what was held behind it.

    let stream: MediaStream
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: true })
    } catch (e) {
      // This surface reports its own generic 'microphone' error rather than
      // routing through humanizeMicError, so hand a DENIAL to the shell here or
      // the desktop app has no route to System Settings (macOS never re-prompts).
      reportIfMicDenied(e)
      onErrorRef.current?.('microphone')
      startingRef.current = false
      return
    }
    streamRef.current = stream

    const lease = openLease()
    activeRef.current = lease
    const onLeaseError = lease.ws.onerror
    try {
      await new Promise<void>((resolve, reject) => {
        lease.ws.onerror = () => reject(new Error('open failed'))
        lease.ws.onopen = () => resolve()
      })
    } catch {
      onErrorRef.current?.('connection')
      cleanup()
      return
    }
    lease.ws.onerror = onLeaseError

    const ctx = new AudioContext()
    ctxRef.current = ctx
    try {
      await ctx.audioWorklet.addModule(withBase('/pcm-worklet.js'))
    } catch {
      onErrorRef.current?.('worklet')
      cleanup()
      return
    }
    const source = ctx.createMediaStreamSource(stream)
    const node = new AudioWorkletNode(ctx, 'pcm-worklet')

    // Route every frame to whichever lease is active WHEN IT LANDS: buffered
    // until that lease is ready, then sent live. Over the cap, drop the OLDEST
    // frames — the most recent speech wins. A rotation changes the target, not
    // the microphone.
    node.port.onmessage = e => {
      const chunk = e.data as ArrayBuffer
      const target = activeRef.current
      if (!target) return
      if (target.ready) {
        if (target.ws.readyState === WebSocket.OPEN) {
          try { target.ws.send(chunk) } catch { /* CLOSING */ }
        }
        return
      }
      target.buffer.push(chunk)
      target.bufferedBytes += chunk.byteLength
      while (target.bufferedBytes > MAX_BUFFERED_BYTES && target.buffer.length > 1) {
        target.bufferedBytes -= target.buffer.shift()!.byteLength
      }
    }
    source.connect(node)
    // The worklet's output is never heard — do NOT connect it to the destination.
    startingRef.current = false
    setActive(true)

    lastFrameRef.current = Date.now()
    clearWatchdog()
    watchdogRef.current = setInterval(() => {
      if (stoppingRef.current || !activeRef.current) return
      // The server decodes behind one lock, so a predecessor's long trailing
      // final keeps the successor silent for as long as that drain may take:
      // while one is draining, its drain budget is the stall window instead.
      const draining = drainingRef.current
      const stallMs = draining ? Math.max(STALL_TIMEOUT_MS, draining.drainBudgetMs) : STALL_TIMEOUT_MS
      if (Date.now() - lastFrameRef.current > stallMs) {
        // A silent stream is indistinguishable from a wedged one from here, and
        // a wedged one loses the rest of the meeting — so reconnect.
        cleanup()
        void start()
      }
    }, WATCHDOG_INTERVAL_MS)

    // Resolve on the server's `ready` (or its refusal), so a backend that fails
    // to start its stream does not leave `start` hanging forever.
    await lease.settled
  }, [cleanup, clearWatchdog, openLease])

  const stop = useCallback(() => {
    stoppingRef.current = true
    clearWatchdog()
    // A successor still connecting has no audio and nothing to drain: drop it
    // now, so its `ready` cannot take the microphone over after the user
    // stopped (the `ready` handler refuses as well, for the frame already in
    // flight).
    const pending = successorRef.current
    successorRef.current = null
    clearLeaseTimers(pending)
    try { pending?.ws.close() } catch { /* never opened */ }
    const lease = activeRef.current
    if (!lease || lease.ws.readyState !== WebSocket.OPEN) {
      cleanup()
      return
    }
    // Ask the server to stop and let IT close, so trailing finals still arrive.
    // Force cleanup after a grace period so the UI can never get stuck. A
    // draining predecessor is left to finish on its own (see `cleanup`).
    try { lease.ws.send(STOP_FRAME) } catch { /* ignore */ }
    window.setTimeout(() => {
      if (activeRef.current === lease) cleanup()
    }, CLOSE_GRACE_MS)
  }, [cleanup, clearWatchdog])

  /**
   * Release every held final now and resolve once every dispatch issued so far
   * has settled, for a caller about to close the meeting's ingress: a final
   * still held, or still retrying, when ingress closes is refused and lost.
   * Dispatches leave one at a time, so the wait follows the chain's last entry:
   * it resolves once that entry has started and settled, or `FLUSH_TIMEOUT_MS`
   * after it started, which gives every queued final its own window rather
   * than one bound for the whole queue, while a dispatch that never answers
   * still cannot keep the caller's status change from going out.
   *
   * Never a stop: the microphone and the live lease are untouched, and a
   * draining predecessor stays open to deliver its trailing final. Its hold is
   * expired as the drain grace would expire it, so the live lease's later
   * finals are dispatched as they land instead of being held again.
   */
  const flush = useCallback(async (): Promise<void> => {
    const draining = drainingRef.current
    if (draining) expireHold(draining)
    else releaseHeld()
    const tail = dispatchTailRef.current
    if (!tail) return
    await tail.started
    await settledOrTimedOut(tail.dispatch, tail.startedAt + FLUSH_TIMEOUT_MS - Date.now())
  }, [expireHold, releaseHeld])

  return { active, start, stop, flush, supported: transcriptionSupported }
}
