/**
 * Screenshot runner for capture/capabilities-drift-repair.html.
 *
 * From website/:
 *   npx vite --host 127.0.0.1 --port 6842 --strictPort
 *   node scripts/capture-capabilities-drift-repair.mjs http://127.0.0.1:6842 <outdir>
 *
 * Three frames per theme: the drifted Approvals section (notice names Review
 * change, button enabled with no edit), the save-failed notice after Review + Save
 * when the drift sits in a setting the page cannot show, and the chat error row
 * that is this path's entry point (the `capabilities_changed` prose with its Open
 * Customize button). Each frame asserts the copy it photographs, so a stale string
 * cannot pass as evidence.
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
    const action = page.getByTestId('next-approvals-save')
    await action.waitFor({ timeout: 10000 })
    if ((await action.innerText()).trim() !== 'Review change') throw new Error(`drift scene: the footer button reads ${await action.innerText()}`)
    if (await action.isDisabled()) throw new Error('drift scene: Review change is disabled')
    const hint = await page.getByTestId('next-approvals-runtime-failed').innerText()
    if (!hint.includes("atlas-writer's agent file was edited outside this page.")) throw new Error(`drift notice does not lead with the fact: ${hint}`)
    if (!hint.includes('Press Review change')) throw new Error(`drift notice does not name the visible button: ${hint}`)
    if (hint.includes('{{')) throw new Error(`drift notice leaked an interpolation placeholder: ${hint}`)
    await root.screenshot({ path: `${OUT}/01-drift-${theme}.png` })

    await action.click()
    await page.getByRole('button', { name: 'Save approvals', exact: true }).waitFor({ timeout: 10000 })
    await action.click()
    const refusal = page.getByTestId('next-approvals-op-error')
    await refusal.waitFor({ timeout: 10000 })
    const text = await refusal.innerText()
    if (!text.includes('That change couldn’t be saved.')) throw new Error(`refusal does not say the save failed: ${text}`)
    await root.screenshot({ path: `${OUT}/02-refused-${theme}.png` })
    if (errors.length) throw new Error(`page errors: ${errors.join(' | ')}`)

    // The entry point: the chat error row that sends the user to this section.
    await page.goto(`${BASE}/capture/capabilities-drift-repair.html?theme=${theme}&scene=errorcard`, { waitUntil: 'networkidle' })
    const card = page.getByTestId('error-card')
    await card.waitFor({ timeout: 10000 })
    if ((await card.getAttribute('data-capabilities-changed')) !== 'true') throw new Error('errorcard scene: the row did not take the capabilities_changed branch')
    const prose = await card.innerText()
    for (const needle of ["This bot's custom agent file changed outside Customize, so new chats cannot start.", 'Open Customize, review and save the change, then start a new chat.']) {
      if (!prose.includes(needle)) throw new Error(`errorcard prose does not say ${needle}: ${prose}`)
    }
    if (prose.includes('materialization_changed')) throw new Error(`errorcard shows the wire code instead of the catalog copy: ${prose}`)
    const open = card.getByRole('button', { name: 'Open Customize', exact: true })
    if (!(await open.count())) throw new Error('errorcard scene: no Open Customize button')
    if (await open.isDisabled()) throw new Error('errorcard scene: Open Customize is disabled')
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
