/**
 * Shared two-shot (rendered / editing) capture sequence for a Notes-preview
 * screenshot harness.
 *
 * Every `capture-mdnb-*.mjs` script needs the same shell around whatever it
 * is actually proving: open the fixture note, wait for the feature to
 * render, optionally click into edit mode, photograph the note pane. Only
 * the note content, the render-wait target and the click-to-edit target
 * vary per feature, so that shell lives here once rather than being
 * recopied into each new script.
 */
import { chromium } from 'playwright'
import { serveDist } from './serve-dist.mjs'
import { logPageProblems, stubDashboardApi } from './stub-dashboard-api.mjs'
import { MDNB_VAULT_ID, notePaneClip } from './mdnb-fixtures.mjs'

const VIEW = { width: 1280, height: 900 }

/**
 * Open the fixture note and photograph it, rendered and (optionally)
 * mid-edit.
 *
 * `renderedText` both locates the note in the sidebar and marks that the
 * feature under test has finished rendering; `editTarget` is the text
 * clicked to enter edit mode, defaulting to `renderedText` when a single
 * element serves both roles.
 */
async function shoot(browser, base, { mdnbApi, noteTitle, renderedText, editTarget, settleMs, out, file, edit }) {
  const context = await browser.newContext({ viewport: VIEW, deviceScaleFactor: 2, locale: 'en-US' })
  const page = await context.newPage()
  await stubDashboardApi(page, { theme: 'dark', extra: mdnbApi })
  logPageProblems(page)
  // The shared stub clears localStorage in its own init script, so the
  // active vault is written in a LATER init script to survive it.
  await page.addInitScript(vaultId => {
    localStorage.setItem('mdnb-active-vault', vaultId)
  }, MDNB_VAULT_ID)

  await page.goto(base + '/md-notebook', { waitUntil: 'domcontentloaded' })
  await page.getByText(noteTitle).first().waitFor({ timeout: 15000 })
  await page.getByText(noteTitle).first().click()
  await page.getByText(renderedText).first().waitFor({ timeout: 15000 })
  await page.waitForTimeout(settleMs)

  if (edit) {
    await page.getByText(editTarget).first().click()
    await page.locator('textarea').first().waitFor({ timeout: 5000 })
    await page.waitForTimeout(300)
  }

  await page.screenshot({ path: `${out}/${file}`, clip: await notePaneClip(page) })
  console.log('wrote', `${out}/${file}`)
  await context.close()
}

/**
 * Run the standard rendered + editing pair against one fixture note.
 */
export async function runMdnbCapture({
  out, noteTitle, mdnbApi, renderedText, editTarget = renderedText, settleMs = 700,
}) {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  try {
    await shoot(browser, base, { mdnbApi, noteTitle, renderedText, editTarget, settleMs, out, file: '01-rendered.png', edit: false })
    await shoot(browser, base, { mdnbApi, noteTitle, renderedText, editTarget, settleMs, out, file: '02-editing-the-source.png', edit: true })
  } finally {
    await browser.close()
    srv.close()
  }
}
