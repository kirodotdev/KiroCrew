/**
 * Evict the desktop shell's HTTP cache for one remote-crew pane origin.
 *
 * Why a pane can need this: its bundle is an ES module graph of ~240
 * content-hashed chunks. A chunk the gateway did not have at request time
 * (dist/ mid-swap during an upgrade) was answered 404 -- and, before the
 * gateway-side fix, with `Cache-Control: immutable`, so Chromium stored the
 * 404 for a year under that URL. Icon chunks keep their hash across releases,
 * so every later bundle that imports the same chunk hits the cached 404, the
 * whole `<script type=module>` fails silently, and the pane never boots. The
 * cache key is the LOCAL loopback URL, so re-minting the token, rebuilding the
 * tunnel or restarting the remote gateway can never clear it -- only eviction
 * of that origin's cache can, and only the main process can do that.
 *
 * Returns null when no desktop bridge is present (plain browser, tests), so a
 * caller can keep its synchronous path there and only await in Electron.
 */
export function clearPaneHttpCache(origin: string): Promise<boolean> | null {
  const fn = typeof window !== 'undefined' ? window.electronAPI?.clearPaneHttpCache : undefined
  if (typeof fn !== 'function') return null
  try {
    return fn(origin).then(
      v => v === true,
      () => false,
    )
  } catch {
    return Promise.resolve(false)
  }
}

/** The loopback origin the parent frames pane `port` on (see InstancesViewport.srcFor). */
export function paneOriginFor(port: number): string {
  return `http://${window.location.hostname}:${port}`
}
