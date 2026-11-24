// Shared helpers for the iteration 3 browser checks: open the real /xr route, read the scene's probe, drive it.
import { createRequire } from 'node:module'

const require = createRequire(import.meta.url)
export const { chromium } = require(process.env.XR_PLAYWRIGHT ?? 'playwright')

export const BASE = process.env.XR_BASE ?? 'http://localhost:6777'
export const MEDIA = (process.env.XR_UI ?? '..') + '/iterations/iter-03/media'
// The sign-in link of the THROWAWAY dev gateway (its own data home under scratch). Never the live gateway's.
const SIGNIN_FILE = process.env.XR_SIGNIN_FILE ?? (process.env.XR_SCRATCH ?? '.') + '/devtoken.txt'

async function signIn(page) {
  const { readFileSync } = await import('node:fs')
  const url = readFileSync(SIGNIN_FILE, 'utf8').split('\n').find(l => l.startsWith('http'))
  if (!url) throw new Error('no sign-in link in ' + SIGNIN_FILE)
  await page.goto(url.trim(), { waitUntil: 'domcontentloaded' })
  await page.waitForTimeout(1500)
}

// The signed-in browser state of the throwaway gateway, kept in scratch so the link is used once.
const STATE_FILE = process.env.XR_STATE_FILE ?? (process.env.XR_SCRATCH ?? '.') + '/devstate.json'

async function signedOut(page) {
  await page.goto(`${BASE}/chat`, { waitUntil: 'domcontentloaded' })
  await page.waitForTimeout(2500)
  return page.evaluate(() => /sign in required|session expired/i.test(document.body.innerText))
}

export async function launch(opts = {}) {
  const { existsSync } = await import('node:fs')
  const browser = await chromium.launch({ args: ['--ignore-gpu-blocklist', '--enable-unsafe-swiftshader', '--use-angle=swiftshader'] })
  const context = await browser.newContext({
    viewport: { width: 1440, height: 900 }, deviceScaleFactor: 1,
    ...(existsSync(STATE_FILE) ? { storageState: STATE_FILE } : {}), ...opts,
  })
  await (await import('./pages-show.mjs')).showPages(context)
  const page = await context.newPage()
  const errors = []
  page.on('console', m => { if (m.type() === 'error') errors.push(m.text()) })
  page.on('pageerror', e => errors.push('pageerror: ' + e.message))
  if (await signedOut(page)) {
    await signIn(page)
    if (await signedOut(page)) throw new Error('sign-in link is used up: mint a new one for the throwaway gateway')
    await context.storageState({ path: STATE_FILE })
  }
  errors.length = 0
  return { browser, context, page, errors }
}

let firstRunWait = 9000

export async function openXr(page, query = 'fixture=1') {
  // XR_EXTRA adds to every page the run opens: `voice=1` runs the parked spoken path.
  if (process.env.XR_EXTRA) query = `${query}&${process.env.XR_EXTRA}`
  await page.goto(`${BASE}/xr?${/(^|&)view=/.test(query) ? query : `view=land&${query}`}`, { waitUntil: 'domcontentloaded', timeout: 120000 })
  await page.waitForSelector('[data-testid="xr-canvas"]', { timeout: 120000 })
  // A fresh data home opens the first-run import dialog over the page, after a scan. Skip it, importing nothing.
  const skip = page.getByText('Skip all').first()
  if (await skip.waitFor({ state: 'visible', timeout: firstRunWait }).then(() => true, () => false)) {
    await skip.click({ timeout: 2500 }).catch(() => {})
    await skip.waitFor({ state: 'hidden', timeout: 5000 }).catch(() => {})
  }
  firstRunWait = 1200
  await page.waitForFunction(() => {
    const p = document.querySelector('[data-testid="xr-host"]')?.dataset.xrProbe
    return p && JSON.parse(p).bots.length > 0 && JSON.parse(p).bots.every(b => b.ready)
  }, null, { timeout: 120000 })
}

export const probe = page => page.evaluate(() => JSON.parse(document.querySelector('[data-testid="xr-host"]').dataset.xrProbe))

/**
 * Wait until the probe satisfies a predicate given as an expression over `p`.
 * The expression runs here in Node: the dashboard's CSP forbids eval in the page, and stays that way.
 */
export async function until(page, body, timeout = 8000) {
  const test = new Function('p', `return (${body})`)
  const end = Date.now() + timeout
  for (;;) {
    const p = await probe(page).catch(() => null)
    let hit = false
    try { hit = !!(p && test(p)) } catch { hit = false }
    if (hit) return p
    if (Date.now() > end) throw new Error(`probe never satisfied: ${body}`)
    await page.waitForTimeout(70)
  }
}

export async function at(page, id) {
  await until(page, `!!p.targets[${JSON.stringify(id)}]`)
  const p = await probe(page)
  const [x, y] = p.targets[id]
  // A target outside the scene's canvas would be a click on the dashboard's own chrome: refuse it.
  const r = await page.evaluate(() => { const b = document.querySelector('[data-testid="xr-canvas"]').getBoundingClientRect(); return [b.left, b.top, b.right, b.bottom] })
  if (x < r[0] + 2 || y < r[1] + 2 || x > r[2] - 2 || y > r[3] - 2) throw new Error(`target ${id} is outside the view at ${x},${y}`)
  return [x, y]
}

export async function hover(page, id) {
  const [x, y] = await at(page, id)
  await page.mouse.move(x, y, { steps: 6 })
}

/** A pinch: press and release on the same target. `hold` is how long the press lasts. */
export async function pinch(page, id, hold = 60) {
  await hover(page, id)
  await page.waitForTimeout(120)
  const [x, y] = await at(page, id)
  await page.mouse.move(x, y)
  await page.mouse.down()
  await page.waitForTimeout(hold)
  await page.mouse.up()
}

/** Open a Bot's chat as a person does: one pinch addresses the Bot, a second pinch opens its chat. */
export async function openChat(page, slug) {
  const s = JSON.stringify(slug)
  await pinch(page, `bot:${slug}`)
  await until(page, `p.addressed === ${s}`)
  await page.waitForTimeout(150)
  await pinch(page, `bot:${slug}`)
  return until(page, `p.panels.some(q => q.slug === ${s} && q.kind === 'chat')`)
}

export async function drag(page, id, to, steps = 24) {
  const [x, y] = await at(page, id)
  await page.mouse.move(x, y)
  await page.waitForTimeout(150)
  await page.mouse.down()
  await page.mouse.move(to[0], to[1], { steps })
  await page.waitForTimeout(150)
  await page.mouse.up()
}

export const key = async (page, k) => { await page.focus('[data-testid="xr-canvas"]'); await page.keyboard.press(k) }
