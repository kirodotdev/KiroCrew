// Shoot the two `title-action-error` states the UX lane found in no screenshot:
// the CONNECTED-failure revert ("Couldn't rename to …") and the concurrent
// refusal that holds the editor open. Run from website/:
//   node scripts/capture-rename-notice-shots.mjs <outdir>
//
// The rename request outcome is controlled by Playwright routing rather than by a
// harness flag: a 500 gives the revert, and a request that never fulfils leaves
// the slot in flight so a second commit is refused.
import { createServer } from 'vite'
import { chromium } from 'playwright-core'
import path from 'node:path'
import { mkdirSync } from 'node:fs'

const outDir = process.argv[2] || path.join(process.env.TMPDIR || '/tmp', 'rename-notice-shots')
mkdirSync(outDir, { recursive: true })

const STUB_EMPTY_LIST = ['/api/chat/folders', '/api/chat/tags', '/api/chat/tag-columns']
const TITLE_ROUTE = '**/api/chat/slots/*/title'

const server = await createServer({
  configFile: 'vite.config.ts',
  server: { port: 5216, strictPort: true, host: '127.0.0.1' },
})
await server.listen()
const browser = await chromium.launch({
  executablePath: process.env.CHROMIUM_PATH,
  env: { ...process.env, LD_LIBRARY_PATH: '' },
})

const shots = [
  {
    name: 'rename-rejected-revert-notice',
    // A rejection while CONNECTED: the paint reverts and the notice names the
    // title that was refused.
    route: route => route.fulfill({ status: 500, contentType: 'application/json', body: '{"error":"nope"}' }),
    drive: async (page, target) => {
      await target.dblclick({ force: true })
      const box = page.locator('.session-row textarea').first()
      await box.fill('Rejected name')
      await box.blur()
      await page.waitForSelector('[data-testid="title-action-error"]', { timeout: 15000 })
    },
    assert: async page => {
      const text = await page.locator('[data-testid="title-action-error"]').innerText()
      return { ok: /Rejected name/.test(text), text: text.replace(/\s+/g, ' ').slice(0, 120) }
    },
  },
]

// NOT shot here: the "a rename is already saving" refusal. Driving it needs a
// first request left in flight, and the optimistic row never settled into a
// re-openable state under route interception. It is covered by tests instead —
// useRenameSlot.crossSurface and ChatSidebar.offline both pin it.

let failures = 0
for (const shot of shots) {
  const ctx = await browser.newContext({ viewport: { width: 380, height: 600 }, reducedMotion: 'reduce' })
  const page = await ctx.newPage()
  for (const p of STUB_EMPTY_LIST) {
    await page.route(`**${p}`, route => route.fulfill({ status: 200, contentType: 'application/json', body: '[]' }))
  }
  await page.route(TITLE_ROUTE, shot.route)
  await page.goto('http://127.0.0.1:5216/capture/offline-rename-gate.html?scene=affordance')
  await page.waitForSelector('text=Release notes draft', { timeout: 45000 })
  const target = page.locator('[data-session-title]').filter({ hasText: 'Release notes draft' }).first()
  await shot.drive(page, target)

  const detail = await shot.assert(page)
  if (!detail.ok) {
    console.error(`ASSERT FAILED ${shot.name}: ${JSON.stringify(detail)}`)
    failures++
  }
  await page.screenshot({ path: path.join(outDir, `${shot.name}.png`) })
  console.log('shot', shot.name, JSON.stringify(detail), detail.ok ? 'OK' : 'MISMATCH')
  await ctx.close()
}
await browser.close()
await server.close()
if (failures) {
  console.error(`${failures} scene(s) did not show the claimed state`)
  process.exit(1)
}
