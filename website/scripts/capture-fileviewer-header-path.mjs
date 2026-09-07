/**
 * Screenshots of the file-viewer header path affordance (#7900), via the
 * capture/fileviewer-header-path harness which mounts the real
 * FileHeaderBreadcrumb.
 *
 * Four frames: {default, keyboard-focus} x {dark, light}. The focus frames
 * document the NEW keyboard route (a focus ring + the aria-labelled group),
 * asserted by role+name before shooting so a blank frame cannot pass silently.
 *
 * Usage: node scripts/capture-fileviewer-header-path.mjs <viteBase> <outDir>
 */
import { chromium } from 'playwright'

const base = process.argv[2] || 'http://127.0.0.1:5199'
const outDir = process.argv[3] || '../temp-screenshots/7900-fileviewer-header-path'
const NESTED = '/home/user/workplace/MyWorkspace/src/PackageName/src/PackageName/index.ts'

const b = await chromium.launch()
try {
  for (const theme of ['dark', 'light']) {
    for (const focus of [false, true]) {
      const ctx = await b.newContext({ viewport: { width: 720, height: 240 }, deviceScaleFactor: 2 })
      const p = await ctx.newPage()
      const q = `theme=${theme}${focus ? '&focus=1' : ''}`
      await p.goto(`${base}/capture/fileviewer-header-path.html?${q}`, { waitUntil: 'networkidle' })
      const group = p.getByRole('group', { name: NESTED })
      await group.waitFor({ state: 'visible', timeout: 15_000 })
      if (focus) {
        // Prove the keyboard route: focus lands on the aria-labelled group.
        await group.focus()
        const focused = await group.evaluate(el => el === document.activeElement)
        if (!focused) throw new Error('focus frame: breadcrumb did not take focus')
      }
      const name = `header-path-${theme}-${focus ? 'focused' : 'default'}.png`
      await p.screenshot({ path: `${outDir}/${name}` })
      console.log(`captured ${outDir}/${name} (role=group name asserted${focus ? ', focus asserted' : ''})`)
      await ctx.close()
    }
  }
  // Narrowest panel the viewer allows (SIDE_PANEL_MIN_W=320) inside a WIDE
  // window, so the panel's overflow-hidden is what could clip -- the real case.
  // Assert the readout's right edge stays within the panel before shooting.
  {
    const ctx = await b.newContext({ viewport: { width: 960, height: 260 }, deviceScaleFactor: 2 })
    const p = await ctx.newPage()
    await p.goto(`${base}/capture/fileviewer-header-path.html?theme=dark&focus=1&panelW=320`, { waitUntil: 'networkidle' })
    const group = p.getByRole('group', { name: NESTED })
    await group.waitFor({ state: 'visible', timeout: 15_000 })
    await group.focus()
    const readout = p.getByTestId('file-header-full-path')
    await readout.waitFor({ state: 'visible', timeout: 15_000 })
    const fits = await readout.evaluate(el => {
      const panel = el.closest('div[style*="overflow"]')
      const r = el.getBoundingClientRect(), pr = panel.getBoundingClientRect()
      return r.right <= pr.right + 0.5 && r.left >= pr.left - 0.5
    })
    if (!fits) throw new Error('narrow frame: readout escapes the 320px panel edge')
    const name = 'header-path-dark-focused-narrow.png'
    await p.screenshot({ path: `${outDir}/${name}` })
    console.log(`captured ${outDir}/${name} (panel 320px, readout within panel asserted)`)
    await ctx.close()
  }
  // Diff mode at both widths, to show the +N/-N badge keeps its position beside
  // the filename (UX finding: the layout change must not silently move it).
  for (const panelW of [460, 320]) {
    const ctx = await b.newContext({ viewport: { width: 720, height: 200 }, deviceScaleFactor: 2 })
    const p = await ctx.newPage()
    await p.goto(`${base}/capture/fileviewer-header-path.html?theme=dark&diff=1&panelW=${panelW}`, { waitUntil: 'networkidle' })
    await p.getByRole('group', { name: NESTED }).waitFor({ state: 'visible', timeout: 15_000 })
    const name = `header-path-dark-diffmode-${panelW}.png`
    await p.screenshot({ path: `${outDir}/${name}` })
    console.log(`captured ${outDir}/${name} (diff mode, panel ${panelW}px)`)
    await ctx.close()
  }
} finally {
  await b.close()
}
