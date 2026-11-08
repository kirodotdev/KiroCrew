import { useState, useEffect, useCallback, useRef } from 'react'

/** A desktop-app setting bridge exposed by electron/preload.js. */
interface MainWindowSettingAPI<In> {
  get: () => Promise<unknown>
  set: (value: In) => Promise<unknown>
}

/**
 * One desktop-app setting the main process answers only to the main window's
 * own dashboard (useCloseAction, useTrayIcon). Like the local-gateway switch,
 * the choice lives in the desktop app's own config, because it governs the app
 * on THIS machine.
 *
 * Unlike useLocalGateway, a present bridge is not enough: `supported` turns
 * true only once a read succeeds. The value is only ever one the main process
 * returned, and a write is ignored while another is in flight. A change of
 * `readKey` reads the value again.
 */
export function useMainWindowSetting<T, In>(
  bridge: () => MainWindowSettingAPI<In> | undefined,
  canonical: (v: unknown) => T,
  initial: T,
  readKey?: unknown,
) {
  const [supported, setSupported] = useState(false)
  const [value, setValue] = useState<T>(initial)
  const [pending, setPending] = useState(false)
  // A refused or failed write, shown under the switch until the next attempt.
  const [failed, setFailed] = useState(false)
  const pendingRef = useRef(false)
  const alive = useRef(true)

  useEffect(() => {
    alive.current = true
    return () => { alive.current = false }
  }, [])

  useEffect(() => {
    let current = true
    const api = bridge()
    if (api) {
      void api.get().then(v => {
        if (!current) return
        setValue(canonical(v))
        setSupported(true)
      }).catch(() => {
        // A failed read cannot establish support, so keep the row hidden.
        if (current) setSupported(false)
      })
    }
    return () => { current = false }
  }, [bridge, canonical, readKey])

  const set = useCallback((next: In) => {
    const api = bridge()
    if (!api || pendingRef.current) return
    pendingRef.current = true
    setPending(true)
    setFailed(false)
    void api.set(next).then(v => {
      if (alive.current) setValue(canonical(v))
    }).catch(() => {
      // Nothing was stored, so the switch keeps the value it already shows.
      if (alive.current) setFailed(true)
    }).finally(() => {
      pendingRef.current = false
      if (alive.current) setPending(false)
    })
  }, [bridge, canonical])

  return { value, supported, pending, failed, set }
}
