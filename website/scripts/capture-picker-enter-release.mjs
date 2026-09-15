/**
 * Screenshot harness + behavior check for the PICKER ENTER RELEASE on the
 * $skill and /command composer pickers (#5041, sibling of #5029).
 *
 * The composer's $skill picker used to swallow Enter whenever it had zero
 * matches — a token like "$zzz-nope" showed "No matching skills" but the
 * message could not be sent. The /command menu was worse: with zero matches
 * it renders null while its keyboard listener stays attached, so an INVISIBLE
 * surface swallowed Enter on unmatched slash input like "/xyz". The fix
 * releases Enter/Tab and closes both surfaces when there is nothing to choose.
 *
 * This asserts as well as photographs, against the REAL built SPA
 * (website/dist): for each picker it types the zero-match input, presses
 * Enter, and exits non-zero unless the send POST actually fired. Nothing in
 * CI runs this file — the CI-enforced half of the invariant is
 * SkillPickerMenu.test.tsx + SlashCommandMenu.test.tsx.
 *
 * Usage: node scripts/capture-picker-enter-release.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/picker-enter-release'
const SLOT = 'chat-pickers'
const PROJECT = '/home/user/workspace/notes'
const SKILL_MESSAGE = 'Write the handover using $zzz-nope'
const SLASH_MESSAGE = '/xyz'

mkdirSync(OUT, { recursive: true })

const slots = [{
  key: SLOT,
  title: 'Sprint wrap-up',
  running: false,
  last_message: 'Ready when you are.',
  messages: 2,
  agent: 'kirocrew',
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
    { role: 'user', ts: Date.now() / 1000 - 600, content: 'Which skills are available for handovers?' },
    { role: 'assistant', ts: Date.now() / 1000 - 590, content: 'Type `$` in the composer to browse skills.' },
  ],
}

// A populated skills list that simply does not match the typed token — the
// zero-match state must come from filtering, not from an empty backend.
const SKILLS = [
  { key: 'kirocrew/oncall-handover', name: 'oncall-handover', description: 'Handover report', source: 'kirocrew' },
  { key: 'kirocrew/ticket-pull', name: 'ticket-pull', description: 'Pull tickets', source: 'kirocrew' },
]

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  const context = await browser.newContext({
    viewport: { width: 1400, height: 950 },
    deviceScaleFactor: 2,
    locale: 'en-US',
  })

  let sendPosts = 0

  /** Routes the shared stub does not know about (await json(); return true). */
  const extra = async (path, route) => {
    if (path === '/api/skills') { await json(route, SKILLS); return true }
    if (path === '/api/slash-commands') {
      await json(route, [{ name: '/help', description: 'Show help' }, { name: '/compact', description: 'Compact the session' }])
      return true
    }
    if (path.startsWith('/api/chat') && route.request().method() === 'POST') {
      sendPosts += 1
      await json(route, { ok: true })
      return true
    }
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
  const menu = page.locator('[role="listbox"]')

  /* ── Scene 1: $skill picker, zero matches ── */
  await composer.click()
  // pressSequentially drives real keydown/input events so the $-trigger
  // detection and the picker open path both run.
  await composer.pressSequentially(SKILL_MESSAGE, { delay: 15 })

  // The zero-match menu must be open and announcing the mode flip before
  // Enter means anything.
  await menu.getByText(/No matching skills — Enter sends the message/).waitFor({ timeout: 10000 })
  await page.waitForTimeout(400)
  await page.screenshot({ path: `${OUT}/1-skill-picker-zero-match-open.png` })
  console.log('wrote', `${OUT}/1-skill-picker-zero-match-open.png`)

  await composer.press('Enter')
  await page.waitForTimeout(800)

  const skillMenuStillOpen = await menu.count()
  const skillSendFired = sendPosts > 0
  await page.screenshot({ path: `${OUT}/2-skill-after-enter-message-sent.png` })
  console.log('wrote', `${OUT}/2-skill-after-enter-message-sent.png`)
  console.log({ scene: 'skill', skillSendFired, skillMenuStillOpen })

  /* ── Scene 2: /command menu, zero matches (announced empty state) ── */
  const before = sendPosts
  await composer.click()
  await composer.pressSequentially(SLASH_MESSAGE, { delay: 15 })
  // The settled zero-match slash menu shows the announcing empty state — the
  // invisible-listener trap is gone AND the mode flip is visible.
  await menu.getByText(/No matching commands — Enter sends the message/).waitFor({ timeout: 10000 })
  await page.waitForTimeout(400)
  const slashEmptyStateShown = await menu.count()
  await page.screenshot({ path: `${OUT}/3-slash-zero-match-announced.png` })
  console.log('wrote', `${OUT}/3-slash-zero-match-announced.png`)

  await composer.press('Enter')
  await page.waitForTimeout(800)
  const slashSendFired = sendPosts > before
  const slashMenuStillOpen = await menu.count()
  await page.screenshot({ path: `${OUT}/4-slash-after-enter-message-sent.png` })
  console.log('wrote', `${OUT}/4-slash-after-enter-message-sent.png`)
  console.log({ scene: 'slash', slashSendFired, slashEmptyStateShown, slashMenuStillOpen })

  await browser.close()
  srv.close()

  if (!skillSendFired || skillMenuStillOpen > 0) {
    console.error('FAIL: Enter did not pass through the zero-match $skill picker')
    process.exit(1)
  }
  if (!slashSendFired || slashEmptyStateShown !== 1 || slashMenuStillOpen > 0) {
    console.error('FAIL: zero-match /command menu did not announce and release Enter')
    process.exit(1)
  }
  console.log('PASS: both zero-match pickers released Enter; messages sent')
}

main().catch(err => { console.error(err); process.exit(1) })
