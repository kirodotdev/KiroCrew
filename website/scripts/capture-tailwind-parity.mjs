/**
 * Visual-parity harness for the Tailwind build: the same five dashboard surfaces,
 * light and dark, shot from a given `dist/` — so two builds (the Tailwind v3
 * baseline and the v4 output, or any before/after pair of the stylesheet
 * toolchain) can be photographed identically and pixel-diffed.
 *
 * Runs the REAL built SPA behind the shared loopback static server with every
 * /api/** call answered from fixtures (gateway-free). Scenes:
 *
 *   chat       chat page with a transcript of user/assistant turns and two tool
 *              rows, sidebar with three sessions
 *   dropdown   the same page with the sidebar's split create-button caret menu
 *              open (a Radix dropdown — the shadcn primitive's entrance
 *              animation, ring and shadow)
 *   settings   /settings?tab=chat
 *   agents     /capabilities?tab=crews — the crew cards and rail
 *   dialog     the "Edit crew" sheet opened from that page (a Radix dialog
 *              centred by `translate` while `animate-in` zooms it)
 *
 * Usage, from website/:
 *
 *   # shoot one dist
 *   node scripts/capture-tailwind-parity.mjs --dist ../path/to/dist --out ../temp-screenshots/tw-parity/after
 *   # pixel-diff two shot directories (same file names), exit 1 past --max-pct
 *   node scripts/capture-tailwind-parity.mjs --compare before/ after/ [--max-pct 0.5] [--out diff/]
 *
 * `--compare` writes one `<name>.diff.png` per pair with the changed pixels
 * painted red over a dimmed copy of the AFTER frame, and prints per-frame changed
 * pixel counts so a reviewer can see WHERE the two builds disagree.
 */
import { chromium } from 'playwright'
import { mkdirSync, readFileSync, readdirSync, writeFileSync, existsSync } from 'node:fs'
import { join, resolve, basename } from 'node:path'
import { serveDist } from './lib/serve-dist.mjs'
import { json, makeFixedApi, handleBootRoute } from './lib/boot-api.mjs'
import { makeToolRowScene, TOOL_ROW_VIEW } from './lib/tool-row-scene.mjs'
import { diffPngs } from './lib/diff-pngs.mjs'

const args = process.argv.slice(2)
const flag = (name, fallback) => {
  const i = args.indexOf(name)
  return i >= 0 && args[i + 1] !== undefined ? args[i + 1] : fallback
}

const PROJECT = '/home/user/workspace/demo-app'
const SLOT = 'chat-1-parity'
/** Per-channel tolerance for the diff: antialiasing jitter on text, not a
 *  colour or layout change. */
const DIFF_TOL = 24

// ---------------------------------------------------------------------------
// Fixtures shared by every scene
// ---------------------------------------------------------------------------

const CREWS = [
  { name: 'default', kiro_agent: 'kirocrew', workspace: 'default', memory_store: 'default', description: 'Used for all new chats', source: 'user', model: '', triggers: '', session_color: '' },
  { name: 'atlas', kiro_agent: 'atlas', workspace: 'default', memory_store: 'default', description: 'Long-horizon planner', source: 'user', model: '', triggers: 'migration', session_color: '' },
  { name: 'scout', kiro_agent: 'scout', workspace: 'default', memory_store: 'default', description: 'Research scout', source: 'user', model: '', triggers: '', session_color: '' },
]
const INSTALLED = [
  { name: 'kirocrew', description: 'Built-in', source: 'kirocrew', model: '', skills: ['memory', 'artifacts'], mcp_servers: ['kirocrew-core'], filename: 'kirocrew.json', kirocrew_owned: true },
  { name: 'atlas', description: 'Long-horizon planner', source: 'builtin', model: '', skills: ['grill'], mcp_servers: ['kirocrew-core'], filename: 'atlas.json', kirocrew_owned: false },
  { name: 'scout', description: 'Built-in', source: 'builtin', model: '', skills: ['memory'], mcp_servers: ['kirocrew-core'], filename: 'scout.json', kirocrew_owned: false },
]
const FOLDERS = [
  { id: 'f1', name: 'Kiro', icon: '🚀', order: 0, collapsed: false },
  { id: 'f2', name: 'Design', icon: '🎨', order: 1, collapsed: true },
]

function makeScene() {
  const { slots, detail } = makeToolRowScene(SLOT)
  const now = Math.floor(Date.now() / 1000)
  // Two more sessions so the sidebar shows a list, one of them in a folder.
  slots.push(
    { key: 'chat-1-b', title: 'Per-app trust grants', running: false, last_message: 'Done.', messages: 4, agent: 'kirocrew', memory_mode: 'persistent', project: PROJECT, modified: now - 3600, folder_id: 'f1', source_links: [], source_links_total: 0 },
    { key: 'chat-1-c', title: 'Windows NSIS target', running: false, last_message: 'Shipped.', messages: 9, agent: 'atlas', memory_mode: 'persistent', project: PROJECT, modified: now - 7200, source_links: [], source_links_total: 0 },
  )
  return { slots, detail }
}

async function bindRoutes(page, theme) {
  const fixedApi = makeFixedApi(PROJECT)
  const scene = makeScene()
  await page.routeWebSocket(/\/api\/ws/, () => {})
  await page.route('**/api/**', async route => {
    const path = new URL(route.request().url()).pathname
    if (path === '/api/chat/slots') return json(route, scene.slots)
    if (path.startsWith('/api/chat/slots/')) return json(route, scene.detail)
    if (path === '/api/chat/folders') return json(route, FOLDERS)
    if (path === '/api/agents') return json(route, { agents: CREWS, default_agent: 'default' })
    if (path === '/api/agents/installed') return json(route, INSTALLED)
    if (path.startsWith('/api/agents/detail/')) {
      const name = decodeURIComponent(path.split('/api/agents/detail/')[1] || '')
      return json(route, { name, model: '', prompt: 'Plan before acting.', skills: [], tools: ['fs_read'], allowedTools: [], mcpServers: {}, toolsSettings: {} })
    }
    if (path === '/api/skills') return json(route, [])
    if (path === '/api/models') return json(route, { models: [] })
    return handleBootRoute(route, path, { project: PROJECT, theme, fixedApi })
  })
  page.on('pageerror', err => console.log('PAGEERROR:', String(err).slice(0, 300)))
  page.on('console', msg => {
    if (msg.type() === 'error') console.log('CONSOLE:', msg.text().slice(0, 300))
  })
  await page.addInitScript(([t, s]) => {
    localStorage.clear()
    localStorage.setItem('mc-theme', t)
    localStorage.setItem('mc-onboarded', '1')
    localStorage.setItem('mc-import-onboarded', '1')
    localStorage.setItem('mc-privacy-acked', '1')
    localStorage.setItem('mc-active-slot-chat', s)
  }, [theme, SLOT])
}

/** Freeze every animation so two builds are photographed at rest, not at
 *  different frames of a spinner or an entrance. `animation: none` rather than
 *  `animation-play-state: paused`: a paused animation holds its FIRST frame, and
 *  for the shadcn overlays (`animate-in fade-in-0`) that frame is fully
 *  transparent, so a menu or dialog opened after the freeze would never show up
 *  in the frame. With no animation at all every element renders its resting
 *  state, which is the thing the two builds are compared on. */
async function freezeMotion(page) {
  await page.addStyleTag({ content: '*,*::before,*::after{animation:none!important;transition:none!important;caret-color:transparent!important}' })
}

/** Refuse a frame whose overlay is not actually painted: a locator can be
 *  "visible" while its computed opacity is 0, which is exactly how a frozen
 *  entrance animation hides a menu or a dialog. */
async function assertPainted(locator, what) {
  const { opacity, w, h } = await locator.evaluate(el => {
    const r = el.getBoundingClientRect()
    return { opacity: getComputedStyle(el).opacity, w: r.width, h: r.height }
  })
  if (parseFloat(opacity) < 0.99 || w < 40 || h < 40) {
    throw new Error(`${what} is not painted (opacity=${opacity}, ${Math.round(w)}x${Math.round(h)}) — the frame would not show it`)
  }
}

// ---------------------------------------------------------------------------
// Scenes
// ---------------------------------------------------------------------------

async function shootChat(page, base, out, theme) {
  await page.goto(base + '/', { waitUntil: 'domcontentloaded' })
  await page.waitForSelector('text=Start a clean npm ci in the Node 22 container', { timeout: 20000 })
  await page.waitForTimeout(1500)
  await freezeMotion(page)
  await page.screenshot({ path: join(out, `${theme}-chat.png`) })

  const caret = page.locator('button[aria-label="More create options"]').first()
  await caret.waitFor({ state: 'visible', timeout: 15000 })
  await caret.click()
  const menu = page.locator('[role="menu"]').first()
  await menu.waitFor({ state: 'visible', timeout: 10000 })
  await page.waitForTimeout(600)
  await assertPainted(menu, 'sidebar caret menu')
  await page.screenshot({ path: join(out, `${theme}-dropdown.png`) })
  await page.keyboard.press('Escape')
}

async function shootSettings(page, base, out, theme) {
  await page.goto(`${base}/settings?tab=chat`, { waitUntil: 'domcontentloaded' })
  await page.locator('#main-content').waitFor({ state: 'visible', timeout: 15000 })
  await page.waitForTimeout(1500)
  await freezeMotion(page)
  await page.screenshot({ path: join(out, `${theme}-settings.png`) })
}

async function shootAgents(page, base, out, theme) {
  await page.goto(`${base}/capabilities?tab=crews`, { waitUntil: 'domcontentloaded' })
  await page.locator('#main-content').getByText('Agents you chat with', { exact: false })
    .waitFor({ state: 'visible', timeout: 15000 })
  await page.waitForTimeout(1200)
  await freezeMotion(page)
  await page.screenshot({ path: join(out, `${theme}-agents.png`) })

  const card = page.getByRole('button', { name: /Edit (crew|agent) atlas/i })
  await card.waitFor({ state: 'visible', timeout: 15000 })
  await card.click()
  const sheet = page.getByRole('dialog', { name: /edit (crew|agent)/i })
  await sheet.waitFor({ state: 'visible', timeout: 10000 })
  await page.waitForTimeout(800)
  await assertPainted(sheet, 'edit-crew dialog')
  await page.screenshot({ path: join(out, `${theme}-dialog.png`) })
}

async function capture(dist, out) {
  mkdirSync(out, { recursive: true })
  const { srv, base } = await serveDist(dist)
  const browser = await chromium.launch()
  try {
    for (const theme of ['dark', 'light']) {
      const context = await browser.newContext({ viewport: TOOL_ROW_VIEW, deviceScaleFactor: 1 })
      const page = await context.newPage()
      await bindRoutes(page, theme)
      await shootChat(page, base, out, theme)
      await shootSettings(page, base, out, theme)
      await shootAgents(page, base, out, theme)
      await context.close()
      console.log(`shot ${theme}: chat, dropdown, settings, agents, dialog -> ${out}`)
    }
  } finally {
    await browser.close()
    srv.close()
  }
}

// ---------------------------------------------------------------------------
// Compare
// ---------------------------------------------------------------------------

/** Paint the thresholded diff of two same-size PNGs: AFTER dimmed, changed
 *  pixels solid red. Returns the data URL of the painted frame. */
async function paintDiff(page, aB64, bB64, tol) {
  return page.evaluate(async ([a, b, t]) => {
    const load = (b64) => new Promise((res) => {
      const img = new Image()
      img.onload = () => res(img)
      img.src = `data:image/png;base64,${b64}`
    })
    const [ia, ib] = await Promise.all([load(a), load(b)])
    const c = document.createElement('canvas')
    c.width = ib.naturalWidth; c.height = ib.naturalHeight
    const ctx = c.getContext('2d')
    ctx.drawImage(ib, 0, 0)
    const after = ctx.getImageData(0, 0, c.width, c.height)
    const ca = document.createElement('canvas')
    ca.width = ia.naturalWidth; ca.height = ia.naturalHeight
    ca.getContext('2d').drawImage(ia, 0, 0)
    const before = ca.getContext('2d').getImageData(0, 0, ca.width, ca.height)
    const o = after.data, p = before.data
    for (let i = 0; i < o.length; i += 4) {
      let d = 0
      if (i < p.length) for (let k = 0; k < 4; k++) d = Math.max(d, Math.abs(o[i + k] - p[i + k]))
      if (d > t) { o[i] = 255; o[i + 1] = 0; o[i + 2] = 0; o[i + 3] = 255 }
      else { o[i] = 128 + (o[i] >> 2); o[i + 1] = 128 + (o[i + 1] >> 2); o[i + 2] = 128 + (o[i + 2] >> 2) }
    }
    ctx.putImageData(after, 0, 0)
    return c.toDataURL('image/png')
  }, [aB64, bB64, tol])
}

async function compare(beforeDir, afterDir, out, maxPct) {
  mkdirSync(out, { recursive: true })
  const names = readdirSync(afterDir).filter(f => f.endsWith('.png') && !f.endsWith('.diff.png')).sort()
  const browser = await chromium.launch()
  const page = await browser.newPage()
  let worst = 0
  const rows = []
  try {
    for (const name of names) {
      const a = join(beforeDir, name)
      if (!existsSync(a)) { rows.push(`${name}: no BEFORE frame`); worst = Infinity; continue }
      const aB64 = readFileSync(a).toString('base64')
      const bB64 = readFileSync(join(afterDir, name)).toString('base64')
      const r = await diffPngs(page, aB64, bB64, DIFF_TOL)
      const total = TOOL_ROW_VIEW.width * TOOL_ROW_VIEW.height
      const pct = (100 * r.n) / total
      worst = Math.max(worst, pct)
      const box = r.n ? ` bbox x${r.minX}-${r.maxX} y${r.minY}-${r.maxY}` : ''
      rows.push(`${name.padEnd(22)} changed ${String(r.n).padStart(7)} px (${pct.toFixed(2)}%), raw ${r.rawN}${box}`)
      const url = await paintDiff(page, aB64, bB64, DIFF_TOL)
      writeFileSync(join(out, name.replace(/\.png$/, '.diff.png')), Buffer.from(url.split(',')[1], 'base64'))
    }
  } finally {
    await browser.close()
  }
  for (const r of rows) console.log(r)
  if (worst > maxPct) {
    console.error(`\nFAIL: worst frame differs by ${worst.toFixed(2)}% > ${maxPct}% (diff frames in ${out})`)
    return 1
  }
  console.log(`\nok: every frame within ${maxPct}% (worst ${worst.toFixed(2)}%)`)
  return 0
}

if (args.includes('--compare')) {
  const i = args.indexOf('--compare')
  const code = await compare(resolve(args[i + 1]), resolve(args[i + 2]), resolve(flag('--out', '../temp-screenshots/tw-parity/diff')), parseFloat(flag('--max-pct', '0.5')))
  process.exit(code)
} else {
  const dist = resolve(flag('--dist', 'dist'))
  await capture(dist, resolve(flag('--out', `../temp-screenshots/tw-parity/${basename(dist)}`)))
}
