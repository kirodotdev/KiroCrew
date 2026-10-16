/**
 * Frames for two composer approval states that only a live run shows (#16389):
 *   22-composer-native-trust: a chat runner's own request in the composer
 *       approval bar, with Trust beside Allow once and Reject.
 *   23-composer-coordinator-no-trust: a coordinator approval parked in the
 *       chat, decided one-shot by its own target: no Trust and no extra
 *       line, as for any unattended source. Read against frame 22.
 * Each frame asserts its text and controls before it is written.
 *
 * Drives the isolated capture entry website/capture/composer-coordinator-trust.html,
 * which mounts the REAL ChatInput.
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6825 --strictPort   # in another shell
 *   node scripts/capture-coordinator-trust.mjs http://127.0.0.1:6825 ../temp-screenshots/notification-approval-refusal
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6825'
const OUT = process.argv[3] || '../temp-screenshots/notification-approval-refusal'
mkdirSync(OUT, { recursive: true })

const browser = await chromium.launch()
let failed = false

function check(name, ok, detail) {
  console.log(`${name}: ${ok ? 'OK' : 'MISMATCH'} ${JSON.stringify(detail)}`)
  if (!ok) failed = true
  return ok
}

// Gateway-free: answer every REAL API call the mounted page makes. Predicate
// on the pathname, so vite-served source modules under /src/api/ still load.
async function stub(page, approvals = []) {
  await page.route(u => new URL(u).pathname.startsWith('/api/'), route => {
    const path = new URL(route.request().url()).pathname
    if (path === '/api/approvals') return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(approvals) })
    const isList = /commands|skills|agents|sessions|files|history|models|tasks|runs/.test(path)
    return route.fulfill({ status: 200, contentType: 'application/json', body: isList ? '[]' : '{}' })
  })
}

for (const kind of ['native', 'coordinator']) {
  const page = await browser.newPage({ viewport: { width: 720, height: 360 }, deviceScaleFactor: 2 })
  await stub(page)
  await page.goto(`${BASE}/capture/composer-coordinator-trust.html?theme=dark&kind=${kind}`)
  await page.waitForSelector('[data-capture-root]')
  await page.getByText('Allow once').waitFor()
  await page.waitForTimeout(400)
  const s = {
    trust: await page.getByRole('button', { name: /^Trust/ }).count(),
    reason: (await page.getByTestId('approval-trust-unavailable').count()) ? await page.getByTestId('approval-trust-unavailable').innerText() : null,
  }
  const name = kind === 'native' ? '22-composer-native-trust' : '23-composer-coordinator-no-trust'
  const ok = kind === 'native' ? s.trust >= 1 && s.reason === null : s.trust === 0 && s.reason === null
  if (check(name, ok, s)) await page.screenshot({ path: `${OUT}/${name}.png` })
  await page.close()
}

await browser.close()
process.exit(failed ? 1 : 0)
