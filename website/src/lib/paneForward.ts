/**
 * Have the desktop app forward the Remote Crew pane ports this window shows.
 *
 * Each pane frames `http://localhost:<port>/`, and that port is the forward the
 * dashboard's gateway opened to the crew on ITS OWN machine. In the desktop
 * app's client-only mode the dashboard itself is a gateway on another machine,
 * reached through the app's managed SSH forward, so on this machine nothing
 * answers on a pane's port until the app forwards it too. The app does that
 * through `window.remoteCrewPaneAPI` (electron/preload.js): this module names
 * every warm pane's port to it and tells the viewport which panes must wait
 * for their forward before their frame loads, so the first load does not race
 * the forward and fail. A pane whose forward is refused or never comes up
 * loads anyway, and the load watchdog explains it as before.
 *
 * Without the bridge (a plain browser, the PWA, a desktop shell older than the
 * bridge) nothing is ever pending, and a desktop window whose app keeps no
 * forward is answered at once.
 */
import { useCallback, useEffect, useState } from 'react'
import { paneLog } from './paneLog'

type ForwardPorts = (ports: number[]) => Promise<unknown>

function paneForwardBridge(): ForwardPorts | null {
  const fn = typeof window !== 'undefined' ? window.remoteCrewPaneAPI?.forwardPorts : undefined
  return typeof fn === 'function' ? fn : null
}

/** One window's pane ports, each with the generation it was asked under. */
interface Forwards {
  key: string
  /** Bumped each time a port joins the set, so a port shown again is asked afresh. */
  gen: Readonly<Record<number, number>>
  /** The generation whose answer has arrived, per port. */
  settled: Readonly<Record<number, number>>
  seq: number
}

const NONE: Forwards = { key: '', gen: {}, settled: {}, seq: 0 }

function portsKey(ports: readonly number[]): string {
  const valid = ports.filter(p => Number.isInteger(p) && p > 0 && p <= 65535)
  return [...new Set(valid)].sort((a, b) => a - b).join(',')
}

/**
 * Ask for `ports` (every warm pane's port) and return whether a pane on a
 * port must still wait for its forward before its frame is mounted.
 */
export function usePaneForwardPending(ports: readonly number[]): (port: number) => boolean {
  const key = paneForwardBridge() ? portsKey(ports) : ''
  const [state, setState] = useState<Forwards>(NONE)
  let current = state
  if (state.key !== key) {
    // Derived during render (React's "adjust state when a prop changes"), so a
    // newly warm pane is pending in the very render that first shows it.
    let seq = state.seq
    const gen: Record<number, number> = {}
    const settled: Record<number, number> = {}
    for (const port of key ? key.split(',').map(Number) : []) {
      // A port still shown keeps its generation; one shown again after it left
      // gets a new one, because the app dropped its forward when it left.
      gen[port] = state.gen[port] ?? ++seq
      if (state.settled[port] === gen[port]) settled[port] = gen[port]
    }
    current = { key, gen, settled, seq }
    setState(current)
  }
  const asked = current.gen
  useEffect(() => {
    const forward = paneForwardBridge()
    if (!forward) return
    const ports = Object.keys(asked).map(Number)
    paneLog('pane-forward-asked', { ports })
    let answer: Promise<unknown>
    try {
      answer = Promise.resolve(forward(ports))
    } catch (err) {
      answer = Promise.reject(err)
    }
    void answer
      .catch((err: unknown) => ({ error: err instanceof Error ? err.message : String(err) }))
      .then(result => {
        paneLog('pane-forward-answered', { ports, result })
        setState(prev => {
          const settled = { ...prev.settled }
          let changed = false
          for (const port of ports) {
            // An answer for a generation the port has since left behind is stale.
            if (prev.gen[port] !== asked[port] || settled[port] === asked[port]) continue
            settled[port] = asked[port]
            changed = true
          }
          return changed ? { ...prev, settled } : prev
        })
      })
  }, [asked])
  return useCallback(
    (port: number) => current.gen[port] !== undefined && current.settled[port] !== current.gen[port],
    [current],
  )
}
