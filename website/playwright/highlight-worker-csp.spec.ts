import { test, expect, type Page } from '@playwright/test'
import { createServer } from 'node:http'
import { readFile, readdir } from 'node:fs/promises'
import path from 'node:path'

// No gateway state or request routing: Playwright routing disables the HTTP
// cache, which is the bug this test must exercise. The production build must
// exist, as it does for the shared E2E gate.
test.use({ storageState: { cookies: [], origins: [] } })

type WorkerReply = { type?: string; requestType?: string; error?: string }

async function initializeWorker(page: Page, url: string): Promise<WorkerReply> {
  return page.evaluate(url => new Promise<WorkerReply>(resolve => {
    const worker = new Worker(url, { type: 'module' })
    const finish = (reply: WorkerReply) => {
      clearTimeout(timer)
      worker.terminate()
      resolve(reply)
    }
    const timer = setTimeout(() => finish({ error: 'worker initialization timed out' }), 10_000)
    worker.addEventListener('error', event => finish({ error: event.message }))
    worker.addEventListener('message', event => finish(event.data))
    worker.postMessage({
      type: 'initialize', id: 'csp-upgrade-probe',
      preferredHighlighter: 'shiki-wasm',
      resolvedThemes: [], resolvedLanguages: [],
      renderOptions: { theme: 'github-dark' },
    })
  }), url)
}

test('highlight worker gets fresh CSP without clearing user data after an upgrade', async ({ page }) => {
  const assets = path.resolve('dist/assets')
  const names = await readdir(assets)
  const name = names.find(name => /^worker-portable-.*\.js$/.test(name))
  expect(name, 'build must emit the portable worker bundle').toBeTruthy()
  const bytes = await readFile(path.join(assets, name!))
  const oldPath = `/assets/${name}`
  const newPath = `${oldPath}?csp=wasm-v1`
  const hits: string[] = []
  let upgraded = false
  const server = createServer((request, response) => {
    const wasm = upgraded ? " 'wasm-unsafe-eval'" : ''
    response.setHeader('Content-Security-Policy',
      `default-src 'self'; script-src 'self' 'unsafe-inline'${wasm}; worker-src 'self';`)
    if (request.url === '/') {
      response.setHeader('Content-Type', 'text/html')
      response.setHeader('Cache-Control', 'no-store')
      response.end('<!doctype html><title>Worker CSP upgrade test</title>')
    } else if (request.url === oldPath || request.url === newPath) {
      hits.push(request.url)
      response.setHeader('Content-Type', 'text/javascript')
      response.setHeader('Cache-Control', 'public, max-age=31536000, immutable')
      // Identical bytes deliberately isolate the response-header cache key.
      response.end(bytes)
    } else {
      response.writeHead(404).end()
    }
  })
  await new Promise<void>(resolve => server.listen(0, '127.0.0.1', resolve))
  try {
    const address = server.address()
    if (!address || typeof address === 'string') throw new Error('missing loopback port')
    const origin = `http://127.0.0.1:${address.port}`
    await page.goto(origin)
    await page.evaluate(() => localStorage.setItem('draft-canary', 'keep this draft'))
    const before = await initializeWorker(page, oldPath)
    expect(before.type).toBe('error')
    expect(before.error).toMatch(/Content Security/i)

    upgraded = true
    await page.goto(origin)
    // A fresh document does not replace the cached worker response's CSP.
    const cached = await initializeWorker(page, oldPath)
    expect(cached.type).toBe('error')
    expect(cached.error).toMatch(/Content Security/i)
    expect(hits.filter(url => url === oldPath)).toHaveLength(1)

    const fresh = await initializeWorker(page, newPath)
    expect(fresh).toMatchObject({ type: 'success', requestType: 'initialize' })
    expect(hits.filter(url => url === newPath)).toHaveLength(1)
    expect(await page.evaluate(() => localStorage.getItem('draft-canary'))).toBe('keep this draft')
  } finally {
    await page.goto('about:blank')
    server.closeAllConnections()
    await new Promise<void>((resolve, reject) => server.close(error => error ? reject(error) : resolve()))
  }
})
