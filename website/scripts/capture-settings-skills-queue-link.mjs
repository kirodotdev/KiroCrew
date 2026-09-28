/**
 * Screenshot harness for #12543: Settings → Skills now leads to the pending
 * auto-skill queue instead of only mentioning it.
 *
 * Same pattern as capture-skill-approval-surface.mjs: the REAL built SPA behind
 * an in-process static server, every /api/** answered from fixtures.
 *
 * Frames:
 *   01-settings-skills-link  Settings → Skills: the two policy toggles plus the
 *                            new link to Agent Capabilities → Skills
 *   02-review-forward        /settings?tab=skills&review=<slug> forwarded to
 *                            /capabilities?tab=skills&review=<slug>, with that
 *                            candidate open in the queue
 *
 * Usage: node scripts/capture-settings-skills-queue-link.mjs [outDir] [prefix]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/settings-skills-queue-link'
const PREFIX = process.argv[3] || 'after'

mkdirSync(OUT, { recursive: true })

const SLUG = 'summarize-oncall-handoffs'

const PENDING = [
  {
    slug: SLUG,
    name: `auto/${SLUG}`,
    description: 'Digest the week\'s pages into a handoff brief',
    has_scripts: false,
    kind: 'new',
    target: null,
    base_version: null,
  },
  {
    slug: 'rotate-staging-fixtures',
    name: 'auto/rotate-staging-fixtures',
    description: 'Regenerate staging fixtures from the latest schema',
    has_scripts: true,
    kind: 'new',
    target: null,
    base_version: null,
  },
]

const extra = async (path, route) => {
  if (path === '/api/skills/-/pending') {
    await json(route, { pending: PENDING })
    return true
  }
  if (path.startsWith('/api/skills/-/pending/')) {
    await json(route, {
      name: `auto/${SLUG}`,
      content: '---\nname: summarize-oncall-handoffs\n---\n\n## Steps\n\n1. Collect the week\'s pages.\n',
      scripts: [],
    })
    return true
  }
  if (path === '/api/skills') {
    await json(route, [])
    return true
  }
  return false
}

const shot = (page, name) =>
  page.screenshot({ path: `${OUT}/${PREFIX}-${name}.png`, animations: 'disabled' })

const { srv, base } = await serveDist()
const browser = await chromium.launch()

try {
  // ── Frame 01: the link on Settings → Skills ──
  {
    const page = await browser.newPage({ viewport: { width: 1280, height: 860 } })
    logPageProblems(page)
    await stubDashboardApi(page, { extra })
    await page.goto(`${base}/settings?tab=skills`, { waitUntil: 'networkidle' })
    await page.getByRole('link', { name: /Review pending candidates/ }).waitFor()
    await shot(page, '01-settings-skills-link')
    await page.close()
  }

  // ── Frame 02: the forwarded deep link, candidate open ──
  {
    const page = await browser.newPage({ viewport: { width: 1280, height: 860 } })
    logPageProblems(page)
    await stubDashboardApi(page, { extra })
    await page.goto(`${base}/settings?tab=skills&review=${SLUG}`, { waitUntil: 'networkidle' })
    await page.getByText('Collect the week').first().waitFor()
    const url = new URL(page.url())
    if (`${url.pathname}${url.search}` !== `/capabilities?tab=skills`) {
      // The panel strips `review=` once it latches, so the settled URL is the
      // capabilities tab without it. Anything else means the forward broke.
      console.error(`unexpected settled URL: ${url.pathname}${url.search}`)
    }
    await shot(page, '02-review-forward')
    await page.close()
  }
  console.log(`wrote frames to ${OUT} (prefix ${PREFIX})`)
} finally {
  await browser.close()
  srv.close()
}
