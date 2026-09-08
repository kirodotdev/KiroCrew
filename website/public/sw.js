// Minimal service worker for PWA installability.
// Network-first for the SPA shell; hashed assets go to the network with a bounded
// 5xx retry; everything else goes straight to network.
//
// Cache contains ONLY the shell (/ and /index.html). No other responses are
// cached — hashed assets rely on HTTP immutable caching, and app/API routes
// must never be served from SW storage.

// CACHE_VERSION: a stable identifier per build. In production this file is
// post-processed by the swVersionPlugin (vite.config.ts) which replaces the
// placeholder below with version+git-SHA. In dev (un-processed) the cache
// name keeps the literal placeholder — still a valid, stable cache name,
// just without per-deploy busting.
const CACHE_VERSION = '%%SW_BUILD_HASH%%'
const CACHE = 'kirocrew-shell-' + CACHE_VERSION
const SHELL = ['/', '/index.html']

self.addEventListener('install', e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL)))
  self.skipWaiting()
})

self.addEventListener('activate', e => {
  // Purge every cache except the current shell cache
  e.waitUntil(
    caches.keys().then(keys => Promise.all(
      keys.filter(k => k !== CACHE).map(k => caches.delete(k))
    ))
  )
  self.clients.claim()
})

// ── Hashed-asset retry ──────────────────────────────────────────────
// Assets are still NOT cached here — the immutable HTTP cache owns them. What
// this adds is a bounded retry, because one 502 on a module script is not a
// degraded page, it is a dead one.
//
// A page load asks for the entire module graph at once, and the hop in front of
// the gateway has a lower real concurrency ceiling than it advertises:
// `tailscale serve` announces 250 concurrent HTTP/2 streams and starts answering
// 502 past roughly 140. Measured against a Windows gateway on one connection —
// 120 streams: 120 OK; 200 streams: 140 OK + 60x502; 247 streams: 143 OK +
// 104x502. The 502s land on module scripts, so the shell paints its dark skeleton
// and the app never boots. On a phone that reads as a black screen that no reload
// clears, because the navigation keeps being served from the shell cache above
// while the modules keep failing.
//
// The failure is capacity-shaped, so the backoff is JITTERED — retrying a whole
// failed wave on one tick just rebuilds the burst that caused it. Only 5xx and
// network errors are retried: a 404 means the asset is genuinely gone (a stale
// shell pointing at a pruned build) and must surface at once instead of costing
// three round trips. Safe to re-issue `request` without cloning because the fetch
// handler already returned for every non-GET, and a GET carries no body to
// consume.
const ASSET_RETRIES = 2
const ASSET_RETRY_BASE_MS = 150

async function fetchAssetWithRetry(request) {
  let lastResponse = null
  for (let attempt = 0; attempt <= ASSET_RETRIES; attempt++) {
    if (attempt > 0) {
      const spread = ASSET_RETRY_BASE_MS * attempt + Math.random() * ASSET_RETRY_BASE_MS
      await new Promise(resolve => setTimeout(resolve, spread))
    }
    try {
      const resp = await fetch(request)
      if (resp.status < 500) return resp
      lastResponse = resp
    } catch {
      // Deliberately keeps any 5xx already captured. Clearing it here would let a
      // later throw downgrade a real status into the synthetic network error the
      // return below exists to avoid, so a 502-then-dropped-connection sequence
      // would misreport the gateway's actual answer. A throw itself carries no
      // status worth recording.
    }
  }
  // Out of attempts. Hand back the last real response when there was one, so the
  // browser reports the true status rather than a synthetic network failure.
  return lastResponse || Response.error()
}

self.addEventListener('fetch', e => {
  if (e.request.method !== 'GET') return
  const url = new URL(e.request.url)

  // ── Skip rules (let the browser handle these natively) ──────────────
  // Cross-origin (CDN scripts, analytics, RUM)
  if (url.origin !== self.location.origin) return
  // Core API
  if (url.pathname.startsWith('/api')) return
  // Single-use sandboxed documents for artifact/widget iframes. Two reasons this
  // must never reach the handler below: the URL carries a one-shot credential the
  // gateway spends on first GET, so any SW-mediated re-fetch resolves to a 404
  // and the frame shows an error page; and an iframe navigation has
  // mode === 'navigate', so the offline fallback would serve the SPA shell
  // (/index.html) INTO the widget frame instead of the document.
  if (url.pathname.startsWith('/sandbox-doc/')) return
  // App backends (e.g. /apps/dev-fleet/api/*)
  if (url.pathname.startsWith('/apps/')) return
  // Vite content-hashed assets — the immutable HTTP cache still owns them, so
  // nothing is cached here. They are routed through the SW only so a 5xx can be
  // retried; see fetchAssetWithRetry for why one 502 here is a black screen.
  if (url.pathname.startsWith('/assets/')) {
    e.respondWith(fetchAssetWithRetry(e.request))
    return
  }
  // Vendor shims, fonts, sprites — stable filenames, no SW caching needed
  if (url.pathname.startsWith('/vendor/')) return
  if (url.pathname.startsWith('/fonts/')) return
  if (url.pathname.startsWith('/sprites/')) return
  // Backend-served brand assets: the sidebar logo + favicon (/logo.png) and the
  // legacy /static/ tree. These are NOT in the shell cache, so the network-first
  // fallback below would resolve them to Response.error() on any transient fetch
  // failure (e.g. a gateway restart/redeploy while a tab is open) and strand them
  // as a broken image until the tab reloads. Let the browser fetch them natively.
  if (url.pathname === '/logo.png') return
  if (url.pathname.startsWith('/static/')) return

  // ── Shell navigation: network-first, fall back to cached shell ──────
  e.respondWith(
    fetch(e.request).then(resp => {
      // Refresh the cached shell on every successful navigation TO THE SHELL.
      // The shell cache was previously written only at install time, so the
      // offline fallback could serve a shell from an arbitrarily old deploy: with
      // a stable CACHE_VERSION across redeploys, one flaky navigation (a phone
      // waking on a tunnel) silently booted a days-old bundle whose hashed assets
      // still lived in the HTTP cache — a complete time capsule that fresh
      // deploys never invalidated.
      //
      // The path check is load-bearing, not defensive. `mode === 'navigate'` is
      // true for EVERY top-level document, including the standalone app-window
      // documents this dashboard opens, so keying on the mode alone stored one of
      // those under `/` and `/index.html` — after which an offline dashboard
      // navigation booted an app window instead of the SPA.
      const url = new URL(e.request.url)
      const isShellPath = url.pathname === '/' || url.pathname === '/index.html'
      if (e.request.mode === 'navigate' && resp.ok && isShellPath) {
        const copy = resp.clone()
        e.waitUntil(caches.open(CACHE).then(c => Promise.all([
          c.put('/', copy.clone()), c.put('/index.html', copy),
        ])).catch(() => {}))
      }
      return resp
    }).catch(() => {
      // Network failed — serve cached shell for navigation requests so
      // the SPA can boot and show an offline/reconnecting state.
      // For non-navigation requests (sub-resources), return a proper
      // network error rather than undefined (which is an illegal
      // respondWith argument that crashes the request).
      if (e.request.mode === 'navigate') {
        return caches.match('/index.html').then(r => r || Response.error())
      }
      return caches.match(e.request).then(r => r || Response.error())
    })
  )
})
