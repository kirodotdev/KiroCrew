/**
 * Screenshots of the proactive "update found" popup and the top-bar update pill.
 *
 * Drives website/capture/update-found-popup.html. SELF-CHECKING: each modal
 * frame is taken only after the dialog is open AND shows the expected primary
 * action for its scene; each pill frame only after the pill carries the
 * expected label — so the run can never quietly emit a frame of the wrong
 * state.
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6813 --strictPort   # in another shell
 *   node scripts/capture-update-found-popup.mjs http://127.0.0.1:6813 ../temp-screenshots/update-found-popup
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6813'
const OUT = process.argv[3] || '../temp-screenshots/update-found-popup'
mkdirSync(OUT, { recursive: true })

/** expect = text that must be visible before the frame is taken. */
const MODAL_SCENES = [
  { scene: 'desktop', lang: 'en', theme: 'dark', expect: 'Download' },
  { scene: 'desktop', lang: 'en', theme: 'light', expect: 'Download' },
  { scene: 'desktop', lang: 'zh-CN', theme: 'dark', expect: '下载' },
  { scene: 'desktop', lang: 'zh-CN', theme: 'light', expect: '下载' },
  { scene: 'command', lang: 'en', theme: 'dark', expect: 'cli.sh' },
  { scene: 'command', lang: 'zh-CN', theme: 'dark', expect: 'cli.sh' },
  { scene: 'apply', lang: 'en', theme: 'dark', expect: 'Update now' },
]

const PILL_SCENES = [
  { scene: 'pill', lang: 'en', theme: 'dark', expect: 'Update available' },
  { scene: 'pill-downloading', lang: 'en', theme: 'dark', expect: '42' },
  { scene: 'pill-downloaded', lang: 'en', theme: 'dark', expect: 'Update ready' },
  { scene: 'pill', lang: 'zh-CN', theme: 'dark', expect: '有可用更新' },
]

const run = async () => {
  const browser = await chromium.launch()
  let failed = 0
  const shoot = async ({ scene, lang, theme, expect }, viewport, selector) => {
    const ctx = await browser.newContext({ viewport, deviceScaleFactor: 2 })
    const page = await ctx.newPage()
    await page.goto(`${BASE}/capture/update-found-popup.html?scene=${scene}&theme=${theme}&lang=${lang}`)
    let ok = false
    try {
      await page.waitForSelector(selector, { timeout: 8000 })
      await page.waitForFunction(
        ([sel, want]) => (document.querySelector(sel)?.textContent || '').includes(want),
        [selector, expect],
        { timeout: 8000 },
      )
      ok = true
    } catch {
      failed += 1
      const got = await page.locator(selector).first().textContent().catch(() => '(absent)')
      console.error(`FAIL ${scene}-${lang}-${theme}: want "${expect}", surface reads "${got}"`)
    }
    await page.screenshot({ path: `${OUT}/${scene}-${lang}-${theme}.png` })
    console.log(`${ok ? 'ok  ' : 'FAIL'} ${scene}-${lang}-${theme}.png — expect "${expect}"`)
    await ctx.close()
  }
  for (const s of MODAL_SCENES) await shoot(s, { width: 900, height: 560 }, '[role="dialog"]')
  for (const s of PILL_SCENES) await shoot(s, { width: 360, height: 56 }, '[data-testid="update-pill"]')
  await browser.close()
  if (failed) process.exit(1)
}

run().catch((e) => { console.error(e); process.exit(1) })
