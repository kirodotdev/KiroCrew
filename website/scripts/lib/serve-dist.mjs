/**
 * Serve a built SPA directory over an ephemeral loopback port.
 *
 * A capture harness drives the REAL built bundle rather than a dev server, so it needs
 * something to serve `website/dist` and to route unknown paths back to `index.html` for
 * the client-side router. Every harness needed the same twenty lines to do it, which is
 * a clone the copy/paste gate refuses at its zero threshold, so it lives here once.
 */
import { readFileSync, existsSync, statSync } from 'node:fs'
import { createServer } from 'node:http'
import { join, extname } from 'node:path'

const MIME = {
  '.html': 'text/html',
  '.js': 'text/javascript',
  '.css': 'text/css',
  '.json': 'application/json',
  '.svg': 'image/svg+xml',
  '.png': 'image/png',
  '.woff2': 'font/woff2',
  '.woff': 'font/woff',
  '.ico': 'image/x-icon',
}

export function serveDist(dist) {
  return new Promise(resolve => {
    const srv = createServer((req, res) => {
      const rel = decodeURIComponent(new URL(req.url, 'http://x').pathname).replace(/^\/+/, '')
      let file = join(dist, rel)
      if (!rel || !existsSync(file) || statSync(file).isDirectory()) file = join(dist, 'index.html')
      res.writeHead(200, { 'Content-Type': MIME[extname(file)] || 'application/octet-stream' })
      res.end(readFileSync(file))
    })
    srv.listen(0, '127.0.0.1', () => resolve({ srv, base: `http://127.0.0.1:${srv.address().port}` }))
  })
}
