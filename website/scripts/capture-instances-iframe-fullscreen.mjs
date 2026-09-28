#!/usr/bin/env node
// Captures before/after evidence for the InstancesViewport iframe fullscreen
// delegation fix. Serves the pane page on one loopback port and the parent on
// another (cross-origin, mirroring the tunnel-iframe topology), then
// screenshots the pane's <video controls> under both allow attribute sets and
// asserts document.fullscreenEnabled flips false -> true.
//
// Usage: node scripts/capture-instances-iframe-fullscreen.mjs <outDir> <videoFile>
import { chromium } from 'playwright'
import http from 'node:http'
import { readFile } from 'node:fs/promises'
import { mkdirSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const outDir = process.argv[2] || '../temp-screenshots/instances-iframe-fullscreen'
const videoFile = process.argv[3]
const captureDir = path.join(path.dirname(fileURLToPath(import.meta.url)), '..', 'capture')

function serve(routes) {
  return new Promise(resolve => {
    const srv = http.createServer(async (req, res) => {
      const route = routes[req.url.split('?')[0]]
      if (!route) { res.writeHead(404); res.end(); return }
      try {
        const body = await readFile(route.file)
        res.writeHead(200, { 'Content-Type': route.type })
        res.end(body)
      } catch { res.writeHead(500); res.end() }
    })
    srv.listen(0, '127.0.0.1', () => resolve(srv))
  })
}

const paneSrv = await serve({
  '/instances-iframe-fullscreen-pane.html': { file: path.join(captureDir, 'instances-iframe-fullscreen-pane.html'), type: 'text/html' },
  '/test-video.webm': { file: videoFile, type: 'video/webm' },
})
const parentSrv = await serve({
  '/parent.html': { file: path.join(captureDir, 'instances-iframe-fullscreen-parent.html'), type: 'text/html' },
})
const paneOrigin = `http://127.0.0.1:${paneSrv.address().port}`
const parentOrigin = `http://127.0.0.1:${parentSrv.address().port}`

mkdirSync(outDir, { recursive: true })
const browser = await chromium.launch()
const page = await browser.newPage({ viewport: { width: 600, height: 420 }, deviceScaleFactor: 1 })

const results = {}
for (const mode of ['before', 'after']) {
  await page.goto(`${parentOrigin}/parent.html?mode=${mode}&pane=${encodeURIComponent(paneOrigin)}`)
  const pane = page.frameLocator('#pane')
  await pane.locator('video').waitFor()
  // Hover the video so Chromium's native controls (incl. the fullscreen
  // button) are fully visible in the capture, then let the fade settle.
  await pane.locator('video').hover()
  await page.waitForTimeout(600)
  const stateText = await pane.locator('#state').textContent()
  results[mode] = stateText.trim()
  await page.screenshot({ path: path.join(outDir, `${mode}.png`) })
  console.log(`${mode}: ${stateText.trim()}`)
}

await browser.close()
paneSrv.close(); parentSrv.close()

// Self-check: the delegation must actually flip the pane's capability.
if (!results.before.endsWith('false')) throw new Error(`expected BEFORE fullscreenEnabled=false, got: ${results.before}`)
if (!results.after.endsWith('true')) throw new Error(`expected AFTER fullscreenEnabled=true, got: ${results.after}`)
console.log('OK: fullscreenEnabled false -> true across the delegation change')
