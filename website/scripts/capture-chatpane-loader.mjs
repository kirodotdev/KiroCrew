/**
 * Screenshots + animation frames for ChatPane's working indicator (the
 * ghost-pose carousel newly hosted by the pane).
 *
 * Drives the isolated capture entry (website/capture/chatpane-loader.html),
 * which mounts the REAL ChatPane. Every frame asserts its state before
 * writing, so a frame cannot document the wrong state:
 *   01-running-dark    tool_running: footer + carousel visible after the last message
 *   02-idle-dark       idle: no footer at all
 *   03-running-light   light-theme parity
 *   anim/f*.png        40-frame burst of the running state (assemble into a GIF
 *                      with ffmpeg; frames are not committed, the GIF is)
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6832 --strictPort   # in another shell
 *   node scripts/capture-chatpane-loader.mjs http://127.0.0.1:6832 ../temp-screenshots/chatpane-loader
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6832'
const OUT = process.argv[3] || '../temp-screenshots/chatpane-loader'
mkdirSync(OUT, { recursive: true })
mkdirSync(`${OUT}/anim`, { recursive: true })

const browser = await chromium.launch()
let failed = false

function check(name, ok, detail) {
  console.log(`${name}: ${ok ? 'OK' : 'MISMATCH'} ${detail}`)
  if (!ok) failed = true
  return ok
}

async function newPage(theme, state) {
  const page = await browser.newPage({ viewport: { width: 860, height: 640 }, deviceScaleFactor: 1 })
  // Gateway-free: answer every REAL API call the pane makes. Array-shaped
  // endpoints must answer [] ({} crashes their .map consumers).
  await page.route(u => new URL(u).pathname.startsWith('/api/'), route => {
    const path = new URL(route.request().url()).pathname
    const isList = /commands|skills|agents|sessions|files|history|models|artifacts|folders/.test(path)
    return route.fulfill({ status: 200, contentType: 'application/json', body: isList ? '[]' : '{}' })
  })
  await page.goto(`${BASE}/capture/chatpane-loader.html?theme=${theme}&state=${state}`)
  await page.waitForSelector('[data-capture-root]')
  await page.getByText('帮我看一下今晚新增的 issue').waitFor()
  return page
}

// 01 — running (dark): footer + carousel present, after the last message
{
  const page = await newPage('dark', 'tool_running')
  await page.getByTestId('loader-carousel').waitFor()
  const footer = await page.getByTestId('chat-footer').count()
  const slots = await page.locator('[data-testid="loader-carousel"] .slot').count()
  check('01-running footer', footer === 1, `footers=${footer}`)
  check('01-running slots', slots === 4, `slots=${slots} (4-ghost carousel)`)
  await page.waitForTimeout(600) // let the slide-up entrance settle before the still
  await page.screenshot({ path: `${OUT}/01-running-dark.png` })
  // Animation burst for the GIF (~1.8s, catches the cross-fade beat)
  for (let i = 0; i < 40; i++) {
    await page.screenshot({ path: `${OUT}/anim/f${String(i).padStart(3, '0')}.png` })
    await page.waitForTimeout(45)
  }
  await page.close()
}

// 02 — idle (dark): no footer at all
{
  const page = await newPage('dark', 'idle')
  const footer = await page.getByTestId('chat-footer').count()
  check('02-idle no footer', footer === 0, `footers=${footer}`)
  await page.screenshot({ path: `${OUT}/02-idle-dark.png` })
  await page.close()
}

// 03 — running (light): theme parity
{
  const page = await newPage('light', 'tool_running')
  await page.getByTestId('loader-carousel').waitFor()
  await page.waitForTimeout(600)
  await page.screenshot({ path: `${OUT}/03-running-light.png` })
  await page.close()
}

await browser.close()
if (failed) {
  console.error('CAPTURE FAILED: at least one frame did not match its asserted state')
  process.exit(1)
}
console.log('all frames verified')
