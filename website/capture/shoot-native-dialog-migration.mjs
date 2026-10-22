// Screenshots for renderer dialog surfaces.
//
// Shoots every scene in capture/native-dialog-migration.tsx, cropped to the
// dialog, notice set or banner, in dark and light themes, and lays them out as
// one contact sheet per scene group and theme.
//
//   Run from website/: node capture/shoot-native-dialog-migration.mjs [outDir] [port]
//   Cropped frames go to $FRAMES_DIR (default: a temp dir), sheets to outDir.
import { createServer } from 'vite'
import { chromium } from 'playwright-core'
import { mkdirSync, mkdtempSync, readFileSync } from 'node:fs'
import { join } from 'node:path'
import { tmpdir } from 'node:os'

const OUT = process.argv[2] || '../.github/screenshots/15662'
const PORT = Number(process.argv[3] || 5179)
const FRAMES = process.env.FRAMES_DIR || mkdtempSync(join(tmpdir(), 'ndm-frames-'))
const CHROMIUM_PATH = process.env.CHROMIUM_PATH
mkdirSync(OUT, { recursive: true })
mkdirSync(FRAMES, { recursive: true })

const GROUPS = [
  ['sessions', 'Sessions, tags and notifications'],
  ['leave', 'Leave without saving'],
  ['settings', 'Settings and Knowledge'],
  ['apps', 'Channels, artifacts and apps'],
  ['entry', 'Text entry, inline notices and the live banner'],
]

const server = await createServer({
  configFile: 'vite.config.ts',
  server: { port: PORT, strictPort: true, host: '127.0.0.1' },
})
await server.listen()
const base = `http://127.0.0.1:${PORT}/capture/native-dialog-migration.html`
const browser = await chromium.launch({ executablePath: CHROMIUM_PATH, args: ['--no-sandbox'] })

async function sceneList() {
  const ctx = await browser.newContext()
  const page = await ctx.newPage()
  await page.goto(`${base}?scene=list`)
  const text = await page.locator('#scene-list').textContent({ timeout: 15000 })
  await ctx.close()
  return JSON.parse(text)
}

// The visible panel inside a dialog: the dialog itself unless it spans the
// viewport as an overlay, in which case its largest smaller descendant.
async function panelBox(page) {
  return page.evaluate(() => {
    const dialog = document.querySelector('[role="dialog"]')
    const vw = window.innerWidth
    let best = dialog.getBoundingClientRect()
    if (best.width >= vw * 0.9) {
      best = null
      for (const el of dialog.querySelectorAll('*')) {
        const r = el.getBoundingClientRect()
        if (r.width < vw * 0.9 && r.width > 200 && (!best || r.width * r.height > best.width * best.height)) best = r
      }
    }
    return { x: best.x, y: best.y, width: best.width, height: best.height }
  })
}

function pad(box, n = 12) {
  return { x: Math.max(0, box.x - n), y: Math.max(0, box.y - n), width: box.width + 2 * n, height: box.height + 2 * n }
}

async function shoot(theme, scene, file) {
  const ctx = await browser.newContext({ viewport: { width: scene.kind === 'page' ? 1000 : 900, height: 700 }, deviceScaleFactor: 2, reducedMotion: 'reduce' })
  const page = await ctx.newPage()
  const errors = []
  page.on('console', m => { if (m.type() === 'error') errors.push(m.text()) })
  page.on('pageerror', e => errors.push(String(e)))
  await page.goto(`${base}?theme=${theme}&scene=${scene.id}`)
  let clip
  if (scene.kind === 'page') {
    await page.locator(scene.ready).first().waitFor({ timeout: 15000 })
    await page.getByRole('dialog').waitFor({ state: 'detached', timeout: 5000 })
    await page.waitForTimeout(300)
    clip = await page.evaluate(() => {
      const marked = [...document.querySelectorAll('[data-capture-crop]')]
      const els = marked.length ? marked : [document.getElementById('page-frame')]
      const rs = els.map(el => el.getBoundingClientRect())
      const x = Math.min(...rs.map(r => r.left)), y = Math.min(...rs.map(r => r.top))
      return { x, y, width: Math.max(...rs.map(r => r.right)) - x, height: Math.max(...rs.map(r => r.bottom)) - y }
    })
    if (errors.length) throw new Error(`${scene.id} (${theme}) console errors:\n${errors.join('\n')}`)
  } else if (scene.kind === 'notice') {
    const frame = page.locator('#notice-frame')
    await frame.locator('[role="alert"], [role="status"]').first().waitFor({ timeout: 15000 })
    await page.waitForTimeout(300)
    clip = await frame.boundingBox()
  } else if (scene.kind === 'banner') {
    const card = page.locator('[data-testid="notification-banner-card"]').first()
    await card.waitFor({ timeout: 15000 })
    await page.waitForTimeout(200)
    await card.hover()
    const shell = page.locator('[data-testid="notification-banner-deck-shell"]').first()
    // The deck shell sits under the top card, so a pointer click lands on the card.
    if (await shell.count()) await shell.dispatchEvent('click')
    await page.waitForTimeout(500)
    clip = pad(await page.locator('[data-testid="notification-banner-region"] > div').boundingBox())
  } else {
    const dialog = page.getByRole('dialog')
    await dialog.waitFor({ timeout: 15000 })
    if (scene.submitEmpty) {
      await dialog.locator('button[type="submit"]').click()
      await dialog.locator('[role="alert"]').first().waitFor({ timeout: 5000 })
    }
    await page.waitForTimeout(300)
    clip = pad(await panelBox(page))
  }
  await page.screenshot({ path: file, clip })
  await ctx.close()
}

async function sheet(theme, title, frames, file) {
  const cells = frames.map(f => {
    const src = `data:image/png;base64,${readFileSync(f.file).toString('base64')}`
    return `<figure${f.wide ? ' class="wide"' : ''}><figcaption>${f.where}</figcaption><img src="${src}"></figure>`
  }).join('')
  const bg = theme === 'light' ? '#f4f4f5' : '#111114'
  const fg = theme === 'light' ? '#18181b' : '#e4e4e7'
  const html = `<!doctype html><meta charset="utf-8"><style>
    body{margin:0;padding:16px;width:968px;background:${bg};color:${fg};font:14px system-ui,sans-serif}
    h1{font-size:16px;margin:0 0 12px}
    main{display:grid;grid-template-columns:repeat(2,476px);gap:16px;align-items:start}
    figure{margin:0} figure.wide{grid-column:1/-1} img{width:100%;display:block}
    figcaption{margin-bottom:4px;line-height:1.3;font-weight:600}
  </style><h1>${title} (${theme})</h1><main>${cells}</main>`
  const ctx = await browser.newContext({ viewport: { width: 1000, height: 800 }, deviceScaleFactor: 2 })
  const page = await ctx.newPage()
  await page.setContent(html, { waitUntil: 'load' })
  await page.screenshot({ path: file, fullPage: true })
  await ctx.close()
  console.log('sheet', file)
}

const scenes = await sceneList()
for (const theme of ['dark', 'light']) {
  const frames = []
  for (const s of scenes) {
    const file = join(FRAMES, `${theme}-${s.id}.png`)
    await shoot(theme, s, file)
    frames.push({ ...s, file })
  }
  for (const [i, [group, title]] of GROUPS.entries()) {
    await sheet(theme, title, frames.filter(f => f.group === group), join(OUT, `${theme}-${i + 1}-${group}.png`))
  }
}

await browser.close()
await server.close()
console.log('done; frames in', FRAMES)
