/**
 * Screenshot harness for agent-switch failure feedback.
 *
 * Runs the REAL built SPA (website/dist) behind the shared `serveDist` server
 * and answers every /api/** call from fixtures through the shared
 * `stubDashboardApi` helper. No gateway, no dashboard auth, no kiro-cli.
 *
 * The scene-specific stub is one route: `POST /api/chat/slots/*​/agent` answers
 * `400 {"error": "invalid agent name"}` — a failure the endpoint really returns
 * today. Nothing here fabricates a status the backend does not produce, so the
 * shot proves exactly the claim under test: an agent-switch API failure becomes
 * visible instead of being swallowed.
 *
 * Usage: node scripts/capture-agent-switch-failure.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { join } from 'node:path'

import { json } from './lib/boot-api.mjs'
import { serveDist } from './lib/serve-dist.mjs'
import { stubDashboardApi } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '/tmp/agent-switch-failure-shots'

mkdirSync(OUT, { recursive: true })

const SLOT = 'chat-1'
const FAILURE = 'invalid agent name'

const { srv, base } = await serveDist()
const browser = await chromium.launch()
const context = await browser.newContext({ viewport: { width: 1500, height: 950 }, deviceScaleFactor: 2 })
const page = await context.newPage()

/** Each branch AWAITS `json()` then returns true; a falsy return means "not handled". */
const extra = async (path, route) => {
  if (/^\/api\/chat\/slots\/[^/]+\/agent$/.test(path)) {
    await json(route, { error: FAILURE }, 400)
    return true
  }
  if (path === '/api/agents') {
    await json(route, {
      agents: [
        { name: 'kirocrew', description: 'Default crew agent' },
        { name: 'reviewer', description: 'Reviews diffs against the repo conventions' },
      ],
      default: 'kirocrew',
    })
    return true
  }
  return false
}

await stubDashboardApi(page, {
  slots: [{ key: SLOT, messages: 0, running: false, agent: 'kirocrew', mode: '' }],
  extra,
})
// Pin the locale: without it the SPA picks one from the environment and the
// shot comes out in whatever language the runner happens to negotiate.
await page.addInitScript(slot => {
  localStorage.setItem('mc-active-slot', slot)
  localStorage.setItem('mc-lang', 'en')
}, SLOT)
await page.goto(base + '/chat', { waitUntil: 'domcontentloaded' })
await page.waitForTimeout(2500)

// Open the agent picker and choose the other agent; the stubbed 400 rejects it.
// Anchored on the composer control's own label — a loose /agent/i matches the
// sidebar's "Agent Capabilities" entry first and opens the wrong surface.
await page.getByRole('button', { name: /^Agent: / }).first().click()
await page.getByRole('option', { name: /reviewer/ }).first().click()

// The notice is an aria-live status region — wait for it rather than sleeping,
// so the shot cannot race the render or photograph an already-expired notice.
await page.getByRole('status').filter({ hasText: FAILURE }).first()
  .waitFor({ state: 'visible', timeout: 5000 })

const out = join(OUT, 'after-01-agent-switch-failure-notice.png')
await page.screenshot({ path: out })
console.log('wrote', out)

await browser.close()
srv.close()
