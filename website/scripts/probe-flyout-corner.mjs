/**
 * Diagnostic probe for the Sessions flyout's top-left corner.
 *
 * Boots the REAL built SPA with the sessions sidebar COLLAPSED and several
 * slots, hovers the collapsed toggle to open the recents flyout, then captures
 * the flyout's top-left corner at high zoom. The double-stroke bug (two
 * concentric rounded borders at that corner) is only visible this close, so the
 * shot is cropped to a ~48px box around the corner and scaled up.
 *
 * Usage: node scripts/probe-flyout-corner.mjs [outDir] [dist]
 */
import { mkdirSync } from 'node:fs'
import { openTranscriptHarness } from './lib/transcript-harness.mjs'

const OUT = process.argv[2] || '../temp-screenshots/flyout-corner'
const DIST = process.argv[3] || undefined
const SLOT = 'chat-aaa'
const PROJECT = '/home/user/workspace/KiroCrew'

mkdirSync(OUT, { recursive: true })

const now = Date.now() / 1000
const slots = Array.from({ length: 6 }, (_, i) => ({
  key: `chat-${'abcdef'[i]}${'abcdef'[i]}${'abcdef'[i]}`,
  title: `Session number ${i + 1}`,
  running: false,
  needs_input: false,
  pending_approval: false,
  agent: 'kirocrew',
  updated: now - i * 60,
  created: now - i * 600,
}))
const detail = { key: SLOT, messages: [], meta: {} }

const harness = await openTranscriptHarness({
  slot: SLOT,
  project: PROJECT,
  slots,
  detail,
  viewport: { width: 1280, height: 900 },
  deviceScaleFactor: 4,
  dist: DIST,
})

const { page, close } = harness

await harness.load('dark', { settle: 500 })

// Collapse the sidebar so the flyout becomes eligible (flyoutEligible requires
// !sidebarOpen). Click the hide-sessions toggle rather than seeding
// localStorage — the harness registers a localStorage.clear() init script that
// re-runs on every navigation, so a reload-based seed is wiped.
await page.locator('button[aria-label*="sessions sidebar" i], button[aria-label*="Hide sessions" i]').first()
  .click({ timeout: 10000 })
await page.waitForTimeout(600)

// The collapsed toggle carries aria-haspopup=menu. Hover it with real pointer
// movement so useHoverIntent fires (it has an open dwell ~HOVER_OPEN_MS), then
// wait for the flyout menu to appear.
const toggle = page.locator('button[aria-haspopup="menu"].pi-morph').first()
await toggle.waitFor({ state: 'visible', timeout: 10000 })
const box = await toggle.boundingBox()
await page.mouse.move(box.x + box.width / 2 - 3, box.y + box.height / 2 - 3)
await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2)
await page.waitForTimeout(500)
await page.mouse.move(box.x + box.width / 2 + 1, box.y + box.height / 2 + 1)
await page.waitForTimeout(500)

const flyout = page.locator('[role="menu"][aria-label]').first()
await flyout.waitFor({ state: 'visible', timeout: 10000 })
await page.waitForTimeout(600) // let the open clip-morph settle

const fb = await flyout.boundingBox()
console.log('FLYOUT_BOX', JSON.stringify(fb))

// Full flyout for context.
await flyout.screenshot({ path: `${OUT}/flyout-full.png` })

// Tight crop around the exact top-left corner (CSS px). At DPR 4 this 20x20
// CSS box renders as an 80x80 image, enough to resolve a 1px double stroke.
await page.screenshot({
  path: `${OUT}/flyout-topleft.png`,
  clip: { x: Math.max(0, fb.x - 2), y: Math.max(0, fb.y - 2), width: 20, height: 20 },
})

const outPath = await close()
console.log('DONE', OUT, outPath || '')
