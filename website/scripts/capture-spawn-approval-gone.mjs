/**
 * Frames of a spawn approval that is already gone, refused through the
 * composer's spawn banner while the activity panel shows the same sub-agent.
 *
 * Writes two contract frames per theme. The first captures the race window:
 * controls are gone while liveness/counts/Reload remain conservative. The
 * second captures the authoritative-empty result: the card is retired and all
 * busy/count/Reload readers are clean. Two more per theme: a non-terminal
 * (500) failure that keeps the banner's buttons under an error notice, and a
 * three-agent banner after one agent's approval is refused as gone. One more
 * per theme: a gone refusal pressed on the activity card whose liveness read
 * (`GET /api/spawn`) fails, so the card reports that failure and stays
 * unresolved rather than retiring. One more per theme: the same failed read
 * after a refusal pressed on the COMPOSER banner, so the banner's notice and,
 * after the chip's 30s re-read also fails, the progress bar's notice say so.
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6824 --strictPort   # in another shell
 *   node scripts/capture-spawn-approval-gone.mjs <baseUrl> <outDir>
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6824'
const OUT = process.argv[3] || '../temp-screenshots/spawn-approval-gone'
const EXPECTED = 'This approval has expired or was already decided'
const LIVENESS_FAILED = "Couldn't check whether it started. Retrying…"
mkdirSync(OUT, { recursive: true })

const browser = await chromium.launch()
const page = await browser.newPage({ viewport: { width: 1040, height: 560 }, deviceScaleFactor: 2, locale: 'en-US' })
let failed = false

for (const theme of ['dark', 'light']) {
  await page.goto(`${BASE}/capture/spawn-approval-gone.html?theme=${theme}`, { waitUntil: 'domcontentloaded', timeout: 120000 })
  const composer = page.locator('[data-capture-composer]')
  const panel = page.locator('[data-capture-panel]')
  await composer.getByText(/awaiting your approval to run/).waitFor()

  await composer.locator('button', { hasText: /^\s*Approve\s*$/ }).click()
  // The banner and the progress bar's ErrorNotice both carry the sentence.
  await composer.getByRole('alert').filter({ hasText: EXPECTED }).first().waitFor()
  await page.getByTestId('subagent-approval-gone-error').waitFor()
  await page.getByTestId('subagent-unresolved-count').waitFor()
  await panel.getByText('Checking whether it started…').waitFor()
  // The banner animates out; count its controls once the exit has finished.
  await composer.locator('button', { hasText: /^\s*Approve(?: all)?\s*$/ }).first().waitFor({ state: 'detached' })
  const unresolved = {
    composerLive: await composer.locator('button', { hasText: /^\s*Approve(?: all)?\s*$/ }).count(),
    panelLive: await panel.locator('button', { hasText: /^\s*(Approve|Reject)\s*$/ }).count(),
    running: await page.getByTestId('capture-running-count').innerText(),
    approval: await page.getByTestId('capture-approval-count').innerText(),
    composer: await page.getByTestId('capture-composer-state').innerText(),
    reload: await page.getByTestId('capture-reload-state').innerText(),
  }
  const unresolvedOk = unresolved.composerLive === 0 && unresolved.panelLive === 0
    && unresolved.running.endsWith('1') && unresolved.approval.endsWith('0')
    && unresolved.composer.endsWith('Busy') && unresolved.reload.endsWith('Blocked')
  console.log(`${theme} unresolved: ${JSON.stringify(unresolved)} ${unresolvedOk ? 'OK' : 'MISMATCH'}`)
  if (!unresolvedOk) { failed = true; continue }
  await page.screenshot({ path: `${OUT}/spawn-approval-gone-${theme}-reconciling.png` })

  await page.getByTestId('capture-reload-state').filter({ hasText: 'Available' }).waitFor({ timeout: 8000 })
  const retired = {
    progress: await page.locator('[data-testid="subagent-histogram"]').count(),
    running: await page.getByTestId('capture-running-count').innerText(),
    approval: await page.getByTestId('capture-approval-count').innerText(),
    composer: await page.getByTestId('capture-composer-state').innerText(),
    reload: await page.getByTestId('capture-reload-state').innerText(),
    panelNotice: (await panel.getByRole('alert').allInnerTexts()).join(' '),
  }
  const retiredOk = retired.progress === 0 && retired.running.endsWith('0')
    && retired.approval.endsWith('0') && retired.composer.endsWith('Idle')
    && retired.reload.endsWith('Available') && retired.panelNotice.includes(EXPECTED)
  console.log(`${theme} retired: ${JSON.stringify(retired)} ${retiredOk ? 'OK' : 'MISMATCH'}`)
  if (!retiredOk) { failed = true; continue }
  await page.screenshot({ path: `${OUT}/spawn-approval-gone-${theme}-retired.png` })

  // A non-terminal failure (500): the decision was not recorded, so the banner
  // reports it through the error notice and keeps its buttons for a retry.
  await page.goto(`${BASE}/capture/spawn-approval-gone.html?theme=${theme}&scenario=retry`, { waitUntil: 'domcontentloaded', timeout: 120000 })
  await composer.getByText(/awaiting your approval to run/).waitFor()
  await composer.locator('button', { hasText: /^\s*Approve\s*$/ }).click()
  await page.getByTestId('approval-decision-error').filter({ hasText: /wasn't recorded/ }).waitFor()
  const retry = {
    notice: await page.getByTestId('approval-decision-error').innerText(),
    role: await page.getByTestId('approval-decision-error').getAttribute('role'),
    composerLive: await composer.locator('button', { hasText: /^\s*(Approve|Reject)\s*$/ }).count(),
    panelNotice: (await panel.getByRole('alert').allInnerTexts()).join(' '),
  }
  const retryOk = retry.role === 'alert' && retry.composerLive === 2 && !retry.panelNotice.includes(EXPECTED)
  console.log(`${theme} retry: ${JSON.stringify(retry)} ${retryOk ? 'OK' : 'MISMATCH'}`)
  if (!retryOk) { failed = true; continue }
  await page.screenshot({ path: `${OUT}/spawn-approval-retry-${theme}.png` })

  // Three pending spawns, one refused as gone: only that row is withdrawn.
  await page.goto(`${BASE}/capture/spawn-approval-gone.html?theme=${theme}&scenario=multi`, { waitUntil: 'domcontentloaded', timeout: 120000 })
  await composer.getByText(/3 sub-agents are awaiting your approval to run/).waitFor()
  await composer.getByRole('button', { name: 'Approve sub-agent: Check the changelog links' }).click()
  await composer.getByText(/2 sub-agents are awaiting your approval to run/).waitFor()
  await composer.getByRole('alert').filter({ hasText: EXPECTED }).first().waitFor()
  const multi = {
    rows: await composer.getByRole('button', { name: /^Approve sub-agent:/ }).count(),
    goneRow: await composer.getByRole('button', { name: 'Approve sub-agent: Check the changelog links' }).count(),
  }
  const multiOk = multi.rows === 2 && multi.goneRow === 0
  console.log(`${theme} multi: ${JSON.stringify(multi)} ${multiOk ? 'OK' : 'MISMATCH'}`)
  if (!multiOk) { failed = true; continue }
  await page.screenshot({ path: `${OUT}/spawn-approval-multi-${theme}.png` })

  // A gone refusal pressed on the activity card, then a FAILED read of the
  // spawn inventory (500): the card says it couldn't check and keeps the
  // spawn unresolved, so counts, the composer, and Reload stay conservative.
  await page.goto(`${BASE}/capture/spawn-approval-gone.html?theme=${theme}&scenario=liveness-fail`, { waitUntil: 'domcontentloaded', timeout: 120000 })
  await panel.locator('button', { hasText: /^\s*Approve\s*$/ }).waitFor()
  await panel.locator('button', { hasText: /^\s*Approve\s*$/ }).click()
  await panel.getByRole('alert').filter({ hasText: LIVENESS_FAILED }).waitFor()
  const liveness = {
    notices: await panel.getByRole('alert').allInnerTexts(),
    panelLive: await panel.locator('button', { hasText: /^\s*(Approve|Reject)\s*$/ }).count(),
    running: await page.getByTestId('capture-running-count').innerText(),
    approval: await page.getByTestId('capture-approval-count').innerText(),
    composer: await page.getByTestId('capture-composer-state').innerText(),
    reload: await page.getByTestId('capture-reload-state').innerText(),
  }
  const livenessOk = liveness.notices.some(n => n.includes(LIVENESS_FAILED))
    && liveness.notices.some(n => n.includes(EXPECTED)) && liveness.panelLive === 0
    && liveness.running.endsWith('1') && liveness.approval.endsWith('0')
    && liveness.composer.endsWith('Busy') && liveness.reload.endsWith('Blocked')
  console.log(`${theme} liveness-fail: ${JSON.stringify(liveness)} ${livenessOk ? 'OK' : 'MISMATCH'}`)
  if (!livenessOk) { failed = true; continue }
  await page.screenshot({ path: `${OUT}/spawn-approval-liveness-fail-${theme}.png` })

  // The same failed read after a refusal pressed on the composer banner: its
  // immediate re-read fails and the banner says so (that notice clears itself
  // after 8s); the chip's 30s re-read then fails too and the progress bar
  // says so beside the gone sentence. One frame for each.
  await page.goto(`${BASE}/capture/spawn-approval-gone.html?theme=${theme}&scenario=liveness-fail`, { waitUntil: 'domcontentloaded', timeout: 120000 })
  await composer.getByText(/awaiting your approval to run/).waitFor()
  await composer.locator('button', { hasText: /^\s*Approve\s*$/ }).click()
  await page.getByTestId('approval-decision-error').filter({ hasText: LIVENESS_FAILED }).waitFor()
  const composerNotice = await page.getByTestId('approval-decision-error').innerText()
  await page.screenshot({ path: `${OUT}/spawn-approval-composer-liveness-fail-${theme}.png` })
  await page.getByTestId('subagent-liveness-error').waitFor({ timeout: 40000 })
  const chip = {
    composerNotice,
    chipNotice: await page.getByTestId('subagent-liveness-error').innerText(),
    unresolved: await page.getByTestId('subagent-unresolved-count').innerText(),
    composer: await page.getByTestId('capture-composer-state').innerText(),
  }
  const chipOk = chip.composerNotice.includes(LIVENESS_FAILED) && chip.chipNotice.includes(LIVENESS_FAILED)
    && chip.unresolved.trim() === '1' && chip.composer.endsWith('Busy')
  console.log(`${theme} chip-liveness-fail: ${JSON.stringify(chip)} ${chipOk ? 'OK' : 'MISMATCH'}`)
  if (!chipOk) { failed = true; continue }
  await page.screenshot({ path: `${OUT}/spawn-approval-chip-liveness-fail-${theme}.png` })
}

await browser.close()
process.exit(failed ? 1 : 0)
