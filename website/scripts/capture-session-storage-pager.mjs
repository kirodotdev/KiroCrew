/**
 * Screenshot harness for the Session storage pager.
 *
 * The reported symptom: on a long-lived install the inventory rendered every row
 * on one page, so the Trash section — the only place a staged delete is undone or
 * confirmed — sat below the entire store. These frames show the paged list with
 * the Trash reachable, and the second page after one click of Next.
 *
 * Runs the REAL built SPA (website/dist) with every /api/** call answered from
 * fixtures — gateway-free. Same technique as capture-armed-delete-touch.mjs.
 *
 * Labels are read from the CATALOG, so a key rename breaks the capture loudly
 * instead of silently screenshotting the wrong element.
 *
 * Usage: node scripts/capture-session-storage-pager.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync, readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/session-storage-pager'

mkdirSync(OUT, { recursive: true })

const LOCALES = fileURLToPath(new URL('../src/i18n/locales/', import.meta.url))
const en = JSON.parse(readFileSync(LOCALES + 'en.json', 'utf-8'))
const ss = en.pages.sessionStorage
const NEXT = ss.next_page
const HEADING = ss.heading
if (!NEXT || !HEADING || !ss.page_of) throw new Error('sessionStorage pager keys missing — renamed?')

const now = Math.floor(Date.now() / 1000)

/** Plausible conversation titles, cycled so the rows read like a real store. */
const TITLES = [
  'MCP Daemon Decoupling Discussion',
  'System View Redesign Task Manager Style',
  'OTEL Metrics Context Not Visible',
  'Review Sage Interactive Chat Window',
  'MCP Server for Session and Folder Control',
  'Code review sage cancel flights',
  'Prevent Image Dimension Exceeded Error',
  'S3 Backup for Cloud Desktop',
  'Search Everywhere Raycast Plugin Design',
  'Telegram group chat implementation',
]

const FOREGROUND = Array.from({ length: 46 }, (_, i) => ({
  uid: `dashboard_chat-${100 - i}`,
  title: `${TITLES[i % TITLES.length]}${i >= TITLES.length ? ` (${Math.floor(i / TITLES.length) + 1})` : ''}`,
  origin: `dashboard · chat-${100 - i}`,
  bytes: Math.round(115_300_000 / (1 + i * 0.35)),
  mtime: now - 3600 * (i + 1) * 7,
  active: i < 4,
  live: false,
  background: false,
}))

const BACKGROUND = Array.from({ length: 200 }, (_, i) => ({
  uid: `subagent_${i}`,
  title: '',
  origin: `subagent · run-${1000 + i}`,
  bytes: Math.round(4_100_000 / (1 + i * 0.05)),
  mtime: now - 3600 * (i + 2),
  active: false,
  live: false,
  background: true,
}))

const INVENTORY = {
  total_bytes: 19_700_000_000,
  total_sessions: 34_905,
  reclaimable_bytes: 16_200_000_000,
  reclaim_blocked_reason: '',
  sessions: [...FOREGROUND, ...BACKGROUND],
  background: { sessions: 34_447, bytes: 16_600_000_000, listed: BACKGROUND.length },
  age_options: [
    { days: 7, sessions: 12, bytes: 240_000_000 },
    { days: 30, sessions: 1, bytes: 4_000 },
    { days: 90, sessions: 0, bytes: 0 },
  ],
  trash: {
    bytes: 2_400,
    still_on_disk: true,
    instant: true,
    batches: [{
      batch_id: '20260812T041500-ab12cd34',
      created_at: now - 86400 * 11,
      reason: 'manual',
      sessions: 2,
      bytes: 2_400,
    }],
  },
}

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  const context = await browser.newContext({
    viewport: { width: 1440, height: 900 },
    deviceScaleFactor: 1,
  })
  const page = await context.newPage()
  logPageProblems(page)

  await stubDashboardApi(page, {
    extra: async (path, route) => {
      if (path === '/api/system/session-storage/sessions') { await json(route, INVENTORY); return true }
      if (path.startsWith('/api/system/session-storage/sessions/')) {
        await json(route, {
          uid: 'dashboard_chat-100', first_message: 'Why does the MCP broker restart on every toggle?',
          turns: 248, images: 58, bytes: 115_300_000, mtime: now - 3600,
        })
        return true
      }
      if (path === '/api/system') {
        await json(route, {
          cpu_pct: 12, mem_total_gb: 128, mem_used_gb: 60, disk_total_gb: 880,
          disk_free_gb: 168, net_rx_kbs: 46, net_tx_kbs: 0,
        })
        return true
      }
      return false
    },
  })

  await page.goto(base + '/developer?tab=system&plane=performance&view=storage', {
    waitUntil: 'domcontentloaded',
  })
  await page.getByText(HEADING, { exact: true }).waitFor({ timeout: 15000 })
  await page.getByText(/^Page 1 of /).waitFor({ timeout: 15000 })
  await page.waitForTimeout(400)

  await page.screenshot({ path: `${OUT}/page-1.png`, fullPage: true })

  await page.getByRole('button', { name: NEXT, exact: true }).first().click()
  await page.getByText(/^Page 2 of /).waitFor({ timeout: 5000 })
  await page.waitForTimeout(200)
  await page.screenshot({ path: `${OUT}/page-2.png`, fullPage: true })

  await browser.close()
  srv.close()
  console.log(`wrote ${OUT}/page-1.png and ${OUT}/page-2.png`)
}

main().catch(err => { console.error(err); process.exit(1) })
