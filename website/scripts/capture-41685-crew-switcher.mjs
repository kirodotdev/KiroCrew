/**
 * Real-pod proof for #41685: the crew switcher's `+` / tabs / ▾ and the Remote
 * Crews window, driven end to end on a live gateway, including adding and
 * connecting a real SSH crew (a second pod reached over `ssh localhost`).
 *
 * Usage:
 *   POD_URL=http://127.0.0.1:<port> POD_TOKEN=<kirocrew pod token> \
 *   CREW_PORT=<other pod's port> CREW_BIN=<wrapper that runs the other pod's kirocrew> \
 *   MODE=after|before node scripts/capture-41685-crew-switcher.mjs <outDir>
 *
 * MODE=before drives main's UI (Settings › Remote Crew › Add Crew) through the same
 * add-and-connect, so the two recordings compare the flow itself.
 */
import { chromium } from 'playwright'
import { mkdirSync, renameSync } from 'node:fs'

const { POD_URL, POD_TOKEN, CREW_PORT, CREW_BIN } = process.env
const MODE = process.env.MODE || 'after'
const OUT = process.argv[2] || `../temp-screenshots/41685-${MODE}`
if (!POD_URL || !POD_TOKEN || !CREW_PORT || !CREW_BIN) {
  console.error('POD_URL, POD_TOKEN, CREW_PORT and CREW_BIN are required')
  process.exit(2)
}
mkdirSync(OUT, { recursive: true })
const VIEW = { width: 1280, height: 800 }

const browser = await chromium.launch()
const context = await browser.newContext({ viewport: VIEW, recordVideo: { dir: OUT, size: VIEW } })
const page = await context.newPage()
const pause = ms => page.waitForTimeout(ms)
let n = 0
const shot = async label => {
  n += 1
  const file = `${OUT}/${String(n).padStart(2, '0')}-${label}.png`
  await page.screenshot({ path: file })
  console.log('shot', file)
}
/** Move the pointer to the element first, so the recording shows each click. */
const go = async loc => {
  const el = typeof loc === 'string' ? page.locator(loc).first() : loc
  await el.waitFor({ state: 'visible', timeout: 20000 })
  const b = await el.boundingBox()
  await page.mouse.move(b.x + b.width / 2, b.y + b.height / 2, { steps: 15 })
  await pause(300)
  await el.click()
}

await page.goto(`${POD_URL}/api/status?token=${encodeURIComponent(POD_TOKEN)}`)
await page.goto(`${POD_URL}/chat`, { waitUntil: 'domcontentloaded' })
await page.waitForSelector('[aria-label="Chat messages"], textarea', { timeout: 30000 })
await pause(2500)
// A pod build reads as "update available"; that popup is not this proof.
const skip = page.getByText('Skip this version', { exact: true })
if (await skip.isVisible().catch(() => false)) { await skip.click(); await pause(800) }
await shot('start')

const dialog = page.getByRole('dialog')
const fillConnect = async () => {
  await go(dialog.getByText('Connect a machine you have', { exact: true }))
  await pause(1500)
  await shot('connect-form')
  await dialog.getByLabel(/^Name/).fill('Main pod')
  await dialog.getByLabel(/SSH host/).fill('localhost')
  await dialog.getByLabel(/Remote port/).fill(String(CREW_PORT))
  await dialog.getByLabel(/Remote kirocrew path/).fill(CREW_BIN)
  await pause(800)
  await shot('connect-filled')
  await go(dialog.getByRole('button', { name: /^Add remote crew$/ }))
}

if (MODE === 'after') {
  await go('[data-testid="crew-switcher-add"]'); await pause(1200); await shot('plus-menu')
  await go('[data-testid="crew-add-remote"]'); await pause(2500); await shot('add-a-crew')
  // Cancel closes everything: no Remote Crews window is left behind.
  await go(dialog.getByRole('button', { name: /^Cancel$/ })); await pause(1500); await shot('add-cancelled')
  await go('[data-testid="crew-switcher-add"]'); await pause(800)
  await go('[data-testid="crew-add-remote"]'); await pause(2000)
  await go(dialog.getByText('Set up a new Crew', { exact: true })); await pause(3000); await shot('setup-form')
  await go(dialog.getByRole('button', { name: /Back/ })); await pause(1000)
  await fillConnect()
} else {
  await page.goto(`${POD_URL}/settings/instances`, { waitUntil: 'domcontentloaded' })
  await pause(2500); await shot('settings-page')
  await go(page.getByRole('button', { name: /Add Crew/ })); await pause(1500); await shot('add-a-crew')
  await go(dialog.getByText('Set up a new Crew', { exact: true })); await pause(3000); await shot('setup-form')
  await go(dialog.getByRole('button', { name: /Back/ })); await pause(1000)
  await fillConnect()
}

// The crew row: wait for it, connect it if the add did not, then open it.
const row = page.getByText('Main pod', { exact: true }).first()
await row.waitFor({ timeout: 30000 })
await pause(2000); await shot('crew-added')
const connect = page.getByRole('button', { name: /^Connect$/ }).first()
if (await connect.isVisible().catch(() => false)) { await go(connect); await pause(1500) }
await page.getByText(/Connected/).first().waitFor({ timeout: 90000 })
await pause(1500); await shot('crew-connected')
await go(page.getByRole('button', { name: 'Open Main pod' }))
await pause(5000); await shot('crew-open')

if (MODE === 'after') {
  // Back to Local by its tab, then the ▾ menu and its Settings.
  // On the crew, the header is that crew's own (main's code, no tabs yet): back to
  // Local through its menu, then the new bar again.
  const pane = page.frames().find(f => f.url().includes(':7') && f !== page.mainFrame() && f.url().startsWith('http://127.0.0.1'))
  if (pane) {
    await pane.locator('[aria-label^="Switch crew"]').first().click(); await pause(1200); await shot('crew-pane-menu')
    await pane.getByRole('menuitemradio', { name: /Local/ }).first().click()
  }
  await pause(2500); await shot('local-tab')
  await go(page.getByRole('button', { name: /Switch crew/ }).first()); await pause(1500); await shot('crew-menu')
  await go('[data-testid="crew-settings"]'); await pause(3000); await shot('remote-crews-window')
  await page.keyboard.press('Escape'); await pause(1000)
}

const video = page.video()
await context.close()
await browser.close()
renameSync(await video.path(), `${OUT}/${MODE}.webm`)
console.log('video', `${OUT}/${MODE}.webm`)
