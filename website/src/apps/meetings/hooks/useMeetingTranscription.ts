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
//
// The upstream app had a second provider backed by a separately built local
// daemon; it is gone, so this is the single path.
//
// Every FINAL segment is POSTed to the meeting's dispatch endpoint, which is
// what feeds the agents. Partials only drive the live caption.
//
// ── This hook owns the meeting's ONE capture pipeline ──────────────────────────
// Microphone (worklet input 0) plus, optionally, system audio (input 1) so the
// remote participants are transcribed too — a meeting where only the local mic is
// captured produces notes with half the conversation missing. Both are mixed
// inside `/pcm-worklet.js`; see its TWO INPUTS, ONE STREAM note.
//
// Every PCM chunk is also teed to `onPcm`, which is how `useMeetingRecording`
// persists a WAV from the same audio. The tee exists so a meeting prompts for the
// microphone once and for a display surface once, no matter how many consumers
// the audio has.

import { useCallback, useEffect, useRef, useState } from 'react'

import { MeetingsApiError, meetingsApi, type TranscriptSegment } from '../api'
import { reportIfMicDenied } from '../../../hooks/mic'
import {
  defaultSystemAudioDeps,
  requestSystemAudio,
  stopStream,
  type SystemAudioFailure,
} from '../audio/systemAudio'

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
    // +1 for the space this segment would be joined with.
    const cost = segments[i].length + (kept.length === 0 ? 0 : 1)
    if (length + cost > CAPTION_WINDOW_CHARS) break
    kept.unshift(segments[i])
    length += cost
  }
  if (kept.length > 0) return kept.join(' ')

  // Even the newest segment alone overflows the window: keep its tail, cut at a
  // word boundary so no word is split mid-token. A segment with no spaces at all
  // is returned as-is rather than mangled.
  const tail = segments[segments.length - 1].slice(-CAPTION_WINDOW_CHARS)
  const firstSpace = tail.indexOf(' ')
  return firstSpace === -1 ? tail : tail.slice(firstSpace + 1)
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
   * Called with each final segment. Returns the text to DISPATCH — which may be
   * only the new suffix of a growing final — or `false` to suppress it entirely.
   * `void` keeps the caption-only callers working without an opt-in.
   */
  onFinal?: (text: string) => string | boolean | void
  /** Called with the recognizer's in-flight text; never persisted. */
  onPartial?: (text: string) => void
  /** Called after the backend has durably accepted a final segment. */
  onCommitted?: (segment: TranscriptSegment) => void
  /** Called with a user-facing message when transcription cannot run. */
  onError?: (message: string) => void
  /**
   * Called with every PCM chunk the worklet produces, mic and system audio already
   * mixed, BEFORE the STT ready-gate. Unconditional on purpose: a recording has its
   * own buffer-until-ready logic, and audio spoken while the STT stream is still
   * coming up (2-3 s cold) still belongs in the file.
   */
  onPcm?: (chunk: ArrayBuffer) => void
  /** Called when a connected system-audio stream ends on its own (user hit "Stop sharing"). */
  onSystemAudioEnded?: () => void
}

export function useMeetingTranscription({
  meetingId,
  onCaption,
  onFinal,
  onPartial,
  onCommitted,
  onError,
  onPcm,
  onSystemAudioEnded,
}: Options) {
  const [active, setActive] = useState(false)
  const [systemAudio, setSystemAudio] = useState(false)
  const wsRef = useRef<WebSocket | null>(null)
  const ctxRef = useRef<AudioContext | null>(null)
  const streamRef = useRef<MediaStream | null>(null)
  /** The live display-capture audio stream, or null. Outlives a socket reconnect. */
  const sysStreamRef = useRef<MediaStream | null>(null)
  /** The worklet node currently receiving audio, so system audio can join a running capture. */
  const nodeRef = useRef<AudioWorkletNode | null>(null)
  const sysSourceRef = useRef<MediaStreamAudioSourceNode | null>(null)
  /**
   * Whether the user has asked for system audio.
   *
   * Separate from `sysStreamRef` because it must SURVIVE the watchdog's
   * reconnect: that tears the AudioContext down, which invalidates the source
   * node but not the MediaStream, so the intent is what tells the rebuilt
   * pipeline to reconnect the stream it still holds — without prompting the user
   * for a display surface a second time.
   */
  const sysWantedRef = useRef(false)
  const watchdogRef = useRef<ReturnType<typeof setInterval> | null>(null)
  const lastFrameRef = useRef(0)
  const finalsRef = useRef<string[]>([])
  const stoppingRef = useRef(false)
  const dispatchBlockedRef = useRef(false)
  /** True from entering `start()` until the socket is live (or it gave up). */
  const startingRef = useRef(false)

  // Keep callback refs fresh so the long-lived socket handlers always invoke the
  // latest caller-supplied callbacks, not the ones captured at start().
  const onCaptionRef = useRef(onCaption)
  const onFinalRef = useRef(onFinal)
  const onPartialRef = useRef(onPartial)
  const onCommittedRef = useRef(onCommitted)
  const onErrorRef = useRef(onError)
  const onPcmRef = useRef(onPcm)
  const onSystemAudioEndedRef = useRef(onSystemAudioEnded)
  onCaptionRef.current = onCaption
  onFinalRef.current = onFinal
  onPartialRef.current = onPartial
  onCommittedRef.current = onCommitted
  onErrorRef.current = onError
  onPcmRef.current = onPcm
  onSystemAudioEndedRef.current = onSystemAudioEnded

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

  const clearWatchdog = useCallback(() => {
    if (watchdogRef.current) {
      clearInterval(watchdogRef.current)
      watchdogRef.current = null
    }
  }, [])

  const cleanup = useCallback(() => {
    clearWatchdog()
    try { wsRef.current?.close() } catch { /* already closing */ }
    wsRef.current = null
    try { streamRef.current?.getTracks().forEach(t => t.stop()) } catch { /* ignore */ }
    streamRef.current = null
    // The system-audio STREAM deliberately outlives this teardown while the user
    // still wants it: `cleanup` also runs on the watchdog's reconnect, and stopping
    // the display capture there would pop the browser's share picker again every
    // time the socket stalled. Its source NODE does not outlive the context, so it
    // is dropped and rebuilt by `start`. `detachSystemAudio` (and unmount) are what
    // actually stop the stream.
    sysSourceRef.current = null
    nodeRef.current = null
    if (!sysWantedRef.current) {
      stopStream(sysStreamRef.current)
      sysStreamRef.current = null
      setSystemAudio(false)
    }
    try { ctxRef.current?.close() } catch { /* ignore */ }
    ctxRef.current = null
    // Release the in-progress guard here as well as on the success path: `cleanup`
    // runs on EVERY teardown, including each of `start`'s own failure exits, so a
    // start that dies partway cannot leave the flag stuck and block every later
    // attempt for the rest of the meeting.
    startingRef.current = false
    onPartialRef.current?.('')
    setActive(false)
  }, [clearWatchdog])

  // Never leave the microphone — or a display capture — open when the page
  // unmounts. Clearing the intent first is what makes `cleanup` release the
  // system-audio stream it otherwise deliberately keeps across a reconnect.
  useEffect(() => () => {
    sysWantedRef.current = false
    cleanup()
  }, [cleanup])

  /**
   * Connect the held system-audio stream to input 1 of the running worklet node.
   *
   * Idempotent, and a no-op when the capture pipeline is not up yet — `start`
   * calls it once the node exists, so a stream granted while the socket happened
   * to be down still lands as soon as the pipeline is rebuilt.
   */
  const wireSystemAudio = useCallback((): boolean => {
    const ctx = ctxRef.current
    const node = nodeRef.current
    const stream = sysStreamRef.current
    if (!ctx || !node || !stream) return false
    if (sysSourceRef.current) return true
    try {
      const sysSource = ctx.createMediaStreamSource(stream)
      sysSource.connect(node, 0, 1)
      sysSourceRef.current = sysSource
    } catch {
      return false
    }
    setSystemAudio(true)
    return true
  }, [])

  /** Stop the display capture and drop it out of the graph. */
  const detachSystemAudio = useCallback(() => {
    sysWantedRef.current = false
    try { sysSourceRef.current?.disconnect() } catch { /* context already closed */ }
    sysSourceRef.current = null
    stopStream(sysStreamRef.current)
    sysStreamRef.current = null
    setSystemAudio(false)
  }, [])

  /**
   * Ask for a display surface and mix its audio into the transcript.
   *
   * Returns `null` on success or the reason it did not happen. Failure is never
   * fatal: the microphone keeps transcribing, so the caller reports the reason and
   * carries on. That is why this is a separate, explicit call rather than part of
   * `start` — the browser's share picker is intrusive, and a meeting must be able
   * to begin without one.
   */
  const attachSystemAudio = useCallback(async (): Promise<SystemAudioFailure | null> => {
    if (sysStreamRef.current) {
      sysWantedRef.current = true
      wireSystemAudio()
      return null
    }
    const outcome = await requestSystemAudio(defaultSystemAudioDeps())
    if (!outcome.ok) return outcome.reason
    sysStreamRef.current = outcome.stream
    sysWantedRef.current = true
    // The user can revoke the share from the browser's own "Stop sharing" bar, which
    // we only learn about from the track. Without this the graph keeps a dead input
    // connected and the UI keeps claiming the remote side is being captured.
    for (const track of outcome.stream.getAudioTracks()) {
      track.addEventListener('ended', () => {
        if (sysStreamRef.current !== outcome.stream) return
        detachSystemAudio()
        onSystemAudioEndedRef.current?.()
      })
    }
    wireSystemAudio()
    return null
  }, [detachSystemAudio, wireSystemAudio])

  const start = useCallback(async () => {
    if (!transcriptionSupported) {
      onErrorRef.current?.('unsupported')
      return
    }
    if (wsRef.current) return
    // `wsRef` alone is not enough: it is only assigned once the socket is created,
    // and everything before that is awaited (getUserMedia, the AudioWorklet module).
    // Two calls landing in that window both proceed and end up with two microphone
    // streams and two sockets, whose finals are dispatched twice.
    //
    // The watchdog is the path that reaches it: its `cleanup()` clears `active`,
    // which the session hook now watches to restart a dropped socket (that is the
    // fix for a silent disconnect) — so the watchdog's own `start()` and the effect's
    // race. Guarding here rather than in the effect keeps the invariant with the
    // function that owns it, and covers any future caller too.
    if (startingRef.current) return
    startingRef.current = true
    stoppingRef.current = false
    finalsRef.current = []

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

    const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
    const ws = new WebSocket(`${proto}//${window.location.host}/api/ws/stt`)
    ws.binaryType = 'arraybuffer'
    wsRef.current = ws

    let resolveReady: () => void = () => {}
    const readyPromise = new Promise<void>(resolve => { resolveReady = resolve })

    let lastPartial = ''
    ws.onmessage = ev => {
      if (typeof ev.data !== 'string') return
      lastFrameRef.current = Date.now()
      let msg: { type?: string; text?: string; message?: string }
      try {
        msg = JSON.parse(ev.data)
      } catch {
        return
      }
      if (msg.type === 'ready') {
        resolveReady()
        return
      }
      if (msg.type === 'partial') {
        lastPartial = msg.text || ''
        onPartialRef.current?.(lastPartial)
        onCaptionRef.current(captionWindow(finalsRef.current, lastPartial))
        return
      }
      if (msg.type === 'final') {
        const text = (msg.text || '').trim()
        lastPartial = ''
        onPartialRef.current?.('')
        if (!text) return
        finalsRef.current.push(text)
        if (finalsRef.current.length > CAPTION_FINALS_LIMIT) {
          finalsRef.current.splice(0, finalsRef.current.length - CAPTION_FINALS_LIMIT)
        }
        onCaptionRef.current(captionWindow(finalsRef.current))
        // The caller's duplicate check gates the dispatch: an overlapping final
        // still belongs in the caption (above), but must not be sent to the
        // agents a second time.
        // The caller's dedup decides WHAT to dispatch, not just whether to. STT
        // emits a growing final (`"yes"` then `"yes please"`), so a boolean answer
        // could only suppress the whole thing and lose the added words; a string
        // lets it hand back just the new suffix.
        const decision = onFinalRef.current?.(text)
        if (decision === false) return
        const toDispatch = typeof decision === 'string' ? decision : text
        if (!toDispatch.trim()) return
        // This is the line that reaches the agents. It must not tear down the
        // stream on failure — but it must not be swallowed either, or a transient
        // request failure silently drops that stretch of the meeting from the
        // notes and tasks. Retried on a short bounded schedule, then reported.
        void dispatchWithRetry(toDispatch)
        return
      }
      if (msg.type === 'error') {
        onErrorRef.current?.(msg.message || 'error')
        resolveReady()
      }
    }
    ws.onclose = () => {
      // Ignore a close for a socket we have already replaced. `close` is
      // delivered asynchronously, so the watchdog's reconnect and a
      // stop()-then-start() both create the NEW socket before the OLD one's
      // close event lands — and an unguarded cleanup() here would then tear
      // down that new session (mic tracks stopped, AudioContext closed,
      // active=false) moments after it came up. The session binding keys only
      // on `status`, which has not changed, so nothing would ever restart it:
      // transcription would stay dead for the rest of the meeting while the UI
      // still showed Live.
      if (wsRef.current !== ws) return
      // A close we did not ask for, while recording, is a transport failure:
      // surface it rather than silently going quiet mid-meeting.
      if (!stoppingRef.current) onErrorRef.current?.('disconnected')
      cleanup()
    }

    try {
      await new Promise<void>((resolve, reject) => {
        ws.onerror = () => reject(new Error('open failed'))
        ws.onopen = () => resolve()
      })
    } catch {
      onErrorRef.current?.('connection')
      cleanup()
      return
    }
    ws.onerror = () => { onErrorRef.current?.('connection') }

    const ctx = new AudioContext()
    ctxRef.current = ctx
    try {
      await ctx.audioWorklet.addModule('/pcm-worklet.js')
    } catch {
      onErrorRef.current?.('worklet')
      cleanup()
      return
    }
    const source = ctx.createMediaStreamSource(stream)
    // TWO inputs: microphone on 0, system audio on 1. The worklet sums them, and
    // an unconnected input 1 contributes nothing — so this is safe before (and
    // without) any system audio, which is what keeps mic-only meetings unchanged.
    //
    // `channelCount: 1` + `'explicit'` makes the graph DOWNMIX each input to mono
    // rather than leaving the worklet to read channel 0 and silently discard the
    // rest. Display capture is routinely stereo, and a participant panned to the
    // right channel would otherwise be transcribed at whatever leaked into the left.
    const node = new AudioWorkletNode(ctx, 'pcm-worklet', {
      numberOfInputs: 2,
      channelCount: 1,
      channelCountMode: 'explicit',
    })
    nodeRef.current = node

    // Buffer PCM until the server is ready, then flush and switch to live send.
    // Over the cap, drop the OLDEST frames — the most recent speech wins.
    let ready = false
    let bufferedBytes = 0
    const buffer: ArrayBuffer[] = []
    node.port.onmessage = e => {
      const chunk = e.data as ArrayBuffer
      // Tee FIRST and unconditionally: a recording keeps its own pre-ready buffer,
      // and it must not inherit this socket's ready-gate or its stalls. Safe to
      // hand the same ArrayBuffer to both — `WebSocket.send` copies it into the
      // outgoing queue rather than retaining it.
      onPcmRef.current?.(chunk)
      if (ready) {
        if (ws.readyState === WebSocket.OPEN) {
          try { ws.send(chunk) } catch { /* CLOSING */ }
        }
        return
      }
      buffer.push(chunk)
      bufferedBytes += chunk.byteLength
      while (bufferedBytes > MAX_BUFFERED_BYTES && buffer.length > 1) {
        bufferedBytes -= buffer.shift()!.byteLength
      }
    }
    source.connect(node, 0, 0)
    // The worklet's output is never heard — do NOT connect it to the destination.
    // Re-attach system audio the user already granted. The stream survived the
    // teardown; only its source node, which belonged to the closed context, did not.
    if (sysWantedRef.current) wireSystemAudio()
    startingRef.current = false
    setActive(true)

    lastFrameRef.current = Date.now()
    clearWatchdog()
    watchdogRef.current = setInterval(() => {
      if (stoppingRef.current || !wsRef.current) return
      if (Date.now() - lastFrameRef.current > STALL_TIMEOUT_MS) {
        // A silent stream is indistinguishable from a wedged one from here, and
        // a wedged one loses the rest of the meeting — so reconnect.
        cleanup()
        void start()
      }
    }, WATCHDOG_INTERVAL_MS)

    await readyPromise
    if (ws.readyState === WebSocket.OPEN) {
      for (const chunk of buffer) {
        try { ws.send(chunk) } catch { break }
      }
    }
    buffer.length = 0
    bufferedBytes = 0
    ready = true
    // `meetingId` is no longer a direct dependency: the only use left in here is
    // inside `dispatchWithRetry`, which closes over it and is listed instead.
  }, [cleanup, clearWatchdog, dispatchWithRetry, wireSystemAudio])

  const stop = useCallback(() => {
    stoppingRef.current = true
    clearWatchdog()
    const ws = wsRef.current
    if (!ws || ws.readyState !== WebSocket.OPEN) {
      cleanup()
      return
    }
    // Ask the server to stop and let IT close, so trailing finals still arrive.
    // Force cleanup after a grace period so the UI can never get stuck.
    try { ws.send(STOP_FRAME) } catch { /* ignore */ }
    window.setTimeout(() => {
      if (wsRef.current === ws) cleanup()
    }, CLOSE_GRACE_MS)
  }, [cleanup, clearWatchdog])

  return {
    active,
    start,
    stop,
    supported: transcriptionSupported,
    /** True while a display-capture stream is mixed into input 1. */
    systemAudio,
    attachSystemAudio,
    detachSystemAudio,
  }
}
