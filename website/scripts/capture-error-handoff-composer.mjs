/**
 * Screenshot harness for what the user SEES after "Ask the agent" -- against a
 * REAL pod, not fixtures.
 *
 * The defect this documents is a viewport one: the hand-off staged the full
 * report into the fresh session's composer, but the box stayed at the ~6-line
 * typing cap and was snapped to its last line, so the frame showed a closing
 * fence and a raw JSON tail with the sentence that says what broke scrolled out
 * of sight. That is only provable from a rendered composer under the real
 * ChatPage -> ChatInput wiring, so the shots come from a live gateway
 * (`kirocrew pod up <worktree> --json`).
 *
 * Two hand-off paths, the same way a user hits them:
 *   cross-page  -- a backend failure on /apps/<name> (the app fetch is answered
 *                  500 at the network layer so the real journal in api/client.ts
 *                  captures route + endpoint + status + body), then "Ask the
 *                  agent" navigates to /chat.
 *   in-chat     -- /chat?sid=<missing> raises the "session not found" banner
 *                  inside chat; the hand-off opens a fresh session with no route
 *                  change.
 *
 * Usage:
 *   kirocrew pod up <worktree> --json | tail -1 > "$KIROCREW_SCRATCH/pod-info.json"
 *   POD_INFO="$KIROCREW_SCRATCH/pod-info.json" \
 *     node scripts/capture-error-handoff-composer.mjs ../temp-screenshots/error-handoff-composer
 *
 * Every frame asserts the composer state it photographs before writing the PNG
 * and prints the lines actually visible inside the textarea, so a run against a
 * stale bundle cannot pass off as evidence. The in-chat scene is also recorded,
 * because the click transforms the composer in place and a still cannot show it.
 */
import { chromium } from 'playwright'
import { mkdirSync, readFileSync, renameSync, rmSync } from 'node:fs'
import { join } from 'node:path'
import { check, podInfo } from './lib/crew-pod-harness.mjs'

const OUT = process.argv[2] || '../temp-screenshots/error-handoff-composer'
mkdirSync(OUT, { recursive: true })

const { BASE, authed } = podInfo(readFileSync)
const COMPOSER = { name: 'Message input' }
const REPORT_LEAD = 'This error just came up in the Kiro Crew dashboard.'

async function primePod(page) {
  await page.goto(authed('/chat'), { waitUntil: 'domcontentloaded' })
  await page.locator('#main-content').waitFor({ state: 'visible', timeout: 20000 })
  const skip = page.getByRole('button', { name: /Skip this version/ })
  if (await skip.waitFor({ state: 'visible', timeout: 3000 }).then(() => true, () => false)) {
    await skip.click()
    await skip.waitFor({ state: 'hidden', timeout: 10000 })
  }
  await page.evaluate(async () => {
    localStorage.setItem('mc-theme', 'dark')
    localStorage.setItem('mc-lang', 'en')
    await fetch('/api/config/theme', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ mode: 'dark', language: 'en', onboarded: true, import_onboarded: true, privacy_acked: true }),
    })
  })
}

/** What the textarea shows: its geometry plus the lines inside its viewport. */
async function composerState(page) {
  return page.getByRole('textbox', COMPOSER).evaluate((ta) => {
    const el = ta
    const lh = parseFloat(getComputedStyle(el).lineHeight) || 21
    const first = Math.floor(el.scrollTop / lh)
    const n = Math.round(el.clientHeight / lh)
    return {
      valueLen: el.value.length,
      clientHeight: el.clientHeight,
      scrollHeight: el.scrollHeight,
      scrollTop: el.scrollTop,
      visibleLines: el.value.split('\n').slice(first, first + n),
    }
  })
}

async function shoot(page, label, file) {
  await page.waitForFunction(
    (lead) => (document.querySelector('textarea[aria-label="Message input"]')?.value ?? '').startsWith(lead),
    REPORT_LEAD,
    { timeout: 15000 },
  )
  await page.waitForTimeout(1200) // let the auto-size settle
  const s = await composerState(page)
  console.log(`${label}:`, JSON.stringify({ ...s, visibleLines: undefined }))
  for (const line of s.visibleLines) console.log('   |', line)
  check(`[${label}] composer holds the report`, s.valueLen > 100, `len=${s.valueLen}`)
  // The claim under test: the first line of the report is inside the viewport,
  // and the box grew to the prefill cap (320px) rather than the typing cap (140px).
  check(`[${label}] report is read from its first line`, s.scrollTop === 0 && (s.visibleLines[0] ?? '').startsWith(REPORT_LEAD), `scrollTop=${s.scrollTop}`)
  check(`[${label}] composer grew past the typing cap`, s.clientHeight > 140, `clientHeight=${s.clientHeight}`)
  check(`[${label}] pre-filled hint is shown`, await page.getByText(/Prompt pre-filled/).count() === 1)
  await page.screenshot({ path: join(OUT, file) })
  return s
}

const browser = await chromium.launch()
const context = await browser.newContext({ viewport: { width: 1280, height: 820 }, deviceScaleFactor: 1 })
const page = await context.newPage()
await primePod(page)

// 1. Cross-page: a real backend failure on an app page.
await page.route('**/api/apps/demo-app', route => route.fulfill({
  status: 500,
  contentType: 'application/json',
  body: JSON.stringify({ error: 'manifest.json: expected object at top level, got array', code: 'manifest_invalid' }),
}))
await page.goto(`${BASE}/apps/demo-app`, { waitUntil: 'domcontentloaded' })
const appBanner = page.getByRole('alert').first()
await appBanner.waitFor({ state: 'visible', timeout: 20000 })
check('[cross-page] error banner offers the hand-off', await appBanner.getByRole('button', { name: /Ask the agent/ }).count() === 1)
await page.waitForTimeout(400)
await page.screenshot({ path: join(OUT, '01-app-error-banner.png') })
await appBanner.getByRole('button', { name: /Ask the agent/ }).click()
await page.waitForURL(/\/chat/, { timeout: 15000 })
await shoot(page, 'cross-page', '02-chat-after-handoff.png')
await page.unroute('**/api/apps/demo-app')

// 2. In-chat: the "session not found" banner hands off with no route change.
//    Recorded as well as shot: the click transforms the composer on screen --
//    it grows, its content is replaced, the band mounts -- and a still cannot
//    prove a transition. The recording context is separate so the video holds
//    only this scene.
const recContext = await browser.newContext({
  viewport: { width: 1280, height: 820 },
  deviceScaleFactor: 1,
  recordVideo: { dir: join(OUT, 'video-raw'), size: { width: 1280, height: 820 } },
})
const rec = await recContext.newPage()
await primePod(rec)
await rec.goto(`${BASE}/chat?sid=chat-1-doesnotexist`, { waitUntil: 'domcontentloaded' })
const sidBanner = rec.getByTestId('sid-error')
await sidBanner.waitFor({ state: 'visible', timeout: 20000 })
await rec.waitForTimeout(1200)
await rec.screenshot({ path: join(OUT, '03-in-chat-error-banner.png') })
await sidBanner.getByRole('button', { name: /Ask the agent/ }).click()
await shoot(rec, 'in-chat', '04-in-chat-after-handoff.png')

// 3. The hint holds while the seed is untouched; the first edit arms its 10s
//    expiry. Type one line of context, wait it out, and photograph the settled
//    state: band gone, box back at the typing cap, the caret line in view.
const box = rec.getByRole('textbox', COMPOSER)
await box.focus()
await rec.keyboard.press('Control+End')
await rec.keyboard.press('Shift+Enter') // a newline; plain Enter would send
await rec.keyboard.type('I hit this right after installing the app.', { delay: 20 })
await rec.waitForTimeout(11000)
const settled = await composerState(rec)
check('[after-edit-expiry] the seed is still in the box', settled.valueLen > 494, `len=${settled.valueLen}`)
console.log('after-edit-expiry:', JSON.stringify({ ...settled, visibleLines: undefined }))
for (const line of settled.visibleLines) console.log('   |', line)
check('[after-edit-expiry] hint is gone', await rec.getByText(/Prompt pre-filled/).count() === 0)
check('[after-edit-expiry] box is back at the typing cap', settled.clientHeight <= 140, `clientHeight=${settled.clientHeight}`)
check('[after-edit-expiry] the typed line is in view', settled.visibleLines.some(l => l.includes('right after installing')))
await rec.screenshot({ path: join(OUT, '05-in-chat-after-edit-expiry.png') })
await rec.waitForTimeout(600)
const video = rec.video()
await recContext.close()
if (video) {
  const raw = await video.path()
  renameSync(raw, join(OUT, '06-in-chat-handoff.webm'))
  rmSync(join(OUT, 'video-raw'), { recursive: true, force: true })
}

await browser.close()
console.log(`wrote 5 screenshots + 1 recording to ${OUT}`)
