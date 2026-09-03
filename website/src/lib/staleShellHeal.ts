// Boot-time self-heal for a stale SPA shell.
//
// The failure this closes (observed on a real phone over a tunnel): the
// service worker's offline fallback serves a cached index.html whose hashed
// assets still live in the HTTP cache — a complete time capsule. APIs keep
// working, so the page LOOKS current while running arbitrarily old code, and
// on iOS Safari an ordinary reload does not bypass the service worker, so the
// user cannot escape by refreshing. Two server-side fixes reduce how the
// capsule forms (shell cache refresh on successful navigations, per-build
// cache names for dirty builds); this module is the client-side backstop that
// DETECTS the capsule and breaks out of it.
//
// Mechanism: shortly after boot, fetch the server's live index.html
// (cache: 'no-store' — the gateway also serves it no-store) and compare its
// entry-script URL against the script tag this document is actually running.
// A mismatch means the server has moved on; unregister service workers, drop
// CacheStorage, and reload once. A sessionStorage stamp caps the heal at one
// automatic reload per window per interval, so a misbehaving server can never
// put the page into a reload loop.

/** Minimum spacing between automatic heal reloads for one tab. */
export const HEAL_RELOAD_MIN_INTERVAL_MS = 10 * 60_000
/** Boot delay before probing: keep the check off the critical startup path. */
export const HEAL_PROBE_DELAY_MS = 3_000

const STAMP_KEY = 'kc-stale-shell-heal-at'

/** The hashed entry-script URL in an index.html document (absolute). */
export function extractEntryScript(html: string, origin: string): string | null {
  const m = html.match(/<script[^>]+type="module"[^>]+src="(\/assets\/[^"]+\.js)"/)
    ?? html.match(/src="(\/assets\/index-[^"]+\.js)"/)
  if (!m) return null
  try {
    return new URL(m[1], origin).href
  } catch {
    return null
  }
}

/** Pure decision core, injectable for tests. */
export async function healIfStale(deps: {
  runningEntry: string | null
  fetchShell: () => Promise<{ ok: boolean; text: () => Promise<string> }>
  origin: string
  now: () => number
  readStamp: () => number
  writeStamp: (t: number) => void
  clearSwAndCaches: () => Promise<void>
  reload: () => void
}): Promise<'healed' | 'fresh' | 'skipped'> {
  const { runningEntry } = deps
  // A dev server (no hashed assets) or an unexpected document shape: nothing
  // to compare, never heal on guesswork.
  if (!runningEntry || !runningEntry.includes('/assets/')) return 'skipped'
  let served: string | null = null
  try {
    const r = await deps.fetchShell()
    if (!r.ok) return 'skipped'
    served = extractEntryScript(await r.text(), deps.origin)
  } catch {
    return 'skipped' // offline — the fallback shell is doing its intended job
  }
  if (!served) return 'skipped'
  if (served === runningEntry) return 'fresh'
  if (deps.now() - deps.readStamp() < HEAL_RELOAD_MIN_INTERVAL_MS) return 'skipped'
  deps.writeStamp(deps.now())
  await deps.clearSwAndCaches()
  deps.reload()
  return 'healed'
}

/** Install the boot probe. Call once from the composition root. */
export function installStaleShellHeal(): void {
  if (typeof window === 'undefined' || typeof document === 'undefined') return
  window.setTimeout(() => {
    const running = document.querySelector<HTMLScriptElement>('script[type="module"][src*="/assets/"]')?.src ?? null
    void healIfStale({
      runningEntry: running,
      fetchShell: () => fetch('/index.html', { cache: 'no-store' }),
      origin: window.location.origin,
      now: () => Date.now(),
      readStamp: () => Number(window.sessionStorage.getItem(STAMP_KEY) ?? 0),
      writeStamp: (t) => window.sessionStorage.setItem(STAMP_KEY, String(t)),
      clearSwAndCaches: async () => {
        try {
          const regs = await window.navigator.serviceWorker?.getRegistrations?.() ?? []
          await Promise.all(regs.map((reg) => reg.unregister()))
        } catch { /* no SW support — nothing to clear */ }
        try {
          const keys = await window.caches?.keys?.() ?? []
          await Promise.all(keys.map((k) => window.caches.delete(k)))
        } catch { /* CacheStorage unavailable */ }
      },
      reload: () => window.location.reload(),
    })
  }, HEAL_PROBE_DELAY_MS)
}
