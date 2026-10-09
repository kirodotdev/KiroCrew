/**
 * Frames of the scene popover's approval-failure notice (#14711): a slot parked
 * on a tool approval is opened from the scene, Approve is pressed, and the
 * server answers that the approval is gone (404). The popover then shows the
 * ErrorNotice it renders for that refusal, in the theme's danger colour, and
 * withdraws the Approve/Deny the notice says are dead.
 *
 * Drives the isolated capture entry (website/capture/scene-approval-failure.html),
 * which mounts the REAL useSceneInteraction hook. Every /api request is answered
 * here: the slot's history with two messages, and the decide with a 404. Each
 * frame asserts before it is written that the notice is on screen and says the
 * decision was not recorded.
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6822 --strictPort   # in another shell
 *   node scripts/capture-scene-approval-failure.mjs http://127.0.0.1:6822 ../temp-screenshots/notification-approval-refusal
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6822'
const OUT = process.argv[3] || '../temp-screenshots/notification-approval-refusal'
mkdirSync(OUT, { recursive: true })

// A refused press reads the sentence every approval surface uses for it.
const GONE = 'This approval has expired or was already decided'
const browser = await chromium.launch()
let failed = false

for (const [name, theme] of [['17-scene-approval-404', 'dark'], ['18-scene-approval-404-light', 'light']]) {
  const page = await browser.newPage({ viewport: { width: 700, height: 460 }, colorScheme: theme })
  // Only the gateway's own routes (a path that STARTS with /api/), never the
  // dev server's module paths such as /src/api/client.ts.
  await page.route(url => url.pathname.startsWith('/api/'), route => {
    const req = route.request()
    if (req.method() === 'POST' && /\/approve(\?|$)/.test(req.url())) {
      return route.fulfill({ status: 404, contentType: 'application/json', body: JSON.stringify({ error: 'no pending approval' }) })
    }
    return route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ messages: [
        { role: 'user', content: 'Deploy the staging stack' },
        { role: 'assistant', content: 'Running the deploy script now.' },
      ] }),
    })
  })
  await page.goto(`${BASE}/capture/scene-approval-failure.html?theme=${theme}`)
  await page.waitForSelector('[data-capture-root]')
  const box = await page.getByTestId('scene').boundingBox()
  await page.mouse.click(box.x + 120, box.y + 120)
  await page.getByRole('dialog').waitFor()
  await page.getByRole('button', { name: /^Approve/ }).click()
  const notice = page.getByTestId('scene-approval-failure')
  await notice.waitFor()
  await page.waitForTimeout(300)
  const text = await notice.innerText()
  const buttons = await page.getByRole('button', { name: /^(Approve|Deny)$/ }).count()
  const ok = text.includes(GONE) && buttons === 0
  console.log(`${name}: ${ok ? 'OK' : 'MISMATCH'} ${JSON.stringify({ text, buttons })}`)
  if (ok) await page.screenshot({ path: `${OUT}/${name}.png` })
  else failed = true
  await page.close()
}

await browser.close()
process.exit(failed ? 1 : 0)
