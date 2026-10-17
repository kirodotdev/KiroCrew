/** Real-browser evidence for the submit-to-chat bar's lifecycle.
 *
 * Drives the ISOLATED capture entry (website/capture/artifact-panel-submit-clear.html),
 * which mounts the REAL `ArtifactPanel` over a fixture-backed fetch boundary.
 * The arc a screenshot pair cannot fake is asserted at every stage:
 *
 *  1-pending:   the bar reads "3 comments to send to this chat".
 *  2-cleared:   the driver clicks the real Submit; the submission carries all
 *               three comment bodies and the bar unmounts.
 *  3-new-alone: `window.__addComment()` models a comment added after the
 *               submission; the bar returns reading "1 comment to send to this
 *               chat", and a second Submit carries ONLY the new comment.
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6823 --strictPort    # in another shell (website/)
 *   node scripts/capture-artifact-panel-submit-clear.mjs http://127.0.0.1:6823 ../temp-screenshots/artifact-panel-submit-clear
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6823'
const OUT = process.argv[3] || '../temp-screenshots/artifact-panel-submit-clear'
mkdirSync(OUT, { recursive: true })

/** Dock width of the real side panel, plus room for the doc body. */
const VIEWPORT = { width: 460, height: 720 }

// mise's node injects LD_LIBRARY_PATH at its own bundled libstdc++, which is
// older than the system Mesa needs; children inherit it, so scrub it here.
const { LD_LIBRARY_PATH: _mise, ...browserEnv } = process.env
const browser = await chromium.launch({ env: browserEnv })
let failures = 0
const check = (label, ok) => {
  console.log(`${label} => ${ok ? 'OK' : 'FAIL'}`)
  if (!ok) failures++
}

for (const theme of ['dark', 'light']) {
  const page = await browser.newPage({ viewport: VIEWPORT })
  page.on('pageerror', e => { console.error(`[${theme}] pageerror:`, e.message); failures++ })
  await page.goto(`${BASE}/capture/artifact-panel-submit-clear.html?theme=${theme}`, { waitUntil: 'networkidle' })

  const bar = page.getByText(/comments? to send to this chat/)
  const submit = page.getByRole('button', { name: 'Submit' })

  // Stage 1 — three durable comments pending.
  await bar.waitFor({ state: 'visible', timeout: 15000 })
  check(`[${theme}] pending bar`, (await bar.textContent())?.includes('3 comments to send to this chat'))
  await page.waitForTimeout(150)
  await page.screenshot({ path: `${OUT}/${theme}-1-pending.png` })

  // Stage 2 — Submit sends the batch and the bar clears.
  await submit.click()
  await bar.waitFor({ state: 'detached', timeout: 15000 })
  const first = await page.evaluate(() => window.__submitted[0] ?? '')
  check(`[${theme}] first submission carries all three comments`,
    first.includes('3 comments') && first.includes('metric definition'))
  await page.waitForTimeout(150)
  await page.screenshot({ path: `${OUT}/${theme}-2-cleared.png` })

  // Stage 3 — a comment added after the submission is counted alone.
  await page.evaluate(() => window.__addComment('Also list who owns the checklist.'))
  await bar.waitFor({ state: 'visible', timeout: 15000 })
  check(`[${theme}] new comment counted alone`, (await bar.textContent())?.includes('1 comment to send to this chat'))
  await page.waitForTimeout(150)
  await page.screenshot({ path: `${OUT}/${theme}-3-new-comment-alone.png` })

  await submit.click()
  await bar.waitFor({ state: 'detached', timeout: 15000 })
  const second = await page.evaluate(() => window.__submitted[1] ?? '')
  check(`[${theme}] second submission carries only the new comment`,
    second.includes('1 comment') && second.includes('who owns the checklist') && !second.includes('metric definition'))
  await page.close()
}

await browser.close()
if (failures) {
  console.error(`${failures} assertion(s) failed`)
  process.exit(1)
}
console.log(`done - evidence in ${OUT}`)
