/** Real-browser evidence for #22367 on the REAL components.
 *
 * Drives website/capture/dashboard-card-send-live.html. For the Dashboard states
 * the script clicks the page's own button INSIDE the frame (a real activation tied
 * to the frame, so the host's send gates run), then asserts the branch before each
 * shot:
 *   - sent:    the thread holds the `[Dashboard card]` row, no notice, chat box empty;
 *   - refused: the send-failed notice with its dismiss control, the reply in the chat box;
 *   - unknown: the "may not have been sent" notice, the chat box left empty.
 * profile-error clicks the real Profile switch with the write answering 500.
 *
 *   node scripts/capture-22367-card-send-live.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { createServer } from 'vite'

const OUT = process.argv[2] || '../temp-screenshots/22367'
mkdirSync(OUT, { recursive: true })

const server = await createServer({
  configFile: 'vite.config.ts',
  server: { host: '127.0.0.1', port: 0, strictPort: false },
  logLevel: 'warn',
})
await server.listen()
const BASE = `http://127.0.0.1:${server.httpServer.address().port}`

const { LD_LIBRARY_PATH: _mise, ...browserEnv } = process.env
const browser = await chromium.launch({ env: browserEnv })
let failures = 0
const check = (label, ok) => {
  console.log(`${label} => ${ok ? 'OK' : 'FAIL'}`)
  if (!ok) failures++
}

async function open(theme, state, height = 440) {
  const page = await browser.newPage({ viewport: { width: 520, height }, deviceScaleFactor: 2 })
  page.on('pageerror', (e) => { console.error('pageerror:', e.message); failures++ })
  await page.goto(`${BASE}/capture/dashboard-card-send-live.html?theme=${theme}&state=${state}`, { waitUntil: 'domcontentloaded' })
  await page.waitForSelector('[data-capture-root]', { timeout: 20000 })
  return page
}

async function clickAnswer(page) {
  const frameEl = page.locator('[data-testid="crew-dashboard-iframe"]')
  await frameEl.waitFor({ state: 'visible', timeout: 20000 })
  const btn = page.frameLocator('[data-testid="crew-dashboard-iframe"]').locator('#answer')
  await btn.waitFor({ state: 'visible', timeout: 20000 })
  // A person's path into the page: the dashboard window holds focus first (so the
  // move into the frame is a real blur the host records), and no click in the
  // dashboard is still live when they click the card (the host's own gate).
  await page.locator('[data-testid="fixture-chat"]').click({ position: { x: 4, y: 4 } })
  await page.waitForTimeout(5600)
  await btn.click()
}

const box = (page) => page.locator('[data-testid="fixture-chat-box"]').inputValue()

for (const [theme, state, name] of [
  ['dark', 'off', '4-dark-switch-off-click-fills-chat-box'],
  ['dark', 'sent', '5-dark-card-before-send'],
  ['dark', 'sent', '6-dark-card-sent-one-click'],
  ['dark', 'refused', '7-dark-send-refused-reply-in-chat-box'],
  ['dark', 'unknown', '8-dark-send-unsure'],
  ['light', 'refused', '10-light-send-refused-reply-in-chat-box'],
]) {
  const page = await open(theme, state)
  if (name.includes('before')) {
    await page.locator('[data-testid="crew-dashboard-iframe"]').waitFor({ state: 'visible', timeout: 20000 })
    await page.frameLocator('[data-testid="crew-dashboard-iframe"]').locator('#answer').waitFor({ state: 'visible', timeout: 20000 })
    check(`${name}: nothing sent yet`, (await page.locator('[data-testid="fixture-thread"]').innerText()).includes('nothing sent'))
  } else {
    await clickAnswer(page)
    const badges = await page.locator('[data-testid="crew-dashboard-card-send-badge"]').count()
    check(`${name}: one-click badge ${state === 'off' ? 'absent' : 'shown'}`, state === 'off' ? badges === 0 : badges === 1)
    if (state === 'off') {
      await page.waitForTimeout(500)
      check(`${name}: reply in chat box`, (await box(page)).includes('Approve the plan'))
      check(`${name}: nothing sent`, (await page.locator('[data-testid="fixture-thread"]').innerText()).includes('nothing sent'))
      check(`${name}: no notice`, (await page.locator('[data-testid="crew-dashboard-send-error"], [data-testid="crew-dashboard-send-unsure"]').count()) === 0)
    } else if (state === 'sent') {
      await page.locator('[data-testid="fixture-thread"]', { hasText: '[Dashboard card]' }).waitFor({ timeout: 10000 })
      check(`${name}: no notice`, (await page.locator('[data-testid="crew-dashboard-send-error"], [data-testid="crew-dashboard-send-unsure"]').count()) === 0)
      check(`${name}: chat box empty`, (await box(page)) === '')
    } else if (state === 'refused') {
      await page.locator('[data-testid="crew-dashboard-send-error"]').waitFor({ state: 'visible', timeout: 10000 })
      check(`${name}: reply in chat box`, (await box(page)).includes('Approve the plan'))
      check(`${name}: notice has dismiss`, (await page.locator('[data-testid="crew-dashboard-send-error"] button').count()) >= 1)
      // The dismiss button must not sit under the floating Download control.
      const x = await page.locator('[data-testid="crew-dashboard-send-error"] button').last().boundingBox()
      const dl = await page.locator('[data-testid="crew-dashboard-frame"] > div.absolute').first().boundingBox()
      check(`${name}: dismiss clear of Download`, !!x && !!dl && x.x + x.width <= dl.x)
    } else {
      await page.locator('[data-testid="crew-dashboard-send-unsure"]').waitFor({ state: 'visible', timeout: 10000 })
      check(`${name}: chat box left empty`, (await box(page)) === '')
      check(`${name}: notice quotes the reply`, (await page.locator('[data-testid="crew-dashboard-send-unsure"]').innerText()).includes('Approve the plan'))
    }
  }
  await page.mouse.move(0, 0)
  await page.waitForTimeout(300)
  await page.screenshot({ path: `${OUT}/${name}.png` })
  console.log('shot', name)
  await page.close()
}

{
  const page = await open('dark', 'profile-error', 640)
  const sw = page.locator('[data-testid="crew-profile-dashboard-card-send"] [role="switch"], [data-testid="crew-profile-dashboard-card-send"] button').first()
  await sw.waitFor({ state: 'visible', timeout: 15000 })
  await sw.click()
  await page.locator('[data-testid="crew-profile-dashboard-card-send-error"]').waitFor({ state: 'visible', timeout: 10000 })
  const notice = page.locator('[data-testid="crew-profile-dashboard-card-send-error"]')
  const vh = page.viewportSize().height
  const nb = await notice.boundingBox()
  check('profile-error: notice inside the shot', !!nb && nb.y + nb.height <= vh)
  await page.waitForTimeout(300)
  await page.screenshot({ path: `${OUT}/9-dark-profile-setting-write-failed.png` })
  console.log('shot 9-dark-profile-setting-write-failed')
  await page.close()
}

await browser.close()
await server.close()
if (failures) {
  console.error(`${failures} assertion(s) failed`)
  process.exit(1)
}
console.log(`done - evidence in ${OUT}/`)
