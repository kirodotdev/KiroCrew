/**
 * Screenshot harness for the Crew Members roster's "+" entry (issue #9513):
 * pressing it must land ON the crew manager's create form, not on the crew
 * list a second "New crew" click would be needed on. Against a REAL pod.
 *
 * Frames, per theme:
 *   01-members-roster         the roster with the "+" at rest (before)
 *   02-create-form            one click later: the "Add crew member" form is
 *                             open, the URL has consumed `new=1&from=members`
 *   02b-create-form-bottom    the same form scrolled to its lower hints
 *   03-after-create-landing   "Create member" landed on the new member's
 *                             thread; its roster row is scrolled into view
 *   04-empty-roster           (fixture, FIXTURE_BASE) the empty roster's
 *                             "Add member" call to action
 *   05-triggers-popover       the Triggers "?" popover open, member-worded
 *   06-create-failed          a rejected submit (duplicate name) staying in
 *                             the form with the error shown
 *
 * Usage:
 *   kirocrew pod up <worktree> --json | tail -1 > "$KIROCREW_SCRATCH/pod-info.json"
 *   npx vite --host 127.0.0.1 --port 6831 --strictPort     # for 04 only, optional
 *   POD_INFO="$KIROCREW_SCRATCH/pod-info.json" FIXTURE_BASE=http://127.0.0.1:6831 \
 *     node scripts/capture-members-add-opens-create.mjs ../temp-screenshots/members-add-opens-create
 */
import { chromium } from 'playwright'
import { mkdirSync, readFileSync } from 'node:fs'
import { join } from 'node:path'
import { check, podInfo, primeCrewPod } from './lib/crew-pod-harness.mjs'

const OUT = process.argv[2] || '../temp-screenshots/members-add-opens-create'
const CREW = 'oncall'
const ADD_MEMBER = 'Add member'
const CREATE_TITLE = 'Add crew member' // the create-mode DialogContent's aria-label when arriving from the roster

mkdirSync(OUT, { recursive: true })
const { BASE, authed } = podInfo(readFileSync)

async function shoot(browser, theme) {
  const page = await browser.newPage({ viewport: { width: 1280, height: 820 } })
  await primeCrewPod(page, authed, CREW, theme)

  await page.goto(`${BASE}/members`, { waitUntil: 'domcontentloaded' })
  const add = page.getByTestId('member-add')
  await add.waitFor({ state: 'visible', timeout: 20000 })
  check(`[${theme}] roster "+" is named "${ADD_MEMBER}"`, (await add.getAttribute('aria-label')) === ADD_MEMBER)
  await page.getByTestId('member-roster').getByText(CREW).first().waitFor({ state: 'visible', timeout: 20000 })
  await page.waitForTimeout(400)
  await page.screenshot({ path: join(OUT, `01-members-roster-${theme}.png`) })

  await add.click()
  const form = page.getByRole('dialog', { name: CREATE_TITLE })
  await form.waitFor({ state: 'visible', timeout: 20000 })
  check(`[${theme}] one click lands on the "${CREATE_TITLE}" form`, true)
  check(`[${theme}] the form is titled in the roster's words, not "Create Agent"`,
    (await form.getByRole('heading', { name: CREATE_TITLE }).count()) === 1)
  check(`[${theme}] the form body keeps the word: "What this member uses", no "agent" heading`,
    (await form.getByRole('heading', { name: 'What this member uses' }).count()) === 1
      && (await form.getByRole('heading', { name: 'What this agent uses' }).count()) === 0)
  await page.waitForFunction(() => !/[?&](new|from)=/.test(location.search), null, { timeout: 10000 })
  check(`[${theme}] deep-link params consumed`, /[?&]tab=crews/.test(page.url()) && !/[?&](new|from)=/.test(page.url()), page.url())
  await page.waitForTimeout(400)
  await page.screenshot({ path: join(OUT, `02-create-form-${theme}.png`) })

  // The form's lower half: workspace and memory-store hints speak member too.
  const body = form.locator('.overflow-y-auto').first()
  await body.evaluate((el) => { el.scrollTop = el.scrollHeight })
  await page.waitForTimeout(300)
  check(`[${theme}] the lower hints speak member (workspace / memory store)`,
    (await form.getByText(/The folder this member reads and writes/).count()) === 1
      && (await form.getByText(/Which memory store this member is bound to/).count()) === 1)
  await page.screenshot({ path: join(OUT, `02b-create-form-bottom-${theme}.png`) })
  await body.evaluate((el) => { el.scrollTop = 0 })

  // Triggers "?" popover open: the info text also speaks member.
  await form.getByRole('button', { name: 'More information' }).first().click()
  const tip = page.getByRole('tooltip')
  await tip.waitFor({ state: 'visible', timeout: 5000 })
  check(`[${theme}] triggers popover says "member", not "agent"`,
    /selects this member/.test((await tip.textContent()) || '') && !/this agent/.test((await tip.textContent()) || ''))
  await page.screenshot({ path: join(OUT, `05-triggers-popover-${theme}.png`) })
  // Toggle it closed with its own button — Escape would dismiss the whole
  // dialog (Radix), and a member-origin dismissal navigates back to /members.
  await form.getByRole('button', { name: 'More information' }).first().click()
  await tip.waitFor({ state: 'hidden', timeout: 5000 }).catch(() => {})

  // Failed submit: a name the registry already holds. The error is the
  // server's, so it is the same text as the crew manager's own path; the
  // frame shows where it lands in the member-titled form.
  await form.getByPlaceholder('e.g. oncall').fill(CREW)
  const template0 = form.getByRole('combobox', { name: 'Agent Template' })
  await template0.click()
  await page.getByRole('option', { name: 'kirocrew', exact: true }).click()
  await form.getByRole('button', { name: 'Create member', exact: true }).click()
  const err = form.getByTestId('crew-sheet-error')
  await err.waitFor({ state: 'visible', timeout: 20000 })
  check(`[${theme}] a failed submit stays in the form and the error speaks member`,
    (await form.isVisible()) && /A member named 'oncall' already exists/.test((await err.textContent()) || ''), await err.textContent())
  await page.waitForTimeout(300)
  await page.screenshot({ path: join(OUT, `06-create-failed-${theme}.png`) })
  await form.getByPlaceholder('e.g. oncall').fill('')

  // Fill the form and create: the landing is the NEW member's thread on the
  // Members page, not the crew list behind the form.
  // Unique per run: a rerun against the same (still-up) pod would otherwise
  // hit the 409 the previous run's member now causes, and time out.
  const newName = `scribe-${theme}-${Date.now().toString(36)}`
  await form.getByPlaceholder('e.g. oncall').fill(newName)
  await form.getByRole('button', { name: 'Create member', exact: true }).click()
  await page.waitForURL((u) => u.pathname === '/members' && u.searchParams.get('member') === newName, { timeout: 20000 })
  check(`[${theme}] create lands on /members?member=${newName}`, true)
  const header = page.getByTestId('member-thread-header')
  await header.getByText(newName).first().waitFor({ state: 'visible', timeout: 20000 })
  check(`[${theme}] the new member's thread is open`, true)
  check(`[${theme}] no "gone" notice on arrival`, (await page.getByTestId('member-gone-notice').count()) === 0)
  // The roster row is in view, not below the fold: a thread with no visible
  // row reads as a member that was never added.
  const row = page.getByTestId('member-roster').getByRole('button', { name: new RegExp(`^${newName}`) }).first()
  await row.waitFor({ state: 'visible', timeout: 10000 })
  const inView = await row.evaluate((el) => {
    const r = el.getBoundingClientRect()
    const list = el.closest('ul').getBoundingClientRect()
    return r.top >= list.top && r.bottom <= list.bottom
  })
  check(`[${theme}] the new member's roster row is scrolled into view`, inView)
  await page.waitForTimeout(500)
  await page.screenshot({ path: join(OUT, `03-after-create-landing-${theme}.png`) })
  await page.close()
}

/** The empty roster cannot be reached on a real pod (the built-in crews are
 *  always there), so that one state comes from the isolated capture entry
 *  (website/capture/members-page.html on a vite dev server, FIXTURE_BASE)
 *  with /api/members answering an empty list. */
async function shootEmptyRoster(browser, theme) {
  const base = process.env.FIXTURE_BASE
  if (!base) { console.log('skip  empty-roster frames (set FIXTURE_BASE=http://127.0.0.1:<vite port>)'); return }
  const page = await browser.newPage({ viewport: { width: 1280, height: 820 } })
  await page.route((u) => new URL(u).pathname.startsWith('/api/'), (route) => {
    const path = new URL(route.request().url()).pathname
    if (path === '/api/members') return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ members: [], default_agent: 'kirocrew' }) })
    if (path === '/api/agents') return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ agents: [], default_agent: 'kirocrew' }) })
    const isList = /commands|skills|agents|sessions|files|history|models|artifacts|folders|crons|webhooks/.test(path)
    return route.fulfill({ status: 200, contentType: 'application/json', body: isList ? '[]' : '{}' })
  })
  await page.goto(`${base}/capture/members-page.html?theme=${theme}`)
  await page.waitForSelector('[data-capture-root]')
  const cta = page.getByTestId('member-empty-cta')
  await cta.waitFor({ state: 'visible', timeout: 20000 })
  check(`[${theme}/empty] CTA reads "${ADD_MEMBER}"`, (await cta.textContent())?.trim() === ADD_MEMBER, await cta.textContent())
  const copy = await page.getByTestId('member-roster').textContent()
  check(`[${theme}/empty] copy no longer sends the user to the crew manager`, /No crew members yet\./.test(copy || '') && !/crew manager/.test(copy || ''))
  await page.waitForTimeout(300)
  await page.screenshot({ path: join(OUT, `04-empty-roster-${theme}.png`) })
  await page.close()
}

const browser = await chromium.launch({ executablePath: process.env.CHROMIUM_PATH || undefined })
try {
  for (const theme of ['light', 'dark']) await shoot(browser, theme)
  for (const theme of ['light', 'dark']) await shootEmptyRoster(browser, theme)
} finally {
  await browser.close()
}
console.log('wrote', OUT)
