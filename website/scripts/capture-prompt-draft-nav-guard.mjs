/**
 * Screenshots + assertions for the Prompts draft guard on IN-APP NAVIGATION
 * (#7773).
 *
 * The sibling harness (capture-prompt-draft-tab-guard.mjs) drives an isolated
 * capture entry, which is enough for the exits SidePanelLayout owns. This one
 * cannot: the exit under test is the GLOBAL SIDEBAR, which lives in App.tsx
 * around the whole router. So this drives the REAL BUILT SPA over serveDist with
 * every /api call stubbed -- the real sidebar, the real CapabilitiesPage, the
 * real PromptsTab, the real leave-guard channel between them, and no gateway.
 *
 * The confirm is the browser's NATIVE dialog, so it cannot appear in a page
 * screenshot. This script therefore ASSERTS it -- one dialog per sidebar click,
 * carrying the pane's own discard copy -- and captures the OUTCOME of each
 * answer. A run where no dialog fires FAILS rather than quietly producing three
 * plausible frames, which is exactly what the bug looked like.
 *
 * Scenes:
 *   1-editor-dirty   the inline editor holding an edited body, before any exit
 *   2-draft-kept     sidebar clicked, confirm DISMISSED -> still on Prompts, text intact
 *                    (named 2-draft-lost instead when the editor did not survive,
 *                     which is what a build without the guard produces)
 *   3-draft-released sidebar clicked, confirm ACCEPTED  -> Sessions shown
 *
 * Usage:
 *   npm run build
 *   node scripts/capture-prompt-draft-nav-guard.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/prompt-draft-nav-guard'
mkdirSync(OUT, { recursive: true })

const EDITED_BODY = 'Read the report, classify it, and name the one experiment that would settle the diagnosis.'
const EXPECTED_CONFIRM = 'Discard unsaved changes?'
const BODY_FIELD = /markdown the agent receives/
/** Settings, one of the sidebar destinations the issue names. Any route works:
 *  the guard is on leaving, not on where to. */
const EXIT_ROW = 'Settings'
const EXIT_PATH = '/settings'

/** Two prompts, so the list pane looks like a real install rather than an empty
 *  state (the empty state hides the list column entirely). */
const PROMPTS = [
  {
    name: 'release-notes', fullName: 'release-notes',
    description: 'Draft release notes from a milestone',
    path: '~/.kiro/prompts/release-notes.md', package: '', source: 'global',
  },
  {
    name: 'triage', fullName: 'triage',
    description: 'Triage an inbound bug report',
    path: '~/.kiro/prompts/triage.md', package: '', source: 'global',
  },
]
const DETAIL = {
  content: '---\ndescription: Triage an inbound bug report\n---\n\nRead the report and classify it.\n',
  redacted: false,
  lossy: false,
  hash: 'a'.repeat(64),
}

const { srv, base } = await serveDist()
const browser = await chromium.launch()
const page = await browser.newPage({ viewport: { width: 1440, height: 900 } })
logPageProblems(page)

await stubDashboardApi(page, {
  extra: async (path, route) => {
    if (path.startsWith('/api/prompts/')) { await json(route, DETAIL); return true }
    if (path === '/api/prompts') { await json(route, PROMPTS); return true }
    return false
  },
})

/** Every native dialog this run raised, so the assertions below can read them. */
const dialogs = []
let answer = 'dismiss'
page.on('dialog', async d => {
  dialogs.push({ message: d.message(), answered: answer })
  if (answer === 'accept') await d.accept()
  else await d.dismiss()
})

// Filling the body scrolls it into view, which clips the pane header out of the
// frame. Park every scroller back at the top so each shot is the whole surface.
const shot = async name => {
  await page.evaluate(() => {
    document.querySelectorAll('*').forEach(el => { el.scrollTop = 0 })
    window.scrollTo(0, 0)
  })
  await page.screenshot({ path: `${OUT}/${name}.png` })
}
const fail = msg => { console.error(`FAIL: ${msg}`); process.exitCode = 1 }
/** The global sidebar row this run leaves through -- the same row for both
 *  answers, so the two frames differ ONLY in how the confirm was answered.
 *  `exact` matters: other chrome carries labels that contain these words. */
const exitRow = () => page.getByRole('button', { name: EXIT_ROW, exact: true })
/** Pane-scoped: the shell chrome carries its own 'Edit'-ish controls. */
const pane = () => page.getByTestId('side-panel-pane')

await page.goto(`${base}/capabilities?tab=prompts`, { waitUntil: 'domcontentloaded' })
await exitRow().waitFor({ timeout: 20000 })

// --- Open the inline editor on a real prompt and edit its body. ---
await pane().getByRole('option', { name: /triage/ }).click()
await pane().getByRole('button', { name: 'Edit' }).click()
await pane().getByPlaceholder(BODY_FIELD).fill(EDITED_BODY)
await shot('1-editor-dirty')

// --- Scene 2: leave via the GLOBAL SIDEBAR and DECLINE. The draft must survive. ---
answer = 'dismiss'
await exitRow().click()
await page.waitForTimeout(400)

if (dialogs.length !== 1) fail(`expected exactly 1 confirm on the declined sidebar click, saw ${dialogs.length}`)
if (dialogs[0] && !dialogs[0].message.includes(EXPECTED_CONFIRM)) {
  fail(`confirm copy was ${JSON.stringify(dialogs[0].message)}, expected it to carry ${JSON.stringify(EXPECTED_CONFIRM)}`)
}
if (!new URL(page.url()).pathname.startsWith('/capabilities')) {
  fail(`navigated away despite the declined confirm: ${page.url()}`)
}
// Counted before it is read, so a run against a build WITHOUT the guard still
// photographs what it did (an empty pane) instead of dying on a missing input.
// That is what makes this harness usable as a before/after pair.
const stillOpen = await pane().getByPlaceholder(BODY_FIELD).count() === 1
if (!stillOpen) fail('the editor unmounted despite the declined confirm -- the draft is gone')
const keptBody = stillOpen ? await pane().getByPlaceholder(BODY_FIELD).inputValue() : ''
if (stillOpen && keptBody !== EDITED_BODY) {
  fail(`draft did not survive the declined click: ${JSON.stringify(keptBody)}`)
}
// Named after what the frame SHOWS, so a base run's output cannot be mistaken
// for the guarded one.
await shot(stillOpen ? '2-draft-kept' : '2-draft-lost')

// --- Scene 3: leave and ACCEPT. The user's choice must be honoured. ---
answer = 'accept'
await exitRow().click()
await page.waitForTimeout(600)

if (dialogs.length !== 2) fail(`expected a second confirm on the accepted click, saw ${dialogs.length}`)
if (!new URL(page.url()).pathname.startsWith(EXIT_PATH)) {
  fail(`accepting the confirm did not navigate to ${EXIT_PATH}: ${page.url()}`)
}
if (await page.getByPlaceholder(BODY_FIELD).count() !== 0) {
  fail('the Prompts editor is still mounted after the accepted navigation')
}
await shot('3-draft-released')

console.log(`final url: ${page.url()}`)
console.log(`dialogs raised: ${JSON.stringify(dialogs, null, 2)}`)
console.log(process.exitCode ? 'capture FAILED' : `capture ok -> ${OUT}`)
await browser.close()
srv.close()
