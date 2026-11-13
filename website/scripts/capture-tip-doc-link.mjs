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
  'delegate-independent-work': {
    id: 'delegate-independent-work',
    feature: 'Subagents',
    title: 'Delegate independent work',
    body: 'Independent tasks? Type **Run these tasks in parallel.** in chat.',
    why: '',
    doc: '',
    doc_link: 'subagents.md',
    cta_prompt: '',
  },
  'zero-token-cron': {
    id: 'zero-token-cron',
    feature: 'Zero-token Crons',
    title: 'Run the same check without the model',
    body: 'Same check every time? Type **Schedule this as a script job** in chat. It runs without using the model.',
    why: '',
    doc: '',
    doc_link: 'cron-and-scheduling.md',
    cta_prompt: '',
  },
  // A route-action tip, copied verbatim from the shipped app-store entry.
  // It has no doc_link, so the harness asserts the action button renders
  // and that no Learn more link appears.
  'app-store': {
    id: 'app-store',
    feature: 'Apps',
    title: 'Add new capabilities with Apps',
    body: 'Need another capability? Open **Apps › Discover** to **Enable** a shipped app or **Install** an external app. After installing, enable it in **Library** and complete any trust review.',
    why: '',
    doc: '',
    cta_prompt: '',
    action: { kind: 'route', label: 'Open Apps', route: '/apps' },
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
    // A tip first shows as a one-line pill; its title button opens the card
    // that carries the Learn more link.
    await card.locator('button[aria-expanded="false"]').first().click().catch(() => {})
    await page.getByTestId('tip-body').first().waitFor({ timeout: 8000 }).catch(() => {})
    const cardBox = await page.getByTestId('tip-card').first().boundingBox().catch(() => null)
    if (!cardBox) {
      results.push({ name, ok: false, why: 'tip card never rendered' })
      await page.screenshot({ path: `${OUT}/${name}-MISSING.png` })
      return
    }
    // Each fixture states what source ships: a Learn more link exactly when
    // doc_link is set, and an action button exactly when action is set.
    const links = card.getByRole('link', { name: /learn more/i })
    const linkCount = await links.count()
    const href = linkCount ? await links.first().getAttribute('href').catch(() => null) : null
    const linkOk = tip.doc_link
      ? !!href && href.endsWith('/' + tip.doc_link)
      : linkCount === 0
    // Every shipped route label starts with "Open"; no card chrome control or
    // fixture title does, so this count is zero exactly when no action renders.
    const actionCount = await card.getByRole('button', { name: tip.action ? tip.action.label : /^Open\b/ }).count()
    const actionOk = tip.action ? actionCount > 0 : actionCount === 0
    const ok = linkOk && actionOk
    const top = Math.max(0, cardBox.y - 16)
    await page.screenshot({
      path: `${OUT}/${name}.png`,
      clip: { x: 500, y: top, width: 980, height: cardBox.height + 110 },
    })
    console.log('wrote', `${OUT}/${name}.png`)
    results.push({ name, ok, href, linkCount, actionCount })
  }

  await scenario('01-delegate-independent-work-link-restored-dark', 'dark', TIPS['delegate-independent-work'])
  await scenario('02-zero-token-cron-link-restored-light', 'light', TIPS['zero-token-cron'])
  await scenario('03-app-store-route-action-light', 'light', TIPS['app-store'])

  await browser.close()
  srv.close()

  console.log('--- assertions (Learn more and action button match each fixture) ---')
  for (const r of results) console.log(JSON.stringify(r))
  if (!results.every(r => r.ok)) {
    console.error('FAIL: a tip Learn more link or action button did not match its fixture')
    process.exit(1)
  }
  console.log('OK')
}

main().catch(err => { console.error(err); process.exit(1) })
