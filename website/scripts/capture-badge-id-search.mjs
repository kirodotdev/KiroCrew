/**
 * Screenshot harness for badge-id session search.
 *
 * Runs the REAL built SPA (website/dist) behind the shared in-process static
 * server with all /api/** answered from fixtures (gateway-free), so the frames
 * show the actual sidebar component rather than a mock of it.
 *
 * The fixture is chosen so the badge clause is the ONLY thing that can retain a
 * row: /api/sessions/search answers with NO sessions, so the backend relevance
 * map is present but empty, and none of the queries below appears in any title.
 * Pre-fix the list empties; post-fix the badge-carrying row survives.
 *
 * Usage: node scripts/capture-badge-id-search.mjs [outDir] [prefix]
 *   Run against the branch build (prefix "after") and against a build carrying
 *   origin/main's ChatSidebar.tsx (prefix "before").
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/badge-id-search'
const PREFIX = process.argv[3] || 'after'

mkdirSync(OUT, { recursive: true })

const base_slot = {
  messages: 6, running: false, agent: 'kirocrew',
  created: '2026-08-30T01:00:00Z', last_ts: '2026-08-30T14:00:00Z', folder_id: '',
}

// Four open sessions. Not one title contains any query used below, so a
// retained row can only have been retained by its badge.
const slots = [
  {
    ...base_slot, key: 'chat-101', title: 'Publish the analytics poller change',
    source_links: [{
      provider: 'github', number: 4287, kind: 'change', label: '#4287',
      url: 'https://github.com/example-org/example-repo/pull/4287',
    }],
    source_links_total: 1,
  },
  {
    ...base_slot, key: 'chat-102', title: 'Tune the batch scheduler',
    source_links: [{
      provider: 'gitlab', number: 912, kind: 'change', label: '!912',
      url: 'https://gitlab.com/example-org/example-svc/-/merge_requests/912',
    }],
    source_links_total: 1,
  },
  // No `label`: the older-gateway shape. `chipLabel` renders `#7031` from the
  // number, so the chip looks identical and the id must still be searchable.
  {
    ...base_slot, key: 'chat-103', title: 'Refresh the cache warmer',
    source_links: [{
      provider: 'github', number: 7031, kind: 'change',
      url: 'https://github.com/example-org/example-repo/pull/7031',
    }],
    source_links_total: 1,
  },
  // No source_links at all: the flag-off / no-chips shape must not throw.
  { ...base_slot, key: 'chat-104', title: 'Draft the migration notes' },
]

const SEARCH_BOX = /search sessions/i

/** Slot keys of the session rows currently rendered, in DOM order. */
const rows = page => page.evaluate(() =>
  [...document.querySelectorAll('[data-session-row][data-session-scope="list"]')]
    .map(r => r.getAttribute('data-session-row')))

/** Badge chip labels currently rendered, so frame 1 proves chips exist. */
const chips = page => page.evaluate(() =>
  [...document.querySelectorAll('a[href*="/pull/"], a[href*="/merge_requests/"]')]
    .map(a => a.textContent.trim()).filter(Boolean))

async function shoot(page, name, note) {
  const seen = await rows(page)
  console.log(`ROWS(${PREFIX}/${name}): ${JSON.stringify(seen)}  ${note}`)
  await page.screenshot({
    path: `${OUT}/${PREFIX}-${name}.png`,
    clip: { x: 0, y: 0, width: 480, height: 900 },
  })
  console.log(`saved ${OUT}/${PREFIX}-${name}.png`)
  return seen
}

async function main() {
  const { srv, base } = await serveDist()
  // A mise-managed node exports its own lib/node on LD_LIBRARY_PATH, which the
  // browser child inherits — its older libstdc++ then loses to the system
  // libgallium/libLLVM and chromium fails to launch. Same override the sibling
  // harnesses carry.
  const browser = await chromium.launch({ env: { ...process.env, LD_LIBRARY_PATH: '/usr/lib64' } })
  const context = await browser.newContext({
    viewport: { width: 1400, height: 900 },
    deviceScaleFactor: 2, // sidebar type renders soft at 1x on GitHub
  })
  const page = await context.newPage()

  await stubDashboardApi(page, {
    folders: [], slots,
    extra: async (path, route) => {
      // Deliberately empty: the backend contributes NOTHING for these queries,
      // so any row that survives was kept by its badge.
      if (path === '/api/sessions/search') {
        await route.fulfill({ contentType: 'application/json', body: JSON.stringify({ sessions: [] }) })
        return true
      }
      return false
    },
  })
  logPageProblems(page)

  await page.goto(base + '/chat', { waitUntil: 'domcontentloaded' })
  await page.waitForTimeout(2600)

  console.log(`CHIPS(${PREFIX}): ${JSON.stringify(await chips(page))}`)
  await shoot(page, '1-no-query', 'all four rows, badges visible')

  // Bare number of a badge, absent from every title.
  await page.getByPlaceholder(SEARCH_BOX).fill('4287')
  await page.waitForTimeout(1400)
  await shoot(page, '2-number-match', 'expect only the #4287 row')

  // Provider label, sigil included.
  await page.getByPlaceholder(SEARCH_BOX).fill('!912')
  await page.waitForTimeout(1400)
  await shoot(page, '3-label-match', 'expect only the !912 row')

  // The rendered text of a chip whose payload carries no label at all.
  await page.getByPlaceholder(SEARCH_BOX).fill('#7031')
  await page.waitForTimeout(1400)
  await shoot(page, '5-rendered-label-match', 'expect only the #7031 row')

  await browser.close()
  srv.close()
}

main().catch(err => { console.error(err); process.exit(1) })
