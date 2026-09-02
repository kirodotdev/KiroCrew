/**
 * Screenshot harness for the Prompts tab's name field: the live sanitized
 * filename, the client-side catch when nothing survives sanitizing, and the
 * localized 400 `invalid_name`.
 *
 * Same shape as capture-profile-fallback-banner.mjs: serves the REAL built SPA
 * (website/dist) behind a loopback server and answers /api/** from the shared
 * fixture router. Nothing here talks to a gateway, which is the point — a bare
 * `vite` harness proxies /api to whatever KIROCREW_PORT names.
 *
 * Five frames, because the change has two halves and the second one only shows
 * up in a language other than English:
 *
 *   01-hint-before-typing   the generic rule. The SAME catalog string as the
 *                           preview, rendered with the literal `<name>.md` --
 *                           which is the sentence the field showed for every
 *                           name before this change.
 *   02-filename-preview     "My Prompt!" typed, hint reads the real filename.
 *   03-no-filename          a name with no character in [a-z0-9-]: the hint says
 *                           why, and Create is disabled instead of sending a
 *                           request that could only 400.
 *   04-name-too-long        the other refusal the same field can earn, caught
 *                           the same way.
 *   06-server-400-japanese  a refusal arriving from the server, in ja -- the
 *                           case Task 1 is about, where the untranslated
 *                           English "invalid prompt name" used to land.
 *
 * Rebuild the SPA (`npm run build`) before running: serve-dist serves whatever
 * is on disk, so shooting a UI change against a stale dist yields an "after"
 * frame identical to before -- indistinguishable from the change not working.
 *
 * Usage: node scripts/capture-prompt-name-preview.mjs [outDir] [prefix]
 */
import { chromium } from 'playwright'
import { mkdirSync, readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'

import { serveDist } from './lib/serve-dist.mjs'
import { json, logPageProblems, stubDashboardApi } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/prompt-name-preview'
const PREFIX = process.argv[3] || 'shot'

mkdirSync(OUT, { recursive: true })

/** Katakana, as code-point escapes: the repo forbids CJK literals in source. */
const NON_LATIN_NAME = '\u30d7\u30ed\u30f3\u30d7\u30c8'

/** Read a promptsTab string out of the catalog the page will render from, so no
 *  localized label is duplicated (and possibly mistyped) in this harness. */
const label = (lang, key) => JSON.parse(readFileSync(
  fileURLToPath(new URL(`../src/i18n/locales/${lang}.json`, import.meta.url)),
  'utf-8',
)).pages.overview.promptsTab[key]

const PROMPTS = [
  {
    name: 'release-notes', fullName: 'release-notes', description: 'Draft release notes from a diff',
    path: '~/.kiro/prompts/release-notes.md', package: '', source: 'global',
  },
  {
    name: 'triage', fullName: 'triage', description: 'Triage an inbound issue',
    path: '~/.kiro/prompts/triage.md', package: '', source: 'global',
  },
  {
    name: 'sop', fullName: 'agent-sop:sop', description: 'Shipped with the package',
    path: '~/pkg/sop.sop.md', package: 'Agent-SOP-1.0', source: 'package',
  },
]

const DETAIL = {
  content: '---\ndescription: Draft release notes from a diff\n---\n\nSummarize the diff as release notes.\n',
  redacted: false,
  lossy: false,
  hash: 'a'.repeat(64),
}

/** One page on the Prompts tab, with the create dialog open. */
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
  // POST is answered here rather than in a second page.route because the stub
  // is method-blind and a create must be able to fail on demand.
  await stubDashboardApi(page, {
    extra: async (path, route) => {
      if (path === '/api/prompts') {
        if (route.request().method() === 'POST') {
          if (invalidName) await json(route, { error: 'invalid prompt name', code: 'invalid_name' }, 400)
          else await json(route, { ok: true, name: 'my-prompt', scope: 'global' }, 201)
        } else {
          await json(route, PROMPTS)
        }
        return true
      }
      if (path.startsWith('/api/prompts/')) { await json(route, DETAIL); return true }
      return false
    },
  })

  await page.goto(`${BASE}/capabilities?tab=prompts`, { waitUntil: 'domcontentloaded' })
  await page.locator('text=/^@release-notes$/').first().waitFor({ timeout: 20000 })
  await page.getByRole('button', { name: label(lang, 'create_new_prompt') }).first().click()
  await page.locator('input[placeholder="my-prompt-name"]').waitFor()
  return page
}

const shoot = (page, name) =>
  page.screenshot({ path: `${OUT}/${PREFIX}-${name}.png`, animations: 'disabled' })

const { srv, base: BASE } = await serveDist()
const browser = await chromium.launch()

try {
  // ---- 01 + 02 + 03: the field's three states, one page ---------------------
  const page = await openCreateDialog(browser, { lang: 'en', locale: 'en-US' })
  const nameField = page.locator('input[placeholder="my-prompt-name"]')

  await shoot(page, '01-hint-before-typing')

  await page.locator('textarea').fill('Summarize the diff as release notes.')
  await nameField.fill('My Prompt!')
  await page.getByText('Saved as my-prompt.md', { exact: false }).waitFor()
  await shoot(page, '02-filename-preview')

  await nameField.fill(NON_LATIN_NAME)
  await page.getByText(label('en', 'invalid_name_hint'), { exact: false }).waitFor()
  await shoot(page, '03-no-filename-create-disabled')

  // The other refusal the same field can earn. `{{max}}` is interpolated at
  // render time, so match on the copy either side of it rather than the whole
  // sentence.
  await nameField.fill('a'.repeat(200))
  await page.getByText('bytes and this name makes a longer one', { exact: false }).waitFor()
  await shoot(page, '04-name-too-long-create-disabled')
  await page.close()

  // ---- 05: the server's own refusal, translated ----------------------------
  const ja = await openCreateDialog(browser, { lang: 'ja', locale: 'ja-JP', invalidName: true })
  await ja.locator('textarea').fill('Summarize the diff as release notes.')
  // A name the client mirror accepts, so the request is actually sent and the
  // 400 has to be rendered by writeError rather than pre-empted by the gate.
  await ja.locator('input[placeholder="my-prompt-name"]').fill('ok-name')
  await ja.getByRole('button', { name: label('ja', 'create'), exact: true }).click()
  // Rendered twice by design: mutationError feeds both the detail pane's error
  // row and the create modal's own, so a failed DELETE (which has no form)
  // still reports. .first() picks the pane one; the frame shows both.
  await ja.getByText(label('ja', 'invalid_name_hint'), { exact: false }).first().waitFor()
  await shoot(ja, '06-server-400-japanese')
  await ja.close()

  console.log(`wrote 5 frames to ${OUT}`)
} finally {
  await browser.close()
  srv.close()
}
