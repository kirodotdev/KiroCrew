import { useState, useEffect, useCallback } from 'react'

// Local-gateway bridge exposed by electron/preload.js. The setting lives in the
// desktop app's own config, not in the gateway's, because it decides whether a
// gateway runs on THIS machine — a question the gateway itself cannot answer for
// the client asking it. In a plain browser or the PWA the bridge is absent and
// there is nothing to manage, so `supported` is false and the UI hides the
// control rather than showing one that cannot work.
type LocalGatewayAPI = {
  get(): Promise<boolean>
  set(enabled: boolean): Promise<boolean>
}
const localGatewayAPI = (): LocalGatewayAPI | undefined =>
  (window as { localGatewayAPI?: LocalGatewayAPI }).localGatewayAPI

/**
 * Read/write the desktop app's "run a local gateway" choice.
 *
 * `enabled` starts true so a first paint before the bridge answers matches the
 * shipped default instead of flashing the opposite state.
 */
export function useLocalGateway() {
  const supported = !!localGatewayAPI()
  const [enabled, setEnabled] = useState(true)

  useEffect(() => {
    const api = localGatewayAPI()
    if (!api) return
    let alive = true
    void api.get().then(v => { if (alive) setEnabled(v) }).catch(() => {})
    return () => { alive = false }
  }, [])

  // The stored value is authoritative, so the switch renders what main.js
  // actually wrote rather than what was asked for.
  const setLocalGatewayEnabled = useCallback((next: boolean) => {
    const api = localGatewayAPI()
    if (!api) return
    setEnabled(next)
    void api.set(next).then(v => setEnabled(v)).catch(() => {})
  }, [])

  return { localGatewayEnabled: enabled, localGatewaySupported: supported, setLocalGatewayEnabled }
}
