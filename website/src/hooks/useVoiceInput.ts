import { useState, useRef, useCallback, useEffect } from 'react'
import { api } from '../api/client'
import { streamingSupported, useStreamingStt } from './useStreamingStt'
import { micAudioConstraints, humanizeMicError, createLevelMeter, createAudioSample, getPreferredMicId, setPreferredMicId } from './mic'
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
  onPartial?: (text: string, sessionId: string | null) => void
  /** Fired when streaming semantic endpointing judges the utterance complete. */
  onEndpoint?: () => void
  /** Id of the session/slot that currently owns the mic. Snapshotted the
   *  instant a recording starts so the resulting transcript can be attributed
   *  to the slot that initiated it — even if the user switches sessions before
   *  the (async) transcription finishes. */
  sessionId?: string | null
}

export function useVoiceInput(onText: (text: string, sessionId: string | null) => void, opts: Opts = {}) {
  const [recording, setRecording] = useState(false)
  const [transcribing, setTranscribing] = useState(false)
  // The slot that owns the current voice session — set when a recording
  // actually starts (not optimistically on click) and held through its
  // transcription, then cleared when the session ends. Recording/transcribing
  // UI is scoped to this slot so a session's busy state never shows in another
  // session. Sessions are exclusive (one shared mic, and the caller blocks a
  // new start while one is in flight), so a single owner is sufficient — no
  // per-session map or request token is needed. Null when idle.
  const [sessionOwner, setSessionOwner] = useState<string | null>(null)
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
  // Set by cancel() to tell the pending MediaRecorder.onstop to DROP its audio
  // instead of transcribing it — how Esc discards a batch dictation. A ref (not
  // state) because onstop reads it synchronously at fire time, after cancel()
  // has already returned.
  const discardRef = useRef(false)
  // Monotonic per-recording token. Bumped when a new batch recorder is created;
  // each recorder's onstop closes over its own value. If a newer recording has
  // bumped it by the time an onstop fires (user cancelled then immediately
  // restarted, and the async onstop is late), that onstop is SUPERSEDED and must
  // not touch the shared meter/chunks/ownership — they belong to the new session.
  const recordingEpochRef = useRef(0)
  const levelStopRef = useRef<(() => void) | null>(null)
  // Pre-warm: a mic stream acquired on pointer-down (before the click toggles
  // recording) so capture can begin instantly. getUserMedia + the first audio
  // frame have noticeable latency on macOS; warming during the press closes
  // that gap so the user's opening words aren't lost to a silent warmup window
  // (which Whisper otherwise hallucinates into a canned phrase like "Thank you.").
  const warmRef = useRef<MediaStream | null>(null)
  const warmPromiseRef = useRef<Promise<MediaStream> | null>(null)
  // The saved preference AS IT WAS when `warmRef` was acquired. Comparing this
  // against the current preference is what detects "this stream predates the user's
  // choice" without mistaking an unavoidable device fallback for staleness.
  const warmWantRef = useRef<string | null>(null)
  const warmTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const clearError = useCallback(() => setError(null), [])

  // Live active-session id, mirrored every render so start() can snapshot the
  // slot that owns a recording at the exact moment capture begins.
  const sessionIdRef = useRef<string | null>(opts.sessionId ?? null)
  sessionIdRef.current = opts.sessionId ?? null
  // The session captured when the CURRENT streaming run started, so its
  // partials + final are attributed to the originating slot. (The batch path
  // snapshots a plain local in start() instead — see below.)
  const streamSessionRef = useRef<string | null>(null)

  const streamEnabled = !!opts.streaming && streamingSupported
  const optsPartial = opts.onPartial
  const streamOnPartial = useCallback(
    (text: string) => { setPartial(text); optsPartial?.(text, streamSessionRef.current) },
    [optsPartial],
  )
  const streamOnFinal = useCallback(
    (text: string) => { setPartial(''); if (text) onText(text, streamSessionRef.current) },
    [onText],
  )
  const optsEndpoint = opts.onEndpoint
  const streamOnEndpoint = useCallback(
    () => { optsEndpoint?.() },
    [optsEndpoint],
  )
  // Destructure individual members so downstream useCallback deps track
  // stable references (start/stop/recording) instead of the hook's
  // always-new return object literal, preventing memoization churn.
  const { recording: streamRecording, start: streamStart, stop: streamStop, switchDevice: streamSwitchDevice, cancel: streamCancel } = useStreamingStt({
    onPartial: streamOnPartial,
    onFinal: streamOnFinal,
    onError: setError,
    onLevel: setLevel,
    onDevice: setDeviceLabel,
    onEndpoint: streamOnEndpoint,
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
    if (live && live.getAudioTracks()[0]?.readyState === 'live') {
      // A pre-warmed stream is bound to the device it was acquired with. If the
      // user changed their input since the press, reusing it records from the OLD
      // mic — the setting appears to have no effect until the WARM_IDLE_MS timer
      // happens to drop the stream.
      //
      // The test is "was this acquired under a DIFFERENT preference", not "is the
      // track on the preferred device". Those differ when the `ideal` constraint
      // silently fell back because the chosen mic was busy: the track is then
      // permanently not-the-preference, so a track-based test would discard a
      // working stream and re-acquire on EVERY press, getting the same fallback
      // device back each time. Re-acquiring cannot fix a busy device; the menu
      // already surfaces it (`saved_device_unavailable`, and the label reads the
      // live track), and re-picking the entry is the user's explicit retry.
      if (warmWantRef.current === getPreferredMicId()) return live
      live.getTracks().forEach(t => t.stop())
      warmRef.current = null
      levelStopRef.current?.()
      levelStopRef.current = null
    }
    if (warmPromiseRef.current) return warmPromiseRef.current
    const want = getPreferredMicId()
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
      warmWantRef.current = want
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
    if (streamEnabled) {
      // Re-entrancy guard for the async startup window (mirrors the batch path
      // below): a second slot's click during streamStart() must not open a
      // second stream or reassign ownership. Capture the session at click time
      // for partial/final attribution, and assign sessionOwner only AFTER the
      // stream actually starts — so a mid-startup slot switch can't misattribute
      // this stream to the slot now on screen.
      if (startingRef.current) return
      startingRef.current = true
      const streamSession = sessionIdRef.current
      streamSessionRef.current = streamSession
      try {
        await streamStart()
        // Aborted by a slot switch during startup — stop the stream rather than
        // capture invisibly for a slot that is no longer on screen.
        if (streamSession !== sessionIdRef.current) { streamStop(); return }
        setSessionOwner(streamSession)
      } finally {
        startingRef.current = false
      }
      return
    }
    if (!voiceInputSupported || startingRef.current) return
    startingRef.current = true
    // Attribute this recording's transcript to the slot that owns the mic RIGHT
    // NOW. Captured as a local (not the ref) so a second recording started in
    // another slot before this one's async transcription returns can't reassign
    // the attribution out from under it.
    const sessionAtStart = sessionIdRef.current
    if (warmTimerRef.current) { clearTimeout(warmTimerRef.current); warmTimerRef.current = null }
    let stream: MediaStream | null = null
    try {
      // Reuse the pre-warmed stream (acquired on pointer-down) so recording
      // starts instantly; otherwise acquire now. acquireWarm sets up the level
      // meter + device label exactly once.
      stream = await acquireWarm()
      warmRef.current = null // ownership transfers to the recorder; releaseWarm now no-ops
      // The user switched slots while the mic was being acquired. Abort instead
      // of starting a recording the now-active slot can neither see nor stop —
      // it would capture invisibly until the user returned to the originating
      // slot. (sessionAtStart is this recording's slot; sessionIdRef.current is
      // the live active slot.)
      if (sessionAtStart !== sessionIdRef.current) {
        stream.getTracks().forEach(t => t.stop())
        levelStopRef.current?.()
        levelStopRef.current = null
        setLevel(0)
        setDeviceLabel('')
        startingRef.current = false
        return
      }
      const mimeType = pickMimeType()
      const mr = new MediaRecorder(stream, mimeType ? { mimeType } : undefined)
      chunksRef.current = []
      // This recording's token; a stale prior discard flag must not carry over.
      discardRef.current = false
      const myEpoch = ++recordingEpochRef.current
      mr.ondataavailable = e => { if (e.data.size) chunksRef.current.push(e.data) }
      mr.onstop = async () => {
        // Superseded by a newer recording — stop only THIS recorder's own tracks
        // and bow out. Touching the shared meter/level/chunks/ownership here would
        // clobber the new session (cancel-then-immediate-restart race).
        if (recordingEpochRef.current !== myEpoch) {
          stream?.getTracks().forEach(t => t.stop())
          return
        }
        levelStopRef.current?.()
        levelStopRef.current = null
        setLevel(0)
        setDeviceLabel('')
        stream?.getTracks().forEach(t => t.stop())
        // Cancelled (Esc): capture is already torn down above; drop the audio
        // without transcribing so an abandoned dictation never lands in the
        // composer. Clicking the mic remains the commit path.
        if (discardRef.current) { discardRef.current = false; chunksRef.current = []; setSessionOwner(null); return }
        const ext = mimeType.includes('mp4') ? 'mp4' : mimeType.includes('ogg') ? 'ogg' : 'webm'
        const blob = new Blob(chunksRef.current, { type: mimeType || 'audio/webm' })
        if (blob.size < 100) { setSessionOwner(null); return }
        setTranscribing(true)
        try {
          const res = await api.sttTranscribe(blob, ext)
          if (res.error) {
            // eslint-disable-next-line no-console -- surface STT failures for debugging
            console.error('[voice] STT error:', res.error)
            setError(i18nT('hooks.useVoiceInput.transcription_failed', { error: res.error }))
          } else if (res.text) onText(res.text, sessionAtStart)
        } catch (err) {
          // eslint-disable-next-line no-console -- surface transcription failures for debugging
          console.error('[voice] transcription failed:', err)
          setError(i18nT('hooks.useVoiceInput.transcription_request_failed'))
        }
        // Session over (recording ended, transcription done) — release ownership
        // so another slot can start. Exclusivity (one session at a time) is
        // enforced by the caller, so there is never a second in-flight request
        // whose state this could clobber.
        setTranscribing(false)
        setSessionOwner(null)
      }
      mr.start()
      mediaRef.current = mr
      setRecording(true)
      // Ownership is assigned HERE — when capture actually begins — not
      // optimistically on click. If the user switched slots during the
      // getUserMedia await, sessionAtStart still points at the slot that
      // initiated this recording, so its audio/transcript can never be
      // misattributed to the slot now on screen.
      setSessionOwner(sessionAtStart)
    } catch (e) {
      if (warmTimerRef.current) { clearTimeout(warmTimerRef.current); warmTimerRef.current = null }
      levelStopRef.current?.()
      levelStopRef.current = null
      setLevel(0)
      setDeviceLabel('')
      stream?.getTracks().forEach(t => t.stop())
      warmRef.current = null
      warmPromiseRef.current = null
      setSessionOwner(null)
      setError(humanizeMicError(e))
    }
    startingRef.current = false
  }, [onText, streamEnabled, streamStart, acquireWarm])

  const stop = useCallback(() => {
    if (streamEnabled) { streamStop(); setSessionOwner(null); return }
    setPartial('')
    levelStopRef.current?.()
    levelStopRef.current = null
    if (mediaRef.current?.state === 'recording') {
      mediaRef.current.stop()
      mediaRef.current = null
    }
    setRecording(false)
  }, [streamEnabled, streamStop])

  // Cancel (discard) — end capture WITHOUT transcribing. Esc routes here so a
  // mistaken or abandoned dictation is thrown away instead of committed; the
  // mic button stays the commit (stop + transcribe) path. Batch: flag the
  // pending onstop to drop its blob before it reaches the transcriber. Streaming:
  // stop the socket and clear the live hypothesis — the caller additionally
  // disarms the draining final and restores the pre-dictation composer text.
  const cancel = useCallback(() => {
    if (warmTimerRef.current) { clearTimeout(warmTimerRef.current); warmTimerRef.current = null }
    setPartial('')
    if (streamEnabled) { streamCancel(); setSessionOwner(null); return }
    if (mediaRef.current?.state === 'recording') {
      // onstop drops the blob (see discardRef) and tears down the meter + stream.
      discardRef.current = true
      mediaRef.current.stop()
      mediaRef.current = null
    } else {
      // Pressed before capture began (prewarm only) — release the warm mic.
      releaseWarm()
    }
    setRecording(false)
    setSessionOwner(null)
  }, [streamEnabled, streamCancel, releaseWarm])

  const isRecording = streamEnabled ? streamRecording : recording
  const toggle = useCallback(() => { if (isRecording) stop(); else start() }, [isRecording, start, stop])

  /**
   * Change the capture device from the in-chat picker.
   *
   * Two behaviors, deliberately different and surfaced to the user by
   * `deviceSwitchIsLive`:
   *
   * - **Streaming session live** — hot-swap the source node; the WebSocket and
   *   everything already transcribed survive (see `useStreamingStt.switchDevice`).
   * - **Otherwise** — persist the preference and drop any pre-warmed stream so
   *   the next press acquires the new device. `MediaRecorder` cannot change its
   *   source mid-recording, and splicing two blobs (or restarting and discarding
   *   what was said) is worse than applying it to the next utterance.
   */
  const switchDevice = useCallback(async (deviceId: string) => {
    if (streamEnabled && streamRecording) { await streamSwitchDevice(deviceId); return }
    setPreferredMicId(deviceId)
    // Invalidate the pre-warm so the next press honors the new choice; without
    // this the stale-device check would do it, but only after re-entering start().
    //
    // The in-flight promise must go too, not just the settled stream: acquireWarm
    // returns `warmPromiseRef.current` BEFORE any staleness check, so a getUserMedia
    // still resolving from the old device would be handed back whole — the next
    // recording captures the previous mic and only the one after is correct. That is
    // the very defect class this picker exists to remove, just in a ~50-200ms window
    // (longer on a first permission grant). Nulling the ref is how releaseWarm() and
    // stopStream() already cancel an in-flight acquisition: the awaiting branch sees
    // `warmPromiseRef.current !== p`, stops the orphaned stream, and rejects.
    warmPromiseRef.current = null
    if (warmRef.current) {
      warmRef.current.getTracks().forEach(t => t.stop())
      warmRef.current = null
      levelStopRef.current?.()
      levelStopRef.current = null
      setLevel(0)
      setDeviceLabel('')
    }
  }, [streamEnabled, streamRecording, streamSwitchDevice])

  /** True when `switchDevice` takes effect immediately rather than next recording. */
  const deviceSwitchIsLive = streamEnabled && streamRecording

  return { recording: isRecording, transcribing, sessionOwner, streamEnabled, toggle, cancel, prewarm, error, level, deviceLabel, clearError, partial, sampleRef, switchDevice, deviceSwitchIsLive }
}
