/**
 * Screenshots for the Schedule form's chat-folder picker (#1620).
 *
 * Drives the ISOLATED capture entry (website/capture/cron-chat-folder.html),
 * which mounts the REAL JobForm with `/api/chat/folders` answered by a fixture.
 *
 * Eight frames, one per state a reader has to be able to see:
 *  - closed:      the control in its default state, with its own hint, on a new job.
 *  - open:        the folder list, where a nested folder reads as `Work / Standups`.
 *                 Captured by CLICKING the real trigger, so the frame documents the
 *                 shipped control rather than a forced state.
 *  - filed:       an existing job opening on the folder it is already filed in — the
 *                 read side, whose absence would silently unfile a job on the next
 *                 unrelated save.
 *  - hidden:      a filed job with "Hide in chat" on — the picker refuses input and
 *                 the hint says why, while the kept folder is still shown.
 *  - stateless:   a job on a fresh session per run — the picker refuses input and
 *                 the hint says filing needs a persistent session.
 *  - unavailable: the folder list failed to load — the notice and its retry.
 *  - empty:       no folders exist yet — the line saying where one is created.
 *  - deleted:     the job's saved folder is gone — the picker reads "Do not file
 *                 runs" and the line under it says what happened.
 *
 * Each frame ASSERTS the text that makes it that state, because a screenshot of
 * a plain picker would look fine and be the defect.
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6813 --strictPort   # in another shell
 *   node scripts/capture-cron-chat-folder.mjs http://127.0.0.1:6813 ../temp-screenshots/1620-cron-chat-folder
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6813'
const OUT = process.argv[3] || '../temp-screenshots/1620-cron-chat-folder'
mkdirSync(OUT, { recursive: true })

const VIEWPORT = { width: 760, height: 900 }
/** The nested label the picker must show, not a bare duplicate folder name. */
const NESTED_LABEL = 'Work \u203a Standups'
/**
 * Radix's popup enters with `fade-in-0 zoom-in-95`, so the panel is still
 * translucent and mid-scale for ~150ms after its content is queryable. A shot
 * taken the moment `waitFor` resolves therefore shows the list bleeding into the
 * controls behind it, which is exactly the frame a blind reader cannot parse.
 * Settle on the animation ENDING rather than on a sleep, so a slower runner does
 * not silently go back to catching it mid-flight.
 */
const POPUP_SETTLE_MS = 400

/** Per scene: the text that proves the frame shows that state. */
const PROOF = {
  filed: { trigger: 'Standups' },
  hidden: { text: 'Turn off \u201cHide in chat\u201d to use this', trigger: 'Standups', disabled: true },
  stateless: { text: 'needs a persistent session', trigger: 'Do not file runs', disabled: true },
  unavailable: { text: 'Could not load your chat folders', button: 'Retry' },
  empty: { text: 'Save this job now' },
  deleted: { text: 'The folder this job filed into was deleted', trigger: 'Do not file runs' },
}

// mise's node injects LD_LIBRARY_PATH at its own bundled libstdc++, which is
// older than the system Mesa needs; children inherit it, so scrub it here.
const { LD_LIBRARY_PATH: _mise, ...browserEnv } = process.env
const browser = await chromium.launch({ env: browserEnv })
let failures = 0
const fail = (scene, why) => {
  failures++
  console.error(`FAIL ${scene}: ${why}`)
}

for (const scene of ['closed', 'open', 'filed', 'hidden', 'stateless', 'unavailable', 'empty', 'deleted']) {
  const page = await browser.newPage({ viewport: VIEWPORT })
  await page.goto(`${BASE}/capture/cron-chat-folder.html?theme=dark&scene=${scene === 'open' ? 'closed' : scene}`, {
    waitUntil: 'networkidle',
  })
  const trigger = page.getByLabel('Chat folder')
  await trigger.waitFor()

  if (scene === 'open') {
    await trigger.click()
    const nested = page.getByText(NESTED_LABEL, { exact: true })
    try {
      await nested.waitFor({ timeout: 5000 })
      // Opacity is the readable signal: the entry keyframe animates it to 1, so
      // waiting on it is waiting on the animation rather than on a guess.
      await page
        .locator('[role="listbox"], [data-radix-select-content]')
        .first()
        .evaluate(el =>
          Promise.all(el.getAnimations({ subtree: true }).map(a => a.finished.catch(() => {}))),
        )
      await page.waitForTimeout(POPUP_SETTLE_MS)
      const opacity = await page
        .locator('[role="listbox"], [data-radix-select-content]')
        .first()
        .evaluate(el => Number(getComputedStyle(el).opacity))
      if (!(opacity >= 0.99)) fail(scene, `panel still at opacity ${opacity} -- frame is mid-transition`)
    } catch (err) {
      fail(scene, `the list never showed "${NESTED_LABEL}" (${err})`)
    }
  } else if (PROOF[scene]) {
    const proof = PROOF[scene]
    try {
      if (proof.text) await page.getByText(proof.text, { exact: false }).first().waitFor({ timeout: 5000 })
      if (proof.button) await page.getByRole('button', { name: proof.button }).waitFor({ timeout: 5000 })
      if (proof.trigger) {
        // The read side: wait for the folder query to resolve into the trigger,
        // or the frame documents an empty picker on a filed job.
        await page.waitForFunction(
          want => (document.querySelector('[aria-label="Chat folder"]')?.textContent || '').includes(want),
          proof.trigger,
          { timeout: 5000 },
        )
      }
      if (proof.disabled && !(await trigger.isDisabled())) fail(scene, 'the picker still accepts input')
    } catch (err) {
      fail(scene, `the frame never reached its state (${err})`)
    }
  }

  await page.screenshot({ path: `${OUT}/${scene}.png`, fullPage: scene !== 'open' })
  console.log(`wrote ${OUT}/${scene}.png`)
  await page.close()
}

await browser.close()
process.exit(failures ? 1 : 0)
