/**
 * Screenshots of ChatEmbed's ask_question card (#25336).
 *
 * Drives website/capture/chat-embed-question-card.html: the REAL ChatEmbed
 * under the REAL AppApiProvider, with the slot's question route answering one
 * blocking card. Frames: 640px dark and light, 340px dark.
 *
 * Asserted per frame: the card renders inside the embed, above the message
 * box. Then: a card that retires while an answer is typed shows the restored
 * notice with the answer in the message box (dark, light); `?fail=1` shows the
 * load-failure notice; `?answered=1` shows no card.
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6812 --strictPort   # in another shell
 *   node scripts/capture-chat-embed-question-card.mjs http://127.0.0.1:6812 ../temp-screenshots/chat-embed-question-card
 */
import { chromium } from 'playwright'
import { mkdirSync, statSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6812'
const OUT = process.argv[3] || '../temp-screenshots/chat-embed-question-card'
mkdirSync(OUT, { recursive: true })

const browser = await chromium.launch()
const pageErrors = []
let failures = 0
const fail = msg => { console.error(`FAIL: ${msg}`); failures++ }

for (const [width, theme] of [[640, 'dark'], [640, 'light'], [340, 'dark']]) {
  const name = `${width}-${theme}`
  const page = await browser.newPage({ viewport: { width: width + 64, height: 640 }, deviceScaleFactor: 1 })
  page.on('pageerror', e => pageErrors.push(String(e)))
  await page.goto(`${BASE}/capture/chat-embed-question-card.html?theme=${theme}&width=${width}`, { waitUntil: 'networkidle' })
  await page.getByTestId('chat-embed-question').waitFor()
  await page.waitForTimeout(500)
  const order = await page.evaluate(() => {
    const card = document.querySelector('[data-testid="chat-embed-question"]')
    const box = document.querySelector('[aria-label="Chat message"]')
    return card && box ? card.getBoundingClientRect().bottom <= box.getBoundingClientRect().top + 1 : null
  })
  if (order !== true) fail(`${name}: card is not above the message box (${order})`)
  const file = `${OUT}/${name}.png`
  await page.locator('[data-capture-root]').screenshot({ path: file })
  const bytes = statSync(file).size
  if (bytes < 4000) fail(`${name} capture is only ${bytes} bytes`)
  console.log(`${name}: captured ${bytes} bytes`)
  await page.close()
}

// The card retires (expired) while an answer is typed: the notice shows and the
// typed answer is in the message box, unsent.
for (const theme of ['dark', 'light']) {
  const page = await browser.newPage({ viewport: { width: 704, height: 640 }, deviceScaleFactor: 1 })
  page.on('pageerror', e => pageErrors.push(String(e)))
  await page.goto(`${BASE}/capture/chat-embed-question-card.html?theme=${theme}`, { waitUntil: 'networkidle' })
  await page.getByTestId('chat-embed-question').waitFor()
  await page.getByPlaceholder(/custom answer/i).fill('Per API key, and per IP for anonymous calls')
  await page.evaluate(() => { window.__retireQuestion = true })
  await page.getByTestId('chat-embed-question-restored').waitFor({ timeout: 15000 })
  await page.waitForTimeout(400)
  const box = await page.evaluate(() => {
    const el = document.querySelector('[data-capture-root] textarea')
    return el ? el.value : ''
  })
  if (!box.includes('Per API key')) fail(`restored-${theme}: typed answer is not in the message box (${JSON.stringify(box)})`)
  const file = `${OUT}/restored-${theme}.png`
  await page.locator('[data-capture-root]').screenshot({ path: file })
  console.log(`restored-${theme}: captured ${statSync(file).size} bytes`)
  await page.close()
}

// A tall card in a short embed: Submit, Dismiss and the message box stay inside it.
{
  const page = await browser.newPage({ viewport: { width: 704, height: 900 }, deviceScaleFactor: 1 })
  page.on('pageerror', e => pageErrors.push(String(e)))
  await page.goto(`${BASE}/capture/chat-embed-question-card.html?theme=dark&tall=1&height=420`, { waitUntil: 'networkidle' })
  await page.getByTestId('chat-embed-question').waitFor()
  await page.waitForTimeout(500)
  const m = await page.evaluate(() => {
    const embed = document.querySelector('[data-capture-root]').firstElementChild.getBoundingClientRect()
    const box = el => (el ? el.getBoundingClientRect() : null)
    const submit = box([...document.querySelectorAll('[data-testid="chat-embed-question"] button')].find(b => b.textContent.includes('Submit')))
    const dismiss = box(document.querySelector('[aria-label="Dismiss question without answering"]'))
    const input = box(document.querySelector('[data-capture-root] textarea'))
    const inside = r => !!r && r.top >= embed.top - 1 && r.bottom <= embed.bottom + 1 && r.height > 0
    return { submit: inside(submit), dismiss: inside(dismiss), input: inside(input) }
  })
  console.log(`tall-in-short: ${JSON.stringify(m)}`)
  if (!m.submit || !m.dismiss || !m.input) fail(`tall-in-short: a control is clipped ${JSON.stringify(m)}`)
  const file = `${OUT}/tall-in-short-dark.png`
  await page.locator('[data-capture-root]').screenshot({ path: file })
  console.log(`tall-in-short-dark: captured ${statSync(file).size} bytes`)
  await page.close()
}

// The question read keeps failing: the load notice shows while the poll retries.
{
  const page = await browser.newPage({ viewport: { width: 704, height: 640 }, deviceScaleFactor: 1 })
  page.on('pageerror', e => pageErrors.push(String(e)))
  await page.goto(`${BASE}/capture/chat-embed-question-card.html?theme=dark&fail=1`, { waitUntil: 'networkidle' })
  await page.getByTestId('chat-embed-question-load-failed').waitFor({ timeout: 15000 })
  await page.waitForTimeout(400)
  const file = `${OUT}/load-failed-dark.png`
  await page.locator('[data-capture-root]').screenshot({ path: file })
  console.log(`load-failed-dark: captured ${statSync(file).size} bytes`)
  await page.close()
}

{
  const page = await browser.newPage({ viewport: { width: 704, height: 640 }, deviceScaleFactor: 1 })
  page.on('pageerror', e => pageErrors.push(String(e)))
  await page.goto(`${BASE}/capture/chat-embed-question-card.html?theme=dark&answered=1`, { waitUntil: 'networkidle' })
  await page.waitForTimeout(800)
  if (await page.getByTestId('chat-embed-question').count()) fail('answered: a card is still shown')
  await page.close()
}

await browser.close()
if (pageErrors.length) { console.error('page errors:', pageErrors); failures++ }
if (failures) {
  console.error(`${failures} assertion failure(s)`)
  process.exit(1)
}
console.log('ALL GREEN')
