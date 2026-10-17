/**
 * Screenshot harness for the "Run in terminal" confirmation dialog.
 *
 * Runs the REAL built SPA (website/dist) with the network answered from
 * fixtures, so the code block, its hover header and the dialog render exactly
 * as in production. The fixture transcript carries three shell blocks:
 *   1. a long single line — the case the dialog exists for, where the block's
 *      horizontally scrolling <pre> clips the tail out of view
 *   2. a short multi-line block — numbered-line preview
 *   3. a credential read — the flagged variant
 *
 * Usage: node scripts/capture-run-confirm.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { json, makeFixedApi, handleBootRoute } from './lib/boot-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/run-confirm'
const SLOT = 'chat-run-confirm'
const PROJECT = '/home/user/workspace/KiroCrew'

mkdirSync(OUT, { recursive: true })

const LONG = 'docker run --rm -it -v "$PWD":/src -w /src node:24 npm ci && npm run build -- --mode production --outDir dist/release && rm -rf node_modules/.cache'
const MULTI = 'cd /srv/app\nnpm ci\nnpm run build'
const SENSITIVE = 'cat ~/.aws/credentials'

const t0 = Date.now() / 1000 - 600
const slots = [{
  key: SLOT,
  title: 'Release build',
  running: false,
  last_message: 'Here are the commands.',
  messages: 4,
  agent: 'kirocrew',
  memory_mode: 'persistent',
  project: PROJECT,
  modified: Math.floor(Date.now() / 1000),
  source_links: [],
  source_links_total: 0,
}]

const detail = {
  running: false,
  has_more: false,
  total: 4,
  queue: [],
  project: PROJECT,
  messages: [
    { role: 'user', ts: t0, content: 'How do I produce the release build?' },
    { role: 'assistant', ts: t0 + 8, content: `One line, in a clean container:\n\n\`\`\`bash\n${LONG}\n\`\`\`\n\nOr step by step:\n\n\`\`\`bash\n${MULTI}\n\`\`\`\n\nAnd to check the publish profile:\n\n\`\`\`bash\n${SENSITIVE}\n\`\`\`` },
  ],
}

const FIXED_API = makeFixedApi(PROJECT)

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  const context = await browser.newContext({
    viewport: { width: 1280, height: 900 },
    deviceScaleFactor: 2,
  })
  const page = await context.newPage()
  await page.routeWebSocket(/\/api\/ws/, () => {})

  const scene = { theme: 'dark' }

  await page.route('**/api/**', async route => {
    const path = new URL(route.request().url()).pathname
    if (path === '/api/chat/slots') return json(route, slots)
    if (path.startsWith('/api/chat/slots/')) return json(route, detail)
    return handleBootRoute(route, path, { project: PROJECT, theme: scene.theme, fixedApi: FIXED_API })
  })

  page.on('pageerror', err => console.log('PAGEERROR:', String(err).slice(0, 300)))

  async function load(theme = 'dark') {
    scene.theme = theme
    await page.addInitScript(([t, slot]) => {
      localStorage.clear()
      localStorage.setItem('mc-theme', t)
      localStorage.setItem('mc-onboarded', '1')
      localStorage.setItem('mc-active-slot-chat', slot)
    }, [theme, SLOT])
    await page.goto(base + '/', { waitUntil: 'domcontentloaded' })
    await page.waitForSelector('.code-block', { timeout: 20000 })
    await page.waitForTimeout(800)
  }

  const shot = async name => {
    await page.screenshot({ path: `${OUT}/${name}.png` })
    console.log('wrote', `${OUT}/${name}.png`)
  }

  /** Open the dialog from the nth code block's terminal button. */
  async function openDialog(n) {
    const block = page.locator('.code-block').nth(n)
    await block.scrollIntoViewIfNeeded()
    await block.hover()
    await page.waitForTimeout(200)
    await block.getByRole('button', { name: 'Run in terminal' }).evaluate(el => el.click())
    await page.waitForSelector('[role="dialog"]', { timeout: 5000 })
    await page.waitForTimeout(500)
  }

  async function closeDialog() {
    await page.keyboard.press('Escape')
    await page.waitForTimeout(400)
  }

  await load('dark')
  const blocks = await page.locator('.code-block').count()
  console.log('code blocks:', blocks)

  // The clipped block itself, before any dialog — evidence of the problem.
  await page.locator('.code-block').first().hover()
  await page.waitForTimeout(300)
  await shot('block-clipped-dark')

  await openDialog(0)
  await shot('confirm-longline-dark')
  await closeDialog()

  await openDialog(1)
  await shot('confirm-multiline-dark')
  await closeDialog()

  await openDialog(2)
  await shot('confirm-flagged-dark')
  await closeDialog()

  await load('light')
  await openDialog(0)
  await shot('confirm-longline-light')
  await closeDialog()
  await openDialog(2)
  await shot('confirm-flagged-light')

  await browser.close()
  srv.close()
}

main().catch(err => { console.error(err); process.exit(1) })
