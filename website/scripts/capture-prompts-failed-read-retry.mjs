/**
 * Screenshot harness for the Prompts tab's failed-read Retry button (#6165).
 *
 * Same shape as capture-prompt-name-preview.mjs: serves the REAL built SPA
 * (website/dist) behind a loopback server and answers /api/** from the shared
 * fixture router.
 *
 * Three frames:
 *
 *   01-failed-read-retry     the detail fetch 500s: the read_only_failed
 *                            caption renders WITH the new Retry button —
 *                            before this change the caption stood alone and
 *                            the only recovery was re-clicking the row.
 *   02-after-retry           Retry clicked while the server answers again:
 *                            the failed state clears and content renders.
 *   03-failed-read-zh        the same failed state in zh-CN, showing the
 *                            localized button label.
 *
 * Rebuild the SPA (`npm run build`) before running.
 *
 * Usage: node scripts/capture-prompts-failed-read-retry.mjs [outDir] [prefix]
 */
import { chromium } from 'playwright'
import { mkdirSync, readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'

import { serveDist } from './lib/serve-dist.mjs'
import { json, logPageProblems, stubDashboardApi } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/prompts-failed-read-retry'
const PREFIX = process.argv[3] || 'shot'

mkdirSync(OUT, { recursive: true })

/** Read a promptsTab string out of the catalog the page will render from. The
 *  Retry label lives in the manual catalog, so fall back to it for en. */
const label = (lang, key) => {
  const read = f => JSON.parse(readFileSync(
    fileURLToPath(new URL(`../src/i18n/locales/${f}.json`, import.meta.url)),
    'utf-8',
  )).pages?.overview?.promptsTab?.[key]
  return read(lang) ?? (lang === 'en' ? read('en.manual') : undefined)
}

const PROMPTS = [
  {
    name: 'release-notes', fullName: 'release-notes', description: 'Draft release notes from a diff',
    path: '~/.kiro/prompts/release-notes.md', package: '', source: 'global',
  },
  {
    name: 'triage', fullName: 'triage', description: 'Triage an inbound issue',
    path: '~/.kiro/prompts/triage.md', package: '', source: 'global',
  },
]

const DETAIL = {
  content: '---\ndescription: Draft release notes from a diff\n---\n\nSummarize the diff as release notes.\n',
  redacted: false,
  lossy: false,
  hash: 'a'.repeat(64),
}

/** One page on the Prompts tab with the detail endpoint failing on demand. */
async function openPromptsTab(browser, { locale }) {
  const state = { failDetail: true }
  const page = await browser.newPage({
    viewport: { width: 1440, height: 900 },
    deviceScaleFactor: 2,
    locale,
  })
  logPageProblems(page)
  await stubDashboardApi(page, {
    extra: async (path, route) => {
      if (path === '/api/prompts') { await json(route, PROMPTS); return true }
      if (path.startsWith('/api/prompts/')) {
        if (state.failDetail) await json(route, { error: 'read failed' }, 500)
        else await json(route, DETAIL)
        return true
      }
      return false
    },
  })
  await page.goto(`${BASE}/capabilities?tab=prompts`, { waitUntil: 'domcontentloaded' })
  await page.locator('text=/^@release-notes$/').first().waitFor({ timeout: 20000 })
  return { page, state }
}

const shoot = (page, name) =>
  page.screenshot({ path: `${OUT}/${PREFIX}-${name}.png`, animations: 'disabled' })

const { srv, base: BASE } = await serveDist()
const browser = await chromium.launch()

try {
  // ---- 01 + 02: fail, then recover through the button, one page ------------
  const { page, state } = await openPromptsTab(browser, { locale: 'en-US' })
  await page.locator('text=/^@release-notes$/').first().click()
  await page.getByText(label('en', 'read_only_failed'), { exact: false }).waitFor()
  await page.getByRole('button', { name: label('en', 'retry_read'), exact: true }).waitFor()
  await shoot(page, '01-failed-read-retry')

  state.failDetail = false
  await page.getByRole('button', { name: label('en', 'retry_read'), exact: true }).click()
  await page.getByText('Summarize the diff as release notes.', { exact: false }).waitFor()
  await shoot(page, '02-after-retry')
  await page.close()

  // ---- 03: the failed state localized -------------------------------------
  const zh = await openPromptsTab(browser, { locale: 'zh-CN' })
  await zh.page.locator('text=/^@release-notes$/').first().click()
  await zh.page.getByRole('button', { name: label('zh-CN', 'retry_read'), exact: true }).waitFor()
  await shoot(zh.page, '03-failed-read-zh')
  await zh.page.close()

  console.log(`wrote 3 frames to ${OUT}`)
} finally {
  await browser.close()
  srv.close()
}
