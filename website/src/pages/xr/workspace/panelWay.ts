/**
 * Which way the gateway's panels hold: open, where a press on a page does what it does on the dashboard, or guarded
 * (`browser.xr_pages_guarded`). The gateway says so when it is asked, and again when the setting changes while a
 * panel of this socket stands (`panel_hub.py`, `way`).
 *
 * The XR page needs it for one thing: a page comes up by itself only while the panels are open. So it asks when the
 * socket opens and before a page comes up, and until it has heard "open" nothing comes up.
 */
import { useSyncExternalStore } from 'react'
import { listenXrPanels, sendXrPanel, xrPanelLinkOpen } from '../../../hooks/xrPanelLink'

export type PanelWay = 'open' | 'guarded' | null

let way: PanelWay = null
let askedAt = 0
const watchers = new Set<() => void>()
let stop: (() => void) | null = null

const set = (next: PanelWay) => { if (next !== way) { way = next; for (const w of [...watchers]) w() } }

/** Ask the gateway which way holds. Once a second at most: the answer is a setting, and it is told when it changes. */
export function askPanelWay(now = performance.now()): void {
  if (!xrPanelLinkOpen() || (askedAt && now - askedAt < 1000)) return
  askedAt = now
  sendXrPanel({ op: 'way' })
}

/** The way that the gateway said last on this socket, or `null` when it has said none. */
export const panelWay = (): PanelWay => way

function subscribe(watch: () => void): () => void {
  watchers.add(watch)
  if (!stop) {
    stop = listenXrPanels({
      // A new socket: what was said on the one before says nothing of this one.
      open: () => { set(null); askedAt = 0; askPanelWay() },
      close: () => set(null),
      message: data => { if (data.op === 'way' && typeof data.guarded === 'boolean') set(data.guarded ? 'guarded' : 'open') },
      picture: () => undefined,
    })
    askPanelWay()
  }
  return () => {
    watchers.delete(watch)
    if (watchers.size === 0 && stop) { stop(); stop = null; way = null; askedAt = 0 }
  }
}

/** The way that holds, as a component reads it. */
export const usePanelWay = (): PanelWay => useSyncExternalStore(subscribe, panelWay, panelWay)
