/**
 * `useKeepAlive`: an app's routed page asks to keep running, hidden, when the
 * person opens another dashboard page, for as long as it is busy (a recording, an
 * upload). Off by default; the dashboard shows the person which app is running
 * and lets them return to it or stop it.
 */
import { createContext, useContext, useEffect, useState } from 'react'

/**
 * - `off`: the page closes when the person leaves it, as usual.
 * - `on`: the page stays mounted, hidden, when the person leaves it.
 * - `refused`: the dashboard already keeps as many pages as it allows; ask again
 *   by setting `busy` false and then true.
 * - `unsupported`: this surface cannot keep the page (a side-panel tab, a popped-out
 *   window).
 */
export type KeepAliveStatus = 'off' | 'on' | 'refused' | 'unsupported'

/** What a host surface that can keep its app's page hands the app. Host-side.
 *  `holder` is one `useKeepAlive` call's own token: the page stays kept while any
 *  holder is busy, and one caller going false releases only its own hold. */
export interface KeepAliveControl {
  request: (holder: object, busy: boolean) => KeepAliveStatus
}

export const KeepAliveContext = createContext<KeepAliveControl | null>(null)

/** Keep this page running while `busy` is true. Turn it off as soon as the work ends.
 *  Several components may call it; the page stays while any of them is busy. */
export function useKeepAlive(busy: boolean): KeepAliveStatus {
  const control = useContext(KeepAliveContext)
  const [holder] = useState<object>(() => ({}))
  const [status, setStatus] = useState<KeepAliveStatus>(control ? 'off' : 'unsupported')
  useEffect(() => {
    if (!control) {
      setStatus('unsupported')
      return
    }
    setStatus(control.request(holder, busy))
    if (!busy) return
    return () => { control.request(holder, false) }
  }, [control, holder, busy])
  return status
}
