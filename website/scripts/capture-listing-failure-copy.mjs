/**
 * Screenshot harness + behavior check for the folder panel's FAILURE COPY.
 *
 * Both notices used to read `(err as Error)?.message || t(...)`. A deadline
 * rejects with a truthy message, so the message won and every timeout rendered
 * that English string -- in all 13 catalogs. `ja` is a scene axis rather than a
 * nicety: an `en` frame alone cannot distinguish translated copy from the leaked
 * rejection text.
 *
 * Covers the full cause matrix on both arms, because each cause is a different
 * string and a frame of one says nothing about the others. Light theme is a scene
 * too -- the notice is coloured copy, so its contrast is theme-dependent.
 *
 * This ASSERTS as well as photographs. Each scene requires the translated string
 * present AND `deadline exceeded` absent -- presence alone would pass while both
 * were on screen. Exits non-zero on any failure. Nothing in CI runs this file; the
 * CI-enforced half is the `cause-keyed` block in FolderPanel.search.test.tsx.
 *
 * Output is LOCAL and uncommitted (temp-screenshots/ is gitignored): attach the
 * frames to the PR with `gh pr edit <n> --attach`, which is how review evidence
 * reaches a reviewer.
 *
 * Usage: node scripts/capture-listing-failure-copy.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { createServer } from 'vite'
import { chromiumExecutable } from './lib/chromium-executable.mjs'

const OUT = process.argv[2] || '../temp-screenshots/listing-failure-copy'
const ENTRY = '/capture/listing-failure-copy.html'

const BROWSE_DEADLINE_MS = 10_000
const SEARCH_DEADLINE_MS = 15_000
/** Past the deadline, with room for the render that follows it. */
const SETTLE_MS = 2_000

mkdirSync(OUT, { recursive: true })

/**
 * The copy each scene must land on.
 *
 * Written out rather than read from the catalogs on purpose: reading the same JSON
 * the component reads would make the assertion tautological, passing even if the
 * panel resolved a different key than intended.
 */
const EXPECT = {
  en: {
    listing: {
      timed_out: 'Folder listing timed out',
      denied: 'No access to this folder',
      root_missing: 'Folder not found',
      failed: 'Unable to list folder',
    },
    search: {
      timed_out: 'Search timed out',
      denied: 'No access to this folder',
      root_missing: 'Folder not found',
      failed: 'Search failed',
    },
  },
  ja: {
    listing: {
      timed_out: 'フォルダーの一覧表示がタイムアウトしました',
      denied: 'このフォルダーへのアクセスが拒否されました',
      root_missing: 'フォルダーが見つかりません',
      failed: 'フォルダーを一覧表示できません',
    },
    search: {
      timed_out: '検索がタイムアウトしました',
      denied: 'このフォルダーへのアクセスが拒否されました',
      root_missing: 'フォルダーが見つかりません',
      failed: '検索に失敗しました',
    },
  },
}

const CAUSES = ['timed_out', 'denied', 'root_missing', 'failed']

/** en covers every cause on both arms; ja proves translation; light proves contrast. */
const SCENES = [
  ...CAUSES.map(cause => ({ arm: 'listing', cause, lang: 'en', theme: 'dark' })),
  ...CAUSES.map(cause => ({ arm: 'search', cause, lang: 'en', theme: 'dark' })),
  { arm: 'listing', cause: 'timed_out', lang: 'ja', theme: 'dark' },
  { arm: 'search', cause: 'timed_out', lang: 'ja', theme: 'dark' },
  { arm: 'listing', cause: 'root_missing', lang: 'ja', theme: 'dark' },
  { arm: 'listing', cause: 'root_missing', lang: 'en', theme: 'light' },
  { arm: 'listing', cause: 'denied', lang: 'en', theme: 'light' },
  { arm: 'search', cause: 'failed', lang: 'en', theme: 'light' },
]

const name = (i, s) => `${String(i + 1).padStart(2, '0')}-${s.arm}-${s.cause}-${s.lang}-${s.theme}`

async function main() {
  const server = await createServer({
    server: { host: '127.0.0.1', port: 6834, strictPort: true },
    logLevel: 'warn',
  })
  await server.listen()
  const base = `http://127.0.0.1:${server.config.server.port}`

  // A mise-managed node exports its own lib/node on LD_LIBRARY_PATH, whose
  // bundled libstdc++ predates the GLIBCXX the system mesa stack needs, so the
  // shell dies before it opens a page. Dropping the variable restores the system
  // resolution order.
  const { LD_LIBRARY_PATH: _mise, ...browserEnv } = process.env
  const browser = await chromium.launch({ env: browserEnv, executablePath: chromiumExecutable() })
  const context = await browser.newContext({
    viewport: { width: 520, height: 400 },
    deviceScaleFactor: 2,
    locale: 'en-US',
  })

  const failures = []

  for (const [i, s] of SCENES.entries()) {
    const file = name(i, s)
    const page = await context.newPage()
    page.on('pageerror', e => failures.push(`${file}: page error ${e.message}`))
    await page.goto(
      `${base}${ENTRY}?arm=${s.arm}&cause=${s.cause}&lang=${s.lang}&theme=${s.theme}`,
      { waitUntil: 'domcontentloaded' },
    )

    if (s.arm === 'search') {
      // The search arm only runs once a query is typed, and the input is labelled
      // from the same catalog under test, so it is located by role rather than by a
      // string that changes with `lang`.
      const box = page.getByRole('textbox').first()
      await box.waitFor({ timeout: 15_000 })
      await box.click()
      await box.pressSequentially('app', { delay: 20 })
      await page.waitForTimeout(s.cause === 'timed_out' ? SEARCH_DEADLINE_MS + SETTLE_MS : SETTLE_MS)
    } else {
      await page.waitForTimeout(s.cause === 'timed_out' ? BROWSE_DEADLINE_MS + SETTLE_MS : SETTLE_MS)
    }

    const want = EXPECT[s.lang][s.arm][s.cause]
    if (await page.getByText(want, { exact: false }).count() !== 1) {
      failures.push(`${file}: expected notice ${JSON.stringify(want)} was not rendered exactly once`)
    }
    if (await page.getByText('deadline exceeded', { exact: false }).count() !== 0) {
      failures.push(`${file}: the rejection's own "deadline exceeded" text is on screen`)
    }

    await page.screenshot({ path: `${OUT}/${file}.png` })
    console.log('wrote', `${OUT}/${file}.png`)
    await page.close()
  }

  await browser.close()
  await server.close()

  if (failures.length) {
    for (const f of failures) console.error('FAIL:', f)
    process.exit(1)
  }
  console.log(`PASS: ${SCENES.length} scenes, every notice translated copy, no frame carries "deadline exceeded"`)
}

main().catch(err => { console.error(err); process.exit(1) })
