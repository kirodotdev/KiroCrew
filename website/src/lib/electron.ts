/**
 * Electron shell detection + frameless-window layout constants.
 *
 * The desktop app (electron/main.js) is a frameless window on macOS
 * (titleBarStyle:"hidden"): the SPA's 42px header row doubles as the title
 * bar, and the native traffic lights are inset into the top-left of the
 * window (see trafficLightPositionForZoom in electron/main.js — x=16,
 * vertically centered in the 42px header, rescaled on zoom). The header gets
 * a left inset clearing them via the `.mac-electron` rule in index.css.
 *
 * On Linux the shell goes frameless (frame:false) on desktops that prefer
 * client-side decorations (see electron/linux-frame.js); the header still
 * doubles as the title bar via an injected drag region, but there are no
 * traffic lights and no caption overlay, so the header needs NO inset —
 * neither `.mac-electron` nor `.win-electron` applies, which is the correct
 * zero-inset layout, locked in by App.linuxElectron.test.tsx.
 */
const mc = window.kirocrew

export const isElectron = !!mc?.isElectron
export const isMacElectron = isElectron && mc?.platform === 'darwin'
export const isWinElectron = isElectron && mc?.platform === 'win32'
/**
 * True when this window is a FRAMELESS Linux window. Unlike the platform
 * consts above this is a runtime decision (desktop environment + operator
 * override) made in electron/linux-frame.js and carried through the preload —
 * a framed Linux window keeps its native controls and needs no header inset.
 */
export const isLinuxFramelessElectron = isElectron && mc?.platform === 'linux' && !!mc?.linuxFrameless

/**
 * Width reserved on the right of the header for the injected Linux caption
 * controls (three 36px buttons — see #electron-linux-controls in
 * electron/main.js). Mirrors WIN_CAPTION_OVERLAY_WIDTH below; keep in sync
 * with the `.linux-electron` rule in index.css and the drag-bar inset in
 * electron/main.js.
 */
export const LINUX_CAPTION_CONTROLS_WIDTH = 108

/**
 * The shell's raw `process.platform` (`'darwin'` / `'win32'` / `'linux'`), or
 * `undefined` in a plain browser tab.
 *
 * For copy about an action the SHELL performs rather than the gateway — Mochi's
 * reveal is an IPC send its main process handles, so the gateway's platform would
 * be the wrong host to name. Read lazily (not from the module-load `mc` capture
 * above) so a test can stub `window.kirocrew` per-case, exactly as `pathForFile`
 * does.
 */
export function electronPlatform(): string | undefined {
  return window.kirocrew?.platform
}

/**
 * Absolute filesystem path for a File the OS handed us (drag-drop), via the
 * desktop shell's preload bridge (webUtils.getPathForFile). Returns '' in a
 * plain browser — pages cannot see real paths there — so callers must treat a
 * falsy result as "no path available" and keep their browser behaviour.
 *
 * Read lazily (not via the module-load `mc` capture above) so tests can stub
 * `window.kirocrew` per-case without import-order coupling.
 */
export function pathForFile(file: File): string {
  const k = window.kirocrew
  try {
    return k?.getPathForFile?.(file) || ''
  } catch {
    return ''
  }
}

/** Header left inset clearing the traffic lights: 16px inset + ~52px button group + 16px gap. */
export const TRAFFIC_LIGHT_INSET_PX = 84

/**
 * Top band kept clear while the window is in macOS native fullscreen. AppKit
 * owns the top strip there: pointing at it slides the menu bar down over the
 * window, so a control laid out at y=0 (the header and its crew switcher) can
 * no longer be clicked. Shifting the dashboard down by the menu bar's height
 * puts the header below that strip.
 */
export const MAC_FULLSCREEN_TOP_RESERVE_PX = 24

/**
 * Whether the "Open in editor" affordance can work in this window.
 *
 * True only when the desktop shell's `fileOpenAPI` preload bridge is present.
 * A plain browser tab and the PWA expose no such bridge, so the caller hides
 * the control there and the built-in viewer stays the only handoff — matching
 * how `browserAPI`/`zoomAPI`/`wslAPI` consumers feature-detect their bridges.
 * Read lazily (not a module-load capture) so a test can stub `window.fileOpenAPI`
 * per-case, exactly as `pathForFile` stubs `window.kirocrew`.
 */
export function canOpenFileInEditor(): boolean {
  return typeof (window as { fileOpenAPI?: { open?: unknown } }).fileOpenAPI?.open === 'function'
}

/**
 * Hand a filesystem PATH to the desktop shell to open in the OS default handler
 * on the user's own machine (via shell.openPath in the main process) — never a
 * URL scheme. Resolves the main process's { ok, error? } verdict, or
 * { ok: false, error: 'unavailable' } when no bridge is present, so a caller in
 * a plain browser gets a definite negative rather than a thrown error.
 */
export async function openFileInEditor(
  filePath: string,
): Promise<{ ok: boolean; error?: string }> {
  const api = (window as {
    fileOpenAPI?: { open?: (p: string) => Promise<{ ok: boolean; error?: string }> }
  }).fileOpenAPI
  if (typeof api?.open !== 'function') return { ok: false, error: 'unavailable' }
  try {
    return await api.open(filePath)
  } catch (err) {
    return { ok: false, error: err instanceof Error ? err.message : String(err) }
  }
}
/**
 * Width reserved on the right for the Windows titleBarOverlay caption buttons
 * (minimize/maximize/close). The overlay is 138px wide at default DPI on
 * Windows 10/11. The header must not place interactive controls in this zone.
 */
export const WIN_CAPTION_OVERLAY_WIDTH = 138

/**
 * Height of the caption band at the top of a frameless window: the 42px header
 * row. The Windows titleBarOverlay is sized to it (its height is derived from
 * the header px, scaled by renderer zoom — `titleBarOverlayOptions` in
 * electron/windows-titlebar.js:30-37, so 42 CSS px at any zoom) and the injected
 * Linux caption controls hard-code the same 42px tall band
 * (electron/runtime/window/linux-captions.js:49).
 *
 * This constant RESTATES those two values rather than deriving them, so if the
 * header height ever changes update all three in lockstep: the Windows overlay
 * is driven by the runtime header px, the Linux CSS hard-codes 42px, and this
 * is the renderer-side mirror the portaled viewers read.
 */
export const CAPTION_BAND_HEIGHT_PX = 42

/**
 * Top clearance for the controls of a full-window overlay that is portaled to
 * <body> (the image and diagram viewers). Those overlays sit outside the shell
 * div that carries `.win-electron` / `.linux-electron`, so the header's CSS
 * insets never reach them, and their top-right controls land under the OS
 * caption buttons and inside the window drag strip. Pushing the control row
 * below the band keeps it in the corner users expect, without parking the
 * viewer's close button next to the window's own.
 *
 * Non-zero on frameless Windows, frameless Linux, and inside an embedded
 * remote pane whose Windows host relays `.embedded-win-inset` on <html>
 * (EmbeddedHostBridge). Read at render time, not module load, because that
 * class toggles at runtime.
 */
export function overlayCaptionClearancePx(): number {
  if (isWinElectron || isLinuxFramelessElectron) return CAPTION_BAND_HEIGHT_PX
  if (document.documentElement.classList.contains('embedded-win-inset')) return CAPTION_BAND_HEIGHT_PX
  return 0
}

/**
 * True when an app declares `platform.requiresDesktopApp` but we are in a
 * browser tab — i.e. its UI needs capabilities only the Electron shell can
 * provide (native always-on-top windows, global shortcuts, tray, capture).
 *
 * Callers should withhold the enable/install action and say the desktop app is
 * required instead of handing over a UI that cannot work.
 *
 * UX gate only. `isElectron` comes from the shell's preload, so it is
 * client-side and spoofable — nothing security-relevant may rest on it. See
 * `PlatformConfig.requiresDesktopApp` in `apps/manifest.py`.
 */
export function needsDesktopApp(app: {
  platform?: { requiresDesktopApp?: boolean }
  manifest?: { platform?: { requiresDesktopApp?: boolean } }
}): boolean {
  // An INSTALLED app carries its manifest fields under `manifest.*`, while a
  // catalog/registry entry exposes `platform` at the top level — so read both,
  // or a desktop-only app's requirement stays hidden on the surfaces that pass
  // the installed shape (the App Store list/detail), and its window silently
  // fails to open with no explanation.
  const requires =
    app.platform?.requiresDesktopApp === true ||
    app.manifest?.platform?.requiresDesktopApp === true
  return requires && !isElectron
}

/** Shared copy so every surface says the same thing. */
/**
 * NOT a catalog key: this module is imported by non-React code and must stay
 * free of the i18n runtime. Call sites that RENDER it use
 * `components.appstore.*.desktop_app_hint` instead; this remains the
 * machine-readable reason string for logs and non-UI callers.
 */
export const DESKTOP_APP_REQUIRED_LABEL = 'Requires the KiroCrew desktop app'
