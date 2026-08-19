/**
 * Screenshot harness for the open-items overflow row in the session-summary
 * panel.
 *
 * The change is that the row is a control rather than a caption, and a still of
 * the resting state cannot show a click working — so the evidence is a pair per
 * theme: the capped block offering the rest, and the same block after the click
 * with every open item rendered and a way back. Each pair is captured twice, as
 * the whole panel in the app for context and clipped to the card for legibility,
 * because the row is 12px type inside a 1400px window.
 *
 * Runs the REAL built SPA (website/dist) behind the shared loopback static
 * server with every /api/** call answered from fixtures (gateway-free — no
 * kiro-cli, no dashboard token, so the theme boots from the stub rather than
 * falling back to the wrong accent).
 *
 * Usage: node scripts/capture-session-summary-open-items.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/session-summary-open-items'
const SLOT = 'session-summary-open-items'

mkdirSync(OUT, { recursive: true })

const slots = [{
  key: SLOT,
  title: 'Session summary — open items overflow',
  running: false,
  messages: 40,
  agent: 'kirocrew',
  modified: Math.floor(Date.now() / 1000),
  last_ts: '2026-08-18T20:00:00Z',
  folder_id: '',
}]

/** Five needs-you intents, so the block caps at three and two are withheld —
 *  the only shape in which the overflow row exists at all. */
const INTENTS = [
  ['Land the session-list redesign', 'Re-run the frontend gates on the rebased branch', 'The rebase moved 47 commits under the branch.', 'tsc and vitest green, or a named failure.'],
  ['Ship a build the team can install', 'Confirm the DMG opens on a clean machine', 'It is unsigned, so Gatekeeper is the real test.', 'A first launch that needs one right-click.'],
  ['Settle the flat-view meta line', 'Decide whether untagged rows show a folder fallback', 'The flat view leaves those rows with an empty meta line.', 'One line of copy either way.'],
  ['Keep the PR body honest after a rebase', 'Re-pin the screenshot SHAs after the force-push', 'The PR body points at the pre-rebase commit.', 'Images that load in the PR body.'],
  ['Tidy the local checkout', 'Prune the four merged local branches', 'They are gone upstream but still listed locally.', 'A shorter branch list.'],
]

const summary = {
  enabled: true,
  stale: false,
  generated_at: Date.now() / 1000 - 240,
  user_turns: 40,
  last_activity: '2026-08-18T20:00:00Z',
  constraints: ['Screenshots are captured from the built bundle, never a live gateway.'],
  intents: INTENTS.map(([title, what, why, expect], i) => ({
    title,
    initial_intent: null,
    progress: [why],
    next_steps: [{ what, why, expect }],
    ranges: [[i * 8 + 1, i * 8 + 7]],
    status: 'completed',
    verified: false,
    state: 'needs-you',
    last_touched_turn: 40 - i,
    origin_turn: null,
  })),
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

async function shootPanel(page, name) {
  await page.screenshot({ path: `${OUT}/${name}.png` })
  console.log('wrote', `${OUT}/${name}.png`)
}

/** Clip to the card holding the heading, with a little air around it. */
async function shootCard(page, name) {
  const box = await page
    .getByText('Open items', { exact: true })
    .locator('xpath=ancestor::div[contains(@class,"rounded-md")][1]')
    .boundingBox()
  const pad = 10
  await page.screenshot({
    path: `${OUT}/${name}.png`,
    clip: { x: box.x - pad, y: box.y - pad, width: box.width + pad * 2, height: box.height + pad * 2 },
  })
  console.log('wrote', `${OUT}/${name}.png`)
}

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  const context = await browser.newContext({
    viewport: { width: 1400, height: 900 },
    deviceScaleFactor: 2, // the 12px overflow row renders soft at 1x
  })

  for (const theme of ['dark', 'light']) {
    const page = await context.newPage()
    logPageProblems(page)
    await stubDashboardApi(page, {
      slots,
      theme,
      extra: (path, route) => {
        if (!path.includes('/summary')) return false
        route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify(summary),
        })
        return true
      },
    })
    await openPanel(page, base)
    await shootPanel(page, `01-panel-collapsed-${theme}`)
    await shootCard(page, `02-card-collapsed-${theme}`)

    await page.getByRole('button', { name: /\+2 more/i }).click()
    await page.waitForTimeout(400)
    await shootPanel(page, `03-panel-expanded-${theme}`)
    await shootCard(page, `04-card-expanded-${theme}`)

    // The row under the pointer, so the hover treatment is on the record too.
    await page.getByRole('button', { name: /Show less/i }).hover()
    await page.waitForTimeout(250)
    await shootCard(page, `05-card-expanded-hover-${theme}`)
    await page.close()
  }

  await browser.close()
  srv.close()
}

main().catch(err => {
  console.error(err)
  process.exit(1)
})
