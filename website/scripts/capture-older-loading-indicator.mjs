/**
 * Screenshot harness for the older-messages loading indicator.
 *
 * Runs the REAL built SPA (website/dist) behind the shared in-process static
 * server with all /api/** answered from fixtures (gateway-free).
 *
 * Reaching the indicator needs `slotHasMore && slotOldestIndex > 0`, and only a
 * BOUNDED load produces that: the unbounded slot-detail fetch normalizes
 * `rawCount` to `total`, so `oldestIndex = total - rawCount` is always 0 there.
 * `resumeFromHistory` uses a fixed 200-row page, so resuming a session whose
 * `total` exceeds it lands on oldestIndex = total - 200. That is why this drives
 * the real Older Sessions -> resume path rather than opening a slot directly.
 *
 * From there a PINNED message outside the loaded window is the one affordance
 * that dispatches `loadOlderMessages`. The fixture holds that request open, so
 * `loadingOlder` stays true — the state a user sees on a slow page fetch.
 *
 * Two frames: idle (nothing in flight) and pending (fetch outstanding).
 *
 * Two fixture shapes the real backend cannot produce, both of which silently
 * defeat this capture:
 *   - `has_more: true` beside an UNBOUNDED slot fetch. The normalizer sets
 *     rawCount = total there, so oldestIndex is 0 and the jump reports the
 *     message as no longer loaded.
 *   - a `surface`/`mode` of 'chat' on the resumed slot. ChatPage's
 *     `filteredSlots` keeps only '' | 'orchestrator' | 'crew', so any other
 *     value hides the slot, URL-sync cannot adopt it, and the post-mount ?sid
 *     effect switches straight back to whatever slot the URL already named.
 *
 * Usage: node scripts/capture-older-loading-indicator.mjs [outDir] [prefix]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/older-loading-indicator'
const PREFIX = process.argv[3] || 'after'

mkdirSync(OUT, { recursive: true })

const SLOT = 'chat-archived-release'
const TOTAL = 500          // history far exceeds the 200-row resume page
const RESUME_PAGE_RAW = 200 // mirrors chatSlice's constant; oldestIndex = TOTAL - this

// Only the newest turns come back with the resume page; the rest stay on disk.
const loaded = [
  { role: 'user', content: 'Can you summarise where the release checklist stands?', ts: '2026-08-13T09:58:00Z', meta: { mid: 'm-497' } },
  { role: 'assistant', content: 'Four items are still open: the changelog entry, the migration note, the smoke run, and the sign-off.', ts: '2026-08-13T09:58:30Z', meta: { mid: 'm-498' } },
  { role: 'user', content: 'Which one is blocking?', ts: '2026-08-13T09:59:00Z', meta: { mid: 'm-499' } },
  { role: 'assistant', content: 'The smoke run — it needs the staging deploy to finish first.', ts: '2026-08-13T09:59:30Z', meta: { mid: 'm-500' } },
]

// The archived session, as the Older Sessions pane lists it.
const history = [{
  key: SLOT, title: 'Release checklist review', messages: TOTAL,
  created: '2026-06-01T09:00:00Z', modified: 1786000000, agent: 'kirocrew',
  memory_mode: 'persistent',
}]

// Pinned early in the session, deliberately OUTSIDE the resumed window.
const pins = [{
  id: 'pin-1', slot_key: SLOT, mid: 'm-3',
  message_ts: '2026-06-01T09:04:00Z', role: 'user',
  preview: 'Original scope for the release checklist',
  pinned_at: '2026-06-01T09:05:00Z',
}]

async function main() {
  const { srv, base } = await serveDist()
  // mise's node injects LD_LIBRARY_PATH at its own bundled libstdc++, which is
  // older than the system Mesa needs; children inherit it, so scrub it here.
  const { LD_LIBRARY_PATH: _mise, ...browserEnv } = process.env
  const browser = await chromium.launch({ env: browserEnv })
  const context = await browser.newContext({ viewport: { width: 1280, height: 820 }, deviceScaleFactor: 2 })
  const page = await context.newPage()

  let olderRequested = false
  let resumed = false
  const reqLog = []

  // Both slots are present from the start: the URL-sync effect only writes a
  // ?sid for a slot in this list, and without that the post-mount sid effect
  // switches back to the URL's slot. Scratch has the newer last_ts so the app
  // still canonicalizes to it on mount rather than opening the archived one.
  // One scratch slot only: the archived session must NOT be open, or its row
  // stops being a history row and the click becomes an ordinary unbounded
  // switchSlot (cursor 0). See scripts/README note in the header.
  const openSlots = [{
    key: 'chat-current', title: 'Scratch', messages: 2, running: false,
    agent: 'kirocrew', created: '2026-08-13T09:00:00Z', last_ts: '2026-08-13T09:30:00Z', folder_id: '',
  }]

  await stubDashboardApi(page, {
    folders: [], slots: openSlots,
    extra: async (path, route) => {
      if (path === '/api/sessions') { await json(route, { sessions: history, has_more: false }); return true }
      if (path === `/api/chat/slots/${SLOT}/resume`) {
        resumed = true
        // next_before is what the server computes (total - len(recent)); it drives
        // the cursor once CR-3354 lands, while total/has_more drive it today. Both
        // agree on TOTAL - RESUME_PAGE_RAW, so this payload fits either shape.
        await json(route, { ok: true, key: SLOT, messages: loaded, has_more: true, total: TOTAL,
          next_before: TOTAL - RESUME_PAGE_RAW, memory_mode: 'persistent' })
        return true
      }
      if (path === '/api/chat/slots/chat-current') {
        await json(route, { messages: [{ role: 'user', content: 'scratch', ts: '2026-08-13T09:30:00Z', meta: { mid: 'sc-1' } }], has_more: false, total: 1 })
        return true
      }
      if (path === `/api/chat/slots/${SLOT}`) {
        const q = new URL(route.request().url()).searchParams
        reqLog.push(q.has('before') ? `before=${q.get('before')} limit=${q.get('limit')}` : `UNBOUNDED limit=${q.get('limit')}`)
        if (q.has('before')) {
          // Held open so `loadingOlder` stays true while the frame is captured.
          // The request really is outstanding — this is the slow-fetch state.
          olderRequested = true
          await new Promise(r => setTimeout(r, 15_000))
          await json(route, { messages: [], has_more: false, total: TOTAL, next_before: 0 })
          return true
        }
        await json(route, { messages: loaded, has_more: true, total: TOTAL })
        return true
      }
      if (path === '/api/chat/pins') { await json(route, { pins }); return true }
      return false
    },
  })
  logPageProblems(page)
  page.on('pageerror', e => console.log('PAGEERROR', e.message))

  // Slotless /chat, as production does: the Older-Sessions handler only changes
  // activeSlot in the store, so a slot in the URL would be re-asserted over it.
  await page.goto(`${base}/chat`, { waitUntil: 'domcontentloaded' })
  await page.waitForTimeout(1500)

  // Open the Older Sessions pane and resume the archived session — the only
  // path that yields a bounded page, hence slotOldestIndex > 0.
  await page.getByText(/Older Sessions/i).first().click()
  await page.waitForTimeout(1200)
  const row = page.locator('[role="button"]').filter({ hasText: 'Release checklist review' }).first()
  await row.click()
  await page.waitForTimeout(2000)
  console.log('resumed=', resumed, 'url=', page.url())

  await page.waitForSelector('[aria-label="Chat messages"]', { timeout: 20_000 })
  await page.waitForTimeout(800)
  await page.screenshot({ path: `${OUT}/${PREFIX}-1-idle.png` })

  await page.getByRole('button', { name: 'Open pinned messages' }).click()
  await page.waitForSelector('[data-testid="pin-entry"]', { timeout: 10_000 })
  await page.waitForTimeout(600)
  await page.locator('[data-testid="pin-entry"]').first().click({ timeout: 15_000 })

  const indicator = page.locator('[data-testid="older-messages-loading"]')
  await indicator.waitFor({ state: 'visible', timeout: 10_000 })
  await page.waitForTimeout(400)

  const role = await indicator.getAttribute('role')
  const label = await indicator.getAttribute('aria-label')
  const { anchor, position } = await indicator.evaluate(el => {
    const s = getComputedStyle(el)
    return { anchor: s.overflowAnchor, position: s.position }
  })
  console.log('older-page request:', JSON.stringify(reqLog))
  console.log(`indicator: role=${role} aria-label=${label} overflow-anchor=${anchor} position=${position} olderRequested=${olderRequested}`)
  if (role !== 'status') throw new Error(`expected role=status, got ${role}`)
  if (!label) throw new Error('indicator has no accessible name')
  if (anchor !== 'none') throw new Error(`expected overflow-anchor=none, got ${anchor}`)
  // Pinned, not parked at the list top -- an unpinned indicator is off-screen
  // for the pins-panel trigger, which is the only thing that fetches today.
  if (position !== 'sticky') throw new Error(`expected position=sticky, got ${position}`)
  if (!olderRequested) throw new Error('indicator visible but no older-page request was made')

  await page.screenshot({ path: `${OUT}/${PREFIX}-2-loading.png` })
  await indicator.screenshot({ path: `${OUT}/${PREFIX}-3-indicator.png` })

  await browser.close()
  srv.close()
  console.log(`wrote ${OUT}/${PREFIX}-{1-idle,2-loading,3-indicator}.png`)
}

main().catch(err => { console.error(err); process.exit(1) })
