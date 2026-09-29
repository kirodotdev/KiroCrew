/**
 * Recording harness for the chat footer's running indicator (#13779 follow-up).
 *
 * Records the REAL built SPA: the ghost carousel alone while a turn runs, then the
 * pose images failing to load, when the visually hidden "Thinking…" label takes
 * the ghosts' place. The failure is forced by pointing the pose <img>s at a
 * missing URL, which fires the same native error event a real paint failure does.
 *
 * Nothing in CI runs this file; it is a manual capture for PR evidence. The
 * CI-enforced half lives in src/test/ChatFooter.test.tsx.
 *
 * Usage: node scripts/capture-footer-loader-label.mjs [outDir]
 * Output: <outDir>/footer-loader.webm (convert to GIF with ffmpeg for the PR body)
 */
import { chromium } from 'playwright'
import { mkdirSync, renameSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/footer-loader-label'
const SLOT = 'chat-loader'

mkdirSync(OUT, { recursive: true })

const slots = [{
  key: SLOT,
  title: 'Why does the footer show twice?',
  running: true,
  last_message: 'Reading ChatFooter.tsx…',
  messages: 1,
  agent: 'kirocrew',
  memory_mode: 'persistent',
  project: '/home/user/workspace/kirocrew',
  folder_id: '',
  modified: Math.floor(Date.now() / 1000),
  source_links: [],
  source_links_total: 0,
}]

const detail = {
  running: true,
  has_more: false,
  total: 1,
  queue: [],
  project: '/home/user/workspace/kirocrew',
  messages: [
    { role: 'user', ts: Date.now() / 1000 - 20, content: 'Why would the loader and the Thinking label show at the same time?' },
  ],
}

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  const context = await browser.newContext({
    viewport: { width: 1000, height: 560 },
    deviceScaleFactor: 2,
    recordVideo: { dir: OUT, size: { width: 1000, height: 560 } },
  })
  const page = await context.newPage()
  logPageProblems(page)
  await stubDashboardApi(page, {
    slots,
    theme: 'dark',
    localStorageEntries: { 'mc-color-theme': 'emerald', 'mc-active-slot': SLOT },
    extra: async (path, route) => {
      if (path.startsWith('/api/chat/slots/')) {
        await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(detail) })
        return true
      }
      return false
    },
  })

  try {
    await page.goto(`${base}/`, { waitUntil: 'domcontentloaded' })
    const footer = page.getByTestId('chat-footer')
    await footer.waitFor({ state: 'visible', timeout: 20_000 })
    // Caption so a viewer knows which case each half of the clip shows.
    const caption = text => page.evaluate(t => {
      let el = document.getElementById('capture-caption')
      if (!el) {
        el = document.createElement('div')
        el.id = 'capture-caption'
        el.style.cssText = 'position:fixed;top:12px;right:12px;z-index:99999;padding:6px 10px;border-radius:6px;font:13px system-ui;background:#2b2d31;color:#f2f2f2;border:1px solid #444'
        document.body.appendChild(el)
      }
      el.textContent = t
    }, text)
    await caption('Normal case: ghosts only (screen readers still hear "Thinking…")')
    await page.waitForTimeout(6000)
    await caption('Pose images fail to load: "Thinking…" takes their place')
    await page.evaluate(() => {
      document.querySelectorAll('.csb4 img').forEach(img => { img.src = '/missing-pose.svg' })
    })
    await footer.getByText('Thinking…', { exact: true }).waitFor({ state: 'visible', timeout: 5000 })
    await page.waitForTimeout(4000)
    console.log(`visible label after failure: ${await footer.getByText('Thinking…', { exact: true }).isVisible()}`)
  } finally {
    const video = page.video()
    await context.close()
    if (video) renameSync(await video.path(), `${OUT}/footer-loader.webm`)
    await browser.close()
    srv.close()
  }
}

main()
