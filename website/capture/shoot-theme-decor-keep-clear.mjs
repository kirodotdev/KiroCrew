/**
 * Screenshot harness for theme-decor-keep-clear: before (?fix=0) and after,
 * at a wide and a narrow width, typing a long line into the real composer.
 * After the shots it counts marked-box reads while text streams into the
 * transcript, which must stay at zero.
 *
 * Usage (from website/): node capture/shoot-theme-decor-keep-clear.mjs [outDir]
 */
import { chromium } from 'playwright'
import { createServer } from 'vite'
import { mkdirSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join, resolve } from 'node:path'
import { chromiumExecutable } from '../scripts/lib/chromium-executable.mjs'

const here = dirname(fileURLToPath(import.meta.url))
const OUT = process.argv[2] || resolve(here, '../../temp-screenshots/theme-decor-keep-clear')
mkdirSync(OUT, { recursive: true })

const server = await createServer({
  configFile: './vite.config.ts',
  server: { host: '127.0.0.1', port: 0, strictPort: false },
  logLevel: 'warn',
})
await server.listen()
const base = server.resolvedUrls?.local?.[0]?.replace(/\/$/, '')

const browser = await chromium.launch({ executablePath: chromiumExecutable(), env: { ...process.env, LD_LIBRARY_PATH: '' } })
const LINE = 'Give me the modify-db-proxy-target-group CLI command for the integ endpoint, the end of this line must stay readable'
const SLOT = '.fixed.inset-0.pointer-events-none'
for (const [label, width, right] of [['wide', 1280, 230], ['narrow', 420, 0]]) {
  for (const fix of ['0', '1']) {
    const page = await browser.newPage({ viewport: { width, height: 520 }, deviceScaleFactor: 2 })
    page.on('pageerror', e => console.log('PAGE EXCEPTION:', e.message))
    await page.goto(`${base}/capture/theme-decor-keep-clear.html?fix=${fix}&right=${right}`)
    const box = page.locator('[data-guide-keep-clear] [contenteditable="true"], [data-guide-keep-clear] textarea').first()
    await box.waitFor()
    await box.click()
    await page.keyboard.type(LINE)
    if (fix === '1') await page.waitForFunction(sel => !!document.querySelector(sel)?.style.clipPath, SLOT)
    const clip = await page.evaluate(sel => document.querySelector(sel)?.style.clipPath || '', SLOT)
    console.log(label, fix === '1' ? 'after' : 'before', 'clip-path set:', clip !== '')
    const shot = join(OUT, `${fix === '1' ? 'after' : 'before'}-${label}.png`)
    await page.screenshot({ path: shot, clip: { x: 0, y: 330, width, height: 190 } })
    console.log('wrote', shot)
    if (fix === '1') {
      // Stream 120 chunks into the transcript, one per frame, and count re-measures.
      const writes = await page.evaluate(async sel => {
        // The hook reads each marked box's rect once per re-measure; count those reads.
        let n = 0
        const real = Element.prototype.getBoundingClientRect
        Element.prototype.getBoundingClientRect = function () {
          if (this.hasAttribute('data-guide-keep-clear')) n += 1
          return real.call(this)
        }
        const para = document.querySelector('[data-capture-root] p') || document.body
        for (let i = 0; i < 120; i++) {
          para.append(` chunk ${i}`)
          await new Promise(r => requestAnimationFrame(r))
        }
        await new Promise(r => requestAnimationFrame(r))
        const streaming = n
        // Control: a window resize must be counted, or zero above proves nothing.
        window.dispatchEvent(new Event('resize'))
        await new Promise(r => requestAnimationFrame(r))
        await new Promise(r => requestAnimationFrame(r))
        return { streaming, afterResize: n - streaming }
      }, SLOT)
      console.log(label, 'marked-box reads while streaming:', writes.streaming, '| after a resize:', writes.afterResize)
    }
    await page.close()
  }
}
await browser.close()
await server.close()
