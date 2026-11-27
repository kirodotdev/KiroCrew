// Counts the sandbox documents one reply's widget mints while the smooth reveal
// drains after the turn ends (capture/assistant-widget-drain.tsx), and shoots the
// widget once the reveal has caught up. Every /api route is stubbed; the
// sandbox-doc stash is modelled at the gateway's 64 entries, freed only by a
// serve. `latencyMs` delays each mint and serve, as a remote link would.
//   node capture/shoot-assistant-widget-drain.mjs <port> <out.json> <out.png> [latencyMs]
import { createServer } from 'vite'
import { chromium } from 'playwright-core'
import { writeFileSync } from 'node:fs'

const [PORT = '5181', OUT_JSON = 'widget-drain.json', OUT_PNG = 'widget-drain.png', LATENCY = '0'] = process.argv.slice(2)
const STASH_ENTRIES = 64
const lag = () => new Promise((resolve) => setTimeout(resolve, Number(LATENCY)))

const server = await createServer({ configFile: 'vite.config.ts', server: { port: Number(PORT), strictPort: true, host: '127.0.0.1' } })
await server.listen()
const browser = await chromium.launch({ executablePath: process.env.CHROMIUM_PATH, args: ['--no-sandbox'] })
const page = await (await browser.newContext({ viewport: { width: 1280, height: 900 } })).newPage()

let mints = 0, refused = 0, serves = 0
const bodies = new Set()
const probes = new Set()
const docs = new Map()
await page.route((u) => u.pathname.startsWith('/api/'), async (route) => {
  const path = new URL(route.request().url()).pathname
  if (path === '/api/sandbox-doc' && route.request().method() === 'POST') {
    await lag()
    const html = JSON.parse(route.request().postData() || '{}').html || ''
    mints++
    bodies.add(html)
    if (docs.size >= STASH_ENTRIES) {
      refused++
      return route.fulfill({ status: 503, json: { error: 'too many documents in flight', code: 'stash_full' } })
    }
    const url = `/api/sandbox-doc/d${mints}`
    docs.set(url, html)
    return route.fulfill({ json: { url } })
  }
  if (path.startsWith('/api/sandbox-doc/')) {
    await lag()
    serves++
    const html = docs.get(path) || ''
    docs.delete(path) // single use, as on the gateway
    return route.fulfill({ contentType: 'text/html', body: html })
  }
  if (path.startsWith('/api/artifacts/')) {
    probes.add(path)
    return route.fulfill({ status: 404, json: { error: 'not found' } })
  }
  return route.fulfill({ json: [] })
})

const errors = []
page.on('pageerror', (e) => errors.push(e.message.slice(0, 160)))
await page.goto(`http://127.0.0.1:${PORT}/capture/assistant-widget-drain.html`)
// The closing sentence follows the widget, so it shows only once the reveal has
// drawn the whole widget body. Then the widget settles: its frame loads, or its
// last mint was refused and it says so.
await page.getByText('That is the whole change.').waitFor({ timeout: 120000 })
await page.waitForFunction(() => {
  const frame = document.querySelector('iframe')
  return (frame && frame.style.opacity === '1') || document.body.innerText.includes("Couldn't render this widget")
}, null, { timeout: 60000 })
await page.screenshot({ path: OUT_PNG })
const out = { mints, distinctBodies: bodies.size, stash_full_503: refused, serves, artifactProbes: probes.size, iframes: await page.locator('iframe').count(), errors: errors.slice(0, 4) }
writeFileSync(OUT_JSON, JSON.stringify(out, null, 2) + '\n')
console.log(JSON.stringify(out))
await browser.close()
await server.close()
