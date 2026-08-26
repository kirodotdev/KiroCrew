/**
 * Recording harness for the three shadcn/ui overlay primitives whose animation
 * classes only become real CSS once `tailwindcss-animate` is installed:
 * `ui/dropdown-menu.tsx`, `ui/context-menu.tsx` and `ui/popover.tsx`.
 *
 * Each carries `animate-in fade-in-0 zoom-in-95 slide-in-from-*` in its class
 * list already. Without the plugin those class names resolve to nothing, so the
 * surface pops in on a single frame; with it, the same markup animates. Only
 * motion distinguishes the two, which is why this is a recording and not a
 * screenshot.
 *
 * One CONTEXT per surface, so Playwright writes one video per surface instead of
 * one long take that has to be searched for three events. The wall-clock offset
 * of each trigger is printed as `TRIGGER <surface> <seconds>` so the encode step
 * can cut a tight window without guessing.
 *
 * Runs the REAL built SPA (website/dist) behind the shared in-process static
 * server with every /api/** call answered from fixtures — gateway-free, no
 * kiro-cli, no dashboard auth. Rebuild dist first: the change under test is CSS
 * the build emits, so a stale bundle records the wrong side.
 *
 * Usage: node scripts/record-menu-animations.mjs [outDir] [prefix]
 */
import { chromium } from 'playwright'
import { mkdirSync, renameSync, readdirSync, existsSync, rmSync } from 'node:fs'
import { join, resolve } from 'node:path'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = resolve(process.argv[2] || '../temp-screenshots/menu-animations')
const PREFIX = process.argv[3] || 'after'
// PREFIX is concatenated into a directory path that is then removed RECURSIVELY,
// so a prefix carrying a separator or `..` would escape OUT and delete something
// else. Constrain it to a slug rather than trying to sanitise a path.
if (!/^[A-Za-z0-9_-]+$/.test(PREFIX)) {
  throw new Error(
    `refusing prefix ${JSON.stringify(PREFIX)}: letters, digits, underscore and hyphen only`,
  )
}
mkdirSync(OUT, { recursive: true })

const SIZE = { width: 1280, height: 800 }
const now = Math.floor(Date.now() / 1000)
const iso = s => new Date(s * 1000).toISOString()

const SLOTS = [
  {
    key: 'chat-a', title: 'Crew 面板开场动画排查', running: true, messages: 4,
    agent: 'kirocrew', modified: now, last_ts: iso(now), folder_id: '',
    last_message: 'Reading ui/dialog.tsx.',
  },
  {
    key: 'chat-b', title: 'App Store 内建行改为来自 catalog', running: false, messages: 12,
    agent: 'kirocrew', modified: now - 420, last_ts: iso(now - 420), folder_id: '',
    last_message: '57 checks green.',
  },
  {
    key: 'chat-c', title: 'Advisory review 加上否决权', running: false, messages: 7,
    agent: 'research', modified: now - 900, last_ts: iso(now - 900), folder_id: '',
    last_message: 'Filed the follow-up issue.',
  },
]

/** The base stub answers /api/instances with an empty list, which hides the
 *  topbar switcher; two instances keep the shell realistic. */
const extra = async (path, route) => {
  if (path.startsWith('/api/instances')) {
    await json(route, {
      instances: [
        { id: 'local', name: 'Local', url: '', active: true, unread: 0 },
        { id: 'lab', name: 'homelab', url: 'http://lab:5476', active: false, unread: 2 },
      ],
      active: 'local',
    })
    return true
  }
  return false
}

/** Each surface: the primitive it exercises, how to open it, what proves it opened. */
const SURFACES = [
  {
    name: 'dropdown',
    open: page => page.getByRole('button', { name: 'More options' }).first().click(),
    settled: page => page.getByRole('menu').first(),
  },
  {
    name: 'context-menu',
    // Right-click a NON-active row: activating one would navigate mid-take.
    open: page => page.getByRole('button', { name: /App Store 内建行/ }).first()
      .click({ button: 'right' }),
    settled: page => page.getByRole('menu').first(),
  },
  {
    name: 'popover',
    open: page => page.getByRole('button', { name: 'Set a goal' }).click(),
    settled: page => page.locator('[data-radix-popper-content-wrapper]').first(),
  },
]

async function recordSurface(browser, base, surface) {
  const dir = join(OUT, `_raw-${PREFIX}-${surface.name}`)
  if (existsSync(dir)) rmSync(dir, { recursive: true })
  mkdirSync(dir, { recursive: true })

  const context = await browser.newContext({
    viewport: SIZE,
    // deviceScaleFactor stays 1: a 2x frame doubles encode cost and every
    // artifact is downscaled anyway.
    deviceScaleFactor: 1,
    recordVideo: { dir, size: SIZE },
  })
  const page = await context.newPage()
  const t0 = Date.now()
  logPageProblems(page)
  await stubDashboardApi(page, { slots: SLOTS, extra })

  await page.goto(base, { waitUntil: 'domcontentloaded' })
  // Wait on the shell's own controls rather than a bare timeout, then let the
  // sidebar's entrance settle so the first frames are a populated app.
  await page.getByRole('button', { name: 'Set a goal' }).waitFor({ state: 'visible', timeout: 25000 })
  await page.waitForTimeout(1800)

  const at = (Date.now() - t0) / 1000
  await surface.open(page)
  await surface.settled(page).waitFor({ state: 'visible', timeout: 10000 })
  console.log(`TRIGGER ${surface.name} ${at.toFixed(3)}`)
  await page.waitForTimeout(1500)

  await page.keyboard.press('Escape')
  await page.waitForTimeout(700)

  await context.close() // flushes the video file
  const webm = readdirSync(dir).filter(f => f.endsWith('.webm')).sort().pop()
  if (!webm) throw new Error(`playwright wrote no video for ${surface.name}`)
  const dest = join(OUT, `${PREFIX}-${surface.name}.webm`)
  renameSync(join(dir, webm), dest)
  rmSync(dir, { recursive: true })
  console.log('WEBM', dest)
}

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  for (const surface of SURFACES) await recordSurface(browser, base, surface)
  await browser.close()
  srv.close()
}

main().catch(err => { console.error(err); process.exit(1) })
