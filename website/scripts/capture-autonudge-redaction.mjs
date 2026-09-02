/**
 * Screenshots for the auto-nudge redacted-projection UI (the notice, the
 * overwrite-confirm row, the ignored-fields notice).
 *
 * Drives the isolated capture entry (website/capture/autonudge-redaction.html),
 * which mounts the REAL AutoNudgePopover on a FABRICATED masked goal. Every
 * frame ASSERTS its state before writing, so a frame cannot document the wrong
 * state:
 *   01-redacted-notice  the masked goal is in the textarea AND the notice
 *                       explaining it is rendered — without that pairing the
 *                       user reads `[REDACTED: ...]` in their own words with no
 *                       explanation, which is what this PR fixes
 *   02-confirm-armed    editing the masked text armed the confirm row BELOW the
 *                       action row (never in Save's position), with focus on the
 *                       safe choice so the ring is in the pixels
 *   03-ignored-fields   a PATCH that answered `message_ignored: true` surfaced
 *                       the notice that the stored goal was kept
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6851 --strictPort   # in another shell
 *   node scripts/capture-autonudge-redaction.mjs http://127.0.0.1:6851 ../temp-screenshots/autonudge-redaction
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

import { chromiumExecutable } from './lib/chromium-executable.mjs'

const BASE = process.argv[2] || 'http://127.0.0.1:6851'
const OUT = process.argv[3] || '../temp-screenshots/autonudge-redaction'
mkdirSync(OUT, { recursive: true })

const MASKED = 'deploy the release with [REDACTED: aws-access-key-id] then post the summary'

/**
 * Launch with `LD_LIBRARY_PATH` STRIPPED from the browser's environment.
 *
 * A node installed by a version manager (mise, asdf) exports that variable
 * pointing at its own bundled `lib/node`, and Chromium then resolves
 * `libstdc++` from there instead of the system copy. The browser dies during
 * startup and Playwright reports only "Target page, context or browser has been
 * closed", which reads as a Playwright problem rather than an inherited-env one.
 * The same binary runs fine from a shell, which is what makes it confusing.
 * Passing an explicit env is enough; everything else is inherited as usual.
 */
const { LD_LIBRARY_PATH: _poisonedByVersionManager, ...browserEnv } = process.env
const browser = await chromium.launch({
  executablePath: chromiumExecutable(),
  env: browserEnv,
})
let failed = false

function check(name, ok, detail) {
  console.log(`${name}: ${ok ? 'OK' : 'MISMATCH'} ${detail}`)
  if (!ok) failed = true
  return ok
}

async function newPage(theme, { ignored = false } = {}) {
  const page = await browser.newPage({
    viewport: { width: 520, height: 600 },
    deviceScaleFactor: 2,
  })
  // Gateway-free. Predicate on the pathname, not a glob: `**/api/**` would also
  // swallow vite-served source modules and break boot. The PATCH answer is what
  // drives frame 3 — `message_ignored` is read off the response, so the notice
  // is produced by the component's own code path rather than forced by a prop.
  await page.route(
    u => new URL(u).pathname.startsWith('/api/'),
    route => {
      const path = new URL(route.request().url()).pathname
      if (path.startsWith('/api/autonudge/')) {
        return route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify({
            ok: true,
            message_ignored: ignored,
            loop: { id: 'loop-capture-1', message: MASKED, message_redacted: true },
          }),
        })
      }
      const isList = /commands|skills|agents|sessions|files|history|models/.test(path)
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: isList ? '[]' : '{}',
      })
    },
  )
  await page.goto(`${BASE}/capture/autonudge-redaction.html?theme=${theme}`)
  await page.waitForSelector('[data-capture-root]')
  await page.getByTestId('autonudge-redacted-notice').waitFor()
  return page
}

for (const theme of ['dark', 'light']) {
  // ---- 01: the masked goal AND the notice that explains it, together
  let page = await newPage(theme)
  const area = page.getByRole('textbox', { name: /goal|describe/i })
  const shown = await area.inputValue()
  const notice = (await page.getByTestId('autonudge-redacted-notice').textContent()) || ''
  if (
    check(
      `01-redacted-notice-${theme}`,
      shown.includes('[REDACTED:') && /masked/i.test(notice),
      `masked_in_field=${shown.includes('[REDACTED:')} notice=${notice.slice(0, 40)}`,
    )
  ) {
    await page.screenshot({ path: `${OUT}/01-redacted-notice-${theme}.png` })
  }

  // ---- 02: editing the mask arms the confirm row, focus on the safe choice
  await area.fill(`${MASKED} and tag the build`)
  // Save is activated by KEY, not by mouse: the component focuses the decline
  // button programmatically, and Chromium only paints a `:focus-visible` ring
  // when the interaction modality is already keyboard. A mouse click leaves the
  // ring unpainted, so the frame would assert focus in prose while showing none
  // in the pixels. Pressing Enter is also the exact path the repeat-key guard on
  // these buttons exists for.
  const save = page.getByRole('button', { name: /^Save$/ })
  await save.focus()
  await page.keyboard.press('Enter')
  const question = page.getByTestId('autonudge-confirm-question')
  await question.waitFor()
  const decline = page.getByTestId('autonudge-decline-overwrite')
  const focused = await decline.evaluate(el => el === document.activeElement)
  // The RING, not just the focus: `:focus-visible` is what actually renders, so
  // assert the pixels rather than the DOM property alone.
  const ringVisible = await decline.evaluate(el => el.matches(':focus-visible'))
  // The confirm must sit BELOW Save, never in its place: compare document order.
  const saveBox = await save.boundingBox()
  const confirmBox = await page.getByTestId('autonudge-confirm-overwrite').boundingBox()
  const below = Boolean(saveBox && confirmBox && confirmBox.y > saveBox.y)
  if (
    check(
      `02-confirm-armed-${theme}`,
      focused && ringVisible && below,
      `decline_focused=${focused} ring_visible=${ringVisible} confirm_below_save=${below}`,
    )
  ) {
    await page.screenshot({ path: `${OUT}/02-confirm-armed-${theme}.png` })
  }
  await page.close()

  // ---- 03: a save the backend answered with message_ignored
  page = await newPage(theme, { ignored: true })
  const area3 = page.getByRole('textbox', { name: /goal|describe/i })
  await area3.fill(`${MASKED} and tag the build`)
  await page.getByRole('button', { name: /^Save$/ }).click()
  await page.getByTestId('autonudge-confirm-overwrite').click()
  const ignoredNotice = page.getByTestId('autonudge-ignored-fields')
  await ignoredNotice.waitFor({ timeout: 5000 }).catch(() => {})
  const text = (await ignoredNotice.textContent().catch(() => '')) || ''
  if (check(`03-ignored-fields-${theme}`, /stored goal was kept/i.test(text), `notice=${text.slice(0, 48)}`)) {
    await page.screenshot({ path: `${OUT}/03-ignored-fields-${theme}.png` })
  }
  await page.close()
}

await browser.close()

if (failed) process.exit(1)
