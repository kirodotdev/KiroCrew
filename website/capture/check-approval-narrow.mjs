/**
 * Real-browser check at a 320px screen, per language (en, de, es, it, en-XA):
 * nothing on the approval card or in its Trust and Reject menus is clipped or
 * runs past its box, in every capture scene. The Duration track stays on one
 * line where it fits and stacks where it does not. jsdom lays nothing out, so
 * the unit tests stub the widths; this is where the widths are real. Same
 * harness and loopback server as shoot-approval-card-redesign.mjs.
 * Writes <outDir>/narrow.json and exits 1 on any finding. With `shots` it
 * photographs the shell scene's open Trust menu for en and de, light and dark.
 *
 * Usage (from website/): node capture/check-approval-narrow.mjs <outDir> [shots]
 */
import { chromium } from 'playwright'
import { createServer } from 'vite'
import { mkdirSync, readFileSync, writeFileSync } from 'node:fs'
import { chromiumExecutable } from '../scripts/lib/chromium-executable.mjs'

const OUT = process.argv[2]
const SHOTS = process.argv[3] === 'shots'
const RAISED_BY_CLASS = readFileSync(new URL('../src/components/chat-input/TypingLockBanner.tsx', import.meta.url), 'utf8')
  .match(/<span role="note" className="([^"]+)" data-testid="raised-by">/)?.[1]
if (!RAISED_BY_CLASS) throw new Error('RaisedBy markup not found in TypingLockBanner.tsx; update this check')
mkdirSync(OUT, { recursive: true })
const server = await createServer({ configFile: './vite.config.ts', server: { host: '127.0.0.1', port: 5199, strictPort: false }, logLevel: 'error' })
await server.listen()
const base = server.resolvedUrls.local[0].replace(/\/$/, '')
const browser = await chromium.launch({ executablePath: chromiumExecutable(), env: { ...process.env, LD_LIBRARY_PATH: '' } })
const LANGS = SHOTS ? ['en', 'de'] : ['en', 'de', 'es', 'it', 'en-XA']
const SCENES = ['shell', 'site', 'mcp', 'mcp-always', 'override', 'ghost', 'long-purpose', 'long-mcp', 'long-base', 'none']
const findings = []
const facts = {}

// Every element under *root* whose own content is wider than its box (and is
// not meant to scroll), or whose right edge runs past *root*'s.
const sweep = root => {
  const r = root.getBoundingClientRect()
  const bad = []
  for (const el of root.querySelectorAll('*')) {
    const s = getComputedStyle(el)
    if (s.display === 'none' || s.visibility === 'hidden') continue
    const b = el.getBoundingClientRect()
    if (b.width === 0 || b.height === 0) continue
    const scrolls = ['auto', 'scroll'].includes(s.overflowX)
    const clipped = !scrolls && el.scrollWidth > el.clientWidth + 1 && el.clientWidth > 0 && s.overflowX !== 'visible'
    const past = b.right > r.right + 1 || b.left < r.left - 1
    if (clipped || past) bad.push({ tag: el.tagName, cls: String(el.className).slice(0, 80), text: (el.textContent || '').slice(0, 40), clipped, past, w: Math.round(b.width), right: Math.round(b.right), rootRight: Math.round(r.right) })
  }
  return bad
}

for (const lang of LANGS) {
  for (const theme of SHOTS ? ['kiro-light', 'kiro-dark'] : ['kiro-light']) {
    const ctx = await browser.newContext({ viewport: { width: 320, height: 760 }, deviceScaleFactor: 2 })
    await ctx.addInitScript(l => { try { localStorage.setItem('mc-lang', l) } catch {} }, lang)
    const page = await ctx.newPage()
    await page.route(u => u.pathname.startsWith('/api/'), r => (r.request().method() === 'POST' ? r.fulfill({ status: 200, contentType: 'application/json', body: '{}' }) : r.continue()))
    for (const scene of SHOTS ? ['shell'] : SCENES) {
      await page.goto(`${base}/capture/approval-card-redesign.html?scene=${scene}&theme=${theme}&lang=${lang}`)
      await page.locator('[data-approval-actions]').waitFor({ timeout: 30000 })
      await page.waitForTimeout(900)
      // On a Shared Crew a request someone else raised carries their name in
      // the header. The harness has no second person, so the chip is added
      // here with RaisedBy's own classes, read from its source, and a long name.
      if (!SHOTS) await page.evaluate(({ cls, text }) => {
        const chip = Object.assign(document.createElement('span'), { className: cls, textContent: text })
        chip.setAttribute('data-testid', 'raised-by')
        document.querySelector('[data-testid="approval-header"]')?.appendChild(chip)
      }, { cls: RAISED_BY_CLASS, text: 'Raised by Alexandra Montgomery-Fairweather' })
      const card = await page.evaluateHandle(() => document.querySelector('[data-approval-actions]').closest('[data-testid="approval-card-motion"]'))
      for (const f of await card.evaluate(sweep)) findings.push({ lang, scene, where: 'card', ...f })
      // The heading must keep a readable width, not shrink beside the pill.
      const titleW = await page.evaluate(() => Math.round(document.querySelector('[data-testid="approval-title"]')?.getBoundingClientRect().width ?? 0))
      if (titleW < 120) findings.push({ lang, scene, where: 'card', tag: 'heading', text: `title width ${titleW}px` })
      for (const [menu, icon] of [['Trust', 'lucide-handshake'], ['Reject', 'lucide-ban']]) {
        const trigger = page.locator(`[data-approval-actions] button:has(svg.${icon})`)
        if (!(await trigger.count()) || await trigger.first().isDisabled()) continue
        await trigger.first().click()
        const opened = await page.getByRole('menu').waitFor({ timeout: 1500 }).then(() => true, () => false)
        if (!opened) continue
        await page.waitForTimeout(300)
        const m = await page.evaluateHandle(() => document.querySelector('[role="menu"]'))
        for (const f of await m.evaluate(sweep)) findings.push({ lang, scene, where: `${menu} menu`, ...f })
        if (menu === 'Trust' && scene === 'shell') {
          facts[lang] = await page.evaluate(() => {
            const t = document.querySelector('[role="menu"] [role="group"]')
            return t && { stacked: t.hasAttribute('data-stacked'), trackW: Math.round(t.getBoundingClientRect().width), segTops: [...t.children].map(c => Math.round(c.getBoundingClientRect().top)), segHeights: [...t.children].map(c => Math.round(c.getBoundingClientRect().height)) }
          })
          if (SHOTS) await page.screenshot({ path: `${OUT}/menu-320-${lang}-${theme.endsWith('light') ? 'light' : 'dark'}.png` })
        }
        await page.keyboard.press('Escape')
        await page.waitForTimeout(200)
      }
      // The receipt a grant leaves names what was trusted: the exact command
      // and the family, each chosen in turn from a fresh card.
      if (!SHOTS && (scene === 'shell' || scene === 'long-base')) {
        for (const row of ['[data-testid="trust-command-full"]', '[data-testid="trust-base-scope"]']) {
          await page.goto(`${base}/capture/approval-card-redesign.html?scene=${scene}&theme=${theme}&lang=${lang}`)
          await page.locator('[data-approval-actions]').waitFor({ timeout: 30000 })
          await page.waitForTimeout(900)
          await page.locator('[data-approval-actions] button:has(svg.lucide-handshake)').first().click()
          await page.getByRole('menu').waitFor({ timeout: 3000 })
          await page.waitForTimeout(300)
          await page.locator(`[role="menuitem"]:has(${row})`).first().click()
          const shown = await page.getByTestId('approval-receipt').waitFor({ timeout: 5000 }).then(() => true, () => false)
          if (!shown) { findings.push({ lang, scene, where: 'receipt', tag: 'missing', text: row }); continue }
          await page.waitForTimeout(600)
          const r = await page.evaluateHandle(() => document.querySelector('[data-testid="approval-receipt"]'))
          for (const f of await r.evaluate(sweep)) findings.push({ lang, scene, where: `receipt ${row}`, ...f })
        }
      }
    }
    await ctx.close()
  }
}
writeFileSync(`${OUT}/narrow.json`, JSON.stringify({ facts, findings }, null, 2))
console.log(JSON.stringify({ facts, count: findings.length }))
await browser.close()
await server.close()
process.exit(findings.length ? 1 : 0)
