/**
 * Screenshot harness for the tag picker's rejected-write state.
 *
 * Runs the REAL built SPA (website/dist) behind the shared `serveDist` server
 * and answers every /api/** call from fixtures through the shared
 * `stubDashboardApi` helper. No gateway, no dashboard auth, no kiro-cli.
 *
 * The scene-specific stub is one route: `PUT /api/chat/slots/{key}/tags` answers
 * `409 {"error": "session was deleted or rebound", "code": "session_gone"}` —
 * the exact rejection `chat_tags.py` returns when the slot vanished between
 * the picker opening and the click. Nothing here fabricates a status the
 * backend does not produce, so the shot proves exactly the claim under test:
 * a rejected tag write rolls the checkmark back AND surfaces a dismissible
 * inline notice inside the picker instead of silently reverting.
 *
 * Usage: node scripts/capture-slot-tag-write-failure.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { join } from 'node:path'

import { json } from './lib/boot-api.mjs'
import { serveDist } from './lib/serve-dist.mjs'
import { stubDashboardApi } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '/tmp/slot-tag-write-failure-shots'

mkdirSync(OUT, { recursive: true })

const SLOT = 'chat-1'
const FAILURE = 'session was deleted or rebound'
const TAGS = [
  { id: 'tag-review', name: 'Review', color: '#3b82f6', order: 1, status: false },
  { id: 'tag-urgent', name: 'Urgent', color: '#ef4444', order: 2, status: false },
]

const { srv, base } = await serveDist()
const browser = await chromium.launch()
const context = await browser.newContext({ viewport: { width: 1500, height: 950 }, deviceScaleFactor: 2 })
const page = await context.newPage()

/** Each branch AWAITS `json()` then returns true; a falsy return means "not handled". */
const extra = async (path, route) => {
  if (/^\/api\/chat\/slots\/[^/]+\/tags$/.test(path) && route.request().method() === 'PUT') {
    await json(route, { error: FAILURE, code: 'session_gone' }, 409)
    return true
  }
  if (path === '/api/chat/tags') {
    await json(route, TAGS)
    return true
  }
  return false
}

await stubDashboardApi(page, {
  slots: [{ key: SLOT, messages: 3, running: false, agent: 'kirocrew', mode: '', tags: [], tags_revision: 'rev-1' }],
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

// Right-click the session row (its Radix ContextMenuTrigger) and pick "Tags…"
// to open the picker — the same gesture the reported bug (#8183) was filed on.
await page.locator(`[data-slot-key="${SLOT}"]`).first().click({ button: 'right' })
await page.getByRole('menuitem', { name: /^tags/i }).first().click()

// Click "Review". The stubbed 409 rejects the write: the checkmark rolls back
// and the inline notice appears. The notice is an `ErrorNotice` alert region —
// wait for it rather than sleeping, so the shot cannot race the render.
const dialog = page.getByRole('dialog')
await dialog.getByRole('menuitemcheckbox', { name: /Review/ }).click()
await dialog.getByRole('alert').filter({ hasText: FAILURE }).first()
  .waitFor({ state: 'visible', timeout: 5000 })

const out = join(OUT, 'after-01-slot-tag-write-failure-notice.png')
await page.screenshot({ path: out })
console.log('wrote', out)

await browser.close()
srv.close()
