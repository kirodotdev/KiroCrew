/**
 * Screenshots of a parent message after a thread reply arrives live (#33306).
 *
 * Drives website/capture/channel-live-reply-count.html, which mounts the REAL
 * ChannelPage. Each frame first asserts there is no reply button, then sends
 * one reply in the parent's thread through the page's socket event, then
 * captures. `--expect fixed` asserts the parent now shows "1 reply";
 * `--expect broken` asserts it still shows none (the build before the fix).
 *
 * Usage, against a dev server or a built directory (served through request
 * routing, so no listener is needed):
 *   npx vite --host 127.0.0.1 --port 6871 --strictPort   # in another shell
 *   node scripts/capture-channel-live-reply-count.mjs http://127.0.0.1:6871 ../temp-screenshots/33306 after fixed
 *   node scripts/capture-channel-live-reply-count.mjs <built dir> ../temp-screenshots/33306 after fixed
 */
import { chromium } from 'playwright'
import { existsSync, mkdirSync, readFileSync, statSync } from 'node:fs'
import { extname, join, normalize } from 'node:path'

const SOURCE = process.argv[2] || 'http://127.0.0.1:6871'
const SERVE_DIR = existsSync(SOURCE) && statSync(SOURCE).isDirectory() ? SOURCE : null
const BASE = SERVE_DIR ? 'http://capture.invalid' : SOURCE
const OUT = process.argv[3] || '../temp-screenshots/33306'
const LABEL = process.argv[4] || 'after'
const EXPECT = process.argv[5] || 'fixed'
mkdirSync(OUT, { recursive: true })

const REPLY = {
  id: 'r1', from_id: 'a1', from_role: 'Researcher', thread_id: 'p1', reply_to: 'human',
  content: 'The health check timed out on two hosts.', msg_type: 'progress',
  timestamp: 1_790_000_060, reply_count: 0,
}

const browser = await chromium.launch()
const page = await browser.newPage({ viewport: { width: 1100, height: 420 }, deviceScaleFactor: 2 })
if (SERVE_DIR) {
  const TYPES = { '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css', '.svg': 'image/svg+xml', '.woff2': 'font/woff2', '.json': 'application/json' }
  await page.route(`${BASE}/**`, route => {
    const path = normalize(decodeURIComponent(new URL(route.request().url()).pathname)).replace(/^([/\\])+/, '')
    const file = join(SERVE_DIR, path)
    if (!file.startsWith(normalize(SERVE_DIR)) || !existsSync(file) || statSync(file).isDirectory()) return route.fulfill({ status: 404, body: '' })
    return route.fulfill({ status: 200, contentType: TYPES[extname(file)] || 'application/octet-stream', body: readFileSync(file) })
  })
}
let failed = false
for (const theme of ['dark', 'light']) {
  const name = `channel-live-reply-${LABEL}-${theme}`
  await page.goto(`${BASE}/capture/channel-live-reply-count.html?theme=${theme}`)
  await page.getByText('Why did the gamma deploy fail?').waitFor()
  const replyButton = page.getByRole('button', { name: /^\d+ repl(y|ies)$/ })
  if (await replyButton.count()) { console.log(`${name}: a reply button before any reply`); failed = true; continue }

  await page.evaluate(message => {
    window.dispatchEvent(new CustomEvent('kirocrew-channel', {
      detail: { type: 'channel_message', data: { channel_id: 'ch1', message } },
    }))
  }, REPLY)

  if (EXPECT === 'fixed') {
    await page.getByRole('button', { name: '1 reply' }).waitFor()
  } else {
    // Give the event every chance to render before asserting it changed nothing.
    await page.waitForFunction(() => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(() => r(true)))))
    if (await replyButton.count()) { console.log(`${name}: expected no reply button`); failed = true; continue }
  }
  await page.locator('[data-capture-root]').screenshot({ path: `${OUT}/${name}.png` })
  console.log(`${name}: ok`)
}
await browser.close()
if (failed) process.exit(1)
