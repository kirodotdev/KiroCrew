/**
 * Screenshot runner for capture/capabilities-drift-repair.html.
 *
 * From website/:
 *   npx vite --host 127.0.0.1 --port 6842 --strictPort
 *   node scripts/capture-capabilities-drift-repair.mjs http://127.0.0.1:6842 <outdir>
 *
 * Three frames per theme: the drifted pane (notice names Review changes, button
 * enabled with no edit), the refusal after Review + Save when the drift sits in a
 * setting the page cannot show (names the file and the turned-off button, beside
 * that button), and the chat error row that is this path's entry point (the
 * `capabilities_changed` prose with its Open Capabilities button). Each frame
 * asserts the copy it photographs, so a stale string cannot pass as evidence.
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6842'
const OUT = process.argv[3] || '../temp-screenshots/capabilities-drift-repair'

mkdirSync(OUT, { recursive: true })
const browser = await chromium.launch()
let failed = 0

for (const theme of ['light', 'dark']) {
  const ctx = await browser.newContext({ viewport: { width: 820, height: 700 }, deviceScaleFactor: 2, colorScheme: theme })
  const page = await ctx.newPage()
  const errors = []
  page.on('pageerror', e => errors.push(String(e)))
  try {
    await page.goto(`${BASE}/capture/capabilities-drift-repair.html?theme=${theme}`, { waitUntil: 'networkidle' })
    const root = page.locator('[data-capture-root]')
    const review = page.getByRole('button', { name: 'Review changes', exact: true })
    await review.waitFor({ timeout: 10000 })
    if (await review.isDisabled()) throw new Error('drift scene: Review changes is disabled')
    const hint = await page.getByText(/was edited outside this page/).innerText()
    if (!hint.startsWith("atlas-writer's agent file was edited outside this page.")) throw new Error(`drift notice does not lead with the fact: ${hint}`)
    if (!hint.includes('Press Review changes')) throw new Error(`drift notice does not name the visible button: ${hint}`)
    if (!hint.includes('that outside edit together with any draft of your own')) throw new Error(`drift notice does not say what Review shows: ${hint}`)
    if (!hint.includes('After reviewing, Save')) throw new Error(`drift notice presents Save as already on screen: ${hint}`)
    if (!hint.includes('or tells you why it cannot')) throw new Error(`drift notice promises Save unconditionally: ${hint}`)
    await root.screenshot({ path: `${OUT}/01-drift-${theme}.png` })

    await review.click()
    const save = page.getByRole('button', { name: 'Save reviewed changes' })
    await save.waitFor({ timeout: 10000 })
    await save.click()
    const footer = page.getByTestId('capability-save-footer')
    const refusal = footer.getByText(/in a setting this page cannot show/)
    await refusal.waitFor({ timeout: 10000 })
    const text = await refusal.innerText()
    for (const needle of ['crew-3f9a1c2e7b4d.json', 'Review changes stays turned off', 'then press Reload from server']) {
      if (!text.includes(needle)) throw new Error(`refusal does not say ${needle}: ${text}`)
    }
    if (/\bSave\b/.test(text)) throw new Error(`refusal names a Save button that is not on screen: ${text}`)
    if (!(await footer.getByRole('button', { name: 'Review changes', exact: true }).isDisabled())) throw new Error('refused scene: Review changes is still enabled')
    if (text.includes('{{')) throw new Error(`refusal leaked an interpolation placeholder: ${text}`)
    if (await page.getByText(/The saved version changed/).count()) throw new Error('refusal fell back to the stale-version copy')
    await root.screenshot({ path: `${OUT}/02-refused-${theme}.png` })
    if (errors.length) throw new Error(`page errors: ${errors.join(' | ')}`)

    // The entry point: the chat error row that sends the user to this pane.
    await page.goto(`${BASE}/capture/capabilities-drift-repair.html?theme=${theme}&scene=errorcard`, { waitUntil: 'networkidle' })
    const card = page.getByTestId('error-card')
    await card.waitFor({ timeout: 10000 })
    if ((await card.getAttribute('data-capabilities-changed')) !== 'true') throw new Error('errorcard scene: the row did not take the capabilities_changed branch')
    const prose = await card.innerText()
    for (const needle of ["This crew member's agent file changed outside the Capabilities page, so new chats cannot start.", 'Open Capabilities, review the change and save it, then start a new chat.']) {
      if (!prose.includes(needle)) throw new Error(`errorcard prose does not say ${needle}: ${prose}`)
    }
    if (prose.includes('materialization_changed')) throw new Error(`errorcard shows the wire code instead of the catalog copy: ${prose}`)
    const open = card.getByRole('button', { name: 'Open Capabilities', exact: true })
    if (!(await open.count())) throw new Error('errorcard scene: no Open Capabilities button')
    if (await open.isDisabled()) throw new Error('errorcard scene: Open Capabilities is disabled')
    if (await card.getByRole('button', { name: 'Resume' }).count()) throw new Error('errorcard scene: Resume offered beside a fix that is not a retry')
    await page.locator('[data-capture-root]').screenshot({ path: `${OUT}/03-errorcard-${theme}.png` })
    if (errors.length) throw new Error(`page errors: ${errors.join(' | ')}`)
    console.log(`ok ${theme}`)
  } catch (e) {
    failed++
    console.error(`FAIL ${theme}: ${e instanceof Error ? e.message : e}`)
  } finally {
    await ctx.close()
  }
}
await browser.close()
process.exit(failed ? 1 : 0)
