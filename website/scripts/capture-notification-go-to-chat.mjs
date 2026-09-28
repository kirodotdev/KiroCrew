/**
 * Screenshot harness for the notification detail panel's cron-with-slot jump.
 *
 * Runs the REAL built SPA (website/dist) gateway-free (stubDashboardApi): a
 * cron notification carrying both `job_id` and `slot` is seeded, the inbox is
 * opened and the note selected, and the detail panel's jump button is framed.
 *
 * The cron-with-slot branch and the directSlot branch of the panel perform the
 * identical action (switchSlot + navigate) and share `go_to_chat`, so the frame
 * must show exactly ONE "Go to Chat" button and no other label for it.
 *
 * Frames:
 *   01-cron-go-to-chat        detail panel, dark theme
 *   02-cron-go-to-chat-light  light-theme parity
 *
 * Usage: node scripts/capture-notification-go-to-chat.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/resume-vs-continue'
mkdirSync(OUT, { recursive: true })

const SLOT = { key: 'cron-job-1234abcd', name: 'cron-job-1234abcd', title: 'Nightly backup', messages: [], running: false }
const NOTE = {
  kind: 'cron',
  source: 'system',
  channel: 'system.cron',
  priority: 'default',
  title: 'Nightly backup finished',
  body: 'All good — 3 archives rotated.',
  ts: '2026-09-10T11:00:00.000000+00:00',
  acked: false,
  job_id: 'job-1234abcd',
  slot: SLOT.key,
}

const { srv, base } = await serveDist()
const browser = await chromium.launch()

async function frame(theme, name) {
  const page = await browser.newPage({ viewport: { width: 1280, height: 800 }, colorScheme: theme })
  logPageProblems(page)
  // `theme` MUST go through the stub: it answers /api/theme/boot with that mode
  // and seeds `mc-theme` inside its own init script. A separate addInitScript
  // would race the stub's localStorage.clear() (Playwright does not order init
  // scripts) and the frame could carry the wrong theme.
  await stubDashboardApi(page, {
    theme,
    slots: [SLOT],
    extra: async (path, route) => {
      if (path === '/api/notifications') {
        await json(route, { notifications: [NOTE], unread: 1 })
        return true
      }
      if (path === '/api/chat/slots' && route.request().method() === 'POST') {
        await json(route, { key: 'chat-1', name: 'chat-1', title: 'New Session…', messages: [], running: false })
        return true
      }
      return false
    },
  })
  await page.goto(base + '/notifications')
  // The mode arrives from /api/theme/boot and is applied asynchronously, so
  // WAIT for it: the frame must carry the theme it is named for.
  await page.waitForFunction(
    t => (document.documentElement.dataset.theme || '').includes(t),
    theme,
    { timeout: 15000 },
  ).catch(async () => {
    const applied = await page.evaluate(() => document.documentElement.dataset.theme || '')
    throw new Error(`expected data-theme to include "${theme}", got "${applied}"`)
  })
  await page.getByText(NOTE.title).first().click()

  const goToChat = page.getByRole('button', { name: /^Go to Chat$/ })
  await goToChat.first().waitFor({ state: 'visible', timeout: 15000 })
  // The whole point: one label for one action, and the two branches did not
  // both render (dedup) — exactly one button, and the retired copy is gone.
  const count = await goToChat.count()
  if (count !== 1) throw new Error(`expected exactly 1 "Go to Chat" button, found ${count}`)
  if (await page.getByRole('button', { name: /continue session/i }).count()) {
    throw new Error('"Continue session" still rendered — frame would show the old copy')
  }
  await page.screenshot({ path: `${OUT}/${name}.png` })
  console.log(`${name}: one "Go to Chat" button, no "Continue session"`)
  await page.close()
}

await frame('dark', '01-cron-go-to-chat')
await frame('light', '02-cron-go-to-chat-light')

await browser.close()
srv.close()
console.log('OK — frames written to', OUT)
