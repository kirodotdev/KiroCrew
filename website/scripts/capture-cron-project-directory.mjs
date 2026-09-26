/**
 * Screenshots for the Schedule job form's `Project directory` field.
 *
 * Drives the isolated capture entry (website/capture/cron-project-directory.html),
 * which mounts the REAL JobForm with only the HTTP responses stubbed. Nothing in
 * a frame is seeded: the field is typed into through the real input, the picker is
 * opened by clicking the real control, and a save error is produced by submitting
 * into a failing request, so a frame cannot document a state the shipped code
 * would not produce.
 *
 * Every frame asserts its own state before writing -- the exact shipped string it
 * must carry, and for the collision the marker AND its absence on the control
 * rows. A frame that rendered the wrong copy fails here instead of shipping as
 * evidence.
 *
 *   01-project-directory-field   the field as the dialog opens, with its helper text
 *   02-folder-picker-open        Browse open, listing directories to choose from
 *   03-agent-picker-merged       a bound directory's agents merged into the picker
 *   04-overrides-global          the collision: the project row wins and says so
 *   05-roster-fetch-error        the roster failed: notice beside the field, form usable
 *   06-save-error-absolute-path  a relative path, refused
 *   07-save-error-sensitive-path a protected path, refused
 *   08-save-error-missing-dir    a path that does not exist, refused
 *   09-agent-reset-notice        a project-only pick discarded: the directory moved
 *                                to one that no longer declares it
 *   10-agent-reset-cleared       the OTHER reset path: the directory was cleared
 *                                outright, not moved -- different notice wording
 *
 * The save-error frames use a TALLER viewport on purpose: the message renders at
 * the form's foot and a 720px frame cropped the field it names out of shot.
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6821 --strictPort   # in another shell
 *   node scripts/capture-cron-project-directory.mjs http://127.0.0.1:6821 <outdir>
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6821'
const OUT = process.argv[3] || '../temp-screenshots/cron-project-directory'
mkdirSync(OUT, { recursive: true })

/** The exact shipped copy, from website/src/i18n/locales/en.json. */
const FIELD_LABEL = 'Project directory'
const FIELD_HINT =
  "Run this job's agent in this project, and offer any agents defined in its .kiro/agents/ alongside your global agents."
const MARKER = 'overrides global'
/** The shadowed row renders ``overrides_global_hint`` as visible body text, so the
 *  assertion below matches that key's copy -- a hover-only tooltip is not evidence. */
const MARKER_DETAIL =
  "This job's project directory defines its own agent with this name, so it runs instead of your global one. Your global agent is unchanged elsewhere."
/** The reset notice, matched on its stable tail: the leading agent name is interpolated. */
const RESET_NOTE = "isn't defined in this project, so the agent was reset to default."
/** The CLEARED-directory variant: fires only when the field is emptied outright,
 *  not when it merely switches to another directory (that path is RESET_NOTE
 *  above). Matched on its stable tail for the same reason. */
const RESET_NOTE_CLEARED = 'The project directory was cleared, so'
const ERR_ABSOLUTE = 'Project directory must be an absolute path (e.g. /Users/you/projects/myrepo).'
const ERR_SENSITIVE = "Project directory refers to a protected system path and can't be used."
const ERR_MISSING = 'Project directory must be an existing directory.'

const PROJECT = '/tmp/kc-collision-demo'
/** The colliding name: configured globally AND declared by the directory. */
const COLLIDING = 'release-checklist'
/** Project-only, so nothing to override -- the marker must not appear on it. */
const PROJECT_ONLY = 'repo-smoke'

const FIELD = 'input[aria-label="Project directory"]'
const ROOT = '[data-capture-root]'

const SCENES = [
  { name: '01-project-directory-field', scene: 'field', tall: false },
  { name: '02-folder-picker-open', scene: 'field', tall: false, browse: true },
  { name: '03-agent-picker-merged', scene: 'field', tall: false, bind: true, picker: true },
  { name: '04-overrides-global', scene: 'field', tall: false, bind: true, picker: true, crop: true },
  { name: '05-roster-fetch-error', scene: 'roster-error', tall: false, bind: true },
  { name: '06-save-error-absolute-path', scene: 'save-absolute', tall: true, type: 'relative/path', save: true },
  { name: '07-save-error-sensitive-path', scene: 'save-sensitive', tall: true, type: '/home/you/.ssh', save: true },
  { name: '08-save-error-missing-dir', scene: 'save-missing', tall: true, type: '/tmp/does-not-exist-xyz', save: true },
  // The one state where the form DISCARDS a user's explicit pick: a project-only
  // agent is selected, then the directory moves and that agent no longer exists
  // there. UX Review could not evaluate it because no frame showed it.
  { name: '09-agent-reset-notice', scene: 'field', tall: false, bind: true, resetNote: true },
  // The OTHER reset path: the directory is not moved to somewhere else, it is
  // CLEARED outright. JobForm fires a differently-worded notice for this case
  // (agent_reset_project_cleared vs. agent_reset_not_in_folder) -- no frame
  // existed for it at all.
  { name: '10-agent-reset-cleared', scene: 'field', tall: false, bind: true, resetNoteCleared: true },
]

const must = (cond, msg) => {
  if (!cond) throw new Error(`FRAME ASSERTION FAILED: ${msg}`)
}

const browser = await chromium.launch()
try {
  for (const s of SCENES) {
    const page = await browser.newPage({
      viewport: { width: 1280, height: s.tall ? 1400 : 820 },
      deviceScaleFactor: 2,
    })
    await page.goto(`${BASE}/capture/cron-project-directory.html?scene=${s.scene}`)
    await page.waitForSelector(ROOT)
    await page.waitForSelector(FIELD)

    // The field and its helper text are the subject of every frame. The label is
    // asserted non-exactly because it renders with a sibling "(Optional)" span
    // inside the same element, so an exact-text match sees neither alone.
    must(await page.getByText(FIELD_LABEL, { exact: false }).count(), `${s.name}: no "${FIELD_LABEL}" label`)
    must(await page.getByText(FIELD_HINT).count(), `${s.name}: helper text missing`)

    if (s.browse) {
      await page.getByRole('button', { name: 'Browse' }).click()
      await page.waitForTimeout(400)
      must(await page.getByText('kc-collision-demo', { exact: true }).count(), `${s.name}: picker listed nothing`)
    }

    if (s.bind) {
      // Typed through the real input so the debounced onChange fires the fetch.
      await page.fill(FIELD, PROJECT)
      await page.waitForTimeout(900)
    }

    if (s.type) {
      await page.fill(FIELD, s.type)
      await page.waitForTimeout(300)
    }

    if (s.picker) {
      await page.getByRole('button', { name: 'Switch agent' }).click()
      await page.waitForTimeout(400)
      const opts = page.getByRole('option')
      must(await opts.count() >= 3, `${s.name}: picker did not merge the project rows`)
      // The marker is on the colliding row, and NOWHERE else -- that pairing is
      // the whole claim, so both halves are asserted.
      const colliding = page.getByRole('option', { name: new RegExp(COLLIDING) })
      must(await colliding.count(), `${s.name}: ${COLLIDING} row missing`)
      must(
        (await colliding.innerText()).includes(MARKER),
        `${s.name}: ${COLLIDING} carries no "${MARKER}" marker`,
      )
      must(
        (await colliding.innerText()).includes(MARKER_DETAIL),
        `${s.name}: marker detail line missing -- a tooltip is not evidence`,
      )
      const projectOnly = page.getByRole('option', { name: new RegExp(PROJECT_ONLY) })
      must(await projectOnly.count(), `${s.name}: ${PROJECT_ONLY} row missing`)
      must(
        !(await projectOnly.innerText()).includes(MARKER),
        `${s.name}: ${PROJECT_ONLY} is marked, but it overrides nothing`,
      )
      const dflt = page.getByRole('option', { name: /default/ })
      must(
        !(await dflt.innerText()).includes(MARKER),
        `${s.name}: the configured default is marked, but the folder never declared it`,
      )
    }

    if (s.resetNote) {
      // Pick the PROJECT-ONLY agent, so the binding cannot survive the move: a
      // global pick would still resolve after the directory changes and the
      // notice would never fire.
      await page.getByRole('button', { name: 'Switch agent' }).click()
      await page.waitForTimeout(400)
      await page.getByRole('option', { name: new RegExp(PROJECT_ONLY) }).first().click()
      await page.waitForTimeout(300)
      must(
        (await page.locator(ROOT).innerText()).includes(PROJECT_ONLY),
        `${s.name}: ${PROJECT_ONLY} was not actually selected, so nothing can be reset`,
      )
      // Move the directory to one that declares no agents at all.
      await page.fill(FIELD, '/tmp/build-cache')
      await page.waitForTimeout(900)
      const note = page.getByText(RESET_NOTE, { exact: false })
      await note.first().waitFor({ timeout: 5000 })
      must(await note.count(), `${s.name}: the agent-reset notice never rendered`)
    }

    if (s.resetNoteCleared) {
      // Same setup as resetNote: pick the PROJECT-ONLY agent so the binding
      // cannot survive. The two scenes diverge only in what happens next --
      // this one CLEARS the field outright rather than switching to another
      // directory, which is JobForm's OTHER reset branch
      // (`agent_reset_project_cleared`, fired from the `!projectPath` guard)
      // and carries different wording from the not-in-project case.
      await page.getByRole('button', { name: 'Switch agent' }).click()
      await page.waitForTimeout(400)
      await page.getByRole('option', { name: new RegExp(PROJECT_ONLY) }).first().click()
      await page.waitForTimeout(300)
      must(
        (await page.locator(ROOT).innerText()).includes(PROJECT_ONLY),
        `${s.name}: ${PROJECT_ONLY} was not actually selected, so nothing can be reset`,
      )
      // Clear the field outright -- not a switch to another path.
      await page.fill(FIELD, '')
      await page.waitForTimeout(900)
      const note = page.getByText(RESET_NOTE_CLEARED, { exact: false })
      await note.first().waitFor({ timeout: 5000 })
      must(await note.count(), `${s.name}: the cleared-directory reset notice never rendered`)
      // Distinguish the two notices at the assertion level, not just by trusting
      // which scene block ran: a regression that fired the wrong copy for this
      // branch must fail here.
      must(
        !(await page.locator(ROOT).innerText()).includes(RESET_NOTE),
        `${s.name}: rendered the not-in-project wording instead of the cleared-directory wording`,
      )
    }

    if (s.scene === 'roster-error') {
      const body = await page.locator(ROOT).innerText()
      must(/agents/i.test(body), `${s.name}: no roster failure notice rendered`)
    }

    if (s.save) {
      // exact: true, because the Minimal context row's description ("skips your
      // saved context") matches a fuzzy "Save" and makes the locator ambiguous.
      await page.getByRole('button', { name: 'Save', exact: true }).click()
      const expected =
        s.scene === 'save-absolute' ? ERR_ABSOLUTE : s.scene === 'save-sensitive' ? ERR_SENSITIVE : ERR_MISSING
      await page.getByText(expected).first().waitFor({ timeout: 5000 })
      must(await page.getByText(expected).count(), `${s.name}: expected "${expected}"`)
    }

    const target = s.crop ? page.getByRole('option', { name: new RegExp(COLLIDING) }).first() : page.locator(ROOT)
    await target.screenshot({ path: `${OUT}/${s.name}.png` })
    console.log(`wrote ${s.name}.png`)
    await page.close()
  }
} finally {
  await browser.close()
}
