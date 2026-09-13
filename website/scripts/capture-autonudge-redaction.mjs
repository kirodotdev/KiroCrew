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
 *   03b-kept-goal       the keep-stored arm's OWN notice, which no earlier frame
 *                       showed -- a different sentence from 03's ignored one
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

async function newPage(
  theme,
  { ignored = false, width = 520, conflict = false, reloadFails = false, active = true } = {},
) {
  const page = await browser.newPage({
    // 900, not 600: the gate grew notice rows over successive rounds until the confirm
    // button fell below the fold and Playwright could not click it at all.
    viewport: { width: width === 520 ? 520 : width, height: 900 },
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
      // The 409 arm refetches the slot to show what moved, so the slot GET decides
      // which of the two conflict messages the component can reach.
      if (path.startsWith('/api/autonudge/slot/')) {
        if (reloadFails) return route.fulfill({ status: 500, body: '{}' })
        return route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify({
            loop: { id: 'loop-capture-1', message: MASKED, message_redacted: true, active },
          }),
        })
      }
      if (path.startsWith('/api/autonudge/')) {
        if (conflict) {
          return route.fulfill({
            status: 409,
            contentType: 'application/json',
            body: JSON.stringify({
              error: 'The goal changed in another window — save again to compare and choose.',
            }),
          })
        }
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
  const activeParam = active ? '' : '&active=0'
  await page.goto(`${BASE}/capture/autonudge-redaction.html?theme=${theme}&w=${width}${activeParam}`)
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
      // The notice must name the SAME marker the field shows, so the reader connects
      // the two rather than being told the text is "masked" in the abstract.
      shown.includes('[REDACTED:') && notice.includes('[REDACTED:'),
      `masked_in_field=${shown.includes('[REDACTED:')} notice=${notice.slice(0, 40)}`,
    )
  ) {
    await page.screenshot({ path: `${OUT}/01-redacted-notice-${theme}.png` })
  }

  // ---- 02: editing the mask arms the confirm row, focus on the safe choice
  await area.fill(`${MASKED} and tag the build`)
  // Save is activated by KEY, not by mouse: the component focuses the dismiss
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
  // The SAFE arm is what gets focus, and that is the dismiss button, not the decline
  // one: asserting the wrong arm made this frame document a focus the code never sets.
  const dismiss = page.getByTestId('autonudge-dismiss-overwrite')
  const focused = await dismiss.evaluate(el => el === document.activeElement)
  // The RING, not just the focus: `:focus-visible` is what actually renders, so
  // assert the pixels rather than the DOM property alone.
  const ringVisible = await dismiss.evaluate(el => el.matches(':focus-visible'))
  // The confirm must sit BELOW Save, never in its place: compare document order.
  const saveBox = await save.boundingBox()
  const confirmBox = await page.getByTestId('autonudge-confirm-overwrite').boundingBox()
  const below = Boolean(saveBox && confirmBox && confirmBox.y > saveBox.y)
  if (
    check(
      `02-confirm-armed-${theme}`,
      focused && ringVisible && below,
      `dismiss_focused=${focused} ring_visible=${ringVisible} confirm_below_save=${below}`,
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
  if (check(`03-ignored-fields-${theme}`, /goal unchanged/i.test(text), `notice=${text.slice(0, 48)}`)) {
    await page.screenshot({ path: `${OUT}/03-ignored-fields-${theme}.png` })
  }
  await page.close()

  // ---- 03b: the keep-stored arm — its own notice, distinct from 03's ignored one
  page = await newPage(theme)
  const area3b = page.getByRole('textbox', { name: /goal|describe/i })
  await area3b.fill(`${MASKED} and tag the build`)
  await page.getByRole('button', { name: /^Save$/ }).click()
  await page.getByTestId('autonudge-decline-overwrite').click()
  const keptNotice = page.getByTestId('autonudge-kept-stored-goal')
  await keptNotice.waitFor({ timeout: 5000 }).catch(() => {})
  const keptText = (await keptNotice.textContent().catch(() => '')) || ''
  if (check(`03b-kept-goal-${theme}`, /typed text is still here/i.test(keptText), `notice=${keptText.slice(0, 48)}`)) {
    await page.screenshot({ path: `${OUT}/03b-kept-goal-${theme}.png` })
  }
  await page.close()

  // ---- 04: the moved-goal arm — the served goal changed under a local edit
  page = await newPage(theme)
  const area4 = page.getByRole('textbox', { name: /goal|describe/i })
  await area4.fill('deploy the release then page me when it lands')
  // Move the served goal only AFTER the edit exists: the arm requires both.
  await page.evaluate(() => window.__captureSetGoal('roll back to the previous release instead'))
  await page.getByRole('button', { name: /^Save$/ }).click()
  const preview = page.getByTestId('autonudge-moved-goal-preview')
  await preview.waitFor({ timeout: 5000 }).catch(() => {})
  const previewText = (await preview.textContent().catch(() => '')) || ''
  const movedBtn =
    (await page
      .getByTestId('autonudge-confirm-overwrite')
      .textContent()
      .catch(() => '')) || ''
  if (
    check(
      `04-moved-goal-${theme}`,
      /roll back to the previous release/.test(previewText) && /your edit/i.test(movedBtn),
      `preview=${previewText.slice(0, 40)} button=${movedBtn.slice(0, 32)}`,
    )
  ) {
    await page.screenshot({ path: `${OUT}/04-moved-goal-${theme}.png` })
  }
  await page.close()

  // ---- 05: the narrow-viewport clamp the popover gained
  page = await newPage(theme, { width: 320 })
  const area5 = page.getByRole('textbox', { name: /goal|describe/i })
  await area5.fill(`${MASKED} and tag the build`)
  await page.getByRole('button', { name: /^Save$/ }).click()
  const declineBox = await page.getByTestId('autonudge-decline-overwrite').boundingBox()
  // The clamp's whole job: the controls stay inside a 320px viewport.
  const insideViewport = Boolean(declineBox && declineBox.x >= 0 && declineBox.x + declineBox.width <= 320)
  if (check(`05-narrow-${theme}`, insideViewport, `decline_box=${JSON.stringify(declineBox)}`)) {
    await page.screenshot({ path: `${OUT}/05-narrow-${theme}.png` })
  }
  await page.close()

  // ---- 06: the gate RE-ARMED — the stored goal moved again mid-confirm
  page = await newPage(theme)
  const area6 = page.getByRole('textbox', { name: /goal|describe/i })
  await area6.fill('deploy the release then page me when it lands')
  await page.evaluate(() => window.__captureSetGoal('roll back to the previous release instead'))
  await page.getByRole('button', { name: /^Save$/ }).click()
  await page.getByTestId('autonudge-confirm-overwrite').waitFor({ timeout: 5000 }).catch(() => {})
  // Move it a SECOND time while the confirm is open, then click confirm: the re-arm is
  // detected IN the save handler, so it needs the click, not just the changed goal.
  await page.evaluate(() => window.__captureSetGoal('hold the release, awaiting sign-off'))
  await page.getByTestId('autonudge-confirm-overwrite').click()
  const rearmed = page.getByTestId('autonudge-confirm-rearmed')
  await rearmed.waitFor({ timeout: 5000 }).catch(() => {})
  const rearmedText = (await rearmed.textContent().catch(() => '')) || ''
  // Gated on the re-arm sentence itself so it cannot pass on 04's preview text.
  if (
    check(
      `06-confirm-rearmed-${theme}`,
      /changed again/i.test(rearmedText),
      `notice=${rearmedText.slice(0, 48)}`,
    )
  ) {
    await page.screenshot({ path: `${OUT}/06-confirm-rearmed-${theme}.png` })
  }
  await page.close()

  // ---- 07: the first Escape WARNS instead of discarding a masked goal
  // Gated on the notice, not on the key press: the guard is narrowed to a masked
  // goal, so a frame that only pressed Escape could pass with no warning shown.
  page = await newPage(theme)
  const area7 = page.getByRole('textbox', { name: /goal|describe/i })
  await area7.fill('ship the release once the shards are green')
  await page.keyboard.press('Escape')
  const warned = page.getByTestId('autonudge-escape-warned')
  await warned.waitFor({ timeout: 5000 }).catch(() => {})
  const warnedText = (await warned.textContent().catch(() => '')) || ''
  const stillOpen = await page
    .getByRole('textbox', { name: /goal|describe/i })
    .isVisible()
    .catch(() => false)
  if (
    check(
      `07-escape-discard-${theme}`,
      /discard/i.test(warnedText) && stillOpen,
      `notice=${warnedText.slice(0, 48)} popover_still_open=${stillOpen}`,
    )
  ) {
    await page.screenshot({ path: `${OUT}/07-escape-discard-${theme}.png` })
  }
  await page.close()

  // ---- 09: the PAUSED arm, whose keep-stored button also starts the run
  page = await newPage(theme, { active: false })
  const area9 = page.getByRole('textbox', { name: /goal|describe/i })
  await area9.fill(`${MASKED} and tag the build`)
  await page.getByRole('button', { name: /^Save$|^Start/ }).first().click()
  const declineBtn = page.getByTestId('autonudge-decline-overwrite')
  await declineBtn.waitFor({ timeout: 5000 }).catch(() => {})
  const declineText = (await declineBtn.textContent().catch(() => '')) || ''
  if (
    check(
      `09-paused-keep-and-start-${theme}`,
      /start loop/i.test(declineText),
      `button=${declineText.slice(0, 56)}`,
    )
  ) {
    await page.screenshot({ path: `${OUT}/09-paused-keep-and-start-${theme}.png` })
  }
  await page.close()

  // ---- 10: the 409 itself — the server's own text, after the reload succeeded
  page = await newPage(theme, { conflict: true })
  const area10 = page.getByRole('textbox', { name: /goal|describe/i })
  await area10.fill('deploy the release then page me when it lands')
  await page.getByRole('button', { name: /^Save$/ }).click()
  await page.getByTestId('autonudge-confirm-overwrite').click()
  const err10 = page.getByTestId('auto-nudge-error')
  await err10.waitFor({ timeout: 5000 }).catch(() => {})
  const err10Text = (await err10.textContent().catch(() => '')) || ''
  if (
    check(
      `10-conflict-409-${theme}`,
      /another window/i.test(err10Text),
      `error=${err10Text.slice(0, 56)}`,
    )
  ) {
    await page.screenshot({ path: `${OUT}/10-conflict-409-${theme}.png` })
  }
  await page.close()

  // ---- 11: the 409 whose reload FAILED, which is why the retry cannot converge
  page = await newPage(theme, { conflict: true, reloadFails: true })
  const area11 = page.getByRole('textbox', { name: /goal|describe/i })
  await area11.fill('deploy the release then page me when it lands')
  await page.getByRole('button', { name: /^Save$/ }).click()
  await page.getByTestId('autonudge-confirm-overwrite').click()
  const err11 = page.getByTestId('auto-nudge-error')
  await err11.waitFor({ timeout: 5000 }).catch(() => {})
  const err11Text = (await err11.textContent().catch(() => '')) || ''
  if (
    check(
      `11-conflict-reload-failed-${theme}`,
      /could not reload/i.test(err11Text),
      `error=${err11Text.slice(0, 56)}`,
    )
  ) {
    await page.screenshot({ path: `${OUT}/11-conflict-reload-failed-${theme}.png` })
  }
  await page.close()
}

await browser.close()

if (failed) process.exit(1)
