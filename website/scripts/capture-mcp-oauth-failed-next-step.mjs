/**
 * Screenshots of the failed MCP sign-in notice with the hand-off off (ChatEmbed,
 * ChatPane) and on (the chat page), from capture/mcp-oauth-failed-next-step.html.
 * Each frame ASSERTS what it documents before it is kept:
 *
 *   before  hand-off off: no next step at all (the error is the whole notice)
 *   after   hand-off off: the "sign in again" line, with no link or button;
 *           hand-off on: unchanged, "Ask the agent" and no extra line
 *
 * Usage (from website/; starts its own vite on 127.0.0.1:5198):
 *   node scripts/capture-mcp-oauth-failed-next-step.mjs <before|after> [outDir]
 * Shoot `before` with main's McpOAuthBanner.tsx on disk, `after` with the fix.
 */
import { createServer } from 'vite'
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const LABEL = process.argv[2]
const OUT = process.argv[3] || '../temp-screenshots/mcp-oauth-failed-next-step'
if (!['before', 'after'].includes(LABEL)) {
  console.error(`usage: … <before|after> [outDir]  (got "${LABEL}")`)
  process.exit(2)
}
const NEXT_STEP = 'To sign in again, open Customize → Connections → MCP Servers.'

mkdirSync(OUT, { recursive: true })
const server = await createServer({
  configFile: 'vite.config.ts',
  server: { port: 5198, strictPort: true, host: '127.0.0.1' },
})
await server.listen()
const BASE = 'http://127.0.0.1:5198'

const browser = await chromium.launch()
let failed = false

for (const theme of ['dark', 'light']) {
  const page = await browser.newPage({ viewport: { width: 620, height: 300 }, deviceScaleFactor: 2, colorScheme: theme })
  page.on('pageerror', e => { console.error('pageerror:', e.message); failed = true })
  await page.goto(`${BASE}/capture/mcp-oauth-failed-next-step.html?theme=${theme}`)
  await page.locator('[data-state="handoff-off"] [role="alert"]').waitFor({ timeout: 45000 })
  await page.waitForTimeout(300)

  const off = page.locator('[data-state="handoff-off"] [role="alert"]')
  const on = page.locator('[data-state="handoff-on"] [role="alert"]')
  const offText = await off.innerText()
  const onText = await on.innerText()
  const offControls = await off.locator('a, button').count()
  const onAsk = await on.getByRole('button', { name: /ask the agent/i }).count()
  const ok = LABEL === 'before'
    ? !offText.includes(NEXT_STEP) && offControls === 0 && onAsk === 1
    : offText.includes(NEXT_STEP) && offControls === 0 && onAsk === 1 && !onText.includes(NEXT_STEP)
  console.log(`${theme}-${LABEL}: ${ok ? 'OK' : 'MISMATCH'} offControls=${offControls} onAsk=${onAsk} offHasNextStep=${offText.includes(NEXT_STEP)}`)
  if (!ok) failed = true
  await page.locator('[data-capture-root]').screenshot({ path: `${OUT}/${theme}-${LABEL}.png` })
  await page.close()
}

await browser.close()
await server.close()
process.exit(failed ? 1 : 0)
