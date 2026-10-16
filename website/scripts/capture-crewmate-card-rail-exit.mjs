/**
 * Leaving the New crewmate card through the nav rail — evidence frames.
 *
 *   01-card-typed-<theme>      the New crewmate card open in place of Radar's
 *                              chat, a name typed into it
 *   02-rail-stay-<theme>       the rail's Crewmates row clicked; the leave
 *                              confirm WAS raised ("Leave this page? Your
 *                              unsaved crewmate draft will be lost.") and was
 *                              answered Stay: the card and its text are still
 *                              here
 *   03-rail-leave-<theme>      the same click answered Leave: the card is
 *                              closed and Radar's chat is back (on a build with
 *                              the defect the card stays open after Leave and
 *                              this frame records the mismatch)
 *   04-rail-pristine-<theme>   an UNTOUCHED card and the same click: no confirm
 *                              (nothing typed to lose), the card closes
 *   05-empty-same-address-<theme>  an EMPTY roster: the card opened from the
 *                              hero at a bare /members, a name typed, the rail's
 *                              Crewmates row clicked while the address is already
 *                              /members (the rail asks nothing there): the page
 *                              asks for itself, Stay keeps the text, Leave closes
 *                              the card and the hero is the page again
 *
 * Runs the REAL built SPA behind `serveDist` with every `/api/**` answered from
 * fixtures (`stubDashboardApi`): the real rail row asks the real leave channel.
 * `window.confirm` is replaced before the app boots: it records each message and
 * answers from `window.__confirmAnswer`, so the frames prove which clicks asked
 * and what each answer did.
 *
 * Usage:
 *   cd website && npm run build && node scripts/capture-crewmate-card-rail-exit.mjs [outDir] [distDir]
 *
 * `distDir` defaults to this checkout's `website/dist`. Point it at a build of
 * the base branch to reproduce the defect: frame 03 then records the card still
 * open after Leave, and the run exits 1.
 */
import { chromium } from 'playwright'
import { mkdirSync, readFileSync } from 'node:fs'
import { join } from 'node:path'

import { json } from './lib/boot-api.mjs'
import { serveDist } from './lib/serve-dist.mjs'
import { stubDashboardApi, logPageProblems } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || join(process.env.KIROCREW_SCRATCH || '/tmp', 'crewmate-card-rail-exit')
const DIST = process.argv[3]
mkdirSync(OUT, { recursive: true })

const now = Math.floor(Date.now() / 1000)
const RADAR = {
  name: 'Radar', slug: 'radar', bound: true, slot_key: 'member-radar', running: false,
  kiro_agent: 'kirocrew', workspace: 'default', memory_store: 'radar', memory_version: 2, memory_owner: 'Radar',
  model: '', description: 'Triage new GitHub issues every morning', source: 'kirocrew',
  last_active_ts: now - 30, last_message: 'Quiet morning: three new issues, one needs a decision.',
}
const SLOTS = [{ key: 'member-radar', title: 'Radar', mode: 'member', created: new Date((now - 86400) * 1000).toISOString(), last_ts: new Date((now - 30) * 1000).toISOString(), running: false, agent: 'Radar' }]
// The Crewmates rail row is preview-gated (`mc-preview-crew`); the rail is
// expanded so its labels read in the frames.
const STORAGE = { 'mc-lang': 'en', 'mc-crewmates-onboarded': '1', 'mc-crewmates-page-entered': '1', 'mc-nav': '0', 'mc-members-panel-open': '0', 'mc-preview-crew': '1', 'mc-privacy-notice-v1': '1' }
const LEAVE_MESSAGE = 'Leave this page? Your unsaved mate draft will be lost.'
const NAME = 'Scribe'
// Mate's greeting reads the crew board on this page; the shipped empty fixture.
const CREW_BOARD = JSON.parse(readFileSync(new URL('./fixtures/crew-board-empty.json', import.meta.url), 'utf8'))

const extraFor = (members) => async (path, route) => {
  if (path === '/api/members') { await json(route, { members, default_agent: 'kirocrew' }); return true }
  if (path === '/api/crew-board') { await json(route, CREW_BOARD); return true }
  if (path === '/api/teams') { await json(route, { teams: [] }); return true }
  if (path === '/api/agents/installed') { await json(route, [{ name: 'kirocrew', kiro_agent: 'kirocrew', scope: 'global' }]); return true }
  if (path === '/api/agents/catalog') { await json(route, { agents: [{ name: 'kirocrew', selection_kind: 'template', kiro_agent: 'kirocrew', scope: 'global' }], default_agent: 'kirocrew' }); return true }
  if (path === '/api/slack/config') { await json(route, { configured: false, connected: false }); return true }
  if (/^\/api\/members\/[^/]+\/thread$/.test(path)) { await json(route, { slot_key: 'member-radar', slug: 'radar', member: 'Radar', created: false }); return true }
  if (/^\/api\/members\/[^/]+\/activity$/.test(path)) { await json(route, { slug: 'radar', member: 'Radar', capped: false, entries: [] }); return true }
  if (/^\/api\/members\/[^/]+\/panel$/.test(path)) { await json(route, { panel: null, html: null }); return true }
  return false
}

const { srv, base } = await serveDist(DIST || undefined)
const browser = await chromium.launch()
let failed = false
function check(name, ok, detail = '') {
  console.log(`${name}: ${ok ? 'OK' : 'MISMATCH'} ${detail}`)
  if (!ok) failed = true
  return ok
}

async function open(theme, scene = 'radar') {
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 1, colorScheme: theme })
  const page = await context.newPage()
  logPageProblems(page)
  const extra = extraFor(scene === 'empty' ? [] : [RADAR])
  // Before the app boots: every confirm the page raises is recorded and
  // answered from a switch the driver flips between Stay and Leave.
  await page.addInitScript(() => {
    window.__confirms = []
    window.__confirmAnswer = false
    window.confirm = (message) => { window.__confirms.push(String(message ?? '')); return window.__confirmAnswer }
  })
  await stubDashboardApi(page, { theme, extra, slots: scene === 'empty' ? [] : SLOTS, localStorageEntries: STORAGE })
  await page.goto(scene === 'empty' ? `${base}/members` : `${base}/members?member=Radar`, { waitUntil: 'domcontentloaded' })
  // The empty hero renders twice (chat column at md+, roster column below).
  await page.getByTestId(scene === 'empty' ? 'crewmate-empty-cta' : 'crewmate-switcher').locator('visible=true').first().waitFor({ state: 'visible', timeout: 30000 })
  await page.waitForTimeout(400)
  return { context, page }
}

async function assertTheme(page, theme, tag) {
  const bg = await page.evaluate(() => getComputedStyle(document.body).backgroundColor)
  const m = bg.match(/\d+/g) || []
  const lum = m.length >= 3 ? (Number(m[0]) + Number(m[1]) + Number(m[2])) / 3 : -1
  check(`${tag} theme ${theme}`, theme === 'light' ? lum > 180 : lum < 90, `body bg=${bg}`)
}

const confirms = (page) => page.evaluate(() => window.__confirms.slice())
const resetConfirms = (page) => page.evaluate(() => { window.__confirms = [] })
const setAnswer = (page, answer) => page.evaluate((a) => { window.__confirmAnswer = a }, answer)
const cardVisible = (page) => page.getByTestId('member-create-pane').evaluate(el => !el.className.split(/\s+/).includes('hidden'))
/** The rail row by its onboarding id — the real `NavItem` the shell renders. */
const railRow = (page, navId) => page.locator(`[data-onboarding-nav="${navId}"]`)
const nameField = (page) => page.locator('#crewmate-create-name')

/** The chat header's crewmate switcher holds the New crewmate door. */
async function openCard(page) {
  await page.getByTestId('crewmate-switcher').click()
  await page.getByTestId('crewmate-switcher-create').click()
  await nameField(page).waitFor({ state: 'visible' })
}

/** Click the rail's Crewmates row and let the page settle either way. */
async function clickCrewmatesRow(page) {
  await railRow(page, 'members').click()
  await page.waitForTimeout(600)
  await page.mouse.move(5, 5)
}

for (const theme of ['dark', 'light']) {
  const { context, page } = await open(theme)
  const pathname = () => page.evaluate(() => location.pathname + location.search)

  // 01 — the card with a typed name
  await openCard(page)
  await nameField(page).fill(NAME)
  await page.waitForTimeout(300)
  check(`01-card-typed-${theme} card shown`, await cardVisible(page), 'card in place of the chat')
  await assertTheme(page, theme, `01-card-typed-${theme}`)
  await page.screenshot({ path: join(OUT, `01-card-typed-${theme}.png`) })

  // 02 — rail click, answered Stay: asked once, card and text still here
  await setAnswer(page, false)
  await clickCrewmatesRow(page)
  const asked02 = await confirms(page)
  check(`02-rail-stay-${theme} asked`, asked02.length === 1 && asked02[0] === LEAVE_MESSAGE, `confirms=${JSON.stringify(asked02)}`)
  check(`02-rail-stay-${theme} card kept`, (await cardVisible(page)) && (await nameField(page).inputValue()) === NAME, `name=${await nameField(page).inputValue().catch(() => '')}`)
  check(`02-rail-stay-${theme} address kept`, (await pathname()) === '/members?member=Radar', `address=${await pathname()}`)
  await assertTheme(page, theme, `02-rail-stay-${theme}`)
  await page.screenshot({ path: join(OUT, `02-rail-stay-${theme}.png`) })

  // 03 — rail click, answered Leave: asked once, the card closes, the chat is back
  await resetConfirms(page)
  await setAnswer(page, true)
  await clickCrewmatesRow(page)
  const asked03 = await confirms(page)
  check(`03-rail-leave-${theme} asked`, asked03.length === 1 && asked03[0] === LEAVE_MESSAGE, `confirms=${JSON.stringify(asked03)}`)
  check(`03-rail-leave-${theme} card closed`, !(await cardVisible(page)), 'create pane hidden, chat column shows')
  check(`03-rail-leave-${theme} still on page`, (await pathname()).startsWith('/members'), `address=${await pathname()}`)
  await assertTheme(page, theme, `03-rail-leave-${theme}`)
  await page.screenshot({ path: join(OUT, `03-rail-leave-${theme}.png`) })

  // 04 — an untouched card and the same click: nothing to lose, no confirm, card closes
  await resetConfirms(page)
  await setAnswer(page, false)
  // On a build with the defect the card is still up from frame 03 and hides the
  // switcher; record that instead of waiting on a door that cannot open.
  if (await cardVisible(page)) check(`04-rail-pristine-${theme} card shown`, false, 'card still open from frame 03; cannot reopen blank')
  else {
    await openCard(page)
    check(`04-rail-pristine-${theme} card shown`, await cardVisible(page), 'card reopened blank')
  }
  await clickCrewmatesRow(page)
  check(`04-rail-pristine-${theme} no confirm`, (await confirms(page)).length === 0, `confirms=${JSON.stringify(await confirms(page))}`)
  check(`04-rail-pristine-${theme} card closed`, !(await cardVisible(page)), 'create pane hidden')
  await assertTheme(page, theme, `04-rail-pristine-${theme}`)
  await page.screenshot({ path: join(OUT, `04-rail-pristine-${theme}.png`) })
  await context.close()

  // 05 — empty roster, same address: the rail asks nothing, the page asks itself
  {
    const tag = `05-empty-same-address-${theme}`
    const { context: ctx, page: pg } = await open(theme, 'empty')
    const address = () => pg.evaluate(() => location.pathname + location.search)
    const confirmsOf = () => pg.evaluate(() => window.__confirms.slice())
    await pg.getByTestId('crewmate-empty-cta').locator('visible=true').first().click()
    await nameField(pg).waitFor({ state: 'visible' })
    await nameField(pg).fill(NAME)
    check(`${tag} bare address`, (await address()) === '/members', `address=${await address()}`)
    await setAnswer(pg, false)
    await railRow(pg, 'members').click()
    await pg.waitForTimeout(600)
    const asked = await confirmsOf()
    check(`${tag} asked once`, asked.length === 1 && asked[0] === LEAVE_MESSAGE, `confirms=${JSON.stringify(asked)}`)
    check(`${tag} stay keeps text`, (await cardVisible(pg)) && (await nameField(pg).inputValue()) === NAME, `name=${await nameField(pg).inputValue().catch(() => '')}`)
    await pg.evaluate(() => { window.__confirms = [] })
    await setAnswer(pg, true)
    await railRow(pg, 'members').click()
    await pg.waitForTimeout(600)
    check(`${tag} leave asked once`, (await confirmsOf()).length === 1, `confirms=${JSON.stringify(await confirmsOf())}`)
    check(`${tag} leave closes`, !(await cardVisible(pg)), 'create pane hidden')
    await pg.getByTestId('crewmate-empty-cta').locator('visible=true').first().waitFor({ state: 'visible' })
    await pg.mouse.move(5, 5)
    await assertTheme(pg, theme, tag)
    await pg.screenshot({ path: join(OUT, `${tag}.png`) })
    await ctx.close()
  }
}

await browser.close()
srv.close()
if (failed) {
  console.error('CAPTURE FAILED: at least one frame did not match its asserted state')
  process.exit(1)
}
console.log('all frames verified')
