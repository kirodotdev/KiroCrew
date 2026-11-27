/**
 * Loads the app-sdk registry (`window.__kirocrew_modules`) that every app bundle
 * needs before it runs. It is its own 501 KB chunk (126 KB gz) because it holds
 * every lucide icon (#18561), so the first load leaves it out.
 *
 * App entry points (rail rows, the pop-out button, app side-panel tabs,
 * session-control chips) start it on hover, focus or press, so the fetch is
 * mostly done by the click. No idle prefetch: a user who never opens an app
 * should not pay for it.
 */
let registry: Promise<unknown> | null = null
/** The load still in flight: who is waiting on it, or null once it settled. */
let inFlight: { demanded: boolean } | null = null

function start(): Promise<unknown> {
  if (!registry) {
    // Marked before the import, and the handlers are chained on a separate
    // statement: chained on `import()` itself, Vite's build puts them inside its
    // preload wrapper, so they ran (and cleared the mark) before the wrapper
    // fired `vite:preloadError`, and main.tsx then reloaded the page.
    const load = { demanded: false }
    inFlight = load
    const p = import('../app-sdk/shared-modules')
    const settle = () => { if (inFlight === load) inFlight = null }
    registry = p.then(
      m => {
        settle()
        // main.tsx cancels the preload error of a hover or startup load; Vite
        // then resolves to undefined instead of rejecting.
        if (!m) throw new Error('app registry chunk failed to load')
        return m
      },
      (e: unknown) => { settle(); throw e },
    ).catch((e: unknown) => {
      // A failed fetch (offline, a redeploy dropped the chunk): forget it so the
      // next call imports again. The browser keeps the failed import in its
      // module map, though, so that call fails at once with no new request; the
      // next app the user opens then takes main.tsx's one reload, which loads it.
      registry = null
      throw e
    })
  }
  return registry
}

/**
 * Start (or join) the registry load. Safe to call any number of times.
 * `demanded` is false for a load that must not reload the page when the chunk
 * is gone (a hover, a background startup mount); an app the user opens passes true.
 */
export function loadAppRegistry({ demanded = true }: { demanded?: boolean } = {}): Promise<unknown> {
  const p = start()
  if (demanded && inFlight) inFlight.demanded = true
  return p
}

function prefetch() {
  loadAppRegistry({ demanded: false }).catch(() => {}) // the real open reports a failure
}

/** Spread onto an app entry point: hover, focus or press starts the load. */
export const appRegistryPrefetchHandlers = {
  onPointerEnter: prefetch,
  onFocus: prefetch,
  onPointerDown: prefetch,
}

const REGISTRY_CHUNK_RE = /\/assets\/shared-modules-[^/\s]*\.(?:js|css)\b/

/**
 * Whether a `vite:preloadError` is the registry chunk failing while only a hover
 * or a startup mount wanted it. main.tsx then skips its stale-chunk reload: a hover must not reload
 * the page over unsaved work. An app open that asks later fails on its own and
 * takes the normal reload path. Same rule as `isSpeculativeTerminalChunkError`.
 */
export function isSpeculativeAppRegistryChunkError(error: unknown): boolean {
  if (!inFlight || inFlight.demanded) return false
  const message = error && typeof error === 'object' && 'message' in error ? (error as { message?: unknown }).message : undefined
  if (typeof message !== 'string') return false
  // WebKit names no URL; in this window the hover's load is the likely source.
  if (!/\/assets\//.test(message)) return true
  return REGISTRY_CHUNK_RE.test(message)
}
