/**
 * Recording for #18313: after a PICK in a composer picker, focus is in the
 * composer, so Enter sends instead of re-opening the menu.
 *
 * Drives website/capture/picker-focus-return.html (the REAL ChatInput) in two
 * scenes and records each as a video, asserting the state the recording claims:
 *   approval  type, open the approval-mode picker, pick Trust, Enter -> "sent: …"
 *   busy      type, open the steer/queue caret, pick Queue, Enter -> a log line
 * A scene whose assertions fail is reported and its video is not kept, unless
 * KEEP_ON_FAIL=1 (used to record the pre-fix behaviour from a main checkout).
 *
 * Usage (starts its own in-process vite dev server on a loopback address and
 * shuts it down when done):
 *   node scripts/capture-picker-focus-return.mjs <out-dir>
 */
import { chromium } from 'playwright'
import { createServer } from 'vite'
import { mkdirSync, renameSync } from 'node:fs'
import { join } from 'node:path'

const OUT = process.argv[2] || '../temp-screenshots/picker-focus-return'
mkdirSync(OUT, { recursive: true })

// Loopback only; an OS-assigned ephemeral address so two runs never collide.
const vite = await createServer({ server: { host: '127.0.0.1', port: 0, strictPort: false }, logLevel: 'error' })
await vite.listen()
const BASE = vite.resolvedUrls.local[0].replace(/\/$/, '')

const browser = await chromium.launch()
let failed = false

function check(name, ok, detail) {
  console.log(`${name}: ${ok ? 'OK' : 'MISMATCH'} ${detail}`)
  if (!ok) failed = true
  return ok
}

const TEXT = 'Deploy to staging'
const activeIsComposer = page => page.evaluate(() => {
  const a = document.activeElement
  return !!a && a.hasAttribute('data-composer-input')
})

for (const scene of ['approval', 'busy']) {
  const ctx = await browser.newContext({
    viewport: { width: 720, height: 420 },
    deviceScaleFactor: 2,
    recordVideo: { dir: OUT, size: { width: 720, height: 420 } },
  })
  const page = await ctx.newPage()
  // Gateway-free: answer every REAL API call the mounted ChatInput makes.
  await page.route(u => new URL(u).pathname.startsWith('/api/'), route => {
    const path = new URL(route.request().url()).pathname
    const isList = /commands|skills|agents|sessions|files|history|models/.test(path)
    return route.fulfill({ status: 200, contentType: 'application/json', body: isList ? '[]' : '{}' })
  })
  await page.goto(`${BASE}/capture/picker-focus-return.html?theme=dark&scene=${scene}`)
  await page.waitForSelector('[data-capture-root]')
  const composer = page.locator('[data-composer-input]')
  await composer.waitFor()
  await page.waitForTimeout(600)

  await composer.click()
  await page.keyboard.type(TEXT, { delay: 45 })
  await page.waitForTimeout(500)

  if (scene === 'approval') {
    await page.getByLabel('Approval mode: Normal').click()
    await page.getByRole('menuitem', { name: /Trust/ }).first().waitFor()
    await page.waitForTimeout(700)
    await page.getByRole('menuitem', { name: /^Trust\b/ }).first().click()
  } else {
    await page.getByTestId('busy-send-caret').click()
    await page.getByTestId('busy-send-mode-queue').waitFor()
    await page.waitForTimeout(700)
    await page.getByTestId('busy-send-mode-queue').click()
  }
  await page.waitForTimeout(500)
  const focused = await activeIsComposer(page)
  const menuGone = (await page.getByRole('menu').count()) === 0
  check(`${scene}-focus-after-pick`, focused && menuGone, `composerFocused=${focused} menuOpen=${!menuGone}`)

  await page.waitForTimeout(400)
  await page.keyboard.press('Enter')
  await page.waitForTimeout(600)
  const logs = await page.locator('[data-capture-log]').allTextContents()
  const stillClosed = (await page.getByRole('menu').count()) === 0
  const left = logs.some(l => l.endsWith(`: ${TEXT}`))
  check(`${scene}-enter-sends`, left && stillClosed, `log=${JSON.stringify(logs)} menuOpen=${!stillClosed}`)
  await page.waitForTimeout(900)

  const video = page.video()
  await ctx.close()
  const raw = await video.path()
  // KEEP_ON_FAIL=1 keeps a mismatching scene's video too -- that is how the
  // "before" recording is produced from a checkout without the fix.
  if (!failed || process.env.KEEP_ON_FAIL === '1') renameSync(raw, join(OUT, `${scene}.webm`))
}

await browser.close()
await vite.close()
if (failed) { console.error('capture failed: at least one assertion mismatched'); process.exit(1) }
