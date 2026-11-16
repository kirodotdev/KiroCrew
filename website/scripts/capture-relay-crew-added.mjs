/**
 * Screenshot harness for the "You've been added to a Team Crew" notice (#37522).
 *
 *   node scripts/capture-relay-crew-added.mjs <outDir> [distDir]
 *
 * Runs the REAL built SPA behind `serveDist` with every `/api/**` answered from
 * fixtures (`stubDashboardApi`). The desktop bridge (`window.relayCrewAPI`) is
 * stubbed so the Team Crews switcher mounts, and `GET /api/relay-viewer/crews`
 * answers one Team Crew the person has not seen yet. Scenes, in dark and light:
 *
 *   banner   the bell's banner as the gateway pushes the note, with Open
 *   summary  the one note a first read with several new Crews gets
 *   caret    the switcher caret's dot, menu closed
 *   menu     the open switcher with the New tag on the new row
 *   refusal  Open from the note on a Crew whose host is offline, naming it
 *   gone     Open from the note on a Crew the gateway no longer grants
 *   seen-failed  the open switcher when the gateway could not save the seen state
 *   card-seen-failed  the same failure after an Open from Settings > Remote Crew
 *   settings  the Team Crews channel row in Settings > Notifications > Sources
 *
 * Every frame asserts what it photographs before writing the PNG.
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { join, resolve } from 'node:path'

import { serveDist, DEFAULT_DIST } from './lib/serve-dist.mjs'
import { stubDashboardApi, logPageProblems, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || join(process.env.KIROCREW_SCRATCH || '/tmp', 'relay-crew-added')
const DIST = process.argv[3] ? resolve(process.argv[3]) : DEFAULT_DIST
mkdirSync(OUT, { recursive: true })

const NEW_ID = 'n4x7c2k9p1m5q8r3t6w0'
const TEAM = {
  endpointId: NEW_ID, name: 'Platform Team', crewType: 'TEAM', presence: 'CONNECTED',
  relayConnectionStatus: 'CONNECTED', crewUrl: `https://${NEW_ID}.relay.example.test`, lastSeenAt: null, unseen: true,
}
const OLD = {
  ...TEAM, endpointId: 'a1b2c3d4e5f6g7h8i9j0', name: 'Docs Crew', crewUrl: 'https://a1b2c3d4e5f6g7h8i9j0.relay.example.test',
  unseen: false,
}
// Named by a note, not in the list, and refused by the gateway: access taken back.
const GONE_ID = 'z9y8x7w6v5u4t3s2r1q0'

let seq = 0
const note = (over) => ({
  kind: 'agent', source: 'system', channel: 'system.relay', priority: 'default',
  ts: new Date(Date.now() - seq++ * 1000).toISOString(), acked: false, ...over,
})
const ADDED = note({
  title: "You've been added to Platform Team", body: 'Open it from Team Crews in the Crew switcher.',
  actions: [{ id: 'open', label: 'Open', url: `/chat?relay_crew=${NEW_ID}` }],
  i18n: { id: 'relay_crew_added', params: { name: 'Platform Team' } },
})
const SUMMARY = note({
  title: 'You have access to 3 Team Crews', body: 'Open them from Team Crews in the Crew switcher.',
  i18n: { id: 'relay_crews_available', params: { count: 3 } },
})
const channel = (id, priority = 'default') => ({
  channel: id, source: 'system', registered: true, default_priority: priority, protected: false, settings: {},
})
const CHANNELS = [channel('system.agent'), channel('system.memory'), channel('system.relay')]

const { srv, base } = await serveDist(DIST)
const browser = await chromium.launch()
let failed = false
function check(name, ok, detail = '') {
  console.log(`${name}: ${ok ? 'OK' : 'MISMATCH'} ${detail}`)
  if (!ok) failed = true
}

async function openPage(theme, { route = '/chat', openResult = { ok: true }, seenStatus = 200 } = {}) {
  const context = await browser.newContext({ viewport: { width: 1280, height: 800 }, deviceScaleFactor: 2, colorScheme: theme })
  const page = await context.newPage()
  logPageProblems(page)
  let ws = null
  await stubDashboardApi(page, {
    theme,
    extra: async (path, r) => {
      if (path === '/api/relay-viewer/crews') { await json(r, { crews: [TEAM, OLD] }); return true }
      if (path === '/api/relay-viewer/crews/seen') {
        await json(r, seenStatus === 200 ? { ok: true } : { error: "Couldn't save that these Crews were seen.", code: 'NOT_SAVED' }, seenStatus)
        return true
      }
      if (path === '/api/notifications/channels') { await json(r, { channels: CHANNELS }); return true }
      return false
    },
  })
  await page.routeWebSocket(/\/api\/ws/, socket => { ws = socket })
  await page.addInitScript((result) => {
    window.relayCrewAPI = { open: async () => result }
  }, openResult)
  await page.goto(`${base}${route}`, { waitUntil: 'domcontentloaded' })
  const push = n => ws.send(JSON.stringify({ type: 'notification', data: n }))
  return { context, page, push }
}

async function caretShot(page, file) {
  const caret = page.getByRole('button', { name: 'Switch crew, new Team Crew' })
  await caret.waitFor({ state: 'visible', timeout: 30000 })
  const box = await caret.boundingBox()
  await page.screenshot({ path: file, clip: { x: Math.max(0, box.x - 160), y: 0, width: 420, height: 60 } })
  return caret
}

for (const theme of ['dark', 'light']) {
  {
    const { context, page, push } = await openPage(theme)
    await page.getByRole('button', { name: 'Switch crew, new Team Crew' }).waitFor({ timeout: 30000 })
    await page.waitForTimeout(600)
    push(ADDED)
    const card = page.getByTestId('notification-banner-region').getByText("You've been added to Platform Team")
    await card.waitFor({ timeout: 10000 })
    await page.waitForTimeout(700)
    check(`[banner/${theme}] note with Open`, await page.getByTestId('notification-banner-region').getByRole('button', { name: 'Open', exact: true }).isVisible())
    await page.screenshot({ path: join(OUT, `banner-${theme}.png`), clip: { x: 900, y: 40, width: 380, height: 200 } })
    await context.close()
  }
  {
    const { context, page, push } = await openPage(theme)
    await page.getByRole('button', { name: 'Switch crew, new Team Crew' }).waitFor({ timeout: 30000 })
    await page.waitForTimeout(600)
    push(SUMMARY)
    await page.getByTestId('notification-banner-region').getByText('You have access to 3 Team Crews').waitFor({ timeout: 10000 })
    await page.waitForTimeout(700)
    check(`[summary/${theme}] one count note`, true)
    await page.screenshot({ path: join(OUT, `summary-${theme}.png`), clip: { x: 900, y: 40, width: 380, height: 200 } })
    await context.close()
  }
  {
    const { context, page } = await openPage(theme)
    const caret = await caretShot(page, join(OUT, `caret-${theme}.png`))
    check(`[caret/${theme}] dot on the caret`, await page.getByTestId('crew-switcher-new-dot').isVisible())
    await caret.click()
    const group = page.getByTestId('team-crews-group')
    await group.waitFor({ timeout: 10000 })
    await page.waitForTimeout(400)
    const tags = group.getByTestId('relay-crew-new')
    check(`[menu/${theme}] New tag on the new row only`, (await tags.count()) === 1 && (await tags.first().textContent()) === 'New')
    const menu = page.getByRole('menu')
    const box = await menu.boundingBox()
    await page.screenshot({ path: join(OUT, `menu-${theme}.png`), clip: { x: Math.max(0, box.x - 8), y: Math.max(0, box.y - 50), width: box.width + 16, height: box.height + 58 } })
    await context.close()
  }
  {
    const { context, page } = await openPage(theme, { route: `/chat?relay_crew=${NEW_ID}`, openResult: { ok: false, error: 'CREW_OFFLINE' } })
    const notice = page.getByTestId('relay-crew-open-refusal')
    await notice.waitFor({ timeout: 30000 })
    await page.waitForTimeout(400)
    check(`[refusal/${theme}] names the Crew and says it is offline`,
      (await notice.textContent()).includes("Couldn't open Platform Team. This Crew is offline."))
    await page.screenshot({ path: join(OUT, `refusal-${theme}.png`), clip: { x: 0, y: 0, width: 900, height: 60 } })
    await context.close()
  }
  {
    // Not in the cached list, and the gateway refuses the open: access taken back.
    const { context, page } = await openPage(theme, {
      route: `/chat?relay_crew=${GONE_ID}`, openResult: { ok: false, error: 'CREW_ACCESS_NOT_GRANTED' },
    })
    const notice = page.getByTestId('relay-crew-open-refusal')
    await notice.waitFor({ timeout: 30000 })
    await page.waitForTimeout(400)
    check(`[gone/${theme}] shows the gateway's not-granted refusal`,
      (await notice.textContent()).includes('You no longer have access to this Crew.'))
    await page.screenshot({ path: join(OUT, `gone-${theme}.png`), clip: { x: 0, y: 0, width: 900, height: 60 } })
    await context.close()
  }
  {
    const { context, page } = await openPage(theme, { seenStatus: 503 })
    await page.getByRole('button', { name: 'Switch crew, new Team Crew' }).click({ timeout: 30000 })
    const failed = page.getByTestId('relay-crews-seen-failed')
    await failed.waitFor({ timeout: 10000 })
    await page.waitForTimeout(400)
    check(`[seen-failed/${theme}] says the seen state was not saved`,
      (await failed.textContent()).includes("Couldn't save that you've seen this."))
    const box = await page.getByRole('menu').boundingBox()
    await page.screenshot({ path: join(OUT, `seen-failed-${theme}.png`), clip: { x: Math.max(0, box.x - 8), y: Math.max(0, box.y - 50), width: box.width + 16, height: box.height + 58 } })
    await context.close()
  }
  {
    // An Open from Settings > Remote Crew records seen too; here the save fails.
    const { context, page } = await openPage(theme, { route: '/settings/instances', seenStatus: 503 })
    await page.getByRole('button', { name: 'Open Platform Team' }).click({ timeout: 30000 })
    const notice = page.getByTestId('shared-crew-refusal')
    await notice.waitFor({ timeout: 10000 })
    await notice.scrollIntoViewIfNeeded()
    await page.waitForTimeout(400)
    check(`[card-seen-failed/${theme}] Settings card says the seen state was not saved`,
      (await notice.textContent()).includes("Couldn't save that you've seen this."))
    const card = await page.getByTestId('shared-crews-card').boundingBox()
    await page.screenshot({ path: join(OUT, `card-seen-failed-${theme}.png`), clip: { x: card.x - 8, y: card.y - 8, width: card.width + 16, height: card.height + 16 } })
    await context.close()
  }
  {
    const { context, page } = await openPage(theme, { route: '/settings/notifications/sources' })
    const row = page.getByText('Team Crews', { exact: true }).first()
    await row.waitFor({ timeout: 30000 })
    await row.scrollIntoViewIfNeeded()
    await page.waitForTimeout(500)
    const tip = row.locator('xpath=ancestor::div[1]').getByRole('button').first()
    if (await tip.count()) { await tip.hover(); await page.waitForTimeout(500) }
    check(`[settings/${theme}] Team Crews channel row`, await row.isVisible())
    const rb = await row.boundingBox()
    await page.screenshot({ path: join(OUT, `settings-${theme}.png`), clip: { x: Math.max(0, rb.x - 40), y: Math.max(0, rb.y - 120), width: 760, height: 240 } })
    await context.close()
  }
}

await browser.close()
srv.close()
if (failed) {
  console.error('one or more frames did not show what they claim')
  process.exit(1)
}
console.log(`wrote ${OUT}`)
