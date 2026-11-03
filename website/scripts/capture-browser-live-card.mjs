/**
 * Screenshots of the live-browser card in each of its states (RFC: live browser
 * in chat, section 5), via the capture/browser-live-card harness. Each scene
 * asserts the card rendered the state it claims before shooting, so a frame
 * can never quietly show a different face.
 *
 * Usage: node scripts/capture-browser-live-card.mjs <viteBase> <outDir>
 */
import { mkdirSync } from 'node:fs'
import { chromium } from 'playwright'

import { chromiumExecutable } from './lib/chromium-executable.mjs'

const base = process.argv[2] || 'http://localhost:5199'
const out = process.argv[3] || '../temp-screenshots/browser-live-card'
mkdirSync(out, { recursive: true })

const SCENES = [
  ['allow', 'waiting_allow', 'dark'], ['live', 'live', 'dark'], ['hover', 'live', 'dark'], ['takeover', 'in_control', 'dark'],
  ['done', 'done', 'dark'], ['login', 'login_needed', 'dark'], ['error', 'error', 'dark'],
  ['live', 'live', 'light'], ['takeover', 'in_control', 'light'], ['done', 'done', 'light'], ['allow', 'waiting_allow', 'light'],
]

const b = await chromium.launch({ executablePath: chromiumExecutable() })
for (const [scene, state, theme] of SCENES) {
  const p = await (await b.newContext({ viewport: { width: 900, height: 640 }, deviceScaleFactor: 2 })).newPage()
  const problems = []
  p.on('pageerror', e => problems.push(String(e)))
  await p.goto(`${base}/capture/browser-live-card.html?scene=${scene}&theme=${theme}`, { waitUntil: 'networkidle' })
  await p.locator('html[data-capture-ready="1"]').waitFor({ timeout: 30_000 })
  const card = p.locator('[data-testid="browser-live-card"]')
  await card.waitFor({ timeout: 20_000 })
  const got = await card.getAttribute('data-state')
  if (got !== state) throw new Error(`scene=${scene} rendered state=${got}, expected ${state}`)
  if (scene !== 'allow') {
    // The painter must have drawn: a canvas sized to the frame, not the placeholder.
    const size = await p.locator('[data-testid="browser-live-canvas"]').evaluate(c => [c.width, c.height])
    if (size[0] !== 1280 || size[1] !== 800) throw new Error(`scene=${scene} canvas is ${size}, expected 1280x800`)
  }
  if (problems.length) throw new Error(`scene=${scene} page errors: ${problems.join('; ')}`)
  const file = `${out}/card-${scene}${theme === 'light' ? '-light' : ''}.png`
  await p.locator('[data-capture-root]').screenshot({ path: file })
  console.log(`captured ${file}`)
  await p.context().close()
}
await b.close()
