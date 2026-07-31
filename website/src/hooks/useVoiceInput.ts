import { useState, useRef, useCallback, useEffect } from 'react'
import { api } from '../api/client'
import { streamingSupported, useStreamingStt } from './useStreamingStt'
import { micAudioConstraints, humanizeMicError, createLevelMeter, createAudioSample } from './mic'
import { i18nT } from '../i18n/t'

function pickMimeType(): string {
  if (typeof MediaRecorder === 'undefined') return ''
  for (const mt of ['audio/webm;codecs=opus', 'audio/webm', 'audio/mp4', 'audio/ogg']) {
    if (MediaRecorder.isTypeSupported(mt)) return mt
  }
  return ''
}

/** True if browser supports mic recording. */
export const voiceInputSupported =
  typeof navigator !== 'undefined' &&
  typeof navigator.mediaDevices !== 'undefined' &&
  typeof navigator.mediaDevices.getUserMedia === 'function' &&
  pickMimeType() !== ''

/** Release a pre-warmed mic if the user presses but doesn't start within this window. */
const WARM_IDLE_MS = 15000

interface Opts {
  streaming?: boolean
  onPartial?: (text: string) => void
}

export function useVoiceInput(onText: (text: string) => void, opts: Opts = {}) {
  const [recording, setRecording] = useState(false)
  const [transcribing, setTranscribing] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [level, setLevel] = useState(0)
  const [deviceLabel, setDeviceLabel] = useState('')
  // Latest partial hypothesis, mirrored so the dictation panel can render it
  // muted. Cleared on final/stop so a stale partial can't linger as grey text.
  const [partial, setPartial] = useState('')
  // Unthrottled per-frame audio features, written in place by the level meter
  // and read by the shader's render loop. A ref (not state) on purpose: this
  // updates ~60x/sec and must never trigger a React render.
  const sampleRef = useRef(createAudioSample())
  const mediaRef = useRef<MediaRecorder | null>(null)
  const chunksRef = useRef<Blob[]>([])
  const startingRef = useRef(false)
  const levelStopRef = useRef<(() => void) | null>(null)
  // Pre-warm: a mic stream acquired on pointer-down (before the click toggles
  // recording) so capture can begin instantly. getUserMedia + the first audio
  // frame have noticeable latency on macOS; warming during the press closes
  // that gap so the user's opening words aren't lost to a silent warmup window
  // (which Whisper otherwise hallucinates into a canned phrase like "Thank you.").
  const warmRef = useRef<MediaStream | null>(null)
  const warmPromiseRef = useRef<Promise<MediaStream> | null>(null)
  const warmTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const clearError = useCallback(() => setError(null), [])

  const streamEnabled = !!opts.streaming && streamingSupported
  const optsPartial = opts.onPartial
  const streamOnPartial = useCallback(
    (text: string) => { setPartial(text); optsPartial?.(text) },
    [optsPartial],
  )
  const streamOnFinal = useCallback(
    (text: string) => { setPartial(''); if (text) onText(text) },
    [onText],
  )
  // Destructure individual members so downstream useCallback deps track
  // stable references (start/stop/recording) instead of the hook's
  // always-new return object literal, preventing memoization churn.
  const { recording: streamRecording, start: streamStart, stop: streamStop } = useStreamingStt({
    onPartial: streamOnPartial,
    onFinal: streamOnFinal,
    onError: setError,
    onLevel: setLevel,
    onDevice: setDeviceLabel,
    sampleRef,
  })

  // Stop any in-flight streaming session when the user toggles streaming off
  // mid-session. Without this, the WebSocket + Transcribe session leak until
  // unmount — Transcribe bills for the whole idle window. Must live here
  // (not ChatPage) to read `streamRecording` directly: routing through the
  // returned `recording` property is racy because `useVoiceInput` flips it
  // to the batch `recording` (false) on the same render where `streamEnabled`
  // goes false, so the caller's `voice.recording` is already false.
  useEffect(() => {
    if (!streamEnabled && streamRecording) streamStop()
  }, [streamEnabled, streamRecording, streamStop])

  const stopStream = useCallback(() => {
    if (warmTimerRef.current) { clearTimeout(warmTimerRef.current); warmTimerRef.current = null }
    levelStopRef.current?.()
    levelStopRef.current = null
    setPartial('')
    if (warmRef.current) { warmRef.current.getTracks().forEach(t => t.stop()); warmRef.current = null }
    warmPromiseRef.current = null
    if (mediaRef.current) {
      mediaRef.current.stream.getTracks().forEach(t => t.stop())
      if (mediaRef.current.state === 'recording') mediaRef.current.stop()
      mediaRef.current = null
    }
  }, [])

  // Cleanup on unmount — stop mic stream
  useEffect(() => () => { stopStream() }, [stopStream])

  // Acquire (or reuse) a live mic stream and attach the level meter + device
  // label exactly once. A single in-flight getUserMedia is shared so a
  // pointer-down prewarm and the click-driven start() never double-acquire —
  // start() reuses the same promise the press already kicked off.
  const acquireWarm = useCallback(async (): Promise<MediaStream> => {
    const live = warmRef.current
    if (live && live.getAudioTracks()[0]?.readyState === 'live') return live
    if (warmPromiseRef.current) return warmPromiseRef.current
    const p = navigator.mediaDevices.getUserMedia(micAudioConstraints())
    warmPromiseRef.current = p
    try {
      const stream = await p
      // If releaseWarm()/stopStream() nulled (or replaced) the promise ref
      // while we were awaiting -- idle timeout, stop, or unmount -- this
      // acquisition was cancelled. Stop the now-orphaned stream instead of
      // leaking a live mic + level meter that nothing will clean up.
      if (warmPromiseRef.current !== p) {
        stream.getTracks().forEach(t => t.stop())
        throw new Error('mic acquisition cancelled')
      }
      setDeviceLabel(stream.getAudioTracks()[0]?.label || '')
      levelStopRef.current = createLevelMeter(stream, setLevel, sampleRef)
      warmRef.current = stream
      return stream
    } finally {
      // Only clear if it's still our promise; a concurrent acquire may have
      // installed a newer one we must not wipe.
      if (warmPromiseRef.current === p) warmPromiseRef.current = null
    }
  }, [])

  // Tear down a pre-warmed (not-yet-recording) stream. No-op once the stream
  // has been handed off to an active recorder (warmRef cleared in start()), so
  // the idle timer can never kill a live recording.
  const releaseWarm = useCallback(() => {
    if (warmTimerRef.current) { clearTimeout(warmTimerRef.current); warmTimerRef.current = null }
    warmPromiseRef.current = null
    if (!warmRef.current) return
    levelStopRef.current?.()
    levelStopRef.current = null
    warmRef.current.getTracks().forEach(t => t.stop())
    warmRef.current = null
    setLevel(0)
    setDeviceLabel('')
  }, [])

  // Pre-warm the mic on pointer-down so the click that follows starts capture
  // instantly. Auto-releases if recording doesn't start within WARM_IDLE_MS so
  // a press-without-record doesn't hold the mic open.
  const prewarm = useCallback(() => {
    if (streamEnabled || !voiceInputSupported || startingRef.current || mediaRef.current) return
    acquireWarm().catch(() => { /* error is surfaced on the actual start() */ })
    if (warmTimerRef.current) clearTimeout(warmTimerRef.current)
    warmTimerRef.current = setTimeout(releaseWarm, WARM_IDLE_MS)
  }, [streamEnabled, acquireWarm, releaseWarm])

  const start = useCallback(async () => {
    setError(null)
    if (streamEnabled) { await streamStart(); return }
    if (!voiceInputSupported || startingRef.current) return
    startingRef.current = true
    if (warmTimerRef.current) { clearTimeout(warmTimerRef.current); warmTimerRef.current = null }
    let stream: MediaStream | null = null
    try {
      // Reuse the pre-warmed stream (acquired on pointer-down) so recording
      // starts instantly; otherwise acquire now. acquireWarm sets up the level
      // meter + device label exactly once.
      stream = await acquireWarm()
      warmRef.current = null // ownership transfers to the recorder; releaseWarm now no-ops
      const mimeType = pickMimeType()
      const mr = new MediaRecorder(stream, mimeType ? { mimeType } : undefined)
      chunksRef.current = []
      mr.ondataavailable = e => { if (e.data.size) chunksRef.current.push(e.data) }
      mr.onstop = async () => {
        levelStopRef.current?.()
        levelStopRef.current = null
        setLevel(0)
        setDeviceLabel('')
        stream?.getTracks().forEach(t => t.stop())
        const ext = mimeType.includes('mp4') ? 'mp4' : mimeType.includes('ogg') ? 'ogg' : 'webm'
        const blob = new Blob(chunksRef.current, { type: mimeType || 'audio/webm' })
        if (blob.size < 100) return
        setTranscribing(true)
        try {
          const res = await api.sttTranscribe(blob, ext)
          if (res.error) {
            // eslint-disable-next-line no-console -- surface STT failures for debugging
            console.error('[voice] STT error:', res.error)
            setError(`Transcription failed: ${res.error}`)
          } else if (res.text) onText(res.text)
        } catch (err) {
          // eslint-disable-next-line no-console -- surface transcription failures for debugging
          console.error('[voice] transcription failed:', err)
          setError(i18nT('hooks.useVoiceInput.transcription_request_failed'))
        }
        setTranscribing(false)
      }
      mr.start()
      mediaRef.current = mr
      setRecording(true)
    } catch (e) {
      if (warmTimerRef.current) { clearTimeout(warmTimerRef.current); warmTimerRef.current = null }
      levelStopRef.current?.()
      levelStopRef.current = null
      setLevel(0)
      setDeviceLabel('')
      stream?.getTracks().forEach(t => t.stop())
      warmRef.current = null
      warmPromiseRef.current = null
      setError(humanizeMicError(e))
    }
    startingRef.current = false
  }, [onText, streamEnabled, streamStart, acquireWarm])

  const stop = useCallback(() => {
    if (streamEnabled) { streamStop(); return }
    setPartial('')
    levelStopRef.current?.()
    levelStopRef.current = null
    if (mediaRef.current?.state === 'recording') {
      mediaRef.current.stop()
      mediaRef.current = null
    }
    setRecording(false)
  }, [streamEnabled, streamStop])

  const isRecording = streamEnabled ? streamRecording : recording
  const toggle = useCallback(() => { if (isRecording) stop(); else start() }, [isRecording, start, stop])

  return { recording: isRecording, transcribing, toggle, prewarm, error, level, deviceLabel, clearError, partial, sampleRef }
}
