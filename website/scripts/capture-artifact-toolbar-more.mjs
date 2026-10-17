/**
 * Screenshot harness for the artifact detail toolbar's "More" overflow menu.
 *
 * Runs the REAL built SPA (website/dist) behind a tiny in-process static server
 * and answers every /api/** call from fixtures via Playwright route interception
 * (gateway-free — no kiro-cli, no live backend).
 *
 * The toolbar row holds at most two actions (Edit + companion chat in view mode)
 * and everything else lives in one labelled "More" menu (`max-two-buttons-per-row`).
 *
 * Frames, each in light and dark:
 *   <mode>-01-toolbar         the header row, menu closed
 *   <mode>-02-more-open       the More menu open
 *   <mode>-03-send-submenu    More → Send to a session submenu open
 *   <mode>-04-edit-more       editing: Save + Cancel in the row, More open
 *   <mode>-05-send-failed     a failed New session create, shown as an ErrorNotice
 *   <mode>-06-prefilled       the picked session ("Release prep") with the reference in its composer
 *   <mode>-07-historical      a historical version: Revert takes Edit's place in the row
 *   <mode>-08-popped-out      popped out: More offers Focus + Bring back instead of Pop out
 *   <mode>-09-mdnb-medium     the Notes app's reading-width toggle (icon, medium) with its tip
 *   <mode>-10-mdnb-full       the same toggle at full width, pressed, with its tip
 *
 * Usage: node scripts/capture-artifact-toolbar-more.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'
import { MDNB_VAULT_ID, mdnbApiStub, mdnbNoteDoc, mdnbNotesList, notePaneClip } from './lib/mdnb-fixtures.mjs'

const OUT = process.argv[2] || '../temp-screenshots/artifact-toolbar-more'
mkdirSync(OUT, { recursive: true })

const ARTIFACT = {
  slug: 'release-checklist',
  name: 'Release checklist',
  kind: 'markdown',
  source: 'chat',
  session_title: 'Release prep',
  description: '',
  tags: ['release'],
  version: 3,
  pinned: false,
  created_at: '2026-10-01T10:00:00.000000+00:00',
  updated_at: '2026-10-08T21:00:00.000000+00:00',
  content: '# Release checklist\n\n- [x] Freeze main\n- [ ] Cut the release branch\n- [ ] Publish notes\n',
}

const COMMENTS = [{
  id: 'c1', author: 'owner', is_agent: false, body: 'Add the rollback step', thread_id: 'c1',
  status: 'open', scope: 'private', origin: 'local', sync_state: 'local_only',
  created_at: '2026-10-08T21:00:00Z', updated_at: '2026-10-08T21:00:00Z',
}]

const slot = (key, title) => ({ key, title, messages: 4, running: false, last_activity_ts: '2026-10-08T21:00:00Z' })
const SLOTS = [slot('chat-1', 'Release prep'), slot('chat-2', 'Docs review'), slot('chat-3', 'Oncall handoff')]

// Set per frame: makes POST /api/chat/slots (New session) fail like a restarting gateway.
let failCreate = false

const extra = async (path, route) => {
  if (path === '/api/artifact-folders') return json(route, { folders: [] }), true
  if (path === '/api/chat/slots' && route.request().method() === 'POST' && failCreate) {
    return route.fulfill({ status: 503, contentType: 'application/json', body: JSON.stringify({ error: 'Kiro Crew is restarting' }) }), true
  }
  const m = /^\/api\/artifacts\/([^/]+)(\/.*)?$/.exec(path)
  if (!m || decodeURIComponent(m[1]) !== ARTIFACT.slug) return false
  const rest = m[2] || ''
  if (rest === '/versions') return json(route, { slug: ARTIFACT.slug, versions: [1, 2, 3] }), true
  const v = /^\/versions\/(\d+)$/.exec(rest)
  if (v) return json(route, { ...ARTIFACT, version: Number(v[1]), content: '# Release checklist\n\n- [ ] Freeze main\n' }), true
  if (rest === '/events') return json(route, { slug: ARTIFACT.slug, events: [] }), true
  if (rest === '/comments') return json(route, { comments: COMMENTS }), true
  if (rest === '/upstream-status') return json(route, {}), true
  if (rest === '') return json(route, ARTIFACT), true
  return false
}

async function shoot(browser, base, theme) {
  const context = await browser.newContext({ viewport: { width: 1400, height: 760 }, deviceScaleFactor: 2 })
  const page = await context.newPage()
  await stubDashboardApi(page, { extra, theme, slots: SLOTS })
  logPageProblems(page)
  await page.goto(base + `/artifacts/${ARTIFACT.slug}`, { waitUntil: 'domcontentloaded' })
  const more = page.getByRole('button', { name: 'More actions' })
  await more.waitFor()
  await page.waitForTimeout(1500)

  const write = async (name, clip) => {
    const path = `${OUT}/${theme}-${name}.png`
    await page.screenshot({ path, clip })
    console.log('wrote', path)
  }
  const header = { x: 0, y: 0, width: 1400, height: 200 }
  await write('01-toolbar', header)

  await more.click()
  await page.getByRole('menu').waitFor()
  await page.waitForTimeout(400)
  await write('02-more-open', { x: 700, y: 0, width: 700, height: 560 })

  await page.getByRole('menuitem', { name: 'Send to a session' }).click()
  await page.getByRole('menuitem', { name: 'New session' }).waitFor()
  await page.waitForTimeout(400)
  await write('03-send-submenu', { x: 400, y: 0, width: 1000, height: 560 })
  await page.keyboard.press('Escape')
  await page.keyboard.press('Escape')
  await page.getByRole('menu').waitFor({ state: 'detached' })

  // Editing: Save + Cancel stay in the row; Snapshot and Preview move to More.
  await page.getByRole('button', { name: 'Edit content' }).click()
  await page.getByRole('button', { name: /Cancel/ }).waitFor()
  // Park the pointer so no toolbar tooltip is left showing in the frame.
  await page.mouse.move(5, 700)
  await page.waitForTimeout(300)
  await more.focus()
  await page.keyboard.press('Enter')
  await page.getByRole('menu').waitFor()
  await page.waitForTimeout(400)
  await write('04-edit-more', { x: 700, y: 0, width: 700, height: 460 })
  // Escape closes the menu; a clean editor also leaves edit mode on Escape.
  await page.keyboard.press('Escape')
  await page.getByRole('menu').waitFor({ state: 'detached' })
  const cancel = page.getByRole('button', { name: /Cancel/ })
  if (await cancel.isVisible()) await cancel.click()
  await page.getByRole('button', { name: 'Edit content' }).waitFor()

  // A failed New session create.
  failCreate = true
  await more.click()
  await page.getByRole('menuitem', { name: 'Send to a session' }).click()
  await page.getByRole('menuitem', { name: 'New session' }).click()
  await page.getByRole('alert').first().waitFor()
  await page.waitForTimeout(400)
  await write('05-send-failed', { x: 260, y: 0, width: 1140, height: 340 })
  failCreate = false

  // Picking an existing session opens it with the reference pre-filled.
  await more.click()
  await page.getByRole('menuitem', { name: 'Send to a session' }).click()
  await page.getByRole('menuitem', { name: 'Release prep' }).click()
  await page.waitForURL(/\/chat/)
  await page.waitForTimeout(2500)
  await write('06-prefilled', { x: 0, y: 0, width: 1400, height: 760 })

  // A historical version: Revert takes Edit's place in the row.
  await page.goto(base + `/artifacts/${ARTIFACT.slug}`, { waitUntil: 'domcontentloaded' })
  await more.waitFor()
  await page.getByRole('combobox', { name: 'Version' }).click()
  await page.getByRole('option', { name: 'v1' }).click()
  await page.getByRole('button', { name: 'Revert to v1' }).waitFor()
  await page.mouse.move(5, 700)
  await page.waitForTimeout(800)
  await write('07-historical', header)

  // Popped out: More offers Focus + Bring back instead of Pop out.
  await page.getByRole('combobox', { name: 'Version' }).click()
  await page.getByRole('option', { name: 'Live' }).click()
  await page.getByRole('button', { name: 'Edit content' }).waitFor()
  const popupPromise = context.waitForEvent('page')
  await more.click()
  await page.getByRole('menuitem', { name: 'Pop out to window' }).click()
  const popup = await popupPromise
  await popup.waitForLoadState('domcontentloaded')
  await page.bringToFront()
  await page.waitForTimeout(2500)
  await more.click()
  await page.getByRole('menuitem', { name: 'Focus popped-out window' }).waitFor()
  await page.waitForTimeout(400)
  await write('08-popped-out', { x: 700, y: 0, width: 700, height: 560 })
  await page.keyboard.press('Escape')
  await popup.close()

  await context.close()
}

const NOTE_PATH = 'release-notes.md'
const NOTE_TITLE = 'Release notes'
const NOTE_CONTENT = `# ${NOTE_TITLE}\n\n| Area | Change | Owner |\n|---|---|---|\n| Artifacts | Toolbar keeps two actions, the rest in More | web |\n`

async function shootNotebook(browser, base, theme) {
  const context = await browser.newContext({ viewport: { width: 1400, height: 760 }, deviceScaleFactor: 2, locale: 'en-US' })
  const page = await context.newPage()
  await stubDashboardApi(page, {
    theme,
    extra: mdnbApiStub({ notes: mdnbNotesList(NOTE_PATH, NOTE_TITLE), doc: mdnbNoteDoc(NOTE_PATH, NOTE_CONTENT) }),
  })
  logPageProblems(page)
  await page.addInitScript(id => {
    localStorage.setItem('mdnb-active-vault', id)
    localStorage.removeItem('mdnb-full-width')
  }, MDNB_VAULT_ID)
  await page.goto(base + '/md-notebook', { waitUntil: 'domcontentloaded' })
  await page.getByText(NOTE_TITLE).first().waitFor({ timeout: 15000 })
  await page.getByText(NOTE_TITLE).first().click()
  await page.getByText('Toolbar keeps two actions').waitFor({ timeout: 15000 })
  for (const [name, label] of [['09-mdnb-medium', 'Medium width'], ['10-mdnb-full', 'Full width']]) {
    const toggle = page.getByRole('button', { name: label })
    await toggle.hover()
    await page.waitForTimeout(500)
    const clip = await notePaneClip(page)
    const path = `${OUT}/${theme}-${name}.png`
    await page.screenshot({ path, clip: { ...clip, height: Math.min(clip.height, 260) } })
    console.log('wrote', path)
    if (label === 'Medium width') {
      await toggle.click()
      await page.mouse.move(5, 700)
      await page.waitForTimeout(400)
    }
  }
  await context.close()
}

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  for (const theme of ['light', 'dark']) {
    await shoot(browser, base, theme)
    await shootNotebook(browser, base, theme)
  }
  await browser.close()
  srv.close()
}

main().catch(err => { console.error(err); process.exit(1) })
