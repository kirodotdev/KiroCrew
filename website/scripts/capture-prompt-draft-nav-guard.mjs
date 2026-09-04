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
 * That rig (boot, stubs, prompt fixtures, dialog recorder, screenshot helper) is
 * shared with capture-prompt-draft-back-guard.mjs -- see
 * lib/prompt-draft-harness.mjs. Only the exit differs.
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
import {
  startPromptDraftHarness, EDITED_BODY, EXPECTED_CONFIRM, BODY_FIELD,
} from './lib/prompt-draft-harness.mjs'
/** Settings, one of the sidebar destinations the issue names. Any route works:
 *  the guard is on leaving, not on where to. */
const EXIT_ROW = 'Settings'
const EXIT_PATH = '/settings'

const rig = await startPromptDraftHarness(process.argv[2] || '../temp-screenshots/prompt-draft-nav-guard')
const { base, page, dialogs, pane, answer, shot, fail } = rig
/** The global sidebar row this run leaves through -- the same row for both
 *  answers, so the two frames differ ONLY in how the confirm was answered.
 *  `exact` matters: other chrome carries labels that contain these words. */
const exitRow = () => page.getByRole('button', { name: EXIT_ROW, exact: true })

await page.goto(`${base}/capabilities?tab=prompts`, { waitUntil: 'domcontentloaded' })
await exitRow().waitFor({ timeout: 20000 })

// --- Open the inline editor on a real prompt and edit its body. ---
await pane().getByRole('option', { name: /triage/ }).click()
await pane().getByRole('button', { name: 'Edit' }).click()
await pane().getByPlaceholder(BODY_FIELD).fill(EDITED_BODY)
await shot('1-editor-dirty')

// --- Scene 2: leave via the GLOBAL SIDEBAR and DECLINE. The draft must survive. ---
answer('dismiss')
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
answer('accept')
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

await rig.done()
