// Durable audio capture for a meeting, over `/api/ws/recording`.
//
// This hook does NOT touch the microphone. `useMeetingTranscription` owns the
// one capture pipeline (mic + system audio + `/pcm-worklet.js`) and tees every
// PCM chunk here through `pushPcm`, so a meeting prompts for audio ONCE and both
// sockets are fed from the same stream. Opening a second `getUserMedia` here
// would also mean a second screen-share picker, which is why the tee exists.
//
// Wire protocol — conforms to `kiro_crew/recording/ws.py:api_ws_recording`:
//   • connect to `/api/ws/recording`
//   • send `{"type":"start", meeting_id, title, language}`; the server replies
//     `{"type":"ready","meeting_id":…}` once the WAV writer is open
//   • send 16 kHz Int16 mono PCM frames as binary; frames arriving BEFORE ready
//     or while paused are discarded server-side, so they are buffered here
//   • `{"type":"pause"}` / `{"type":"resume"}` gate what reaches the file
//   • `{"type":"level","rms":…}` arrives ~5 Hz and drives the meter
//   • send `{"type":"stop"}` and let the SERVER close, so the WAV is finalized
//     before the socket goes away
//
// Why a separate socket from `/api/ws/stt`: that one is capped at 300 s
// unconditionally because it exists for dictation. This one caps duration only
// when the STT provider bills per second (AWS Transcribe), so a local provider
// can hold it for a whole meeting. It is also the only path that persists audio.

import { useCallback, useEffect, useRef, useState } from 'react'

/** The stop frame the recording protocol expects (a wire frame, not copy). */
const STOP_FRAME = JSON.stringify({ type: 'stop' })
const PAUSE_FRAME = JSON.stringify({ type: 'pause' })
const RESUME_FRAME = JSON.stringify({ type: 'resume' })

/**
 * Cap the locally buffered pre-`ready` audio at ~8 s (16 kHz mono Int16 = 32 KB/s).
 *
 * Matches `useMeetingTranscription`'s cap for the same reason: the server takes a
 * moment to open the WAV writer, and audio spoken in that window belongs in the
 * file. Over the cap the OLDEST frames are dropped — if something is wrong and
 * ready never arrives, the most recent speech is the part worth keeping.
 */
const MAX_BUFFERED_BYTES = 8 * 32 * 1024

/** How long to wait for the server to close after `stop` before forcing it. */
const CLOSE_GRACE_MS = 5_000

/** Recording failure code -> the caller's error channel. Kept narrow on purpose. */
export type RecordingErrorCode =
  | 'unsupported'
  | 'unavailable'
  | 'disconnected'
  | 'duration'
  | 'server'

/**
 * Classify a server `error` message into a code the UI can translate.
 *
 * The server's `message` is an untranslated English string, so it is matched here
 * rather than displayed. The duration cap is worth separating out because it is
 * the one server error that is neither a fault nor a misuse: the recording really
 * did run to the AWS Transcribe billing bound, and the user needs to be told that
 * specifically, not "recording failed".
 *
 * Exported for its own test — this is the only place the wire's English leaks into
 * behaviour, so it should be pinned.
 */
export function classifyServerError(message: string | undefined): RecordingErrorCode {
  return (message ?? '').includes('duration') ? 'duration' : 'server'
}

interface Options {
  /**
   * The meeting this recording belongs to. Sent in the `start` frame so the
   * server writes `audio.wav` into that meeting's directory; it is validated
   * server-side by the meetings store (`safe_meeting_id` + `contain`), never
   * trusted as a path.
   */
  meetingId: string
  /** Meeting title, stored with the session for the recovery listing. */
  title?: string
  /** Called with a user-facing failure code. */
  onError?: (code: RecordingErrorCode) => void
}

export const recordingSupported = typeof window !== 'undefined' && typeof window.WebSocket !== 'undefined'

export function useMeetingRecording({ meetingId, title, onError }: Options) {
  const [active, setActive] = useState(false)
  const [paused, setPaused] = useState(false)
  const wsRef = useRef<WebSocket | null>(null)
  const readyRef = useRef(false)
  const stoppingRef = useRef(false)
  const startingRef = useRef(false)
  /** Pre-`ready` PCM, flushed in order once the server's writer is open. */
  const bufferRef = useRef<ArrayBuffer[]>([])
  const bufferedBytesRef = useRef(0)

  const onErrorRef = useRef(onError)
  onErrorRef.current = onError
  const titleRef = useRef(title)
  titleRef.current = title

  // ── level meter ───────────────────────────────────────────────────────────
  // Deliberately NOT React state. `level` events arrive ~5 Hz, and holding them
  // in this hook's state would re-render MeetingView and every AgentPanel five
  // times a second for a bar in the header. Listeners let the meter component
  // subscribe and re-render alone.
  const levelRef = useRef(0)
  const levelListenersRef = useRef(new Set<(rms: number) => void>())

  const subscribeLevel = useCallback((fn: (rms: number) => void) => {
    levelListenersRef.current.add(fn)
    fn(levelRef.current)
    return () => {
      levelListenersRef.current.delete(fn)
    }
  }, [])

  const emitLevel = useCallback((rms: number) => {
    levelRef.current = rms
    for (const fn of levelListenersRef.current) {
      try {
        fn(rms)
      } catch {
        /* a listener must not break the socket handler */
      }
    }
  }, [])

  const resetBuffer = useCallback(() => {
    bufferRef.current = []
    bufferedBytesRef.current = 0
  }, [])

  const cleanup = useCallback(() => {
    try {
      wsRef.current?.close()
    } catch {
      /* already closing */
    }
    wsRef.current = null
    readyRef.current = false
    startingRef.current = false
    resetBuffer()
    emitLevel(0)
    setActive(false)
    setPaused(false)
  }, [emitLevel, resetBuffer])

  // Never leave a recording socket open when the view unmounts. The server
  // finalizes the WAV on close, so this loses nothing already captured.
  useEffect(() => () => { cleanup() }, [cleanup])

  const start = useCallback(async () => {
    if (!recordingSupported) {
      onErrorRef.current?.('unsupported')
      return
    }
    if (wsRef.current || startingRef.current) return
    startingRef.current = true
    stoppingRef.current = false
    readyRef.current = false
    resetBuffer()

    const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
    const ws = new WebSocket(`${proto}//${window.location.host}/api/ws/recording`)
    ws.binaryType = 'arraybuffer'
    wsRef.current = ws

    ws.onmessage = ev => {
      if (typeof ev.data !== 'string') return
      let msg: { type?: string; rms?: number; message?: string; meeting_id?: string }
      try {
        msg = JSON.parse(ev.data)
      } catch {
        return
      }
      if (msg.type === 'ready') {
        readyRef.current = true
        // Flush what was spoken while the writer was opening, oldest first.
        if (ws.readyState === WebSocket.OPEN) {
          for (const chunk of bufferRef.current) {
            try {
              ws.send(chunk)
            } catch {
              break
            }
          }
        }
        resetBuffer()
        setActive(true)
        return
      }
      if (msg.type === 'level') {
        emitLevel(typeof msg.rms === 'number' ? msg.rms : 0)
        return
      }
      if (msg.type === 'error') {
        // The server reports and keeps going for some errors ("session already
        // started") and closes for others ("max recording duration exceeded").
        // Either way the user is told; `onclose` handles the teardown. The
        // subsequent close is marked expected so it does not also report
        // 'disconnected' — one cause, one message.
        const code = classifyServerError(msg.message)
        if (code === 'duration') stoppingRef.current = true
        onErrorRef.current?.(code)
        // An error BEFORE ready means the session never started — a rejected
        // meeting_id, or storage that could not be resolved. The server keeps the
        // socket open (it stays available for a corrected start), but this client
        // has no retry path on a live socket: `start` returns early while one
        // exists, so leaving it open would make the Record button do nothing for
        // the rest of the meeting. Tearing down is what keeps a retry possible.
        if (!readyRef.current) {
          stoppingRef.current = true
          cleanup()
        }
      }
    }

    ws.onclose = () => {
      if (wsRef.current !== ws) return
      // A close we did not ask for ends the recording: the server has already
      // finalized the WAV. Deliberately NOT auto-reconnected — a new socket
      // would open a SECOND session writing a second file, and the concurrency
      // cap is one, so a retry races the session being torn down. Reporting it
      // lets the user decide to record again.
      if (!stoppingRef.current) onErrorRef.current?.('disconnected')
      cleanup()
    }

    try {
      await new Promise<void>((resolve, reject) => {
        ws.onerror = () => reject(new Error('open failed'))
        ws.onopen = () => resolve()
      })
    } catch {
      // The upgrade was refused. The WebSocket API does not expose the status, and
      // every refusal here is a guard in `api_ws_recording`: a non-loopback client,
      // a disallowed origin, or — much the most likely in practice — the
      // one-session concurrency cap. Reported as "unavailable" rather than guessed at.
      onErrorRef.current?.('unavailable')
      cleanup()
      return
    }
    ws.onerror = () => { onErrorRef.current?.('disconnected') }

    try {
      ws.send(
        JSON.stringify({
          type: 'start',
          meeting_id: meetingId,
          title: titleRef.current ?? '',
        }),
      )
    } catch {
      onErrorRef.current?.('unavailable')
      cleanup()
      return
    }
    startingRef.current = false
    // `active` is set on `ready`, not here: until the server has a writer open,
    // nothing is being persisted, and a meter that lit up early would claim
    // otherwise.
  }, [cleanup, emitLevel, meetingId, resetBuffer])

  /**
   * Feed one PCM chunk to the recording, or buffer it until the server is ready.
   *
   * Called from the capture tee at ~10 Hz, so it stays allocation-light and
   * never touches React state.
   */
  const pushPcm = useCallback((chunk: ArrayBuffer) => {
    const ws = wsRef.current
    if (!ws) return
    if (readyRef.current) {
      if (ws.readyState === WebSocket.OPEN) {
        try {
          ws.send(chunk)
        } catch {
          /* CLOSING — the close handler owns the teardown */
        }
      }
      return
    }
    bufferRef.current.push(chunk)
    bufferedBytesRef.current += chunk.byteLength
    while (bufferedBytesRef.current > MAX_BUFFERED_BYTES && bufferRef.current.length > 1) {
      bufferedBytesRef.current -= bufferRef.current.shift()!.byteLength
    }
  }, [])

  const pause = useCallback(() => {
    const ws = wsRef.current
    if (!ws || ws.readyState !== WebSocket.OPEN) return
    try {
      ws.send(PAUSE_FRAME)
    } catch {
      return
    }
    // Optimistic: the server acknowledges a pause only by discarding audio, and
    // an invalid transition is ignored there rather than reported.
    setPaused(true)
    emitLevel(0)
  }, [emitLevel])

  const resume = useCallback(() => {
    const ws = wsRef.current
    if (!ws || ws.readyState !== WebSocket.OPEN) return
    try {
      ws.send(RESUME_FRAME)
    } catch {
      return
    }
    setPaused(false)
  }, [])

  const stop = useCallback(() => {
    stoppingRef.current = true
    const ws = wsRef.current
    if (!ws || ws.readyState !== WebSocket.OPEN) {
      cleanup()
      return
    }
    // Ask the server to stop and let IT close: `stop` is what flushes the WAV
    // header and the transcript, and closing from here first would leave the
    // file's length field unwritten.
    try {
      ws.send(STOP_FRAME)
    } catch {
      /* ignore */
    }
    // Force cleanup if the close never lands, so the UI cannot get stuck.
    window.setTimeout(() => {
      if (wsRef.current === ws) cleanup()
    }, CLOSE_GRACE_MS)
  }, [cleanup])

  return {
    active,
    paused,
    start,
    stop,
    pause,
    resume,
    pushPcm,
    subscribeLevel,
    supported: recordingSupported,
  }
}
