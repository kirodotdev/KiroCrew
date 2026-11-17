/**
 * Relay Crews as tabs in this window: the desktop shell shows the Crew as a
 * native pane over <main>, under the local top bar, instead of in a window of
 * its own. Only the endpoint ID, the pane's rect and on/off flags cross the
 * bridge; the Crew's own page has no preload and sends nothing.
 *
 * The default wherever the shell has the pane calls. A desktop app older than
 * them still opens the Crew in a window of its own.
 */
import { useCallback } from 'react'
import { useAppDispatch, useAppSelector, type AppDispatch, type RootState } from '../store'
import { relayPaneOpened, setActiveId, type RelayTab } from '../store/instancesSlice'
import { OVERLAY_SELECTOR } from '../hooks/useNativeBrowser'

/** shortcut: a placeholder for how many relay panes stay loaded; the least recent hidden one closes past it. */
export const RELAY_PANE_WARM_CAP = 3
/** The Preview panel's overlays, plus menus and lists (the switcher's own menu drops over the pane) and the relay-only marker on the notification sheet, banner cards and update overlay. */
export const RELAY_PANE_OVERLAY_SELECTOR = [OVERLAY_SELECTOR, '[role="menu"],[role="listbox"],[data-relay-pane-overlay]'].join(',')

type Rect = { x: number; y: number; width: number; height: number }
type Viewport = { width: number; height: number }
/** `window`: the Crew is open in a window of its own, which came forward instead; no pane exists. */
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

/** The pane half of the desktop bridge, or null when the shell has none (a browser tab, or a desktop app older than the pane calls). */
export function relayPaneBridge(): RelayPaneBridge | null {
  const b = (window as { relayCrewAPI?: Partial<RelayPaneBridge> }).relayCrewAPI
  return typeof b?.openPane === 'function' && typeof b.onPaneEvent === 'function' ? (b as RelayPaneBridge) : null
}

/** Where the pane goes: <main>'s rect in CSS px, with this page's viewport; null while <main> has no size. */
export function relayPaneRect(): [Rect, Viewport] | null {
  const r = document.getElementById('main-content')?.getBoundingClientRect()
  if (!r?.width || !r.height) return null
  return [{ x: r.x, y: r.y, width: r.width, height: r.height }, { width: window.innerWidth, height: window.innerHeight }]
}

/** Open (or bring forward) a relay Crew's pane and make its tab the active one. Never throws. */
export async function openRelayPane(bridge: RelayPaneBridge, tab: RelayTab, dispatch: AppDispatch): Promise<OpenResult> {
  // <main> is hidden under an SSH Crew: bring the local Crew forward and let it lay out before measuring.
  if (dispatch((_d, getState: () => RootState) => getState().instances.activeId) != null) {
    dispatch(setActiveId(null))
    await new Promise(r => requestAnimationFrame(r))
  }
  const at = relayPaneRect()
  let res: OpenResult = { ok: false, error: 'UNAVAILABLE' }
  try {
    if (at) res = await bridge.openPane(tab.id, ...at)
  } catch { /* stays UNAVAILABLE */ }
  if (res.ok && !res.window) dispatch(relayPaneOpened(tab))
  return res
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
