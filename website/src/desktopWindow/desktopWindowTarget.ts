/**
 * The document a desktop-hosted app window loads, and the bridge it is handed.
 *
 * The Electron shell's window host (`website/electron/app-window-host.js`) opens
 * a window an app declares in its manifest `desktop` block and points it at
 * `/popout/desktop/<app>/<window>`. That document mounts only the app's declared
 * `entry` module (see `DesktopWindowRoot.tsx`), never the dashboard shell. The
 * shell's preload exposes `window.kirocrewDesktopWindow` in that window and in no
 * other: a plain browser tab on the same path finds no bridge and renders nothing.
 *
 * Pure, so `main.tsx` can read it at document load before any provider mounts.
 */

/** Document-path prefix of a hosted window. Keep in sync with `app-window-host.js`. */
export const DESKTOP_WINDOW_PREFIX = '/popout/desktop'

const APP_NAME_RE = /^[a-z0-9][a-z0-9-]{0,63}$/
const WINDOW_ID_RE = /^[a-z][a-z0-9-]{0,31}$/

export interface DesktopWindowTarget {
  app: string
  window: string
}

/** The app and window a document path names, or `null` when it is not a hosted window. */
export function desktopWindowTarget(pathname: string): DesktopWindowTarget | null {
  if (!pathname.startsWith(`${DESKTOP_WINDOW_PREFIX}/`)) return null
  const segs = pathname.slice(DESKTOP_WINDOW_PREFIX.length + 1).split('/')
  if (segs.length !== 2) return null
  const [app, window] = segs
  if (!APP_NAME_RE.test(app) || !WINDOW_ID_RE.test(window)) return null
  return { app, window }
}

/** A box in the page's own pixels. */
export interface DesktopWindowBox { x: number; y: number; width: number; height: number }

/**
 * What the shell's preload exposes; `level` rises when a call is added. The
 * level 2 calls are optional here because a shell of an older version may load
 * this page: an app checks `level` before it uses one.
 */
export interface DesktopWindowBridge {
  level: number
  app: string
  window: string
  kind: string
  ready(): void
  close(windowId?: string): void
  open(windowId: string, options?: { anchor?: DesktopWindowBox }): void
  setHitRegions(regions: DesktopWindowBox[]): void
  setFocusable(focusable: boolean): void
  onShortcut(cb: (id: string) => void): () => void
  // Level 2: messages between the app's windows, and the spanning overlay.
  closeAll?(): void
  send?(windowId: string, message: unknown): void
  broadcast?(message: unknown): void
  onMessage?(cb: (fromWindowId: string, message: unknown) => void): () => void
  onWindowChange?(cb: (windowId: string, open: boolean) => void): () => void
  onPlaced?(cb: (placement: { side: 'left' | 'right' }) => void): () => void
  onActive?(cb: (state: { active: boolean; x?: number; y?: number; dragging?: boolean }) => void): () => void
  beginDrag?(drag: { offsetX: number; offsetY: number; width: number; height: number }): void
  endDrag?(): void
  onDrag?(cb: (phase: 'start' | 'move' | 'end', x?: number, y?: number) => void): () => void
  showSession?(slotKey: string): Promise<boolean>
  openExternal?(url: string): void
  /** Level 2: the capabilities this window was opened with (declared and approved). */
  capabilities?: readonly string[]
  /** Level 2, `localFiles`: show an existing local file in the system file manager. */
  revealFile?(path: string): void
  /** Level 2, `localFiles`: open a local image in the system viewer; resolves whether it opened. */
  openImage?(path: string): Promise<boolean>
  /** Level 2, `gatewayChoice`: the operator's gateways, or null when refused. */
  listGateways?(): Promise<DesktopGatewayList | null>
  /** Level 2, `gatewayChoice`: load this app's windows from gateway *id* (`"self"`: this computer's). */
  chooseGateway?(id: string): Promise<{ ok: boolean; chosen?: string; showing?: string | null }>
}

/** What `listGateways()` answers: no host names, ports or credentials. */
export interface DesktopGatewayList {
  state: 'ready' | 'inactive' | 'disabled' | 'error'
  gateways: { id: string; name: string; state: string; connected: boolean; appEnabled: boolean | null }[]
  /** The stored choice, and the gateway the windows load from now. */
  chosen: string
  showing: string
}

/** The level 1 calls every bridge has; the host route needs no newer one. */
const CALLS = ['ready', 'close', 'open', 'setHitRegions', 'setFocusable', 'onShortcut'] as const

/** The shell's bridge when it describes exactly *target*, else `null`. */
export function desktopWindowBridge(
  target: DesktopWindowTarget,
  win: { kirocrewDesktopWindow?: unknown } = window as unknown as { kirocrewDesktopWindow?: unknown },
): DesktopWindowBridge | null {
  const raw = win.kirocrewDesktopWindow
  if (!raw || typeof raw !== 'object') return null
  const bridge = raw as Partial<DesktopWindowBridge>
  if (typeof bridge.level !== 'number' || bridge.level < 1) return null
  if (bridge.app !== target.app || bridge.window !== target.window) return null
  if (!CALLS.every(name => typeof bridge[name] === 'function')) return null
  return bridge as DesktopWindowBridge
}

/** The declared window *id* of an installed app row's manifest, or `null`. */
export function declaredDesktopWindow(
  manifest: unknown,
  id: string,
): { id: string; entry: string; kind: string } | null {
  if (!manifest || typeof manifest !== 'object') return null
  const desktop = (manifest as { desktop?: unknown }).desktop
  const windows = desktop && typeof desktop === 'object' ? (desktop as { windows?: unknown }).windows : null
  if (!Array.isArray(windows)) return null
  for (const w of windows) {
    if (!w || typeof w !== 'object') continue
    const { id: wid, entry, kind } = w as { id?: unknown; entry?: unknown; kind?: unknown }
    if (wid === id && typeof entry === 'string' && entry && typeof kind === 'string') {
      return { id, entry, kind }
    }
  }
  return null
}
