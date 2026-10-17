/**
 * Recording harness: collapsing a list-view folder from its PINNED header.
 *
 * The fix is about motion, so stills of the before and after states do not
 * show it. This records the collapse as a video and writes a filmstrip of
 * every compositor frame from just before the collapse commits until the
 * folder has closed, cropped to the top of the sessions lane and labelled with
 * its time from that commit. A frame showing the folder's FIRST sessions under the header means the
 * rows the person was looking at swapped for others before folding away.
 *
 * Runs the REAL built SPA on the scene `lib/folder-collapse-scene.mjs` fixes
 * (no gateway): two folders of 40 sessions each; the lane is scrolled 400px
 * into "alpha folder" so its header is pinned and its first sessions are above
 * the lane.
 *
 * Usage: node scripts/capture-sticky-folder-collapse.mjs <baseUrl> <outDir> <label> [reduce]
 * Writes <outDir>/<label>.webm and <outDir>/<label>-filmstrip.png, and prints
 * the header's top before and after the collapse. `reduce` runs the page with
 * prefers-reduced-motion, where the folder body closes with no transition.
 */
import { chromium } from 'playwright'
import { mkdirSync, readdirSync, renameSync, rmSync } from 'node:fs'
import { join } from 'node:path'
import { routeFolderCollapseScene } from './lib/folder-collapse-scene.mjs'

const BASE = process.argv[2] || 'http://127.0.0.1:7824'
const OUT = process.argv[3] || '../temp-screenshots/17529-sticky-folder-collapse'
const LABEL = process.argv[4] || 'fix'
const REDUCE = process.argv[5] === 'reduce'
mkdirSync(OUT, { recursive: true })

const browser = await chromium.launch({ env: { ...process.env, LD_LIBRARY_PATH: '/usr/lib64' } })
try {
  const videoDir = join(OUT, `.video-${LABEL}`)
  const context = await browser.newContext({
    viewport: { width: 1400, height: 900 },
    reducedMotion: REDUCE ? 'reduce' : 'no-preference',
    recordVideo: { dir: videoDir, size: { width: 1400, height: 900 } },
  })
  const page = await context.newPage()
  await routeFolderCollapseScene(page)
  page.on('pageerror', err => console.log('PAGEERROR:', String(err).slice(0, 240)))
  await page.addInitScript(() => {
    localStorage.setItem('mc-onboarded', '1')
    localStorage.setItem('kc-onboarded', '1')
  })
  await page.goto(`${BASE}/chat`, { waitUntil: 'domcontentloaded' })
  const lane = page.getByTestId('tree-view-lane')
  await lane.waitFor({ timeout: 20_000 })
  await page.getByText('beta session 39').first().waitFor({ state: 'attached', timeout: 20_000 })
  await lane.evaluate(el => { el.scrollTop = 400 })
  await page.waitForTimeout(1200)
  const headerTop = () => page.locator('[data-folder-drop="f-a"] > [data-folder-row]').evaluate(el => el.getBoundingClientRect().top)
  const headerBefore = await headerTop()

  // Every compositor frame from just before the click until the collapse has
  // settled, straight from the screencast: these are the pixels a person sees,
  // which is the only honest evidence here (the DOM already reports the rows
  // hidden on frames where they were still painted).
  const cdp = await context.newCDPSession(page)
  const frames = []
  let recording = false
  cdp.on('Page.screencastFrame', async f => {
    if (recording) frames.push({ ms: f.metadata.timestamp * 1000, data: f.data })
    await cdp.send('Page.screencastFrameAck', { sessionId: f.sessionId }).catch(() => {})
  })
  await cdp.send('Page.startScreencast', { format: 'png', everyNthFrame: 1 })
  await page.waitForTimeout(300)
  recording = true
  await page.waitForTimeout(150)
  // The collapse commits a few hundred ms after the click in headless Chromium;
  // anchor the filmstrip on the commit (the header's aria-expanded flipping),
  // not on the click, so the frames cover the close itself.
  await page.evaluate(() => {
    const btn = document.querySelector('[data-folder-drop="f-a"] > [data-folder-row] [aria-expanded]')
    new MutationObserver((_, obs) => {
      if (btn.getAttribute('aria-expanded') === 'false') { window.__committedAt = Date.now(); obs.disconnect() }
    }).observe(btn, { attributes: true })
  })
  await page.getByRole('button', { name: /collapse folder alpha folder/i }).click()
  await page.waitForFunction(() => window.__committedAt, null, { timeout: 10_000 })
  await page.waitForTimeout(800)
  const headerAfter = await headerTop()
  const committedAt = await page.evaluate(() => window.__committedAt)
  recording = false
  await cdp.send('Page.stopScreencast')
  const box = await lane.boundingBox()
  await context.close()
  const [webm] = readdirSync(videoDir).filter(n => n.endsWith('.webm'))
  renameSync(join(videoDir, webm), join(OUT, `${LABEL}.webm`))
  rmSync(videoDir, { recursive: true, force: true })

  // Filmstrip: every frame from just before the commit until the close has
  // settled, the top of the sessions lane only, labelled with its time from
  // the commit.
  const tiles = frames.filter(f => f.ms >= committedAt - 120 && f.ms <= committedAt + 450)
  const cell = { x: Math.round(box.x), y: Math.round(box.y), w: Math.round(box.width), h: 200 }
  const strip = await browser.newPage({ viewport: { width: 1600, height: 400 } })
  await strip.setContent(`<body style="margin:0;background:#555;display:flex;flex-wrap:wrap;gap:4px;width:${(cell.w + 4) * 8}px">${tiles.map(t => `
    <div style="width:${cell.w}px;height:${cell.h + 18}px;overflow:hidden;background:#000;font:12px monospace;color:#ff0">
      <div style="height:18px;padding-left:4px">${Math.round(t.ms - committedAt)} ms</div>
      <div style="width:${cell.w}px;height:${cell.h}px;overflow:hidden;position:relative">
        <img src="data:image/png;base64,${t.data}" style="position:absolute;left:${-cell.x}px;top:${-cell.y}px">
      </div></div>`).join('')}</body>`)
  await strip.locator('body').screenshot({ path: join(OUT, `${LABEL}-filmstrip.png`) })
  await strip.close()
  console.log(`${LABEL}: header top ${headerBefore} -> ${headerAfter}${REDUCE ? ' (reduced motion)' : ''}`)
  console.log(`${LABEL}: ${tiles.length} frames -> ${join(OUT, `${LABEL}-filmstrip.png`)}, video ${join(OUT, `${LABEL}.webm`)}`)
} finally {
  await browser.close()
}
