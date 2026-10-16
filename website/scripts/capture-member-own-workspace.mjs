/**
 * A new crewmate's own workspace — evidence frames for the PR.
 *
 *   01-advanced-default-<theme>  the real New crewmate dialog, Advanced unfolded,
 *                       the Workspace select CLOSED on its default pick:
 *                       "Its own workspace (created automatically)", with the
 *                       rewritten hint under the field
 *   02-advanced-open-<theme>  the same select OPEN: the "+ New workspace…"
 *                       action on top (where SimpleSelect always puts it), then
 *                       the own-workspace option, selected, above the existing
 *                       workspaces
 *   03-profile-<theme>  a created crewmate's Profile card: the Workspace tile
 *                       reads the generated `member-radar` name
 *
 * Real components against stubbed /api (Vite + Playwright); the backend is
 * exercised by CI only. Reuses the Crewmates page harness
 * (`capture/crewmates-page-create.html`).
 *
 * Usage:
 *   cd website && npx vite --host 127.0.0.1 --port 6931 --strictPort   # in another shell
 *   node scripts/capture-member-own-workspace.mjs http://127.0.0.1:6931 ../temp-screenshots/member-own-workspace
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6931'
const OUT = process.argv[3] || '../temp-screenshots/member-own-workspace'
mkdirSync(OUT, { recursive: true })

const OWN = 'Its own workspace (created automatically)'
const HINT = 'Pick an existing workspace only to share files with another mate.'

// A crewmate created by the new path: its record names the workspace the
// server allocated for it.
const RADAR = {
  name: 'Radar', slug: 'radar', bound: true, slot_key: 'member-radar', running: false,
  kiro_agent: 'kirocrew', workspace: 'member-radar', memory_store: 'member-radar', memory_version: 2, memory_owner: 'Radar',
  model: '', description: 'Triage new GitHub issues every morning', source: 'kirocrew',
  last_active_ts: Math.floor(Date.now() / 1000) - 30,
  last_message: "Hi! I'm Radar. Each morning I'll read the new issues, sort them by what they need, and bring you only the ones that need a decision.",
}

const browser = await chromium.launch()
let failed = false
function check(name, ok, detail) {
  console.log(`${name}: ${ok ? 'OK' : 'MISMATCH'} ${detail}`)
  if (!ok) failed = true
  return ok
}

async function newPage(theme, scene, viewport = { width: 1440, height: 1100 }) {
  const page = await browser.newPage({ viewport, deviceScaleFactor: 1 })
  const members = scene === 'done' ? [RADAR] : []
  await page.route(u => new URL(u).pathname.startsWith('/api/'), route => {
    const path = new URL(route.request().url()).pathname
    const json = (body) => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) })
    if (path === '/api/members') return json({ members, default_agent: 'kirocrew' })
    if (path === '/api/agents' && route.request().method() === 'POST') return json({ ok: true })
    if (path === '/api/crons') return json({ jobs: [] })
    if (path === '/api/webhooks') return json({ tokens: [] })
    if (path === '/api/agents') return json({ agents: [], default_agent: 'kirocrew' })
    if (path === '/api/agents/catalog') return json({
      agents: [
        { name: 'kirocrew', selection_kind: 'template', kiro_agent: 'kirocrew', scope: 'global' },
        { name: 'kirocrew-research', selection_kind: 'template', kiro_agent: 'kirocrew-research', scope: 'global' },
      ],
      default_agent: 'kirocrew',
    })
    // Two declared workspaces, so the open list shows the own-workspace option
    // ABOVE existing names rather than alone.
    if (path === '/api/workspaces') return json({ workspaces: [{ name: 'default' }, { name: 'research' }] })
    if (path === '/api/config/default-agent') return json({ default_agent: 'kirocrew' })
    if (path === '/api/autonudge') return json({ enabled: true, loops: [] })
    // The opened thread reads the work board for its resume summary.
    if (path === '/api/crew-board') return json({ conductor: null, conductor_alive: 'closed', items: [], take_over_available: false })
    const thread = path.match(/^\/api\/members\/([^/]+)\/thread$/)
    if (thread) {
      const slug = decodeURIComponent(thread[1]).toLowerCase()
      return json({ slot_key: `member-${slug}`, slug, member: 'Radar', created: false })
    }
    if (/^\/api\/members\/[^/]+\/activity$/.test(path)) return json({ slug: 'radar', member: 'Radar', capped: false, entries: [] })
    if (/^\/api\/members\/[^/]+\/panel$/.test(path)) return json({ panel: null, html: null })
    if (/^\/api\/chat\/slots\/[^/]+$/.test(path)) return json({ key: 'member-radar', title: 'Radar', running: false, messages: [] })
    if (/\/api\/chat\/(tags|pins|folders|tag-columns)$/.test(path)) return route.fulfill({ status: 200, contentType: 'application/json', body: '[]' })
    const isList = /commands|skills|agents$|sessions|files|history|models|artifacts|folders|slots$/.test(path)
    return route.fulfill({ status: 200, contentType: 'application/json', body: isList ? '[]' : '{}' })
  })
  await page.goto(`${BASE}/capture/crewmates-page-create.html?theme=${theme}&scene=${scene}`)
  await page.waitForSelector('[data-capture-root]')
  // With a thread open (`done`) the roster heading is hidden; its header is the ready signal.
  if (scene === 'done') await page.getByTestId('member-thread-header').waitFor()
  else await page.getByText('Mates', { exact: true }).first().waitFor()
  return page
}

async function assertTheme(page, theme, tag) {
  const bg = await page.evaluate(() => getComputedStyle(document.body).backgroundColor)
  const m = bg.match(/\d+/g) || []
  const lum = m.length >= 3 ? (Number(m[0]) + Number(m[1]) + Number(m[2])) / 3 : -1
  check(`${tag} theme`, theme === 'dark' ? lum < 80 : lum > 180, `body bg=${bg}`)
}

/** With zero crewmates the hero's "Advanced" link opens the full create form
 *  as a modal over the chat (the primary button starts the guided Meet
 *  Crewmates flow). */
async function openDialog(page) {
  await page.locator('section [data-testid=crewmate-empty-advanced]').click()
  const dlg = page.getByTestId('crewmate-create-form')
  await dlg.waitFor()
  return dlg
}

for (const theme of ['dark', 'light']) {
  // 01 / 02 — the create dialog's Advanced fold.
  {
    const page = await newPage(theme, 'empty')
    const dlg = await openDialog(page)
    await dlg.getByLabel('Name').fill('Radar')
    await dlg.getByLabel('What it looks after').fill('Triage new GitHub issues every morning')
    await dlg.getByTestId('crewmate-create-advanced-toggle').click()
    const adv = dlg.getByTestId('crewmate-create-advanced')
    await adv.waitFor()
    const select = adv.getByRole('combobox', { name: 'Workspace' })
    await select.waitFor()
    await page.waitForTimeout(300)
    check(`01-advanced-default-${theme} default pick`, (await select.innerText()).trim() === OWN, `trigger="${(await select.innerText()).trim()}"`)
    check(`01-advanced-default-${theme} hint`, await adv.getByText(HINT, { exact: true }).isVisible(), 'rewritten hint under the field')
    check(`01-advanced-default-${theme} no blank`, !/^\s*(—|-)?\s*$/.test(await select.innerText()), 'the empty value renders as its label, not a dash')
    await assertTheme(page, theme, `01-advanced-default-${theme}`)
    await page.screenshot({ path: `${OUT}/01-advanced-default-${theme}.png` })

    await select.click()
    const options = page.getByRole('option')
    await options.first().waitFor()
    const labels = (await options.allInnerTexts()).map(s => s.trim())
    const choices = labels.filter(l => l !== '+ New workspace…')
    check(`02-advanced-open-${theme} own first`, choices[0] === OWN, `options=${JSON.stringify(labels)}`)
    check(`02-advanced-open-${theme} existing after`, choices.slice(1).join(',') === 'default,research', 'declared workspaces follow it')
    check(`02-advanced-open-${theme} action kept`, labels.includes('+ New workspace…'), 'the New workspace action is still offered')
    const own = page.getByRole('option', { name: OWN })
    check(`02-advanced-open-${theme} selected`, (await own.getAttribute('data-state')) === 'checked' || (await own.getAttribute('aria-selected')) === 'true', `data-state=${await own.getAttribute('data-state')} aria-selected=${await own.getAttribute('aria-selected')}`)
    await page.screenshot({ path: `${OUT}/02-advanced-open-${theme}.png` })
    await page.close()
  }

  // 03 — a created crewmate's Profile card names its own workspace.
  {
    const page = await newPage(theme, 'done', { width: 1440, height: 900 })
    const pill = page.getByTestId('member-identity-pill')
    await pill.waitFor({ state: 'visible', timeout: 30000 })
    await pill.click()
    const card = page.getByTestId('crew-profile-card')
    await card.waitFor({ state: 'visible', timeout: 10000 })
    // The card opens on Sessions; the Workspace tile is on the Profile tab.
    await page.getByTestId('crew-profile-tabs').getByRole('tab', { name: 'Profile' }).click()
    const tile = page.getByTestId('crew-profile-workspace')
    await tile.waitFor({ state: 'visible', timeout: 10000 })
    await page.waitForTimeout(700)
    check(`03-profile-${theme} workspace tile`, (await tile.innerText()).includes('member-radar'), `tile="${(await tile.innerText()).replace(/\s+/g, ' ').trim()}"`)
    await assertTheme(page, theme, `03-profile-${theme}`)
    await page.screenshot({ path: `${OUT}/03-profile-${theme}.png` })
    await page.close()
  }
}

await browser.close()
if (failed) {
  console.error('CAPTURE FAILED: at least one frame did not match its asserted state')
  process.exit(1)
}
console.log('all frames verified')
