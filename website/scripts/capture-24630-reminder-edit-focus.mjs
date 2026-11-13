/**
 * Screenshot harness: closing a Crew Companion reminder's in-place editor with
 * Enter or Escape hands focus back to the control it was opened from (the Edit
 * button, or the More-actions trigger on a recurring row).
 *
 * Real built SPA behind the shared loopback static server, every /api/** call
 * answered from fixtures (gateway-free). The reminder update is STATEFUL, so
 * the refetch after a save shows the new text.
 *
 * MODE=before (the build without the fix) records where focus lands;
 * MODE=after (default) asserts it lands on the trigger. THEME picks dark
 * (default) or light.
 *
 * Usage: DIST=<dist dir> MODE=after THEME=dark node scripts/capture-24630-reminder-edit-focus.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync, readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/24630'
const MODE = process.env.MODE || 'after'
const THEME = process.env.THEME || 'dark'
const VIEW = { width: 1280, height: 800 }
mkdirSync(OUT, { recursive: true })

function assert(label, ok, detail = '') {
  console.log(`${label}: ${ok ? 'OK' : 'FAIL'}${detail ? ` — ${detail}` : ''}`)
  if (!ok) throw new Error(`${label} failed${detail ? `: ${detail}` : ''}`)
}

const MANIFEST = JSON.parse(readFileSync(fileURLToPath(
  new URL('../../src/kiro_crew/apps/builtins/crew_companion/app.json', import.meta.url)), 'utf8'))
const installed = [{
  name: 'crew-companion', version: MANIFEST.version, displayName: MANIFEST.displayName,
  enabled: true, installedAt: '2026-08-25T12:00:00Z', source: 'builtin', origin: 'builtin',
  resources: 'gateway', lifecycle: 'locked', manifest: MANIFEST,
}]

const { srv, base } = await serveDist(process.env.DIST || undefined)
const browser = await chromium.launch({ env: { ...process.env, LD_LIBRARY_PATH: '/usr/lib64' } })

async function openPage() {
  // Local wall-clock times a little ahead, so the row tags read like a real day.
  const inMinutes = (m) => {
    const d = new Date(Date.now() + m * 60_000)
    const p = (n) => String(n).padStart(2, '0')
    return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}T${p(d.getHours())}:${p(d.getMinutes())}:00`
  }
  const reminders = [
    { id: 'r1', text: 'Drink water', fireAt: inMinutes(25), recurrence: null },
    { id: 'r2', text: 'Stand up and stretch', fireAt: inMinutes(50), recurrence: { everyMinutes: 60 } },
  ]
  const updates = []
  const context = await browser.newContext({ viewport: VIEW, deviceScaleFactor: 1 })
  const page = await context.newPage()
  logPageProblems(page)
  await stubDashboardApi(page, {
    theme: THEME,
    localStorageEntries: { 'kc-onboarded': '1', 'mc-changelog-seen': '9999', 'mc-yolo-ack': '1' },
    extra: async (path, route) => {
      const method = route.request().method()
      if (path === '/api/apps') return json(route, installed), true
      if (path === '/api/apps/crew-companion') return json(route, installed[0]), true
      if (path === '/api/apps/crew-companion/reminders') {
        return json(route, {
          reminders: reminders.map((r) => ({ ...r })), breakNudgesEnabled: true,
          sessionNotificationsEnabled: true, breakReminderMins: 45, language: 'English', present: true,
        }), true
      }
      if (path === '/api/apps/crew-companion/stats') {
        return json(route, {
          stats: {
            firstLaunch: '2026-09-01T09:00:00Z', streak: 12, companionSeconds: 86_400, breathingSessions: 3,
            remindersCreated: 9, latestActiveTime: '22:10', earliestActiveTime: '08:30',
          },
          petName: 'Mochi', language: 'English',
        }), true
      }
      if (path === '/api/apps/crew-companion/reminders/update' && method === 'POST') {
        const body = route.request().postDataJSON()
        updates.push(body)
        const r = reminders.find((x) => x.id === body.id)
        if (r) r.text = body.text
        return json(route, {}), true
      }
      if (path.startsWith('/api/apps/crew-companion/')) return json(route, {}), true
      return false
    },
  })
  await page.goto(`${base}/crew-companion`, { waitUntil: 'domcontentloaded' })
  await page.getByText('Drink water', { exact: true }).waitFor({ timeout: 15_000 })
  return { context, page, updates }
}

/** What holds focus, by accessible label, read in the page. */
const focused = (page) => page.evaluate(() => {
  const el = document.activeElement
  return el ? (el.getAttribute('aria-label') || el.tagName) : 'none'
})

/** The reminders card, so the row and its focus ring fill the frame. */
async function shootCard(page, name) {
  const card = page.locator('.cc-rem-row').first().locator('xpath=ancestor::*[contains(@class,"cc-card")][1]')
  const box = (await card.count()) ? await card.boundingBox() : null
  const row = await page.locator('.cc-rem-row').first().boundingBox()
  const clip = box
    ? { x: Math.max(0, box.x - 12), y: Math.max(0, box.y - 12), width: Math.min(VIEW.width, box.width + 24), height: box.height + 24 }
    : { x: Math.max(0, row.x - 24), y: Math.max(0, row.y - 120), width: Math.min(VIEW.width, row.width + 48), height: 280 }
  await page.screenshot({ path: `${OUT}/${name}-${THEME}.png`, clip })
}

const EDIT = 'Edit “Drink water”'
const MORE = 'More actions'

async function viaEditButton(page, key, type) {
  await page.getByRole('button', { name: EDIT }).focus()
  await page.keyboard.press('Enter')
  const input = page.getByRole('textbox', { name: EDIT })
  await input.waitFor()
  if (type) { await page.keyboard.press('ControlOrMeta+a'); await page.keyboard.type(type) }
  await page.keyboard.press(key)
  await input.waitFor({ state: 'detached' })
  await page.waitForTimeout(300)
}

async function viaMoreMenu(page, key) {
  await page.getByRole('button', { name: MORE }).focus()
  await page.keyboard.press('Enter')
  await page.getByRole('menuitem', { name: 'Edit' }).waitFor()
  await page.getByRole('menuitem', { name: 'Edit' }).focus()
  await page.keyboard.press('Enter')
  const input = page.getByRole('textbox', { name: 'Edit “Stand up and stretch”' })
  await input.waitFor()
  await page.keyboard.press(key)
  await input.waitFor({ state: 'detached' })
  await page.waitForTimeout(300)
}

try {
  const runs = [
    // The row's label follows the refetch, so match the Edit button, not its text.
    ['enter-edit', (p) => viaEditButton(p, 'Enter', 'Drink a glass of water'), /^Edit “/],
    ['escape-edit', (p) => viaEditButton(p, 'Escape', 'Discarded draft'), EDIT],
    ['escape-more', (p) => viaMoreMenu(p, 'Escape'), MORE],
  ]
  for (const [name, act, want] of runs) {
    const { context, page, updates } = await openPage()
    await act(page)
    const got = await focused(page)
    const hit = want instanceof RegExp ? want.test(got) : got === want
    if (MODE === 'before') console.log(`before ${name}: focus on ${got}`)
    else assert(`${name}: focus on trigger`, hit, got)
    if (name === 'enter-edit') {
      await page.getByText('Drink a glass of water', { exact: true }).waitFor({ timeout: 5000 })
      assert(`${name}: saved once`, updates.length === 1, JSON.stringify(updates))
      if (MODE !== 'before') assert(`${name}: focus kept after refetch`, /^Edit “Drink a glass/.test(await focused(page)))
    }
    if (name.startsWith('escape')) assert(`${name}: nothing written`, updates.length === 0, JSON.stringify(updates))
    await shootCard(page, `${MODE}-${name}`)
    await context.close()
  }
} finally {
  await browser.close()
  srv.close()
}
