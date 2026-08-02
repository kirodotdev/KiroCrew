import { useCallback, useEffect, useRef, useState } from 'react'
import { micAudioConstraints, humanizeMicError, createLevelMeter } from './mic'
import type { AudioSample } from './mic'
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
  /** Active capture device label (e.g. "MacBook Pro Microphone"). */
  onDevice?: (label: string) => void
  /** Fired when the backend semantic endpointer judges the utterance complete. */
  onEndpoint?: () => void
  /** Unthrottled per-frame audio features for canvas consumers (see mic.ts). */
  sampleRef?: { current: AudioSample }
}

export function useStreamingStt ({ onPartial, onFinal, onError, onLevel, onDevice, onEndpoint, sampleRef }: Opts) {
  const [recording, setRecording] = useState(false)
  const wsRef = useRef<WebSocket | null>(null)
  const ctxRef = useRef<AudioContext | null>(null)
  const streamRef = useRef<MediaStream | null>(null)
  const levelStopRef = useRef<(() => void) | null>(null)
  const finalsRef = useRef<string[]>([])
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

  const cleanup = useCallback(() => {
    try { levelStopRef.current?.() } catch { /* ignore */ }
    levelStopRef.current = null
    try { wsRef.current?.close() } catch { /* ignore */ }
    wsRef.current = null
    try { streamRef.current?.getTracks().forEach(t => t.stop()) } catch { /* ignore */ }
    streamRef.current = null
    try { ctxRef.current?.close() } catch { /* ignore */ }
    ctxRef.current = null
    onLevelRef.current?.(0)
    onDeviceRef.current?.('')
    setRecording(false)
  }, [])

  useEffect(() => () => { cleanup() }, [cleanup])

  const start = useCallback(async () => {
    if (!streamingSupported || wsRef.current) return
    finalsRef.current = []
    let stream: MediaStream
    try {
      stream = await navigator.mediaDevices.getUserMedia(micAudioConstraints())
    } catch (e) {
      onErrorRef.current?.(humanizeMicError(e))
      return
    }
    streamRef.current = stream
    onDeviceRef.current?.(stream.getAudioTracks()[0]?.label || '')
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
        if (msg.type === 'ready') resolveReady()
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
          onErrorRef.current?.(msg.message || i18nT('hooks.useStreamingStt.stt_error'))
          rejectReady(new Error(msg.message || 'stt error'))
        } else if (msg.type === 'endpoint') {
          // Backend semantic endpointer judged the utterance complete.
          // The composer already holds the streamed transcript (via onPartial),
          // so the caller can submit directly.
          if (msg.complete) onEndpointRef.current?.()
        }
      } catch { /* ignore */ }
    }
    ws.onclose = () => {
      // Prefer Transcribe's finals. If none arrived (user stopped before
      // Transcribe finalized), fall back to the last partial so the
      // user's words aren't lost.
      const combined = finalsRef.current.length
        ? finalsRef.current.join(' ').trim()
        : lastPartial.trim()
      if (combined) onFinalRef.current(combined)
      else onPartialRef.current('')  // clear any dangling partial when nothing transcribed
      rejectReady(new Error('ws closed before ready'))
      cleanup()
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
    // PCM routing: buffer until server is ready (Transcribe start-up is
    // ~2-3s), then flush and switch to live send. Cap buffer at ~8s of
    // audio (16 kHz mono Int16 = 32 KB/s) so a never-arriving `ready`
    // can't grow memory unbounded. If we hit the cap before ready,
    // drop the oldest frames FIFO — user's most recent speech wins.
    const MAX_BUFFERED_BYTES = 8 * 32 * 1024
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
  }, [cleanup, sampleRef])

  const stop = useCallback(() => {
    const ws = wsRef.current
    if (ws && ws.readyState === WebSocket.OPEN) {
      try { ws.send('{"type":"stop"}') } catch { /* ignore */ }
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
    } else {
      // WS never opened or already closing — cleanup directly.
      cleanup()
    }
  }, [cleanup])

  return { recording, start, stop }
}
