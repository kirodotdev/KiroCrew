/**
 * Screenshots for the Prompts tab-switch draft guard (#7358).
 *
 * Drives the isolated capture entry (website/capture/prompt-draft-tab-guard.html),
 * which renders the REAL SidePanelLayout rail around the REAL PromptsTab with
 * only the two /api/prompts reads stubbed.
 *
 * The confirm is the browser's NATIVE dialog, so it cannot appear in a page
 * screenshot. This script therefore ASSERTS it -- one dialog per rail click,
 * carrying the pane's own discard copy -- and captures the OUTCOME of each
 * answer. A run where no dialog fires fails rather than quietly producing three
 * plausible frames, which is what a regression would look like.
 *
 * It also pins the reachability fact the guard's scope depends on: while the
 * CREATE modal is open its backdrop covers the rail, so a create draft cannot be
 * lost to a tab click and the guard deliberately does not cover it. That is
 * asserted here (the click must be intercepted) rather than left as a claim in a
 * comment.
 *
 * Scenes:
 *   1-editor-dirty    the inline editor holding an edited body, before any switch
 *   2-draft-kept      rail clicked, confirm DISMISSED -> still on Prompts, text intact
 *   3-draft-released  rail clicked, confirm ACCEPTED  -> Steering shown, editor closed
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6811 --strictPort   # in another shell
 *   node scripts/capture-prompt-draft-tab-guard.mjs http://127.0.0.1:6811 ../temp-screenshots/prompt-draft-tab-guard
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6811'
const OUT = process.argv[3] || '../temp-screenshots/prompt-draft-tab-guard'
mkdirSync(OUT, { recursive: true })

const EDITED_BODY = 'Read the report, classify it, and name the one experiment that would settle the diagnosis.'
const EXPECTED_CONFIRM = 'Discard unsaved changes?'
const BODY_FIELD = /markdown the agent receives/

const browser = await chromium.launch()
const page = await browser.newPage({ viewport: { width: 1440, height: 900 } })

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

await page.goto(`${BASE}/capture/prompt-draft-tab-guard.html?theme=dark`, { waitUntil: 'networkidle' })

// --- Reachability check: the create modal's backdrop covers the rail, which is
// why the guard scopes to the editor. Assert the rail is genuinely unclickable
// while the modal is open, then close it and carry on. ---
await page.getByText('Create New Prompt').first().click()
await page.getByPlaceholder('my-prompt-name').fill('scratch')
let railReachableUnderModal = true
try {
  await page.getByRole('button', { name: 'Steering' }).click({ timeout: 1500 })
} catch {
  railReachableUnderModal = false
}
if (railReachableUnderModal) {
  fail('the rail WAS clickable with the create modal open -- the create form needs the guard too')
}
if (dialogs.length !== 0) fail(`a confirm fired while the modal was open, saw ${dialogs.length}`)
// Closing a DIRTY create form raises the pane's own discard confirm; accept it,
// otherwise the modal stays open and every later step is stuck behind it.
answer = 'accept'
await page.getByRole('button', { name: 'Cancel' }).click()
answer = 'dismiss'
await page.waitForTimeout(150)
dialogs.length = 0

// --- Open the inline editor on a real prompt and edit its body. ---
await page.getByRole('option', { name: /triage/ }).click()
await page.getByRole('button', { name: 'Edit' }).click()
await page.getByPlaceholder(BODY_FIELD).fill(EDITED_BODY)
await shot('1-editor-dirty')

// --- Scene 2: switch away and DECLINE. The draft must survive. ---
answer = 'dismiss'
await page.getByRole('button', { name: 'Steering' }).click()
await page.waitForTimeout(250)

if (dialogs.length !== 1) fail(`expected exactly 1 confirm on the declined switch, saw ${dialogs.length}`)
if (dialogs[0] && !dialogs[0].message.includes(EXPECTED_CONFIRM)) {
  fail(`confirm copy was ${JSON.stringify(dialogs[0].message)}, expected it to carry ${JSON.stringify(EXPECTED_CONFIRM)}`)
}
if (await page.getByTestId('other-pane').count() !== 0) {
  fail('the Steering pane rendered even though the confirm was declined')
}
const keptBody = await page.getByPlaceholder(BODY_FIELD).inputValue()
if (keptBody !== EDITED_BODY) fail(`draft did not survive the declined switch: ${JSON.stringify(keptBody)}`)
await shot('2-draft-kept')

// --- Scene 3: switch away and ACCEPT. The user's choice must be honoured. ---
answer = 'accept'
await page.getByRole('button', { name: 'Steering' }).click()
await page.waitForTimeout(250)

if (dialogs.length !== 2) fail(`expected a second confirm on the accepted switch, saw ${dialogs.length}`)
if (await page.getByTestId('other-pane').count() !== 1) {
  fail('accepting the confirm did not switch to the Steering pane')
}
await shot('3-draft-released')

console.log(`rail clickable under create modal: ${railReachableUnderModal}`)
console.log(`dialogs raised: ${JSON.stringify(dialogs, null, 2)}`)
console.log(process.exitCode ? 'capture FAILED' : `capture ok -> ${OUT}`)
await browser.close()
