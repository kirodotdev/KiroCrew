/**
 * Where exactly does a tool row's content start?
 *
 * Walks the wrapper -> pill -> icon chain for every tool row and reports each
 * element's left offset from the column's text edge, so the inset can be
 * attributed to the element that actually introduces it rather than guessed
 * from reading class strings.
 *
 *   npx vite --host 127.0.0.1 --port 6819 --strictPort
 *   node scripts/probe-tool-call-inset.mjs http://127.0.0.1:6819
 */
import { chromium } from 'playwright'

const BASE = process.argv[2] || 'http://127.0.0.1:6819'

const browser = await chromium.launch()
const ctx = await browser.newContext({ viewport: { width: 900, height: 700 }, colorScheme: 'dark' })
const page = await ctx.newPage()
await page.goto(`${BASE}/capture/tool-call-states.html?theme=dark`, { waitUntil: 'networkidle' })
await page.waitForSelector('[data-capture-root]', { timeout: 15000 })
await page.waitForTimeout(1000)

const out = await page.evaluate(() => {
  const root = document.querySelector('[data-capture-root]')
  const rb = root.getBoundingClientRect()
  // Derive the gutter from the DOM rather than hardcoding it. A hardcoded class is
  // how this probe's sibling silently went to zero rows: it kept selecting on
  // `px-5` after the gutter was unified to `px-4`, so its assertion loop never ran
  // and it exited 0. Reading the real padding cannot drift out of date.
  const all = [...root.querySelectorAll('[style*="--mc-content-width"]')]
  const gutter = {}
  for (const el of all) {
    const g = [...el.classList].find(c => /^px-[\d.]+$/.test(c))
    if (g) gutter[g] = (gutter[g] || 0) + 1
  }
  const gutterClass = Object.entries(gutter).sort((a, b) => b[1] - a[1])[0]?.[0] || ''
  const wrappers = all.filter(el => el.classList.contains(gutterClass))
  if (wrappers.length === 0) throw new Error('probe matched zero row wrappers — the selector has drifted')
  const w0 = wrappers[0]
  const textEdge = w0.getBoundingClientRect().x + parseFloat(getComputedStyle(w0).paddingLeft)
  const dx = el => Math.round(el.getBoundingClientRect().x - textEdge)


  return wrappers.map(w => {
    const chain = []
    let el = w
    let depth = 0
    while (el && depth < 6) {
      const cs = getComputedStyle(el)
      chain.push({
        tag: el.tagName.toLowerCase(),
        cls: (typeof el.className === 'string' ? el.className : '').slice(0, 52),
        dx: dx(el),
        padL: cs.paddingLeft,
      })
      if (el.tagName === 'BUTTON') {
        const svg = el.querySelector('svg')
        if (svg) chain.push({ tag: 'svg(icon)', cls: '', dx: dx(svg), padL: '-' })
        break
      }
      el = el.firstElementChild
      depth++
    }
    return { label: (w.textContent || '').trim().slice(0, 30), chain }
  })
})

for (const row of out) {
  console.log(`\n${row.label}`)
  for (const c of row.chain) {
    console.log(`   dx=${String(c.dx).padStart(4)}  pad-l=${String(c.padL).padStart(6)}  ${c.tag.padEnd(10)} ${c.cls}`)
  }
}

await browser.close()
