/**
 * Screenshot harness for the session-summary panel's on-demand generation.
 *
 * The change is the empty state, so the evidence has to be the empty state in
 * each of its three forms: the session that CAN be summarized (a button that
 * names the work and a line that names its cost), the session too short to be
 * worth summarizing (a sentence and deliberately no button), and the
 * unavailable case that keeps the read-only "Check again" this panel shipped
 * with. The in-progress frame is included because the button's label changes
 * while the pass runs, and a still of the resting state cannot show that.
 *
 * Runs the REAL built SPA (website/dist) behind the shared loopback static
 * server with every /api/** call answered from fixtures (gateway-free — no
 * kiro-cli, no dashboard token, so the theme boots from the stub rather than
 * falling back to the wrong accent).
 *
 * Usage: node scripts/capture-session-summary-generate.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/session-summary-on-demand'
const SLOT = 'session-summary-on-demand'

mkdirSync(OUT, { recursive: true })

const slots = [{
  key: SLOT,
  title: 'Session summary — on-demand generation',
  running: false,
  messages: 24,
  agent: 'kirocrew',
  modified: Math.floor(Date.now() / 1000),
  last_ts: '2026-08-14T22:00:00Z',
  folder_id: '',
}]

/** The panel's payload, empty of intents, in each generate_state. */
const emptyBody = state => ({
  enabled: true,
  stale: false,
  intents: [],
  constraints: [],
  generated_at: null,
  user_turns: null,
  last_activity: null,
  generate_state: state,
})

/** What a completed pass returns, so the after-frame is a real render. */
const populated = {
  enabled: true,
  stale: false,
  generated_at: Date.now() / 1000,
  user_turns: 24,
  last_activity: '2026-08-14T22:00:00Z',
  constraints: ['Screenshots are captured from the built bundle, never a live gateway.'],
  intents: [{
    title: 'Summarize a session that was never summarized',
    initial_intent: 'I turned summaries on but every older session stayed empty.',
    progress: [
      'Generation is a separate POST, so opening the panel still costs nothing.',
      'The forced pass lifts only the clean-stop and cadence gates.',
    ],
    next_steps: [{
      what: 'Summarize the other sessions you care about',
      why: 'Enabling the feature does not backfill them.',
      expect: 'One pass each, on your click.',
    }],
    ranges: [[1, 24]],
    status: 'active',
    verified: null,
    state: 'in-progress',
    last_touched_turn: 24,
    origin_turn: 1,
  }],
}

async function shoot(page, name) {
  await page.screenshot({ path: `${OUT}/${name}.png` })
  console.log('wrote', `${OUT}/${name}.png`)
}

async function openPanel(page, base) {
  await page.addInitScript(slot => {
    localStorage.setItem('mc-active-slot', slot)
    localStorage.setItem('mc-activity-open:' + slot, 'true')
    localStorage.setItem('mc-privacy-notice-v1', '1')
    localStorage.setItem('mc-panel-tabs:' + slot, JSON.stringify({
      tabs: [{ id: 'summary', kind: 'summary', title: 'Summary' }],
      activeId: 'summary',
    }))
  }, SLOT)
  await page.goto(base + '/chat', { waitUntil: 'domcontentloaded' })
  await page.waitForTimeout(2600)
}

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  const context = await browser.newContext({
    viewport: { width: 1400, height: 900 },
    deviceScaleFactor: 2, // 11-12px empty-state copy renders soft at 1x
  })

  // --- the three resting states, dark ---------------------------------------
  for (const [state, name] of [
    ['ready', '01-ready'],
    ['too_few_turns', '02-too-few-turns'],
    ['unavailable', '03-unavailable-check-again'],
  ]) {
    const page = await context.newPage()
    logPageProblems(page)
    await stubDashboardApi(page, {
      slots,
      theme: 'dark',
      extra: (path, route) => {
        if (path.includes('/summary')) {
          route.fulfill({
            status: 200,
            contentType: 'application/json',
            body: JSON.stringify(emptyBody(state)),
          })
          return true
        }
        return false
      },
    })
    await openPanel(page, base)
    await shoot(page, name)
    await page.close()
  }

  // --- blocked mid-turn: ready from the server, disabled by the live turn ----
  // The block comes from the STORE, not the payload, so this frame is produced
  // by answering the slot-detail endpoint with `running: true` — the same route
  // `switchSlot` reads to set the slot's stream state. The summary payload is
  // deliberately still `ready`, which is what makes this frame prove the
  // client-side gate rather than a second server state.
  {
    const page = await context.newPage()
    logPageProblems(page)
    await stubDashboardApi(page, {
      slots,
      theme: 'dark',
      extra: (path, route) => {
        if (path.includes('/summary')) {
          route.fulfill({
            status: 200,
            contentType: 'application/json',
            body: JSON.stringify(emptyBody('ready')),
          })
          return true
        }
        if (path.startsWith('/api/chat/slots/')) {
          route.fulfill({
            status: 200,
            contentType: 'application/json',
            body: JSON.stringify({
              key: SLOT,
              messages: [],
              running: true,
              stopping: false,
              has_more: false,
              total: 24,
              queue: [],
              next_before: 0,
            }),
          })
          return true
        }
        return false
      },
    })
    await openPanel(page, base)
    await shoot(page, '07-blocked-mid-turn')
    await page.close()
  }

  // --- in progress, then the result ----------------------------------------
  {
    const page = await context.newPage()
    logPageProblems(page)
    let done = false
    await stubDashboardApi(page, {
      slots,
      theme: 'dark',
      extra: async (path, route) => {
        if (!path.includes('/summary')) return false
        if (route.request().method() === 'POST') {
          // Hold the response open so the in-progress label is capturable.
          await new Promise(r => setTimeout(r, 2500))
          done = true
          route.fulfill({
            status: 200,
            contentType: 'application/json',
            body: JSON.stringify(populated),
          })
          return true
        }
        route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify(done ? populated : emptyBody('ready')),
        })
        return true
      },
    })
    await openPanel(page, base)
    await page.getByRole('button', { name: /^Summarize$/i }).click()
    await page.waitForTimeout(600)
    await shoot(page, '04-generating')
    await page.waitForTimeout(3500)
    await shoot(page, '05-after-generating')
    await page.close()
  }

  // --- the offer in light, since the cost line is the thing to read --------
  {
    const page = await context.newPage()
    logPageProblems(page)
    await stubDashboardApi(page, {
      slots,
      theme: 'light',
      extra: (path, route) => {
        if (path.includes('/summary')) {
          route.fulfill({
            status: 200,
            contentType: 'application/json',
            body: JSON.stringify(emptyBody('ready')),
          })
          return true
        }
        return false
      },
    })
    await openPanel(page, base)
    await shoot(page, '06-ready-light')
    await page.close()
  }

  await browser.close()
  srv.close()
}

main().catch(err => {
  console.error(err)
  process.exit(1)
})
