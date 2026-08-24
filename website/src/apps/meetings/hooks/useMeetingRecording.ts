// Post-meeting BATCH capture for a meeting (approach A).
//
// While a meeting is active this records the microphone with `MediaRecorder`,
// and when the user stops the meeting it uploads the ONE recorded blob to the
// backend's audio endpoint. There is no live transcript during the meeting; the
// server transcribes the whole file after stop (see
// `docs/design/local-meeting-transcription-batch.md`).
//
// This is deliberately SEPARATE from `useStreamingStt` (the shared chat
// dictation / push-to-talk path) and does NOT touch it: it acquires its own
// `getUserMedia` stream so a meeting recording can never interfere with a
// dictation session, and vice versa. The streaming STT hook downsamples PCM for
// a socket; this one just captures a compressed blob for a single upload.

import { useCallback, useEffect, useRef, useState } from 'react'

import { meetingsApi } from '../api'
import { reportIfMicDenied } from '../../../hooks/mic'

/** Codecs to try, best first. Opus-in-webm is the smallest and whisper/ffmpeg
 *  decode it; the bare fallbacks cover browsers (notably Safari) that only
 *  expose mp4/aac. `isTypeSupported` gates each, and an empty string lets the
 *  browser pick its own default as the last resort. */
const PREFERRED_MIME_TYPES = [
  'audio/webm;codecs=opus',
  'audio/webm',
  'audio/mp4',
  'audio/ogg;codecs=opus',
]

/** How often MediaRecorder hands back a buffered chunk, in ms. A timeslice
 *  bounds how much audio a crash could lose and keeps the chunk array from
 *  being one enormous blob, without adding meaningful overhead. */
const TIMESLICE_MS = 5_000

export function recordingSupported(): boolean {
  return (
    typeof window !== 'undefined' &&
    typeof window.MediaRecorder !== 'undefined' &&
    typeof navigator !== 'undefined' &&
    typeof navigator.mediaDevices !== 'undefined' &&
    typeof navigator.mediaDevices.getUserMedia === 'function'
  )
}

/** The first `PREFERRED_MIME_TYPES` entry the browser can actually record, or
 *  `''` to let MediaRecorder choose. */
export function pickMimeType(): string {
  const isSupported = (window as unknown as {
    MediaRecorder?: { isTypeSupported?: (t: string) => boolean }
  }).MediaRecorder?.isTypeSupported
  if (typeof isSupported !== 'function') return ''
  for (const type of PREFERRED_MIME_TYPES) {
    try {
      if (isSupported(type)) return type
    } catch {
      /* some engines throw instead of returning false */
    }
  }
  return ''
}

interface Options {
  meetingId: string
  /** Called with a user-facing code when recording cannot start. Capture is
   *  best-effort: a denied mic must not block the meeting, only skip the audio. */
  onError?: (code: string) => void
}

export function useMeetingRecording({ meetingId, onError }: Options) {
  const [recording, setRecording] = useState(false)
  const recorderRef = useRef<MediaRecorder | null>(null)
  const streamRef = useRef<MediaStream | null>(null)
  const chunksRef = useRef<BlobPart[]>([])
  const mimeRef = useRef('')
  /** Guards the async gap inside `start()` before the recorder exists. */
  const startingRef = useRef(false)

  const onErrorRef = useRef(onError)
  onErrorRef.current = onError

  const releaseStream = useCallback(() => {
    try { streamRef.current?.getTracks().forEach(t => t.stop()) } catch { /* ignore */ }
    streamRef.current = null
  }, [])

  const start = useCallback(async () => {
    if (!recordingSupported()) {
      onErrorRef.current?.('unsupported')
      return
    }
    // `recorderRef` is set only after the awaited getUserMedia resolves, so a
    // second call landing in that window would open a second mic stream and
    // recorder. The synchronous guard closes that race.
    if (recorderRef.current || startingRef.current) return
    startingRef.current = true

    let stream: MediaStream
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: true })
    } catch (e) {
      // Hand a denial to the shell so the desktop app can route to System
      // Settings (macOS never re-prompts once denied).
      reportIfMicDenied(e)
      onErrorRef.current?.('microphone')
      startingRef.current = false
      return
    }

    // Torn down (stop/unmount) while acquiring: release and bail rather than
    // start a recorder nobody is waiting for.
    if (!startingRef.current) {
      stream.getTracks().forEach(t => t.stop())
      return
    }
    streamRef.current = stream

    const mimeType = pickMimeType()
    mimeRef.current = mimeType
    chunksRef.current = []
    let recorder: MediaRecorder
    try {
      recorder = new MediaRecorder(stream, mimeType ? { mimeType } : undefined)
    } catch {
      releaseStream()
      onErrorRef.current?.('recorder')
      startingRef.current = false
      return
    }
    recorder.ondataavailable = ev => {
      if (ev.data && ev.data.size > 0) chunksRef.current.push(ev.data)
    }
    recorderRef.current = recorder
    try {
      recorder.start(TIMESLICE_MS)
    } catch {
      recorderRef.current = null
      releaseStream()
      onErrorRef.current?.('recorder')
      startingRef.current = false
      return
    }
    startingRef.current = false
    setRecording(true)
  }, [releaseStream])

  /**
   * Stop recording, assemble the blob, and upload it. Resolves only after the
   * upload has COMPLETED (or been skipped), so the caller can then call the stop
   * endpoint knowing the audio is already on the server — the order the backend
   * stop hook needs.
   *
   * Returns `true` when a blob was uploaded, `false` when there was nothing to
   * upload (a typed-only meeting, or no recorder). Never rejects: an upload
   * failure is reported through `onError` and the meeting still stops.
   */
  const stopAndUpload = useCallback(async (): Promise<boolean> => {
    startingRef.current = false
    const recorder = recorderRef.current
    recorderRef.current = null
    setRecording(false)

    if (!recorder) {
      // Typed-only meeting, or capture never started: nothing to upload.
      releaseStream()
      return false
    }

    // Wait for the recorder to flush its final chunk before assembling.
    await new Promise<void>(resolve => {
      const done = () => resolve()
      recorder.onstop = done
      try {
        if (recorder.state !== 'inactive') recorder.stop()
        else done()
      } catch {
        done()
      }
    })
    releaseStream()

    const chunks = chunksRef.current
    chunksRef.current = []
    if (chunks.length === 0) return false
    const blob = new Blob(chunks, mimeRef.current ? { type: mimeRef.current } : undefined)
    if (blob.size === 0) return false

    try {
      await meetingsApi.uploadAudio(meetingId, blob)
      return true
    } catch {
      onErrorRef.current?.('upload')
      return false
    }
  }, [meetingId, releaseStream])

  // Never leave the microphone open on unmount.
  useEffect(() => () => {
    startingRef.current = false
    try {
      if (recorderRef.current && recorderRef.current.state !== 'inactive') {
        recorderRef.current.stop()
      }
    } catch { /* ignore */ }
    recorderRef.current = null
    releaseStream()
  }, [releaseStream])

  return { recording, start, stopAndUpload, supported: recordingSupported() }
}
