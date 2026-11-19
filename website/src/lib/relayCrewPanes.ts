/**
 * Relay Crews as tabs in this window: the desktop shell shows the Crew as a
 * native pane over <main>, under the local top bar, instead of in a window of
 * its own. Only the endpoint ID, the pane's rect and on/off flags cross the
 * bridge; the Crew's own page has no preload and sends nothing. A shell without
 * the pane calls still opens a window.
 *
 * The shell creates every pane hidden; only RelayCrewPaneHost shows the active
 * one, so what fills <main> is decided here, in the instances slice, alone.
 */
import { useCallback } from 'react'
import { useAppDispatch, useAppSelector, type AppDispatch, type RootState } from '../store'
import { relayOpenStarted, relayPaneOpened, type RelayTab } from '../store/instancesSlice'
import { OVERLAY_SELECTOR } from '../hooks/useNativeBrowser'

/** shortcut: a placeholder for how many relay panes stay loaded; the least recent hidden one closes past it. */
export const RELAY_PANE_WARM_CAP = 3
/** The Preview panel's overlays, plus menus and lists (the switcher's own menu drops over the pane) and the relay-only marker on the notification sheet, banner cards and update overlay. */
export const RELAY_PANE_OVERLAY_SELECTOR = [OVERLAY_SELECTOR, '[role="menu"],[role="listbox"],[data-relay-pane-overlay]'].join(',')

type Rect = { x: number; y: number; width: number; height: number }
type Viewport = { width: number; height: number }
/** `window`: the Crew is shown in another window (its own, or the main window when another
 *  dashboard window asked), which came forward instead; no pane exists in this one. */
type OpenResult = { ok: boolean; error?: string; window?: boolean }
export type RelayPaneEvent = { type: string; endpointId: string; index: number }
export type RelayPaneBridge = {
  openPane: (endpointId: string, rect: Rect, viewport: Viewport) => Promise<OpenResult>
  setPaneBounds: (endpointId: string, rect: Rect, viewport: Viewport) => Promise<unknown>
  setPaneOverlay: (endpointId: string, active: boolean) => Promise<unknown>
  setPaneInactive: (endpointId: string, inactive: boolean) => Promise<unknown>
  closePane: (endpointId: string) => Promise<unknown>
  onPaneEvent: (cb: (event: RelayPaneEvent) => void) => () => void
}

/** The pane half of the desktop bridge, or null when the shell has none (a browser, the PWA, an older shell). */
export function relayPaneBridge(): RelayPaneBridge | null {
  const b = (window as { relayCrewAPI?: Partial<RelayPaneBridge> }).relayCrewAPI
  return typeof b?.openPane === 'function' && typeof b.onPaneEvent === 'function' ? (b as RelayPaneBridge) : null
}

/** Where the pane goes, in CSS px with this page's viewport: <main>'s rect, widened
 *  over the local nav rail's column when one sits to its left, because the remote
 *  Crew draws its own rail (one rail on screen, as with an SSH Crew). Null while
 *  <main> has no size. */
export function relayPaneRect(): [Rect, Viewport] | null {
  const r = document.getElementById('main-content')?.getBoundingClientRect()
  if (!r?.width || !r.height) return null
  const rail = document.querySelector<HTMLElement>('nav.focus-chrome-rail')
  const rr = rail?.getBoundingClientRect()
  const x = rail && rr && rr.width > 0 && rr.right <= r.x
    ? Math.max(0, rr.x - (parseFloat(getComputedStyle(rail).marginLeft) || 0))
    : r.x
  return [{ x, y: r.y, width: r.x + r.width - x, height: r.height }, { width: window.innerWidth, height: window.innerHeight }]
}

/** Open (or bring forward) a relay Crew's pane and make its tab the active one, unless a newer
 *  choice was made while it waited: then it stays a hidden tab. Nothing on screen changes before
 *  the answer, so a refusal leaves whatever is in front (an SSH Crew included). Never throws. */
export async function openRelayPane(bridge: RelayPaneBridge, tab: RelayTab, dispatch: AppDispatch): Promise<OpenResult> {
  dispatch(relayOpenStarted())
  const gen = dispatch((_d, getState: () => RootState) => getState().instances.pickSeq ?? 0)
  // <main> has no size while an SSH Crew covers it. The pane starts hidden and the host reports
  // <main>'s real rect when it shows it, so the window's own rect only has to be a valid one.
  const viewport = { width: window.innerWidth, height: window.innerHeight }
  const at = relayPaneRect() ?? [{ x: 0, y: 0, ...viewport }, viewport]
  let res: OpenResult = { ok: false, error: 'UNAVAILABLE' }
  try {
    res = await bridge.openPane(tab.id, ...at)
  } catch { /* stays UNAVAILABLE */ }
  if (res.ok && !res.window) dispatch(relayPaneOpened({ ...tab, gen }))
  return res
}

/** Calls `cb` with each relay Crew another dashboard window handed to this one. Only the main
 *  window's dashboard is handed any, and only while it listens, so a reloading one makes the
 *  other window's open fail visibly instead of being lost. Answers the unsubscribe. */
export function relayOpenRequests(cb: (endpointId: string) => void): () => void {
  const b = (window as { relayCrewAPI?: { onOpenRequest?: (cb: (endpointId: string) => void) => () => void } }).relayCrewAPI
  return typeof b?.onOpenRequest === 'function' ? b.onOpenRequest(cb) : () => {}
}

const NO_TABS: RelayTab[] = []

/** The relay tabs, and picking or closing one. Every tab has an open pane: picking
 *  one brings it forward, and closing one closes its pane, whose `closed` event drops the tab. */
export function useRelayTabs(): { tabs: RelayTab[]; select: (id: string) => void; close: (id: string) => void } {
  const dispatch = useAppDispatch()
  const tabs = useAppSelector(s => s.instances?.relayTabs ?? NO_TABS)
  const select = useCallback((id: string) => {
    const tab = tabs.find(t => t.id === id)
    if (tab) dispatch(relayPaneOpened(tab))
  }, [dispatch, tabs])
  const close = useCallback((id: string) => { void relayPaneBridge()?.closePane(id) }, [])
  return { tabs, select, close }
}
