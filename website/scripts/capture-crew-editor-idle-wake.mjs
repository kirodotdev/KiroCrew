/**
 * Screenshot of the Issue Radar crew editor's "Idle wake" field, via the
 * capture/crew-editor-idle-wake harness. Asserts the read-only field holds the
 * backstop (86400) and the line under it names both gaps (300 s while an item is
 * in progress, 86400 s otherwise) before shooting, so a harness that silently
 * rendered the old single-gap line cannot produce a green screenshot.
 *
 * Usage: node scripts/capture-crew-editor-idle-wake.mjs <viteBase> <outDir>
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { join } from 'node:path'

const base = process.argv[2] || 'http://127.0.0.1:5199'
const out = process.argv[3] || '../temp-screenshots/crew-idle-wake'
mkdirSync(out, { recursive: true })

const b = await chromium.launch()
for (const theme of ['dark', 'light']) {
  const p = await (
    await b.newContext({ viewport: { width: 1180, height: 1400 }, deviceScaleFactor: 2 })
  ).newPage()
  await p.goto(`${base}/capture/crew-editor-idle-wake.html?theme=${theme}`, {
    waitUntil: 'load',
    timeout: 120_000,
  })
  const field = p.locator('[data-testid="crew-editor-backstop"]')
  await field.waitFor({ state: 'visible', timeout: 120_000 })
  const value = await field.inputValue()
  if (value !== '86400') throw new Error(`idle wake field reads ${JSON.stringify(value)}`)
  const line = (await p.locator('#crew-editor-backstop-fixed').textContent())?.trim() ?? ''
  if (!line.includes('300') || !line.includes('86400')) {
    throw new Error(`idle wake line names one gap only: ${JSON.stringify(line)}`)
  }
  await field.scrollIntoViewIfNeeded()
  // The field's own block: its label, the read-only input and the line under it.
  const block = field.locator('xpath=ancestor::div[.//label][1]')
  await block.screenshot({ path: join(out, `crew-idle-wake-${theme}.png`) })
  await p.locator('[data-testid="crew-editor"]').screenshot({
    path: join(out, `crew-editor-${theme}.png`),
  })
  console.log(`captured ${theme}: ${line}`)
}
await b.close()
