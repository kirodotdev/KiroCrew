// Shoot the offline sidebar-rename refusal via the offline-rename-gate harness.
// Run from website/: node scripts/capture-offline-rename-shots.mjs <outdir>
//
// Route stubbing is deliberately NARROW. The harness has no gateway, so calls
// 403; three of them surface as "Could not load … / Session expired" notices in
// the sidebar, which is harness furniture rather than product behaviour and must
// not be photographed. Those three are stubbed empty.
//
// The other observed calls (/api/config/kirocrew, /api/theme/boot, /api/themes,
// /api/auth/refresh) are left alone ON PURPOSE: measured, the app renders fine
// when they 403, and answering them with an empty object instead blanks the tree
// entirely — an empty body is not a valid config/theme, so stubbing them is
// worse than letting them fail.
import { createServer } from 'vite'
import { chromium } from 'playwright-core'
import path from 'node:path'
import { mkdirSync } from 'node:fs'

const outDir = process.argv[2] || path.join(process.env.TMPDIR || '/tmp', 'offline-rename-shots')
mkdirSync(outDir, { recursive: true })

const STUB_EMPTY_LIST = ['/api/chat/folders', '/api/chat/tags', '/api/chat/tag-columns']

const server = await createServer({
  configFile: 'vite.config.ts',
  server: { port: 5214, strictPort: true, host: '127.0.0.1' },
})
await server.listen()
// node (via mise) puts its own lib dir on LD_LIBRARY_PATH, and its bundled
// libstdc++.so.6 is older than the system one Chromium's libgallium/libLLVM need,
// so the browser dies at launch on a missing GLIBCXX_3.4.29. Drop the inherited
// path for the browser child only.
const browser = await chromium.launch({
  executablePath: process.env.CHROMIUM_PATH,
  env: { ...process.env, LD_LIBRARY_PATH: '' },
})

const shots = [
  { name: 'rename-affordance-online', scene: 'affordance', reconnect: false, expectEditor: true },
  { name: 'rename-refused-offline', scene: 'refused', reconnect: false, expectEditor: false },
  { name: 'rename-recovered-online', scene: 'recovered', reconnect: true, expectEditor: true },
]

let failures = 0
for (const shot of shots) {
  const ctx = await browser.newContext({ viewport: { width: 360, height: 560 }, reducedMotion: 'reduce' })
  const page = await ctx.newPage()
  for (const p of STUB_EMPTY_LIST) {
    await page.route(`**${p}`, route => {
      route.fulfill({ status: 200, contentType: 'application/json', body: '[]' })
    })
  }
  await page.goto(`http://127.0.0.1:5214/capture/offline-rename-gate.html?scene=${shot.scene}`)
  await page.waitForSelector('text=Release notes draft', { timeout: 45000 })
  if (shot.reconnect) {
    await page.click('[data-testid="harness-reconnect"]')
    await page.waitForTimeout(200)
  }
  const target = page.locator('[data-session-title]').filter({ hasText: 'Release notes draft' }).first()
  // force: the offline row is aria-disabled, so Playwright's actionability check
  // refuses to act on it — but a real user CAN double-click it. The guard being
  // photographed lives in the handler, not in pointer-events.
  await target.dblclick({ force: true })
  await page.waitForTimeout(400)

  const editors = await page.locator('.session-row textarea').count()
  const editorOk = shot.expectEditor ? editors > 0 : editors === 0
  // Anything that is harness breakage rather than product behaviour fails the
  // run instead of shipping: a fixture gap ("undefined", a thrown render) and a
  // loading/auth failure notice both misrepresent the change being evidenced.
  const errorCards = await page.locator('text=Cannot read properties').count()
  const undefinedLabels = await page.getByText('undefined', { exact: false }).count()
  const notices = await page.locator('text=Could not load').count()
  const expired = await page.locator('text=Session expired').count()
  const cleanOk = errorCards === 0 && undefinedLabels === 0 && notices === 0 && expired === 0
  if (!editorOk || !cleanOk) {
    console.error(`ASSERT FAILED ${shot.name}: editors=${editors} (want ${shot.expectEditor ? '>0' : '0'}), errorCards=${errorCards}, undefined=${undefinedLabels}, notices=${notices}, expired=${expired}`)
    failures++
  }
  await page.locator(`[data-testid="scene-${shot.scene}"]`).screenshot({ path: path.join(outDir, `${shot.name}.png`) })
  console.log('shot', shot.name, 'editors=', editors, 'errorCards=', errorCards, 'undefined=', undefinedLabels, 'notices=', notices, 'expired=', expired, editorOk && cleanOk ? 'OK' : 'MISMATCH')
  await ctx.close()
}
await browser.close()
await server.close()
if (failures) {
  console.error(`${failures} scene(s) did not show the claimed state`)
  process.exit(1)
}
