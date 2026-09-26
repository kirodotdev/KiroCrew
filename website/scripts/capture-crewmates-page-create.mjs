/**
 * Crewmates page create flow — evidence frames for the PR.
 *
 *   01-empty-<theme>    zero crewmates: the hero in the chat column
 *   02-dialog-<theme>   the real New crewmate dialog, opened from the hero,
 *                       Name + job typed into the real form
 *   02b-dialog-advanced-<theme>  the same dialog with Advanced unfolded
 *                       (workspace / model / triggers / session colour)
 *   02c-dialog-new-workspace-<theme>  nested New workspace form over the
 *                       inert New crewmate parent
 *   03-created-<theme>  Radar is the only row; its chat is open with the
 *                       seeded first turn and Radar's greeting
 *   04-empty-mobile-<theme>  390x844: the hero inside the roster list, the
 *                       below-md copy (the chat column is hidden there)
 *   05-post-create-error-<theme>  the create landed but the roster re-read
 *                       failed: the notice + Try again above the chat column
 *   05b-post-create-error-mobile-<theme>  390x844, the same failure after a
 *                       FIRST create: the roster yields, the notice + Refresh
 *                       the list are the screen (nothing off-screen to the right)
 *   05c-post-create-error-existing-mobile-<theme>  390x844, the same failure
 *                       with ANOTHER crewmate already on the roster: the
 *                       notice carries a dismiss, so the existing chat stays
 *                       reachable; dismissing shows the old list again
 *   06-greeting-failed-<theme>  the create and the roster re-read landed, the
 *                       seeded first turn was REFUSED: Radar's chat is open
 *                       under a notice with a dismiss and "Send it again"
 *   06b-greeting-failed-mobile-<theme>  390x844: the same refused greeting
 *                       after the header back closed Radar's chat: the notice
 *                       sits in the roster (the screen), its retry and dismiss
 *                       on screen, the header + held until one of them acts
 *   07a-dialog-name-required-<theme>  Create pressed on a blank name: the hint
 *                       under the field, no request
 *   07b-dialog-name-taken-<theme>  the server's 409 agent_exists, said in the
 *                       dialog with the name
 *   07c-dialog-create-failed-<theme>  a 500 on the create (server body
 *                       "config lock held"), said in the product's words
 *   07d-dialog-options-failed-<theme>  the installed-agents read failed: the
 *                       defaults are shown with the notice
 *   07e-dialog-creating-<theme>  the create in flight: "Creating…", fields
 *                       and Cancel disabled
 *   07f-dialog-name-invalid-<theme>  a name the roster grammar would drop
 *                       ("Release Radar"): the hint under the field, no request
 *   07g-dialog-unconfirmed-<theme>  the create request dropped with no answer
 *   07i-dialog-credential-name-<theme>  server rejects a credential-shaped
 *                       name: ErrorNotice plus the marked Name field
 *                       AND the roster could not be re-read: said as unconfirmed
 *   08-roster-load-failed-<theme>  the roster's first read failed: at >= md
 *                       the chat column (no chat open) carries the one notice
 *                       instead of a blank pane or the empty hero
 *   08b-roster-load-failed-mobile-<theme>  390x844: the roster column carries
 *                       the notice, the ask link on its own line under it
 *
 * Real components against stubbed /api (Vite + Playwright); the backend is
 * exercised by CI only.
 *
 * Usage:
 *   cd website && node scripts/capture-crewmates-page-create.mjs http://127.0.0.1:6931 ../temp-screenshots/crewmates-page-create
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6931'
const OUT = process.argv[3] || '../temp-screenshots/crewmates-page-create'
mkdirSync(OUT, { recursive: true })

const RADAR = {
  name: 'Radar', slug: 'radar', bound: true, slot_key: 'member-radar', running: false,
  kiro_agent: 'kirocrew', workspace: 'default', memory_store: 'radar', memory_version: 2, memory_owner: 'Radar',
  model: '', description: 'Triage new GitHub issues every morning', source: 'kirocrew',
  last_active_ts: Math.floor(Date.now() / 1000) - 30,
  last_message: "Hi! I'm Radar. Each morning I'll read the new issues, sort them by what they need, and bring you only the ones that need a decision.",
}

// The crewmate that already exists in the `roster-fail-existing` scene: a
// failed re-read must not lock its chat away below md.
const ONCALL = {
  ...RADAR, name: 'Oncall', slug: 'oncall', slot_key: 'member-oncall', memory_store: 'oncall', memory_owner: 'Oncall',
  description: 'Watches the pager and drafts the first reply', last_message: 'Quiet night: two pages, both auto-resolved.',
}

const browser = await chromium.launch()
let failed = false
function check(name, ok, detail) {
  console.log(`${name}: ${ok ? 'OK' : 'MISMATCH'} ${detail}`)
  if (!ok) failed = true
  return ok
}

async function newPage(theme, scene, viewport = { width: 1440, height: 900 }) {
  const page = await browser.newPage({ viewport, deviceScaleFactor: 1 })
  const members = scene === 'done' ? [RADAR] : scene === 'roster-fail-existing' ? [ONCALL] : []
  let rosterReads = 0
  await page.route(u => new URL(u).pathname.startsWith('/api/'), route => {
    const path = new URL(route.request().url()).pathname
    const json = (body) => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) })
    if (path === '/api/members') {
      rosterReads += 1
      // roster-load-fail: the ARRIVAL read fails; nothing was created, the
      // page has no roster to show and no chat to open.
      if (scene === 'roster-load-fail' && rosterReads === 1) return route.fulfill({ status: 500, contentType: 'application/json', body: '{"error":"roster unavailable"}' })
      // roster-fail: the first read (arrival) is empty and fine; the re-read
      // after the create rejects, which is the failure the notice is about.
      if ((scene === 'roster-fail' || scene === 'roster-fail-existing' || scene === 'unconfirmed') && rosterReads > 1) return route.fulfill({ status: 500, contentType: 'application/json', body: '{"error":"roster unavailable"}' })
      // greeting-fail: the re-read after the create finds Radar; only the
      // seeded first turn is refused (POST /api/chat above).
      // A refused greeting produced no reply, so the roster row has no preview.
      if (scene === 'greeting-fail' && rosterReads > 1) return json({ members: [{ ...RADAR, last_message: '', last_active_ts: null }], default_agent: 'kirocrew' })
      return json({ members, default_agent: 'kirocrew' })
    }
    if (path === '/api/agents' && route.request().method() === 'POST') {
      if (scene === 'taken') return route.fulfill({ status: 409, contentType: 'application/json', body: JSON.stringify({ error: "Agent 'Radar' already exists", code: 'agent_exists' }) })
      if (scene === 'credential') return route.fulfill({ status: 400, contentType: 'application/json', body: JSON.stringify({ error: 'credential shaped name', code: 'credential_shaped_name' }) })
      // A realistic server sentence, so the frame proves the dialog does
      // not echo it: the copy under test is the product's, not this body's.
      if (scene === 'create-fail') return route.fulfill({ status: 500, contentType: 'application/json', body: '{"error":"config lock held"}' })
      if (scene === 'slow') return new Promise(() => {}) // never answers: the in-flight frame
      if (scene === 'unconfirmed') return route.abort('connectionfailed') // no answer at all
      return json({ ok: true })
    }
    if (path === '/api/chat' && route.request().method() === 'POST') {
      if (scene === 'greeting-fail') return route.fulfill({ status: 503, contentType: 'application/json', body: '{"error":"backend unavailable"}' })
      return json({ ok: true })
    }
    if (path === '/api/crons') return json({ jobs: [] })
    if (path === '/api/webhooks') return json({ tokens: [] })
    if (path === '/api/agents') return json({ agents: [], default_agent: 'kirocrew' })
    if (path === '/api/agents/catalog' && scene === 'options-fail') return route.fulfill({ status: 503, contentType: 'application/json', body: '{"error":"Agent choices could not be loaded. Retry the catalog.","code":"agent_catalog_unavailable"}' })
    if (path === '/api/agents/catalog') return json({
      agents: [
        { name: 'kirocrew', selection_kind: 'template', kiro_agent: 'kirocrew', scope: 'global' },
        { name: 'kirocrew-autofix', selection_kind: 'template', kiro_agent: 'kirocrew-autofix', scope: 'global' },
        { name: 'kirocrew-research', selection_kind: 'template', kiro_agent: 'kirocrew-research', scope: 'global' },
      ],
      default_agent: 'kirocrew',
    })
    if (path === '/api/workspaces') return json({ workspaces: [{ name: 'default' }] })
    if (path === '/api/config/default-agent') return json({ default_agent: 'kirocrew' })
    if (path === '/api/autonudge') return json({ enabled: true, loops: [] })
    const thread = path.match(/^\/api\/members\/([^/]+)\/thread$/)
    if (thread) {
      const slug = decodeURIComponent(thread[1]).toLowerCase()
      return json({ slot_key: `member-${slug}`, slug, member: 'Radar', created: false })
    }
    if (/^\/api\/members\/[^/]+\/activity$/.test(path)) return json({ slug: 'radar', member: 'Radar', capped: false, entries: [] })
    if (/^\/api\/members\/[^/]+\/panel$/.test(path)) return json({ panel: null, html: null })
    if (/^\/api\/chat\/slots\/[^/]+$/.test(path)) {
      return json({ key: 'member-radar', title: 'Radar', running: false, messages: [] })
    }
    if (/\/api\/chat\/(tags|pins|folders|tag-columns)$/.test(path)) return route.fulfill({ status: 200, contentType: 'application/json', body: '[]' })
    const isList = /commands|skills|agents$|sessions|files|history|models|artifacts|folders|slots$/.test(path)
    return route.fulfill({ status: 200, contentType: 'application/json', body: isList ? '[]' : '{}' })
  })
  await page.goto(`${BASE}/capture/crewmates-page-create.html?theme=${theme}&scene=${scene}`)
  await page.waitForSelector('[data-capture-root]')
  await page.getByText('Crewmates', { exact: true }).first().waitFor()
  return page
}

async function assertTheme(page, theme, tag) {
  const bg = await page.evaluate(() => getComputedStyle(document.body).backgroundColor)
  const m = bg.match(/\d+/g) || []
  const lum = m.length >= 3 ? (Number(m[0]) + Number(m[1]) + Number(m[2])) / 3 : -1
  check(`${tag} theme ${theme}`, theme === 'light' ? lum > 180 : lum < 90, `body bg=${bg}`)
}

async function assertClean(page, tag) {
  const dialogs = await page.getByRole('dialog').count()
  check(`${tag} no stray dialog`, dialogs === 0, `dialogs=${dialogs}`)
  const rawKeys = await page.getByText(/pages\.[a-zA-Z]+\./).count()
  check(`${tag} no raw i18n keys`, rawKeys === 0, `raw=${rawKeys}`)
  const legacy = await page.getByText(/crew member|agent template|pick a member/i).count()
  check(`${tag} vocabulary`, legacy === 0, `legacy-noun hits=${legacy}`)
}

for (const theme of ['dark', 'light']) {
  // 01 — empty
  {
    const page = await newPage(theme, 'empty')
    const hero = page.locator('section [data-testid=crewmate-empty-hero]')
    await hero.waitFor()
    check(`01-empty-${theme} headline`, await hero.getByText('No crewmates yet', { exact: true }).isVisible(), 'hero headline')
    check(`01-empty-${theme} sentence`, await hero.getByText(/keeps working while you are away/).isVisible(), 'hero sentence')
    check(`01-empty-${theme} cta`, await hero.getByTestId('crewmate-empty-cta').isVisible(), 'New crewmate button')
    check(`01-empty-${theme} one door`, (await page.getByTestId('member-add').count()) === 0, 'no header + while the hero is the create door')
    check(`01-empty-${theme} count`, (await page.getByTestId('member-count').textContent()) === '0 crewmates', `count=${await page.getByTestId('member-count').textContent()}`)
    // Below-md copy of the hero must not show on a wide viewport.
    const rosterHero = await page.locator('ul [data-testid=crewmate-empty-hero]').isVisible().catch(() => false)
    check(`01-empty-${theme} one hero`, !rosterHero, `roster copy visible=${rosterHero}`)
    await assertClean(page, `01-empty-${theme}`)
    await assertTheme(page, theme, `01-empty-${theme}`)
    await page.screenshot({ path: `${OUT}/01-empty-${theme}.png` })
    await page.close()
  }
  // 02 — dialog, opened from the hero's real button, filled through the real form
  {
    const page = await newPage(theme, 'empty')
    await page.locator('section [data-testid=crewmate-empty-cta]').click()
    const dlg = page.getByRole('dialog')
    await dlg.waitFor()
    await dlg.getByLabel('Name').fill('Radar')
    await dlg.getByLabel('What it looks after').fill('Triage new GitHub issues every morning')
    check(`02-dialog-${theme} title`, await dlg.getByText('New crewmate', { exact: true }).isVisible(), 'dialog title')
    check(`02-dialog-${theme} built from`, await dlg.getByText('Built from', { exact: true }).isVisible() && await dlg.getByRole('combobox', { name: 'Built from' }).getByText('kirocrew — the standard setup (default)').isVisible(), 'Built from + default option')
    check(`02-dialog-${theme} job hint`, await dlg.getByText(/on the Schedule page\.$/).isVisible(), 'HEAD wording of the job hint')
    check(`02-dialog-${theme} name placeholder`, (await dlg.getByLabel('Name').getAttribute('placeholder')) === 'e.g. Radar or release-radar', 'placeholder shows the no-spaces grammar')
    check(`02-dialog-${theme} advanced`, (await dlg.getByTestId('crewmate-create-advanced-toggle').getAttribute('aria-expanded')) === 'false', 'Advanced folded')
    check(`02-dialog-${theme} submit`, await dlg.getByRole('button', { name: 'Create crewmate' }).isVisible(), 'primary button')
    check(`02-dialog-${theme} no options error`, (await dlg.getByTestId('crewmate-create-options-error').count()) === 0, 'options loaded')
    await page.waitForTimeout(400) // modal entrance motion
    const rawKeys = await page.getByText(/pages\.[a-zA-Z]+\./).count()
    check(`02-dialog-${theme} no raw i18n keys`, rawKeys === 0, `raw=${rawKeys}`)
    await assertTheme(page, theme, `02-dialog-${theme}`)
    await page.screenshot({ path: `${OUT}/02-dialog-${theme}.png` })
    await page.close()
  }
  // 02b — the same dialog, Advanced unfolded. Taller viewport: the unfolded
  // dialog is ~810px and the modal caps at 90vh, so at 900 the last field
  // (session colour) sits below the fold inside the modal's own scroll.
  {
    const page = await newPage(theme, 'empty', { width: 1440, height: 1100 })
    await page.locator('section [data-testid=crewmate-empty-cta]').click()
    const dlg = page.getByRole('dialog')
    await dlg.waitFor()
    await dlg.getByLabel('Name').fill('Radar')
    await dlg.getByLabel('What it looks after').fill('Triage new GitHub issues every morning')
    await dlg.getByTestId('crewmate-create-advanced-toggle').click()
    const adv = dlg.getByTestId('crewmate-create-advanced')
    await adv.waitFor()
    check(`02b-dialog-advanced-${theme} expanded`, (await dlg.getByTestId('crewmate-create-advanced-toggle').getAttribute('aria-expanded')) === 'true', 'aria-expanded=true')
    check(`02b-dialog-advanced-${theme} workspace`, await adv.getByRole('combobox', { name: 'Workspace' }).isVisible(), 'workspace field')
    check(`02b-dialog-advanced-${theme} model`, await adv.getByRole('combobox', { name: 'Edit default model' }).isVisible(), 'model field')
    check(`02b-dialog-advanced-${theme} triggers`, await adv.getByLabel('Triggers').isVisible(), 'triggers field')
    check(`02b-dialog-advanced-${theme} triggers hint`, await adv.getByText(/^Situations when Kiro Crew should hand a task to this crewmate/).isVisible(), 'HEAD wording of the triggers hint (when-to-use, no schedule contrast)')
    check(`02b-dialog-advanced-${theme} no stale hint`, (await dlg.getByText(/not a schedule/).count()) === 0, 'old routing-vs-schedule sentence gone')
    const colour = adv.getByLabel('Session color hex value')
    check(`02b-dialog-advanced-${theme} colour`, await colour.isVisible(), 'session colour field')
    const box = await colour.boundingBox()
    check(`02b-dialog-advanced-${theme} colour in frame`, !!box && box.y + box.height <= 1100, `colour bottom=${box ? Math.round(box.y + box.height) : 'n/a'}`)
    const legacy = await dlg.getByText(/this member|member color|crew member/i).count()
    check(`02b-dialog-advanced-${theme} vocabulary`, legacy === 0, `legacy-noun hits=${legacy}`)
    await page.waitForTimeout(500) // disclosure + modal motion
    await assertTheme(page, theme, `02b-dialog-advanced-${theme}`)
    await page.screenshot({ path: `${OUT}/02b-dialog-advanced-${theme}.png` })
    await page.close()
  }
  // 02c — nested New workspace owns the active layer above its inert parent.
  {
    const page = await newPage(theme, 'empty', { width: 1440, height: 1100 })
    await page.locator('section [data-testid=crewmate-empty-cta]').click()
    const parent = page.getByRole('dialog')
    await parent.waitFor()
    await parent.getByTestId('crewmate-create-advanced-toggle').click()
    await parent.getByRole('combobox', { name: 'Workspace' }).click()
    await page.getByRole('option', { name: '+ New workspace…' }).click()
    const nested = page.getByRole('dialog', { name: 'Create workspace' })
    await nested.waitFor()
    await page.waitForTimeout(400)
    const inertParent = page.locator('[role="dialog"][inert]')
    check(`02c-dialog-new-workspace-${theme} nested`, await nested.isVisible(), 'New workspace form visible')
    check(`02c-dialog-new-workspace-${theme} parent inert`, (await inertParent.count()) === 1, 'one inert parent dialog')
    check(`02c-dialog-new-workspace-${theme} overlay`, (await page.locator('.z-\\[109\\]').count()) === 1, 'nested overlay above parent')
    check(`02c-dialog-new-workspace-${theme} two dialogs`, (await page.locator('[role="dialog"]').count()) === 2, 'parent plus nested dialog')
    await assertTheme(page, theme, `02c-dialog-new-workspace-${theme}`)
    await page.screenshot({ path: `${OUT}/02c-dialog-new-workspace-${theme}.png` })
    await page.close()
  }

  // 03 — created: Radar's chat open with the greeting
  {
    const page = await newPage(theme, 'done')
    await page.getByTestId('member-thread-header').waitFor()
    await page.getByText(/I'm Radar\./).first().waitFor()
    const rows = await page.locator('[data-testid=member-roster] li').count()
    check(`03-created-${theme} one row`, rows === 1, `rows=${rows}`)
    check(`03-created-${theme} count`, (await page.getByTestId('member-count').textContent()) === '1 crewmate', `count=${await page.getByTestId('member-count').textContent()}`)
    check(`03-created-${theme} greeting`, await page.getByText(/I'm Radar\./).first().isVisible(), 'greeting visible')
    check(`03-created-${theme} seed`, await page.getByText(/Your job: Triage new GitHub issues/).first().isVisible(), 'seeded first turn visible')
    // The follow-up (re-read, chat open, greeting) is over: the + is free again.
    await page.getByTestId('member-add').waitFor()
    check(`03-created-${theme} add freed`, await page.getByTestId('member-add').isEnabled(), 'header + enabled once the greeting landed')
    await assertClean(page, `03-created-${theme}`)
    await page.waitForTimeout(400)
    await assertTheme(page, theme, `03-created-${theme}`)
    await page.screenshot({ path: `${OUT}/03-created-${theme}.png` })
    await page.close()
  }
}

// 05 — post-create failure: the create landed, the roster re-read did not
for (const theme of ['dark', 'light']) {
  const page = await newPage(theme, 'roster-fail')
  await page.locator('section [data-testid=crewmate-empty-cta]').click()
  const dlg = page.getByRole('dialog')
  await dlg.waitFor()
  await dlg.getByLabel('Name').fill('Radar')
  await dlg.getByLabel('What it looks after').fill('Triage new GitHub issues every morning')
  await dlg.getByRole('button', { name: 'Create crewmate' }).click()
  const notice = page.getByTestId('member-post-create-error')
  await notice.waitFor()
  await page.getByRole('dialog').waitFor({ state: 'detached' }) // exit motion
  check(`05-post-create-error-${theme} text`, await notice.getByText("Radar was created, but the list didn't refresh.").isVisible(), 'notice text')
  check(`05-post-create-error-${theme} retry`, await notice.getByTestId('member-post-create-retry').isVisible() && (await notice.getByTestId('member-post-create-retry').textContent()) === 'Refresh your crewmates', 'Refresh your crewmates')
  check(`05-post-create-error-${theme} no dismiss`, (await notice.getByRole('button', { name: /dismiss/i }).count()) === 0, 'a roster failure has no dismiss')
  const textBox = await notice.getByText("Radar was created, but the list didn't refresh.").boundingBox()
  const retryBox = await notice.getByTestId('member-post-create-retry').boundingBox()
  // Centred column: the retry sits directly under the sentence, within one line height of it.
  check(`05-post-create-error-${theme} retry under text`, !!textBox && !!retryBox && retryBox.y >= textBox.y + textBox.height && retryBox.y - (textBox.y + textBox.height) < 40, `gap=${textBox && retryBox ? Math.round(retryBox.y - (textBox.y + textBox.height)) : 'n/a'}px`)
  const noticeBox = await page.getByTestId('member-post-create-error').boundingBox()
  check(`05-post-create-error-${theme} centred`, !!noticeBox && !!textBox && Math.abs((textBox.x + textBox.width / 2) - (noticeBox.x + noticeBox.width / 2)) < 40, 'notice centred in the chat column')
  check(`05-post-create-error-${theme} no hero`, (await page.locator('section [data-testid=crewmate-empty-hero]').count()) === 0, 'hero yields to the notice')
  check(`05-post-create-error-${theme} no dialog`, (await page.getByRole('dialog').count()) === 0, 'dialog closed')
  check(`05-post-create-error-${theme} count is a dash`, ((await page.getByTestId('member-count').textContent()) || '').trim() === '\u2014', 'no stale "0 crewmates" over a list that did not refresh; same dash as a failed arrival read')
  // A second create would clear this record and drop the retry: the header
  // "+" is held, and its title says why, until the retry lands or the notice goes.
  // The cached roster is still empty, so the header + (hidden while the hero would be the door) stays away; the notice's retry is the one way forward.
  check(`05-post-create-error-${theme} no second door`, (await page.getByTestId('member-add').count()) === 0 && (await page.locator('[data-testid=crewmate-empty-cta]:visible').count()) === 0, 'no create door while the list is stale')
  await page.waitForTimeout(400)
  await assertTheme(page, theme, `05-post-create-error-${theme}`)
  await page.screenshot({ path: `${OUT}/05-post-create-error-${theme}.png` })
  await page.close()
}

// 05b — below md, first create, roster re-read failed: no chat is open, so
// only the notice keeps the column shown; the full-width roster must yield
// or the notice and its retry sit off-screen to the right (GPT round 4).
for (const theme of ['dark', 'light']) {
  const page = await newPage(theme, 'roster-fail', { width: 390, height: 844 })
  await page.locator('ul [data-testid=crewmate-empty-cta]').click()
  const dlg = page.getByRole('dialog')
  await dlg.waitFor()
  await dlg.getByLabel('Name').fill('Radar')
  await dlg.getByRole('button', { name: 'Create crewmate' }).click()
  const notice = page.getByTestId('member-post-create-error')
  await notice.waitFor()
  await page.getByRole('dialog').waitFor({ state: 'detached' })
  const retry = notice.getByTestId('member-post-create-retry')
  const box = await retry.boundingBox()
  check(`05b-post-create-error-mobile-${theme} retry on screen`, !!box && box.x >= 0 && box.x + box.width <= 390 && box.y + box.height <= 844, `retry box=${JSON.stringify(box)}`)
  check(`05b-post-create-error-mobile-${theme} text`, await notice.getByText("Radar was created, but the list didn't refresh.").isVisible(), 'notice text')
  check(`05b-post-create-error-mobile-${theme} roster yields`, !(await page.getByTestId('member-roster').isVisible()), 'roster hidden below md while the notice is up')
  check(`05b-post-create-error-mobile-${theme} no dialog`, (await page.getByRole('dialog').count()) === 0, 'dialog closed')
  await page.waitForTimeout(400)
  await assertTheme(page, theme, `05b-post-create-error-mobile-${theme}`)
  await page.screenshot({ path: `${OUT}/05b-post-create-error-mobile-${theme}.png` })
  await page.close()
}

// 05c — below md, a create over a roster that already has Oncall, re-read
// failed: the notice replaces the roster here, so it MUST be dismissable or
// Oncall's chat is locked behind the failing server (Opus round 36).
// Dismissing returns the old list (Radar joins it on the next read).
for (const theme of ['dark', 'light']) {
  const page = await newPage(theme, 'roster-fail-existing', { width: 390, height: 844 })
  await page.getByTestId('member-roster').getByText('Oncall').waitFor()
  await page.getByTestId('member-add').click()
  const dlg = page.getByRole('dialog')
  await dlg.waitFor()
  await dlg.getByLabel('Name').fill('Radar')
  await dlg.getByRole('button', { name: 'Create crewmate' }).click()
  const notice = page.getByTestId('member-post-create-error')
  await notice.waitFor()
  await page.getByRole('dialog').waitFor({ state: 'detached' })
  const tag = `05c-post-create-error-existing-mobile-${theme}`
  check(`${tag} text`, await notice.getByText("Radar was created, but the list didn't refresh.").isVisible(), 'notice text')
  check(`${tag} roster yields`, !(await page.getByTestId('member-roster').isVisible()), 'roster hidden below md while the notice is up')
  const dismiss = notice.getByRole('button', { name: /dismiss/i })
  const dismissBox = await dismiss.boundingBox()
  check(`${tag} dismiss on screen`, !!dismissBox && dismissBox.x >= 0 && dismissBox.x + dismissBox.width <= 390 && dismissBox.y + dismissBox.height <= 844, `dismiss box=${JSON.stringify(dismissBox)}`)
  check(`${tag} retry`, (await notice.getByTestId('member-post-create-retry').textContent()) === 'Refresh your crewmates', 'Refresh your crewmates')
  await page.waitForTimeout(400)
  await assertTheme(page, theme, tag)
  await page.screenshot({ path: `${OUT}/${tag}.png` })
  await dismiss.click()
  await page.getByTestId('member-post-create-error').waitFor({ state: 'detached' })
  check(`${tag} roster back`, await page.getByTestId('member-roster').getByText('Oncall').isVisible(), 'Oncall reachable again after dismiss')
  check(`${tag} old list`, (await page.getByTestId('member-roster').getByText('Radar').count()) === 0, 'Radar not on the stale list until the next read')
  check(`${tag} door open`, await page.getByTestId('member-add').isEnabled(), '+ enabled again')
  await page.close()
}

// 06 — greeting refused: chat open, notice with dismiss + "Send it again"
for (const theme of ['dark', 'light']) {
  const page = await newPage(theme, 'greeting-fail')
  await page.locator('section [data-testid=crewmate-empty-cta]').click()
  const dlg = page.getByRole('dialog')
  await dlg.waitFor()
  await dlg.getByLabel('Name').fill('Radar')
  await dlg.getByLabel('What it looks after').fill('Triage new GitHub issues every morning')
  await dlg.getByRole('button', { name: 'Create crewmate' }).click()
  const notice = page.getByTestId('member-post-create-error')
  await notice.waitFor()
  await page.getByRole('dialog').waitFor({ state: 'detached' })
  await page.getByTestId('member-thread-header').waitFor()
  check(`06-greeting-failed-${theme} text`, await notice.getByText("Radar was created, but its first message didn't send.").isVisible(), 'notice text')
  check(`06-greeting-failed-${theme} retry`, (await notice.getByTestId('member-post-create-retry').textContent()) === 'Send it again', 'Send it again')
  check(`06-greeting-failed-${theme} dismiss`, (await notice.getByRole('button', { name: "Dismiss — Send it again won't be offered after this" }).count()) === 1, 'a greeting failure can be dismissed, and the dismiss names what closing costs')
  check(`06-greeting-failed-${theme} chat open`, await page.getByTestId('member-thread-header').getByText('Radar', { exact: true }).isVisible(), "Radar's chat is open under the notice")
  check(`06-greeting-failed-${theme} no preview`, (await page.getByText(/I'm Radar\./).count()) === 0, 'no invented reply preview in the roster')
  await page.waitForTimeout(400)
  await assertTheme(page, theme, `06-greeting-failed-${theme}`)
  await page.screenshot({ path: `${OUT}/06-greeting-failed-${theme}.png` })
  await page.close()
}

// 06b — below md, the refused greeting after the header back closed the
// chat: the roster is the screen, so the notice must be ON it, or the "+"
// is held with no visible reason and no control that frees it (Opus round
// 43 — the notice used to live only in the hidden chat column).
for (const theme of ['dark', 'light']) {
  const page = await newPage(theme, 'greeting-fail', { width: 390, height: 844 })
  await page.locator('ul [data-testid=crewmate-empty-cta]').click()
  const dlg = page.getByRole('dialog')
  await dlg.waitFor()
  await dlg.getByLabel('Name').fill('Radar')
  await dlg.getByLabel('What it looks after').fill('Triage new GitHub issues every morning')
  await dlg.getByRole('button', { name: 'Create crewmate' }).click()
  const notice = page.getByTestId('member-post-create-error')
  await notice.waitFor()
  await page.getByRole('dialog').waitFor({ state: 'detached' })
  await page.getByTestId('member-thread-header').waitFor()
  await page.getByTestId('member-back').click()
  await page.getByTestId('member-thread-header').waitFor({ state: 'detached' })
  const roster = page.getByTestId('member-roster')
  await roster.getByTestId('member-post-create-error').waitFor()
  check(`06b-greeting-failed-mobile-${theme} roster shown`, await roster.isVisible(), 'roster is the screen once the chat is closed')
  check(`06b-greeting-failed-mobile-${theme} one notice`, (await page.getByTestId('member-post-create-error').count()) === 1, 'one notice, in the roster')
  check(`06b-greeting-failed-mobile-${theme} text`, await notice.getByText("Radar was created, but its first message didn't send.").isVisible(), 'notice text')
  const box = await notice.getByTestId('member-post-create-retry').boundingBox()
  check(`06b-greeting-failed-mobile-${theme} retry on screen`, !!box && box.x >= 0 && box.x + box.width <= 390 && box.y + box.height <= 844, `retry box=${JSON.stringify(box)}`)
  check(`06b-greeting-failed-mobile-${theme} dismiss`, (await notice.getByRole('button', { name: "Dismiss — Send it again won't be offered after this" }).count()) === 1, 'the dismiss that frees the + is on screen')
  check(`06b-greeting-failed-mobile-${theme} add held`, (await page.getByTestId('member-add').count()) === 1 && !(await page.getByTestId('member-add').isEnabled()), 'header + held while the greeting retry is pending')
  await page.waitForTimeout(400)
  await assertTheme(page, theme, `06b-greeting-failed-mobile-${theme}`)
  await page.screenshot({ path: `${OUT}/06b-greeting-failed-mobile-${theme}.png` })
  await page.close()
}

// 07 — the dialog's own states
async function openDialog(theme, scene) {
  const page = await newPage(theme, scene)
  await page.locator('section [data-testid=crewmate-empty-cta]').click()
  const dlg = page.getByRole('dialog')
  await dlg.waitFor()
  await page.waitForTimeout(400) // modal entrance motion
  return { page, dlg }
}
for (const theme of ['dark', 'light']) {
  {
    const { page, dlg } = await openDialog(theme, 'empty')
    await dlg.getByRole('button', { name: 'Create crewmate' }).click()
    const hint = dlg.getByText('Give your crewmate a name.', { exact: true })
    await hint.waitFor()
    check(`07a-dialog-name-required-${theme} hint`, await hint.isVisible(), 'hint under the field')
    check(`07a-dialog-name-required-${theme} invalid`, (await dlg.getByLabel('Name').getAttribute('aria-invalid')) === 'true', 'aria-invalid')
    await page.waitForTimeout(400) // the field's border-color transition (focus-ring, 0.2s) settles
    const hintRole = await dlg.getByTestId('crewmate-create-name-hint').getAttribute('role')
    const nameBorder = await dlg.getByLabel('Name').evaluate((el) => getComputedStyle(el).borderColor)
    const jobBorder = await dlg.getByLabel('What it looks after').evaluate((el) => getComputedStyle(el).borderColor)
    check(`07a-dialog-name-required-${theme} error tone`, hintRole === 'alert' && nameBorder !== jobBorder, `role=${hintRole} name-border=${nameBorder} job-border=${jobBorder}`)
    check(`07a-dialog-name-required-${theme} still open`, (await page.getByRole('dialog').count()) === 1, 'dialog stays')
    await assertTheme(page, theme, `07a-dialog-name-required-${theme}`)
    await page.screenshot({ path: `${OUT}/07a-dialog-name-required-${theme}.png` })
    await page.close()
  }
  {
    const { page, dlg } = await openDialog(theme, 'taken')
    await dlg.getByLabel('Name').fill('Radar')
    await dlg.getByRole('button', { name: 'Create crewmate' }).click()
    const err = dlg.getByTestId('crewmate-create-error')
    await err.waitFor()
    check(`07b-dialog-name-taken-${theme} text`, await err.getByText('A crewmate named Radar already exists.', { exact: true }).isVisible(), 'said with the name')
    check(`07b-dialog-name-taken-${theme} still open`, (await page.getByRole('dialog').count()) === 1, 'dialog stays')
    check(`07b-dialog-name-taken-${theme} name kept`, (await dlg.getByLabel('Name').inputValue()) === 'Radar', 'draft kept')
    await assertTheme(page, theme, `07b-dialog-name-taken-${theme}`)
    await page.screenshot({ path: `${OUT}/07b-dialog-name-taken-${theme}.png` })
    await page.close()
  }
  {
    const { page, dlg } = await openDialog(theme, 'credential')
    await dlg.getByLabel('Name').fill('token_private_value')
    await dlg.getByRole('button', { name: 'Create crewmate' }).click()
    const err = dlg.getByTestId('crewmate-create-error')
    await err.waitFor()
    await page.waitForTimeout(400)
    check(`07i-dialog-credential-name-${theme} text`, await err.getByText('That name looks like a credential or a URL carrying one. Pick a name that says who the crewmate is.', { exact: true }).isVisible(), 'plain refusal copy')
    check(`07i-dialog-credential-name-${theme} invalid`, (await dlg.getByLabel('Name').getAttribute('aria-invalid')) === 'true', 'field marked invalid')
    check(`07i-dialog-credential-name-${theme} no echo`, (await err.getByText(/token_private_value/).count()) === 0, 'credential-shaped name is not echoed')
    check(`07i-dialog-credential-name-${theme} still open`, (await page.getByRole('dialog').count()) === 1, 'dialog stays')
    await assertTheme(page, theme, `07i-dialog-credential-name-${theme}`)
    await page.screenshot({ path: `${OUT}/07i-dialog-credential-name-${theme}.png` })
    await page.close()
  }
  {
    const { page, dlg } = await openDialog(theme, 'create-fail')
    await dlg.getByLabel('Name').fill('Radar')
    await dlg.getByLabel('What it looks after').fill('Triage new GitHub issues every morning')
    await dlg.getByRole('button', { name: 'Create crewmate' }).click()
    const err = dlg.getByTestId('crewmate-create-error')
    await err.waitFor()
    check(`07c-dialog-create-failed-${theme} text`, await err.getByText("Couldn't create the crewmate. Nothing was created — try again.", { exact: true }).isVisible(), 'failure said in the product\'s words')
    check(`07c-dialog-create-failed-${theme} no server text`, (await page.getByText(/lock held/).count()) === 0, 'server body not echoed')
    check(`07c-dialog-create-failed-${theme} still open`, (await page.getByRole('dialog').count()) === 1, 'dialog stays')
    check(`07c-dialog-create-failed-${theme} name kept`, (await dlg.getByLabel('Name').inputValue()) === 'Radar', 'draft kept')
    check(`07c-dialog-create-failed-${theme} job kept`, (await dlg.getByLabel('What it looks after').inputValue()) === 'Triage new GitHub issues every morning', 'draft kept')
    await assertTheme(page, theme, `07c-dialog-create-failed-${theme}`)
    await page.screenshot({ path: `${OUT}/07c-dialog-create-failed-${theme}.png` })
    await page.close()
  }
  {
    const { page, dlg } = await openDialog(theme, 'options-fail')
    const err = dlg.getByTestId('crewmate-create-options-error')
    await err.waitFor()
    check(`07d-dialog-options-failed-${theme} text`, await err.getByText('Couldn\'t load the Built from choices; the default is shown.', { exact: true }).isVisible(), 'options notice names the list')
    check(`07d-dialog-options-failed-${theme} default kept`, await dlg.getByRole('combobox', { name: 'Built from' }).getByText('kirocrew — the standard setup (default)').isVisible(), 'built-in default still offered')
    await assertTheme(page, theme, `07d-dialog-options-failed-${theme}`)
    await page.screenshot({ path: `${OUT}/07d-dialog-options-failed-${theme}.png` })
    await page.close()
  }
  {
    const { page, dlg } = await openDialog(theme, 'slow')
    await dlg.getByLabel('Name').fill('Radar')
    await dlg.getByLabel('What it looks after').fill('Triage new GitHub issues every morning')
    // Submitted with Enter in the Name field, not the button: the Create
    // button lives in the Modal footer outside the form and is associated by
    // `form=`, which is what gives the form implicit submission at all.
    await dlg.getByLabel('Name').press('Enter')
    const busy = dlg.getByRole('button', { name: 'Creating…' })
    await busy.waitFor()
    check(`07e-dialog-creating-${theme} enter submits`, await busy.isVisible(), 'Enter in Name submitted the form')
    check(`07e-dialog-creating-${theme} busy label`, await busy.isDisabled(), 'Creating… disabled')
    check(`07e-dialog-creating-${theme} fields locked`, await dlg.getByLabel('Name').isDisabled(), 'name field disabled')
    check(`07e-dialog-creating-${theme} cancel locked`, await dlg.getByRole('button', { name: 'Cancel' }).isDisabled(), 'Cancel disabled while pending')
    check(`07e-dialog-creating-${theme} advanced locked`, await dlg.getByTestId('crewmate-create-advanced-toggle').isDisabled(), 'Advanced toggle disabled while pending (whole-form fieldset)')
    // Btn's disabled dim is a 0.2 s transition; settle before reading it and
    // shooting, or the frame shows Cancel at full strength.
    await page.waitForTimeout(400)
    const cancelOpacity = await dlg.getByRole('button', { name: 'Cancel' }).evaluate((el) => getComputedStyle(el).opacity)
    check(`07e-dialog-creating-${theme} cancel dimmed`, Number(cancelOpacity) < 0.5, `Cancel opacity=${cancelOpacity}`)
    await assertTheme(page, theme, `07e-dialog-creating-${theme}`)
    await page.screenshot({ path: `${OUT}/07e-dialog-creating-${theme}.png` })
    await page.close()
  }

  {
    const { page, dlg } = await openDialog(theme, 'empty')
    let posts = 0
    page.on('request', (r) => { if (r.method() === 'POST' && new URL(r.url()).pathname === '/api/agents') posts += 1 })
    await dlg.getByLabel('Name').fill('Release Radar')
    await dlg.getByRole('button', { name: 'Create crewmate' }).click()
    const hint = dlg.getByTestId('crewmate-create-name-hint')
    await hint.waitFor()
    await page.waitForTimeout(400) // aria-invalid border transition
    check(`07f-dialog-name-invalid-${theme} text`, await hint.getByText('Use letters, numbers, hyphens or underscores — no spaces — and start and end with a letter or number.', { exact: true }).isVisible(), 'grammar hint under the field')
    check(`07f-dialog-name-invalid-${theme} invalid`, (await dlg.getByLabel('Name').getAttribute('aria-invalid')) === 'true', 'field marked invalid')
    check(`07f-dialog-name-invalid-${theme} no request`, posts === 0, `create POSTs=${posts}`)
    check(`07f-dialog-name-invalid-${theme} no error notice`, (await dlg.getByTestId('crewmate-create-error').count()) === 0, 'a hint, not a failed request')
    await assertTheme(page, theme, `07f-dialog-name-invalid-${theme}`)
    await page.screenshot({ path: `${OUT}/07f-dialog-name-invalid-${theme}.png` })
    await page.close()
  }
  {
    const { page, dlg } = await openDialog(theme, 'unconfirmed')
    await dlg.getByLabel('Name').fill('Radar')
    await dlg.getByRole('button', { name: 'Create crewmate' }).click()
    // Its own test id: an unconfirmed create is not one of the retry-safe
    // "nothing was created" notices and must not be captured as one.
    const err = dlg.getByTestId('crewmate-create-unconfirmed')
    await err.waitFor()
    check(`07g-dialog-unconfirmed-${theme} not the failed notice`, (await dlg.getByTestId('crewmate-create-error').count()) === 0, 'distinct from create_failed')
    check(`07g-dialog-unconfirmed-${theme} lead`, await err.locator('strong', { hasText: 'Not confirmed' }).isVisible(), 'bold lead names the state')
    check(`07g-dialog-unconfirmed-${theme} text`, await err.getByText("Couldn't confirm whether the crewmate was created. Check the list before trying again.", { exact: true }).isVisible(), 'unconfirmed, not "nothing was created"')
    check(`07g-dialog-unconfirmed-${theme} still open`, (await page.getByRole('dialog').count()) === 1, 'dialog stays with the draft')
    check(`07g-dialog-unconfirmed-${theme} unlocked`, !(await dlg.getByLabel('Name').isDisabled()), 'form unlocked again')
    await page.waitForTimeout(400)
    await assertTheme(page, theme, `07g-dialog-unconfirmed-${theme}`)
    await page.screenshot({ path: `${OUT}/07g-dialog-unconfirmed-${theme}.png` })
    await page.close()
  }
}
// 08 — the roster's first read failed: list column notice + chat column notice
for (const theme of ['dark', 'light']) {
  const page = await newPage(theme, 'roster-load-fail')
  const columnNotice = page.getByTestId('member-column-load-error')
  await columnNotice.waitFor()
  const listNotice = page.getByTestId('member-roster-error')
  // At >= md the chat column carries the one notice; the roster-column copy
  // of it is for below md only, or the two read as one message doubled.
  check(`08-roster-load-failed-${theme} one notice`, (await listNotice.count()) === 1 && !(await listNotice.isVisible()), 'roster-column notice hidden at >= md')
  check(`08-roster-load-failed-${theme} column notice`, await columnNotice.isVisible() && await columnNotice.getByText('Could not load your crewmates.', { exact: false }).isVisible(), 'chat column carries the notice at >= md')
  check(`08-roster-load-failed-${theme} retry`, await page.getByTestId('member-column-load-retry').isVisible() && (await page.getByTestId('member-column-load-retry').textContent()) === 'Try again', 'plain retry beside the ask link')
  check(`08-roster-load-failed-${theme} no hero`, (await page.getByTestId('crewmate-empty-hero').count()) === 0, 'a failed read is not an empty crew')
  check(`08-roster-load-failed-${theme} count is a dash`, ((await page.getByTestId('member-count').textContent()) || '').trim() === '\u2014', 'no "0 crewmates" stated over a failed read')
  check(`08-roster-load-failed-${theme} + held`, (await page.getByTestId('member-add').isDisabled()) && (await page.getByTestId('member-add').getAttribute('title')) === 'Could not load your crewmates.', 'no create door while the names it would check against are unknown')
  check(`08-roster-load-failed-${theme} no dialog`, (await page.getByRole('dialog').count()) === 0, 'nothing opened')
  await assertClean(page, `08-roster-load-failed-${theme}`)
  await page.waitForTimeout(400)
  await assertTheme(page, theme, `08-roster-load-failed-${theme}`)
  await page.screenshot({ path: `${OUT}/08-roster-load-failed-${theme}.png` })
  await page.close()
}

// 08b — below md the chat column is hidden, so the roster column carries the
// notice; the ask link sits on its own line under the sentence (flex-wrap).
for (const theme of ['dark', 'light']) {
  const page = await newPage(theme, 'roster-load-fail', { width: 390, height: 844 })
  const listNotice = page.getByTestId('member-roster-error')
  await listNotice.waitFor()
  const msg = listNotice.getByText('Could not load your crewmates.', { exact: false })
  check(`08b-roster-load-failed-mobile-${theme} notice`, await msg.isVisible(), 'roster-column notice shown below md')
  const msgBox = await msg.boundingBox()
  const link = listNotice.getByRole('button', { name: 'Ask Kiro Crew about this (opens a chat)' })
  const linkBox = await link.boundingBox()
  check(`08b-roster-load-failed-mobile-${theme} sentence unsqueezed`, !!msgBox && msgBox.width > 150 && msgBox.height < 40, `message box=${JSON.stringify(msgBox)}`)
  check(`08b-roster-load-failed-mobile-${theme} link below`, !!msgBox && !!linkBox && linkBox.y >= msgBox.y + msgBox.height - 2, `link box=${JSON.stringify(linkBox)}`)
  check(`08b-roster-load-failed-mobile-${theme} no column notice`, !(await page.getByTestId('member-column-load-error').isVisible()), 'chat column hidden below md')
  check(`08b-roster-load-failed-mobile-${theme} retry`, await page.getByTestId('member-roster-retry').isVisible(), 'plain retry under the notice')
  check(`08b-roster-load-failed-mobile-${theme} + held`, await page.getByTestId('member-add').isDisabled(), 'no create door over an unread roster')
  await assertClean(page, `08b-roster-load-failed-mobile-${theme}`)
  await page.waitForTimeout(400)
  await assertTheme(page, theme, `08b-roster-load-failed-mobile-${theme}`)
  await page.screenshot({ path: `${OUT}/08b-roster-load-failed-mobile-${theme}.png` })
  await page.close()
}

// 04 — below md: the hero inside the roster list (dark only is enough for the
// layout question, light proves the tokens; shoot both).
for (const theme of ['dark', 'light']) {
  const page = await newPage(theme, 'empty', { width: 390, height: 844 })
  const hero = page.locator('ul [data-testid=crewmate-empty-hero]')
  await hero.waitFor()
  check(`04-empty-mobile-${theme} hero`, await hero.getByText('No crewmates yet', { exact: true }).isVisible(), 'hero in the roster list')
  check(`04-empty-mobile-${theme} cta`, await hero.getByTestId('crewmate-empty-cta').isVisible(), 'New crewmate button')
  check(`04-empty-mobile-${theme} one door`, (await page.getByTestId('member-add').count()) === 0, 'no header + while the hero is the create door')
  const desktopHero = await page.locator('section [data-testid=crewmate-empty-hero]').isVisible().catch(() => false)
  check(`04-empty-mobile-${theme} one hero`, !desktopHero, `chat-column copy visible=${desktopHero}`)
  await assertTheme(page, theme, `04-empty-mobile-${theme}`)
  await page.screenshot({ path: `${OUT}/04-empty-mobile-${theme}.png` })
  await page.close()
}

await browser.close()
if (failed) {
  console.error('CAPTURE FAILED: at least one frame did not match its asserted state')
  process.exit(1)
}
console.log('all frames verified')
