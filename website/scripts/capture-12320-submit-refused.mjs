/** Real-browser evidence for #12320: a refused batch send answers beside
 *  Submit and hands back the typed instruction; the draft action reads as a
 *  quiet link named for its outcome.
 *
 * Drives website/capture/artifact-submit-refused.html (the REAL CommentsSidebar
 * and ArtifactPanel over stubs). Asserted per theme:
 *  1-before:  both bars pending; "Draft a prompt in chat" in the sidebar footer.
 *  2-refused: after typing an instruction and pressing Submit on each host, a
 *             role=alert sits inside each bar and the instruction is back.
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6824 --strictPort    # in another shell (website/)
 *   node scripts/capture-12320-submit-refused.mjs http://127.0.0.1:6824 ../temp-screenshots/12320
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6824'
const OUT = process.argv[3] || '../temp-screenshots/12320'
mkdirSync(OUT, { recursive: true })

const { LD_LIBRARY_PATH: _mise, ...browserEnv } = process.env
const browser = await chromium.launch({ env: browserEnv })
let failures = 0
const check = (label, ok) => {
  console.log(`${label} => ${ok ? 'OK' : 'FAIL'}`)
  if (!ok) failures++
}

for (const theme of ['dark', 'light']) {
  const page = await browser.newPage({ viewport: { width: 860, height: 640 } })
  page.on('pageerror', e => { console.error(`[${theme}] pageerror:`, e.message); failures++ })
  await page.goto(`${BASE}/capture/artifact-submit-refused.html?theme=${theme}`, { waitUntil: 'networkidle' })
  const hosts = { sidebar: page.getByTestId('page-sidebar'), panel: page.getByTestId('side-panel') }
  await hosts.panel.getByText(/to send to this chat/).waitFor({ state: 'visible', timeout: 15000 })
  check(`[${theme}] draft action named for its outcome`,
    await hosts.sidebar.getByRole('button', { name: 'Draft a prompt in chat' }).isVisible())
  await page.waitForTimeout(150)
  await page.screenshot({ path: `${OUT}/${theme}-1-before.png` })

  for (const [name, host] of Object.entries(hosts)) {
    await host.getByRole('button', { name: 'Add overall instruction' }).click()
    await host.getByRole('textbox', { name: 'Overall instruction' }).fill('Keep the answer short.')
    await host.getByRole('button', { name: 'Submit' }).click()
    const field = host.getByRole('textbox', { name: 'Overall instruction' })
    // The field closes on press and reopens when the refusal hands it back.
    await field.waitFor({ state: 'visible', timeout: 15000 })
    check(`[${theme}] ${name}: instruction handed back`, (await field.inputValue()) === 'Keep the answer short.')
    if (name === 'sidebar') {
      // The page frames the server reason; the chat side panel leaves the
      // notice to its chat host, which reports its own send failures.
      const alert = host.getByRole('alert')
      await alert.waitFor({ state: 'visible', timeout: 15000 })
      check(`[${theme}] sidebar: reason framed in the bar`,
        (await alert.textContent())?.includes("Couldn't send — your comments are still pending. (slot agent mismatch)"))
    } else {
      check(`[${theme}] panel: no second notice`, (await host.getByRole('alert').count()) === 0)
    }
  }
  // Past the bar's 400ms double-fire guard and the button's fade, so Submit
  // shows re-enabled at full opacity.
  for (const host of Object.values(hosts)) {
    await host.getByRole('button', { name: 'Submit' }).waitFor({ state: 'visible' })
  }
  // Move the pointer off the last-clicked Submit, so the shot shows its resting
  // color and not its hover color.
  await page.mouse.move(5, 5)
  await page.waitForTimeout(1500)
  for (const [name, host] of Object.entries(hosts)) {
    check(`[${theme}] ${name}: Submit offered again`, await host.getByRole('button', { name: 'Submit' }).isEnabled())
  }
  await page.screenshot({ path: `${OUT}/${theme}-2-refused.png` })
  await page.close()
}

await browser.close()
if (failures) {
  console.error(`${failures} assertion(s) failed`)
  process.exit(1)
}
console.log(`done - evidence in ${OUT}`)
