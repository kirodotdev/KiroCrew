/**
 * Screenshot harness for the pinned-prompt banner's collapsed shape.
 *
 * Runs the REAL built SPA (website/dist) against a static file server with every
 * /api/** call answered from fixtures — no gateway, no token, no agent. Only the
 * network is stubbed, so the banner, its clamp measurement, its thumbnails and the
 * scroll-driven push geometry are the unmodified production path.
 *
 * The transcript is seeded with the two prompt shapes this change is about:
 *   - a long multi-paragraph prompt, which the collapsed card clamps
 *   - a prompt whose entire content is an image, which used to pin as a BLANK card
 *     because `promptPreview` strips image markdown
 *
 * Scroll position is driven by scrolling the transcript element directly rather
 * than by wheel events: the pin is recomputed from `getBoundingClientRect` on a
 * rAF, so a deterministic `scrollTop` gives a deterministic banner state, where a
 * wheel gesture's momentum does not.
 *
 * Usage: node scripts/capture-pinned-prompt.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'

const OUT = process.argv[2] || '../temp-screenshots/pinned-prompt'
const SLOT = 'chat-pinned'
const PROJECT = '/home/user/workspace/KiroCrew'

mkdirSync(OUT, { recursive: true })

/** 160x100 PNG shaped like a screenshot (title bar + text bands), so a
 *  thumbnail reads as a real image rather than a flat swatch. */
const PNG_B64 =
  'iVBORw0KGgoAAAANSUhEUgAAAKAAAABkCAIAAACO1KzYAAAA4UlEQVR42u3YQQ2AMBBE0cog6Q0dPaOkUioBFVWwUpCDCDhs2pd8BfNuU9rVtXDFBIAFWIAFWIAFWIABC7AAC7AA6xPwUU8tHGDAAizAAizAAizAgAVYgJUfeMajvwIMGDBgwIABAwYMWIAFGLAnS4AFGLAAC7AAC7AAC7AAbw887tgtwIABAwYMGDBgwIABAwYMGLAnS4AFWIAFWIABC7AAC7AAyxftxwYMGDBgAQYMGDBgwIABAwbsyXJVCrAAC7AAC7AAAxZgARZgARZgAQYswAIswAIswAIswIAFWLl7Ac5VlVwTVUKoAAAAAElFTkSuQmCC'

const LONG_PROMPT = [
  'Clean up leftover local infrastructure from a finished profiling task in the',
  'KiroCrew workspace: stop the demo server on :8931, stop the Vite dev server on',
  ':3000, and remove the worktrees whose PRs already merged. Leave anything that',
  'still holds uncommitted work, and do not touch the primary checkout — it is 773',
  'commits behind main and I want it that way for now.',
].join(' ')

const slots = [{
  key: SLOT,
  title: 'Clean up leftover local infrastructure',
  running: false,
  last_message: 'Two complaints now, and they share a cause in the preview helper.',
  messages: 6,
  agent: 'kirocrew',
  memory_mode: 'persistent',
  project: PROJECT,
  modified: Math.floor(Date.now() / 1000),
  source_links: [],
  source_links_total: 0,
}]

const paraOnce = (n) => [
  `Paragraph ${n}. Filler with enough length to give the transcript runway, so the`,
  'incoming prompt can actually reach the fold and the push-out can complete —',
  'without it the scroller saturates and the hand-off is unreachable. The banner is',
  'recomputed from getBoundingClientRect on every animation frame, so the state it',
  'lands in is a pure function of scrollTop, which is what makes this harness',
  'deterministic rather than dependent on gesture momentum. A few hundred more',
  'characters here buy several hundred pixels of scroll range, and the hand-off',
  'needs the incoming prompt to travel a full band height past the fold before it',
  'takes the pin itself.',
].join(' ')

/** Four paragraphs per message — the image prompt is several messages down, and
 *  the sweep has to have enough scroll range to actually arrive at it. */
const para = (n) => [paraOnce(n), paraOnce(n + 100), paraOnce(n + 200), paraOnce(n + 300)].join('\n\n')

const t0 = Date.now() / 1000 - 900
const detail = {
  running: false,
  messages: [
    { role: 'assistant', ts: t0, content: para(1) },
    { role: 'user', ts: t0 + 10, content: LONG_PROMPT },
    { role: 'assistant', ts: t0 + 20, content: 'No problem — stopping here, nothing was removed.' },
    { role: 'assistant', ts: t0 + 21, content: para(2) },
    { role: 'assistant', ts: t0 + 22, content: para(3) },
    // The image-only prompt: no text at all once promptPreview has run.
    { role: 'user', ts: t0 + 30, content: '![screenshot](/tmp/pinned-shot.png)' },
    { role: 'assistant', ts: t0 + 40, content: 'That screenshot is the bug.' },
    { role: 'assistant', ts: t0 + 41, content: para(4) },
    { role: 'assistant', ts: t0 + 42, content: para(5) },
    { role: 'assistant', ts: t0 + 43, content: para(6) },
  ],
}

const json = (route, body, status = 200) => route.fulfill({
  status, contentType: 'application/json', body: JSON.stringify(body),
})

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  const context = await browser.newContext({
    viewport: { width: 1280, height: 860 },
    // The card is 14px type; a 1x shot renders it soft on GitHub.
    deviceScaleFactor: 2,
  })
  const page = await context.newPage()

  await page.routeWebSocket(/\/api\/ws/, () => {})

  // ONE handler for every /api route. The image endpoint is handled INSIDE it
  // rather than as its own `page.route`: Playwright resolves the most RECENTLY
  // registered matching route first, so a later '**/api/**' catch-all silently
  // swallows an earlier '**/api/file-raw**' and every thumbnail renders as the
  // browser's broken-image glyph.
  await page.route('**/api/**', async route => {
    const path = new URL(route.request().url()).pathname
    // Thumbnails resolve through the gateway file endpoint (`pinnedImageUrl`), so
    // answering it is what proves the URL mapping, not just the markup.
    if (path === '/api/file-raw') {
      return route.fulfill({
        status: 200, contentType: 'image/png', body: Buffer.from(PNG_B64, 'base64'),
      })
    }
    if (path === '/api/chat/slots') return json(route, slots)
    if (path.startsWith('/api/chat/slots/')) return json(route, detail)
    // The prerequisite gate blocks the whole app shell until this resolves, and it
    // reads `operation.status` unguarded — a catch-all `[]` throws inside the
    // ErrorBoundary and the transcript never mounts at all.
    if (path === '/api/kiro-prerequisite') {
      return json(route, { ready: true, setup_allowed: true, operation: { status: 'idle', message: '' } })
    }
    if (path.startsWith('/api/instances')) return json(route, { instances: [], active: '' })
    if (path === '/api/status') return json(route, { sessions: 1, crons: 0, lessons: 0, uptime: 120, version: 'dev' })
    if (path === '/api/notifications') return json(route, { notifications: [], unread: 0 })
    if (path === '/api/auth/me') return json(route, { user: 'owner', app: '' })
    if (path === '/api/models') return json(route, { models: [], default: 'auto' })
    if (path === '/api/themes') return json(route, { themes: [], installed: [] })
    if (path === '/api/theme/boot') return json(route, { mode: 'dark', theme: '' })
    if (path === '/api/dashboard/branding') return json(route, { bot_name: 'Kiro', avatar: '' })
    if (path === '/api/recent-projects') return json(route, { dirs: [PROJECT] })
    if (path === '/api/chat/nav/resolve-links') return json(route, { summaries: [] })
    if (/(config|tips|voice|autonudge|branding|status|usage-summary)/.test(path)) return json(route, {})
    return json(route, [])
  })

  page.on('pageerror', err => console.log('PAGEERROR:', String(err).slice(0, 300)))

  await page.addInitScript(() => {
    localStorage.clear()
    localStorage.setItem('mc-theme', 'dark')
    localStorage.setItem('mc-onboarded', '1')
    localStorage.setItem('mc-active-slot', 'chat-pinned')
  })
  await page.goto(base + '/', { waitUntil: 'domcontentloaded' })
  await page.waitForTimeout(3000)
  console.log('DIAG', await page.evaluate(() => JSON.stringify({
    rows: document.querySelectorAll('[data-display-index]').length,
    body: document.body.innerText.slice(0, 140),
    scrollables: [...document.querySelectorAll('*')]
      .filter(e => e.scrollHeight > e.clientHeight + 20 && /auto|scroll/.test(getComputedStyle(e).overflowY))
      .map(e => ({ c: (e.className || '').toString().slice(0, 50), sh: e.scrollHeight, ch: e.clientHeight })).slice(0, 5),
  })))

  /** Drive the transcript to an exact offset and let the rAF pin recompute land. */
  const SCROLLER = '.chat-container'

  async function scrollTo(top) {
    await page.evaluate(async (t) => {
      const sc = document.querySelector('.chat-container')
      if (sc) sc.scrollTop = t
      await new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)))
    }, top)
    await page.waitForTimeout(350)
  }

  /** Max offset, re-read each time: the virtualizer grows it as rows mount. */
  const maxTop = () => page.evaluate(() => {
    const sc = document.querySelector('.chat-container')
    return sc ? sc.scrollHeight - sc.clientHeight : 0
  })

  /**
   * Sweep downward in small steps, re-reading the max each time (mounting rows
   * extends it), and stop at the first offset whose banner satisfies `want`.
   */
  async function sweepUntil(want, label) {
    let top = 0
    for (let i = 0; i < 90; i++) {
      const max = await maxTop()
      if (top > max) break
      await scrollTo(top)
      const s = await state()
      if (s.pinned) console.log(label, 'top', top, 'of', max, JSON.stringify(s))
      if (want(s)) return s
      top += 90
    }
    return null
  }

  async function shot(name) {
    await page.screenshot({ path: `${OUT}/${name}.png` })
    console.log('wrote', `${OUT}/${name}.png`)
  }

  /** Crop to the banner plus a little transcript, which is the whole story. */
  async function band(name) {
    const card = page.locator('[data-testid="pinned-prompt"]')
    if (await card.count()) {
      const box = await card.first().boundingBox()
      if (box) {
        await page.screenshot({
          path: `${OUT}/${name}.png`,
          clip: {
            x: Math.max(0, box.x - 40), y: Math.max(0, box.y - 70),
            width: Math.min(1280 - Math.max(0, box.x - 40), box.width + 80),
            height: box.height + 200,
          },
        })
        console.log('wrote', `${OUT}/${name}.png`)
        return
      }
    }
    console.log('NOTE: no pinned card mounted for', name)
    await shot(name)
  }

  const state = () => page.evaluate(() => {
    const c = document.querySelector('[data-testid="pinned-prompt"]')
    if (!c) return { pinned: false }
    const p = c.querySelector('p')
    return {
      pinned: true,
      cardH: +c.getBoundingClientRect().height.toFixed(2),
      lines: p ? Math.round(p.getBoundingClientRect().height / 22.75) : 0,
      clampedAway: p ? p.scrollHeight > p.clientHeight + 1 : false,
      thumbs: c.querySelectorAll('img').length,
      chevron: !!c.querySelector('button[aria-expanded]'),
    }
  })

  // 1. The long prompt pinned and CLAMPED — the multi-line collapsed card.
  const clamp = await sweepUntil(s => s.pinned && s.clampedAway && s.thumbs === 0, 'clamp')
  if (clamp) {
    console.log('clamped card:', JSON.stringify(clamp))
    await band('01-clamped-multi-line')
    await shot('02-clamped-full')
    const chev = page.locator('[data-testid="pinned-prompt"] button[aria-expanded]')
    if (await chev.count()) {
      await chev.first().click()
      await page.waitForTimeout(450)
      await band('03-expanded')
      await chev.first().click()
      await page.waitForTimeout(450)
    }
  } else {
    console.log('NOTE: never reached a clamped pinned card')
  }

  // 2. The image-only prompt pinned — used to be a completely blank card.
  const img = await sweepUntil(s => s.pinned && s.thumbs > 0, 'image')
  if (img) {
    console.log('image card:', JSON.stringify(img))
    await band('04-image-only-prompt')
    await shot('05-image-only-full')
    // Expanding an image-only prompt is only reachable because images earn the
    // chevron on their own (an empty text never clamps) — capture the strip it
    // opens, since that affordance is the point of the fix.
    const ichev = page.locator('[data-testid="pinned-prompt"] button[aria-expanded]')
    if (await ichev.count()) {
      await ichev.first().click()
      await page.waitForTimeout(500)
      console.log('image expanded:', JSON.stringify(await state()))
      await band('06-image-only-expanded')
    } else {
      console.log('NOTE: image-only card has no chevron — the expand affordance regressed')
    }
  } else {
    console.log('NOTE: never reached a pinned image prompt')
  }

  await browser.close()
  srv.close()
}

main().catch(err => { console.error(err); process.exit(1) })
