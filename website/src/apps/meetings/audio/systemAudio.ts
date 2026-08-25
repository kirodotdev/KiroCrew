// System-audio capture for meetings: what the OTHER participants say.
//
// `getUserMedia` gives us the local microphone, which is half a meeting. The
// remote side arrives as audio played back by the conferencing app, so it has to
// be captured from the display-capture surface instead. That stream is mixed
// with the mic in `/pcm-worklet.js` (input 1) so speech-to-text sees one mono
// stream — see that file's TWO INPUTS, ONE STREAM note.
//
// Pure, browser-API-free units live here so the track-selection logic is
// unit-testable without a real `getDisplayMedia`, mirroring `useScreenSnip.ts`.
//
// ── Why a video track is requested and then thrown away ───────────────────────
// `getDisplayMedia` REQUIRES a video track: per the spec, omitting `video` or
// passing `video: false` rejects with a TypeError. So audio-only display capture
// is not expressible, and `getDisplayMedia({ audio: true })` fails in every
// browser. The video track is requested, then stopped and removed immediately —
// tracks are independent, so the audio keeps flowing after the video ends, and
// dropping it means no frames are decoded for a stream nobody looks at.
//
// ── What the user actually has to pick ────────────────────────────────────────
// Display capture only *offers* audio for some surfaces, and which ones is
// platform-dependent: Chromium offers tab audio everywhere, and window/monitor
// audio only on some platforms (notably not macOS). Picking a surface that
// offers none yields a video-only stream, which is why `'no-audio'` is reported
// separately from a cancel — the user needs to be told to pick the meeting's TAB
// and tick "share audio", not that capture failed. The durable fix for the
// native-app case is an Electron loopback source, which is a separate change.

/** The slice of `MediaStreamTrack` this module needs. Structural so tests can fake it. */
export interface TrackLike {
  kind: string
  stop: () => void
}

/**
 * The slice of `MediaStream` this module needs. `MediaStream` satisfies it structurally.
 *
 * Method syntax, not property-function syntax: `strictFunctionTypes` checks a
 * property-typed `removeTrack` contravariantly, which would make `MediaStream`
 * (whose `removeTrack` takes the narrower `MediaStreamTrack`) unassignable to
 * `StreamLike<TrackLike>`. Methods are checked bivariantly, which is what lets
 * the real DOM types and the tests' structural fakes both satisfy this.
 */
export interface StreamLike<T extends TrackLike = TrackLike> {
  getTracks(): T[]
  getAudioTracks(): T[]
  getVideoTracks(): T[]
  removeTrack(track: T): void
}

type NavLike = { mediaDevices?: { getDisplayMedia?: unknown } }

const defaultNav = (): NavLike | undefined =>
  typeof navigator !== 'undefined' ? (navigator as NavLike) : undefined

/** True when the browser can capture a display surface at all (desktop + Electron). */
export function isSystemAudioSupported(nav: NavLike | undefined = defaultNav()): boolean {
  return typeof nav?.mediaDevices?.getDisplayMedia === 'function'
}

/**
 * `getDisplayMedia` options that maximise the chance of getting audio.
 *
 * `systemAudio` and `surfaceSwitching` are Chromium hints absent from the
 * standard `DisplayMediaStreamOptions` lib type (the same gap `useScreenSnip`
 * works around for `preferCurrentTab`); other engines ignore them.
 *
 * `video` is deliberately constrained to a 1 fps thumbnail rather than `true`.
 * It cannot be omitted — that rejects — but it is stopped microseconds later, so
 * asking for the cheapest possible track avoids briefly negotiating a full-rate
 * screen capture for frames that are never read.
 *
 * NOT `preferCurrentTab`: the meeting is in another tab or app, so pre-selecting
 * this one would be the wrong surface every time.
 */
export function systemAudioConstraints(): DisplayMediaStreamOptions {
  const opts: DisplayMediaStreamOptions & {
    systemAudio?: 'include' | 'exclude'
    surfaceSwitching?: 'include' | 'exclude'
  } = {
    audio: true,
    video: { frameRate: { max: 1 } },
    systemAudio: 'include',
    surfaceSwitching: 'include',
  }
  return opts
}

/**
 * Drop the video track from a display-capture stream, keeping audio.
 *
 * Returns the same stream when it carries audio, or `null` when it does not —
 * in which case every track is stopped first, so a surface the user shared but
 * we cannot use does not leave a live capture (and its "sharing" indicator)
 * behind.
 *
 * Stopped AND removed: `stop()` ends the track, `removeTrack` takes it out of
 * the stream so a `MediaStreamAudioSourceNode` built from it cannot see a dead
 * track at all.
 */
export function extractSystemAudio<T extends TrackLike, S extends StreamLike<T>>(
  stream: S,
): S | null {
  for (const track of stream.getVideoTracks()) {
    try {
      track.stop()
    } catch {
      /* already ended */
    }
    try {
      stream.removeTrack(track)
    } catch {
      /* not in the stream */
    }
  }
  if (stream.getAudioTracks().length > 0) return stream
  // Video-only: the chosen surface offers no audio. Release it entirely.
  stopStream(stream)
  return null
}

/** Stop every track on a stream, ignoring tracks that already ended. */
export function stopStream(stream: StreamLike | null | undefined): void {
  if (!stream) return
  for (const track of stream.getTracks()) {
    try {
      track.stop()
    } catch {
      /* already ended */
    }
  }
}

/** Why system audio is unavailable, when it is. */
export type SystemAudioFailure = 'unsupported' | 'cancelled' | 'no-audio'

export type SystemAudioOutcome<S> = { ok: true; stream: S } | { ok: false; reason: SystemAudioFailure }

export interface SystemAudioDeps<S> {
  getDisplayMedia: (opts: DisplayMediaStreamOptions) => Promise<S>
  supported?: () => boolean
}

/** Real browser I/O boundary — the only untested glue here. */
export function defaultSystemAudioDeps(): SystemAudioDeps<MediaStream> {
  return {
    getDisplayMedia: opts => navigator.mediaDevices.getDisplayMedia(opts),
    supported: () => isSystemAudioSupported(),
  }
}

/**
 * Prompt for a display surface and return an audio-only stream from it.
 *
 * Never throws: every failure is a reported `reason`, because system audio is an
 * ENHANCEMENT. A meeting still transcribes the local microphone without it, so a
 * cancelled picker must degrade to mic-only rather than failing the recording.
 */
export async function requestSystemAudio<T extends TrackLike, S extends StreamLike<T>>(
  deps: SystemAudioDeps<S>,
): Promise<SystemAudioOutcome<S>> {
  if (deps.supported && !deps.supported()) return { ok: false, reason: 'unsupported' }
  let stream: S
  try {
    stream = await deps.getDisplayMedia(systemAudioConstraints())
  } catch {
    // Cancelled picker, denied permission, or a TypeError from an engine that
    // rejects these constraints. All three mean the same thing to the caller:
    // carry on with the microphone only.
    return { ok: false, reason: 'cancelled' }
  }
  const audioOnly = extractSystemAudio<T, S>(stream)
  if (!audioOnly) return { ok: false, reason: 'no-audio' }
  return { ok: true, stream: audioOnly }
}
