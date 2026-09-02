/**
 * Screenshots of the declined re-investigate notice (issue #6270).
 *
 * Drives the ISOLATED capture entry (website/capture/declined-reinvestigate-notice.html),
 * which mounts the REAL IssueDetail with `fetch` stubbed at the network seam. The
 * decline is produced by CLICKING Resume, so `openSession` runs its own probe,
 * record re-read and concluded branch — the frame is the shipped code path, not a
 * prop set by hand.
 *
 * Every scene asserts its headline state before shooting, so this can never
 * quietly emit a screenshot of a resting header or an error boundary.
 *
 * The narrow scene is the one the two earlier in-row attempts failed: 320px is the
 * floor `website/docs/page-layout.md` requires, and it is where a wrapping sibling
 * in the header's `flex-shrink-0` action group pushed the row apart. The notice is
 * portalled out of that row, so this frame is the evidence that it no longer can.
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6812 --strictPort   # in another shell
 *   node scripts/capture-declined-reinvestigate.mjs http://127.0.0.1:6812 ../temp-screenshots/declined-reinvestigate-6270
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6812'
const OUT = process.argv[3] || '../temp-screenshots/declined-reinvestigate-6270'
mkdirSync(OUT, { recursive: true })

const browser = await chromium.launch()

async function shoot({ scene, width, height, click, mustSee, mustNotSee = [], name }) {
  const page = await browser.newPage({ viewport: { width, height } })
  await page.goto(`${BASE}/capture/declined-reinvestigate-notice.html?scene=${scene}&theme=dark`)
  // The record is read cache-first, so the control starts as the primary label
  // and becomes Resume once the read lands.
  await page.waitForSelector('text=Resume', { timeout: 15000 })
  if (click) {
    await page.getByRole('button', { name: /Resume/ }).click()
  }
  for (const text of mustSee) await page.waitForSelector(`text=${text}`, { timeout: 15000 })
  for (const text of mustNotSee) {
    if (await page.locator(`text=${text}`).count()) {
      throw new Error(`scene ${scene}: expected NOT to see ${text}`)
    }
  }
  await page.waitForTimeout(400) // let the popover's entry animation settle
  await page.screenshot({ path: `${OUT}/${name}`, fullPage: false })
  console.log(`captured ${name}`)
  await page.close()
}

// 1. Resting: the record has a LIVE session, so the same click resumes. Proves the
//    notice is a decline surface and not permanent furniture on a finished item.
await shoot({
  scene: 'resting',
  width: 1040, height: 620,
  click: false,
  mustSee: ['Resume'],
  mustNotSee: ['Already finished'],
  name: '01-resting-resume-no-notice.png',
})

// 2. Declined: the session was closed and the work had concluded. The reason is on
//    screen, and the transcript's destination is a control rather than a noun.
await shoot({
  scene: 'declined',
  width: 1040, height: 620,
  click: true,
  mustSee: ['Start over', 'Already finished', 'Open Older Sessions'],
  name: '02-declined-visible-notice.png',
})

// 3. Declined at the 320px floor. The sentence wraps freely here because it is out
//    of the action row; in the row it stretched the button to its height.
await shoot({
  scene: 'declined',
  width: 320, height: 620,
  click: true,
  mustSee: ['Start over', 'Already finished', 'Open Older Sessions'],
  name: '03-declined-320px.png',
})

await browser.close()
