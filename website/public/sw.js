// Minimal service worker for PWA installability.
// Network-first: always fetch from server, fall back to cache for offline shell.
// Cache version updated at build time — forces re-cache on deploy.

const CACHE = 'kirocrew-' + Date.now()
const SHELL = ['/', '/index.html']

self.addEventListener('install', e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL)))
  self.skipWaiting()
})

self.addEventListener('activate', e => {
  // Delete all old caches
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
  // Skip cross-origin requests (CDN scripts, analytics, etc.)
  if (url.origin !== self.location.origin) return
  // Skip API/WebSocket requests
  if (url.pathname.startsWith('/api')) return
  // Skip Vite hashed assets — they have unique filenames already
  if (url.pathname.startsWith('/assets/')) return

  e.respondWith(
    fetch(e.request)
      .then(r => {
        const clone = r.clone()
        caches.open(CACHE).then(c => c.put(e.request, clone))
        return r
      })
      .catch(() => caches.match(e.request))
  )
})
