/**
 * Screenshot harness: closing the color customizer's "Save preset" name field
 * with Enter or Escape hands focus back to the "Save preset" button that
 * opened it.
 *
 * Real built gallery window behind the shared loopback static server, every
 * /api/** call answered from fixtures (gateway-free). The custom-preset save is
 * STATEFUL, so the refetch-free grid shows the new preset card after Enter.
 *
 * MODE=before (the build without the fix) records where focus lands;
 * MODE=after (default) asserts it lands on the trigger. THEME picks dark
 * (default) or light.
 *
 * Usage: DIST=<dist dir> MODE=after THEME=dark node scripts/capture-26563-save-preset-focus.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync, readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/26563'
const MODE = process.env.MODE || 'after'
const THEME = process.env.THEME || 'dark'
const VIEW = { width: 900, height: 900 }
mkdirSync(OUT, { recursive: true })

function assert(label, ok, detail = '') {
  console.log(`${label}: ${ok ? 'OK' : 'FAIL'}${detail ? ` — ${detail}` : ''}`)
  if (!ok) throw new Error(`${label} failed${detail ? `: ${detail}` : ''}`)
}

// Fixture idle art that carries the source colours the customizer edits.
const IDLE_SVG = readFileSync(fileURLToPath(
  new URL('../src/apps/mochi/assets/animations/mochi_idle.svg', import.meta.url)), 'utf8')
// The customizer renders only in the detail panel of the built-in `kiro-ghost`
// pack, and the gallery grid opens a detail panel only through a CUSTOM pack's
// Manage button. So the fixture lists `kiro-ghost` as `custom` to reach the
// real panel; nothing else about the panel is stubbed.
const PACK = {
  id: 'kiro-ghost', name: 'Kiro', author: 'Kiro Crew', description: '',
  type: 'custom', format: 'svg', thumbnail: '',
}

const { srv, base } = await serveDist(process.env.DIST || undefined)
const browser = await chromium.launch({ env: { ...process.env, LD_LIBRARY_PATH: '/usr/lib64' } })

async function openPage() {
  const saves = []
  let customPresets = []
  const context = await browser.newContext({ viewport: VIEW, deviceScaleFactor: 1 })
  const page = await context.newPage()
  logPageProblems(page)
  await stubDashboardApi(page, {
    theme: THEME,
    localStorageEntries: { 'kc-onboarded': '1', 'mc-changelog-seen': '9999', 'mc-yolo-ack': '1' },
    extra: async (path, route) => {
      const method = route.request().method()
      if (path === '/api/apps/crew-companion/appearances') return json(route, { packs: [PACK] }), true
      if (path === '/api/apps/crew-companion/appearances/detail') {
        return json(route, { meta: PACK, animations: { idle: { content: IDLE_SVG, format: 'svg' } }, colorMap: {} }), true
      }
      if (path === '/api/apps/crew-companion/reminders' && method === 'GET') {
        return json(route, { activeAppearance: 'kiro-ghost', language: 'en', customPresets }), true
      }
      if (path === '/api/apps/crew-companion/reminders/config' && method === 'POST') {
        const body = route.request().postDataJSON()
        if (Array.isArray(body.customPresets)) { saves.push(body.customPresets); customPresets = body.customPresets }
        return json(route, {}), true
      }
      if (path.startsWith('/api/apps/crew-companion/')) return json(route, {}), true
      return false
    },
  })
  await page.goto(`${base}/src/apps/crew-companion/gallery.html`, { waitUntil: 'domcontentloaded' })
  await page.getByRole('button', { name: 'Manage' }).click()
  await page.getByRole('button', { name: 'Customize Colors' }).click()
  await page.getByRole('button', { name: 'Save as Preset' }).waitFor({ timeout: 15_000 })
  return { context, page, saves }
}

/** What holds focus, by accessible label or text, read in the page. */
const focused = (page) => page.evaluate(() => {
  const el = document.activeElement
  if (!el) return 'none'
  return el.getAttribute('aria-label') || (el.tagName === 'BODY' ? 'BODY' : (el.textContent || el.tagName).trim())
})

/** The Reset / Save preset row, so the focus ring fills the frame. */
async function shootRow(page, name) {
  const btn = page.getByRole('button', { name: 'Reset', exact: true })
  const box = await btn.boundingBox()
  const clip = { x: Math.max(0, box.x - 24), y: Math.max(0, box.y - 150), width: 420, height: box.height + 174 }
  await page.screenshot({ path: `${OUT}/${name}-${THEME}.png`, clip })
}

async function viaKeyboard(page, key, type) {
  await page.getByRole('button', { name: 'Save as Preset' }).focus()
  await page.keyboard.press('Enter')
  const input = page.getByRole('textbox', { name: 'Preset name' })
  await input.waitFor()
  await page.keyboard.type(type)
  await page.keyboard.press(key)
  await input.waitFor({ state: 'detached' })
  await page.waitForTimeout(300)
}

try {
  const runs = [
    ['enter', (p) => viaKeyboard(p, 'Enter', 'Sunset')],
    ['escape', (p) => viaKeyboard(p, 'Escape', 'Discarded')],
  ]
  for (const [name, act] of runs) {
    const { context, page, saves } = await openPage()
    await act(page)
    const got = await focused(page)
    if (MODE === 'before') console.log(`before ${name}: focus on ${got}`)
    else assert(`${name}: focus on Save as Preset`, got === 'Save as Preset', got)
    if (name === 'enter') {
      await page.getByAltText('Sunset').waitFor({ timeout: 5000 })
      assert(`${name}: saved once`, saves.length === 1, JSON.stringify(saves.map((s) => s.length)))
    } else {
      assert(`${name}: nothing written`, saves.length === 0, JSON.stringify(saves.map((s) => s.length)))
    }
    await shootRow(page, `${MODE}-${name}`)
    await context.close()
  }
} finally {
  await browser.close()
  srv.close()
}
