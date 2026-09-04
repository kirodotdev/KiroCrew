/**
 * Screenshot harness + behavior check for the ?sid= DEEP-LINK NOT-FOUND BANNER
 * once its 5s deadline waits for the slot list.
 *
 * The resolve effects can only match `sid` against `dashboard.slots`, filled by
 * `GET /api/chat/slots`. The deadline used to arm on WebSocket connect alone, and
 * firing is one-way — it clears `initialSidRef` — so a list arriving at 5.1s left
 * nothing to resolve and the banner denied a session that was live. Gating the
 * timer on `slotsLoaded` moves the deadline to the list's arrival.
 *
 * Both arms are the point, so both are photographed: the gate must DELAY the
 * deadline, not remove it.
 *
 * This asserts as well as photographs, against the REAL built SPA
 * (website/dist): each scene holds the slot list past the old deadline and exits
 * non-zero unless the banner's presence matches the arm. Nothing in CI runs this
 * file — the CI-enforced half is ChatPage.sid.test.tsx.
 *
 * Usage: node scripts/capture-deeplink-sid-late-slots.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/deeplink-sid-late-slots'

/** The deep-linked session. Live on the gateway, absent from the slow list. */
const LINKED = 'chat-1042-1788500000'
const PROJECT = '/home/user/workspace/notes'

/** Longer than the banner's 5s deadline, so the old code had already fired. */
const HOLD_MS = 8000
/** Long enough after the list lands for a re-armed 5s deadline to elapse. */
const SETTLE_MS = 7000

mkdirSync(OUT, { recursive: true })

const slot = (key, title) => ({
  key,
  title,
  running: false,
  last_message: 'Ready when you are.',
  messages: 2,
  agent: 'default',
  memory_mode: 'persistent',
  project: PROJECT,
  folder_id: '',
  modified: Math.floor(Date.now() / 1000),
  source_links: [],
  source_links_total: 0,
})

/** Sessions the list DOES carry either way — so nothing auto-creates a slot. */
const OTHERS = [slot('chat-2519-1788540001', 'Release notes pass'), slot('chat-2402-1788498220', 'Packout retry audit')]

const detail = {
  running: false,
  has_more: false,
  total: 2,
  queue: [],
  project: PROJECT,
  messages: [
    { role: 'user', ts: Date.now() / 1000 - 600, content: 'Why is the deep link intermittent?' },
    { role: 'assistant', ts: Date.now() / 1000 - 590, content: 'The not-found deadline was racing the slot list.' },
  ],
}

/**
 * One fresh page per arm, opened on the deep link with the slot list withheld.
 *
 * `carriesLink` decides whether the list, when it finally lands, contains the
 * linked session. The hold is applied inside the route handler for that one
 * request, so every other endpoint answers immediately and the page boots.
 */
async function openDeepLink(context, base, { carriesLink }) {
  const list = carriesLink ? [...OTHERS, slot(LINKED, 'Chat not found bug investigation')] : OTHERS
  const extra = async (path, route) => {
    if (path === '/api/chat/slots') {
      await new Promise(r => setTimeout(r, HOLD_MS))
      await json(route, list)
      return true
    }
    if (path.startsWith('/api/chat/slots/')) { await json(route, detail); return true }
    if (path === '/api/slash-commands') { await json(route, []); return true }
    return false
  }
  const page = await context.newPage()
  logPageProblems(page)
  await stubDashboardApi(page, { slots: [], theme: 'dark', extra })
  await page.goto(`${base}/chat?sid=${LINKED}`, { waitUntil: 'domcontentloaded' })
  return page
}

const banner = page => page.getByText(`Session "${LINKED}" not found`, { exact: false })

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  const context = await browser.newContext({
    viewport: { width: 1400, height: 950 },
    deviceScaleFactor: 2,
    locale: 'en-US',
  })

  const failures = []

  /* ── Scene 1: list still in flight at 6s → no banner (the old deadline had fired here) ── */
  let page = await openDeepLink(context, base, { carriesLink: true })
  await page.waitForTimeout(6000)
  if (await banner(page).count() !== 0) failures.push('scene 1: banner fired while the slot list was still in flight')
  await page.screenshot({ path: `${OUT}/1-list-in-flight-no-banner.png` })
  console.log('wrote', `${OUT}/1-list-in-flight-no-banner.png`)

  /* ── Scene 2 (same page): the list lands CARRYING the key → link resolves, still no banner ── */
  await page.waitForTimeout(SETTLE_MS)
  if (await banner(page).count() !== 0) failures.push('scene 2: banner fired for a session the list carries')
  if (await page.getByText('Chat not found bug investigation').count() === 0) {
    failures.push('scene 2: the deep-linked session did not open')
  }
  await page.screenshot({ path: `${OUT}/2-late-list-carries-key-resolves.png` })
  console.log('wrote', `${OUT}/2-late-list-carries-key-resolves.png`)
  await page.close()

  /* ── Scene 3: the list lands WITHOUT the key → banner fires (control: the gate delays) ── */
  page = await openDeepLink(context, base, { carriesLink: false })
  await page.waitForTimeout(HOLD_MS + SETTLE_MS)
  if (await banner(page).count() === 0) {
    failures.push('scene 3: no banner for a key genuinely absent from the list — the gate disabled the deadline')
  }
  await page.screenshot({ path: `${OUT}/3-late-list-lacks-key-banner.png` })
  console.log('wrote', `${OUT}/3-late-list-lacks-key-banner.png`)
  await page.close()

  await browser.close()
  srv.close()

  if (failures.length) {
    for (const f of failures) console.error('FAIL:', f)
    process.exit(1)
  }
  console.log('PASS: the not-found deadline waits for the slot list, and still fires once a list arrives without the key')
}

main().catch(err => { console.error(err); process.exit(1) })
