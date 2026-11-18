/**
 * The browser of an app's own chat slot, for an app page (`@kirocrew/app-sdk`):
 * `useBrowserView` reads the live view the dashboard already holds and takes over
 * or hands back; `useBrowserCoords` converts between the coordinates of a
 * mounted `BrowserView` and the page's frame pixels.
 *
 * Host code over host state. Every member reads `browserLiveStore`, which the
 * dashboard's own websocket feeds with `browser_live` events, and writes through
 * the owner routes the chat card uses (`/api/browser/input/claim`, `release`).
 * Nothing here opens a channel or a route: an app page runs inside the owner's
 * dashboard, which already receives that slot's events (RFC app-sdk-browser,
 * section 5).
 *
 * Access. Behind the `app_browser_sdk` feature preview, once the owner approved
 * the app's `permissions.browser` `view` verb (published as
 * `info.permissions.browser`), and only for a slot the
 * gateway stamped with this app's name (`ChatSlot.app`). The key's prefix is not
 * read: one app name can extend another (`foo`, `foo-bar`), and the gateway mints
 * the stamp only through `ctx.chat_slots`, under the app's own prefix. The check
 * is a convenience that gives an honest app a clear `denied`; it is not a
 * security boundary, because app page code runs in the owner's page (section 3
 * of the RFC). The gateway's own checks are the boundary.
 */
import { useCallback, useContext, useMemo, useSyncExternalStore } from 'react'

import { browserLiveStore, useBrowserLiveCurrent, type BrowserLiveState } from '../hooks/useBrowserLive'
import { drawnRect, type FrameSize } from '../hooks/useBrowserLive.input'
import type { BrowserHandbackSaid } from '../api/client/browserLive'
import { usePreviewFlag } from '../hooks/usePreviewFlag'
import { useAppSelector } from '../store'
import type { RootState } from '../store'
import { PREVIEW_APP_BROWSER_SDK } from '../utils/previewFlags'
import { useAppIdentity } from './identity'
import { AppSdkContext } from './scopedApi'

/**
 * Whether the page may show *slot*: `off` (preview off), `not_granted` (the owner
 * has not approved this app's `view` verb), `denied` (not this app's slot), or `ok`.
 */
export type BrowserViewAccess = 'off' | 'not_granted' | 'denied' | 'ok'

/** `idle` before the slot's browser has sent anything, else the live view's own state. */
export type BrowserViewState = 'idle' | BrowserLiveState

export interface BrowserViewInfo {
  readonly access: BrowserViewAccess
  readonly state: BrowserViewState
  /** The page's address and title, empty while idle or not allowed. */
  readonly url: string
  readonly title: string
  /** Take over the page; false when refused, not allowed, or nothing is live. */
  claim(): Promise<boolean>
  /** Hand the page back, with an optional note for the Bot (or the hand-back bar's note and choice). */
  release(note?: string | BrowserHandbackSaid): Promise<boolean>
}

const selectSlotApp = (key: string) => (s: RootState): string | null => {
  const slot = s.dashboard.slots.find(x => x.key === key)
  return slot ? slot.app ?? '' : null
}

/** The access rule in the module comment, as a hook. */
export function useBrowserViewAccess(slot: string): BrowserViewAccess {
  const on = usePreviewFlag(PREVIEW_APP_BROWSER_SDK)
  const appId = useAppIdentity()?.appId ?? ''
  const verbs = useContext(AppSdkContext)?.info.permissions.browser
  const stamp = useAppSelector(useMemo(() => selectSlotApp(slot), [slot]))
  if (!on) return 'off'
  if (!verbs?.includes('view')) return 'not_granted'
  return !!appId && stamp === appId ? 'ok' : 'denied'
}

/** The live view of *slot*, and the takeover. Every member is inert unless access is `ok`. */
export function useBrowserView(slot: string): BrowserViewInfo {
  const access = useBrowserViewAccess(slot)
  const ok = access === 'ok'
  const turn = useBrowserLiveCurrent(ok ? slot : null)
  const claim = useCallback(async () => (ok ? browserLiveStore.claim(slot) : false), [ok, slot])
  const release = useCallback(
    async (note?: string | BrowserHandbackSaid) => {
      if (!ok) return false
      const said = typeof note === 'string' ? (note ? { note } : undefined) : note
      return browserLiveStore.release(slot, said)
    },
    [ok, slot],
  )
  return {
    access,
    state: turn?.state ?? 'idle',
    url: turn?.url ?? '',
    title: turn?.title ?? '',
    claim,
    release,
  }
}

// ---- coordinates -------------------------------------------------------------

/** What a mounted `BrowserView` last drew: its picture box in CSS pixels and the frame size. */
interface Geometry { width: number; height: number; frame: FrameSize | null }

const geometry = new Map<string, Geometry>()
const geometryListeners = new Set<() => void>()

/** Called by `BrowserView` when its box or its frame changes size. */
export function reportBrowserViewGeometry(slot: string, next: Geometry | null): void {
  const prev = geometry.get(slot)
  if (next === null) {
    if (!prev) return
    geometry.delete(slot)
  } else {
    if (prev && prev.width === next.width && prev.height === next.height
      && prev.frame?.w === next.frame?.w && prev.frame?.h === next.frame?.h) return
    geometry.set(slot, next)
  }
  for (const l of geometryListeners) l()
}

const subscribeGeometry = (l: () => void) => {
  geometryListeners.add(l)
  return () => { geometryListeners.delete(l) }
}

export interface BrowserCoords {
  /** View point (CSS pixels from the view's top left) to frame pixels; null outside the frame or before one is drawn. */
  toPage(x: number, y: number): { x: number; y: number } | null
  /** Frame pixels to a view point; null before a frame is drawn. */
  toView(x: number, y: number): { x: number; y: number } | null
}

/** Pure form of `useBrowserCoords`, for one geometry. */
export function coordsFor(g: Geometry | undefined): BrowserCoords {
  const rect = () => (g?.frame ? drawnRect({ left: 0, top: 0, width: g.width, height: g.height }, g.frame) : null)
  return {
    toPage(x, y) {
      const r = rect()
      if (!r || !g?.frame || r.width <= 0 || r.height <= 0) return null
      if (x < r.left || y < r.top || x > r.left + r.width || y > r.top + r.height) return null
      return { x: ((x - r.left) / r.width) * g.frame.w, y: ((y - r.top) / r.height) * g.frame.h }
    },
    toView(x, y) {
      const r = rect()
      if (!r || !g?.frame || r.width <= 0 || r.height <= 0) return null
      return { x: r.left + (x / g.frame.w) * r.width, y: r.top + (y / g.frame.h) * r.height }
    },
  }
}

/** Coordinate conversion for the `BrowserView` of *slot* that is on screen. */
export function useBrowserCoords(slot: string): BrowserCoords {
  const g = useSyncExternalStore(subscribeGeometry, () => geometry.get(slot), () => undefined)
  return useMemo(() => coordsFor(g), [g])
}

/** Tests only. */
export function __resetBrowserViewGeometryForTests(): void {
  geometry.clear()
}
