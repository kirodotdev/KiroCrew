/**
 * Screenshot harness + behavior check for the AGENT SCOPE CUE on the $skill
 * picker (#6028).
 *
 * PR #3820 made the picker agent-scoped, but the UI gave no cue that
 * filtering happened: a scoped list looked identical to the legacy
 * unfiltered one, and a scoped-EMPTY catalog showed the generic "No matching
 * skills" copy even though the emptiness was caused by the agent's skill://
 * mapping. The fix keys both cues on the server's `agent_scoped` envelope:
 * a "Scoped to agent …" footer on a non-empty scoped list, and a
 * "No skills mapped to …" empty state when the scoped catalog is empty.
 *
 * This asserts as well as photographs, against the REAL built SPA
 * (website/dist): scene by scene it opens the $ picker under each payload
 * shape and exits non-zero unless the right cue (and ONLY the right cue)
 * rendered. Nothing in CI runs this file — the CI-enforced half is
 * SkillPickerMenu.test.tsx + test_skill_browser.py.
 *
 * Usage: node scripts/capture-skill-picker-scope-cue.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/skill-picker-scope-cue'
const SLOT = 'chat-scope-cue'
const PROJECT = '/home/user/workspace/notes'
const AGENT = 'writer-agent'

mkdirSync(OUT, { recursive: true })

const slots = [{
  key: SLOT,
  title: 'Sprint wrap-up',
  running: false,
  last_message: 'Ready when you are.',
  messages: 2,
  agent: AGENT,
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

const SKILLS = [
  { key: 'kirocrew/oncall-handover', name: 'oncall-handover', description: 'Handover report', source: 'kirocrew' },
  { key: 'kirocrew/ticket-pull', name: 'ticket-pull', description: 'Pull tickets', source: 'kirocrew' },
]

/** One fresh page per scene: the skills query is cached per (slot, project,
 *  agent) with a long staleTime, so a payload-shape change mid-page would
 *  never refetch. */
async function scene(context, base, payload) {
  const extra = async (path, route) => {
    if (path.startsWith('/api/skills')) { await json(route, payload); return true }
    if (path === '/api/slash-commands') { await json(route, []); return true }
    if (path.startsWith('/api/chat/slots/')) { await json(route, detail); return true }
    return false
  }
  const page = await context.newPage()
  logPageProblems(page)
  await stubDashboardApi(page, { slots, theme: 'dark', extra })
  await page.addInitScript(slot => { localStorage.setItem('mc-active-slot', slot) }, SLOT)
  await page.goto(base + '/', { waitUntil: 'domcontentloaded' })
  await page.waitForTimeout(2000)
  const composer = page.locator('textarea').first()
  await composer.click()
  await composer.pressSequentially('$', { delay: 15 })
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

  /* ── Scene 1: scoped envelope, non-empty → footer cue ── */
  let page = await scene(context, base, { skills: SKILLS, agent_scoped: true, agent: AGENT })
  // The footer is a pinned sibling BELOW the listbox (not inside it), so
  // search the page, not the listbox.
  const footer = page.getByText(/Scoped to agent writer-agent/i)
  if (await footer.count() !== 1) failures.push('scene 1: scoped footer missing on a scoped non-empty list')
  await page.screenshot({ path: `${OUT}/1-scoped-list-footer.png` })
  console.log('wrote', `${OUT}/1-scoped-list-footer.png`)
  await page.close()

  /* ── Scene 2: scoped envelope, EMPTY → mapping-attributed empty state ── */
  page = await scene(context, base, { skills: [], agent_scoped: true, agent: AGENT })
  const mapped = page.locator('[role="listbox"]').getByText(/No skills mapped to writer-agent/)
  if (await mapped.count() !== 1) failures.push('scene 2: mapped-empty copy missing on a scoped empty catalog')
  await page.screenshot({ path: `${OUT}/2-scoped-empty-mapped-copy.png` })
  console.log('wrote', `${OUT}/2-scoped-empty-mapped-copy.png`)
  await page.close()

  /* ── Scene 3: legacy bare array → NO cue (control) ── */
  page = await scene(context, base, SKILLS)
  const lb = page.locator('[role="listbox"]')
  if (await page.getByText(/Scoped to agent/i).count() !== 0) failures.push('scene 3: footer rendered for the legacy bare-array shape')
  if (await lb.getByText(/\$oncall-handover/).count() !== 1) failures.push('scene 3: legacy list rows missing')
  await page.screenshot({ path: `${OUT}/3-legacy-list-no-cue.png` })
  console.log('wrote', `${OUT}/3-legacy-list-no-cue.png`)
  await page.close()

  await browser.close()
  srv.close()

  if (failures.length) {
    for (const f of failures) console.error('FAIL:', f)
    process.exit(1)
  }
  console.log('PASS: scope cues render exactly when the server flags the scope')
}

main().catch(err => { console.error(err); process.exit(1) })
