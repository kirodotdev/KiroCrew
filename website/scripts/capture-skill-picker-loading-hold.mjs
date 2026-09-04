/**
 * Screenshot harness + behavior check for the HELD-ENTER ANNOUNCEMENT on the
 * $skill picker's loading state.
 *
 * While /api/skills is in flight the picker has no matches, so
 * `useListKeyboardNav`'s count===0 branch preventDefaults Enter and Tab at
 * document capture and ChatInput's handleKeyDown never sees them. A mouse click
 * on the send arrow is not a keydown, so it still reaches fireComposer() — the
 * two send affordances disagree. The swallow is deliberate (releasing Enter while
 * matches are transiently unknowable would irreversibly send a draft whose
 * $token the user was still completing), but the loading branch was a plain
 * <div> reading only "Loading skills…", so the capture was silent: no visual
 * signal, and for a screen-reader user no signal at all, unlike every
 * settled-empty branch which announces the flip in a role="status" live region.
 *
 * This asserts as well as photographs, against the REAL built SPA
 * (website/dist): each scene pins /api/skills to a chosen shape and exits
 * non-zero unless the right copy rendered inside a role="status" region.
 * Nothing in CI runs this file — the CI-enforced half is
 * SkillPickerMenu.loadingAnnounce.test.tsx.
 *
 * Usage: node scripts/capture-skill-picker-loading-hold.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/skill-picker-loading-hold'
const SLOT = 'chat-loading-hold'
const PROJECT = '/home/user/workspace/notes'

mkdirSync(OUT, { recursive: true })

const slots = [{
  key: SLOT,
  title: 'Sprint wrap-up',
  running: false,
  last_message: 'Ready when you are.',
  messages: 2,
  agent: 'default',
  memory_mode: 'persistent',
  project: PROJECT,
  folder_id: '',
  modified: Math.floor(Date.now() / 1000),
  source_links: [],
  source_links_total: 0,
}]

const detail = {
  running: false,
  has_more: false,
  total: 2,
  queue: [],
  project: PROJECT,
  messages: [
    { role: 'user', ts: Date.now() / 1000 - 600, content: 'Which skills can this agent use?' },
    { role: 'assistant', ts: Date.now() / 1000 - 590, content: 'Type `$` in the composer to browse skills.' },
  ],
}

/**
 * One fresh page per scene.
 *
 * `skills` is either the literal string 'hang' — the route is marked handled and
 * never fulfilled, so react-query stays isLoading exactly as a wedged gateway
 * leaves it — or a payload to serve. `sendMode` seeds the composer's send
 * binding through the same localStorage key ChatSettings reads.
 */
async function scene(context, base, { skills, sendMode }) {
  const extra = async (path, route) => {
    if (path.startsWith('/api/skills')) {
      if (skills === 'hang') return true
      await json(route, skills)
      return true
    }
    if (path === '/api/slash-commands') { await json(route, []); return true }
    if (path.startsWith('/api/chat/slots/')) { await json(route, detail); return true }
    return false
  }
  const page = await context.newPage()
  logPageProblems(page)
  await stubDashboardApi(page, { slots, theme: 'dark', extra })
  await page.addInitScript(([slot, mode]) => {
    localStorage.setItem('mc-active-slot', slot)
    if (mode) localStorage.setItem('mc-chat-config', JSON.stringify({ sendOnEnter: mode }))
  }, [SLOT, sendMode || ''])
  await page.goto(base + '/', { waitUntil: 'domcontentloaded' })
  await page.waitForTimeout(2000)
  const composer = page.locator('textarea').first()
  await composer.click()
  await composer.pressSequentially('$grill', { delay: 15 })
  await page.locator('[role="listbox"]').waitFor({ timeout: 10000 })
  await page.waitForTimeout(400)
  return page
}

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  const context = await browser.newContext({
    viewport: { width: 1400, height: 950 },
    deviceScaleFactor: 2,
    locale: 'en-US',
  })

  const failures = []
  const HELD = 'Loading skills… — Enter won’t send yet; use the Send button'
  const HELD_CTRL = 'Loading skills… — Ctrl+Enter won’t send yet; use the Send button'

  /* ── Scene 1: fetch in flight → "won't send yet" copy in a live region ── */
  let page = await scene(context, base, { skills: 'hang' })
  let live = page.locator('[role="status"]', { hasText: /Loading skills…/ })
  if (await live.count() !== 1) failures.push('scene 1: loading copy is not inside a role="status" live region')
  if (await page.getByText(HELD, { exact: false }).count() !== 1) failures.push('scene 1: Enter-won\u2019t-send copy missing')
  await page.screenshot({ path: `${OUT}/1-loading-enter-held.png` })
  console.log('wrote', `${OUT}/1-loading-enter-held.png`)
  await page.close()

  /* ── Scene 2: same, but Ctrl+Enter is the send binding ── */
  page = await scene(context, base, { skills: 'hang', sendMode: 'ctrl-enter' })
  live = page.locator('[role="status"]', { hasText: /Loading skills…/ })
  if (await live.count() !== 1) failures.push('scene 2: loading copy is not inside a role="status" live region')
  if (await page.getByText(HELD_CTRL, { exact: false }).count() !== 1) failures.push('scene 2: Ctrl+Enter copy missing')
  if (await page.getByText(HELD, { exact: true }).count() !== 0) failures.push('scene 2: promised bare Enter under a ctrl-enter binding')
  await page.screenshot({ path: `${OUT}/2-loading-ctrl-enter-held.png` })
  console.log('wrote', `${OUT}/2-loading-ctrl-enter-held.png`)
  await page.close()

  /* ── Scene 3: settled empty → the pre-existing released-Enter copy (control) ── */
  page = await scene(context, base, { skills: [] })
  const settled = page.locator('[role="status"]').getByText(/No matching skills — Enter sends the message/)
  if (await settled.count() !== 1) failures.push('scene 3: settled-empty copy missing')
  if (await page.getByText(/Loading skills…/).count() !== 0) failures.push('scene 3: loading copy still showing after settle')
  await page.screenshot({ path: `${OUT}/3-settled-empty-enter-sends.png` })
  console.log('wrote', `${OUT}/3-settled-empty-enter-sends.png`)
  await page.close()

  await browser.close()
  srv.close()

  if (failures.length) {
    for (const f of failures) console.error('FAIL:', f)
    process.exit(1)
  }
  console.log('PASS: the loading branch announces that the send key will not send yet, in a live region')
}

main().catch(err => { console.error(err); process.exit(1) })
