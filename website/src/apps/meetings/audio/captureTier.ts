// Which system-audio capture tier this client gets.
//
// A meeting needs the remote participants' voices, and how they can be obtained
// differs by more than "browser vs desktop app". The tier decides two things: what
// the user is told, and whether asking is even worth doing.
//
// ── The tiers ─────────────────────────────────────────────────────────────────
//
// `loopback`      Electron on Windows. `display-media.js` grants a loopback audio
//                 device alongside a source it selects itself, so system audio
//                 arrives with NO picker and nothing for the user to get wrong.
//
// `system-picker` Electron on macOS. `setDisplayMediaRequestHandler` is installed
//                 with `useSystemPicker: true`, and when the native picker is
//                 available Electron uses it and never invokes our handler — so
//                 the OS picker is what offers or withholds audio.
//
// `video-only`    Electron on a platform with neither: our handler runs, cannot
//                 grant audio (Electron's loopback device is Windows-only), and
//                 auto-selects a source — so there is not even a picker in which
//                 the user could share audio. Asking here is worse than useless:
//                 it starts a real screen capture that can only ever return video.
//                 See `tierCanCaptureSystemAudio`.
//
// `browser`       A plain browser. `getDisplayMedia` shows the engine's own picker;
//                 audio is offered for some surfaces (tab audio broadly, window and
//                 monitor audio only on some platforms).
//
// `unsupported`   No display capture at all.
//
// The Electron tiers come from the preload bridge, which computes them with the
// same module that decides the actual grant (`electron/display-media.js`'s
// `describeAudioTier`), so the guidance and the behaviour cannot drift apart.

/** Audio-capture tiers, best to worst. */
export type CaptureTier =
  | 'loopback'
  | 'system-picker'
  | 'browser'
  | 'video-only'
  | 'unsupported'

/** The shape the Electron preload exposes. Absent in a browser. */
interface KiroCrewBridge {
  isElectron?: boolean
  audioTier?: string
}

interface WindowLike {
  kirocrew?: KiroCrewBridge
  navigator?: { mediaDevices?: { getDisplayMedia?: unknown } }
}

/** Tiers the preload is allowed to assert, so an unknown string cannot leak through. */
const ELECTRON_TIERS = new Set<CaptureTier>(['loopback', 'system-picker', 'video-only'])

const defaultWindow = (): WindowLike | undefined =>
  typeof window !== 'undefined' ? (window as unknown as WindowLike) : undefined

/**
 * Resolve the capture tier for this client.
 *
 * Order matters: display-capture support is checked FIRST, because a client that
 * cannot capture at all is `unsupported` whatever shell it is running in.
 */
export function detectCaptureTier(win: WindowLike | undefined = defaultWindow()): CaptureTier {
  const nav = win?.navigator ?? (typeof navigator !== 'undefined' ? navigator : undefined)
  if (typeof (nav as WindowLike['navigator'])?.mediaDevices?.getDisplayMedia !== 'function') {
    return 'unsupported'
  }
  const bridge = win?.kirocrew
  if (bridge?.isElectron) {
    const claimed = bridge.audioTier as CaptureTier | undefined
    if (claimed && ELECTRON_TIERS.has(claimed)) return claimed
    // An Electron build whose preload predates the tier field. Fall back to the
    // browser tier rather than guessing a better one: its guidance ("share the
    // meeting's window and allow audio") is the safe superset, and treating an
    // unknown build as `video-only` would refuse to even try on a shell that may
    // well support it.
    return 'browser'
  }
  return 'browser'
}

/**
 * Whether asking for system audio can possibly succeed on this tier.
 *
 * `false` for `video-only` specifically: our Electron handler auto-selects a source
 * and cannot attach audio there, so a request would silently begin capturing the
 * user's screen and hand back a video-only stream every time. Not asking is both
 * the honest answer and the private one.
 */
export function tierCanCaptureSystemAudio(tier: CaptureTier): boolean {
  return tier !== 'video-only' && tier !== 'unsupported'
}
