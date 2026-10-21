/**
 * Screenshot capture for the system-wide summon hotkey OFF state
 * (#22615 / PR #22616) — Settings → Shortcuts → "Desktop App".
 *
 * Produces (into the out dir):
 *   off-dark.png / off-light.png  — the Off row ("Off" status) + the
 *                                   how-to-enable hint, the new surface.
 *   on-dark.png                   — the bound control case, for contrast.
 *
 * Usage:
 *   node scripts/capture-22615-global-hotkey-off.mjs [base] [out-dir]
 *   Default base: http://127.0.0.1:5199
 *   Start the dev server first, from the website/ directory:
 *     npx vite --host 127.0.0.1 --port 5199
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const base = process.argv[2] || 'http://127.0.0.1:5199'
const out = process.argv[3] || '../temp-screenshots/global-hotkey-off'
mkdirSync(out, { recursive: true })

const scenes = [
  { name: 'off-dark', scene: 'off', theme: 'dark', label: 'summon hotkey OFF — "Off" row + how-to-enable hint (dark)' },
  { name: 'off-light', scene: 'off', theme: 'light', label: 'summon hotkey OFF — "Off" row + how-to-enable hint (light)' },
  { name: 'on-dark', scene: 'on', theme: 'dark', label: 'summon hotkey bound — control case (dark)' },
]

const browser = await chromium.launch()
try {
  for (const { name, scene, theme, label } of scenes) {
    const page = await browser.newPage({
      viewport: { width: 760, height: 900 },
      deviceScaleFactor: 2,
    })
    page.on('pageerror', err => { throw err })

    await page.goto(
      `${base}/capture/global-hotkey-off-22615.html?scene=${scene}&theme=${theme}`,
      { waitUntil: 'networkidle' },
    )

    // The "Desktop App" section only renders once the stubbed bridge resolves.
    const heading = page.getByText('Desktop App', { exact: true })
    await heading.waitFor({ state: 'visible', timeout: 15_000 })

    // Crop to the wrapper <div> that holds BOTH the "Desktop App" heading and
    // its SettingsCard (the summon row + the how-to-enable hint) — that whole
    // block is the surface the Design/UX lanes asked to see. The wrapper is the
    // div that contains the heading AND a [data-settings-card]; the bare
    // heading strip (an earlier, wrong crop) showed only the title.
    const section = page
      .locator('div:has(> div > h4:text-is("Desktop App")):has([data-settings-card])')
      .last()
    await section.waitFor({ state: 'visible', timeout: 8_000 })

    const file = `${out}/${name}.png`
    await section.screenshot({ path: file, animations: 'disabled' })
    console.log(`${name}: captured → ${file}  (${label})`)
    await page.close()
  }
} finally {
  await browser.close()
}
