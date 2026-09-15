/**
 * Screenshot + 4px-grid style probe for capture/transcript-row-style.html.
 *
 * Two shells, from website/:
 *   npx vite --host 127.0.0.1 --port 6817 --strictPort
 *   node scripts/capture-transcript-row-style.mjs http://127.0.0.1:6817 \
 *     ../temp-screenshots/row-style-audit
 *
 * The probe is the deliverable, not the image. For every rendered transcript row
 * it reports, measured off the live DOM:
 *
 *   - dx            box offset from the column's text edge (0 = flush)
 *   - padding       T/R/B/L of the row's own painted card box
 *   - height        the row's rendered height
 *   - font/line     of the element that actually holds the row's primary text
 *   - radius        the card's top-left border radius
 *   - pitch         top-to-top distance to the next row
 *   - edges         EVERY internal horizontal and vertical edge offset, measured
 *                   from the ROW'S OWN top-left, so a padding that is a clean 4px
 *                   multiple but lands its content off-grid is still caught
 *
 * Numbers are reported unrounded. A 13px font on a 20px line-height inside a 6px
 * pad produces fractional edges, and that fractional edge IS the defect class --
 * rounding before judging would hide exactly what this probe exists to find.
 *
 * HISTORY / WHY THE SELECTOR IS ASSERTED. This runner previously filtered host
 * row wrappers with `el.className.includes('px-5')`. The row gutter was unified
 * to `px-4` (ChatMessageList.tsx, ChatPage.tsx), so that filter matched nothing:
 * the probe measured ZERO rows and every assertion in it passed vacuously. The
 * gutter class is now read from the DOM instead of hardcoded, and a zero-row
 * frame is a hard FAIL.
 */
import { chromium } from 'playwright'
import { mkdirSync, writeFileSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6817'
const OUT = process.argv[3] || '../temp-screenshots/row-style-audit'

mkdirSync(OUT, { recursive: true })

/** Off-grid amount for a value against the 4px grid, unrounded. */
const offGrid = v => {
  const m = Math.abs(v) % 4
  return Math.min(m, 4 - m)
}
const isGrid = v => offGrid(v) < 1e-6
const n = v => (Number.isInteger(v) ? String(v) : v.toFixed(3).replace(/0+$/, ''))

const browser = await chromium.launch()
let failed = 0
const report = {}

for (const theme of ['dark', 'light']) {
  const ctx = await browser.newContext({
    viewport: { width: 900, height: 1000 },
    deviceScaleFactor: 1,
    colorScheme: theme,
  })
  const page = await ctx.newPage()
  const errors = []
  page.on('pageerror', e => errors.push(String(e)))

  try {
    await page.goto(`${BASE}/capture/transcript-row-style.html?theme=${theme}`, { waitUntil: 'networkidle' })
    await page.waitForSelector('[data-capture-root]', { timeout: 15000 })
    await page.waitForTimeout(900)

    const probe = await page.evaluate(() => {
      const root = document.querySelector('[data-capture-root]')
      const rb = root.getBoundingClientRect()

      // Host row wrappers are the direct children of the list that carry the
      // column geometry. Read the gutter off the DOM rather than hardcoding it:
      // a hardcoded class is how this probe silently went to zero rows before.
      const all = [...root.querySelectorAll('[style*="--mc-content-width"]')]
      const gutters = {}
      for (const el of all) {
        const g = [...el.classList].find(c => /^px-[\d.]+$/.test(c))
        if (g) gutters[g] = (gutters[g] || 0) + 1
      }
      const gutterClass = Object.entries(gutters).sort((a, b) => b[1] - a[1])[0]?.[0] || ''
      const wrappers = all.filter(el => el.classList.contains(gutterClass))

      // The text edge is the wrapper's own content-box left edge: whatever the
      // gutter actually is, this is where a reader sees the column's text start.
      const w0 = wrappers[0]
      let textEdge = null
      if (w0) {
        const b0 = w0.getBoundingClientRect()
        textEdge = b0.x + parseFloat(getComputedStyle(w0).paddingLeft)
      }

      const seen = new Set()
      const rowEls = []
      for (const w of wrappers) {
        const el = w.firstElementChild
        if (!el || seen.has(el)) continue
        seen.add(el)
        const b = el.getBoundingClientRect()
        if (b.width === 0 || b.height === 0) continue
        rowEls.push(el)
      }

      const paints = el => {
        const cs = getComputedStyle(el)
        return cs.borderTopWidth !== '0px'
          || !['rgba(0, 0, 0, 0)', 'transparent'].includes(cs.backgroundColor)
      }

      const out = rowEls.map((el, i) => {
        const b = el.getBoundingClientRect()

        // Walk to the first descendant that actually paints a surface: that is
        // the "card" a reader perceives; some rows put it one level down.
        let card = el
        for (let k = 0; k < 3; k++) {
          if (paints(card)) break
          if (!card.firstElementChild) break
          card = card.firstElementChild
        }
        const ccs = getComputedStyle(card)
        const cb = card.getBoundingClientRect()

        // Font must be read off the element that HOLDS TEXT, not the card box:
        // a container inherits the page 14px while its label is 13px.
        const textEl = (() => {
          const walk = document.createTreeWalker(card, NodeFilter.SHOW_TEXT)
          let t
          while ((t = walk.nextNode())) {
            if ((t.textContent || '').trim() && t.parentElement) return t.parentElement
          }
          return card
        })()
        const tcs = getComputedStyle(textEl)

        // EVERY internal edge, relative to the row's own top-left. Both box
        // sides on both axes, for every descendant that occupies space.
        const hx = new Map()
        const vy = new Map()
        for (const d of el.querySelectorAll('*')) {
          const r = d.getBoundingClientRect()
          if (r.width === 0 && r.height === 0) continue
          const cs = getComputedStyle(d)
          if (cs.visibility === 'hidden' || cs.display === 'none') continue
          const label = d.tagName.toLowerCase()
            + (d.getAttribute('data-testid') ? `[${d.getAttribute('data-testid')}]` : '')
          for (const [map, val, side] of [
            [hx, r.x - b.x, 'L'], [hx, r.right - b.x, 'R'],
            [vy, r.y - b.y, 'T'], [vy, r.bottom - b.y, 'B'],
          ]) {
            const key = val.toFixed(3)
            if (!map.has(key)) map.set(key, { off: val, who: `${label}.${side}` })
          }
        }

        return {
          index: i,
          tag: el.tagName.toLowerCase(),
          testid: el.getAttribute('data-testid') || card.getAttribute('data-testid') || '',
          cls: (typeof el.className === 'string' ? el.className : '').slice(0, 60),
          cardIsRow: card === el,
          dx: b.x - textEdge,
          width: b.width,
          height: b.height,
          cardTop: cb.y - b.y,
          cardLeft: cb.x - b.x,
          padTop: parseFloat(ccs.paddingTop),
          padRight: parseFloat(ccs.paddingRight),
          padBottom: parseFloat(ccs.paddingBottom),
          padLeft: parseFloat(ccs.paddingLeft),
          radius: parseFloat(ccs.borderTopLeftRadius),
          fontSize: parseFloat(tcs.fontSize),
          lineHeight: tcs.lineHeight === 'normal' ? null : parseFloat(tcs.lineHeight),
          absTop: b.y,
          hEdges: [...hx.values()].sort((a, c) => a.off - c.off),
          vEdges: [...vy.values()].sort((a, c) => a.off - c.off),
        }
      })

      // Pitch: top-to-top to the next row, in DOM order.
      for (let i = 0; i < out.length; i++) {
        out[i].pitch = i + 1 < out.length ? out[i + 1].absTop - out[i].absTop : null
      }

      return {
        gutterClass,
        gutters,
        wrapperCount: wrappers.length,
        candidateCount: all.length,
        rootWidth: rb.width,
        textEdgeRelRoot: textEdge === null ? null : textEdge - rb.x,
        rows: out,
      }
    })

    await page.locator('[data-capture-root]').screenshot({ path: `${OUT}/${theme}.png` })

    report[theme] = { ...probe, errors }

    let frameFailed = 0
    if (probe.rows.length === 0) {
      frameFailed++
      console.error(`FAIL ${theme}: probe found ZERO rows (gutter='${probe.gutterClass}', `
        + `candidates=${probe.candidateCount}) — every assertion below would pass vacuously`)
    }
    for (const r of probe.rows) {
      if (Math.abs(r.dx) > 1e-6) {
        frameFailed++
        console.error(`FAIL ${theme}: row ${r.testid || r.tag} box starts at ${n(r.dx)}px, `
          + 'not on the column text edge')
      }
    }
    if (errors.length) {
      frameFailed++
      console.error(`FAIL ${theme}: ${errors.length} page error(s)\n  ${errors.join('\n  ')}`)
    }
    failed += frameFailed

    // ---- 4px-grid report -------------------------------------------------
    console.log(`\n=== ${theme} — ${probe.rows.length} rows (gutter ${probe.gutterClass}, `
      + `text edge +${n(probe.textEdgeRelRoot)}px) ===`)
    const head = 'row'.padEnd(30) + 'h'.padStart(8) + 'pitch'.padStart(8)
      + 'padT/R/B/L'.padStart(20) + 'radius'.padStart(8) + 'font/line'.padStart(12)
    console.log(head)
    for (const r of probe.rows) {
      const pads = [r.padTop, r.padRight, r.padBottom, r.padLeft].map(n).join('/')
      console.log(
        (r.testid || r.tag + '.' + r.index).padEnd(30)
        + n(r.height).padStart(8)
        + (r.pitch === null ? '—' : n(r.pitch)).padStart(8)
        + pads.padStart(20)
        + n(r.radius).padStart(8)
        + `${n(r.fontSize)}/${r.lineHeight === null ? 'normal' : n(r.lineHeight)}`.padStart(12),
      )
    }

    console.log(`\n--- ${theme}: 4px verdict per row ---`)
    const pitches = probe.rows.map(r => r.pitch).filter(p => p !== null)
    const pitchSet = [...new Set(pitches.map(p => p.toFixed(3)))]
    for (const r of probe.rows) {
      const padBad = [['padTop', r.padTop], ['padRight', r.padRight],
        ['padBottom', r.padBottom], ['padLeft', r.padLeft]]
        .filter(([, v]) => !isGrid(v))
        .map(([k, v]) => `${k}=${n(v)} (off by ${n(offGrid(v))})`)
      const edgeBad = [...r.hEdges.map(e => ({ ...e, ax: 'x' })), ...r.vEdges.map(e => ({ ...e, ax: 'y' }))]
        .filter(e => !isGrid(e.off))
      const name = r.testid || r.tag + '.' + r.index
      const c1 = padBad.length === 0
      const c2 = edgeBad.length === 0
      console.log(`${name.padEnd(30)} padding:${c1 ? 'PASS' : 'FAIL'}  edges:${c2 ? 'PASS' : 'FAIL'}`
        + `  h=${n(r.height)}${isGrid(r.height) ? '' : ` (off ${n(offGrid(r.height))})`}`
        + `  edges=${r.hEdges.length}h/${r.vEdges.length}v`)
      if (padBad.length) console.log('    pad  ' + padBad.join('; '))
      for (const e of edgeBad.slice(0, 14)) {
        console.log(`    ${e.ax}    ${e.who} at ${n(e.off)} (off by ${n(offGrid(e.off))})`)
      }
      if (edgeBad.length > 14) console.log(`    ... ${edgeBad.length - 14} more off-grid edges`)
    }
    console.log(`\npitch values (${theme}): ${pitchSet.join(', ')} — `
      + `${pitchSet.length === 1 ? 'CONSTANT' : 'NOT CONSTANT'}`)
    const pitchOff = pitchSet.filter(p => !isGrid(parseFloat(p)))
    if (pitchOff.length) console.log(`pitch off-grid: ${pitchOff.join(', ')}`)
  } catch (err) {
    failed++
    report[theme] = { error: err.message, errors }
    console.error(`FAIL ${theme}: ${err.message}`)
  }
  await ctx.close()
}

await browser.close()
writeFileSync(`${OUT}/probe.json`, JSON.stringify(report, null, 2))
console.log(`\nwrote ${OUT}/probe.json`)
if (failed) {
  console.error(`\n${failed} assertion(s) failed.`)
  process.exit(1)
}
