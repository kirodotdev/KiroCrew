/**
 * Screenshot harness for Settings > Remote Crew's Shared Crews cards.
 *
 * Four frames, one per state a reader has to see:
 *
 *  1. The list: two Crews (one online, one offline) under the Shared Crews
 *     heading, and the "Set up a new Shared Crew" card with its console link.
 *  2. The empty list: the empty text, with the setup card still there.
 *  3. A failed read: one inline error with Ask the agent and Try again.
 *  4. A refused Open: the bridge refuses "Platform team", and its row shows the
 *     reason with Ask the agent.
 *
 * The desktop bridge (`window.relayCrewAPI`) is stubbed so the relay hook reads
 * the list; every /api/** call is answered from fixtures, with made-up names.
 *
 * Usage: npm run build && node scripts/capture-shared-crews-card.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/shared-crews-card'
mkdirSync(OUT, { recursive: true })

const SSO = { state: 'ok', seconds_remaining: 72000, expires_at: null, reason: 'valid' }
const CREWS = [
  { endpointId: 'ep-1', name: 'Platform team', crewType: 'TEAM', presence: 'CONNECTED', relayConnectionStatus: 'CONNECTED', crewUrl: 'https://ep-1.relay.example', lastSeenAt: null },
  { endpointId: 'ep-2', name: 'Data pipeline', crewType: 'TEAM', presence: 'GONE', relayConnectionStatus: 'DISCONNECTED', crewUrl: 'https://ep-2.relay.example', lastSeenAt: null },
]

const card = (page) => page.getByTestId('shared-crews-card')

let failures = 0
const fail = (msg) => { console.error(`FAIL: ${msg}`); failures++ }

const { srv, base } = await serveDist()
const browser = await chromium.launch()

async function frame(name, crewsAnswer, { openRefusal = null, before = null } = {}) {
  const context = await browser.newContext({ viewport: { width: 1200, height: 1000 }, deviceScaleFactor: 2 })
  const page = await context.newPage()
  await page.addInitScript((refusal) => {
    window.relayCrewAPI = { open: async () => (refusal ? { ok: false, error: refusal } : { ok: true }) }
  }, openRefusal)
  await stubDashboardApi(page, {
    extra: async (path, route) => {
      if (path === '/api/relay-viewer/crews') {
        await route.fulfill(crewsAnswer)
        return true
      }
      if (path === '/api/instances') {
        await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ active: true, instances: [], warm_set_cap: 5, sso: SSO }) })
        return true
      }
      if (path === '/api/cloud/launch') {
        await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ jobs: [] }) })
        return true
      }
      if (path.startsWith('/api/cloud/')) {
        await route.fulfill({ status: 200, contentType: 'application/json', body: '{}' })
        return true
      }
      return false
    },
  })
  logPageProblems(page)
  await page.goto(`${base}/settings?tab=instances`, { waitUntil: 'domcontentloaded' })
  const card = page.getByTestId('shared-crews-card')
  await card.waitFor({ timeout: 20000 })
  await page.waitForTimeout(500)
  if (before) await before(page)
  const setup = page.getByTestId('shared-crews-setup-card')
  await setup.scrollIntoViewIfNeeded()
  const a = await card.boundingBox()
  const b = await setup.boundingBox()
  const pad = 12
  await page.screenshot({
    path: `${OUT}/${name}.png`,
    clip: { x: Math.max(0, a.x - pad), y: Math.max(0, a.y - pad), width: a.width + 2 * pad, height: b.y + b.height - a.y + 2 * pad },
  })
  console.log(`wrote ${name}`)
  return { page, context }
}

{
  const { page, context } = await frame('01-shared-crews-list', { status: 200, contentType: 'application/json', body: JSON.stringify({ crews: CREWS }) })
  const rows = await page.getByTestId('shared-crew-row').count()
  if (rows !== 2) fail(`expected 2 rows, saw ${rows}`)
  const link = page.getByRole('link', { name: 'Open the AWS Management Console' })
  if ((await link.getAttribute('target')) !== '_blank') fail('the console link must open outside the dashboard')
  await context.close()
}
{
  const { page, context } = await frame('02-shared-crews-empty', { status: 200, contentType: 'application/json', body: JSON.stringify({ crews: [] }) })
  if (!(await page.getByTestId('shared-crews-empty').isVisible())) fail('the empty text is missing')
  await context.close()
}
{
  const { page, context } = await frame('03-shared-crews-error', { status: 502, contentType: 'application/json', body: JSON.stringify({ error: 'CPS failed', code: 'SERVICE_ERROR' }) })
  if ((await page.getByTestId('shared-crews-refusal').count()) !== 1) fail('expected exactly one inline error')
  if (!(await page.getByTestId('shared-crews-retry').isVisible())) fail('Try again is missing')
  if (!(await card(page).getByRole('button', { name: /Ask the agent/ }).isVisible())) fail('the list error has no Ask the agent')
  await context.close()
}
{
  const { page, context } = await frame(
    '04-shared-crew-open-refused',
    { status: 200, contentType: 'application/json', body: JSON.stringify({ crews: CREWS }) },
    {
      openRefusal: 'CREW_ACCESS_NOT_GRANTED',
      before: async (pg) => {
        await pg.getByRole('button', { name: 'Open Platform team' }).click()
        await pg.getByTestId('shared-crew-refusal').waitFor({ timeout: 10000 })
        await pg.waitForTimeout(300)
      },
    },
  )
  const text = await page.getByTestId('shared-crew-refusal').innerText()
  if (!/You no longer have access to this Crew\./.test(text)) fail(`row refusal wording: ${JSON.stringify(text)}`)
  if (!(await card(page).getByRole('button', { name: /Ask the agent/ }).isVisible())) fail('the row refusal has no Ask the agent')
  await context.close()
}

await browser.close()
srv.close()
if (failures) {
  console.error(`${failures} check(s) failed`)
  process.exit(1)
}
