// Minimal service worker for PWA installability.
// Network-first for the SPA shell; everything else goes straight to network.
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

self.addEventListener('fetch', e => {
  if (e.request.method !== 'GET') return
  const url = new URL(e.request.url)

  // ── Skip rules (let the browser handle these natively) ──────────────
  // Cross-origin (CDN scripts, analytics, RUM)
  if (url.origin !== self.location.origin) return
  // Core API
  if (url.pathname.startsWith('/api')) return
  // App backends (e.g. /apps/dev-fleet/api/*)
  if (url.pathname.startsWith('/apps/')) return
  // Vite content-hashed assets — immutable HTTP cache handles them
  if (url.pathname.startsWith('/assets/')) return
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
    fetch(e.request).catch(() => {
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
