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
import { useCallback, useContext, useEffect, useMemo, useRef, useState, useSyncExternalStore } from 'react'

import { api } from '../api/client'

import { browserLiveStore, useBrowserLiveCurrent, type BrowserLiveState } from '../hooks/useBrowserLive'
import { drawnRect, type FrameSize } from '../hooks/useBrowserLive.input'
import type { BrowserHandbackSaid } from '../api/client/browserLive'
import { usePreviewFlag } from '../hooks/usePreviewFlag'
import { useAppSelector } from '../store'
import type { RootState } from '../store'
import { PREVIEW_APP_BROWSER_SDK } from '../utils/previewFlags'
import { useAppIdentity } from './identity'
import { useAppStateSeen } from './appState'
import { AppSdkContext } from './scopedApi'

/**
 * Whether the page may show *slot*: `off` (preview off), `app_off` (the app is
 * turned off), `not_granted` (the owner
 * has not approved this app's `view` verb), `denied` (not this app's slot), or `ok`.
 */
export type BrowserViewAccess = 'off' | 'app_off' | 'not_granted' | 'denied' | 'ok'

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
  /** The slot's own tabs, to show one in a second `BrowserView` (`tabId`); empty when not allowed. */
  tabs(): Promise<BrowserViewTab[]>
}

/** One of a slot's own tabs. `current` is the tab its Bot is on. */
export interface BrowserViewTab {
  readonly tabId: string
  readonly url: string
  readonly title: string
  readonly current: boolean
}

function readTabs(raw: unknown): BrowserViewTab[] {
  const list = raw && typeof raw === 'object' ? (raw as { tabs?: unknown }).tabs : undefined
  if (!Array.isArray(list)) return []
  return list.flatMap((t) => {
    if (!t || typeof t !== 'object') return []
    const r = t as Record<string, unknown>
    if (typeof r.tab_id !== 'string' || !r.tab_id) return []
    return [{
      tabId: r.tab_id,
      url: typeof r.url === 'string' ? r.url : '',
      title: typeof r.title === 'string' ? r.title : '',
      current: r.current === true,
    }]
  })
}

const selectSlotApp = (key: string) => (s: RootState): string | null => {
  const exactSlot = s.dashboard.slots.find(x => x.key === key)
  return exactSlot ? (exactSlot.app ?? '') : null
}

/** The access rule in the module comment, as a hook. */
export function useBrowserViewAccess(slot: string): BrowserViewAccess {
  const on = usePreviewFlag(PREVIEW_APP_BROWSER_SDK)
  const appId = useAppIdentity()?.appId ?? ''
  const info = useContext(AppSdkContext)?.info
  const verbs = info?.permissions.browser
  const stamp = useAppSelector(useMemo(() => selectSlotApp(slot), [slot]))
  const seen = useAppStateSeen(appId)
  if (!on) return 'off'
  // The app was turned off (here, in another tab, or by the gateway): the
  // gateway's `app_state` frame says so at once, newer than the host's own read of
  // the app (`info.enabled`), and the view stops. The gateway refuses an off app's
  // slot on every live-view route, so this is the second line.
  if (seen === undefined ? info?.enabled === false : !seen) return 'app_off'
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
  const tabs = useCallback(async () => {
    if (!ok) return []
    try { return readTabs(await api.browserLiveViewTabs(slot)) } catch { return [] }
  }, [ok, slot])
  return {
    access,
    state: turn?.state ?? 'idle',
    url: turn?.url ?? '',
    title: turn?.title ?? '',
    claim,
    release,
    tabs,
  }
}

/**
 * A second live view of *tabId*, one of *slot*'s own tabs, while *enabled*: its key,
 * or the gateway's refusal. Opened on mount and closed on unmount or a new tab. A
 * reload or a closed page runs no cleanup, so the gateway also ends a view nobody
 * has looked at for a while (`live_hub_views.VIEW_IDLE_S`); `BrowserView` keeps its
 * view looked at (`VIEW_KEEPALIVE_MS`) and opens it again if it was ended. A view
 * is for looking only.
 *
 * Returns `turnId` alongside the key so the caller can pass it to `browserLiveViewClose`,
 * which checks it matches the current turn — preventing a stale component from
 * accidentally closing a later view that received the same key.
 */
export function useBrowserSecondView(slot: string, tabId: string | undefined, enabled: boolean): { viewKey: string | null; turnId: string | null; error: string; reopen(): void } {
  const [opened, setOpened] = useState<{ for: string; viewKey: string | null; error: string }>({ for: '', viewKey: null, error: '' })
  // Bumped to open the view again, after the gateway ended it (idle, `VIEW_IDLE_S`).
  const [round, setRound] = useState(0)
  const want = enabled && tabId ? `${slot}\n${tabId}\n${round}` : ''
  const reopen = useCallback(() => setRound(n => n + 1), [])
  // The turn the gateway assigned this open: passed on close so the backend can
  // reject closes from stale components that happen to hold the same key.
  const openTurnRef = useRef<string | null>(null)
  useEffect(() => {
    if (!want || !tabId) return
    let gone = false
    let key = ''
    openTurnRef.current = null
    void api.browserLiveViewOpen(slot, tabId).then((r) => {
      const viewKey = typeof r?.view_key === 'string' ? r.view_key : ''
      const turnId = typeof r?.turn_id === 'string' ? r.turn_id : ''
      if (gone) {
        if (viewKey) {
          void api.browserLiveViewClose(viewKey, turnId).catch(() => {})
          browserLiveStore.forgetView(viewKey)
        }
        return
      }
      key = viewKey
      openTurnRef.current = turnId
      // Clear the tombstone for this key: a fresh open should receive its events.
      if (viewKey) browserLiveStore.reviveView(viewKey)
      setOpened({ for: want, viewKey: viewKey || null, error: '' })
    }, (e: unknown) => {
      if (!gone) setOpened({ for: want, viewKey: null, error: e instanceof Error ? e.message : String(e) })
    })
    return () => {
      gone = true
      if (key) {
        void api.browserLiveViewClose(key, openTurnRef.current ?? '').catch(() => {})
        browserLiveStore.forgetView(key)
      }
    }
  }, [want, slot, tabId])
  return opened.for === want && want ? { viewKey: opened.viewKey, turnId: openTurnRef.current, error: opened.error, reopen } : { viewKey: null, turnId: null, error: '', reopen }
}

/** How often a mounted second view on screen tells the gateway it is still looked at; well inside `VIEW_IDLE_S` (120 s). */
export const VIEW_KEEPALIVE_MS = 30_000

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
