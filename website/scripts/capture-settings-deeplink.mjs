/**
 * Screenshots of the settings deep-link collapsed-group reveal (#12703), via the
 * capture/settings-deeplink harness (real SttSettings + real useSettingHighlight,
 * only the two STT reads seeded). Asserts the Streaming row count in the frame it
 * shoots, so a frame can never quietly show the wrong state:
 *   plain    -> 0 "Streaming" rows (Fine-tuning collapsed), 0 "Shortcut key"
 *   deeplink -> 1 "Streaming" row (Fine-tuning revealed), 0 "Shortcut key"
 *               (the nested Push-to-talk group stays closed -- scoped reveal)
 *
 * Usage: node scripts/capture-settings-deeplink.mjs <viteBase> <outDir>
 */
import { chromium } from 'playwright'

const base = process.argv[2] || 'http://localhost:5199'
const out = process.argv[3] || '../temp-screenshots/settings-deeplink'

const b = await chromium.launch()
for (const [scene, streamingRows, shortcutRows] of [['plain', 0, 0], ['deeplink', 1, 0]]) {
  const p = await (await b.newContext({ viewport: { width: 820, height: 900 }, deviceScaleFactor: 2 })).newPage()
  await p.goto(`${base}/capture/settings-deeplink.html?scene=${scene}&theme=dark`, { waitUntil: 'networkidle' })
  // The "Fine-tuning" group header always renders (collapsed or open); wait for
  // it, then let the deep-link probe settle (reveal + the one-microtask
  // containment decision + the 100ms parameter strip).
  await p.getByText('Fine-tuning', { exact: false }).first().waitFor({ timeout: 20_000 })
  await p.waitForTimeout(1200)
  const streaming = await p.locator('[data-setting-label="Streaming"]').count()
  const shortcut = await p.locator('[data-setting-label="Shortcut key"]').count()
  if (streaming !== streamingRows) {
    throw new Error(`scene=${scene}: expected ${streamingRows} Streaming row(s), saw ${streaming}`)
  }
  if (shortcut !== shortcutRows) {
    throw new Error(`scene=${scene}: expected ${shortcutRows} Shortcut key row(s), saw ${shortcut}`)
  }
  const root = p.locator('[data-capture-root]')
  const file = `${out}/${scene}.png`
  await root.screenshot({ path: file })
  console.log(`captured ${file} (Streaming=${streaming}, Shortcut key=${shortcut})`)
}
await b.close()
