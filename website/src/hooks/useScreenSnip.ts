// Screen snip capture helpers. Pure, browser-API-free units live here so the
// crop geometry and File synthesis are unit-testable without getDisplayMedia.

type NavLike = { mediaDevices?: { getDisplayMedia?: unknown } }

const defaultNav = (): NavLike | undefined =>
  typeof navigator !== 'undefined' ? (navigator as NavLike) : undefined

/** True when the browser can capture the screen (desktop browsers + Electron). */
export function isScreenSnipSupported(nav: NavLike | undefined = defaultNav()): boolean {
  return typeof nav?.mediaDevices?.getDisplayMedia === 'function'
}

/** Feature-detect const for ergonomic use in render gates. */
export const screenSnipSupported = isScreenSnipSupported()

export interface SnipRect {
  x: number
  y: number
  width: number
  height: number
}

const clamp = (v: number, max: number): number => Math.min(Math.max(v, 0), max)

/**
 * Normalize a drag (two points) into an integer rect clamped to the source
 * bounds. Handles reversed drags (any direction) by min/max, floors the
 * top-left and ceils the bottom-right so a partial-pixel drag still yields a
 * rect that covers what the user enclosed.
 */
export function normalizeRect(
  start: { x: number; y: number },
  end: { x: number; y: number },
  bounds: { width: number; height: number },
): SnipRect {
  const left = clamp(Math.floor(Math.min(start.x, end.x)), bounds.width)
  const top = clamp(Math.floor(Math.min(start.y, end.y)), bounds.height)
  const right = clamp(Math.ceil(Math.max(start.x, end.x)), bounds.width)
  const bottom = clamp(Math.ceil(Math.max(start.y, end.y)), bounds.height)
  return { x: left, y: top, width: right - left, height: bottom - top }
}

export interface CaptureDeps {
  getDisplayMedia: () => Promise<MediaStream>
  createVideo: () => HTMLVideoElement
  createCanvas: () => HTMLCanvasElement
}

/** Real browser I/O boundary — the only untested glue (mirrors useVoiceInput's getUserMedia). */
export function defaultCaptureDeps(): CaptureDeps {
  return {
    getDisplayMedia: () => navigator.mediaDevices.getDisplayMedia({ video: true }),
    createVideo: () => document.createElement('video'),
    createCanvas: () => document.createElement('canvas'),
  }
}

/**
 * Capture deps that pre-target the CURRENT tab via Chromium's `preferCurrentTab`,
 * collapsing the multi-source screen picker into a single "Share this tab?"
 * confirm. Used by the Web Preview snip, whose target (the preview iframe) always
 * lives in this tab. Non-Chromium browsers ignore the hint (normal picker), and
 * the Electron desktop app's `setDisplayMediaRequestHandler` supplies the source
 * regardless (no prompt at all) — so this only streamlines the browser path.
 */
export function currentTabCaptureDeps(): CaptureDeps {
  return {
    getDisplayMedia: () => {
      // `preferCurrentTab` isn't in the standard DisplayMediaStreamOptions lib type yet.
      const opts: DisplayMediaStreamOptions & { preferCurrentTab?: boolean } = {
        video: true,
        preferCurrentTab: true,
      }
      return navigator.mediaDevices.getDisplayMedia(opts)
    },
    createVideo: () => document.createElement('video'),
    createCanvas: () => document.createElement('canvas'),
  }
}

/**
 * Prompt the screen-share picker, grab a single frame into a canvas, and stop
 * the stream so no "sharing" indicator lingers. Returns null if the user
 * cancels/denies the picker. Deps are injectable for testing.
 */
export async function captureScreen(
  deps: CaptureDeps = defaultCaptureDeps(),
): Promise<HTMLCanvasElement | null> {
  let stream: MediaStream
  try {
    stream = await deps.getDisplayMedia()
  } catch {
    return null // user cancelled or denied the share prompt
  }
  try {
    const video = deps.createVideo()
    video.srcObject = stream
    await video.play()
    const canvas = deps.createCanvas()
    canvas.width = video.videoWidth
    canvas.height = video.videoHeight
    canvas.getContext('2d')?.drawImage(video, 0, 0)
    return canvas
  } catch {
    return null // frame grab failed (play/draw error) — degrade gracefully like the cancel path
  } finally {
    stream.getTracks().forEach(t => t.stop())
  }
}

/** Draw a normalized sub-region of a source image into a new rect-sized canvas. */
export function cropCanvas(
  source: CanvasImageSource,
  rect: SnipRect,
  createCanvas: () => HTMLCanvasElement = () => document.createElement('canvas'),
): HTMLCanvasElement {
  const canvas = createCanvas()
  canvas.width = rect.width
  canvas.height = rect.height
  canvas
    .getContext('2d')
    ?.drawImage(source, rect.x, rect.y, rect.width, rect.height, 0, 0, rect.width, rect.height)
  return canvas
}

/** Encode a canvas to a PNG File, reusing the existing image-upload pipeline. */
export function canvasToFile(
  canvas: HTMLCanvasElement,
  name = `snip-${Date.now()}.png`,
): Promise<File> {
  return new Promise((resolve, reject) => {
    canvas.toBlob(blob => {
      if (!blob) {
        reject(new Error('canvas.toBlob returned null'))
        return
      }
      resolve(new File([blob], name, { type: 'image/png' }))
    }, 'image/png')
  })
}
