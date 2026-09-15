/**
 * Screenshots + assertions for the Prompts draft guard on the BROWSER'S OWN BACK
 * BUTTON (#8010).
 *
 * The sibling harness (capture-prompt-draft-nav-guard.mjs) covers the global
 * sidebar. This one covers the exit no component owns: a Back press reaches no
 * click handler, `beforeunload` stays silent because the document never unloads,
 * and the veto is reached instead through the history stack — while the pane holds
 * a draft, `NavigationBackGuard` keeps one duplicate entry, so the first Back
 * lands on the pane's own entry with the page still mounted and asks there.
 *
 * The rig (real built SPA over serveDist, stubbed /api, prompt fixtures, dialog
 * recorder, screenshot helper) is shared with that sibling — see
 * lib/prompt-draft-harness.mjs. Only the exit differs.
 *
 * The confirm is the browser's NATIVE dialog, so it cannot appear in a page
 * screenshot. This script therefore ASSERTS it — one dialog per Back press,
 * carrying the pane's own discard copy — and captures the OUTCOME of each answer.
 * A run where no dialog fires FAILS rather than quietly producing plausible
 * frames, which is exactly what the bug looked like.
 *
 * Scenes:
 *   1-editor-dirty            the inline editor holding an edited body
 *   2-draft-kept-on-back      Back pressed, confirm DISMISSED -> still on Prompts,
 *                             text intact (named 2-draft-lost-on-back instead when
 *                             the editor did not survive, which is what a build
 *                             without the guard produces)
 *   3-draft-released-on-back  Back pressed, confirm ACCEPTED -> the earlier page
 *
 * Usage:
 *   npm run build
 *   node scripts/capture-prompt-draft-back-guard.mjs [outDir]
 */
import {
  startPromptDraftHarness, EDITED_BODY, EXPECTED_CONFIRM, BODY_FIELD,
} from './lib/prompt-draft-harness.mjs'

/** The page Back returns to. Loaded first, as a real earlier history entry —
 *  which is what makes the accepted answer observable at all. */
const ORIGIN_PATH = '/settings'

const rig = await startPromptDraftHarness(process.argv[2] || '../temp-screenshots/prompt-draft-back-guard')
const { base, page, dialogs, pane, answer, shot, fail } = rig

/** The sidebar rows this run travels through. `exact` matters: other chrome
 *  carries labels containing these words. */
const capabilitiesRow = () => page.getByRole('button', { name: 'Agent Capabilities', exact: true })
const originRow = () => page.getByRole('button', { name: 'Settings', exact: true })
/** The gesture under test, driven the way the platform delivers it. Not
 *  `page.goBack()`: that awaits a document navigation, and the press this guard
 *  answers is same-document by construction (it pops onto the duplicate entry). */
const pressBack = async () => {
  await page.evaluate(() => { window.history.back() })
  await page.waitForTimeout(600)
}

await page.goto(`${base}/capabilities?tab=prompts`, { waitUntil: 'domcontentloaded' })
await originRow().waitFor({ timeout: 20000 })
await pane().getByRole('option', { name: /triage/ }).waitFor({ timeout: 20000 })
// Out and back through the SIDEBAR, so the entry Back returns to is same-document
// (the layout's rememberKey restores the Prompts tab on the way in). Deliberately
// not a second `page.goto`: that would put a document boundary between the two
// entries, so the accepted Back would be a cross-document unload and PromptsTab's
// `beforeunload` would raise a dialog of its own on top of the guard's. A real
// session arrives here by soft navigation, which is the case this guard is for.
await originRow().click()
await page.waitForURL(`**${ORIGIN_PATH}`, { timeout: 20000 })
await capabilitiesRow().click()
await pane().getByRole('option', { name: /triage/ }).waitFor({ timeout: 20000 })

// --- Open the inline editor on a real prompt and edit its body. ---
await pane().getByRole('option', { name: /triage/ }).click()
await pane().getByRole('button', { name: 'Edit' }).click()
await pane().getByPlaceholder(BODY_FIELD).fill(EDITED_BODY)
await shot('1-editor-dirty')

// --- Scene 2: press BACK and DECLINE. The draft must survive, and the address
// must not have moved. ---
answer('dismiss')
await pressBack()

if (dialogs.length !== 1) fail(`expected exactly 1 confirm on the declined Back, saw ${dialogs.length}`)
if (dialogs[0] && !dialogs[0].message.includes(EXPECTED_CONFIRM)) {
  fail(`confirm copy was ${JSON.stringify(dialogs[0].message)}, expected it to carry ${JSON.stringify(EXPECTED_CONFIRM)}`)
}
if (!new URL(page.url()).pathname.startsWith('/capabilities')) {
  fail(`left the page despite the declined confirm: ${page.url()}`)
}
// Counted before it is read, so a run against a build WITHOUT the guard still
// photographs what it did (an empty pane, or the earlier page) instead of dying on
// a missing input. That is what makes this harness usable as a before/after pair.
const stillOpen = await pane().getByPlaceholder(BODY_FIELD).count() === 1
if (!stillOpen) fail('the editor unmounted despite the declined confirm -- the draft is gone')
const keptBody = stillOpen ? await pane().getByPlaceholder(BODY_FIELD).inputValue() : ''
if (stillOpen && keptBody !== EDITED_BODY) {
  fail(`draft did not survive the declined Back: ${JSON.stringify(keptBody)}`)
}
// Named after what the frame SHOWS, so a base run's output cannot be mistaken for
// the guarded one.
await shot(stillOpen ? '2-draft-kept-on-back' : '2-draft-lost-on-back')

// --- Scene 3: press BACK again and ACCEPT. One press must be enough: the
// duplicate entry absorbed the pop, so the guard owes the real one. ---
answer('accept')
await pressBack()

if (dialogs.length !== 2) fail(`expected a second confirm on the next Back, saw ${dialogs.length}`)
if (new URL(page.url()).pathname !== ORIGIN_PATH) {
  fail(`accepting the confirm did not land on ${ORIGIN_PATH} in one press: ${page.url()}`)
}
if (await pane().getByPlaceholder(BODY_FIELD).count() !== 0) {
  fail('the Prompts editor is still mounted after the accepted Back')
}
await capabilitiesRow().waitFor({ timeout: 20000 })
await shot('3-draft-released-on-back')

await rig.done()
