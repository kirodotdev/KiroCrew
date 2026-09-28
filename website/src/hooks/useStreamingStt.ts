import { useCallback, useEffect, useRef, useState } from 'react'
import { acquireMicStream, humanizeMicError, createLevelMeter, setPreferredMicId, activeDeviceId } from './mic'
import type { AudioSample } from './mic'
import { streamErrorMessage } from '../lib/sttProviders'
import type { SttModelProgress } from '../lib/sttProviders'
import { joinTranscript } from '../lib/dictationText'
import { i18nT } from '../i18n/t'

/**
 * Streaming STT over `/api/ws/stt`.
 *
 * Emits live partial transcripts via `onPartial` and commits a final
 * joined transcript via `onFinal` when the user stops recording or the
 * backend closes the stream. Falls back silently if the browser lacks
 * AudioWorklet or WebSocket support — callers should then use the
 * batch hook.
 */

/** Wire frame that tells the backend to end the transcription stream. Protocol,
 *  not copy — it is never shown to anyone. */
const STOP_FRAME = JSON.stringify({ type: 'stop' })

/** `status.stage` reported while model weights are still being fetched. */
const STAGE_DOWNLOADING = 'downloading'
/** `status.stage` reported while fetched weights are being loaded into memory. */
const STAGE_PREPARING = 'preparing'
const READY_TIMEOUT_MS = 60000
/**
 * Fallback budget for the retained audio's wait once the backend has ANNOUNCED
 * that it is fetching or loading the model, used only when the announcement
 * carries no `prepare_timeout_ms` of its own.
 *
 * A cold local model is minutes of work — a multi-hundred-megabyte fetch, a digest
 * check, then a GPU-pipeline compile — and the mic is already released by the time
 * this budget starts, so waiting costs the user nothing but the wait. The silent
 * socket keeps the short budget: nothing has said anyone is working, so a longer
 * wait there is just a slower way to report a dead backend.
 *
 * A number chosen HERE can only be wrong, because the wait belongs to the other
 * side: too short abandons a load the backend is still running and discards the
 * utterance, which is the whole defect. So a backend that states its ceiling is
 * believed instead, and this covers the older ones that do not.
 */
const PREPARE_TIMEOUT_FALLBACK_MS = 300000
// Older gateways omit their finalization budget. Give their default five-minute
// native decode ceiling a little transport/cleanup headroom.
const DEFAULT_FINAL_TIMEOUT_MS = 315000
const MAX_TIMER_DELAY_MS = 2147483647
// 16 kHz mono Int16: retain a preparation window of speech before auto-stopping.
const MAX_BUFFERED_BYTES = (READY_TIMEOUT_MS / 1000) * 16000 * 2
const WORKLET_FLUSH_TIMEOUT_MS = 500

export const streamingSupported =
  typeof window !== 'undefined' &&
  typeof window.AudioContext !== 'undefined' &&
  typeof (window as unknown as { AudioWorkletNode?: unknown }).AudioWorkletNode !== 'undefined' &&
  typeof window.WebSocket !== 'undefined' &&
  typeof navigator !== 'undefined' &&
  typeof navigator.mediaDevices !== 'undefined' &&
  typeof navigator.mediaDevices.getUserMedia === 'function'

interface Opts {
  onPartial: (text: string) => void
  onFinal: (text: string) => void
  /** Capture ended and finals may still arrive, including an automatic buffer stop. */
  onCaptureStop?: () => void
  onError?: (msg: string) => void
  /** Live input level in [0,1] for the recording meter. */
  onLevel?: (v: number) => void
  /** Active capture device: human label + the live track's deviceId. The id is
   *  what makes the source picker data-driven (checkmark on the device that is
   *  ACTUALLY capturing); it may be `''` when permission-scoped redaction hides
   *  it, in which case consumers fall back to the label. */
  onDevice?: (label: string, id: string) => void
  /** Fired when the backend semantic endpointer judges the utterance complete. */
  onEndpoint?: () => void
  /**
   * What the recogniser is doing while this session waits for it — byte progress
   * of a one-time model download, or a load with no bytes to report — and `null`
   * once the recogniser is ready.
   *
   * The local recogniser fetches its weights on first use, and that is between
   * 78 MB and 1.6 GB. Without this the user holds the mic against a session that
   * looks identical to a hang, which is the worst possible first run, so the
   * backend reports its stage and the recording surface shows it.
   */
  onDownload?: (progress: SttModelProgress | null) => void
  /** Unthrottled per-frame audio features for canvas consumers (see mic.ts). */
  sampleRef?: { current: AudioSample }
}

export function useStreamingStt ({ onPartial, onFinal, onCaptureStop, onError, onLevel, onDevice, onEndpoint, onDownload, sampleRef }: Opts) {
  const [recording, setRecording] = useState(false)
  const [draining, setDraining] = useState(false)
  const flushCaptureRef = useRef<(() => void) | null>(null)
  const captureStoppedRef = useRef(false)
  const wsRef = useRef<WebSocket | null>(null)
  const ctxRef = useRef<AudioContext | null>(null)
  const streamRef = useRef<MediaStream | null>(null)
  // Held so the capture device can be swapped mid-session: the worklet (and the
  // WebSocket behind it) survives, only the upstream source node is replaced.
  const sourceRef = useRef<MediaStreamAudioSourceNode | null>(null)
  const workletRef = useRef<AudioWorkletNode | null>(null)
  // Claim-check for concurrent device switches — see switchDevice.
  const switchGenRef = useRef(0)
  const levelStopRef = useRef<(() => void) | null>(null)
  const finalsRef = useRef<string[]>([])
  // Per-start() cancel flag. cancel() flips the CURRENT session's flag; the
  // socket's onclose (which closes over its own session object) reads it to
  // discard instead of delivering. Per-session, not a shared boolean, so a
  // socket superseded by a restart can never mistake a new session for its own.
  const sessionRef = useRef<{ cancelled: boolean } | null>(null)

  // `ready` itself lives in start()'s closure, but stop() has to know whether
  // the PCM it would be ending is still sitting in the local buffer. These two
  // refs are the only channel between them.
  //
  // Why it matters: capture begins (and `recording` goes true) as soon as the
  // worklet connects, but PCM cannot be SENT until the server's `ready` frame
  // lands ~2-3s later. A stop frame sent inside that window ends the Transcribe
  // stream while the user's speech is still local, so it is transcribed as
  // silence -- which is the normal case for a short push-to-talk tap, not an
  // edge case.
  const readyRef = useRef(false)
  // True once a `status` frame has said the model is being fetched or loaded.
  //
  // It is the only thing that distinguishes the two waits stop() can land in. A
  // socket that has said nothing may be dead, so it keeps the short budget; a
  // backend that announced a cold load is working, and the retained audio waits
  // for it. Without the frame both waits look the same from here, and one budget
  // has to serve a dead socket and a five-minute load at once.
  const prepareAnnouncedRef = useRef(false)
  // The budget the ANNOUNCING backend stated for its own preparation, mirroring
  // what `ready` does with `final_timeout_ms`. Holds the fallback until a frame
  // states one, so a backend that announces preparation without a ceiling is
  // treated exactly as before.
  const prepareTimeoutRef = useRef(PREPARE_TIMEOUT_FALLBACK_MS)
  const finalTimeoutRef = useRef(DEFAULT_FINAL_TIMEOUT_MS)
  const pendingStopRef = useRef(false)
  const pendingStopTimerRef = useRef<number | null>(null)
  // Keep callback refs fresh so the long-lived WS handlers (`ws.onmessage`
  // / `ws.onclose`) always invoke the latest caller-supplied callbacks,
  // not the versions captured when `start()` was invoked.
  const onPartialRef = useRef(onPartial)
  const onFinalRef = useRef(onFinal)
  const onCaptureStopRef = useRef(onCaptureStop)
  const onErrorRef = useRef(onError)
  const onLevelRef = useRef(onLevel)
  const onDeviceRef = useRef(onDevice)
  onPartialRef.current = onPartial
  onFinalRef.current = onFinal
  onCaptureStopRef.current = onCaptureStop
  onErrorRef.current = onError
  onLevelRef.current = onLevel
  onDeviceRef.current = onDevice
  const onEndpointRef = useRef(onEndpoint)
  onEndpointRef.current = onEndpoint
  const onDownloadRef = useRef(onDownload)
  onDownloadRef.current = onDownload

  const endCaptureOnce = useCallback((notify = true) => {
    if (captureStoppedRef.current) return
    captureStoppedRef.current = true
    if (notify) onCaptureStopRef.current?.()
  }, [])

  const cleanup = useCallback((notifyCaptureStop = true) => {
    endCaptureOnce(notifyCaptureStop)
    try { levelStopRef.current?.() } catch { /* ignore */ }
    levelStopRef.current = null
    // Clear the download line on every teardown, including a cancel and the
    // force-cleanup path: a progress figure left on screen after the session it
    // described is gone reads as a transfer that is still running.
    onDownloadRef.current?.(null)
    if (pendingStopTimerRef.current !== null) {
      clearTimeout(pendingStopTimerRef.current)
      pendingStopTimerRef.current = null
    }
    readyRef.current = false
    prepareAnnouncedRef.current = false
    prepareTimeoutRef.current = PREPARE_TIMEOUT_FALLBACK_MS
    pendingStopRef.current = false
    flushCaptureRef.current = null
    try { sourceRef.current?.disconnect() } catch { /* already detached */ }
    if (workletRef.current) workletRef.current.port.onmessage = null
    sourceRef.current = null
    workletRef.current = null
    try { wsRef.current?.close() } catch { /* ignore */ }
    wsRef.current = null
    try { streamRef.current?.getTracks().forEach(t => t.stop()) } catch { /* ignore */ }
    streamRef.current = null
    try { ctxRef.current?.close() } catch { /* ignore */ }
    ctxRef.current = null
    onLevelRef.current?.(0)
    onDeviceRef.current?.('', '')
    setRecording(false)
    setDraining(false)
  }, [endCaptureOnce])

  useEffect(() => () => { cleanup(false) }, [cleanup])

  /** Send the stop frame and hand the socket to the backend to drain. */
  const commitStop = useCallback((ws: WebSocket) => {
    try { ws.send(STOP_FRAME) } catch { /* ignore */ }
    if (pendingStopTimerRef.current !== null) clearTimeout(pendingStopTimerRef.current)
    // Capture has stopped; a slow CPU may still need time for the final decode.
    pendingStopTimerRef.current = window.setTimeout(() => {
      if (wsRef.current !== ws) return
      onErrorRef.current?.(i18nT('hooks.useStreamingStt.stt_connection_lost'))
      cleanup()
    }, finalTimeoutRef.current)
  }, [cleanup])

  /**
   * Arm (or re-arm) the wait for `ready` that a released utterance sits in.
   *
   * Re-armable on purpose, because a budget counted from the release bounds the
   * WORK and the only thing this side can actually judge is SILENCE. A cold
   * model's fetch has no upper bound worth naming -- the weights run to 1.6 GB
   * and the link is whatever the user has -- so any fixed figure is wrong for
   * someone, and being wrong here means discarding a recording the backend was
   * still about to transcribe. Each frame that says work is under way is proof
   * the far side is alive, so it buys the wait another full budget, and the
   * timer only fires once nothing has been heard for one whole budget.
   *
   * The message names the stage for the same reason: a socket that announced a
   * load and then went quiet did not "lose the connection", and reporting it as
   * one sends the user to their network for a model that is still loading.
   */
  const armPreReadyWait = useCallback((ws: WebSocket) => {
    if (pendingStopTimerRef.current !== null) clearTimeout(pendingStopTimerRef.current)
    const announced = prepareAnnouncedRef.current
    pendingStopTimerRef.current = window.setTimeout(() => {
      if (wsRef.current !== ws) return
      onErrorRef.current?.(i18nT(
        announced
          ? 'hooks.useStreamingStt.stt_model_still_loading'
          : 'hooks.useStreamingStt.stt_connection_lost',
      ))
      cleanup()
    }, announced ? prepareTimeoutRef.current : READY_TIMEOUT_MS)
  }, [cleanup])

  const stop = useCallback(() => {
    if (captureStoppedRef.current) return
    endCaptureOnce()
    switchGenRef.current++
    try { levelStopRef.current?.() } catch { /* ignore */ }
    levelStopRef.current = null
    try { streamRef.current?.getTracks().forEach(t => t.stop()) } catch { /* ignore */ }
    streamRef.current = null
    onLevelRef.current?.(0)
    setRecording(false)
    const ws = wsRef.current
    if (!ws || (ws.readyState !== WebSocket.OPEN && ws.readyState !== WebSocket.CONNECTING) || !flushCaptureRef.current) { cleanup(); return }
    setDraining(true)
    // Keep the port until its final short frame arrives; its acknowledgment
    // commits stop after every captured byte.
    flushCaptureRef.current?.()
    if (!readyRef.current && pendingStopTimerRef.current === null) {
      // The mic tracks are already stopped above, so this budget holds only the
      // socket and the retained PCM. Nothing is being recorded while it runs.
      armPreReadyWait(ws)
    }
  }, [cleanup, endCaptureOnce, armPreReadyWait])

  const start = useCallback(async () => {
    if (!streamingSupported || wsRef.current) return false
    finalsRef.current = []
    finalTimeoutRef.current = DEFAULT_FINAL_TIMEOUT_MS
    prepareAnnouncedRef.current = false
    prepareTimeoutRef.current = PREPARE_TIMEOUT_FALLBACK_MS
    captureStoppedRef.current = false
    setDraining(false)
    // Claim this start()'s session token BEFORE getUserMedia. A restart during
    // the (async) acquire immediately replaces sessionRef.current, so a stale
    // socket's onclose can detect supersession via `sessionRef.current !== session`
    // even in the window where wsRef is transiently null (old cleanup nulled it,
    // the new ws not created yet). onclose closes over this object; cancel() sets
    // its .cancelled flag.
    const session = { cancelled: false }
    sessionRef.current = session
    let stream: MediaStream
    try {
      stream = await acquireMicStream()
    } catch (e) {
      // Only the still-current start surfaces the error; a superseded one is moot.
      if (sessionRef.current === session && !session.cancelled) onErrorRef.current?.(humanizeMicError(e))
      return false
    }
    // Superseded or cancelled DURING the acquire — don't build a live socket for a
    // session the user already restarted or cancelled. Release the mic and bail.
    if (sessionRef.current !== session || session.cancelled) {
      stream.getTracks().forEach(t => t.stop())
      return false
    }
    streamRef.current = stream
    onDeviceRef.current?.(stream.getAudioTracks()[0]?.label || '', activeDeviceId(stream))
    levelStopRef.current = createLevelMeter(stream, v => onLevelRef.current?.(v), sampleRef)

    const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
    const ws = new WebSocket(`${proto}//${window.location.host}/api/ws/stt`)
    ws.binaryType = 'arraybuffer'
    wsRef.current = ws

    // Server sends `{"type":"ready"}` after Transcribe stream has started.
    // Client must wait for this before sending PCM — frames sent earlier
    // hit aiohttp's buffer and never reach Transcribe.
    let resolveReady: () => void = () => {}
    let rejectReady: (err: Error) => void = () => {}
    const readyPromise = new Promise<void>((resolve, reject) => {
      resolveReady = resolve
      rejectReady = reject
    })
    // Setup may fail before the audio module finishes loading.
    void readyPromise.catch(() => {})

    let lastPartial = ''
    let reportedError = false
    ws.onmessage = ev => {
      if (typeof ev.data !== 'string' || session.cancelled || sessionRef.current !== session || wsRef.current !== ws) return
      try {
        const msg = JSON.parse(ev.data)
        // `ready` also clears any download line: it is the one frame guaranteed
        // to follow preparation, so the progress cannot be left on screen by a
        // backend that reports no closing `status`.
        if (msg.type === 'ready') {
          // Finalization can include waiting behind a decode already in flight.
          // The server owns that budget; preparation latency is a separate limit.
          const timeout = msg.final_timeout_ms
          if (typeof timeout === 'number' && Number.isFinite(timeout) && timeout > 0 && timeout <= MAX_TIMER_DELAY_MS) {
            finalTimeoutRef.current = timeout
          }
          onDownloadRef.current?.(null)
          prepareAnnouncedRef.current = false
          prepareTimeoutRef.current = PREPARE_TIMEOUT_FALLBACK_MS
          resolveReady()
        }
        else if (msg.type === 'partial') {
          const text = msg.text || ''
          lastPartial = text
          // Transcribe partials cover only the current unstable utterance;
          // emit accumulated finals + current partial so the UI grows
          // monotonically instead of flickering between utterances.
          onPartialRef.current(joinTranscript([...finalsRef.current, text]))
        }
        else if (msg.type === 'final') {
          if (msg.text) finalsRef.current.push(msg.text)
          lastPartial = ''  // this partial has been finalized by Transcribe
          // Re-emit so UI reflects the new committed segment even if no
          // follow-up partial arrives (e.g. user stops mid-silence).
          onPartialRef.current(joinTranscript(finalsRef.current))
        } else if (msg.type === 'error') {
          // Keyed off `code`, not `message`: the backend's message is advisory
          // English and this UI renders in 12 languages. It reaches a state of its
          // own in the consumer, which is what keeps the `onclose` below -- where a
          // failed session has no finals to deliver -- from clearing the one
          // explanation the user got.
          reportedError = true
          onErrorRef.current?.(
            streamErrorMessage(String(msg.code || ''), String(msg.message || '')) ||
            i18nT('hooks.useStreamingStt.stt_error'),
          )
          rejectReady(new Error(msg.message || 'stt error'))
          // Fatal frames end capture immediately; a native decoder may take
          // time to abort before the server's close reaches the browser.
          cleanup()
        } else if (msg.type === 'endpoint') {
          // Backend semantic endpointer judged the utterance complete.
          // The composer already holds the streamed transcript (via onPartial),
          // so the caller can submit directly.
          if (msg.complete) onEndpointRef.current?.()
        } else if (msg.type === 'status') {
          // Preparation progress, ahead of `ready`. Two stages say work is under
          // way -- the weight fetch, which has bytes, and the load that follows
          // it, which has none -- and both are shown, because this line is all
          // the user has while there is no transcript yet. Both also mark the
          // session as preparing, so a release during either one waits for the
          // model instead of for a socket nobody has heard from.
          const preparing = msg.stage === STAGE_DOWNLOADING || msg.stage === STAGE_PREPARING
          if (preparing) prepareAnnouncedRef.current = true
          // The announcing backend owns this ceiling, so take the figure it
          // sends rather than assuming one, exactly as `ready` takes
          // `final_timeout_ms`. Range-checked: a malformed or absent figure
          // leaves the fallback alone instead of arming a timer that either
          // fires at once or never fires at all.
          const prepareMs = msg.prepare_timeout_ms
          if (
            preparing
            && typeof prepareMs === 'number'
            && Number.isFinite(prepareMs)
            && prepareMs > 0
            && prepareMs <= MAX_TIMER_DELAY_MS
          ) {
            prepareTimeoutRef.current = prepareMs
          }
          // A frame saying work is under way is proof the far side is alive, so
          // an already-released utterance gets its full budget again from here.
          // Without this the deadline is counted from the release and expires
          // mid-fetch on a slow link however large the figure is, which is the
          // discarded-recording defect again at a later minute.
          if (preparing && !readyRef.current && pendingStopTimerRef.current !== null) {
            armPreReadyWait(ws)
          }
          onDownloadRef.current?.(
            preparing
              ? {
                  done: Number(msg.downloaded_bytes) || 0,
                  total: Number(msg.total_bytes) || 0,
                  stage: msg.stage === STAGE_PREPARING ? 'preparing' : 'downloading',
                }
              : null,
          )
        }
      } catch { /* ignore */ }
    }
    ws.onclose = () => {
      // Settle the startup promise FIRST — always, even on cancel — so a cancel
      // that fires before `ready` unblocks start()'s `await readyPromise` and
      // never wedges the caller's startingRef. (No-op once already resolved.)
      rejectReady(new Error('ws closed before ready'))
      // Only the socket that is still the current one may tear down the shared
      // refs; a socket superseded by a restart must not cleanup() the new
      // session's stream. On cancel, cleanup() already nulled wsRef, so this is
      // false and the redundant teardown is skipped.
      const isCurrent = wsRef.current === ws
      // Supersession is detected via the SESSION TOKEN, not wsRef: a restart
      // claims sessionRef.current BEFORE its getUserMedia resolves, so in that
      // window wsRef is transiently null yet this socket IS superseded. Keying on
      // wsRef would wrongly deliver this stale transcript into the restarting
      // session. When sessionRef still points at THIS session (no successor),
      // the timeout-hang fallback below still delivers.
      const superseded = sessionRef.current !== session
      if (session.cancelled || superseded) { if (isCurrent) cleanup(); return }
      // Finals commit earlier utterances; a last partial belongs to the next
      // unfinished utterance and must survive an interrupted connection too.
      const combined = joinTranscript([...finalsRef.current, lastPartial])
      if (!captureStoppedRef.current && !reportedError) {
        onErrorRef.current?.(i18nT('hooks.useStreamingStt.stt_connection_lost'))
      }
      if (combined) onFinalRef.current(combined)
      else onPartialRef.current('')  // clear any dangling partial when nothing transcribed
      if (isCurrent) cleanup()
    }

    // Capture while the socket connects; readiness buffering preserves the
    // opening word even on a slow handshake.
    ws.onerror = () => {
      if (session.cancelled || sessionRef.current !== session || reportedError) return
      reportedError = true
      onErrorRef.current?.(i18nT('hooks.useStreamingStt.stt_connection_error'))
      rejectReady(new Error('ws connection failed'))
    }

    const ctx = new AudioContext()
    ctxRef.current = ctx
    try {
      await ctx.audioWorklet.addModule('/pcm-worklet.js')
    } catch {
      if (sessionRef.current === session && !session.cancelled) {
        onErrorRef.current?.(i18nT('hooks.useStreamingStt.audio_worklet_unavailable'))
        cleanup()
      }
      return false
    }
    if (session.cancelled || sessionRef.current !== session || wsRef.current !== ws) {
      void ctx.close()
      return false
    }
    try {
      if (ctx.state === 'suspended') await ctx.resume()
    } catch {
      if (sessionRef.current === session) {
        onErrorRef.current?.(i18nT('hooks.useStreamingStt.audio_worklet_unavailable'))
        cleanup()
      }
      return false
    }
    if (session.cancelled || sessionRef.current !== session || wsRef.current !== ws) return false
    const source = ctx.createMediaStreamSource(stream)
    const node = new AudioWorkletNode(ctx, 'pcm-worklet')
    sourceRef.current = source
    workletRef.current = node
    // Preserve the complete opening while the recognizer prepares. At the
    // preparation-window limit, stop capture and drain the retained audio rather
    // than rotating the buffer and silently discarding the user's first words.
    // The server inbox admits this ready-time burst plus the worklet's short tail.
    let ready = false
    let bufferedBytes = 0
    const buffer: ArrayBuffer[] = []
    let captureFlushed = false
    let flushTimer: ReturnType<typeof setTimeout> | null = null
    const finishCapture = () => {
      if (captureFlushed) return
      captureFlushed = true
      if (flushTimer !== null) clearTimeout(flushTimer)
      node.port.onmessage = null
      if (sessionRef.current !== session || session.cancelled || wsRef.current !== ws) return
      if (ready && ws.readyState === WebSocket.OPEN) commitStop(ws)
      else pendingStopRef.current = true
    }
    flushCaptureRef.current = () => {
      // Disconnect first: room audio after release must not enter the tail flush.
      sourceRef.current?.disconnect()
      if (typeof node.port.postMessage === 'function') {
        flushTimer = setTimeout(finishCapture, WORKLET_FLUSH_TIMEOUT_MS)
        node.port.postMessage({ type: 'flush' })
      } else finishCapture()
    }
    node.port.onmessage = e => {
      if (e.data?.type === 'flushed') { finishCapture(); return }
      if (captureFlushed || session.cancelled || sessionRef.current !== session) return
      const chunk = e.data as ArrayBuffer
      if (!(chunk instanceof ArrayBuffer)) return
      if (ready) {
        if (ws.readyState === WebSocket.OPEN) {
          try { ws.send(chunk) } catch { /* ignore CLOSING state */ }
        }
        return false
      }
      buffer.push(chunk)
      bufferedBytes += chunk.byteLength
      if (bufferedBytes >= MAX_BUFFERED_BYTES && !captureStoppedRef.current) stop()
    }
    source.connect(node)
    // The processor writes no output samples (silence). Connecting that silent
    // output keeps the graph pulled on browsers that suspend unconnected nodes.
    node.connect(ctx.destination)
    setRecording(true)

    // Startup ends when capture starts; preparation and final decoding have
    // their own states so the composer owns the mic while the model loads.
    void readyPromise.then(() => {
      if (session.cancelled || sessionRef.current !== session || wsRef.current !== ws) return false
      if (ws.readyState === WebSocket.OPEN) {
        for (const chunk of buffer) {
          try { ws.send(chunk) } catch { break }
        }
      }
      buffer.length = 0
      bufferedBytes = 0
      ready = true
      readyRef.current = true
      if (pendingStopRef.current) {
        pendingStopRef.current = false
        if (pendingStopTimerRef.current !== null) clearTimeout(pendingStopTimerRef.current)
        pendingStopTimerRef.current = null
        if (ws.readyState === WebSocket.OPEN) commitStop(ws)
        else cleanup()
      }
    }).catch(() => { /* onclose delivers the transcript and owns teardown */ })
    return true
  }, [cleanup, commitStop, sampleRef, stop, armPreReadyWait])


  /**
   * Swap the capture device WITHOUT ending the transcription session.
   *
   * The WebSocket, the worklet and the accumulated finals all survive — only the
   * upstream `MediaStreamAudioSourceNode` is replaced. That is what makes a
   * mid-sentence switch cost a sliver of audio (the gap between stopping the old
   * track and the new one delivering its first frame, ~0.2s in practice) instead
   * of the whole utterance.
   *
   * A no-op when not capturing: the next `start()` reads the saved preference
   * anyway, so there is nothing to do.
   */
  const switchDevice = useCallback(async (deviceId: string) => {
    setPreferredMicId(deviceId)
    const ctx = ctxRef.current
    const worklet = workletRef.current
    if (!ctx || !worklet || !streamRef.current) return

    // Nothing to do when we are ALREADY capturing from that device. Decided here,
    // not in the menu: the menu only knows the saved preference, and re-picking the
    // checked entry is meaningful precisely when the session STARTED on a fallback
    // device (start()'s acquire falls back when the saved one is gone or busy) —
    // that tap is the user's retry. Keying on the live track makes it a
    // no-op only when it truly is one, so a redundant tap costs no audio and a
    // corrective tap still re-acquires.
    //
    // Monotonic generation, claimed BEFORE both the no-op check and the await.
    //
    // Before the await: two switches in flight (pick A, pick B before A resolves)
    // complete in acquisition order, not click order — B could connect first and
    // then A, arriving later, would replace it, leaving the graph on A while the UI
    // and the saved preference both say B, and every word spoken after that lost.
    // Only the newest claim may mutate.
    //
    // Before the no-op check: returning without claiming would leave an in-flight
    // switch owning the current generation, so pick B then re-pick the live device
    // A and B — still resolving — goes on to replace the graph even though the
    // user's last action said "stay on A" and `setPreferredMicId(A)` already ran.
    // The saved preference and the audio graph would then disagree.
    const gen = ++switchGenRef.current
    if (activeDeviceId(streamRef.current) === deviceId) return

    let next: MediaStream
    try {
      // EXPLICIT pick ⇒ `exact`, no fallback (see acquireMicStream): a switch
      // that cannot be honored fails loudly here and the old source keeps
      // running, instead of `ideal` silently handing back the previous device
      // while the picker claimed the switch happened.
      next = await acquireMicStream(deviceId)
    } catch (e) {
      // Keep the old source running — a failed switch must not end the session.
      // Only the newest attempt owns the error surface; a superseded one is moot.
      if (gen === switchGenRef.current) onErrorRef.current?.(humanizeMicError(e))
      return
    }
    // Re-check after the await: superseded by a newer switch, or the graph was
    // torn down by stop() while acquiring (connecting then would resurrect a
    // dead session).
    if (gen !== switchGenRef.current || ctxRef.current !== ctx || workletRef.current !== worklet) {
      next.getTracks().forEach(t => t.stop())
      return
    }

    const prevStream = streamRef.current
    try { sourceRef.current?.disconnect() } catch { /* already detached */ }
    try { levelStopRef.current?.() } catch { /* ignore */ }
    levelStopRef.current = null

    const source = ctx.createMediaStreamSource(next)
    source.connect(worklet)
    sourceRef.current = source
    streamRef.current = next
    // Report the ACTUAL device off the live track. With `exact` acquisition a
    // success genuinely IS the requested device, but the session-start fallback
    // path can still land elsewhere, so the track stays the single source of
    // truth. The saved preference is deliberately NOT rewritten on failure — a
    // device that enumerates but cannot be opened right now (busy, held by
    // another app) would otherwise have the user's explicit pick permanently
    // replaced, so it would never be tried again once free.
    onDeviceRef.current?.(next.getAudioTracks()[0]?.label || '', activeDeviceId(next))
    levelStopRef.current = createLevelMeter(next, v => onLevelRef.current?.(v), sampleRef)
    // Stop the old tracks LAST: releasing them before the replacement is live
    // would surrender the mic and can drop the device's hardware clock.
    prevStream.getTracks().forEach(t => t.stop())
  }, [sampleRef])

  // Immediate discard (Esc). Unlike stop(), does NOT drain: tears down the
  // socket, mic tracks and AudioContext right away so capture ends the instant
  // the user cancels — no graceful-drain window. Marks
  // the current session cancelled so the resulting onclose delivers no final;
  // onclose still runs (settling any pending startup promise so the caller is
  // never wedged), it just discards.
  const cancel = useCallback(() => {
    if (sessionRef.current) sessionRef.current.cancelled = true
    // Detach onmessage so a partial/endpoint message already queued on THIS
    // socket cannot fire after cancel. Otherwise, if the user Escapes and
    // immediately restarts, the restart re-arms the shared onPartial/onEndpoint
    // callbacks, and a late message from the discarded socket would inject or
    // submit the abandoned dictation into the NEW session. onclose stays
    // attached so it still settles readyPromise (no startup wedge).
    const ws = wsRef.current
    if (ws) ws.onmessage = null
    cleanup(false)
  }, [cleanup])

  return { recording, draining, start, stop, switchDevice, cancel }
}
