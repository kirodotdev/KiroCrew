/**
 * Scan a QR code with the device camera, in the page.
 *
 * The session-expired banner (`api/client.ts`) offers this on a phone. The desktop
 * "Connect your phone" QR carries the same `https://<host>/?token=<token>` URL the
 * banner's paste field accepts, but the phone's camera app opens that URL in the
 * default browser. When the dashboard runs in a different browser, the new session
 * lands in the wrong one and the expired tab stays expired. Scanning here keeps the
 * exchange in the browser that is already open.
 *
 * Decoding uses the browser's own `BarcodeDetector`, so no dependency ships. Where the
 * API is missing (iOS WebKit today) `qrScanSupported()` answers false and the caller
 * renders nothing. Frames never leave the device: the only output is the decoded
 * string, which the caller hands to the existing token exchange.
 *
 * Every exit path stops the camera: a decode, a cancel, an error, the page being
 * hidden or unloaded, and the host being removed from the document while the
 * viewfinder is open.
 */
import { i18nT } from '../i18n/t'
import {
  QR_SCAN_CANCEL_CSS,
  QR_SCAN_OVERLAY_CSS,
  QR_SCAN_VIDEO_CSS,
} from './authBannerStyles'

/**
 * The slice of the Shape Detection API this module uses. `lib.dom` does not declare
 * `BarcodeDetector` yet, so the shape is pinned here rather than cast at each use.
 */
interface DetectedBarcode {
  rawValue: string
}

interface BarcodeDetectorLike {
  detect(source: HTMLVideoElement): Promise<DetectedBarcode[]>
}

interface BarcodeDetectorCtor {
  new (options: { formats: string[] }): BarcodeDetectorLike
  getSupportedFormats(): Promise<string[]>
}

function barcodeDetectorCtor(): BarcodeDetectorCtor | null {
  if (typeof window === 'undefined' || !('BarcodeDetector' in window)) return null
  const ctor = (window as unknown as { BarcodeDetector?: unknown }).BarcodeDetector
  return typeof ctor === 'function' ? (ctor as BarcodeDetectorCtor) : null
}

function hasCameraApi(): boolean {
  return typeof navigator !== 'undefined'
    && typeof navigator.mediaDevices?.getUserMedia === 'function'
}

/**
 * Whether this browser can scan a QR code from the camera: a `BarcodeDetector` that
 * lists `qr_code`, and `getUserMedia`. Async because the format list is. Any failure
 * answers false, so a caller that renders on true never shows a button that cannot
 * work.
 */
export async function qrScanSupported(): Promise<boolean> {
  const ctor = barcodeDetectorCtor()
  if (!ctor || !hasCameraApi()) return false
  try {
    const formats = await ctor.getSupportedFormats()
    return Array.isArray(formats) && formats.includes('qr_code')
  } catch {
    return false
  }
}

/** The camera could not be opened because the user, or a policy, refused it. */
export function isCameraPermissionDenied(err: unknown): boolean {
  const name = (err as { name?: unknown } | null)?.name
  return name === 'NotAllowedError' || name === 'SecurityError'
}

export interface QrScanOptions {
  /**
   * Answers whether the scan still has a reason to run. Polled once per frame; a
   * false answer cancels the scan and stops the camera. The banner passes
   * `() => el.isConnected`, so a banner torn down by a 2xx elsewhere, or by its own
   * dismiss, takes the camera down with it instead of leaving it on.
   */
  alive?: () => boolean
}

/**
 * Open the rear camera in a full-screen viewfinder and resolve with the first QR
 * code's raw text, or with null when the user cancels, the page is hidden or
 * unloaded, or `alive` turns false. While it is open the rest of the page is inert,
 * and focus returns to where it was when it closes.
 *
 * Rejects when the camera cannot be opened (permission denied, no camera, a device in
 * use); the overlay is already gone and no track is left running by then. The caller
 * words that failure, since it owns the element it shows it in.
 *
 * The decoded string is never logged here, and the caller must not log it either: it
 * is, in the intended use, a sign-in token.
 */
export function scanQrCode(options: QrScanOptions = {}): Promise<string | null> {
  const alive = options.alive ?? (() => true)
  return new Promise<string | null>((resolve, reject) => {
    const ctor = barcodeDetectorCtor()
    if (!ctor || !hasCameraApi()) {
      reject(new Error('qr scan unsupported'))
      return
    }

    const overlay = document.createElement('div')
    overlay.setAttribute('role', 'dialog')
    overlay.setAttribute('aria-modal', 'true')
    overlay.style.cssText = QR_SCAN_OVERLAY_CSS
    const video = document.createElement('video')
    video.setAttribute('playsinline', '')
    // The viewfinder is for the eye only; the dialog's label says what to do.
    video.setAttribute('aria-hidden', 'true')
    video.muted = true
    video.autoplay = true
    video.style.cssText = QR_SCAN_VIDEO_CSS
    const prompt = i18nT('api.client.scan_qr_point_camera')
    const instruction = document.createElement('div')
    instruction.textContent = prompt
    const cancel = document.createElement('button')
    cancel.type = 'button'
    cancel.textContent = i18nT('api.client.scan_qr_cancel')
    cancel.style.cssText = QR_SCAN_CANCEL_CSS
    overlay.append(video, instruction, cancel)
    overlay.setAttribute('aria-label', prompt)

    let stream: MediaStream | null = null
    let frame = 0
    let done = false

    const stopCamera = (): void => {
      // `stop()` on every track, not `srcObject = null`: detaching the element leaves
      // the capture running and the camera light on.
      for (const track of stream?.getTracks() ?? []) {
        try { track.stop() } catch { /* a track already ended */ }
      }
      stream = null
    }

    // `aria-modal` only tells assistive tech the rest of the page is out of play; it
    // does not take the page out of play. `inert` on every other body child does:
    // nothing under the overlay can be focused, clicked or typed into, including the
    // banner's paste field and the button that opened this. The previous state is
    // recorded per element, so a child that was already inert stays inert on close.
    const inerted: Array<{ el: Element; had: boolean }> = []
    const makePageInert = (): void => {
      for (const child of Array.from(document.body.children)) {
        if (child === overlay) continue
        inerted.push({ el: child, had: child.hasAttribute('inert') })
        child.setAttribute('inert', '')
      }
    }
    const restorePage = (): void => {
      for (const { el, had } of inerted) {
        if (!had) el.removeAttribute('inert')
      }
      inerted.length = 0
    }

    // Focus goes to the cancel button while the viewfinder is up and back to where
    // it was on close, so a keyboard or screen-reader user lands on the control they
    // left (the banner's scan button) rather than at the top of the document.
    const previouslyFocused = document.activeElement

    const finish = (outcome: { value: string | null } | { error: unknown }): void => {
      if (done) return
      done = true
      if (frame) cancelAnimationFrame(frame)
      document.removeEventListener('keydown', onKey, { capture: true })
      document.removeEventListener('visibilitychange', onHidden)
      window.removeEventListener('pagehide', onPageHide)
      stopCamera()
      try { video.srcObject = null } catch { /* detached element */ }
      overlay.remove()
      restorePage()
      if (previouslyFocused instanceof HTMLElement && previouslyFocused.isConnected) {
        previouslyFocused.focus()
      }
      if ('error' in outcome) reject(outcome.error)
      else resolve(outcome.value)
    }

    // Captured, and stopped there: Escape closes the viewfinder and nothing else.
    // Without this the same keypress also reaches the dashboard's own Escape
    // handlers (a panel or a dialog behind the overlay) and closes those too.
    const onKey = (e: KeyboardEvent): void => {
      if (e.key !== 'Escape') return
      e.stopPropagation()
      finish({ value: null })
    }
    // A hidden or unloading page cannot be scanning anything, and a camera left on
    // behind a switched tab, a locked phone or a closed sheet is the failure a user
    // notices as a camera light that never goes off.
    const onHidden = (): void => {
      if (document.visibilityState === 'hidden') finish({ value: null })
    }
    const onPageHide = (): void => finish({ value: null })
    cancel.addEventListener('click', () => finish({ value: null }))
    document.addEventListener('keydown', onKey, { capture: true })
    document.addEventListener('visibilitychange', onHidden)
    window.addEventListener('pagehide', onPageHide)

    const detector = new ctor({ formats: ['qr_code'] })
    let detecting = false
    const tick = (): void => {
      if (done) return
      if (!alive()) {
        finish({ value: null })
        return
      }
      frame = requestAnimationFrame(tick)
      // One detect in flight at a time: a frame that arrives while the previous
      // decode is still running is skipped rather than queued.
      if (detecting) return
      detecting = true
      detector.detect(video).then(
        (codes) => {
          detecting = false
          const value = codes.find((c) => typeof c.rawValue === 'string' && c.rawValue)
          if (value && !done) finish({ value: value.rawValue })
        },
        () => {
          // Before the first frame `detect` rejects with InvalidStateError; a frame
          // that fails to decode is not a result either. Both are retried next frame.
          detecting = false
        },
      )
    }

    document.body.append(overlay)
    makePageInert()
    cancel.focus()
    navigator.mediaDevices.getUserMedia({ video: { facingMode: 'environment' } }).then(
      (s) => {
        if (done || !alive()) {
          // Cancelled, or the host left the document, while the permission prompt
          // was up: the stream arrived after the scan was over, so stop it here
          // rather than leaving it live. `finish` is a no-op when already done.
          for (const track of s.getTracks()) {
            try { track.stop() } catch { /* already ended */ }
          }
          finish({ value: null })
          return
        }
        stream = s
        try {
          video.srcObject = s
        } catch (err) {
          finish({ error: err })
          return
        }
        try {
          // A rejected `play()` is tolerated, not worked around: the element is
          // muted, and muted autoplay is exempt from the user-gesture policy, so
          // the only rejections seen are a detached element or a test DOM with no
          // `play()` at all. A video that genuinely stayed paused would not decode,
          // and the scan would then end by cancel, not by a wrong result.
          (video.play() as Promise<void> | undefined)?.catch(() => {})
        } catch { /* no play() on this element */ }
        frame = requestAnimationFrame(tick)
      },
      (err: unknown) => finish({ error: err }),
    )
  })
}
