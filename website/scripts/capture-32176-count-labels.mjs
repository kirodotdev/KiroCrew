/**
 * Screenshots for #32176: Dev Fleet's row count and the local storage debug
 * page's key counts, in English and Russian.
 *
 * Drives the ISOLATED capture entry (website/capture/count-labels-32176.html),
 * which mounts the REAL pages. Every shot asserts its count text first, so a
 * frame of the wrong state or an error boundary can never be emitted.
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6832 --strictPort   # in another shell
 *   node scripts/capture-32176-count-labels.mjs http://127.0.0.1:6832 ../temp-screenshots/32176 [after|before]
 *
 * `before` runs the same scenes against a checkout of main (copy the capture
 * entry and this script there) and asserts main's glued text instead.
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6832'
const OUT = process.argv[3] || '../temp-screenshots/32176'
const MODE = process.argv[4] || 'after'
mkdirSync(OUT, { recursive: true })

const browser = await chromium.launch()
const page = await browser.newPage({ viewport: { width: 1280, height: 720 } })

async function shoot({ scene, lang, theme = 'dark', filter, expect, name, exact = true }) {
  await page.goto(`${BASE}/capture/count-labels-32176.html?scene=${scene}&lang=${lang}&theme=${theme}`)
  if (filter) {
    const box = page.locator('input[placeholder]').first()
    await box.waitFor({ timeout: 15000 })
    await box.fill(filter)
  }
  const want = expect[MODE]
  const el = page.getByText(want, { exact }).first()
  await el.waitFor({ timeout: 15000 })
  await el.scrollIntoViewIfNeeded()
  await page.waitForTimeout(300)
  await page.screenshot({ path: `${OUT}/${MODE}-${name}`, fullPage: false })
  console.log(`captured ${MODE}-${name} (saw "${want}")`)
}

await shoot({ scene: 'fleet-one', lang: 'en', expect: { after: '1 row', before: '1 rows' }, name: '01-fleet-one-en.png' })
await shoot({ scene: 'fleet-many', lang: 'en', filter: 'docs', expect: { after: '2 / 4 rows', before: '2 / 4 rows' }, name: '02-fleet-filtered-en.png' })
await shoot({ scene: 'fleet-one', lang: 'ru', expect: { after: '1 строка', before: '1 строк' }, name: '03-fleet-one-ru.png' })
await shoot({ scene: 'storage-one', lang: 'en', expect: { after: '1 key', before: '1 keys' }, name: '04-storage-one-en.png' })
await shoot({ scene: 'storage-one', lang: 'ru', theme: 'light', expect: { after: '1 ключ', before: '1 ключей' }, name: '05-storage-one-ru-light.png' })
await shoot({ scene: 'storage-many', lang: 'ru', expect: { after: 'Показано 200 из 205', before: 'Показано 200 из' }, exact: false, name: '06-storage-overflow-ru.png' })

await browser.close()
