/**
 * The list-view scene the folder-collapse recording harnesses share: two
 * folders, "alpha folder" and "beta folder", of 40 sessions each, with every
 * /api/** call answered from these fixtures (no gateway). Scrolled a few
 * hundred px into alpha, its header is pinned and beta's rows sit well below
 * the lane, which is the shape both the pinned-header hold
 * (`capture-sticky-folder-collapse.mjs`) and the rising rows
 * (`capture-collapse-rising-rows.mjs`) are about.
 */
import { handleBootRoute, json, makeFixedApi } from './boot-api.mjs'

const iso = minutesAgo => new Date(Date.now() - minutesAgo * 60_000).toISOString()

/**
 * Build the fixtures and bind them to `page`'s /api/** routes. The folders
 * array is live: a PATCH keeps what it was sent, after a realistic round trip.
 * @param {import('playwright').Page} page
 * @param {{ theme?: 'dark' | 'light' }} [opts]
 */
export async function routeFolderCollapseScene(page, { theme = 'dark' } = {}) {
  const folders = [
    { id: 'f-a', name: 'alpha folder', order: 0, collapsed: false },
    { id: 'f-b', name: 'beta folder', order: 1, collapsed: false },
  ]
  const slots = ['a', 'b'].flatMap((f, fi) => Array.from({ length: 40 }, (_, i) => ({
    key: `${f}-${i}`, title: `${f === 'a' ? 'alpha' : 'beta'} session ${i}`, agent: 'kirocrew',
    running: false, messages: 3, folder_id: `f-${f}`, last_ts: iso(fi * 100 + i), created: iso(1000 + i),
  })))
  const bootApi = makeFixedApi('/tmp/demo').set('/api/status', {
    sessions: slots.length, crons: 0, lessons: 0, uptime: 120, version: 'dev',
  })
  await page.routeWebSocket(/\/api\/ws/, () => {})
  await page.route(url => url.pathname.startsWith('/api/'), async route => {
    const req = route.request()
    const path = new URL(req.url()).pathname
    const m = path.match(/^\/api\/chat\/folders\/([^/]+)$/)
    if (m && req.method() === 'PATCH') {
      const body = req.postDataJSON() || {}
      const f = folders.find(x => x.id === decodeURIComponent(m[1]))
      if (f) Object.assign(f, body)
      await new Promise(r => setTimeout(r, 120))
      return json(route, f || {})
    }
    if (path === '/api/chat/folders') return json(route, folders)
    if (path === '/api/chat/slots') return json(route, slots)
    if (path === '/api/chat/tags' || path === '/api/chat/tag-columns') return json(route, [])
    if (path.startsWith('/api/apps')) return json(route, { apps: [], installed: [] })
    return handleBootRoute(route, path, { project: '/tmp/demo', theme, fixedApi: bootApi })
  })
  return { folders, slots }
}
