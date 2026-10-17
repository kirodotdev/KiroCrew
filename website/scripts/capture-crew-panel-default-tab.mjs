/**
 * Capture the crewmate panel's LANDING tab and the Notes tab's agent-only line
 * on a REAL pod. Not a test — a screenshot harness.
 *
 * Usage:
 *   POD_INFO="$KIROCREW_SCRATCH/pod-info.json" \
 *     node scripts/capture-crew-panel-default-tab.mjs <outDir>
 *
 * Writes <outDir>/landing-dashboard.png (a crewmate opened cold: the strip's
 * first chip is Dashboard, it is the selected one, and the Notes body is not on
 * screen), <outDir>/notes-agent-only.png (the Notes tab, with the line that says
 * whose notes these are above the crewmate's own text) and
 * <outDir>/notes-empty.png (the same tab over a crewmate that has written
 * nothing: the line is still there, and it is the only sentence claiming the tab
 * -- the empty state's second sentence was removed because this line says it).
 *
 * Three frames because the line's claim is "rendered in EVERY state", and a
 * filled read cannot show the state where it does the most work: a reader whose
 * first Notes visit finds nothing has only this line to tell them why.
 *
 * Every frame's claim is asserted BEFORE the shutter, so a screenshot of a stale
 * bundle cannot pass for evidence.
 */
import { chromium } from 'playwright'
import { mkdirSync, readFileSync } from 'node:fs'
import { join } from 'node:path'
import { check, podInfo, primeCrewPod, openMembersDm } from './lib/crew-pod-harness.mjs'

const OUT = process.argv[2]
if (!OUT) throw new Error('usage: <outDir>')
mkdirSync(OUT, { recursive: true })
const CREW = process.env.CREW || 'oncall'
// A SECOND crewmate, for the empty frame. Created and never written to, rather
// than the first one's briefing deleted: a crewmate that has never worked is the
// real occasion for this state, and it leaves frame 2's read untouched.
const BLANK_CREW = process.env.BLANK_CREW || 'scribe'
const { BASE, authed } = podInfo(readFileSync)

const browser = await chromium.launch()
const page = await browser.newPage({ viewport: { width: 1600, height: 1000 }, deviceScaleFactor: 2 })

await primeCrewPod(page, authed, CREW, 'dark')
await primeCrewPod(page, authed, BLANK_CREW, 'dark')
await openMembersDm(page, authed, BASE, CREW, { theme: 'dark', lang: 'en' })

const panel = page.getByTestId('member-side-panel')
if (!(await panel.isVisible().catch(() => false))) await page.getByTestId('member-panel-toggle').click()
await panel.waitFor({ state: 'visible', timeout: 15000 })

// ---- Frame 1: the landing tab of a crewmate opened cold.
const chips = page.locator('[data-testid^="side-panel-leading-tab-"]')
await chips.first().waitFor({ state: 'visible', timeout: 15000 })
const chipIds = await chips.evaluateAll(els =>
  els.map(el => el.getAttribute('data-testid').replace('side-panel-leading-tab-', '')))
check('strip order is Dashboard, Work log, Notes, Schedules',
  chipIds.join(',') === 'crew-dashboard,crew-work-log,crew-notes,crew-schedules', chipIds.join(','))
check('Dashboard is the selected chip',
  await page.getByTestId('side-panel-leading-tab-crew-dashboard').getAttribute('aria-selected') === 'true')
check('Notes is NOT the selected chip',
  await page.getByTestId('side-panel-leading-tab-crew-notes').getAttribute('aria-selected') === 'false')
await page.getByTestId('member-dashboard').waitFor({ state: 'visible', timeout: 15000 })
check('the Notes body is not on screen', !(await page.getByTestId('member-notes').isVisible().catch(() => false)))
await page.waitForTimeout(500)
await page.screenshot({ path: join(OUT, 'landing-dashboard.png') })
console.log('wrote landing-dashboard.png')

// ---- Frame 2: the Notes tab, reached by its chip.
await page.getByTestId('side-panel-leading-tab-crew-notes').click()
const notes = page.getByTestId('member-notes')
await notes.waitFor({ state: 'visible', timeout: 15000 })
const line = page.getByTestId('member-notes-agent-only')
await line.waitFor({ state: 'visible', timeout: 15000 })
const text = (await line.textContent()) ?? ''
check('the agent-only line names the crewmate and says it is read-only here',
  text.includes(CREW) && /writes these notes for itself/.test(text) && /not change them/.test(text), text)
check('no edit control anywhere in the tab',
  (await notes.locator('button', { hasText: /edit/i }).count()) === 0
  && (await notes.locator('textarea, input, [contenteditable="true"]').count()) === 0)
// The line sits ABOVE whatever the read produced, not under it.
const bodyOrEmpty = page.locator('[data-testid="member-notes-body"], [data-testid="member-notes-empty"]').first()
await bodyOrEmpty.waitFor({ state: 'visible', timeout: 15000 })
const lineBox = await line.boundingBox()
const bodyBox = await bodyOrEmpty.boundingBox()
check('the line is above the notes', lineBox.y < bodyBox.y, `${lineBox.y} vs ${bodyBox.y}`)
await page.waitForTimeout(500)
await page.screenshot({ path: join(OUT, 'notes-agent-only.png') })
console.log('wrote notes-agent-only.png')

// ---- Frame 3: the same tab over a crewmate that has written nothing yet.
await openMembersDm(page, authed, BASE, BLANK_CREW, { theme: 'dark', lang: 'en' })
const panel3 = page.getByTestId('member-side-panel')
if (!(await panel3.isVisible().catch(() => false))) await page.getByTestId('member-panel-toggle').click()
await panel3.waitFor({ state: 'visible', timeout: 15000 })
await page.getByTestId('side-panel-leading-tab-crew-notes').click()
const notes3 = page.getByTestId('member-notes')
await notes3.waitFor({ state: 'visible', timeout: 15000 })
const line3 = page.getByTestId('member-notes-agent-only')
await line3.waitFor({ state: 'visible', timeout: 15000 })
const empty = page.getByTestId('member-notes-empty')
await empty.waitFor({ state: 'visible', timeout: 15000 })
check('this is the EMPTY state, not a read', !(await page.getByTestId('member-notes-body').isVisible().catch(() => false)))
const text3 = (await line3.textContent()) ?? ''
check('the line is there with nothing to read, and names THIS crewmate',
  text3.includes(BLANK_CREW) && /writes these notes for itself/.test(text3), text3)
// The removed sentence: the empty state used to repeat what the line now says.
const emptyText = (await empty.textContent()) ?? ''
check('the empty state does not repeat the line', !/for itself|not change them/.test(emptyText), emptyText)
const lineBox3 = await line3.boundingBox()
const emptyBox = await empty.boundingBox()
check('the line is above the empty state', lineBox3.y < emptyBox.y, `${lineBox3.y} vs ${emptyBox.y}`)
await page.waitForTimeout(500)
await page.screenshot({ path: join(OUT, 'notes-empty.png') })
console.log('wrote notes-empty.png')

await browser.close()
