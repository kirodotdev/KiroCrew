import { useMainWindowSetting } from './useMainWindowSetting'

// Local-gateway bridge exposed by electron/preload.js. The setting lives in the
// desktop app's own config, not in the gateway's, because it decides whether a
// gateway runs on THIS machine — a question the gateway itself cannot answer for
// the client asking it. In a plain browser or the PWA the bridge is absent and
// there is nothing to manage, so `supported` is false and the UI hides the
// control rather than showing one that cannot work.
const localGatewayAPI = (): LocalGatewayAPI | undefined => window.localGatewayAPI

const canonical = (v: unknown): boolean => v !== false

/**
 * Read/write the desktop app's "run a local gateway" choice.
 *
 * main.js answers only the main window's dashboard, so `supported` turns true
 * only once a read succeeds: a connection window to another machine, whose
 * read is refused, never paints the switch, rather than painting it and then
 * pulling it out. The switch shows only values main.js returned, so a refused
 * write leaves it where it was. `enabled` starts true, the shipped default.
 */
export function useLocalGateway() {
  const s = useMainWindowSetting(localGatewayAPI, canonical, true)
  return {
    localGatewayEnabled: s.value,
    localGatewaySupported: s.supported,
    localGatewayPending: s.pending,
    setLocalGatewayEnabled: s.set,
  }
}
