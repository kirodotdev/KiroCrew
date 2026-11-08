/**
 * Post-create roster retry label — evidence frames for the PR.
 *
 * The create of "Radar" lands (POST /api/agents answers 200) and the roster
 * re-read that follows fails (GET /api/members answers 500). The notice and
 * its retry are captured on desktop and mobile, dark and light:
 *
 *   roster-retry-desktop-<theme>   1440x900, first create: the notice and the
 *                                  retry centred in the chat column
 *   roster-retry-mobile-<theme>    390x844, the same failure below md
 *
 * Each scene then presses the retry once more against the still-failing
 * roster and checks that the press issued one more roster read and no second
 * create or chat message.
 *
 * Real components against stubbed /api (Vite + Playwright), through the
 * crewmates-page-create capture entry.
 *
 * Usage (`self` starts a loopback Vite dev server for the run):
 *   cd website && node scripts/capture-roster-retry-label.mjs self <outDir> [expectedLabel]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

// `self` starts a Vite dev server in-process on a loopback ephemeral port and
// stops it at the end; any other value is the base URL of a running one.
const BASE_ARG = process.argv[2] || 'self'
const OUT = process.argv[3] || '../temp-screenshots/roster-retry-label'
const LABEL = process.argv[4] || 'Refresh the list'
mkdirSync(OUT, { recursive: true })

let devServer = null
let BASE = BASE_ARG
if (BASE_ARG === 'self') {
  const { createServer } = await import('vite')
  devServer = await createServer({ server: { host: '127.0.0.1', port: 0, strictPort: false, hmr: false } })
  await devServer.listen()
  const addr = devServer.httpServer.address()
  BASE = `http://127.0.0.1:${addr.port}`
}

const browser = await chromium.launch()
let failed = false
function check(name, ok, detail) {
  console.log(`${name}: ${ok ? 'OK' : 'MISMATCH'} ${detail}`)
  if (!ok) failed = true
  return ok
}

async function newPage(theme, viewport) {
  const page = await browser.newPage({ viewport, deviceScaleFactor: 1 })
  const counts = { rosterReads: 0, creates: 0, chats: 0 }
  await page.route(u => new URL(u).pathname.startsWith('/api/'), route => {
    const path = new URL(route.request().url()).pathname
    const method = route.request().method()
    const json = (body) => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) })
    if (path === '/api/members') {
      counts.rosterReads += 1
      // The arrival read is empty and fine; every read after the create fails.
      if (counts.rosterReads > 1) return route.fulfill({ status: 500, contentType: 'application/json', body: '{"error":"roster unavailable"}' })
      return json({ members: [], default_agent: 'kirocrew' })
    }
    if (path === '/api/agents' && method === 'POST') {
      counts.creates += 1
      return json({ ok: true })
    }
    if (path === '/api/chat' && method === 'POST') {
      counts.chats += 1
      return json({ ok: true })
    }
    if (path === '/api/teams') return json({ teams: [] })
    if (path === '/api/crons') return json({ jobs: [] })
    if (path === '/api/webhooks') return json({ tokens: [] })
    if (path === '/api/agents') return json({ agents: [], default_agent: 'kirocrew' })
    if (path === '/api/agents/catalog') return json({
      agents: [{ name: 'kirocrew', selection_kind: 'template', kiro_agent: 'kirocrew', scope: 'global' }],
      default_agent: 'kirocrew',
    })
    if (path === '/api/workspaces') return json({ workspaces: [{ name: 'default' }] })
    if (path === '/api/config/default-agent') return json({ default_agent: 'kirocrew' })
    if (path === '/api/autonudge') return json({ enabled: true, loops: [] })
    if (/\/api\/chat\/(tags|pins|folders|tag-columns)$/.test(path)) return route.fulfill({ status: 200, contentType: 'application/json', body: '[]' })
    const isList = /commands|skills|agents$|sessions|files|history|models|artifacts|folders|slots$/.test(path)
    return route.fulfill({ status: 200, contentType: 'application/json', body: isList ? '[]' : '{}' })
  })
  await page.goto(`${BASE}/capture/crewmates-page-create.html?theme=${theme}&scene=roster-fail`)
  await page.waitForSelector('[data-capture-root]')
  await page.getByText('Mates', { exact: true }).first().waitFor()
  return { page, counts }
}

async function assertTheme(page, theme, tag) {
  const bg = await page.evaluate(() => getComputedStyle(document.body).backgroundColor)
  const m = bg.match(/\d+/g) || []
  const lum = m.length >= 3 ? (Number(m[0]) + Number(m[1]) + Number(m[2])) / 3 : -1
  check(`${tag} theme ${theme}`, theme === 'light' ? lum > 180 : lum < 90, `body bg=${bg}`)
}

const SCENES = [
  { name: 'desktop', viewport: { width: 1440, height: 900 }, cta: 'section [data-testid=crewmate-empty-cta]' },
  { name: 'mobile', viewport: { width: 390, height: 844 }, cta: 'ul [data-testid=crewmate-empty-cta]' },
]

for (const scene of SCENES) {
  for (const theme of ['dark', 'light']) {
    const tag = `roster-retry-${scene.name}-${theme}`
    const { page, counts } = await newPage(theme, scene.viewport)
    await page.locator(scene.cta).click()
    const dlg = page.getByRole('dialog')
    await dlg.waitFor()
    await dlg.getByLabel('Name').fill('Radar')
    await dlg.getByRole('button', { name: 'Create mate' }).click()
    const notice = page.getByTestId('member-post-create-error')
    await notice.waitFor()
    await page.getByRole('dialog').waitFor({ state: 'detached' })
    const retry = notice.getByTestId('member-post-create-retry')
    check(`${tag} text`, await notice.getByText("Radar was created, but the list didn't refresh.").isVisible(), 'notice text')
    const label = (await retry.textContent()) || ''
    check(`${tag} label`, label === LABEL, `label=${JSON.stringify(label)}`)
    const box = await retry.boundingBox()
    check(`${tag} retry on screen`, !!box && box.x >= 0 && box.x + box.width <= scene.viewport.width && box.y + box.height <= scene.viewport.height, `retry box=${JSON.stringify(box)}`)
    await page.waitForTimeout(400)
    await assertTheme(page, theme, tag)
    await page.screenshot({ path: `${OUT}/${tag}.png` })
    // The press repeats only the roster read.
    const before = { ...counts }
    await retry.click()
    await page.waitForFunction(() => document.querySelector('[data-testid=member-post-create-retry]'))
    await page.waitForTimeout(600)
    check(`${tag} retry re-reads`, counts.rosterReads > before.rosterReads, `roster reads ${before.rosterReads} -> ${counts.rosterReads}`)
    check(`${tag} no second create`, counts.creates === 1 && before.creates === 1, `creates=${counts.creates}`)
    check(`${tag} no message`, counts.chats === 0, `chat POSTs=${counts.chats}`)
    await page.close()
  }
}

await browser.close()
if (devServer) await devServer.close()
if (failed) {
  console.error('capture-roster-retry-label: MISMATCH')
  process.exit(1)
}
console.log(`capture-roster-retry-label: frames in ${OUT}`)
