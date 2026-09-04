/**
 * Screenshot harness for project-scoped skills (#3551).
 *
 * Same pattern as capture-skill-approval-surface.mjs: the REAL built SPA behind an
 * in-process static server, every /api/** answered from fixtures. Fixtures are the
 * only way to photograph these states at all -- a trusted project skill needs a
 * chat slot bound to a real project directory that ships a SKILL.md, and an
 * UNtrusted one needs the grant store to be empty for that exact directory.
 *
 * Frames:
 *   01-picker-needs-trust   the $ picker listing a project skill with its
 *                           "Needs trust" marker -- listed, not hidden, because a
 *                           silently absent skill reads as one that never existed
 *   02-trust-dialog         the consent dialog: what the skill is, what trusting
 *                           it permits, and how to withdraw
 *   03-picker-trusted       the same row after the grant, selectable like any
 *                           local skill
 *
 * Usage: node scripts/capture-project-skills.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { join } from 'node:path'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/project-skills'
mkdirSync(OUT, { recursive: true })

const PROJECT = '~/work/checkout-service'
const SLOT = 'chat-1'

/** Catalog rows, as `/api/skills` actually answers: a FLAT array of items each
 *  carrying `key`. `trusted` is present only on `kiro-workspace` rows and is the
 *  field the picker keys its marker on. */
const catalog = (trusted) => ([
  {
    key: 'prepare-pr',
    name: 'prepare-pr',
    source: 'kiro-user',
    description: 'Drive working-tree changes to a review-ready pull request.',
  },
  {
    key: 'checkout-conventions',
    name: 'checkout-conventions',
    source: 'kiro-workspace',
    description: "This project's API and migration conventions.",
    trusted,
  },
])

const trustState = (trusted) => ({
  project: PROJECT,
  project_trusted: trusted,
  grants: trusted ? [{ path: PROJECT, granted_at: 1755600000, exists: true }] : [],
})

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  const context = await browser.newContext({
    viewport: { width: 1280, height: 900 },
    // 11-13px UI type renders soft at 1x, which reads as a rendering bug in a
    // screenshot rather than as the anti-aliasing it is.
    deviceScaleFactor: 2,
  })

  let page = null

  async function open(trusted) {
    if (page) await page.close()
    page = await context.newPage()
    logPageProblems(page)
    await stubDashboardApi(page, {
      slots: [{ key: SLOT, title: 'checkout-service', messages: 2, running: false, project: PROJECT }],
      extra: async (path, route) => {
        // Must return TRUTHY to claim the route: json() resolves to undefined, so
        // returning it directly lets the stub's catch-all fulfil a second time.
        if (path === '/api/skills') {
          await json(route, catalog(trusted))
          return true
        }
        if (path === '/api/skills/-/trust') {
          await json(route, trustState(trusted))
          return true
        }
        return false
      },
    })
    await page.goto(`${base}/chat?sid=${SLOT}`, { waitUntil: 'domcontentloaded' })
    // Wait on a REAL locator -- a blank page must fail loudly rather than
    // silently produce an empty screenshot.
    const composer = page.locator('textarea').first()
    await composer.waitFor({ state: 'visible', timeout: 20000 })
    await page.waitForTimeout(700)
    return composer
  }

  // 01 + 02: untrusted.
  let composer = await open(false)
  await composer.click()
  await composer.type('$checkout')
  await page.waitForTimeout(600)
  await page.screenshot({ path: join(OUT, '01-picker-needs-trust.png') })
  console.log('wrote 01-picker-needs-trust.png')

  // Choosing an untrusted row asks for consent instead of inserting the token.
  await page.keyboard.press('Enter')
  await page.getByRole('dialog').waitFor({ state: 'visible', timeout: 10000 })
  await page.waitForTimeout(400)
  await page.screenshot({ path: join(OUT, '02-trust-dialog.png') })
  console.log('wrote 02-trust-dialog.png')

  // 03: the same row once the directory carries a grant.
  composer = await open(true)
  await composer.click()
  await composer.type('$checkout')
  await page.waitForTimeout(600)
  await page.screenshot({ path: join(OUT, '03-picker-trusted.png') })
  console.log('wrote 03-picker-trusted.png')

  await browser.close()
  srv.close()
  console.log('done ->', OUT)
}

main().catch((err) => {
  console.error(err)
  process.exit(1)
})
