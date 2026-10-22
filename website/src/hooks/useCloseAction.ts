import { useState, useEffect, useCallback, useRef } from 'react'

// Close-button bridge exposed by electron/preload.js. Like the local-gateway
// switch, the choice lives in the desktop app's own config, because it governs
// the app on THIS machine.
//
// Unlike useLocalGateway, a present bridge is not enough: the main process
// answers only the main window's own dashboard, so `supported` turns true only
// once a read succeeds. The switch shows only values the main process
// returned, and is disabled while a write is in flight.
const closeActionAPI = (): CloseActionAPI | undefined => window.closeActionAPI

const canonical = (v: unknown): CloseAction => (v === 'quit' ? 'quit' : 'background')

/** Read/write what the desktop app's main-window close button does. */
export function useCloseAction() {
  const [supported, setSupported] = useState(false)
  // The shipped default, so a first paint matches an untouched install.
  const [closeAction, setCloseActionState] = useState<CloseAction>('background')
  const [pending, setPending] = useState(false)
  // A refused or failed write, shown under the switch until the next attempt.
  const [failed, setFailed] = useState(false)
  const pendingRef = useRef(false)
  const alive = useRef(true)

  useEffect(() => {
    alive.current = true
    const api = closeActionAPI()
    if (api) {
      void api.get().then(v => {
        if (!alive.current) return
        setCloseActionState(canonical(v))
        setSupported(true)
      }).catch(() => {
        // A failed read cannot establish support, so keep the row hidden.
        if (alive.current) setSupported(false)
      })
    }
    return () => { alive.current = false }
  }, [])

  const setCloseAction = useCallback((next: CloseAction) => {
    const api = closeActionAPI()
    if (!api || pendingRef.current) return
    pendingRef.current = true
    setPending(true)
    setFailed(false)
    void api.set(next).then(v => {
      if (alive.current) setCloseActionState(canonical(v))
    }).catch(() => {
      // Nothing was stored, so the switch keeps the value it already shows.
      if (alive.current) setFailed(true)
    }).finally(() => {
      pendingRef.current = false
      if (alive.current) setPending(false)
    })
  }, [])

  return {
    closeAction,
    closeActionSupported: supported,
    closeActionPending: pending,
    closeActionFailed: failed,
    setCloseAction,
  }
}
