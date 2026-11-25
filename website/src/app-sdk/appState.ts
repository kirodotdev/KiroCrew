/**
 * Which apps the gateway last said are on or off in this tab, from its
 * `app_state` frame (`useWebSocket` relays it as {@link APP_STATE_EVENT}).
 *
 * An app page reads its app once (`AppPage`), so the host's `info.enabled` can be
 * stale after another tab or the gateway turns the app off. A mounted
 * `BrowserView` reads this instead, and stops at once. The gateway refuses an off
 * app's slot on every live-view route anyway (`browser_live.py`); this is the
 * second line.
 */
import { useSyncExternalStore } from 'react'

/** The window event `useWebSocket` dispatches for each `app_state` frame: `{ app, enabled, seq, boot }`. */
export const APP_STATE_EVENT = 'mc:app-state'

/**
 * The newest frame per app. The gateway sends one app's frames in commit order, but
 * a tab can still get them out of order (a reconnect), so an older `seq` from the
 * same gateway process (`boot`) is dropped: a late "off" must not overwrite a newer
 * "on". Comparing beats re-reading the app on each frame, whose replies can land out
 * of order too. A new `boot` (the gateway restarted) is always taken.
 */
const seen = new Map<string, { enabled: boolean; seq: number; boot: string }>()
const listeners = new Set<() => void>()

if (typeof window !== 'undefined') {
  window.addEventListener(APP_STATE_EVENT, (e: Event) => {
    const d = (e as CustomEvent<{ app?: unknown; enabled?: unknown; seq?: unknown; boot?: unknown }>).detail
    if (typeof d?.app !== 'string' || typeof d.enabled !== 'boolean') return
    if (typeof d.seq !== 'number' || typeof d.boot !== 'string') return
    const last = seen.get(d.app)
    if (last && last.boot === d.boot && d.seq < last.seq) return
    seen.set(d.app, { enabled: d.enabled, seq: d.seq, boot: d.boot })
    for (const l of listeners) l()
  })
}

const subscribe = (l: () => void) => {
  listeners.add(l)
  return () => { listeners.delete(l) }
}

/** What the gateway last said about *app* in this tab: true (on), false (off), or undefined (nothing yet). */
export function useAppStateSeen(app: string): boolean | undefined {
  return useSyncExternalStore(subscribe, () => (app ? seen.get(app)?.enabled : undefined), () => undefined)
}

/** Tests only. */
export function __resetAppStateForTests(): void {
  seen.clear()
  for (const l of listeners) l()
}
