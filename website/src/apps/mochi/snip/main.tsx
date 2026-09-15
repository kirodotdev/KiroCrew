/**
 * Mochi's crop window entry.
 *
 * Sequence, and why it is in this order:
 *
 *   1. CAPTURE FIRST, while this window is still hidden. macOS presents its own
 *      source picker for `getDisplayMedia`, and a visible full-screen always-on-top
 *      window would cover that dialog.
 *   2. Ask the shell to SHOW the window only once a frame is in hand.
 *   3. Drag a region on the frame. `SnipOverlay` is the SAME component the
 *      dashboard composer uses — it maps the drag from display pixels back to
 *      source pixels and captures on release, no confirm button. This window
 *      exists to give it a host the size of the screen; hosted in the 320px chat
 *      panel the frame scaled to ~288px wide and a pixel of drag moved ~13 source
 *      pixels.
 *   4. Hand the cropped PNG to the shell, which relays it to the panel's composer.
 *      The crop is what crosses process boundaries — never the full frame, which
 *      is multiple megabytes at 4K.
 *
 * Cancel, a refused capture and an encode failure all take the same exit: close
 * the window. A full-screen surface with nothing on it is the worst thing to
 * leave behind, so every path out of here closes.
 *
 * ── Why this entry imports the REAL stylesheet ───────────────────────────────
 *
 * `SnipOverlay` is styled with Tailwind utilities (`fixed inset-0`, `bg-black/50`,
 * `max-w-[90vw]`). The other Mochi windows deliberately do NOT get those: their
 * theme installer keeps only custom-property rules so component styles cannot
 * leak a background into the TRANSPARENT PET overlay (see themes.ts
 * `installCoreThemeVars`). Hosted under that filter the overlay collapses into
 * plain document flow — a 3840px-wide image with no positioning and no backdrop,
 * which is unusable no matter how large the window is.
 *
 * This window is not the pet. A dim backdrop over the whole screen is exactly
 * what it is for, so it takes the stylesheet directly and the pet's filter stays
 * as strict as it was.
 *
 * NO StrictMode: it double-invokes effects in development, and this entry's
 * effect opens the OS capture picker — twice would mean two pickers for one
 * shortcut press.
 */
import { createRoot } from 'react-dom/client'
import { useCallback, useEffect, useState } from 'react'

import SnipOverlay from '../../../components/SnipOverlay'
import { captureScreen, isScreenSnipSupported, type CaptureDeps } from '../../../hooks/useScreenSnip'
import { applyThemeVarsOnly } from '../src/shared/themes'
import { MochiLocalized, initMochiI18n } from '../mochiLanguage'
import '../../../index.css'

/** The slice of the preload API this window uses. */
interface SnipShell {
  snipReady?: () => void
  snipResult?: (base64: string) => void
  snipClose?: () => void
}

function shell(): SnipShell {
  return (window as unknown as { mochi?: SnipShell }).mochi ?? {}
}

/** The panel's composer wants bare base64; it builds its own data: URL. */
function fileToBase64(file: File): Promise<string | null> {
  return new Promise((resolve) => {
    const reader = new FileReader()
    reader.onload = () => {
      const result = typeof reader.result === 'string' ? reader.result : ''
      const comma = result.indexOf(',')
      resolve(comma === -1 ? null : result.slice(comma + 1))
    }
    reader.onerror = () => resolve(null)
    reader.readAsDataURL(file)
  })
}

/**
 * Capture deps that ask for the display's NATIVE pixel resolution.
 *
 * `getDisplayMedia({ video: true })` — the core default — lets the browser pick,
 * and Chromium then hands back a rescaled stream: the probe on this path reported
 * `resizeMode: "crop-and-scale"`, so the frame arrived softer than the screen and
 * every crop inherited that blur. Asking for `width`/`height` at the device pixel
 * ratio makes the stream match the panel a Retina display actually draws, so a
 * crop is as sharp as the screen it came from.
 *
 * `ideal` rather than `exact` on purpose: an unsatisfiable exact constraint fails
 * the whole capture, and a slightly smaller stream is worth far more than no
 * screenshot. The low frameRate is a hint that this is a still — one frame is
 * grabbed and the track is stopped immediately.
 *
 * Passed through the deps seam `captureScreen` already exposes, so the shared
 * hook keeps its own defaults for the dashboard.
 */
function nativeResolutionCaptureDeps(): CaptureDeps {
  const dpr = window.devicePixelRatio || 1
  const w = Math.round(window.screen.width * dpr)
  const h = Math.round(window.screen.height * dpr)
  return {
    getDisplayMedia: () =>
      navigator.mediaDevices.getDisplayMedia({
        video: {
          width: { ideal: w },
          height: { ideal: h },
          frameRate: { ideal: 5 },
        },
      }),
    createVideo: () => document.createElement('video'),
    createCanvas: () => document.createElement('canvas'),
  }
}

function SnipApp() {
  const [frame, setFrame] = useState<HTMLCanvasElement | null>(null)

  const close = useCallback(() => {
    shell().snipClose?.()
  }, [])

  useEffect(() => {
    let cancelled = false
    if (!isScreenSnipSupported()) {
      // Nothing to crop and no way to say so on a hidden window — the shell log
      // is the only place a user can find out why the shortcut did nothing.
      // eslint-disable-next-line no-console -- diagnosing a silent shortcut cost two sessions
      console.warn('[mochi] crop window: getDisplayMedia is not exposed here')
      close()
      return
    }
    void captureScreen(nativeResolutionCaptureDeps())
      .then((canvas) => {
        if (cancelled) return
        if (canvas === null) {
          // The user cancelled the picker, or the OS refused (display-media.js
          // surfaces the permission dialog in the refused case).
          close()
          return
        }
        setFrame(canvas)
        // Frame in hand — safe to cover the screen now.
        shell().snipReady?.()
      })
      .catch(() => {
        if (!cancelled) close()
      })
    return () => {
      cancelled = true
    }
  }, [close])

  // Nothing to draw until the capture resolves; the window is hidden anyway.
  if (frame === null) return null

  return (
    <SnipOverlay
      frame={frame}
      onComplete={(file) => {
        void fileToBase64(file).then((base64) => {
          if (base64 !== null) shell().snipResult?.(base64)
          close()
        })
      }}
      onCancel={close}
      // A failed encode must not strand the overlay: report and close.
      onError={() => close()}
    />
  )
}

// Theme variables only — NOT applyTheme(), which paints an opaque body
// background. This window must stay transparent until the overlay draws its own
// backdrop, so the frame is never framed by a grey rectangle.
applyThemeVarsOnly()

// Seeded before first paint so the crop hint does not flash the fallback language.
initMochiI18n()

const rootEl = document.getElementById('root')
if (rootEl !== null) {
  createRoot(rootEl).render(
    <MochiLocalized remount={false}>
      <SnipApp />
    </MochiLocalized>,
  )
}
