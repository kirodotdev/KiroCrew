/**
 * Screenshot + recording harness for the pinned-prompt banner's three heights:
 * at REST (one line), PEEKED (pointer over the card, three lines) and EXPANDED
 * (chevron, the whole prompt).
 *
 * Same setup as capture-pinned-prompt.mjs: the REAL built SPA (website/dist) on a
 * loopback static server with every /api/** call answered from fixtures — no
 * gateway, no token, no agent. Only the network is stubbed, so the clamp, the
 * hover/focus peek, the height morph and the scroll-driven pin geometry are the
 * unmodified production path.
 *
 * Two outputs, because the change is a TRANSITION and a still cannot prove one:
 *   - three cropped stills of the band, one per height, in light and dark
 *   - a short video of the pointer entering and leaving the card, which is the
 *     evidence the #8714 revert asked for ("attach a recording, since
 *     screenshots cannot carry continuity")
 *
 * Usage: node scripts/capture-pinned-prompt-peek.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync, readdirSync, renameSync } from 'node:fs'
import { join } from 'node:path'
import { serveDist } from './lib/serve-dist.mjs'
import { installApiFixtures, json, logPageFailures } from './lib/api-fixtures.mjs'

const OUT = process.argv[2] || '../temp-screenshots/pinned-prompt-peek'
const SLOT = 'chat-pinned'
const PROJECT = '/home/user/workspace/KiroCrew'

mkdirSync(OUT, { recursive: true })

const LONG_PROMPT = [
  'Clean up leftover local infrastructure from a finished profiling task in the',
  'Kiro Crew workspace: stop the demo server on :8931, stop the Vite dev server on',
  ':3000, and remove the worktrees whose PRs already merged. Leave anything that',
  'still holds uncommitted work, and do not touch the primary checkout — it is 773',
  'commits behind main and I want it that way for now. When you are done, list',
  'what you removed and what you left, with the reason for each item you kept.',
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
    { role: 'assistant', ts: t0 + 23, content: para(4) },
  ],
}

/** `/api/theme/boot` wins over the `mc-theme` localStorage seed (useTheme
 *  applies `bootData.mode` on arrival), so the stub must serve the theme too.
 *  The boot-path table comes from the shared fixture router; only the routes
 *  this harness cares about are overridden, and the slot-detail prefix (an id
 *  segment the exact-path table cannot express) is registered AFTER it, since
 *  Playwright resolves the most recently registered matching route first. */
async function routeApi(page, theme) {
  await installApiFixtures(page, {
    '/api/chat/slots': slots,
    '/api/theme/boot': { mode: theme, theme: '' },
    '/api/recent-projects': { dirs: [PROJECT] },
    '/api/models': { models: [], default: 'auto' },
    '/api/chat/nav/resolve-links': { summaries: [] },
  })
  await page.route('**/api/chat/slots/**', route => json(route, detail))
  logPageFailures(page)
}

const CARD = '[data-testid="pinned-prompt"]'

async function openSession(context, theme) {
  const page = await context.newPage()
  await routeApi(page, theme)
  await page.addInitScript((t) => {
    localStorage.clear()
    localStorage.setItem('mc-theme', t)
    localStorage.setItem('mc-onboarded', '1')
    localStorage.setItem('mc-active-slot', 'chat-pinned')
  }, theme)
  return page
}

async function scrollTo(page, top) {
  await page.evaluate(async (t) => {
    const sc = document.querySelector('.chat-container')
    if (sc) sc.scrollTop = t
    await new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)))
  }, top)
  await page.waitForTimeout(350)
}

const maxTop = (page) => page.evaluate(() => {
  const sc = document.querySelector('.chat-container')
  return sc ? sc.scrollHeight - sc.clientHeight : 0
})

const state = (page) => page.evaluate(() => {
  const c = document.querySelector('[data-testid="pinned-prompt"]')
  if (!c) return { pinned: false }
  const p = c.querySelector('p')
  return {
    pinned: true,
    cardH: +c.getBoundingClientRect().height.toFixed(2),
    pH: p ? +p.getBoundingClientRect().height.toFixed(2) : 0,
    clamp: p ? p.style.webkitLineClamp || p.style.WebkitLineClamp || '' : '',
    clampedAway: p ? p.scrollHeight > p.clientHeight + 1 : false,
    chevron: !!c.querySelector('button[aria-expanded]'),
  }
})

/** Sweep down until the long prompt is pinned and clamped, at rest. */
async function pinLongPrompt(page) {
  let top = 0
  for (let i = 0; i < 90; i++) {
    const max = await maxTop(page)
    if (top > max) break
    await scrollTo(page, top)
    const s = await state(page)
    if (s.pinned && s.clampedAway) return s
    top += 90
  }
  return null
}

/** Crop to the band plus a little transcript below it. */
async function band(page, name) {
  const box = await page.locator(CARD).first().boundingBox()
  if (!box) { console.log('NOTE: no pinned card for', name); return }
  await page.screenshot({
    path: `${OUT}/${name}.png`,
    clip: {
      x: Math.max(0, box.x - 40), y: Math.max(0, box.y - 70),
      width: Math.min(1280 - Math.max(0, box.x - 40), box.width + 80),
      height: box.height + 200,
    },
  })
  console.log('wrote', `${OUT}/${name}.png`)
}

/** Park the pointer well away from the card so the next session starts at rest. */
const parkPointer = (page) => page.mouse.move(20, 700)

async function stills(browser, theme) {
  const context = await browser.newContext({ viewport: { width: 1280, height: 860 }, deviceScaleFactor: 2 })
  const page = await openSession(context, theme)
  await page.goto(BASE + '/', { waitUntil: 'domcontentloaded' })
  await page.waitForTimeout(3000)
  await parkPointer(page)
  const rest = await pinLongPrompt(page)
  if (!rest) { console.log('NOTE: never reached a clamped pinned card in', theme); await context.close(); return }
  console.log(theme, 'rest', JSON.stringify(rest))
  await band(page, `${theme}-1-rest`)

  const card = page.locator(CARD).first()
  await card.hover()
  await page.waitForTimeout(600) // the intent delay, the 150ms morph, then settle
  const peeked = await state(page)
  console.log(theme, 'peek', JSON.stringify(peeked))
  await band(page, `${theme}-2-peek-hover`)

  await page.locator(`${CARD} button[aria-expanded]`).first().click()
  await page.waitForTimeout(450)
  console.log(theme, 'expanded', JSON.stringify(await state(page)))
  await band(page, `${theme}-3-expanded`)

  await page.locator(`${CARD} button[aria-expanded]`).first().click()
  await parkPointer(page)
  await page.waitForTimeout(450)
  console.log(theme, 'back at rest', JSON.stringify(await state(page)))
  await context.close()
}

async function recording(browser) {
  const context = await browser.newContext({
    viewport: { width: 1280, height: 860 },
    recordVideo: { dir: OUT, size: { width: 1280, height: 860 } },
  })
  const page = await openSession(context, 'dark')
  await page.goto(BASE + '/', { waitUntil: 'domcontentloaded' })
  await page.waitForTimeout(3000)
  await parkPointer(page)
  const rest = await pinLongPrompt(page)
  if (!rest) { console.log('NOTE: recording: never reached a clamped pinned card'); await context.close(); return }
  const card = page.locator(CARD).first()
  // Hover in, hold, out, hold — twice, so the loop reads as a loop.
  for (let i = 0; i < 2; i++) {
    await page.waitForTimeout(700)
    await card.hover()
    await page.waitForTimeout(1200)
    await parkPointer(page)
  }
  await page.waitForTimeout(700)
  // Keyboard: Tab onto the card's jump region holds the peek open too.
  await page.locator(`${CARD} button`).first().focus()
  await page.waitForTimeout(1200)
  await page.keyboard.press('Tab') // to the chevron — still inside, stays open
  await page.waitForTimeout(900)
  await page.keyboard.press('Tab') // out of the card — closes
  await page.waitForTimeout(900)
  const video = page.video()
  await context.close()
  const src = await video.path()
  const dst = join(OUT, 'peek-transition.webm')
  renameSync(src, dst)
  console.log('wrote', dst)
}

let BASE = ''
async function main() {
  const { srv, base } = await serveDist()
  BASE = base
  const browser = await chromium.launch()
  await stills(browser, 'dark')
  await stills(browser, 'light')
  await recording(browser)
  await browser.close()
  srv.close()
  console.log('files:', readdirSync(OUT).join(', '))
}

main().catch(err => { console.error(err); process.exit(1) })
