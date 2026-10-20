// Screenshots for the SidePanel Sessions overview tab: the populated tab
// (driver, Blocked/Complete badges, a running run with Stop run), the open
// `+` menu with the Sessions entry, both confirm dialogs, the unfiltered
// empty state, and both ErrorNotice states. Light + dark per repo convention.
//   node capture/shoot-sessions-overview.mjs [outDir] [port]
import { createServer } from 'vite'
import { chromium } from 'playwright-core'
import { mkdirSync } from 'node:fs'
import { join } from 'node:path'

const OUT = process.argv[2] || '/tmp/sessions-overview-shots'
const PORT = Number(process.argv[3] || 5181)
mkdirSync(OUT, { recursive: true })

const server = await createServer({
  configFile: 'vite.config.ts',
  server: { port: PORT, strictPort: true, host: '127.0.0.1' },
})
await server.listen()
console.log(`vite up on ${PORT}`)

const launchOpts = { args: ['--no-sandbox'] }
if (process.env.CHROMIUM_PATH) launchOpts.executablePath = process.env.CHROMIUM_PATH
const browser = await chromium.launch(launchOpts)

async function newPage(theme, width) {
  const ctx = await browser.newContext({
    viewport: { width, height: 760 },
    reducedMotion: 'reduce',
  })
  const page = await ctx.newPage()
  return { ctx, page }
}

async function shootPopulated(theme, out) {
  const { ctx, page } = await newPage(theme, 440)
  await page.goto(
    `http://127.0.0.1:${PORT}/capture/sessions-overview.html?scene=populated&theme=${theme}`,
  )
  await page.getByText('Alpha checkout').waitFor({ timeout: 15000 })
  await page.getByText('Nova').waitFor({ timeout: 15000 })
  await page.waitForTimeout(400)
  await page.screenshot({ path: join(OUT, out) })
  console.log('shot', out)
  await ctx.close()
}

async function shootConfirms(theme, suffix) {
  const { ctx, page } = await newPage(theme, 760)
  await page.goto(
    `http://127.0.0.1:${PORT}/capture/sessions-overview.html?scene=populated&theme=${theme}`,
  )
  const alpha = page.locator('li', { hasText: 'Alpha checkout' })
  await alpha.getByRole('button', { name: 'Close' }).click()
  await page.getByRole('dialog').waitFor({ timeout: 15000 })
  await page.waitForTimeout(300)
  await page.screenshot({ path: join(OUT, `sessions-confirm-slot-${suffix}.png`) })
  console.log('shot', `sessions-confirm-slot-${suffix}.png`)
  await page.keyboard.press('Escape')
  const run = page.locator('li', { hasText: 'Indexing docs' })
  await run.getByRole('button', { name: 'Stop run' }).click()
  await page.getByRole('dialog').waitFor({ timeout: 15000 })
  await page.waitForTimeout(300)
  await page.screenshot({ path: join(OUT, `sessions-confirm-run-${suffix}.png`) })
  console.log('shot', `sessions-confirm-run-${suffix}.png`)
  await ctx.close()
}

async function shootScene(scene, out, theme, width, waitText) {
  const { ctx, page } = await newPage(theme, width)
  await page.goto(
    `http://127.0.0.1:${PORT}/capture/sessions-overview.html?scene=${scene}&theme=${theme}`,
  )
  await page.getByText(waitText).first().waitFor({ timeout: 15000 })
  await page.waitForTimeout(400)
  await page.screenshot({ path: join(OUT, out) })
  console.log('shot', out)
  await ctx.close()
}

async function shootErrorClose(theme, out) {
  const { ctx, page } = await newPage(theme, 440)
  await page.goto(
    `http://127.0.0.1:${PORT}/capture/sessions-overview.html?scene=error-close&theme=${theme}`,
  )
  const run = page.locator('li', { hasText: 'Indexing docs' })
  await run.getByText('Indexing docs').waitFor({ timeout: 15000 })
  await run.getByRole('button', { name: 'Stop run' }).click()
  await page.getByRole('dialog').waitFor({ timeout: 15000 })
  await page.getByRole('button', { name: 'Stop run' }).last().click()
  await page.getByText("Couldn't close the session.").waitFor({ timeout: 15000 })
  await page.waitForTimeout(300)
  await page.screenshot({ path: join(OUT, out) })
  console.log('shot', out)
  await ctx.close()
}

await shootPopulated('light', 'sessions-light.png')
await shootPopulated('dark', 'sessions-dark.png')
await shootScene('empty', 'sessions-empty-light.png', 'light', 440, 'No sessions')
await shootScene('empty', 'sessions-empty-dark.png', 'dark', 440, 'No sessions')
await shootScene('menu', 'sessions-menu-light.png', 'light', 700, 'Sessions')
await shootScene('menu', 'sessions-menu-dark.png', 'dark', 700, 'Sessions')
await shootConfirms('light', 'light')
await shootConfirms('dark', 'dark')
await shootScene('error-roster', 'sessions-error-roster-light.png', 'light', 440, 'roster unavailable')
await shootScene('error-roster', 'sessions-error-roster-dark.png', 'dark', 440, 'roster unavailable')
await shootErrorClose('light', 'sessions-error-close-light.png')
await shootErrorClose('dark', 'sessions-error-close-dark.png')

await browser.close()
await server.close()
console.log('done')
