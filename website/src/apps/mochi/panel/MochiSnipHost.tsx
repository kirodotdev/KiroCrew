/**
 * Screen-capture host for the Mochi panel.
 *
 * In the Electron shell the capture and the crop BOTH happen in a dedicated
 * full-screen window (electron/mochi/snipWindow.js + src/apps/mochi/snip/): the
 * crop surface is `position: fixed; inset: 0`, so hosting it here put a
 * full-screen frame inside a 320x470 panel — the image scaled to ~288px wide, a
 * pixel of drag moved ~13 source pixels, and the Tailwind utilities it is styled
 * with are deliberately filtered out of Mochi's windows (see themes.ts
 * `installCoreThemeVars`), so it did not even lay out. This component's job in
 * the shell is therefore just to RECEIVE the finished crop and hand it to the
 * vendored ChatPanel.
 *
 * The in-panel capture path is kept for surfaces with no crop window — the panel
 * opened as a plain browser tab, where `api.startCapture()` still exists but the
 * shell does not. It is degraded there for the reasons above, which is better
 * than a button that does nothing.
 *
 * The capture itself is KiroCrew's own (`captureScreen` + `SnipOverlay`), so Mochi
 * adds no second capture mechanism and no second permission path, and it works on
 * Windows and Linux — unlike the original, which shelled out to macOS's
 * `screencapture -i`.
 */
import { useCallback, useEffect, useState } from 'react'

import SnipOverlay from '../../../components/SnipOverlay'
import { captureScreen, isScreenSnipSupported } from '../../../hooks/useScreenSnip'
import { deliverCapture, onCaptureRequested } from '../src/mochiApi'
import { reportStat } from './panelBridge'

/** The vendored ChatPanel expects bare base64 (it builds its own data: URL). */
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

interface ShellApi {
  onStartSnip?: (cb: () => void) => () => void
  /** Present only where the shell owns a crop window. */
  onSnipDelivered?: (cb: (base64: string) => void) => () => void
}

function shell(): ShellApi {
  return (window as unknown as { mochi?: ShellApi }).mochi ?? {}
}

export function MochiSnipHost() {
  const [frame, setFrame] = useState<HTMLCanvasElement | null>(null)
  const [busy, setBusy] = useState(false)

  /** One place a finished crop lands, whichever surface produced it. */
  const accept = useCallback((base64: string) => {
    deliverCapture(base64)
    reportStat('screenshot')
  }, [])

  // Shell path: the crop window did the work and sends the PNG.
  useEffect(() => {
    const off = shell().onSnipDelivered?.((base64: string) => {
      if (typeof base64 === 'string' && base64 !== '') accept(base64)
    })
    return () => { off?.() }
  }, [accept])

  const begin = useCallback(async () => {
    // Re-entrancy guard: the trigger can fire again while the picker or the
    // overlay is already up, and a second frame would stack overlays.
    if (busy || frame !== null) return
    if (!isScreenSnipSupported()) {
      // A SILENT return here reads as "the shortcut is broken". Say why: a Mochi
      // window has no notice surface to render a refusal on, and the caller is a
      // global accelerator with nothing to return a reason to.
      // eslint-disable-next-line no-console -- names the missing capability where nothing else can
      console.warn('[mochi] screen capture unavailable: getDisplayMedia is not exposed in this window')
      return
    }
    setBusy(true)
    try {
      const canvas = await captureScreen()
      // null means the user cancelled or the OS refused; display-media.js
      // surfaces the permission dialog in the refused case, but a cancel and a
      // silent rejection look identical from here, so log it either way.
      if (canvas !== null) setFrame(canvas)
      // eslint-disable-next-line no-console -- the only record the shortcut ran: no frame means no overlay
      else console.warn('[mochi] screen capture produced no frame (cancelled or refused)')
    } catch (err) {
      // The reason is the only thing separating an OS/permission refusal from a bug
      // in captureScreen, and nothing above this catch surfaces either one.
      // eslint-disable-next-line no-console -- carries the rejection reason nothing else reports
      console.warn('[mochi] screen capture failed', err)
    } finally {
      setBusy(false)
    }
  }, [busy, frame])

  useEffect(() => {
    // In the shell the accelerator opens the crop window instead, so this only
    // fires where there is none. `api.startCapture()` from vendored code still
    // routes here in both cases.
    const offShell = shell().onStartSnip?.(() => {
      void begin()
    })
    const offPanel = onCaptureRequested(() => {
      void begin()
    })
    return () => {
      offShell?.()
      offPanel()
    }
  }, [begin])

  if (frame === null) return null

  return (
    <SnipOverlay
      frame={frame}
      onComplete={(file) => {
        setFrame(null)
        void fileToBase64(file).then((base64) => {
          if (base64 !== null) accept(base64)
        })
      }}
      onCancel={() => setFrame(null)}
    />
  )
}
