/**
 * Screenshot harness for the Skills tab's name field: the live sanitized
 * filename, the client-side catch when a segment the user typed would not
 * survive sanitizing, and the localized 400 `invalid_name`.
 *
 * Same shape as capture-prompt-name-preview.mjs: serves the REAL built SPA
 * (website/dist) behind a loopback server and answers /api/** from the shared
 * fixture router. Nothing here talks to a gateway, which is the point -- a bare
 * `vite` harness proxies /api to whatever KIROCREW_PORT names.
 *
 * Seven frames. Skills differ from prompts in having a second field that feeds the
 * stored name -- the optional Category -- and in letting BOTH fields nest, so the
 * frames build up the composition the gate has to reason about:
 *
 *   01-hint-before-typing   the generic rule, rendered from the SAME catalog
 *                           string as the preview with the literal `<name>`.
 *   02-filename-preview     "My Tool!" typed, hint reads the stored filename.
 *   03-combined-preview     the same name UNDER a category: the hint previews the
 *                           combined `utils/code-review` stem, because that is
 *                           what the tab POSTs and what the server sanitizes.
 *   04-no-filename          a name in a non-Latin script, WITH a category filled.
 *                           Captured that way deliberately: `utils/<non-Latin>`
 *                           sanitizes to a non-empty `utils`, so this is the frame
 *                           that shows the gate reading each segment rather than
 *                           the combined path -- the hint is red and Create is
 *                           disabled instead of the server storing a skill named
 *                           `utils` with the typed name discarded.
 *   05-server-400-japanese  a refusal arriving from the server, in ja -- the case
 *                           where the untranslated English "invalid skill name"
 *                           would otherwise land.
 *   06-nested-segment       the same defect one level DOWN, with NO category: a
 *                           vanishing segment nested inside Name. This is why the
 *                           gate judges segments rather than whole fields.
 *   07-nested-name-allowed  the control -- a nested name whose every segment
 *                           survives is still creatable, so the gate is not simply
 *                           refusing slashes.
 *
 * Rebuild the SPA (`npm run build`) before running: serve-dist serves whatever is
 * on disk, so shooting a UI change against a stale dist yields an "after" frame
 * identical to before -- indistinguishable from the change not working.
 *
 * Usage: node scripts/capture-skills-name-preview.mjs [outDir] [prefix]
 */
import { chromium } from 'playwright'
import { mkdirSync, readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'

import { serveDist } from './lib/serve-dist.mjs'
import { json, logPageProblems, stubDashboardApi } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/skills-name-preview'
const PREFIX = process.argv[3] || 'shot'

mkdirSync(OUT, { recursive: true })

/** Katakana, as code-point escapes: the repo forbids CJK literals in source. */
const NON_LATIN_NAME = '\u30b9\u30ad\u30eb'

/** Read a string out of the catalog the page will render from, so no localized
 *  label is duplicated (and possibly mistyped) in this harness. */
const catalog = lang => JSON.parse(readFileSync(
  fileURLToPath(new URL(`../src/i18n/locales/${lang}.json`, import.meta.url)),
  'utf-8',
))
const tabLabel = (lang, key) => catalog(lang).pages.overview.skillsTab[key]
const formLabel = (lang, key) => catalog(lang).components.skillForm[key]

const SKILLS = [
  {
    key: 'release-notes', name: 'release-notes', description: 'Draft release notes from a diff',
    source: 'kirocrew', loaded_by_agents: [], always: false,
  },
  {
    key: 'utils/triage', name: 'utils/triage', description: 'Triage an inbound issue',
    source: 'kirocrew', loaded_by_agents: [], always: false,
  },
]

/** `GET /api/skills/{name}/-/tree` — the ENVELOPE matters, not the contents.
 *
 *  The shared stub's catch-all answers an unmapped path with `[]`, and the
 *  directory browser destructures `entries` out of it: a bare array throws
 *  "e is not iterable" into the app-shell error boundary, which unmounts the tab
 *  mid-capture. The failure is a blank frame rather than a crash, so it has to be
 *  named here rather than left to the guess. */
const SKILL_TREE = {
  name: 'release-notes', root: '~/.kiro/crew/skills/release-notes', entries: [],
}

/** One page on the Skills tab, with the create dialog open. */
async function openCreateDialog(browser, { lang, locale, invalidName }) {
  const page = await browser.newPage({
    viewport: { width: 1440, height: 900 },
    deviceScaleFactor: 2,
    // The context locale, not localStorage: `mc-lang` mirrors an EXPLICIT user
    // choice and the boot path clears it back to auto when the config carries
    // none, so seeding it does not survive. `navigator.languages` is the other
    // input `resolveLanguage` reads, and it does.
    locale,
  })
  logPageProblems(page)
  // Feature fixtures only; boot-path endpoints come from the shared stub. The
  // POST is answered here rather than in a second page.route because the stub is
  // method-blind and a create must be able to fail on demand.
  await stubDashboardApi(page, {
    extra: async (path, route) => {
      if (path === '/api/skills') {
        if (route.request().method() === 'POST') {
          if (invalidName) await json(route, { error: 'invalid skill name', code: 'invalid_name' }, 400)
          else await json(route, { ok: true, name: 'my-tool' }, 200)
        } else {
          await json(route, SKILLS)
        }
        return true
      }
      if (path.endsWith('/-/tree')) { await json(route, SKILL_TREE); return true }
      return false
    },
  })

  await page.goto(`${BASE}/capabilities?tab=skills`, { waitUntil: 'domcontentloaded' })
  // Wait for the list to resolve: while `skills` is loading the tab renders its
  // skeleton branch, where the Create button exists but is DISABLED.
  await page.locator('text=/^release-notes$/').first().waitFor({ timeout: 20000 })
  await page.getByRole('button', { name: tabLabel(lang, 'create_new_skill') }).first().click()
  await page.locator(`input[placeholder="${formLabel(lang, 'e_g_my_tool')}"]`).waitFor()
  return page
}

const shoot = (page, name) =>
  page.screenshot({ path: `${OUT}/${PREFIX}-${name}.png`, animations: 'disabled' })

/** The hint sentence either side of the `{{filename}}` placeholder, so a frame
 *  can wait on the rendered preview without hardcoding the whole sentence. */
const previewText = (lang, filename) =>
  formLabel(lang, 'name_preview').replace('{{filename}}', filename)

const { srv, base: BASE } = await serveDist()
const browser = await chromium.launch()

try {
  // ---- 01 -> 04, 06, 07: the field's states, one page ----------------------
  const page = await openCreateDialog(browser, { lang: 'en', locale: 'en-US' })
  const nameField = page.locator(`input[placeholder="${formLabel('en', 'e_g_my_tool')}"]`)
  const categoryField = page.locator(`input[placeholder="${formLabel('en', 'e_g_utils_code')}"]`)

  // Every frame waits on its own rendered text before shooting. An unasserted
  // frame is the one that can go blank -- or stale -- without the run failing.
  await page.getByText(previewText('en', '<name>'), { exact: false }).waitFor()
  await shoot(page, '01-hint-before-typing')

  await nameField.fill('My Tool!')
  await page.getByText(previewText('en', 'my-tool'), { exact: false }).waitFor()
  await shoot(page, '02-filename-preview')

  // The combined stem: category-then-name, each sanitized, joined by the
  // surviving slash. Computing this on `name` alone would show `code-review` for
  // a file the server writes at `utils/code-review`.
  await categoryField.fill('utils')
  await nameField.fill('Code Review')
  await page.getByText(previewText('en', 'utils/code-review'), { exact: false }).waitFor()
  await shoot(page, '03-combined-category-name-preview')

  // WITH the category still filled. `utils/<non-Latin>` sanitizes to a non-empty
  // `utils`, so a gate reading the combined path would leave Create enabled here.
  await nameField.fill(NON_LATIN_NAME)
  await page.getByText(formLabel('en', 'invalid_name_hint'), { exact: false }).waitFor()
  await shoot(page, '04-no-filename-create-disabled')

  // The same defect one level DOWN, and the reason the gate judges segments rather
  // than fields: nesting is typed into Name directly, so `utils/<non-Latin>` needs
  // no category at all. It sanitizes to a non-empty `utils`, so a whole-field check
  // passes and the server would store `utils` with the typed word gone.
  await categoryField.fill('')
  await nameField.fill(`utils/${NON_LATIN_NAME}`)
  await page.getByText(formLabel('en', 'invalid_name_hint'), { exact: false }).waitFor()
  await shoot(page, '06-nested-segment-vanishes-create-disabled')

  // The control: a nested name whose every segment survives is still creatable.
  await nameField.fill('utils/code')
  await page.getByText(previewText('en', 'utils/code'), { exact: false }).waitFor()
  await shoot(page, '07-nested-name-allowed')
  await page.close()

  // ---- 05: the server's own refusal, translated ----------------------------
  const ja = await openCreateDialog(browser, { lang: 'ja', locale: 'ja-JP', invalidName: true })
  // A name the client mirror accepts, so the request is actually sent and the 400
  // has to be rendered from its code rather than pre-empted by the gate.
  await ja.locator(`input[placeholder="${formLabel('ja', 'e_g_my_tool')}"]`).fill('ok-name')
  await ja.getByRole('button', { name: tabLabel('ja', 'create'), exact: true }).click()
  // The refusal renders UNDER the form, inside the modal's own scroll container,
  // so `waitFor()` alone is not enough: Playwright calls an element outside the
  // scrolled region visible (it has a box), and the frame would show the top of a
  // form with the evidence below the fold. `ok-name` previews fine, so this text
  // appears exactly once -- on the createError paragraph, not the Name hint.
  const refusal = ja.getByText(formLabel('ja', 'invalid_name_hint'), { exact: false })
  await refusal.waitFor()
  await refusal.scrollIntoViewIfNeeded()
  await shoot(ja, '05-server-400-japanese')
  await ja.close()

  console.log(`wrote 7 frames to ${OUT}`)
} finally {
  await browser.close()
  srv.close()
}
