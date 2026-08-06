/**
 * Electron shell detection + frameless-window layout constants.
 *
 * The desktop app (electron/main.js) is a frameless window on macOS
 * (titleBarStyle:"hidden"): the SPA's 42px header row doubles as the title
 * bar, and the native traffic lights are inset into the top-left of the
 * window (see trafficLightPositionForZoom in electron/main.js — x=16,
 * vertically centered in the 42px header, rescaled on zoom). The header gets
 * a left inset clearing them via the `.mac-electron` rule in index.css.
 */
const mc = (window as { kirocrew?: { isElectron?: boolean; platform?: string } }).kirocrew

export const isElectron = !!mc?.isElectron
export const isMacElectron = isElectron && mc?.platform === 'darwin'
export const isWinElectron = isElectron && mc?.platform === 'win32'

/** Header left inset clearing the traffic lights: 16px inset + ~52px button group + 16px gap. */
export const TRAFFIC_LIGHT_INSET_PX = 84

/**
 * Width reserved on the right for the Windows titleBarOverlay caption buttons
 * (minimize/maximize/close). The overlay is 138px wide at default DPI on
 * Windows 10/11. The header must not place interactive controls in this zone.
 */
export const WIN_CAPTION_OVERLAY_WIDTH = 138

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
