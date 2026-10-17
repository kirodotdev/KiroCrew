/**
 * Pass/fail sweep for the desktop top bar's measured collapse ladders
 * (`.topbar.tb-measured`, src/lib/useTopbarCollapse.ts), plus before/after
 * contact sheets.
 *
 * Drives the shared top-bar capture entry (capture/topbar-search-variants.html)
 * for 24 state combinations: English / Chinese, no remote crews / crew switcher /
 * switcher + 3 pinned crews, update pill on / off, nightly (with the "Report
 * problem" chip) / stable. Each one is swept from MAX down to MIN window width in
 * STEP px in both layouts, split into ranges where the bar looks the same, and
 * photographed at both ends of every range.
 *
 * The MEASURED form must hold at every width, or the script exits 1:
 *  - the search is on the header's centre line at its full window-derived width
 *  - the connection dot is on screen; neither group clips; the groups do not
 *    overlap the search cell; the header does not overflow
 *  - with pinned crews, the dropdown stays glued to the last visible chip
 *  - pinned crews never move the actions group's ladder, and move the identity
 *    group's by no more than the one 4px gap their row takes: every width
 *    settles on the level the same state without pins has at that width or 4px
 *    narrower (the pins are cut instead)
 *  - revisiting a width from another one reads the same state (path-independent)
 *  - no page errors
 * The container-query ladder (before) is photographed for comparison and never
 * asserted.
 *
 * Usage (from website/):
 *   npx vite --host 127.0.0.1 --port 6811 --strictPort   # in another shell
 *   node scripts/capture-topbar-flow.mjs http://127.0.0.1:6811 ../temp-screenshots/topbar-flow
 * STEP=4 node scripts/... for a quicker, coarser sweep (STEP must divide 4).
 */
import { chromium } from 'playwright'
import { mkdirSync, writeFileSync } from 'node:fs'
import { resolve } from 'node:path'

const BASE = process.argv[2] || 'http://127.0.0.1:6811'
const OUT = resolve(process.argv[3] || '../temp-screenshots/topbar-flow')
const MIN = 768, MAX = 1920, STEP = Number(process.env.STEP) || 2, POOL = 8
// The pinned-crew check compares each width with the one 4px narrower, so every
// such width must be sampled: a STEP that does not divide 4 would pass it vacuously.
if (![1, 2, 4].includes(STEP)) {
  console.error(`STEP=${STEP}: use 1, 2 or 4 (the pinned-crew check samples the width 4px narrower)`)
  process.exit(2)
}

const combos = []
for (const lang of ['en', 'zh']) for (const left of ['none', 'crew', 'pins'])
  for (const update of [false, true]) for (const chip of [true, false]) combos.push({ lang, left, update, chip })
const comboId = c => `${c.lang}-${c.left}-${c.update ? 'update' : 'noupdate'}-${c.chip ? 'nightly' : 'stable'}`
const comboTitle = c => [
  c.lang === 'en' ? 'English' : 'Chinese (zh-CN)',
  { none: 'no remote crews', crew: 'crew switcher', pins: 'crew switcher + 3 pinned crews' }[c.left],
  c.update ? 'update pill' : 'no update pill',
  c.chip ? 'nightly (Report problem chip)' : 'stable (no chip)',
].join(' · ')
function url(c, layout) {
  const p = new URLSearchParams({ pill: 'real', form: 'desktop' })
  if (c.lang === 'en') p.set('lang', 'en')
  if (c.left === 'none') p.set('crews', 'off')
  if (c.left === 'pins') p.set('pins', '3')
  if (c.update) p.set('update', 'on')
  if (!c.chip) p.set('chip', 'off')
  if (layout === 'measured') p.set('layout', 'measured')
  return `${BASE}/capture/topbar-search-variants.html?${p}`
}

// Runs in the page.
function probe() {
  const h = document.querySelector('[data-topbar]')
  const hr = h.getBoundingClientRect()
  const cell = h.children[1].getBoundingClientRect()
  const left = h.querySelector(':scope > .tb-left'), right = h.querySelector(':scope > .tb-right')
  const lr = left.getBoundingClientRect(), rr = right.getBoundingClientRect()
  const shown = e => !!e && e.getClientRects().length > 0 && e.getBoundingClientRect().width > 0
  const vis = (root, sel) => [...root.querySelectorAll(sel)].some(shown)
  const contents = g => g.querySelector(':scope > .tb-measure') ?? g
  const extent = g => {
    const ks = [...contents(g).children].filter(shown).map(k => k.getBoundingClientRect())
    return ks.length ? { l: Math.min(...ks.map(k => k.left)), r: Math.max(...ks.map(k => k.right)) } : null
  }
  const re = extent(right), le = extent(left)
  const capsule = right.querySelector('.tb-capsule')
  // The capsule is a Liquid Glass host (#15523) whose effect layers come first,
  // so the dot is its first child that is not a layer.
  const segs = capsule ? [...capsule.children].filter(k => !k.hasAttribute('data-liquid-glass-layer')) : []
  const dot = segs[0]?.getBoundingClientRect()
  const row = h.querySelector('[data-testid=crew-chip-row]')
  const trigger = row?.nextElementSibling
  return {
    level: `${h.dataset.tbLeft ?? '-'}/${h.dataset.tbRight ?? '-'}`,
    leftLevel: h.dataset.tbLeft ?? null,
    rightLevel: h.dataset.tbRight ?? null,
    offset: Math.round((cell.left + cell.width / 2) - (hr.left + hr.width / 2)),
    centreW: Math.round(cell.width * 10) / 10,
    clampW: Math.round(Math.min(480, Math.max(240, 0.22 * window.innerWidth))),
    numbers: vis(h, '.tb-drop-metrics'),
    labels: vis(h, '.tb-drop-feedback-label'),
    arrows: vis(h, '.tb-drop-navhistory'),
    credits: vis(h, '.tb-drop-usage'),
    feedback: vis(h, '.tb-drop-feedback [data-feedback-pill]'),
    crews: !!left.querySelector('.tb-crew-active-chip'),
    crewNames: vis(left, '.tb-drop-crew-name'),
    activeChip: vis(left, '.tb-crew-active-chip'),
    capsuleFull: segs.slice(1).some(shown),
    dot: !!dot && dot.width > 0 && dot.left >= rr.left - 0.5 && dot.right <= rr.right + 0.5,
    rightClip: !!re && (re.l < rr.left - 0.5 || re.r > rr.right + 0.5),
    leftClip: (!!le && (le.l < lr.left - 0.5 || le.r > lr.right + 0.5)) || left.scrollWidth > left.clientWidth + 1,
    chipsCut: !!row && row.dataset.cut === 'true',
    dropdownGap: row && trigger ? Math.round(trigger.getBoundingClientRect().left - row.getBoundingClientRect().right) : null,
    overlap: lr.right > cell.left + 0.5 || cell.right > rr.left + 0.5,
    headerOverflow: h.scrollWidth > h.clientWidth + 1,
  }
}
const centred = r => Math.abs(r.offset) <= 1 && Math.abs(r.centreW - r.clampW) <= 1
const sig = r => JSON.stringify([r.level, centred(r), r.numbers, r.labels, r.arrows, r.credits, r.feedback, r.crewNames,
  r.activeChip, r.capsuleFull, r.dot, r.rightClip, r.leftClip, r.chipsCut, r.overlap, r.headerOverflow])
function describe(r, layout) {
  const c = []
  if (!r.numbers) c.push('metrics → waveform')
  if (r.feedback && !r.labels) c.push('feedback labels → icons')
  if (!r.arrows) c.push('arrows hidden')
  if (!r.credits) c.push('credits → coin')
  if (!r.feedback) c.push('feedback pill hidden')
  if (r.crews && r.activeChip && !r.crewNames) c.push('crew names hidden')
  if (!r.capsuleFull) c.push('capsule → dot only')
  if (r.crews && !r.activeChip) c.push('active crew chip hidden')
  if (r.chipsCut) c.push('pinned chips cut')
  const d = []
  if (!r.dot) d.push('DOT CLIPPED')
  if (r.rightClip) d.push('RIGHT GROUP CLIPPED')
  if (r.leftClip) d.push('LEFT GROUP CLIPPED')
  if (r.overlap) d.push('OVERLAP')
  if (r.headerOverflow) d.push('HEADER OVERFLOW')
  if (layout === 'measured' && Math.abs(r.offset) > 1) d.push(`SEARCH ${r.offset}px OFF CENTRE`)
  if (layout === 'measured' && Math.abs(r.centreW - r.clampW) > 1) d.push(`SEARCH ${r.centreW}px, NOT ${r.clampW}px`)
  if (layout === 'measured' && r.dropdownGap !== null && r.dropdownGap > 4) d.push(`DROPDOWN ${r.dropdownGap}px FROM ROW`)
  return { collapsed: c, defects: d }
}
const settle = (page, n = 3) => page.evaluate(n => new Promise(res => {
  const step = k => (k === 0 ? res() : requestAnimationFrame(() => step(k - 1))); step(n)
}), n)

async function run(browser, c, layout) {
  const id = comboId(c)
  const dir = `${OUT}/frames/${id}`
  mkdirSync(dir, { recursive: true })
  const page = await browser.newPage({ viewport: { width: MAX, height: 120 } })
  const errors = []
  page.on('pageerror', e => errors.push(String(e.message || e)))
  page.on('console', m => { if (m.type() === 'error') errors.push(m.text()) })
  await page.goto(url(c, layout))
  await page.waitForSelector('[data-topbar]')
  await page.evaluate(() => document.fonts.ready)
  await page.waitForTimeout(300)
  const levels = { left: new Map(), right: new Map() }
  const rows = []
  for (let w = MAX; w >= MIN; w -= STEP) {
    await page.setViewportSize({ width: w, height: 120 })
    await settle(page, 2)
    const r = { w, ...(await page.evaluate(probe)) }
    levels.left.set(w, r.leftLevel)
    levels.right.set(w, r.rightLevel)
    rows.push(r)
  }
  const segs = []
  for (const r of rows) {
    const s = sig(r)
    const last = segs[segs.length - 1]
    if (last && last.sig === s) { last.lo = r.w; last.rows.push(r) } else segs.push({ sig: s, hi: r.w, lo: r.w, rows: [r] })
  }
  const mismatches = []
  for (const s of segs) {
    s.frames = []
    for (const w of s.hi === s.lo ? [s.hi] : [s.hi, s.lo]) {
      await page.setViewportSize({ width: w, height: 120 })
      await settle(page, 3)
      const r = await page.evaluate(probe)
      if (sig(r) !== s.sig) mismatches.push({ w, sweep: s.sig, revisit: sig(r) })
      const file = `${dir}/${layout}-${w}.png`
      await page.screenshot({ path: file, clip: { x: 0, y: 0, width: w, height: 42 } })
      s.frames.push({ w, file })
    }
    const defectRows = s.rows.map(r => describe(r, layout).defects).filter(d => d.length)
    const offs = s.rows.map(r => r.offset), cws = s.rows.map(r => r.centreW)
    Object.assign(s, { level: s.rows[0].level, ...describe(s.rows[0], layout),
      defects: [...new Set(defectRows.flat())],
      offset: [Math.min(...offs), Math.max(...offs)], centreW: [Math.min(...cws), Math.max(...cws)] })
    delete s.rows
  }
  await page.close()
  return { id, layout, segs, mismatches, errors, levels }
}

const browser = await chromium.launch()
const tasks = combos.flatMap(c => ['measured', 'current'].map(layout => ({ c, layout })))
const results = []
let next = 0
await Promise.all(Array.from({ length: POOL }, async () => {
  while (next < tasks.length) {
    const t = tasks[next++]
    results.push({ ...(await run(browser, t.c, t.layout)), combo: t.c })
  }
}))
const find = (c, layout) => results.find(x => x.id === comboId(c) && x.layout === layout)

// Verdict: measured form only.
const failures = []
for (const c of combos) {
  const f = find(c, 'measured')
  for (const s of f.segs) if (s.defects.length) failures.push(`${f.id} ${s.hi}–${s.lo}px: ${s.defects.join(', ')}`)
  for (const m of f.mismatches) failures.push(`${f.id} ${m.w}px: state differs on revisit`)
  for (const e of f.errors) failures.push(`${f.id}: page error: ${e}`)
  if (c.left === 'pins') {
    // The row contributes no width of its own, only the one 4px flex gap that
    // separates it from the active chip, so with pins the identity group must
    // behave exactly as it does without them at most 4px narrower. The last 4px of
    // the sweep have no narrower no-pins reading (below MIN is the phone layout),
    // so they are compared only where both readings exist. The actions group's
    // track does not depend on the identity group at all.
    const bare = find({ ...c, left: 'crew' }, 'measured')
    const moved = [...f.levels.left]
      .filter(([w, l]) => l !== bare.levels.left.get(w) && bare.levels.left.has(w - 4) && l !== bare.levels.left.get(w - 4))
      .map(([w]) => w)
    if (moved.length) failures.push(`${f.id}: pinned crews moved the identity ladder at ${moved.length} widths (first ${moved[0]}px)`)
    const movedRight = [...f.levels.right].filter(([w, l]) => l !== bare.levels.right.get(w)).map(([w]) => w)
    if (movedRight.length) failures.push(`${f.id}: pinned crews moved the actions ladder at ${movedRight.length} widths (first ${movedRight[0]}px)`)
  }
}

mkdirSync(`${OUT}/sheets`, { recursive: true })
writeFileSync(`${OUT}/summary.json`, JSON.stringify({
  failures,
  results: results.map(({ combo, levels, ...r }) => ({ combo: comboId(combo), title: comboTitle(combo), ...r })),
}, null, 1))

// Contact sheets: one per combination, the measured form first, then the container ladder.
const esc = s => s.replace(/&/g, '&amp;').replace(/</g, '&lt;')
function segLabel(s, layout) {
  const parts = [s.hi === s.lo ? `${s.hi}px` : `${s.hi}–${s.lo}px`]
  if (layout === 'measured') parts.push(`levels ${s.level} (identity/actions)`)
  parts.push(s.collapsed.length ? s.collapsed.join(', ') : 'everything shown')
  return esc(parts.join(' · ')) + (s.defects.length ? ` <b class="bad">${esc(s.defects.join(' · '))}</b>` : '')
}
const sheet = await browser.newPage({ viewport: { width: MAX + 40, height: 400 } })
for (const c of combos) {
  const id = comboId(c)
  const sec = layout => `<h2>${layout === 'measured' ? 'Measured ladders (after)' : 'Container ladder (before)'}</h2>` +
    find(c, layout).segs.map(s => `<div class="seg"><div class="lbl">${segLabel(s, layout)}</div>` +
      s.frames.map(f => `<div class="fr"><span>${f.w}px</span><img src="file://${f.file}" width="${f.w}" height="42"></div>`).join('') +
      `</div>`).join('')
  writeFileSync(`${OUT}/sheets/${id}.html`, `<!doctype html><meta charset="utf-8"><style>
    body{margin:16px;background:#101014;color:#d6d6de;font:13px/1.35 system-ui,sans-serif}
    h1{font-size:17px;margin:0 0 4px} h2{font-size:15px;margin:18px 0 6px;color:#fff}
    .seg{margin:0 0 10px;padding:6px 8px;border-left:3px solid #3a3a48} .lbl{margin:0 0 4px} .bad{color:#ff6b6b}
    .fr{display:flex;align-items:center;gap:8px;margin:2px 0} .fr span{width:52px;color:#8a8a99;flex:none;text-align:right}
    img{display:block;outline:1px solid #2a2a33}
  </style><h1>${esc(comboTitle(c))}</h1><div>Window widths ${MAX}→${MIN}px in ${STEP}px steps; each range shows the bar at both ends.</div>
  ${sec('measured')}${sec('current')}`)
  await sheet.goto(`file://${OUT}/sheets/${id}.html`)
  await sheet.waitForTimeout(150)
  await sheet.screenshot({ path: `${OUT}/sheets/${id}.png`, fullPage: true })
}
await browser.close()

for (const c of combos) {
  const f = find(c, 'measured'), cur = find(c, 'current')
  const bad = cur.segs.filter(s => s.defects.length).map(s => `${s.hi}–${s.lo}`).join(', ')
  console.log(`${comboId(c).padEnd(28)} measured: ${f.segs.length} ranges · container ladder clips at: ${bad || 'none'}`)
}
if (failures.length) {
  console.error(`\nFAIL: ${failures.length} problem(s) in the measured form\n  ${failures.join('\n  ')}`)
  process.exit(1)
}
console.log(`\nPASS: the measured form holds across ${combos.length} states, ${MAX}→${MIN}px @${STEP}px. Sheets in ${OUT}/sheets`)
