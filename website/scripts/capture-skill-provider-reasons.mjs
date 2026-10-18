/**
 * Capture the Discover panel's provider-reason states (capture/skill-provider-reasons.tsx).
 *
 * Usage: node scripts/capture-skill-provider-reasons.mjs [baseUrl] [outDir]
 * With no baseUrl it starts its own Vite dev server on 127.0.0.1 (loopback only)
 * and closes it when done.
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

let base = process.argv[2]
const OUT = process.argv[3] || '../temp-screenshots/skill-provider-reasons'
mkdirSync(OUT, { recursive: true })

let server = null
if (!base) {
  const { createServer } = await import('vite')
  server = await createServer({ server: { host: '127.0.0.1', port: 0, strictPort: false }, logLevel: 'error' })
  await server.listen()
  const addr = server.httpServer.address()
  base = `http://127.0.0.1:${addr.port}`
}

const browser = await chromium.launch()
const page = await browser.newPage({ viewport: { width: 1180, height: 720 }, deviceScaleFactor: 2 })
let failed = false
try {
  for (const theme of ['dark', 'light']) {
    for (const state of ['rate-limited', 'partial', 'empty']) {
      await page.goto(`${base}/capture/skill-provider-reasons.html?theme=${theme}&state=${state}`)
      await page.waitForSelector('[data-capture-root]')
      await page.addStyleTag({ content: '*, *::before, *::after { animation: none !important; transition: none !important; }' })
      await page.getByRole('combobox', { name: 'Search skills' }).fill('acme/release-notes')
      await page.waitForTimeout(700)
      const notices = await page.getByTestId('skill-browser-provider-error').allInnerTexts()
      const empty = await page.getByText(/found for/).count()
      const expectNotices = state === 'empty' ? 0 : state === 'partial' ? 1 : 2
      const expectEmpty = state === 'empty' ? 1 : 0
      const ok = notices.length === expectNotices && empty === expectEmpty
      if (!ok) failed = true
      console.log(`${theme}/${state}: notices=${notices.length} empty=${empty} ${ok ? 'OK' : 'UNEXPECTED'}`)
      notices.forEach(n => console.log(`  - ${n.replace(/\s+/g, ' ').trim()}`))
      await page.screenshot({ path: `${OUT}/${state}-${theme}.png` })
    }
    // Detail-pane states: select the row, then (for install) press Install.
    for (const state of ['preview-error', 'install-refused']) {
      await page.goto(`${base}/capture/skill-provider-reasons.html?theme=${theme}&state=${state}`)
      await page.waitForSelector('[data-capture-root]')
      await page.addStyleTag({ content: '*, *::before, *::after { animation: none !important; transition: none !important; }' })
      await page.getByRole('combobox', { name: 'Search skills' }).fill('acme/release-notes')
      await page.getByRole('option', { name: 'release-notes' }).click()
      let expected
      if (state === 'preview-error') {
        await page.getByTestId('skill-preview-error').waitFor()
        expected = 'limiting requests'
      } else {
        await page.getByRole('button', { name: /^Install/ }).last().click()
        await page.getByText(/over the 1 MB limit/).first().waitFor()
        expected = 'over the 1 MB limit'
      }
      const shown = await page.getByText(new RegExp(expected)).count()
      const ok = shown > 0
      if (!ok) failed = true
      console.log(`${theme}/${state}: reason shown=${shown} ${ok ? 'OK' : 'UNEXPECTED'}`)
      await page.screenshot({ path: `${OUT}/${state}-${theme}.png` })
    }
  }
} finally {
  await browser.close()
  if (server) await server.close()
}
process.exit(failed ? 1 : 0)
