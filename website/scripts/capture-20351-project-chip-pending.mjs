/**
 * Screenshot harness for #20351: the project chip while a project pick waits.
 *
 * Runs the REAL built SPA (website/dist) with every /api/** call answered from
 * fixtures. The project route (POST /api/chat/slots/<slot>/project) is HELD,
 * the way the gateway holds it while a turn prepares (the route waits on the
 * slot's turn-preparation lock), and released only after the pending frame.
 *
 * Frames, at a desktop and a narrow width:
 *   01-pending   the pick was taken: spinner on the chip, picker closed
 *   02-success   the route answers: the new project shows, spinner gone
 *   03-failure   a second pick is refused: spinner gone, old project kept,
 *                the existing switch notice explains
 *
 * Usage: node scripts/capture-20351-project-chip-pending.mjs <outDir> [label]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { json, makeFixedApi, handleBootRoute } from './lib/boot-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/20351-project-chip-pending'
const LABEL = process.argv[3] || 'after'
const SLOT = 'chat-project-pending'
const START = '/home/user/workspace/KiroCrew'
const NEXT = '/home/user/workspace/website-docs'
const FAIL = '/home/user/workspace/old-notes'

mkdirSync(OUT, { recursive: true })

async function run(base, width, height, tag) {
  const browser = await chromium.launch()
  const context = await browser.newContext({ viewport: { width, height }, deviceScaleFactor: 2 })
  const page = await context.newPage()
  let project = START
  const held = []
  const slots = () => [{
    key: SLOT, title: 'Tidy the docs folder', running: false, messages: 2, agent: 'kirocrew',
    memory_mode: 'persistent', project, modified: Math.floor(Date.now() / 1000), source_links: [], source_links_total: 0,
  }]
  const detail = () => ({
    running: false, has_more: false, total: 2, queue: [], project,
    messages: [{ role: 'user', ts: Date.now() / 1000 - 20, content: 'Tidy the docs folder.' }, { role: 'assistant', ts: Date.now() / 1000 - 10, content: 'Done. Pick the docs project and I will start there.' }],
  })
  const fixedApi = makeFixedApi(START)
  await page.routeWebSocket(/\/api\/ws/, () => {})
  await page.route('**/api/**', async route => {
    const path = new URL(route.request().url()).pathname
    if (path === `/api/chat/slots/${SLOT}/project`) {
      held.push(route)
      return
    }
    if (path === '/api/recent-projects') return json(route, { dirs: [START, NEXT, FAIL] })
    if (path === '/api/favorite-projects') return json(route, { dirs: [] })
    if (path === '/api/browse-dirs') return json(route, { path: '/home/user/workspace', parent: '/home/user', dirs: [START, NEXT, FAIL].map(p => ({ name: p.split('/').pop(), path: p })) })
    if (path === '/api/aws/project-profile') return json(route, { path: project, profile: '', profiles: [], boundDir: '' })
    if (path === '/api/members') return json(route, { members: [] })
    if (path === '/api/browse-drives') return json(route, { drives: [] })
    if (path === '/api/chat/slots') return json(route, slots())
    if (path.startsWith('/api/chat/slots/')) return json(route, detail())
    return handleBootRoute(route, path, { project: START, theme: 'dark', fixedApi })
  })
  page.on('console', m => { if (m.type() === 'error') console.log('CONSOLE:', m.text().slice(0, 300)) })
  page.on('pageerror', err => console.log('PAGEERROR:', String(err).slice(0, 300)))
  await page.addInitScript(slot => {
    localStorage.clear()
    localStorage.setItem('mc-theme', 'dark')
    localStorage.setItem('mc-onboarded', '1')
    localStorage.setItem('mc-active-slot', slot)
  }, SLOT)
  await page.goto(base + '/', { waitUntil: 'domcontentloaded' })
  await page.waitForSelector('[data-testid="composer-project-chip"]', { timeout: 20000 })
  await page.waitForTimeout(1500)

  const shot = async name => {
    const file = `${OUT}/${LABEL}-${tag}-${name}.png`
    await page.screenshot({ path: file })
    console.log('wrote', file)
  }
  const pick = async dir => {
    await page.click('[data-testid="composer-project-chip"]')
    const row = page.locator('[role="option"]', { hasText: dir }).first()
    await row.waitFor({ timeout: 10000 })
    await row.click()
    await page.waitForFunction(() => document.querySelector('[data-testid="composer-project-chip"]')?.getAttribute('aria-busy') === 'true', null, { timeout: 5000 })
  }

  await pick(NEXT)
  await page.waitForTimeout(400)
  await shot('01-pending')
  project = NEXT
  await held.shift().fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ ok: true, project: NEXT }) })
  await page.waitForFunction(() => !document.querySelector('[data-testid="composer-project-chip"]')?.hasAttribute('aria-busy'), null, { timeout: 5000 })
  await page.waitForTimeout(600)
  await shot('02-success')

  await pick(FAIL)
  await held.shift().fulfill({ status: 409, contentType: 'application/json', body: JSON.stringify({ error: 'The project could not be changed right now.' }) })
  await page.waitForFunction(() => !document.querySelector('[data-testid="composer-project-chip"]')?.hasAttribute('aria-busy'), null, { timeout: 5000 })
  await page.waitForTimeout(600)
  await shot('03-failure')

  await context.close()
  await browser.close()
}

async function main() {
  const { srv, base } = await serveDist()
  try {
    await run(base, 1200, 760, 'desktop')
    await run(base, 390, 760, 'narrow')
  } finally {
    srv.close()
  }
}

main().catch(err => { console.error(err); process.exit(1) })
