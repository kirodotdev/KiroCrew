/**
 * Screenshot harness for per-source indexing spend on the Knowledge sources tab.
 *
 * Runs the REAL built SPA (website/dist) behind the shared loopback server with
 * /api/** answered by fixtures, so the sources list is exercised unmodified — only
 * the network is stubbed. The `spend` block in the `/api/knowledge/sources`
 * fixture is the exact shape the handler now returns.
 *
 * It doubles as a regression check rather than just a camera: the run exits
 * non-zero unless the standing cost notice, the per-source progress figure and the
 * remaining-Kiro-requests figure are all present and rounded, and unless the
 * finished source renders progress WITHOUT a remaining figure.
 *
 * Usage: node scripts/capture-knowledge-spend.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/knowledge-spend'
mkdirSync(OUT, { recursive: true })

const spend = over => ({
  files_total: 0,
  files_done: 0,
  files_failed: 0,
  files_skipped: 0,
  files_pending: 0,
  chunks_embedded: 0,
  estimated_llm_calls_remaining: 0,
  ...over,
})

const sources = [
  {
    id: 'src-repo',
    name: 'Work notes',
    source_type: 'local_folder',
    uri: '/home/user/notes',
    sync_status: 'active',
    item_count: 412,
    last_synced: new Date(Date.now() - 4 * 60_000).toISOString(),
    properties: '{}',
    // Mid-scan: a paced folder that is still drawing billed requests at idle, with
    // a handful of unreadable files so the failure signal is exercised too.
    spend: spend({
      files_total: 1240,
      files_done: 300,
      files_skipped: 18,
      files_pending: 920,
      files_failed: 2,
      chunks_embedded: 2871,
      estimated_llm_calls_remaining: 11460,
    }),
  },
  {
    id: 'src-done',
    name: 'Design docs',
    source_type: 'local_folder',
    uri: '/home/user/design',
    sync_status: 'active',
    item_count: 96,
    last_synced: new Date(Date.now() - 3 * 3600_000).toISOString(),
    properties: '{}',
    // Finished: progress shows, no remaining figure.
    spend: spend({ files_total: 84, files_done: 84, chunks_embedded: 512 }),
  },
  {
    id: 'src-file',
    name: 'onboarding.md',
    source_type: 'local_file',
    uri: '/home/user/onboarding.md',
    sync_status: 'synced',
    item_count: 6,
    last_synced: new Date(Date.now() - 26 * 3600_000).toISOString(),
    // No per-file state: nothing queued, so nothing extra is rendered.
    spend: spend({ chunks_embedded: 6 }),
  },
]

const { srv, base } = await serveDist()

const browser = await chromium.launch()
const failures = []

try {
  for (const [label, width, height] of [['wide', 1440, 900], ['narrow', 820, 900]]) {
    const page = await browser.newPage({ viewport: { width, height } })
    logPageProblems(page)
    await stubDashboardApi(page, {
      // The shared stub treats a truthy return as "handled"; `json` resolves to
      // undefined, so each branch has to await it and say so explicitly or the
      // request falls through and gets fulfilled twice.
      extra: async (path, route) => {
        if (path === '/api/knowledge/sources') {
          await json(route, sources)
          return true
        }
        if (path === '/api/knowledge/config') {
          await json(route, { enabled: true, supported_formats: ['md', 'txt'], folder_picker: false })
          return true
        }
        if (path === '/api/knowledge/namespaces') {
          await json(route, [])
          return true
        }
        if (path === '/api/knowledge/stats') {
          await json(route, {
            items: 514,
            entities: 1902,
            relations: 3311,
            sources: sources.length,
            embeddings: { enabled: true, available: true, model: 'bge-small', embedded_items: 3389 },
          })
          return true
        }
        return false
      },
    })

    await page.goto(`${base}/knowledge`, { waitUntil: 'networkidle' })
    // Let the page's entry animation settle before driving it: clicking straight
    // out of networkidle lands while the list container is still mid-transition
    // and the row never resolves visible.
    await page.waitForTimeout(1000)
    // Sources is where sources are managed; the list view is the default tab.
    const tab = page.getByRole('button', { name: /^sources$/i }).first()
    if (!(await tab.count())) throw new Error('Sources tab not found')
    await tab.click()
    await page.getByText('Work notes').first().waitFor({ state: 'attached', timeout: 15_000 })
    await page.waitForTimeout(800)

    const body = await page.locator('body').innerText()
    const expect = (cond, msg) => { if (!cond) failures.push(`[${label}] ${msg}`) }
    expect(/Indexing uses Kiro requests/.test(body), 'standing cost notice missing')
    // The gradual-charge caveat must be visible text, not a title attribute.
    expect(/spread out over time/.test(body), 'gradual-charge caveat not in visible copy')
    // 300 done + 18 skipped resolved; the 2 failures are the remaining gap.
    expect(/318\/1,?240 files indexed/.test(body), 'in-progress figure missing')
    expect(/2 failed/.test(body), 'failure count missing')
    // Two significant figures, not the raw 11,460 -- the estimate must not render a
    // precision the leading ~ disclaims.
    expect(/~11K Kiro requests left/.test(body), 'rounded remaining figure missing')
    expect(!/11,460/.test(body), 'raw unrounded estimate still rendered')
    expect(/84\/84 files indexed/.test(body), 'finished source progress missing')
    // Exactly one source is still outstanding, so exactly one remaining figure.
    expect((body.match(/Kiro requests left/g) || []).length === 1,
      'remaining figure shown for a source with nothing outstanding')

    // The figures widen an already-crowded meta row, so assert geometry rather than
    // trusting the eye: nothing may render past its own card's border, and the
    // source name must keep a readable width instead of being squeezed to nothing.
    const overflow = await page.evaluate(() => {
      const bad = []
      for (const card of document.querySelectorAll('.border.border-border.rounded-lg')) {
        const cb = card.getBoundingClientRect()
        if (cb.height === 0) continue
        for (const child of card.querySelectorAll('*')) {
          const b = child.getBoundingClientRect()
          if (b.width === 0 || b.height === 0) continue
          if (b.right > cb.right + 1 || b.left < cb.left - 1) {
            bad.push(`${(child.textContent || child.tagName).trim().slice(0, 24)} escapes its card`)
            break
          }
        }
      }
      const names = [...document.querySelectorAll('span.text-sm.font-medium')]
        .map(n => ({ text: (n.textContent || '').trim(), w: n.getBoundingClientRect().width }))
        .filter(n => n.text)
      return { bad, names }
    })
    for (const b of overflow.bad) expect(false, b)
    expect(overflow.names.length >= 3, `expected 3 source names, saw ${overflow.names.length}`)
    for (const n of overflow.names) {
      expect(n.w >= 40, `source name "${n.text}" squeezed to ${Math.round(n.w)}px`)
    }

    await page.screenshot({ path: `${OUT}/sources-spend-${label}.png`, fullPage: false })
    await page.close()
  }
} finally {
  await browser.close()
  srv.close()
}

if (failures.length) {
  for (const f of failures) console.error('FAIL', f)
  process.exit(1)
}
console.log(`captured to ${OUT}`)
