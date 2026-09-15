/**
 * Screenshot harness for the Trash empty progress states.
 *
 * The reported symptom: emptying a batch of tens of thousands of sessions greyed
 * out three buttons and said nothing else, so a run that takes minutes looked
 * identical to a stuck screen — the user only learned it had worked by coming back
 * later and finding the batch gone.
 *
 * Three frames: mid-run (partial figure, bar, and the line that says leaving is
 * safe), finished (what it freed), and kept (a batch deliberately held back, which
 * raises nothing and used to render as "Freed 0 B."). Captured
 * at 1440x1180 so the whole screen fits without scrolling — the status sits under
 * the Trash section at the foot of the page.
 *
 * Runs the REAL built SPA (website/dist) with every /api/** call answered from
 * fixtures — gateway-free. Same technique as capture-armed-delete-touch.mjs.
 *
 * Labels are read from the CATALOG, so a key rename breaks the capture loudly
 * instead of silently screenshotting the wrong element.
 *
 * Usage: node scripts/capture-trash-empty-progress.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync, readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/trash-empty-progress'

mkdirSync(OUT, { recursive: true })

const LOCALES = fileURLToPath(new URL('../src/i18n/locales/', import.meta.url))
const en = JSON.parse(readFileSync(LOCALES + 'en.json', 'utf-8'))
const ss = en.pages.sessionStorage
const EMPTYING = ss.emptying
const LEAVE_OK = ss.emptying_leave_ok
if (!EMPTYING || !LEAVE_OK || !ss.emptied) throw new Error('sessionStorage empty-progress keys missing — renamed?')

const now = Math.floor(Date.now() / 1000)

const SESSIONS = Array.from({ length: 8 }, (_, i) => ({
  uid: `dashboard_chat-${100 - i}`,
  title: [
    'MCP Daemon Decoupling Discussion',
    'System View Redesign Task Manager Style',
    'OTEL Metrics Context Not Visible',
    'Review Sage Interactive Chat Window',
    'MCP Server for Session and Folder Control',
    'Code review sage cancel flights',
    'Prevent Image Dimension Exceeded Error',
    'S3 Backup for Cloud Desktop',
  ][i],
  origin: `dashboard · chat-${100 - i}`,
  bytes: Math.round(115_300_000 / (1 + i * 0.4)),
  mtime: now - 3600 * (i + 1) * 7,
  active: i < 3,
  live: false,
  background: false,
}))

/** The reported store: one policy batch holding 55,323 sessions and 18GB. */
const inventory = {
  total_bytes: 19_700_000_000,
  total_sessions: 34_905,
  reclaimable_bytes: 16_200_000_000,
  reclaim_blocked_reason: '',
  sessions: SESSIONS,
  background: { sessions: 0, bytes: 0, listed: 0 },
  age_options: [
    { days: 7, sessions: 12, bytes: 240_000_000 },
    { days: 30, sessions: 1, bytes: 4_000 },
    { days: 90, sessions: 0, bytes: 0 },
  ],
  trash: {
    bytes: 18_000_000_000,
    still_on_disk: true,
    instant: true,
    batches: [
      // THREE batches, not one, and that is a correctness point rather than dressing:
      // progress is reported per batch, so a partial figure is only reachable when more
      // than one batch is staged. A single 18GB batch would sit at 0% until it finished,
      // and a frame showing it mid-way would depict a state this code cannot produce.
      {
        batch_id: '20260823T041500-ab12cd34',
        created_at: now - 180,
        reason: 'policy',
        sessions: 21_207,
        bytes: 6_900_000_000,
      },
      {
        batch_id: '20260823T052000-cd34ef56',
        created_at: now - 120,
        reason: 'policy',
        sessions: 21_822,
        bytes: 7_100_000_000,
      },
      {
        batch_id: '20260823T061000-ef56ab12',
        created_at: now - 60,
        // 'policy' for all three deliberately: a `manual` batch renders "Moved by
        // manual", which is a pre-existing copy defect in this screen's reason label
        // and not something this PR touches. The frame should demonstrate the progress
        // feature, not showcase an unrelated string bug.
        reason: 'policy',
        sessions: 12_294,
        bytes: 4_000_000_000,
      },
    ],
  },
}

/** Trash already gone, as it is once the delete lands. */
const emptied = {
  ...inventory,
  trash: { ...inventory.trash, bytes: 0, batches: [] },
}

const RUNNING = {
  // 6.9GB is exactly the first batch: per-batch reporting can only ever show a figure
  // that is the sum of FINISHED batches, so this is the state after one of three.
  job_id: 'empty-1', running: true,
  total_bytes: 18_000_000_000, freed_bytes: 6_900_000_000, error: '', skipped: [],
}
const DONE = { ...RUNNING, running: false, freed_bytes: 18_000_000_000 }
/**
 * The realistic refusal: nothing raises, so this used to render "Freed 0 B."
 *
 * All three batches refused, which is what makes zero freed reachable now that the
 * store holds more than one - and the three identical codes dedupe to one sentence.
 */
const KEPT = {
  ...RUNNING,
  running: false,
  freed_bytes: 0,
  skipped: ['unlisted_files', 'unlisted_files', 'unlisted_files'],
}

async function frame(base, browser, name, { job, list }) {
  const context = await browser.newContext({ viewport: { width: 1440, height: 1180 } })
  const page = await context.newPage()
  logPageProblems(page)
  await stubDashboardApi(page, {
    extra: async (path, route) => {
      if (path === '/api/system/session-storage/sessions') { await json(route, list); return true }
      if (path === '/api/system/session-storage/empty') { await json(route, { job }); return true }
      if (path === '/api/system') {
        await json(route, {
          cpu_pct: 12, mem_total_gb: 128, mem_used_gb: 60,
          disk_total_gb: 880, disk_free_gb: 168, net_rx_kbs: 46, net_tx_kbs: 0,
        })
        return true
      }
      return false
    },
  })
  await page.goto(base + '/developer?tab=system&plane=performance&view=storage', {
    waitUntil: 'domcontentloaded',
  })
  const status = page.getByRole('status').first()
  await status.waitFor({ timeout: 15000 })
  // The status sits under the Trash section at the foot of the screen; with a real
  // inventory above it that is below the fold.
  // `scrollIntoViewIfNeeded` is a no-op here: the row is inside the page's own
  // scroll container and Playwright already considers it in view. Scroll the
  // container itself.
  await page.evaluate(() => {
    // The LAST one: since the running banner was added at the top of the list this
    // page has two `role="status"` nodes, and `querySelector` started returning the
    // banner - which scrolled the frame away from the progress row it exists to show.
    const rows = document.querySelectorAll('[role="status"]')
    rows[rows.length - 1]?.scrollIntoView({ block: 'center' })
  })
  await page.waitForTimeout(600)
  await page.screenshot({ path: `${OUT}/${name}.png` })
  await context.close()
}

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()

  await frame(base, browser, 'running', { job: RUNNING, list: inventory })
  await frame(base, browser, 'finished', { job: DONE, list: emptied })
  await frame(base, browser, 'kept', { job: KEPT, list: inventory })

  await browser.close()
  srv.close()
  console.log(`wrote ${OUT}/running.png, ${OUT}/finished.png and ${OUT}/kept.png`)
}

main().catch(err => { console.error(err); process.exit(1) })
