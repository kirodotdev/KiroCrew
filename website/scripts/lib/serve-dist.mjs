/**
 * Shared static-file server for the screenshot harnesses in this folder.
 *
 * Every harness runs the REAL built SPA (website/dist) behind a tiny in-process
 * server with index.html fallback, so deep links like /settings resolve, and
 * binds to 127.0.0.1 on an ephemeral port. Keeping it here means one copy of the
 * MIME table and the fallback rule instead of one per capture script.
 */
import { readFileSync, existsSync, statSync } from 'node:fs'
import { createServer } from 'node:http'
import { join, extname } from 'node:path'
import { fileURLToPath } from 'node:url'

// fileURLToPath, not URL.pathname: on Windows .pathname yields "/C:/…", which
// join() then turns into an invalid "\C:\…" and every read fails with ENOENT.
export const DEFAULT_DIST = fileURLToPath(new URL('../../dist/', import.meta.url))

export const MIME = {
  '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css',
  '.json': 'application/json', '.svg': 'image/svg+xml', '.png': 'image/png',
  '.woff2': 'font/woff2', '.woff': 'font/woff', '.ico': 'image/x-icon',
}

/**
 * Serve `dist` on a loopback ephemeral port with index.html fallback.
 * @returns {Promise<{srv: import('node:http').Server, base: string}>}
 */
export function serveDist(dist = DEFAULT_DIST) {
  return new Promise(resolve => {
    const srv = createServer((req, res) => {
      // new URL() normalizes away ".." segments before the join, so a crafted
      // request cannot escape dist.
      const rel = decodeURIComponent(new URL(req.url, 'http://x').pathname).replace(/^\/+/, '')
      let file = join(dist, rel)
      if (!rel || !existsSync(file) || statSync(file).isDirectory()) file = join(dist, 'index.html')
      res.writeHead(200, { 'Content-Type': MIME[extname(file)] || 'application/octet-stream' })
      res.end(readFileSync(file))
    })
    srv.listen(0, '127.0.0.1', () => resolve({ srv, base: `http://127.0.0.1:${srv.address().port}` }))
  })
}
