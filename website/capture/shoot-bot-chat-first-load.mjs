// Screenshots of a bot chat's first load: with history (the transcript
// skeleton while the first page is held, then the landed chat) and without
// (nothing drawn while held, then the empty chat). A mutation observer set
// before any page script runs records whether the skeleton was EVER in the
// document, so the no-history run proves there was no one-frame flash.
//   node capture/shoot-bot-chat-first-load.mjs [outDir] [port]
import { createServer } from 'vite'
import { chromium } from 'playwright-core'
import { mkdirSync, mkdtempSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

const OUT = process.argv[2] || '/tmp/bot-chat-first-load-shots'
const VIDEO_TMP = mkdtempSync(join(tmpdir(), 'bot-chat-first-load-'))
const PORT = Number(process.argv[3] || 5179)
const CHROMIUM_PATH = process.env.CHROMIUM_PATH
mkdirSync(OUT, { recursive: true })

const server = await createServer({
  configFile: 'vite.config.ts',
  server: { port: PORT, strictPort: true, host: '127.0.0.1' },
})
await server.listen()

const browser = await chromium.launch({ executablePath: CHROMIUM_PATH, args: ['--no-sandbox'] })

const PAGE = [
  { role: 'user', content: 'What changed in the issue queue overnight?', cls: '', ts: '2026-11-23T09:00:00Z' },
  { role: 'assistant', content: 'Three new issues and one fix PR. The MCP one looks like a Crew bug; I am checking it now.', cls: '', ts: '2026-11-23T09:00:05Z' },
]

const results = {}

async function shoot(history, width, prefix) {
  const ctx = await browser.newContext({
    viewport: { width, height: 640 },
    recordVideo: width > 400 ? { dir: VIDEO_TMP, size: { width, height: 640 } } : undefined,
  })
  const page = await ctx.newPage()
  await page.addInitScript(() => {
    window.__skeletonSeenAt = null
    const t0 = performance.now()
    const sel = '[data-testid="chat-pane-skeleton"]'
    // The records' own nodes, so a skeleton that came and went before the
    // callback runs still counts: an added node, or a removed one when it
    // came inside a larger subtree.
    const isSkeleton = (n) => n instanceof Element && (n.matches(sel) || !!n.querySelector(sel))
    const touched = (records) => records.some(r => [...r.addedNodes, ...r.removedNodes].some(isSkeleton))
    new MutationObserver((records) => {
      if (window.__skeletonSeenAt === null && touched(records)) {
        window.__skeletonSeenAt = Math.round(performance.now() - t0)
      }
    }).observe(document, { childList: true, subtree: true })
  })
  let release
  const held = new Promise((r) => { release = r })
  // Gateway routes only: the dev server serves modules under /src/api/ too.
  await page.route((u) => u.pathname.startsWith('/api/'), async (route) => {
    const url = new URL(route.request().url())
    if (url.pathname.startsWith('/api/chat/slots/member-radar-first-load')) {
      await held
      const messages = history === '1' ? PAGE : []
      return route.fulfill({ json: { key: 'member-radar-first-load', messages, running: false, has_more: false, total: messages.length } })
    }
    // An empty list reads as "nothing" to both list and record readers.
    return route.fulfill({ json: [] })
  })
  await page.goto(`http://127.0.0.1:${PORT}/capture/bot-chat-first-load.html?theme=dark&history=${history}`)
  page.on('pageerror', (e) => console.error('pageerror', prefix, e.message))
  await page.waitForSelector('[data-chat-pane]', { state: 'attached', timeout: 60000 })
  await page.waitForTimeout(900)
  await page.screenshot({ path: join(OUT, `${prefix}-1-loading.png`) })
  release()
  if (history === '1') await page.getByText('Three new issues').waitFor({ timeout: 15000 })
  await page.waitForTimeout(600)
  await page.screenshot({ path: join(OUT, `${prefix}-2-loaded.png`) })
  results[prefix] = { history, width, skeletonFirstSeenMs: await page.evaluate(() => window.__skeletonSeenAt) }
  const video = page.video()
  await ctx.close()
  if (video) await video.saveAs(join(OUT, `${prefix}.webm`))
}

await shoot('1', 1280, 'after-has-history-desktop')
await shoot('1', 380, 'after-has-history-380')
await shoot('0', 1280, 'after-no-history-desktop')
await shoot('0', 380, 'after-no-history-380')

writeFileSync(join(OUT, 'skeleton-seen.json'), JSON.stringify(results, null, 2) + '\n')
console.log(JSON.stringify(results, null, 2))
await browser.close()
await server.close()
