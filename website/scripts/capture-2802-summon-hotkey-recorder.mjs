/**
 * Screenshot capture for the summon-hotkey recorder (#2802): the "Desktop App"
 * card in Settings → Shortcuts, and the summon row in the shortcuts reference
 * dialog.
 *
 * Produces (into the out dir):
 *   settings-off-{dark,light}.png  — hotkey off: the record button, no Clear
 *   settings-recording-dark.png    — after clicking the record button
 *   settings-recorded-dark.png     — after pressing Alt+Shift+J: bound at once,
 *                                    tagged "always works", with Clear
 *   settings-fallback-dark.png     — a stored chord the shell could not bind
 *   settings-remote-dark.png       — a window on a remote host: read-only row
 *                                    and the config-file hint
 *   settings-save-failed-dark.png  — the shell refused a Clear: the error line
 *   modal-bound-dark.png           — reference dialog, bound and tagged
 *   modal-off-dark.png             — reference dialog, off, pointing at Settings
 *
 * Usage:
 *   node scripts/capture-2802-summon-hotkey-recorder.mjs [base] [out-dir]
 *   Default base: http://127.0.0.1:5199
 *   Start the dev server first, from the website/ directory:
 *     npx vite --host 127.0.0.1 --port 5199
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const base = process.argv[2] || 'http://127.0.0.1:5199'
const out = process.argv[3] || '../temp-screenshots/summon-hotkey-recorder'
mkdirSync(out, { recursive: true })

const browser = await chromium.launch()

async function open(surface, scene, theme) {
  const page = await browser.newPage({ viewport: { width: 900, height: 1000 }, deviceScaleFactor: 2 })
  page.on('pageerror', err => { throw err })
  await page.goto(
    `${base}/capture/summon-hotkey-recorder-2802.html?surface=${surface}&scene=${scene}&theme=${theme}`,
    { waitUntil: 'networkidle' },
  )
  // The summon row only renders once the stubbed bridge resolves.
  await page.getByText('Show or focus', { exact: false }).first().waitFor({ state: 'visible', timeout: 15_000 })
  return page
}

async function shot(page, name) {
  const file = `${out}/${name}.png`
  // The summon row sits last, in the panel and in the dialog's scroll area.
  // Bring it into view and shoot the viewport, so it is seen in its panel or
  // dialog with the rows above it, not cropped out of it.
  await page.getByText('Show or focus', { exact: false }).first().scrollIntoViewIfNeeded()
  await page.evaluate(() => window.scrollTo(0, document.body.scrollHeight))
  await page.screenshot({ path: file, animations: 'disabled' })
  console.log(`captured → ${file}`)
}

try {
  for (const theme of ['dark', 'light']) {
    const page = await open('settings', 'off', theme)
    await shot(page, `settings-off-${theme}`)
    if (theme === 'dark') {
      // The summon row's record button is the last one on the panel.
      await page.getByRole('button', { name: 'Press keys…' }).last().click()
      await shot(page, 'settings-recording-dark')
      await page.keyboard.press('Alt+Shift+KeyJ')
      await page.getByText('always works', { exact: true }).waitFor({ state: 'visible', timeout: 8_000 })
      await shot(page, 'settings-recorded-dark')
    }
    await page.close()
  }
  for (const scene of ['fallback', 'remote']) {
    const page = await open('settings', scene, 'dark')
    await shot(page, `settings-${scene}-dark`)
    await page.close()
  }
  {
    // The shell refuses a Clear: the error shows and the hotkey stays bound.
    const page = await open('settings', 'refused', 'dark')
    await page.getByRole('button', { name: 'Clear' }).last().click()
    await page.getByTestId('global-hotkey-save-error').waitFor({ state: 'visible', timeout: 8_000 })
    await shot(page, 'settings-save-failed-dark')
    await page.close()
  }
  for (const scene of ['bound', 'off']) {
    const page = await open('modal', scene, 'dark')
    await shot(page, `modal-${scene}-dark`)
    await page.close()
  }
} finally {
  await browser.close()
}
