/**
 * Screenshot harness for media referenced with markdown image syntax.
 *
 * Serves capture/markdown-media.html from a programmatic Vite dev server on
 * loopback and answers the file endpoints the way the backend does:
 * - /api/file-raw serves the PNG and refuses anything else with 403
 *   ("file content is not a recognized format"), as it does for an MP4;
 * - /api/file-stream streams capture/assets/placeholder.mp4 (as WebM, see
 *   below) for demo.mp4 and 404s every other path.
 *
 * Usage (from website/): node capture/shoot-markdown-media.mjs [outDir] [prefix]
 */
import { chromium } from 'playwright'
import { createServer } from 'vite'
import { spawnSync } from 'node:child_process'
import { mkdirSync, readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join, resolve } from 'node:path'
import { chromiumExecutable } from '../scripts/lib/chromium-executable.mjs'

const here = dirname(fileURLToPath(import.meta.url))
const OUT = process.argv[2] || resolve(here, '../../temp-screenshots/markdown-media')
const PREFIX = process.argv[3] || 'after'
mkdirSync(OUT, { recursive: true })

const PNG = readFileSync(resolve(here, '../public/icon-192.png'))
// Playwright's Chromium ships without H.264, so the placeholder MP4 would show
// as a decode failure. Transcode it to VP9 WebM once (ffmpeg on PATH) and
// serve those bytes for demo.mp4; the renderer only reads the extension.
const webm = join(OUT, 'placeholder.webm')
const ff = spawnSync('ffmpeg', ['-y', '-v', 'error', '-i', resolve(here, 'assets/placeholder.mp4'), '-c:v', 'libvpx-vp9', '-an', webm])
if (ff.status !== 0) throw new Error(`ffmpeg failed: ${ff.stderr}`)
const CLIP = readFileSync(webm)

const server = await createServer({
  configFile: './vite.config.ts',
  server: { host: '127.0.0.1', port: 5199, strictPort: false },
  logLevel: 'warn',
})
await server.listen()
const base = server.resolvedUrls?.local?.[0]?.replace(/\/$/, '') || `http://127.0.0.1:${server.config.server.port}`

const browser = await chromium.launch({
  executablePath: chromiumExecutable(),
  env: { ...process.env, LD_LIBRARY_PATH: '' },
})
const page = await browser.newPage({ viewport: { width: 760, height: 1100 }, deviceScaleFactor: 2 })
page.on('pageerror', e => console.log('PAGE EXCEPTION:', e.message))
await page.route('**/api/file-raw*', route => {
  const path = new URL(route.request().url()).searchParams.get('path') || ''
  const png = path.endsWith('.png')
  console.log(`file-raw ${png ? 200 : 403} ${path}`)
  return png
    ? route.fulfill({ status: 200, contentType: 'image/png', body: PNG })
    : route.fulfill({ status: 403, contentType: 'application/json', body: '{"error":"file content is not a recognized format"}' })
})
await page.route('**/api/file-stream*', route => {
  const path = new URL(route.request().url()).searchParams.get('path') || ''
  const hit = path.endsWith('/demo.mp4')
  console.log(`file-stream ${hit ? 200 : 404} ${path}`)
  return hit
    ? route.fulfill({ status: 200, contentType: 'video/webm', body: CLIP })
    : route.fulfill({ status: 404, contentType: 'application/json', body: '{"error":"not found"}' })
})

for (const theme of ['dark', 'light']) {
  await page.goto(`${base}/capture/markdown-media.html?theme=${theme}`)
  await page.waitForFunction(() => document.body.textContent?.includes('A clip hosted elsewhere'))
  // Settle: images decoded, the video's first frame (if any) ready, chips shown.
  await page.waitForFunction(() => {
    const imgs = [...document.querySelectorAll('img')]
    const vids = [...document.querySelectorAll('video')]
    return imgs.every(i => i.complete) && vids.every(v => v.readyState >= 2)
  })
  await page.waitForTimeout(1500)
  const shot = join(OUT, `${PREFIX}-${theme}.png`)
  await page.locator('#root > div').screenshot({ path: shot })
  console.log('wrote', shot)
}

await browser.close()
await server.close()
