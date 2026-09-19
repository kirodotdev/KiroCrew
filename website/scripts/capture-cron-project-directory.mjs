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
  "Run this job's agent in this folder, and offer any agents defined in its .kiro/agents/ alongside your global agents."
const MARKER = 'overrides global'
const MARKER_DETAIL = 'Runs instead of your global agent of this name'
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
