/**
 * Screenshot harness for the tool card's risk badge, both tiers and both themes.
 *
 * Runs the REAL built SPA (website/dist) behind the shared transcript harness,
 * with every /api/** call answered from fixtures. No gateway, no agent, no Jev:
 * only the network is stubbed, so `readToolRiskRecord`, the transcript
 * virtualizer, `ToolCallLine` and the badge itself render exactly as in
 * production.
 *
 * Three tool rows, because the badge's whole claim is comparative. One `risky`,
 * one `caution`, and one tool call the seam answered `safe` for -- which stamps no
 * record at all, so its card is the control: it is what every tool card looks like
 * without this feature, sitting in the same shot as the two that carry a note.
 * Photographing the flagged rows alone would show the badge and hide the thing
 * that makes it mean something.
 *
 * The record rides `meta.decisions_tool_risk`, which is where a transcript
 * reloaded from history carries it, so these shots exercise the same door a
 * scrollback read goes through rather than a live frame.
 *
 * The three rows sit in the LAST turn, with filler turns above them, which is the
 * shape `lib/tool-row-scene.mjs` uses and it is load-bearing rather than
 * decorative: an older turn is drawn inside a collapse wrapper whose computed
 * opacity is 0 until it is opened, and a screenshot of a row inside it is a blank
 * rectangle with perfectly correct `getBoundingClientRect` numbers behind it.
 * `assertPaintable` below refuses to photograph that state instead of writing it
 * to a file.
 *
 * `/api/decisions/consent` is answered ON even though the badge does not read it:
 * the Settings card shares that query key, and answering it keeps the boot fixture
 * honest about the state being photographed -- notes on calls the seam really
 * looked at. The badge draws from the record alone.
 *
 * Usage: node scripts/capture-tool-risk-badge.mjs [outDir]
 */
import { mkdirSync } from 'node:fs'

import { json } from './lib/boot-api.mjs'
import { openTranscriptHarness } from './lib/transcript-harness.mjs'

const OUT = process.argv[2] || '../temp-screenshots/tool-risk-badge'
const SLOT = 'chat-tool-risk'
const PROJECT = '/home/user/workspace/demo-app'

mkdirSync(OUT, { recursive: true })

/** The purpose of the last tool bubble; the scene is on screen once it is visible. */
const LAST_PURPOSE = 'Point the config at the new toolchain'

/** Consent is on and pointed at the address it was given for. */
const CONSENT = {
  enabled: true,
  endpoint: 'https://api.typesafe.ai/v1/systemone',
  configured_endpoint: 'https://api.typesafe.ai/v1/systemone',
  permits: true,
}

/** The outcome row the point writes, exactly as the log holds it. */
const record = (turnId, tool, tier, p) => ({
  ts: '2026-09-20T07:04:11Z',
  point: 'tool.risk',
  session: '0123456789ab',
  latency_ms: 412,
  scrubbed: false,
  answers: null,
  error: null,
  turn_id: turnId,
  tool,
  tier,
  p,
  policy: 'trust',
  flagged: true,
})

const RISKY = record('tr-9d41c7', 'bash', 'risky', 0.88)
const CAUTION = record('tr-2b07fa', 'fsWrite', 'caution', 0.86)

const now = Math.floor(Date.now() / 1000)

/** A tool bubble as the backend persists one: 🔧 + label, id on the meta. */
const tool = (id, label, purpose, ts, risk) => ({
  role: 'tool',
  ts,
  content: `🔧 ${label}`,
  meta: {
    tool_call_id: id,
    purpose,
    output: 'done',
    kind: 'execute',
    ...(risk ? { decisions_tool_risk: risk } : {}),
  },
})

const slots = [
  {
    key: SLOT,
    title: 'Clear the stale build tree and rewrite the config',
    running: false,
    last_message: 'Cleared the tree and rewrote the config.',
    messages: 11,
    agent: 'kirocrew',
    memory_mode: 'persistent',
    project: PROJECT,
    modified: now,
    source_links: [],
    source_links_total: 0,
  },
]

/** Scrollback, so the transcript overflows and pins to its working end. */
const filler = [
  ['Set up the container toolchain for this worktree.', 'Host Node is 16 and the package floor is 22, so every install and test run goes through the container. I will keep the host untouched.'],
  ['Where do the dependencies come from?', 'The lockfile is identical to a sibling worktree, so the fastest path is reusing its install rather than downloading the tree again.'],
  ['Try that first then.', 'The shared tree in the sibling worktree is largely root-owned, so hardlinking into it is refused. Falling back to a clean install.'],
].flatMap(([q, a], i) => [
  { role: 'user', ts: now - 400 + i * 40, content: q },
  { role: 'assistant', ts: now - 390 + i * 40, content: a },
])

const detail = {
  running: false,
  has_more: false,
  total: filler.length + 5,
  queue: [],
  project: PROJECT,
  messages: [
    ...filler,
    {
      role: 'user',
      ts: now - 90,
      content: 'The build tree is stale. Clear it and point the config at the new toolchain.',
    },
    // The control: a read the seam answered `safe` for, so it stamps nothing and
    // the card is exactly what it is without this feature.
    tool('tc-a', 'Read the build config', 'Check which toolchain the config names', now - 70, null),
    {
      role: 'assistant',
      ts: now - 58,
      content: 'The config still names the old toolchain, and the build tree is from it. Clearing the tree first.',
    },
    tool('tc-b', 'Clear the stale build tree', 'Remove the tree the old toolchain left', now - 40, RISKY),
    tool('tc-c', 'Rewrite the toolchain config', LAST_PURPOSE, now - 20, CAUTION),
  ],
}

async function main() {
  const { page, load, close } = await openTranscriptHarness({
    slot: SLOT,
    project: PROJECT,
    slots,
    detail,
  })

  // Registered AFTER the harness's own catch-all, so it wins: Playwright matches
  // route handlers in reverse registration order.
  await page.route('**/api/decisions/consent', route => json(route, CONSENT))

  /**
   * Pin the locale to English.
   *
   * The harness's init script CLEARS localStorage on every navigation, so
   * writing the key and reloading loses it again. Init scripts run in
   * registration order, so this one is registered after the harness's first
   * navigation and therefore runs after its clear on the reload.
   */
  let localePinned = false
  async function loadInEnglish(theme) {
    await load(theme, { selector: `text=${LAST_PURPOSE}` })
    if (!localePinned) {
      await page.addInitScript(() => localStorage.setItem('mc-lang', 'en'))
      localePinned = true
    }
    await page.reload({ waitUntil: 'domcontentloaded' })
    await page.waitForSelector(`text=${LAST_PURPOSE}`, { timeout: 20000 })
    await page.waitForSelector('[data-testid="tool-risk-badge"]', { timeout: 20000 })
    await page.waitForTimeout(1500)
  }

  /**
   * Refuse to photograph a row nothing will paint.
   *
   * The failure this exists for is silent: a transcript row inside a collapsed
   * turn wrapper reports real layout numbers while an ancestor sits at computed
   * opacity 0, so every shot of it is a blank rectangle of the right size in the
   * right place. Two such files were written, correctly named, before anything
   * noticed. A harness that cannot see its subject must fail, not deliver.
   */
  async function assertPaintable(selector) {
    const bad = await page.evaluate(sel => {
      const start = document.querySelector(sel)
      if (!start) return 'element absent'
      for (let el = start; el; el = el.parentElement) {
        const cs = getComputedStyle(el)
        if (Number(cs.opacity) === 0) return `ancestor ${el.tagName}.${String(el.className || '').slice(0, 40)} has opacity 0`
        if (cs.visibility === 'hidden') return `ancestor ${el.tagName} is visibility:hidden`
        if (cs.display === 'none') return `ancestor ${el.tagName} is display:none`
      }
      const r = start.getBoundingClientRect()
      if (r.width < 4 || r.height < 4) return `element measures ${r.width}x${r.height}`
      if (r.bottom < 0 || r.top > window.innerHeight) return `element is off screen at y=${Math.round(r.top)}`
      return ''
    }, selector)
    if (bad) throw new Error(`${selector} is not paintable: ${bad}`)
  }

  /**
   * The viewport rectangle covering one badge AND the tool pill it annotates.
   *
   * Measured in the page and applied as a full-page `clip`, rather than taken as
   * a per-element `locator.screenshot()`: the transcript is virtualised and rows
   * are absolutely positioned, and Playwright's own scroll-into-view for a 20px
   * row inside that container resolved both badges to one region.
   *
   * The pill is included deliberately: a badge photographed alone says a tier and
   * a number, and what a reviewer needs to see is WHICH call it sits under.
   */
  async function rowClip(selector) {
    return page.evaluate(sel => {
      const badge = document.querySelector(sel)
      if (!badge) throw new Error(`no badge for ${sel}`)
      let row = badge.parentElement
      while (row && !row.querySelector('[data-testid="tool-pill-label"]')) row = row.parentElement
      const a = badge.getBoundingClientRect()
      const b = (row || badge).getBoundingClientRect()
      const pad = 6
      const top = Math.max(0, Math.min(a.top, b.top) - pad)
      const left = Math.max(0, Math.min(a.left, b.left) - pad)
      return {
        x: left,
        y: top,
        width: Math.min(window.innerWidth - left, Math.max(a.right, b.right) - left + pad),
        height: Math.min(window.innerHeight - top, Math.max(a.bottom, b.bottom) - top + pad),
      }
    }, selector)
  }

  const RISKY_SEL = '[data-testid="tool-risk-badge"][data-tier="risky"]'
  const CAUTION_SEL = '[data-testid="tool-risk-badge"][data-tier="caution"]'

  for (const theme of ['light', 'dark']) {
    await loadInEnglish(theme)

    await assertPaintable(RISKY_SEL)
    await assertPaintable(CAUTION_SEL)

    // Measured before anything is captured, and asserted distinct. Two equal
    // rectangles is the other quiet way this harness produced one picture twice.
    const riskyClip = await rowClip(RISKY_SEL)
    const cautionClip = await rowClip(CAUTION_SEL)
    if (JSON.stringify(riskyClip) === JSON.stringify(cautionClip)) {
      throw new Error(`both badges measured to one rectangle (${JSON.stringify(riskyClip)})`)
    }

    // The three cards together: the unflagged read, the risky clear, the
    // cautioned write. This is the shot that carries the feature, because the
    // silence on the first card is half of what the other two say.
    await page.screenshot({ path: `${OUT}/transcript-${theme}.png` })
    console.log('wrote', `${OUT}/transcript-${theme}.png`)

    // Each badge with its own pill, so the tier word, the score and the thumbs
    // are legible rather than described.
    await page.screenshot({ path: `${OUT}/badge-risky-${theme}.png`, clip: riskyClip })
    console.log('wrote', `${OUT}/badge-risky-${theme}.png`, JSON.stringify(await page.locator(RISKY_SEL).first().textContent()))

    await page.screenshot({ path: `${OUT}/badge-caution-${theme}.png`, clip: cautionClip })
    console.log('wrote', `${OUT}/badge-caution-${theme}.png`, JSON.stringify(await page.locator(CAUTION_SEL).first().textContent()))
  }

  await close()
}

main().catch(err => {
  console.error(err)
  process.exit(1)
})
