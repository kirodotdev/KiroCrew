/**
 * Screenshot + assertion runner for capture/mcp-tab-configured-sign-in.html.
 *
 * The label is an ARGUMENT: before and after differ by which version of the MCP
 * table's source is on disk, not by anything the capture page fakes.
 *
 * From website/, with the dev server already up:
 *   npx vite --host 127.0.0.1 --port 6816 --strictPort
 *
 *   git checkout <base> -- src/pages/overview/McpTab.tsx src/pages/overview/McpRowSignIn.tsx
 *   node scripts/capture-mcp-tab-configured-sign-in.mjs http://127.0.0.1:6816 OUT before
 *   git checkout HEAD -- src/pages/overview/McpTab.tsx src/pages/overview/McpRowSignIn.tsx
 *   node scripts/capture-mcp-tab-configured-sign-in.mjs http://127.0.0.1:6816 OUT after
 *
 * `before` must show the chat guidance and no sign-in control on the user-added
 * row; `after` must show Sign in on it and Sign in again on the two rows that
 * already went through a sign-in.
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6816'
const OUT = process.argv[3] || '../temp-screenshots/mcp-tab-configured-sign-in'
const LABEL = process.argv[4] || 'after'

if (!['before', 'after'].includes(LABEL)) {
  console.error(`usage: … <baseUrl> <outDir> <before|after>  (got "${LABEL}")`)
  process.exit(2)
}

mkdirSync(OUT, { recursive: true })

const browser = await chromium.launch()
let failed = 0

for (const theme of ['dark', 'light']) {
  const ctx = await browser.newContext({ viewport: { width: 1240, height: 760 }, deviceScaleFactor: 2, colorScheme: theme })
  const page = await ctx.newPage()
  const errors = []
  page.on('pageerror', e => errors.push(String(e)))
  const name = `${theme}-${LABEL}.png`
  try {
    await page.goto(`${BASE}/capture/mcp-tab-configured-sign-in.html?theme=${theme}`, { waitUntil: 'networkidle' })
    await page.waitForSelector('text=docs-internal', { timeout: 15000 })
    await page.waitForTimeout(500)

    const signIn = await page.getByRole('button', { name: 'Sign in', exact: true }).count()
    const again = await page.getByRole('button', { name: 'Sign in again', exact: true }).count()
    const chatLink = await page.getByRole('link', { name: /Go to chat/ }).count()

    await page.locator('[data-capture-root]').screenshot({ path: `${OUT}/${name}` })

    if (LABEL === 'before' && !(signIn === 0 && again === 0 && chatLink === 1)) {
      failed++
      console.error(`FAIL ${name}: expected chat guidance only, got signIn=${signIn} again=${again} chatLink=${chatLink}`)
    }
    if (LABEL === 'after' && !(signIn === 1 && again === 2 && chatLink === 0)) {
      failed++
      console.error(`FAIL ${name}: expected 1 Sign in + 2 Sign in again, got signIn=${signIn} again=${again} chatLink=${chatLink}`)
    }

    if (LABEL === 'after') {
      await page.getByRole('button', { name: 'Sign in', exact: true }).click()
      await page.getByRole('link', { name: /Authorize docs-internal/ }).waitFor({ timeout: 10000 })
      await page.locator('[data-capture-root]').screenshot({ path: `${OUT}/${theme}-after-authorize.png` })
    }
    if (errors.length) {
      failed++
      console.error(`FAIL ${name}: page errors: ${errors.join(' | ')}`)
    }
    console.log(`${failed ? 'checked' : 'ok'} ${name}`)
  } catch (e) {
    failed++
    console.error(`FAIL ${name}: ${e}`)
  }
  await ctx.close()
}

await browser.close()
process.exit(failed ? 1 : 0)
