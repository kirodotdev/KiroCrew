/**
 * Gate: no surface may vanish in kiro-light because it has no edge of its own.
 *
 * `kiro-light` paints the canvas `#ffffff`, the same value as `--card` (and
 * `--bg-elevated`), so a borderless `bg-card` box on the page draws nothing in
 * that one theme while every other theme shows a card. The rule is written in
 * docs/theming-contract.md ("A card must carry its own edge"). A class-attribute
 * grep cannot enforce it: an edge can come from an inline `style`, a parent's
 * `divide-*`, or a ring/shadow utility, and an opaque cover that matches its
 * parent in every theme is not a card at all. So this renders the real built SPA
 * (website/dist) over loopback with every /api/** call answered from fixtures,
 * opens each route in kiro-light, and asks the computed styles.
 *
 * For every painted element (non-transparent background, at least 16x16 px) the
 * probe composites its background over the nearest painted ancestors, then:
 *
 *  - in kiro-light it must be indistinguishable from what is behind it (every
 *    sRGB channel within MIN_DELTA), AND
 *  - in a reference theme (`light`, the default palette of the same polarity
 *    where `--card` and `--bg` differ, and `kiro-dark`) it must be distinct, AND
 *  - in kiro-light it must have no visible edge: no border side, painting
 *    `box-shadow` layer (Tailwind's `ring-*` is a box-shadow) or outline whose
 *    colour differs from the backdrop.
 *
 * Such an element is a card in every reference theme and nothing in kiro-light:
 * it is reported as `route  selector` and the script exits 1. Surfaces equal to
 * their parent in every theme (opaque covers, redundant fills) are not reported,
 * because their look does not depend on the theme.
 *
 * The reference themes are applied by switching `data-theme` / `data-mode` on
 * the root in place, with transitions and animations off, so both readings come
 * from the same DOM. Every route must render: a route whose page shows an error
 * boundary, or whose main pane holds no text, fails the gate rather than
 * passing with nothing checked.
 *
 * A deliberate exception goes in EXEMPT below with the reason, keyed on a CSS
 * selector the element matches; an exemption that matches nothing fails the gate
 * so the list cannot rot.
 *
 * Usage: node scripts/check-kiro-light-cards.mjs [--dist <dir>] [--json <file>]
 */
import { chromium } from 'playwright'
import { writeFileSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { json, makeFixedApi, handleBootRoute } from './lib/boot-api.mjs'

// mise's node injects LD_LIBRARY_PATH at its own bundled libstdc++, which is
// older than the system Mesa needs; children inherit it, so scrub it here.
delete process.env.LD_LIBRARY_PATH

const args = process.argv.slice(2)
const argValue = name => {
  const i = args.indexOf(name)
  return i >= 0 ? args[i + 1] : undefined
}
const DIST = argValue('--dist')
const JSON_OUT = argValue('--json')

const PROJECT = '/home/user/workspace/KiroCrew'
const SLOT = 'chat-kiro-light-cards'
const VIEWPORT = { width: 1440, height: 900 }
/** Max per-channel sRGB difference (0-255) that still reads as the same colour. */
const MIN_DELTA = 3
/** Smallest box (px, both sides) the probe inspects; below it is glyph-sized. */
const MIN_SIDE = 16
const REFERENCE_THEMES = [
  { theme: 'light', mode: 'light' },
  { theme: 'kiro-dark', mode: 'dark' },
]

/**
 * The routes the gate renders. `ready` is text the page shows once it has
 * rendered past its loading state, so a blank or crashed page cannot pass.
 */
const ROUTES = [
  { path: '/', name: 'chat', ready: 'Reply in the composer' },
  { path: '/settings/overview', name: 'settings-overview' },
  { path: '/settings/chat', name: 'settings-chat' },
  { path: '/settings/display/theme', name: 'settings-display-theme' },
  { path: '/settings/security', name: 'settings-security' },
  { path: '/schedule', name: 'schedule' },
  { path: '/artifacts', name: 'artifacts' },
  { path: '/capabilities?tab=knowledge', name: 'knowledge' },
  { path: '/apps/library', name: 'apps-library' },
  { path: '/members', name: 'members' },
]

/**
 * Deliberate borderless surfaces: `{ selector, why }`. Each must match at least
 * one reported element on some route, or the gate fails as stale.
 */
const EXEMPT = []

/** Planted on every route: `edge` is the box's own edge CSS, `reported` whether the probe must flag it. */
const CANARIES = [
  { name: 'bare', edge: 'border:none', reported: true },
  { name: 'shadow-reset', edge: 'border:none;box-shadow:0 0 0 0 transparent,0 0 #0000', reported: true },
  { name: 'edged', edge: 'border:1px solid var(--border)', reported: false },
  { name: 'ringed', edge: 'border:none;box-shadow:0 0 0 1px var(--border)', reported: false },
]

const t0 = Math.floor(Date.now() / 1000) - 600
const slots = [{
  key: SLOT, title: 'kiro-light card sweep', running: false, unread: 0,
  messages: 2, agent: 'kirocrew', memory_mode: 'persistent', project: PROJECT,
  modified: Math.floor(Date.now() / 1000), source_links: [], source_links_total: 0,
}]
const detail = {
  running: false, has_more: false, total: 2, key: SLOT, title: 'kiro-light card sweep', queue: [],
  messages: [
    { role: 'user', ts: t0, content: 'Summarise the open review comments.' },
    { role: 'assistant', ts: t0 + 8, content: 'Two comments are open. Reply in the composer when you want me to address them.' },
  ],
}

/** Runs in the page: returns every element that is a card in a reference theme and nothing in kiro-light. */
function probe({ references, minDelta, minSide }) {
  const root = document.documentElement
  const canvas = document.createElement('canvas')
  canvas.width = canvas.height = 1
  const ctx = canvas.getContext('2d', { willReadFrequently: true })
  const cache = new Map()
  // Canvas normalises any CSS colour syntax (rgb, color(srgb), oklch, color-mix
  // results) to unpremultiplied sRGB bytes.
  const rgba = str => {
    if (!str || str === 'transparent') return { r: 0, g: 0, b: 0, a: 0 }
    if (cache.has(str)) return cache.get(str)
    ctx.clearRect(0, 0, 1, 1)
    ctx.fillStyle = '#000'
    ctx.fillStyle = str
    ctx.fillRect(0, 0, 1, 1)
    const [r, g, b, a] = ctx.getImageData(0, 0, 1, 1).data
    const c = { r, g, b, a: a / 255 }
    cache.set(str, c)
    return c
  }
  const over = (top, under) => ({
    r: top.r * top.a + under.r * (1 - top.a),
    g: top.g * top.a + under.g * (1 - top.a),
    b: top.b * top.a + under.b * (1 - top.a),
    a: 1,
  })
  const delta = (x, y) => Math.max(Math.abs(x.r - y.r), Math.abs(x.g - y.g), Math.abs(x.b - y.b))
  const backdrop = el => {
    const layers = []
    for (let p = el.parentElement; p; p = p.parentElement) {
      const c = rgba(getComputedStyle(p).backgroundColor)
      if (c.a > 0) layers.push(c)
      if (c.a >= 1) break
    }
    let acc = { r: 255, g: 255, b: 255, a: 1 }
    for (let i = layers.length - 1; i >= 0; i--) acc = over(layers[i], acc)
    return acc
  }
  const shown = el => {
    for (let p = el; p && p !== root; p = p.parentElement) {
      const s = getComputedStyle(p)
      if (s.visibility === 'hidden' || s.display === 'none' || Number(s.opacity) === 0) return false
    }
    return true
  }
  const hasEdge = (s, under) => {
    for (const side of ['Top', 'Right', 'Bottom', 'Left']) {
      if (parseFloat(s[`border${side}Width`]) > 0 && s[`border${side}Style`] !== 'none') {
        const bc = rgba(s[`border${side}Color`])
        if (bc.a > 0 && delta(over(bc, under), under) >= minDelta) return true
      }
    }
    if (shadowPaints(s.boxShadow, under)) return true
    if (s.outlineStyle === 'none' || !(parseFloat(s.outlineWidth) > 0)) return false
    const oc = rgba(s.outlineColor)
    return oc.a > 0 && delta(over(oc, under), under) >= minDelta
  }
  // `shadow-none` / `ring-0` compute to transparent, zero-sized layers rather
  // than `none`, so each layer must both have a colour that shows against the
  // backdrop and a non-zero offset, blur or spread before it counts as an edge.
  const shadowPaints = (value, under) => {
    if (!value || value === 'none') return false
    const layers = []
    let depth = 0
    let start = 0
    for (let i = 0; i < value.length; i++) {
      if (value[i] === '(') depth++
      else if (value[i] === ')') depth--
      else if (value[i] === ',' && depth === 0) { layers.push(value.slice(start, i)); start = i + 1 }
    }
    layers.push(value.slice(start))
    return layers.some(layer => {
      const colour = layer.match(/[a-z-]+\([^()]*(?:\([^()]*\)[^()]*)*\)|#[0-9a-f]+/i)
      const c = rgba(colour ? colour[0] : 'currentcolor')
      if (c.a === 0 || delta(over(c, under), under) < minDelta) return false
      const lengths = (colour ? layer.replace(colour[0], '') : layer).match(/-?[\d.]+px/g) || []
      return lengths.some(len => parseFloat(len) !== 0)
    })
  }
  const describe = el => {
    const parts = []
    for (let p = el; p && p !== document.body && parts.length < 4; p = p.parentElement) {
      let part = p.tagName.toLowerCase()
      if (p.id) part += `#${p.id}`
      const testid = p.getAttribute('data-testid')
      if (testid) part += `[data-testid="${testid}"]`
      const cls = typeof p.className === 'string' ? p.className.trim().split(/\s+/).filter(Boolean).slice(0, 4) : []
      if (cls.length) part += '.' + cls.join('.')
      parts.unshift(part)
    }
    return parts.join(' > ')
  }

  const candidates = []
  for (const el of document.body.querySelectorAll('*')) {
    const r = el.getBoundingClientRect()
    if (r.width < minSide || r.height < minSide) continue
    const s = getComputedStyle(el)
    const own = rgba(s.backgroundColor)
    if (own.a === 0) continue
    if (!shown(el)) continue
    const under = backdrop(el)
    if (delta(over(own, under), under) >= minDelta) continue
    if (hasEdge(s, under)) continue
    candidates.push(el)
  }

  const saved = { theme: root.dataset.theme, mode: root.dataset.mode }
  const distinctSomewhere = new Map(candidates.map(el => [el, []]))
  for (const ref of references) {
    root.dataset.theme = ref.theme
    root.dataset.mode = ref.mode
    for (const el of candidates) {
      const own = rgba(getComputedStyle(el).backgroundColor)
      const under = backdrop(el)
      if (delta(over(own, under), under) >= minDelta) distinctSomewhere.get(el).push(ref.theme)
    }
  }
  root.dataset.theme = saved.theme
  root.dataset.mode = saved.mode

  return candidates
    .filter(el => distinctSomewhere.get(el).length)
    .map(el => {
      const r = el.getBoundingClientRect()
      return {
        selector: describe(el),
        size: `${Math.round(r.width)}x${Math.round(r.height)}`,
        background: getComputedStyle(el).backgroundColor,
        distinctIn: distinctSomewhere.get(el),
        el,
      }
    })
}

const { srv, base } = await serveDist(DIST)
const browser = await chromium.launch()
const fixedApi = makeFixedApi(PROJECT)
const failures = []
const report = []
const exemptHits = new Map(EXEMPT.map(e => [e.selector, 0]))

for (const route of ROUTES) {
  const context = await browser.newContext({ viewport: VIEWPORT })
  const page = await context.newPage()
  const pageErrors = []
  page.on('pageerror', e => pageErrors.push(String(e).slice(0, 300)))
  await page.routeWebSocket(/\/api\/ws/, () => {})
  await page.route('**/api/**', r => {
    const path = new URL(r.request().url()).pathname
    if (path === '/api/chat/slots') return json(r, slots)
    if (path.startsWith('/api/chat/slots/')) return json(r, detail)
    if (path === '/api/theme/boot') return json(r, { mode: 'light', color: 'kiro', theme: '' })
    return handleBootRoute(r, path, { project: PROJECT, theme: 'light', fixedApi })
  })
  await page.addInitScript(slot => {
    localStorage.clear()
    localStorage.setItem('mc-theme', 'light')
    localStorage.setItem('mc-color-theme', 'kiro')
    localStorage.setItem('mc-onboarded', '1')
    localStorage.setItem('mc-active-slot-chat', slot)
  }, SLOT)
  await page.goto(base + route.path, { waitUntil: 'domcontentloaded' })
  try {
    await page.waitForFunction(
      ready => {
        const main = document.querySelector('main') || document.body
        const text = main.innerText || ''
        return ready ? text.includes(ready) : text.trim().length > 40
      },
      route.ready,
      { timeout: 20000 },
    )
  } catch {
    failures.push(`${route.name}: page never rendered${route.ready ? ` "${route.ready}"` : ' its main pane'}`)
    await context.close()
    continue
  }
  await page.addStyleTag({ content: '*,*::before,*::after{transition:none!important;animation:none!important}' })
  await page.waitForTimeout(800)

  const theme = await page.evaluate(() => document.documentElement.dataset.theme)
  if (theme !== 'kiro-light') {
    failures.push(`${route.name}: data-theme is ${theme}, not kiro-light`)
    await context.close()
    continue
  }
  const crashed = await page.evaluate(() => /Something went wrong/i.test(document.body.innerText))
  if (crashed) failures.push(`${route.name}: an error boundary rendered`)
  if (pageErrors.length) failures.push(`${route.name}: page error: ${pageErrors[0]}`)

  // Planted controls prove the probe is live on this route: a bare `--card`
  // box and one carrying the transparent zero-size shadow layers `shadow-none`
  // computes to, which MUST both be reported, and the same box with a border,
  // which must NOT be. Without them a probe that matched nothing would pass.
  await page.evaluate(canaries => {
    canaries.forEach(([name, edge], i) => {
      const el = document.createElement('div')
      el.setAttribute('data-kiro-light-canary', name)
      el.style.cssText = `position:fixed;left:${8 + i * 132}px;bottom:8px;width:120px;height:60px;background:var(--card);${edge};z-index:2147483647`
      document.body.appendChild(el)
    })
  }, CANARIES.map(c => [c.name, c.edge]))
  const handle = await page.evaluateHandle(probe, { references: REFERENCE_THEMES, minDelta: MIN_DELTA, minSide: MIN_SIDE })
  const all = await handle.evaluate((list, exempt) => list.map(h => ({
    selector: h.selector, size: h.size, background: h.background, distinctIn: h.distinctIn,
    canary: h.el.getAttribute('data-kiro-light-canary'),
    exemptBy: exempt.find(sel => h.el.matches(sel)) || null,
  })), EXEMPT.map(e => e.selector))
  await handle.dispose()
  for (const c of CANARIES) {
    const reported = all.some(h => h.canary === c.name)
    if (reported !== c.reported) {
      failures.push(`${route.name}: the probe ${reported ? 'reported' : 'missed'} the planted ${c.name} card`)
    }
  }
  const hits = all.filter(h => !h.canary)

  for (const hit of hits) {
    if (hit.exemptBy) {
      exemptHits.set(hit.exemptBy, exemptHits.get(hit.exemptBy) + 1)
      continue
    }
    failures.push(`${route.name}  ${hit.selector}  (${hit.size}, ${hit.background}, a card in ${hit.distinctIn.join(' + ')})`)
  }
  report.push({ route: route.path, hits })
  console.log(`${hits.filter(h => !h.exemptBy).length ? 'FAIL' : 'ok  '} ${route.name} (${hits.length} borderless surface(s), ${hits.filter(h => h.exemptBy).length} exempt)`)
  await context.close()
}

for (const [selector, n] of exemptHits) {
  if (!n) failures.push(`stale exemption: "${selector}" matched nothing on any route`)
}

await browser.close()
srv.close()
if (JSON_OUT) writeFileSync(JSON_OUT, JSON.stringify(report, null, 2))

if (failures.length) {
  console.error(`\n${failures.length} problem(s):`)
  for (const f of failures) console.error(`  ${f}`)
  console.error('\nA card must carry its own edge (website/docs/theming-contract.md): give it a border-border, ring-1 ring-border or shadow-*, or add a reasoned EXEMPT entry.')
  process.exit(1)
}
console.log(`\nkiro-light card gate: ${ROUTES.length} routes clean`)
