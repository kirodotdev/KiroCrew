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

/** Header left inset clearing the traffic lights: 16px inset + ~52px button group + 16px gap. */
export const TRAFFIC_LIGHT_INSET_PX = 84

/**
 * Absolute filesystem path of a dropped/selected File, or undefined outside
 * Electron. Electron 32+ removed `File.path`; the replacement is
 * `webUtils.getPathForFile`, which must run in the preload and is exposed here
 * as `window.electronAPI.getPathForFile`. A plain browser has no such bridge and
 * cannot reveal a local path, so callers get undefined and must degrade.
 */
type DropPathApi = { getPathForFile?: (file: File) => string }
export function droppedFilePath(file: File | null): string | undefined {
  if (!file) return undefined
  const api = (window as { electronAPI?: DropPathApi }).electronAPI
  try {
    const p = api?.getPathForFile?.(file)
    return p || undefined
  } catch {
    return undefined
  }
}
