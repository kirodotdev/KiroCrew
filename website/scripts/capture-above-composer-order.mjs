/**
 * Screenshot harness + geometry check for the ABOVE-COMPOSER STACKING ORDER.
 *
 * The band between the transcript and the input box can hold two very different
 * things at once:
 *
 *   - an OPTIONS row (FollowUpBar) — the assistant's `[OPTIONS:]` choices, which
 *     belong with the turn that asked the question, i.e. up with the transcript;
 *   - a CARD (TipCard, or FolderSuggestionCard) — an ambient/one-shot note that
 *     is attached to the COMPOSER, not to any turn.
 *
 * The rule: the card is the LAST thing before the input box, so it stays flush
 * against it, and the options row stacks above the card.
 *
 * This asserts as well as photographs: it measures real bounding boxes in the
 * REAL built SPA (website/dist) and exits non-zero if the card is not (a) below
 * every options chip and (b) within GAP_MAX px of the input box. Nothing in CI
 * runs this file, so treat it as a manual guard — the CI-enforced half of the
 * invariant is the DOM-order test in src/test/ChatInput.test.tsx.
 *
 * To photograph the inverted layout for a before/after comparison, check the two
 * components out at a ref that predates the fix
 * (`git checkout <ref> -- src/components/ChatInput.tsx src/pages/ChatPage.tsx`),
 * `npm run build`, and run this with a different outDir; every scenario then
 * reports `belowOptions: false` with a ~50px gap.
 *
 * Usage: node scripts/capture-above-composer-order.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/above-composer-order'
const SLOT = 'chat-band'
const PROJECT = '/home/user/workspace/notes'

/**
 * Card bottom → input-box top, in the NO-APPROVAL state these scenarios set up.
 * The composer carries a 6px drag-handle gutter, so 6 is "flush"; the ceiling
 * allows sub-pixel layout rounding, and would be blown wide open (50px+) by an
 * options row sneaking back in between.
 *
 * A tool-approval bar or a sub-agent spawn-approval banner legitimately renders
 * between the card and the textarea — the approval bar is border-fused to the
 * composer, so the card is still attached to the composer assembly. Do NOT
 * raise this ceiling to accommodate such a fixture: measure the top of the
 * approval bar as `boxTop` instead, or the guard stops meaning anything.
 */
const GAP_MAX = 10

mkdirSync(OUT, { recursive: true })

const OPTIONS = ['Show me the diff', 'Run the tests first', 'Skip it for now']

const slots = [{
  key: SLOT,
  title: 'Why does the export button do nothing?',
  running: true,            // the tip trigger only arms on a running slot
  last_message: 'Two candidates — which do you want first?',
  messages: 2,
  agent: 'kirocrew',
  memory_mode: 'persistent',
  project: PROJECT,
  folder_id: '',            // unfiled — precondition for the folder card
  modified: Math.floor(Date.now() / 1000),
  source_links: [],
  source_links_total: 0,
}]

/**
 * Last message is `assistant` (NOT `streaming`) so the options row renders:
 * deriveFollowUpOptions() returns nothing while a turn is still streaming.
 * The slot stays `running` so useTipTrigger arms — that combination (running
 * slot, settled last turn) is exactly when both surfaces can co-occur.
 */
const detail = {
  running: true,
  has_more: false,
  total: 2,
  queue: [],
  project: PROJECT,
  messages: [
    {
      role: 'user',
      ts: Date.now() / 1000 - 600,
      content: 'The export button does nothing on the second click.',
    },
    {
      role: 'assistant',
      ts: Date.now() / 1000 - 30,
      content:
        'The handler is registered twice, so the second click hits a listener '
        + 'whose closure captured a stale row id.\n\n'
        + `[OPTIONS: ${OPTIONS.join(' | ')}]`,
    },
  ],
}

const folders = [
  { id: 'f-notes', name: 'Notes', order: 0, parent_id: '' },
  { id: 'f-bugs', name: 'bug', order: 1, parent_id: 'f-notes' },
]

const tip = {
  id: 'tip-band-1',
  feature: 'monitor',
  title: 'Keep an eye on a long job',
  body: 'Ask to _monitor_ a build and the loop wakes on an interval instead of holding the turn open.',
  why: 'Long waits do not need a held turn',
  doc: 'monitoring.md',
  cta_prompt: '',
  action: null,
}

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  const context = await browser.newContext({
    viewport: { width: 1500, height: 950 },
    // The band is dense 11–12px type; 1x renders it soft on GitHub.
    deviceScaleFactor: 2,
  })

  /**
   * Routes the shared stub does not know about. Each branch AWAITS `json()` and
   * then returns `true`: the stub reads a falsy return as "not handled" and
   * fulfils the route itself, and `json()` resolves to undefined, so
   * `return json(...)` alone would both double-fulfil ("Route is already
   * handled!") and leave the fulfilment unawaited — which surfaces as an
   * unhandled rejection when a page is closed mid-route, and this harness
   * closes and re-creates its page per theme.
   */
  const extra = async (path, route) => {
    if (path === '/api/tips/status') {
      // Tiny cadence → the client's 20-minute floor collapses, so the tip is
      // not gated by a previous run's localStorage stamp.
      await json(route, { enabled: true, cadence_hours: 0.0001 })
      return true
    }
    if (path === '/api/tips/next') { await json(route, { tip, glow: false }); return true }
    if (path === '/api/tips/feedback') { await json(route, { ok: true }); return true }
    if (/^\/api\/chat\/slots\/[^/]+\/folder$/.test(path)) { await json(route, { ok: true }); return true }
    if (path.startsWith('/api/chat/slots/')) { await json(route, detail); return true }
    return false
  }

  let page = null
  let wsServer = null

  /** A FRESH page per theme — stubDashboardApi bakes the theme into /api/theme/boot. */
  async function load(theme) {
    if (page) await page.close()
    wsServer = null
    page = await context.newPage()
    logPageProblems(page)
    await stubDashboardApi(page, { folders, slots, theme, extra })
    // AFTER the shared stub so this wins: the stub swallows /api/ws to stop a
    // retry storm, but this harness needs the socket to push the card frame.
    await page.routeWebSocket(/\/api\/ws/, ws => { wsServer = ws })
    await page.addInitScript(slot => {
      localStorage.setItem('mc-active-slot', slot)
      localStorage.removeItem('kirocrew.tips.lastShownAt')
    }, SLOT)
    await page.goto(base + '/', { waitUntil: 'domcontentloaded' })
    await page.waitForTimeout(2500)
  }

  /** Push the folder card exactly as maybe_suggest_folder broadcasts it. */
  async function pushFolderCard() {
    if (!wsServer) throw new Error('websocket route never bound')
    wsServer.send(JSON.stringify({
      type: 'slot_folder_suggestion',
      data: {
        slot: SLOT,
        folder_id: 'f-bugs',
        folder_name: 'bug',
        breadcrumb: 'Notes › bug',
        ts: Date.now() / 1000,
      },
    }))
    await page.waitForTimeout(900)
  }

  /**
   * Measure the band. Returns null when a piece is missing so the caller can
   * fail loudly instead of silently "passing" on an empty page.
   */
  async function measure(cardTestId) {
    const card = await page.getByTestId(cardTestId).first().boundingBox().catch(() => null)
    const box = await page.getByTestId('input-wrapper').first().boundingBox().catch(() => null)
    const chips = []
    for (const label of OPTIONS) {
      const b = await page.getByRole('button', { name: label }).first().boundingBox().catch(() => null)
      if (b) chips.push(b)
    }
    if (!card || !box || chips.length !== OPTIONS.length) return null
    const chipsBottom = Math.max(...chips.map(c => c.y + c.height))
    return {
      cardTop: card.y,
      cardBottom: card.y + card.height,
      chipsTop: Math.min(...chips.map(c => c.y)),
      chipsBottom,
      boxTop: box.y,
      belowOptions: card.y >= chipsBottom - 0.5,
      aboveBox: card.y + card.height <= box.y + 0.5,
      gapToBox: box.y - (card.y + card.height),
    }
  }

  /** Crop the whole band: topmost chip through the first composer row. */
  async function band(name, m) {
    const top = Math.max(0, Math.min(m.chipsTop, m.cardTop) - 18)
    await page.screenshot({
      path: `${OUT}/${name}.png`,
      clip: { x: 250, y: top, width: 1240, height: (m.boxTop - top) + 96 },
    })
    console.log('wrote', `${OUT}/${name}.png`)
  }

  const results = []

  async function scenario(name, theme, prepare, cardTestId) {
    await load(theme)
    await prepare()
    const m = await measure(cardTestId)
    if (!m) {
      results.push({ name, ok: false, why: 'band did not render (card, options row, or input box missing)' })
      await page.screenshot({ path: `${OUT}/${name}-MISSING.png` })
      return
    }
    await band(name, m)
    const ok = m.belowOptions && m.aboveBox && m.gapToBox <= GAP_MAX
    results.push({
      name,
      ok,
      belowOptions: m.belowOptions,
      aboveBox: m.aboveBox,
      gapToBox: Math.round(m.gapToBox * 10) / 10,
    })
  }

  // 1 + 2. Folder card + options together — the reported bug, both themes.
  await scenario('01-folder-card-with-options-dark', 'dark', pushFolderCard, 'folder-suggestion-card')
  await scenario('02-folder-card-with-options-light', 'light', pushFolderCard, 'folder-suggestion-card')

  // 3. Tip card + options together. The tip arms on a 10s timer inside
  //    useTipTrigger, so this waits it out rather than reaching into the store.
  await scenario('03-tip-card-with-options-dark', 'dark', async () => {
    await page.waitForTimeout(11500)
    await page.getByTestId('tip-card').first().waitFor({ timeout: 8000 })
  }, 'tip-card')

  await browser.close()
  srv.close()

  console.log('--- assertions (card must be BELOW the options row and flush to the input box) ---')
  for (const r of results) console.log(JSON.stringify(r))

  if (!results.every(r => r.ok)) {
    console.error(`FAIL: a card was not flush against the input box (gap ceiling ${GAP_MAX}px)`)
    process.exit(1)
  }
  console.log('OK')
}

main().catch(err => { console.error(err); process.exit(1) })
