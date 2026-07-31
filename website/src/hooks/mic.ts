import { safeSetItem } from '../utils/safeStorage'
import { i18nT } from '../i18n/t'

// Shared microphone helpers for voice input.
//
// Centralizes the bits both capture paths (batch `useVoiceInput` and streaming
// `useStreamingStt`) need: a persisted preferred input device, getUserMedia
// constraints honoring it, human-readable error mapping, a live input-level
// meter, and device enumeration for the settings picker.

const MIC_DEVICE_KEY = 'mc-mic-device-id'

/** Preferred input deviceId. Empty string means "system default". */
export function getPreferredMicId(): string {
  try {
    return localStorage.getItem(MIC_DEVICE_KEY) || ''
  } catch {
    return ''
  }
}

export function setPreferredMicId(id: string): void {
  try {
    if (id) safeSetItem(MIC_DEVICE_KEY, id)
    else localStorage.removeItem(MIC_DEVICE_KEY)
  } catch {
    /* localStorage unavailable — fall back to default device */
  }
}

/**
 * getUserMedia audio constraints honoring the saved device. Uses `ideal`
 * (not `exact`) so an unplugged/removed device falls back to the default
 * instead of throwing OverconstrainedError.
 */
export function micAudioConstraints(): MediaStreamConstraints {
  const id = getPreferredMicId()
  return { audio: id ? { deviceId: { ideal: id } } : true }
}

/** Map a getUserMedia rejection to a short, human-readable message. */
export function humanizeMicError(e: unknown): string {
  const name = (e as { name?: string } | null)?.name || ''
  switch (name) {
    case 'NotAllowedError':
    case 'SecurityError':
      return i18nT('hooks.mic.microphone_permission_denied_allow_mic_access_in')
    case 'NotFoundError':
    case 'OverconstrainedError':
      return i18nT('hooks.mic.no_microphone_found_connect_one_or_pick_a_differ')
    case 'NotReadableError':
    case 'AbortError':
      return i18nT('hooks.mic.microphone_is_unavailable_another_app_may_be_usi')
    default:
      return i18nT('hooks.mic.could_not_start_the_microphone')
  }
}

/**
 * Per-frame audio features for canvas/shader consumers.
 *
 * Distinct from the `onLevel` callback below on purpose. `onLevel` feeds React
 * state and a CSS-width bar, so it is throttled to ~15fps and quantized to 25
 * steps to bound re-renders. A shader driven off that signal visibly
 * stair-steps. This struct is instead written IN PLACE every animation frame
 * into a caller-owned ref, so a render loop can read it without causing a
 * single React re-render.
 */
export interface AudioSample {
  /** Envelope-smoothed RMS in [0, 1]. */
  level: number
  /** Spectral centroid in [0, 1] — roughly "how bright/sibilant". */
  centroid: number
  /** Decaying transient spike in [0, 1], for onset-driven accents. */
  onset: number
}

export function createAudioSample(): AudioSample {
  return { level: 0, centroid: 0.5, onset: 0 }
}

/** Envelope time constants. Fast attack, slow release: without the asymmetry
 * every consonant reads as a strobe rather than as speech. */
const ATTACK_SECS = 0.05
const RELEASE_SECS = 0.25
const CENTROID_SECS = 0.12
const ONSET_DECAY_SECS = 0.11

/**
 * Attach a live RMS level meter to a stream. Calls `onLevel` with a value in
 * [0, 1], throttled to ~15fps and only when the (quantized) level changes, so
 * it bounds re-renders of the consuming component during recording. Returns a
 * stop function that tears down the RAF loop + AudioContext and emits 0.
 *
 * When `sampleRef` is supplied, the same analyser additionally writes richer
 * per-frame features into it (see {@link AudioSample}) with no throttling and
 * no React involvement. One AudioContext, one analyser, two consumers.
 */
export function createLevelMeter(
  stream: MediaStream,
  onLevel: (v: number) => void,
  sampleRef?: { current: AudioSample },
): () => void {
  let stopped = false
  let raf = 0
  let lastEmit = 0
  let lastQuantized = -1
  let lastFrame = 0
  let prevRms = 0
  let ctx: AudioContext | null = null
  try {
    ctx = new AudioContext()
    const source = ctx.createMediaStreamSource(stream)
    const analyser = ctx.createAnalyser()
    analyser.fftSize = 512
    source.connect(analyser)
    // Worklet/analyser output is never routed to destination — no echo.
    const buf = new Uint8Array(analyser.frequencyBinCount)
    const freq = sampleRef ? new Uint8Array(analyser.frequencyBinCount) : null
    const tick = (t: number) => {
      if (stopped) return
      analyser.getByteTimeDomainData(buf)
      let sum = 0
      for (let i = 0; i < buf.length; i++) {
        const v = (buf[i] - 128) / 128
        sum += v * v
      }
      // Light gain so ordinary speech visibly moves the meter.
      const rms = Math.min(1, Math.sqrt(sum / buf.length) * 2.2)
      const q = Math.round(rms * 25) / 25
      if (t - lastEmit > 66 && q !== lastQuantized) {
        lastQuantized = q
        lastEmit = t
        onLevel(q)
      }
      if (sampleRef && freq) {
        // Clamp dt so a backgrounded tab (or the very first frame, where
        // lastFrame is 0) can't jump the envelope by a huge step on resume.
        const dt = lastFrame ? Math.min(0.05, Math.max(0, (t - lastFrame) / 1000)) : 0
        lastFrame = t
        const s = sampleRef.current
        analyser.getByteFrequencyData(freq)
        let num = 0
        let den = 0
        for (let i = 0; i < freq.length; i++) {
          num += i * freq[i]
          den += freq[i]
        }
        // 0.42 maps a speech-typical centroid onto roughly the middle of the
        // range rather than compressing everything into the bottom third.
        const centroid = den > 0 ? Math.min(1, num / den / (freq.length * 0.42)) : 0.5
        const tau = rms > s.level ? ATTACK_SECS : RELEASE_SECS
        s.level += (rms - s.level) * (1 - Math.exp(-dt / tau))
        s.centroid += (centroid - s.centroid) * (1 - Math.exp(-dt / CENTROID_SECS))
        s.onset = Math.max(
          s.onset * Math.exp(-dt / ONSET_DECAY_SECS),
          Math.min(1, Math.max(0, rms - prevRms) * 5.5),
        )
        prevRms = rms
      }
      raf = requestAnimationFrame(tick)
    }
    raf = requestAnimationFrame(tick)
  } catch {
    /* AudioContext unavailable — recording still works, just no meter */
  }
  return () => {
    stopped = true
    if (raf) cancelAnimationFrame(raf)
    try {
      ctx?.close()
    } catch {
      /* already closed */
    }
    if (sampleRef) {
      sampleRef.current.level = 0
      sampleRef.current.onset = 0
    }
    onLevel(0)
  }
}

/**
 * List audio input devices. Device labels are only populated after the page
 * has been granted microphone access at least once; before that, labels are
 * empty strings (callers should show a fallback name or a grant affordance).
 */
export async function listMicrophones(): Promise<MediaDeviceInfo[]> {
  try {
    if (!navigator.mediaDevices?.enumerateDevices) return []
    const devices = await navigator.mediaDevices.enumerateDevices()
    return devices.filter(d => d.kind === 'audioinput')
  } catch {
    return []
  }
}
