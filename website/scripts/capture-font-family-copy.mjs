/**
 * Screenshots for the Font Family description copy (issue #2700).
 *
 * Every frame is ASSERTED, not just captured, because the first pass of these
 * shots shipped two false claims: a "light theme" frame that rendered dark, and
 * a caption naming System while the pixels showed Sans. So this harness fails
 * rather than writing a frame when the option is not the one being claimed, or
 * when the light frame's background is not actually light.
 *
 * A fresh browser context per scenario — page.addInitScript ACCUMULATES, so
 * reusing one page silently stacks every scenario's seed script.
 *
 * Usage: npm run build && node scripts/capture-font-family-copy.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { stubDashboardApi } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/font-family-copy'
const EN = 'UI font family for the dashboard. Code font follows the active theme.'
const ZH = '仪表板的界面字体。代码字体跟随当前主题。'

/** Scenarios: the copy under test, in both modes and a CJK catalog. */
const SHOTS = [
  { name: 'font-family-dark.png', lang: 'en', mode: 'dark', expect: EN },
  { name: 'font-family-light.png', lang: 'en', mode: 'light', expect: EN },
  { name: 'font-family-zh-CN.png', lang: 'zh-CN', mode: 'dark', expect: ZH },
]

async function main() {
  mkdirSync(OUT, { recursive: true })
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  const errors = []

  for (const shot of SHOTS) {
    const ctx = await browser.newContext({ viewport: { width: 1280, height: 900 } })
    const page = await ctx.newPage()
    page.on('console', m => { if (m.type() === 'error') errors.push(`${shot.name}: ${m.text()}`) })
    page.on('pageerror', e => errors.push(`${shot.name}: pageerror: ${e.message}`))
    await stubDashboardApi(page)

    // Seed BEFORE first paint: System is the option the new sentence is about,
    // so it is the one the frame must show selected.
    await page.addInitScript(([lang, mode]) => {
      localStorage.setItem('mc-lang', lang)
      localStorage.setItem('mc-theme', mode)
      localStorage.setItem('mc-font-family', 'system')
      localStorage.setItem('mc-onboarded', '1')
    }, [shot.lang, shot.mode])

    await page.goto(base + '/settings?tab=display', { waitUntil: 'domcontentloaded' })

    const desc = page.getByText(shot.expect, { exact: true })
    await desc.waitFor({ timeout: 20000 })

    // The mode preference resolves through the provider; pin the resolved
    // attribute so a 'light' preference cannot render dark unnoticed.
    await page.evaluate(m => { document.documentElement.dataset.theme = `kiro-${m}` }, shot.mode)
    await page.waitForTimeout(400)

    // Assertion 1: the claimed option is the selected one (aria-pressed).
    const system = page.getByRole('button', { name: 'System', pressed: true })
    if (await system.count() === 0) {
      throw new Error(`${shot.name}: System is not the selected Font Family option`)
    }

    // Assertion 2: a light frame is actually light. Luminance, not vibes.
    const bg = await page.evaluate(() => getComputedStyle(document.body).backgroundColor)
    const [r, g, b] = bg.match(/\d+/g).map(Number)
    const lum = (0.2126 * r + 0.7152 * g + 0.0722 * b) / 255
    if (shot.mode === 'light' && lum < 0.5) throw new Error(`${shot.name}: light frame rendered dark (${bg})`)
    if (shot.mode === 'dark' && lum > 0.5) throw new Error(`${shot.name}: dark frame rendered light (${bg})`)

    await desc.scrollIntoViewIfNeeded()
    const box = await desc.boundingBox()
    await page.screenshot({
      path: `${OUT}/${shot.name}`,
      clip: { x: box.x - 24, y: box.y - 30, width: Math.min(1280 - (box.x - 24), box.width + 120), height: 96 },
    })
    console.log(`ok ${shot.name}  (System selected, ${shot.mode} bg ${bg})`)
    await ctx.close()
  }

  await browser.close()
  srv.close()

  if (errors.length) {
    console.error('console errors:\n  ' + errors.join('\n  '))
    process.exit(1)
  }
  console.log(`screenshots in ${OUT}`)
}

main()
