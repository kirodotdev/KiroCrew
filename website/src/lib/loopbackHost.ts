/** Loopback hostnames that share cookies by host (port-agnostic). */
const LOOPBACK_HOSTS = new Set(['localhost', '127.0.0.1', '0.0.0.0', '[::1]', '::1'])

/** True for any host that resolves to the local machine and can therefore share
 *  host-scoped cookies with a same-host dashboard — the loopback IPs/names plus
 *  the `*.localhost` reserved TLD (e.g. `kirocrew.localhost`, which the desktop
 *  app and tunnels use). Lives outside the Browser panel's modules so the
 *  offline cover can share it without importing a panel owner. */
export function isLoopbackHost(h: string): boolean {
  return LOOPBACK_HOSTS.has(h) || h === 'localhost' || h.endsWith('.localhost')
}
