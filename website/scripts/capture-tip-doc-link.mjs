/**
 * Screenshot harness for the TIP LEARN-MORE LINK driven by `doc_link`.
 *
 * The doc -> doc_link split (issue #3524) restores the "Learn more" link on
 * curated tips whose `doc` was cleared to stop the dismissal-identity
 * collision. This photographs the restored state in the REAL built SPA
 * (website/dist): a curated tip with doc="" and a non-empty doc_link renders
 * the link, and the anchor resolves to the doc_link target.
 *
 * It asserts as well as photographs: exits non-zero when the tip card or the
 * link is missing, or when the link href does not end in the doc_link value.
 * Nothing in CI runs this file — the CI-enforced half of the behavior lives
 * in src/test/TipCard.test.tsx.
 *
 * Usage: node scripts/capture-tip-doc-link.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/tip-doc-link'
const SLOT = 'chat-band'

mkdirSync(OUT, { recursive: true })

const slots = [{
  key: SLOT,
  title: 'Parallel research over the release notes',
  running: true, // the tip trigger only arms on a running slot
  last_message: 'Fanning the file survey out now.',
  messages: 2,
  agent: 'kirocrew',
  memory_mode: 'persistent',
  project: '/home/user/workspace/notes',
  folder_id: '',
  modified: Math.floor(Date.now() / 1000),
  source_links: [],
  source_links_total: 0,
}]

const detail = {
  running: true,
  has_more: false,
  total: 2,
  queue: [],
  project: '/home/user/workspace/notes',
  messages: [
    { role: 'user', ts: Date.now() / 1000 - 600, content: 'Survey the release notes for breaking changes.' },
    { role: 'assistant', ts: Date.now() / 1000 - 30, content: 'Fanning the file survey out now.' },
  ],
}

// The restored curated tips: doc="" (dismissal identity stays empty, so
// dismissing them cannot suppress the catalog tip for the same doc) while
// doc_link carries the learn-more target — exactly the shape shipped in
// src/kiro_crew/data/tips_curated.json.
const TIPS = {
  'subagent-parallelism': {
    id: 'subagent-parallelism',
    feature: 'Subagent Parallelism',
    title: 'Tune how many subagents run at once',
    body: '`agent.max_subagents` caps concurrent subagents. **0 = auto-size** to your machine\'s memory and CPU; pin a number with `kirocrew config set agent.max_subagents 4`.',
    why: '',
    doc: '',
    doc_link: 'dynamic-subagent-sizing.md',
    cta_prompt: '',
    action: { kind: 'route', label: 'Open Subagent config', route: '/settings?tab=overview' },
  },
  'zero-token-cron': {
    id: 'zero-token-cron',
    feature: 'Zero-token Crons',
    title: 'Run crons with no LLM',
    body: 'A scheduled job can run a shell `command` or a Python `script` directly — no model, zero tokens. Ideal for polling, cleanup, and health checks.',
    why: '',
    doc: '',
    doc_link: 'cron-and-scheduling.md',
    cta_prompt: '',
    action: { kind: 'route', label: 'New scheduled job', route: '/schedule' },
  },
}

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  const context = await browser.newContext({
    viewport: { width: 1500, height: 950 },
    deviceScaleFactor: 2, // the strip is 11–12px type; 1x renders soft on GitHub
  })

  let page = null

  async function load(theme, tip) {
    if (page) await page.close()
    page = await context.newPage()
    logPageProblems(page)
    const extra = async (path, route) => {
      if (path === '/api/tips/status') {
        // Tiny cadence → the client's 20-minute floor collapses.
        await json(route, { enabled: true, cadence_hours: 0.0001 })
        return true
      }
      if (path === '/api/tips/next') { await json(route, { tip, glow: false }); return true }
      if (path === '/api/tips/feedback') { await json(route, { ok: true }); return true }
      if (path.startsWith('/api/chat/slots/')) { await json(route, detail); return true }
      return false
    }
    await stubDashboardApi(page, { folders: [], slots, theme, extra })
    await page.addInitScript(slot => {
      localStorage.setItem('mc-active-slot', slot)
      localStorage.removeItem('kirocrew.tips.lastShownAt')
    }, SLOT)
    await page.goto(base + '/', { waitUntil: 'domcontentloaded' })
    await page.waitForTimeout(2500)
  }

  const results = []

  async function scenario(name, theme, tip) {
    await load(theme, tip)
    // The tip arms on a 10s timer inside useTipTrigger; wait it out.
    await page.waitForTimeout(11500)
    const card = page.getByTestId('tip-card').first()
    await card.waitFor({ timeout: 8000 }).catch(() => {})
    const cardBox = await card.boundingBox().catch(() => null)
    if (!cardBox) {
      results.push({ name, ok: false, why: 'tip card never rendered' })
      await page.screenshot({ path: `${OUT}/${name}-MISSING.png` })
      return
    }
    const link = card.getByRole('link', { name: /learn more/i }).first()
    const href = await link.getAttribute('href').catch(() => null)
    const ok = !!href && href.endsWith('/' + tip.doc_link)
    const top = Math.max(0, cardBox.y - 16)
    await page.screenshot({
      path: `${OUT}/${name}.png`,
      clip: { x: 250, y: top, width: 1240, height: cardBox.height + 110 },
    })
    console.log('wrote', `${OUT}/${name}.png`)
    results.push({ name, ok, href })
  }

  await scenario('01-subagent-parallelism-link-restored-dark', 'dark', TIPS['subagent-parallelism'])
  await scenario('02-zero-token-cron-link-restored-light', 'light', TIPS['zero-token-cron'])

  await browser.close()
  srv.close()

  console.log('--- assertions (Learn more must render from doc_link when doc is empty) ---')
  for (const r of results) console.log(JSON.stringify(r))
  if (!results.every(r => r.ok)) {
    console.error('FAIL: a restored tip did not render its Learn more link from doc_link')
    process.exit(1)
  }
  console.log('OK')
}

main().catch(err => { console.error(err); process.exit(1) })
