import { useCallback, useEffect, useRef, useState } from 'react'
import { acquireMicStream, humanizeMicError, createLevelMeter, setPreferredMicId, activeDeviceId } from './mic'
import type { AudioSample } from './mic'
import { streamErrorMessage } from '../lib/sttProviders'
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
   * Byte progress of a one-time model download the session is waiting on, or
   * `null` once the recogniser is ready.
   *
   * The local recogniser fetches its weights on first use, and that is between
   * 78 MB and 1.6 GB. Without this the user holds the mic against a session that
   * looks identical to a hang, which is the worst possible first run, so the
   * backend reports byte progress and the recording surface shows it.
   */
  onDownload?: (progress: { done: number; total: number } | null) => void
  /** Unthrottled per-frame audio features for canvas consumers (see mic.ts). */
  sampleRef?: { current: AudioSample }
}

export function useStreamingStt ({ onPartial, onFinal, onError, onLevel, onDevice, onEndpoint, onDownload, sampleRef }: Opts) {
  const [recording, setRecording] = useState(false)
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
  const pendingStopRef = useRef(false)
  const pendingStopTimerRef = useRef<number | null>(null)
  // Keep callback refs fresh so the long-lived WS handlers (`ws.onmessage`
  // / `ws.onclose`) always invoke the latest caller-supplied callbacks,
  // not the versions captured when `start()` was invoked.
  const onPartialRef = useRef(onPartial)
  const onFinalRef = useRef(onFinal)
  const onErrorRef = useRef(onError)
  const onLevelRef = useRef(onLevel)
  const onDeviceRef = useRef(onDevice)
  onPartialRef.current = onPartial
  onFinalRef.current = onFinal
  onErrorRef.current = onError
  onLevelRef.current = onLevel
  onDeviceRef.current = onDevice
  const onEndpointRef = useRef(onEndpoint)
  onEndpointRef.current = onEndpoint
  const onDownloadRef = useRef(onDownload)
  onDownloadRef.current = onDownload

  const cleanup = useCallback(() => {
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
    pendingStopRef.current = false
    try { wsRef.current?.close() } catch { /* ignore */ }
    wsRef.current = null
    try { streamRef.current?.getTracks().forEach(t => t.stop()) } catch { /* ignore */ }
    streamRef.current = null
    try { ctxRef.current?.close() } catch { /* ignore */ }
    ctxRef.current = null
    onLevelRef.current?.(0)
    onDeviceRef.current?.('', '')
    setRecording(false)
  }, [])

  useEffect(() => () => { cleanup() }, [cleanup])

  /** Send the stop frame and hand the socket to the backend to drain. */
  const commitStop = useCallback((ws: WebSocket) => {
    try { ws.send(STOP_FRAME) } catch { /* ignore */ }
    // Do NOT call ws.close() here — let the backend flush any in-flight
    // finals from Transcribe and close the socket itself. Our onclose
    // handler joins finalsRef and fires onFinal. If the backend hangs,
    // force-cleanup after 8s so the UI never gets stuck. Must exceed
    // the backend's 3s handler-drain timeout + a safety margin for
    // end_stream() and network RTT.
    window.setTimeout(() => {
      if (wsRef.current === ws) {
        try { ws.close() } catch { /* ignore */ }
        cleanup()
      }
    }, 8000)
  }, [cleanup])

  const start = useCallback(async () => {
    if (!streamingSupported || wsRef.current) return
    finalsRef.current = []
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
      if (sessionRef.current === session) onErrorRef.current?.(humanizeMicError(e))
      return
    }
    // Superseded or cancelled DURING the acquire — don't build a live socket for a
    // session the user already restarted or cancelled. Release the mic and bail.
    if (sessionRef.current !== session || session.cancelled) {
      stream.getTracks().forEach(t => t.stop())
      return
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

    let lastPartial = ''
    ws.onmessage = ev => {
      if (typeof ev.data !== 'string') return
      try {
        const msg = JSON.parse(ev.data)
        // `ready` also clears any download line: it is the one frame guaranteed
        // to follow preparation, so the progress cannot be left on screen by a
        // backend that reports no closing `status`.
        if (msg.type === 'ready') { onDownloadRef.current?.(null); resolveReady() }
        else if (msg.type === 'partial') {
          const text = msg.text || ''
          lastPartial = text
          // Transcribe partials cover only the current unstable utterance;
          // emit accumulated finals + current partial so the UI grows
          // monotonically instead of flickering between utterances.
          const prefix = finalsRef.current.join(' ')
          onPartialRef.current(prefix ? `${prefix} ${text}`.trim() : text)
        }
        else if (msg.type === 'final') {
          if (msg.text) finalsRef.current.push(msg.text)
          lastPartial = ''  // this partial has been finalized by Transcribe
          // Re-emit so UI reflects the new committed segment even if no
          // follow-up partial arrives (e.g. user stops mid-silence).
          onPartialRef.current(finalsRef.current.join(' '))
        } else if (msg.type === 'error') {
          // Keyed off `code`, not `message`: the backend's message is advisory
          // English and this UI renders in 12 languages. It reaches a state of its
          // own in the consumer, which is what keeps the `onclose` below -- where a
          // failed session has no finals to deliver -- from clearing the one
          // explanation the user got.
          onErrorRef.current?.(
            streamErrorMessage(String(msg.code || ''), String(msg.message || '')) ||
            i18nT('hooks.useStreamingStt.stt_error'),
          )
          rejectReady(new Error(msg.message || 'stt error'))
        } else if (msg.type === 'endpoint') {
          // Backend semantic endpointer judged the utterance complete.
          // The composer already holds the streamed transcript (via onPartial),
          // so the caller can submit directly.
          if (msg.complete) onEndpointRef.current?.()
        } else if (msg.type === 'status') {
          // Preparation progress, ahead of `ready`. The only stage with anything
          // to show is the weight download; every other stage is instant, so it
          // clears the line rather than adding a message nobody can act on.
          onDownloadRef.current?.(
            msg.stage === STAGE_DOWNLOADING
              ? { done: Number(msg.downloaded_bytes) || 0, total: Number(msg.total_bytes) || 0 }
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
      // Prefer Transcribe's finals. If none arrived (user stopped before
      // Transcribe finalized), fall back to the last partial so the
      // user's words aren't lost.
      const combined = finalsRef.current.length
        ? finalsRef.current.join(' ').trim()
        : lastPartial.trim()
      if (combined) onFinalRef.current(combined)
      else onPartialRef.current('')  // clear any dangling partial when nothing transcribed
      if (isCurrent) cleanup()
    }

    // Wait only for the WS handshake here — we start the audio graph
    // *before* the server's `ready` and buffer PCM locally so the user
    // can speak immediately. Starting Transcribe server-side takes
    // ~2-3s cold (credential fetch + SigV4 handshake).
    try {
      await new Promise<void>((resolve, reject) => {
        ws.onerror = () => {
          onErrorRef.current?.(i18nT('hooks.useStreamingStt.stt_connection_error'))
          reject(new Error('ws open failed'))
        }
        ws.onopen = () => resolve()
      })
    } catch {
      cleanup()
      return
    }
    // Reassign onerror so mid-session transport failures surface to the
    // user — the promise-reject handler above is dead once resolved.
    ws.onerror = () => { onErrorRef.current?.(i18nT('hooks.useStreamingStt.stt_connection_lost')) }

    const ctx = new AudioContext()
    ctxRef.current = ctx
    try {
      await ctx.audioWorklet.addModule('/pcm-worklet.js')
    } catch {
      onErrorRef.current?.(i18nT('hooks.useStreamingStt.audio_worklet_unavailable'))
      cleanup()
      return
    }
    const source = ctx.createMediaStreamSource(stream)
    const node = new AudioWorkletNode(ctx, 'pcm-worklet')
    sourceRef.current = source
    workletRef.current = node
    // PCM routing: buffer until the server is ready, then flush and switch to live
    // send. The cap bounds memory so a `ready` that never arrives cannot grow it
    // without limit, and the FIFO drop keeps the user's most RECENT speech when the
    // cap is hit.
    //
    // Sized for the slowest READINESS, not for one provider's connect time. At 8s
    // this was calibrated for Transcribe's ~2-3s spin-up, and the local recogniser
    // goes straight through it: a cold resident load compiles a GPU pipeline
    // (measured 7.4s) after verifying the weights' digest (up to ~4s for the largest
    // model), so the first press after a gateway start silently lost the opening
    // seconds of the sentence — the OLDEST frames, which is the half the user said
    // first, and nothing in the UI said so. 16 kHz mono Int16 is 32 KB/s, so 40s of
    // headroom costs 1.25 MB of ArrayBuffers.
    //
    // Deliberately NOT sized against the server's own 1800s prepare ceiling: that
    // covers a multi-hundred-megabyte model DOWNLOAD, which the client is told about
    // with a `downloading` status and would not expect the user to talk through.
    const MAX_BUFFERED_SECS = 40
    const MAX_BUFFERED_BYTES = MAX_BUFFERED_SECS * 32 * 1024
    let ready = false
    let bufferedBytes = 0
    const buffer: ArrayBuffer[] = []
    node.port.onmessage = e => {
      const chunk = e.data as ArrayBuffer
      if (ready) {
        if (ws.readyState === WebSocket.OPEN) {
          try { ws.send(chunk) } catch { /* ignore CLOSING state */ }
        }
        return
      }
      buffer.push(chunk)
      bufferedBytes += chunk.byteLength
      while (bufferedBytes > MAX_BUFFERED_BYTES && buffer.length > 1) {
        const dropped = buffer.shift()!
        bufferedBytes -= dropped.byteLength
      }
    }
    source.connect(node)
    // Worklet output is never heard — do NOT connect node to destination.
    setRecording(true)

    // Now wait for the server's ready signal and flush the buffer.
    try {
      await readyPromise
    } catch {
      // cleanup() was already called by onclose (or will be), and
      // setRecording(false) happens there.
      return
    }
    if (ws.readyState === WebSocket.OPEN) {
      for (const chunk of buffer) {
        try { ws.send(chunk) } catch { break }
      }
    }
    buffer.length = 0
    bufferedBytes = 0
    ready = true
    readyRef.current = true
    // The user may already have released the key while we were waiting. The
    // buffered speech has just gone out, so NOW the stop frame is safe to send:
    // the Transcribe stream ends after the audio rather than before it.
    if (pendingStopRef.current) {
      pendingStopRef.current = false
      if (pendingStopTimerRef.current !== null) {
        clearTimeout(pendingStopTimerRef.current)
        pendingStopTimerRef.current = null
      }
      if (ws.readyState === WebSocket.OPEN) commitStop(ws)
      else cleanup()
    }
  }, [cleanup, commitStop, sampleRef])

  const stop = useCallback(() => {
    const ws = wsRef.current
    if (ws && ws.readyState === WebSocket.OPEN) {
      if (!readyRef.current) {
        // Pre-`ready`: the speech is still in start()'s local buffer, so
        // sending stop NOW would end the Transcribe stream before a single
        // frame of it had been sent. Record the intent instead; the flush in
        // start() commits it the moment `ready` lands.
        pendingStopRef.current = true
        // Deferring the FRAME must not defer the END OF CAPTURE. The worklet
        // handler appends to the same buffer while `ready` is false, so leaving
        // it attached would keep recording the room after the user let go and
        // ship all of it on flush -- extra words in the transcript, and audio
        // captured after release sent to the transcriber. Freeze the buffer at
        // release: detach the handler, stop the level meter, and release the
        // mic. The socket and the already-buffered PCM deliberately survive,
        // because they are what the flush still has to send.
        try {
          if (workletRef.current) workletRef.current.port.onmessage = null
        } catch { /* ignore */ }
        try { levelStopRef.current?.() } catch { /* ignore */ }
        levelStopRef.current = null
        onLevelRef.current?.(0)
        try { streamRef.current?.getTracks().forEach(t => t.stop()) } catch { /* ignore */ }
        // Ceiling, because a `ready` that never arrives would otherwise leave
        // the mic hot forever. 8s matches the buffer cap: past that point the
        // oldest audio is already being dropped FIFO, so waiting longer cannot
        // preserve a whole utterance anyway.
        if (pendingStopTimerRef.current === null) {
          pendingStopTimerRef.current = window.setTimeout(() => {
            pendingStopTimerRef.current = null
            if (wsRef.current === ws && pendingStopRef.current) {
              pendingStopRef.current = false
              cleanup()
            }
          }, 8000)
        }
        return
      }
      commitStop(ws)
    } else {
      // WS never opened or already closing — cleanup directly.
      cleanup()
    }
  }, [cleanup, commitStop])

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
  // the user cancels — no 8s graceful-drain window keeping the mic live. Marks
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
    cleanup()
  }, [cleanup])

  return { recording, start, stop, switchDevice, cancel }
}
