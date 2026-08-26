/**
 * Measures — and photographs — the gap between the narrow tab strip's border
 * and the first thing each SidePanelLayout tab paints, across all three pages
 * built on that shell: Agent Capabilities, Developer, and Settings.
 *
 * On a phone SidePanelLayout replaces the desktop header block (whose `pb-3`
 * spaces a tab's title from its content) with a pill strip that ends in a drawn
 * `border-b`. The pane below it had no top inset, so a tab whose first element
 * is a Card or a stat row rendered that element's own border ON the divider —
 * two lines touching, with no gap to read as separation. The pane now carries
 * that inset, which makes it the ONE owner of the gap: a tab that also puts a
 * top margin on its own first element lands further down than its siblings, and
 * this harness is what shows which tabs do.
 *
 * jsdom computes no geometry, so the unit guard
 * (src/test/SidePanelLayout.narrowPaneTopInset.test.tsx) can only assert the
 * class. This is the harness that produces the number: it runs the REAL built
 * SPA (website/dist) behind a tiny static server and answers every /api/** call
 * from fixtures, so the client code under test is unmodified — only the network
 * is stubbed. Per tab it reports both the divider→first-in-flow-box distance
 * (the number a tab's own top margin moves, so the one to compare across tabs)
 * and the divider→first-painted-pixel distance, and writes a 390px screenshot.
 *
 * Usage: node scripts/capture-side-panel-pane-inset.mjs [outDir]
 * Run it once on the fix and once with the inset reverted to get before/after.
 */
import { chromium } from 'playwright'
import { mkdirSync, writeFileSync } from 'node:fs'
// The static server is shared with the other capture harnesses in this folder:
// it carries the MIME table, the index.html fallback that makes /capabilities
// deep-link, a path-traversal containment check, and the `/logo.png` route the
// real dashboard serves from outside the SPA bundle (a local copy 404s it and
// renders a broken brand mark in every frame).
import { serveDist } from './lib/serve-dist.mjs'
// Same reason as the server: the ~25 boot endpoints every gateway-free harness
// needs live in one place, with `extra` for the per-harness ones.
import { stubDashboardApi, json, logPageProblems } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '/tmp/capabilities-pane-inset'
// Viewport width: 390 (a phone) by default; pass a second argument to measure
// another width. Above the md breakpoint the shell swaps the pill strip for the
// desktop header, so `gap()` reports the header's own inset there.
const WIDTH = Number(process.argv[3] || 390)

mkdirSync(OUT, { recursive: true })

const SKILLS = [
  { key: 'babysit', name: 'babysit', description: 'Same-session monitoring loop for PRs and CI runs', path: '/home/user/.kiro/crew/skills/babysit/SKILL.md', source: 'kirocrew' },
  { key: 'prepare-pr', name: 'prepare-pr', description: 'Drive working-tree changes to a review-ready pull request', path: '/home/user/.kiro/crew/skills/prepare-pr/SKILL.md', source: 'kirocrew' },
  { key: 'widgets', name: 'widgets', description: 'Render rich HTML inline via mcwidget tags', path: '/home/user/.kiro/crew/skills/widgets/SKILL.md', source: 'kirocrew' },
]
const STEERING = {
  files: [
    { key: 'user/api-standards.md', rel: 'api-standards.md', source: 'user', bytes: 1840 },
    { key: 'user/tone.md', rel: 'tone.md', source: 'user', bytes: 620 },
  ],
}
const HOOKS = [
  { name: 'lint-on-save', event: 'PostToolUse', matcher: 'fs_write', command: 'npm run lint', enabled: true, source: 'user' },
  { name: 'block-force-push', event: 'PreToolUse', matcher: 'execute_bash', command: 'scripts/guard.sh', enabled: true, source: 'user' },
]
const PROMPTS = [
  { name: 'triage', fullName: 'triage', source: 'user', description: 'Sort an inbox of reports into buckets' },
  { name: 'retro', fullName: 'retro', source: 'user', description: 'Write a retrospective from a deploy log' },
]
const MCP = {
  servers: {
    'kirocrew-core': { command: 'kirocrew', args: ['mcp'], disabled: false },
    'kirocrew-cron': { command: 'kirocrew', args: ['mcp-cron'], disabled: false },
  },
}
const WORKSPACES = [
  { name: 'default', path: '/home/user/.kiro/crew/workspace', active: true, sessions: 3 },
  { name: 'research', path: '/home/user/.kiro/crew/workspaces/research', active: false, sessions: 0 },
]
const AGENTS = [
  { name: 'kirocrew', description: 'Autonomous personal AI agent', source: 'kirocrew', model: 'auto', mcp_servers: ['kirocrew-core'], filename: 'kirocrew.json', skills: [] },
  { name: 'code-reviewer', description: 'Reviews changes against the repo conventions', source: 'builtin', model: 'auto', mcp_servers: [], filename: 'code-reviewer.json', skills: [] },
]

const { srv, base } = await serveDist()
const browser = await chromium.launch()
const context = await browser.newContext({
  // Width is a parameter because the inset is a NARROW-branch value and the
  // margins it replaced were unprefixed: the same edit therefore moves desktop
  // too, and a claim about desktop has to be measured at desktop, not inferred
  // from the phone number.
  viewport: { width: WIDTH, height: WIDTH >= 768 ? 900 : 844 },
  // 11–13px type at 1x renders soft once GitHub scales the image.
  deviceScaleFactor: 2,
})
const page = await context.newPage()

// Boot fixtures (prerequisite gate, status, notifications, auth, branding,
// instances, websocket swallow, localStorage) come from the shared lib rather
// than a local copy: it is consulted after `extra`, so only the endpoints this
// harness actually cares about live here. It also gets the two-word `bot_name`
// right, which a hand-rolled copy does not.
await stubDashboardApi(page, {
  // `extra` must report that it HANDLED the route: the lib awaits the return
  // value, and `json()`'s promise resolves to undefined, so returning it alone
  // reads as unhandled and the lib fulfils a second time ("Route is already
  // handled!"). Hence the sibling harnesses' `return json(...), true` shape.
  extra: (path, route) => {
    if (path === '/api/skills') return json(route, SKILLS), true
    if (path === '/api/steering') return json(route, STEERING), true
    if (path === '/api/hooks') return json(route, HOOKS), true
    if (path === '/api/prompts') return json(route, PROMPTS), true
    if (path === '/api/mcp') return json(route, MCP), true
    if (path === '/api/workspaces') return json(route, WORKSPACES), true
    if (path === '/api/agents/installed') return json(route, AGENTS), true
    if (path === '/api/config/default-agent') {
      return json(route, { default_agent: 'kirocrew' }), true
    }
    if (path === '/api/models') {
      return json(route, [{ model_name: 'auto', description: 'Let Kiro choose' }]), true
    }
    return false
  },
})

logPageProblems(page)

/**
 * Distance from the tab strip's bottom border to the top of the pane's content.
 *
 * Two numbers, because they answer different questions and an earlier version of
 * this harness reported only the first one and misread a tab because of it:
 *
 *  - `boxGapPx` — the top of the first element that occupies flow space. This is
 *    what a tab's own top margin moves, so it is the number to compare when
 *    asking "does every tab start at the same place".
 *  - `inkGapPx` — the topmost pixel that actually PAINTS: a drawn border, a
 *    non-transparent background, an icon, or rendered text measured through a
 *    Range (an element's own box is larger than its glyphs, and a text-bearing
 *    element usually has element children too, so an `childElementCount === 0`
 *    test silently skips every label — which is how Connections' sub-tab strip
 *    went unmeasured and the empty-state card 40px below it got reported as
 *    that tab's first content).
 */
async function gap() {
  return page.evaluate(() => {
    // The reference edge differs by breakpoint, because the shell swaps the
    // block above the pane: narrow gets the pill strip (ends in a drawn border),
    // desktop gets the header block (spaces its content with `pb-3`). Whichever
    // is present is the thing the pane's first element must clear.
    const strip = document.querySelector('[data-testid="side-panel-header"]')
      || document.querySelector('div.shrink-0.border-b')
    // On the narrow branch the scrolling column has no header block, so its
    // first child IS the pane; on desktop the pane is the header's next sibling.
    const pane = document.querySelector('[data-testid="side-panel-pane"]')
      || strip?.nextElementSibling
      || strip?.parentElement?.nextElementSibling?.firstElementChild
    if (!pane || !strip) {
      return {
        error: 'pane or strip not found',
        sawStrip: Boolean(strip),
        stripClass: strip ? String(strip.className).slice(0, 80) : null,
      }
    }
    const dividerY = strip.getBoundingClientRect().bottom

    const rendered = (el) => {
      const cs = getComputedStyle(el)
      return cs.visibility !== 'hidden' && cs.display !== 'none' && cs.position !== 'fixed'
    }

    let box = null, boxWhat = null, ink = null, inkWhat = null
    const name = (el) =>
      el.tagName.toLowerCase() + (el.className ? '.' + String(el.className).split(/\s+/)[0] : '')

    for (const el of pane.querySelectorAll('*')) {
      if (!rendered(el)) continue
      const r = el.getBoundingClientRect()
      if (r.height < 1 || r.width < 1) continue
      const cs = getComputedStyle(el)

      // In-flow box. `relative` and `sticky` still occupy flow space (Card is
      // relative, for its glow) — only absolute/fixed are lifted out, and those
      // a tab's own top margin does not move, so they cannot answer the question.
      const inFlow = cs.position !== 'absolute' && cs.position !== 'fixed'
      if (inFlow && (box === null || r.top < box)) {
        box = r.top
        boxWhat = name(el)
      }

      const paints = parseFloat(cs.borderTopWidth) > 0
        || cs.backgroundColor !== 'rgba(0, 0, 0, 0)'
        || el instanceof SVGElement || el.tagName === 'IMG'
      if (paints && (ink === null || r.top < ink)) {
        ink = r.top
        inkWhat = name(el)
      }

      // Glyph ink, via the element's OWN text nodes only, so a wrapper is not
      // credited with the position of a descendant's text.
      for (const node of el.childNodes) {
        if (node.nodeType !== Node.TEXT_NODE || !node.textContent.trim()) continue
        const range = document.createRange()
        range.selectNodeContents(node)
        const tr = range.getBoundingClientRect()
        range.detach?.()
        if (tr.height < 1 || tr.width < 1) continue
        if (ink === null || tr.top < ink) {
          ink = tr.top
          inkWhat = name(el) + ':text'
        }
      }
    }

    const round = (v) => (v === null ? null : Math.round((v - dividerY) * 10) / 10)
    return {
      paneInsetTop: getComputedStyle(pane).paddingTop,
      boxGapPx: round(box),
      firstBox: boxWhat,
      inkGapPx: round(ink),
      firstInk: inkWhat,
    }
  })
}

/**
 * Every page built on SidePanelLayout, and every tab on it. The shell is shared,
 * so the inset is shared — and so is the way a tab can stack its own margin on
 * top of it. Measuring one page would leave the other two unproven.
 */
const PAGES = [
  {
    route: 'capabilities',
    tabs: ['crews', 'templates', 'mcp', 'skills', 'steering', 'hooks', 'prompts'],
  },
  {
    route: 'developer',
    tabs: ['logs', 'system', 'telemetry', 'storage', 'mcp-pool', 'memory', 'config',
      'feature-previews', 'archive'],
  },
  {
    route: 'settings',
    tabs: ['overview', 'imports', 'chat', 'display', 'voice', 'notifications', 'shortcuts',
      'skills', 'channels', 'browser', 'computer-use', 'webhooks', 'instances', 'privacy',
      'security', 'developer', 'releases', 'about'],
  },
]

const rows = []
for (const { route, tabs } of PAGES) {
  for (const tab of tabs) {
    await page.goto(`${base}/${route}?tab=${tab}`, { waitUntil: 'domcontentloaded' })
    await page.waitForTimeout(1800)
    const m = await gap()
    rows.push({ page: route, tab, ...m })
    await page.screenshot({ path: `${OUT}/${route}-${tab}.png` })
    console.log(`${route}/${tab}`.padEnd(26), JSON.stringify(m))
  }
}

writeFileSync(`${OUT}/measurements.json`, JSON.stringify(rows, null, 2))
console.log('wrote', OUT)

await browser.close()
srv.close()
