/**
 * Screenshots of the failure surfaces that moved onto `ErrorNotice`, each driven
 * into its real failed state through the component's own mutation code
 * (website/capture/error-notice-surfaces.html stubs the network answers).
 *
 * Each frame ASSERTS the state before writing the file, so a frame cannot
 * silently document a surface that never failed:
 *   - workflow-source-panel: the load-failure notice is up, and after Edit →
 *     Re-run the 400 body's two problems render in the validation notice; no
 *     "Ask the agent" button (the editor holds an unsaved draft).
 *   - workspace-picker: after choosing a directory and pressing Create, the
 *     backend's `{ error }` renders in a notice; no hand-off button.
 *   - workflows-page: Validate renders the validator's rejection, a second
 *     Validate renders the 503 as the "Couldn't validate the script" notice (each
 *     its own frame, since Run re-validates and clears them), then Run's 500
 *     renders as the "Couldn't start the run" notice — two different titles, so
 *     the frames assert each title is on its own notice; no hand-off button on
 *     any of them.
 *   - skill-browser (its own page, `?surface=skill-browser`): pressing Install on
 *     a row lands the 500 as the row's inline notice — the row stays unselected
 *     and carries no hand-off button — then selecting that row shows the detail
 *     pane's notice with the "Ask the agent" hand-off.
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6842 --strictPort   # in another shell
 *   node scripts/capture-error-notice-surfaces.mjs http://127.0.0.1:6842 ../temp-screenshots/error-notice-surfaces
 */
import { chromium } from 'playwright'
import { mkdirSync, writeFileSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6842'
const OUT = process.argv[3] || '../temp-screenshots/error-notice-surfaces'
mkdirSync(OUT, { recursive: true })

const ALERT = '[role="alert"]'
const HANDOFF = 'button:has-text("Ask the agent")'  // ErrorNotice's hand-off label

const browser = await chromium.launch()
const page = await browser.newPage({ viewport: { width: 860, height: 1600 }, deviceScaleFactor: 2 })

let failed = false
for (const theme of ['dark', 'light']) {
  await page.goto(`${BASE}/capture/error-notice-surfaces.html?theme=${theme}`)
  await page.waitForSelector('[data-capture-root]')
  await page.addStyleTag({ content: '*, *::before, *::after { animation: none !important; transition: none !important; }' })

  // workflow-source-panel: open, Edit, Re-run → 400 → validation notice.
  const wsp = page.locator('[data-capture-section="workflow-source-panel"]')
  await wsp.locator('button[aria-expanded]').click()
  await wsp.locator(ALERT).first().waitFor()
  await wsp.getByRole('button', { name: 'Edit', exact: true }).click()
  await wsp.getByRole('button', { name: 'Rerun with edits', exact: true }).click()
  await wsp.locator(ALERT).nth(1).waitFor()
  const wspAlerts = await wsp.locator(ALERT).count()
  const wspText = await wsp.locator(ALERT).allInnerTexts()
  const wspHandoffs = await wsp.locator(HANDOFF).count()

  // workspace-picker: the popover is portaled to <body>, so address it at page
  // level: Select the browsed directory, Create → { error } → notice.
  await page.getByRole('button', { name: 'Select', exact: true }).click()
  const wp = page.locator('div.fixed').filter({ has: page.getByLabel('Workspace name') })
  await wp.getByRole('button', { name: 'Create', exact: true }).click()
  await wp.locator(ALERT).waitFor()
  const wpText = (await wp.locator(ALERT).allInnerTexts()).join(' ')
  const wpHandoffs = await wp.locator(HANDOFF).count()
  // Shot now: the popover dismisses itself once the page scrolls to the sections below.
  const wpShot = await wp.screenshot()

  // workflows-page, two frames: Validate → the rejected list (Run re-validates,
  // which clears it, so it is shot on its own), then Run → 500 → request-failed.
  const wf = page.locator('[data-capture-section="workflows-page"]')
  await wf.getByRole('button', { name: 'Validate', exact: true }).click()
  await wf.locator(ALERT).first().waitFor()
  const wfValidateText = (await wf.locator(ALERT).allInnerTexts()).join(' ')
  const wfValidateShot = await wf.screenshot()
  // Second Validate → 503 → the validate notice replaces the rejection.
  await wf.getByRole('button', { name: 'Validate', exact: true }).click()
  await wf.locator(ALERT).filter({ hasText: /503/ }).waitFor()
  const wfUnreachableText = (await wf.locator(ALERT).allInnerTexts()).join(' ')
  const wfUnreachableShot = await wf.screenshot()
  await wf.getByRole('button', { name: 'Run', exact: true }).first().click()
  await wf.locator(ALERT).filter({ hasText: /500/ }).waitFor()
  const wfText = (await wf.locator(ALERT).allInnerTexts()).join(' ')
  const wfHandoffs = await wf.locator(HANDOFF).count()
  // Each failure is titled after the operation that failed, never the other one.
  const titledRight =
    /Couldn't validate the script/.test(wfUnreachableText) && !/Couldn't start the run/.test(wfUnreachableText) &&
    /Couldn't start the run/.test(wfText) && !/Couldn't validate the script/.test(wfText)

  const ok =
    wspAlerts === 2 && wspText.some(t => /missing entrypoint/.test(t) && /line 2/.test(t)) && wspHandoffs === 0 &&
    /already exists/.test(wpText) && wpHandoffs === 0 &&
    /line 2/.test(wfValidateText) && /503/.test(wfUnreachableText) && !/line 2/.test(wfUnreachableText) &&
    /500/.test(wfText) && titledRight && wfHandoffs === 0
  console.log(`${theme}: source-panel alerts=${wspAlerts} handoffs=${wspHandoffs} | workspace "${wpText.slice(0, 60)}" handoffs=${wpHandoffs} | workflows validate "${wfUnreachableText.slice(0, 60)}" run "${wfText.slice(0, 60)}" handoffs=${wfHandoffs} ${ok ? 'OK' : 'MISMATCH'}`)
  if (!ok) { failed = true; continue }

  for (const section of ['workflow-source-panel', 'workflows-page']) {
    await page.locator(`[data-capture-section="${section}"]`).screenshot({ path: `${OUT}/${section}-${theme}.png` })
  }
  writeFileSync(`${OUT}/workspace-picker-${theme}.png`, wpShot)
  writeFileSync(`${OUT}/workflows-page-validate-${theme}.png`, wfValidateShot)
  writeFileSync(`${OUT}/workflows-page-validate-unreachable-${theme}.png`, wfUnreachableShot)

  // skill-browser: search, Install on the first row → 500 → the row's inline
  // notice; the row is NOT selected by installing and hosts no hand-off. Then
  // select the row → the detail pane's notice carries the hand-off.
  await page.goto(`${BASE}/capture/error-notice-surfaces.html?theme=${theme}&surface=skill-browser`)
  await page.waitForSelector('[data-capture-root]')
  await page.addStyleTag({ content: '*, *::before, *::after { animation: none !important; transition: none !important; }' })
  const dialog = page.getByRole('dialog')
  await dialog.getByRole('combobox').fill('release')
  const row = dialog.getByRole('option', { name: 'release-notes' })
  await row.waitFor()
  await row.getByRole('button', { name: 'Install', exact: true }).click()
  await row.locator(ALERT).waitFor()
  const rowText = (await row.locator(ALERT).allInnerTexts()).join(' ')
  const rowSelected = await row.getAttribute('aria-selected')
  const rowHandoffs = await row.locator(HANDOFF).count()
  const dialogAlertsBeforeSelect = await dialog.locator(ALERT).count()
  const rowShot = await dialog.screenshot()
  await row.click()
  await dialog.locator(HANDOFF).waitFor()
  const detailHandoffs = await dialog.locator(HANDOFF).count()
  const skillOk =
    /EACCES/.test(rowText) && rowSelected === 'false' && rowHandoffs === 0 && dialogAlertsBeforeSelect === 1 &&
    detailHandoffs === 1
  console.log(`${theme}: skill row "${rowText.slice(0, 60)}" selected=${rowSelected} handoffs=${rowHandoffs} alerts=${dialogAlertsBeforeSelect} | detail handoffs=${detailHandoffs} ${skillOk ? 'OK' : 'MISMATCH'}`)
  if (!skillOk) { failed = true; continue }
  writeFileSync(`${OUT}/skill-row-failure-${theme}.png`, rowShot)
  await dialog.screenshot({ path: `${OUT}/skill-detail-${theme}.png` })
}

await browser.close()
process.exit(failed ? 1 : 0)
