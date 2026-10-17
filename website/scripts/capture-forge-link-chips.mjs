/**
 * Screenshot harness for synchronous GitHub / GitLab link chips (#2579).
 *
 * Runs the REAL built SPA (website/dist) behind the shared static server and
 * answers every /api/** call from fixtures — no gateway or kiro-cli needed.
 * The transcript places forge URLs in BOTH a user message and an assistant
 * message, with `link_previews` left OFF (the default), because that is the
 * exact combination the network unfurl path never covered: before this fix
 * every one of these links rendered as raw URL text.
 *
 * ASSERTS before shooting: all four chip shapes present (GitHub PR + issue,
 * GitLab MR + issue), the user-message chip rendered, and the lookalike host
 * did NOT chip — a blank or half-rendered page fails the run instead of
 * producing a lying screenshot.
 *   01 dark  → chat transcript with forge chips in user + assistant messages
 *   02 light → same surface, light theme
 *
 * Usage: node scripts/capture-forge-link-chips.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/forge-link-chips'
const SLOT = 'chat-forge-chips'
const PROJECT = '/home/user/workspace/KiroCrew'

mkdirSync(OUT, { recursive: true })

const now = () => Date.now() / 1000

const slots = [{
  key: SLOT,
  title: 'Fix the forge link rendering',
  running: false,
  last_message: 'Opened the PR',
  messages: 2,
  agent: 'kirocrew',
  memory_mode: 'persistent',
  project: PROJECT,
  modified: Math.floor(Date.now() / 1000),
  source_links: [],
  source_links_total: 0,
}]

const detail = {
  running: false,
  has_more: false,
  total: 2,
  queue: [],
  project: PROJECT,
  messages: [
    {
      role: 'user',
      ts: now() - 900,
      content: 'Can you look at https://github.com/kirodotdev/KiroCrew/issues/2579 and the related '
        + 'https://gitlab.com/acme/widgets/-/issues/9 report?',
    },
    {
      role: 'assistant',
      ts: now() - 60,
      content: 'Opened https://github.com/kirodotdev/KiroCrew/pull/2600 for it, mirroring the approach from '
        + 'https://gitlab.com/acme/widgets/-/merge_requests/7. Note that '
        + 'https://evil-github.com.attacker.test/kirodotdev/KiroCrew/pull/1 stays a plain link — lookalike hosts never chip.',
    },
  ],
}

async function capture(browser, base, theme, name) {
  const context = await browser.newContext({
    viewport: { width: 1500, height: 950 },
    deviceScaleFactor: 2,
  })
  const page = await context.newPage()
  await page.routeWebSocket(/\/api\/ws/, () => {})
  await stubDashboardApi(page, {
    slots,
    theme,
    extra: async (path, route) => {
      if (path.startsWith('/api/chat/slots/')) { json(route, detail); return true }
      if (path === '/api/recent-projects') { json(route, { dirs: [PROJECT] }); return true }
      if (path === '/api/chat/nav/resolve-links') { json(route, { summaries: [] }); return true }
      return false
    },
  })
  logPageProblems(page)

  await page.addInitScript((t) => {
    localStorage.clear()
    localStorage.setItem('mc-theme', t)
    localStorage.setItem('mc-onboarded', '1')
    localStorage.setItem('mc-active-slot', 'chat-forge-chips')
  }, theme)
  await page.goto(base + '/', { waitUntil: 'domcontentloaded' })

  // All four chip shapes, by their forge-convention labels.
  for (const label of ['kirodotdev/KiroCrew#2579', 'acme/widgets#9', 'kirodotdev/KiroCrew#2600', 'acme/widgets!7']) {
    await page.waitForSelector(`text=${label}`, { timeout: 15000 })
  }
  // Provider marks: 2 GitHub + 2 GitLab chips; the lookalike host must have none.
  const github = await page.locator('[data-provider-mark="github"]').count()
  const gitlab = await page.locator('[data-provider-mark="gitlab"]').count()
  if (github !== 2 || gitlab !== 2) throw new Error(`expected 2+2 provider marks, got github=${github} gitlab=${gitlab}`)
  const lookalike = page.locator('a[href*="attacker.test"]')
  if (await lookalike.count() !== 1) throw new Error('lookalike anchor missing')
  if (await lookalike.locator('[data-provider-mark]').count() !== 0) throw new Error('lookalike host was chipped')

  await page.waitForTimeout(400)
  await page.screenshot({ path: `${OUT}/${name}.png` })
  console.log('wrote', `${OUT}/${name}.png`)
  await context.close()
}

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  await capture(browser, base, 'dark', '01-forge-chips-dark')
  await capture(browser, base, 'light', '02-forge-chips-light')
  await browser.close()
  srv.close()
}

main().catch(e => { console.error(e); process.exit(1) })
