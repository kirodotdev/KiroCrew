/**
 * Screenshot harness for the credit-multiplier badge in the model picker.
 *
 * Runs the REAL built SPA (website/dist) behind a tiny in-process static server
 * and answers every /api/** call from fixtures via Playwright route
 * interception. No gateway, no dashboard credential, no kiro-cli spawn.
 *
 * The model rows are the ACTUAL `kiro-cli chat --list-models --format json`
 * payload shape, with the real served spread (0.05x to 4.4x) so the tiers and
 * the badge column are exercised at both extremes rather than on tidy fixtures.
 *
 * `minimax-m2.5` deliberately ships WITHOUT `rate_multiplier`: that is the
 * degraded row (a 24h localStorage cache written before the field existed, or a
 * gateway predating it), and the harness ASSERTS it renders no badge. An
 * invented 1x there would be a wrong price shown as confidently as a right one.
 *
 * Usage: node scripts/capture-model-multiplier.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

import { serveDist } from './lib/serve-dist.mjs'

const OUT = process.argv[2] || '../temp-screenshots/model-multiplier'
const SLOT = 'chat-model-multiplier'
const PROJECT = '/home/user/workspace/KiroCrew'

mkdirSync(OUT, { recursive: true })

/** Real --list-models rows. minimax-m2.5 has NO rate_multiplier on purpose. */
const MODELS = [
  { model_name: 'auto', description: 'Models chosen by task for optimal usage and consistent quality', context_window_tokens: 1000000, rate_multiplier: 1.0 },
  { model_name: 'claude-opus-5', description: 'Claude Opus 5 model with 1M context window', context_window_tokens: 1000000, rate_multiplier: 2.2 },
  { model_name: 'claude-sonnet-5', description: 'Claude Sonnet 5 model with 1M context window', context_window_tokens: 1000000, rate_multiplier: 1.3 },
  { model_name: 'gpt-5.6-sol', description: 'OpenAI GPT 5.6 Sol with 272k context window', context_window_tokens: 272000, rate_multiplier: 2.4 },
  { model_name: 'gpt-5.6-terra', description: 'OpenAI GPT 5.6 Terra with 272k context window', context_window_tokens: 272000, rate_multiplier: 1.0 },
  { model_name: 'gpt-5.6-luna', description: 'OpenAI GPT 5.6 Luna with 272k context window', context_window_tokens: 272000, rate_multiplier: 0.1 },
  { model_name: 'claude-haiku-4.5', description: 'The latest Claude Haiku model', context_window_tokens: 200000, rate_multiplier: 0.4 },
  { model_name: 'glm-5', description: 'GLM-5 model', context_window_tokens: 200000, rate_multiplier: 0.5 },
  { model_name: 'minimax-m2.5', description: 'MiniMax M2.5 model', context_window_tokens: 196000 },
  { model_name: 'qwen3-coder-next', description: 'Qwen3 Coder Next', context_window_tokens: 256000, rate_multiplier: 0.05 },
]

/** What each row's badge must show, and which tier border it must carry. */
const EXPECTED = {
  auto: ['1.0x', 'border-muted'],
  'claude-opus-5': ['2.2x', 'border-warn'],
  'claude-sonnet-5': ['1.3x', 'border-muted'],
  'gpt-5.6-sol': ['2.4x', 'border-warn'],
  'gpt-5.6-terra': ['1.0x', 'border-muted'],
  'gpt-5.6-luna': ['0.1x', 'border-ok'],
  'claude-haiku-4.5': ['0.4x', 'border-ok'],
  'glm-5': ['0.5x', 'border-ok'],
  'qwen3-coder-next': ['0.05x', 'border-ok'],
}

const slot = {
  key: SLOT, title: 'New session', running: false, last_message: '', messages: 0,
  agent: 'default', memory_mode: 'persistent', project: PROJECT,
  model: 'claude-opus-5', reasoning_effort: 'high',
  modified: Math.floor(Date.now() / 1000), source_links: [], source_links_total: 0,
}
const detail = {
  running: false, has_more: false, total: 0, queue: [], project: PROJECT,
  model: 'claude-opus-5', reasoning_effort: 'high', messages: [],
}

const json = (route, body, status = 200) => route.fulfill({
  status, contentType: 'application/json', body: JSON.stringify(body),
})

const failures = []
const check = (ok, msg) => { if (!ok) failures.push(msg); console.log(`${ok ? 'ok  ' : 'FAIL'} ${msg}`) }

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()

  for (const theme of ['dark', 'light', 'unpriced']) {
    const context = await browser.newContext({ viewport: { width: 1500, height: 950 }, deviceScaleFactor: 2 })
    const page = await context.newPage()
    await page.routeWebSocket(/\/api\/ws/, () => {})

    await page.route('**/api/**', async route => {
      const path = new URL(route.request().url()).pathname
      if (path === '/api/config/kirocrew') {
        return json(route, {
          agent: { model: 'claude-opus-5', reasoning_effort: 'high', provider: 'acp' },
          session: { autocompact_pct: 90 },
          dashboard: { user_role: '', user_technical_level: '' },
        })
      }
      if (path === '/api/models') {
        // 'unpriced' serves the same rows with rate_multiplier removed — a
        // gateway/kiro-cli predating the field, or a cached list written
        // before it existed.
        return json(route, theme === 'unpriced'
          ? MODELS.map(({ rate_multiplier: _drop, ...rest }) => rest)
          : MODELS)
      }
      if (path.startsWith('/api/effort-levels')) return json(route, ['low', 'medium', 'high', 'xhigh', 'max'])
      if (path === '/api/agents') {
        return json(route, {
          agents: [{ name: 'default', kiro_agent: 'kirocrew', description: 'Default crew agent' }],
          default_agent: 'default',
        })
      }
      if (path.startsWith('/api/agents/detail/')) return json(route, { name: 'kirocrew', model: 'claude-opus-5', skills: [] })
      if (path === '/api/agents/installed') return json(route, [])
      if (path === '/api/kiro-prerequisite') {
        return json(route, {
          platform: 'linux', installed: true, authenticated: true, ready: true,
          initial_setup_complete: true, can_auto_install: false, can_login: false,
          repair_required: false, docs_url: '', setup_allowed: false,
          operation: { kind: '', status: 'idle', message: '', detail: '', url: '', error: '' },
        })
      }
      if (path === '/api/chat/slots') return json(route, [slot])
      if (path.startsWith('/api/chat/slots/')) return json(route, detail)
      if (path.startsWith('/api/instances')) return json(route, { instances: [], active: '' })
      if (path === '/api/status') return json(route, { sessions: 1, crons: 0, lessons: 0, uptime: 120, version: 'dev' })
      if (path === '/api/notifications') return json(route, { notifications: [], unread: 0 })
      if (path === '/api/auth/me') return json(route, { user: 'owner', app: '' })
      if (path === '/api/themes') return json(route, { themes: [], installed: [] })
      if (path === '/api/theme/boot') return json(route, { mode: theme === 'unpriced' ? 'dark' : theme, theme: '' })
      if (path === '/api/dashboard/branding') return json(route, { bot_name: 'Kiro Crew', avatar: '' })
      if (path === '/api/recent-projects') return json(route, { dirs: [PROJECT] })
      if (path === '/api/chat/nav/resolve-links') return json(route, { summaries: [] })
      if (path === '/api/dashboard/config') {
        return json(route, { restore_sessions: false, restore_window_minutes: 30, merge_queued_messages: false, widget_density: 'more' })
      }
      if (/(config|tips|voice|autonudge|branding|status|usage-summary)/.test(path)) return json(route, {})
      return json(route, [])
    })

    page.on('pageerror', err => console.log('PAGEERROR:', String(err).slice(0, 300)))

    await page.addInitScript(([s, t]) => {
      localStorage.clear()
      localStorage.setItem('mc-theme', t === 'unpriced' ? 'dark' : t)
      localStorage.setItem('mc-onboarded', '1')
      localStorage.setItem('mc-active-slot', s)
    }, [SLOT, theme])
    await page.goto(base + '/', { waitUntil: 'domcontentloaded' })
    await page.waitForTimeout(3200)

    const capsule = page.locator('[title^="Model:"]').first()
    await capsule.waitFor({ timeout: 25000 })
    await capsule.click()
    await page.waitForTimeout(900)

    const listbox = page.getByRole('listbox', { name: /model list/i }).first()
    await listbox.waitFor({ timeout: 10000 })
    await page.screenshot({ path: `${OUT}/01-picker-${theme}.png` })
    console.log('wrote', `${OUT}/01-picker-${theme}.png`)

    // 'unpriced' serves the same rows with rate_multiplier removed: no badge may
    // render, and the picker must look exactly as it did before this feature.
    if (theme === 'unpriced') {
      const badgeCount = await page.locator('[role="option"] span.rounded-full').count()
      check(badgeCount === 0, `unpriced: no badges render (got ${badgeCount})`)
      const rowCount = await page.locator('[role="option"]').count()
      check(rowCount === MODELS.length, `unpriced: all ${MODELS.length} rows still render (got ${rowCount})`)
      await context.close()
      continue
    }

    // Tight crop of the popover itself — the reviewable artefact.
    const dd = page.locator('[role="listbox"][aria-label]').first()
    const popBox = () => dd.evaluate(el => {
      const pop = el.closest('.fixed') ?? el
      const r = pop.getBoundingClientRect()
      return { x: r.x, y: r.y, width: r.width, height: r.height }
    })
    await page.screenshot({ path: `${OUT}/02-popover-${theme}.png`, clip: await popBox() })
    console.log('wrote', `${OUT}/02-popover-${theme}.png`)

    // The list scrolls, so the default crop only ever shows the top rows — and
    // every budget-tier (green) badge lives below the fold. Filter to `gpt` to
    // get all three tiers in ONE frame: Sol 2.4x premium, Terra 1x standard,
    // Luna 0.1x budget. Without this the green border has no evidence at all.
    const filter = page.getByRole('textbox', { name: /filter models/i }).first()
    await filter.fill('gpt')
    await page.waitForTimeout(500)
    await page.screenshot({ path: `${OUT}/03-all-tiers-${theme}.png`, clip: await popBox() })
    console.log('wrote', `${OUT}/03-all-tiers-${theme}.png`)
    const tierFrame = await page.$$eval('[role="option"]', els => els.map(el => ({
      name: el.querySelector('[data-model-name]')?.textContent?.trim() ?? '',
      badge: (el.querySelector('span.rounded-full [aria-hidden="true"]')?.textContent ?? '').trim(),
      tier: /border-warn/.test(el.querySelector('span.rounded-full')?.className ?? '') ? 'premium'
        : /border-ok/.test(el.querySelector('span.rounded-full')?.className ?? '') ? 'budget' : 'standard',
    })))
    check(new Set(tierFrame.map(r => r.tier)).size === 3,
      `${theme}: all three tiers visible in one frame (${tierFrame.map(r => `${r.badge}=${r.tier}`).join(' ')})`)
    await filter.fill('')
    await page.waitForTimeout(400)

    {
      // ── Assertions: the badge is only evidence if it says the right thing ──
      const rows = await page.$$eval('[role="option"]', els => els.map(el => {
        const name = el.querySelector('[data-model-name]')?.textContent?.trim() ?? ''
        const badge = el.querySelector('span.rounded-full')
        return {
          name,
          badge: badge ? (badge.querySelector('[aria-hidden="true"]')?.textContent ?? '').trim() : null,
          cls: badge ? badge.className : '',
          srText: badge ? (badge.querySelector('.sr-only')?.textContent ?? '') : '',
        }
      }))
      const byName = Object.fromEntries(rows.map(r => [r.name, r]))
      check(rows.length === MODELS.length, `all ${MODELS.length} rows rendered (got ${rows.length})`)

      for (const [name, [glyph, border]] of Object.entries(EXPECTED)) {
        const r = byName[name]
        check(!!r && r.badge === glyph, `${name} badge reads ${glyph} (got ${r ? r.badge : '<row missing>'})`)
        check(!!r && r.cls.includes(border), `${name} carries ${border}`)
        check(!!r && r.cls.includes('text-text'), `${name} digits use text-text (contrast-safe on every theme)`)
      }

      // The degraded row: absent, not guessed.
      const stale = byName['minimax-m2.5']
      check(!!stale, 'minimax-m2.5 row present')
      check(!!stale && stale.badge === null, 'minimax-m2.5 shows NO badge (no rate_multiplier reported)')

      // Screen-reader text explains the bare glyph.
      check((byName['claude-opus-5']?.srText ?? '').includes('the credit cost of Auto'),
        'selected row carries the sr-only explanation')

      // The selected row's badge keeps its own surface so the tier hue survives.
      check((byName['claude-opus-5']?.cls ?? '').includes('bg-bg-elevated'),
        'selected row badge keeps bg-bg-elevated over the accent wash')

      // Real computed contrast of the digits, measured in the shipped bundle.
      //
      // Two traps this deliberately avoids:
      //
      // 1. Computed colours are NOT `rgb(...)` here. The theme tokens resolve
      //    through color-mix(), so the browser reports CSS Color 4 syntax —
      //    `color(srgb 0.862745 0.854902 0.87451)`. A `[\d.]+` regex reads the
      //    `4` out of "srgb" as the red channel and every ratio comes out 1:1.
      //    Parsing is therefore handed to a canvas, which normalises ANY CSS
      //    colour syntax to 8-bit sRGB + alpha.
      // 2. The surface UNDER the badge is whatever ancestor first paints one
      //    (the row's accent-subtle wash when selected, the popover otherwise),
      //    so the backdrop is resolved by walking up and compositing.
      const worst = await page.evaluate(() => {
        const cv = document.createElement('canvas')
        cv.width = cv.height = 1
        const ctx = cv.getContext('2d', { willReadFrequently: true })
        ctx.globalCompositeOperation = 'copy'
        /** Any CSS colour -> {r,g,b,a}, normalised by the browser itself. */
        const parse = css => {
          ctx.fillStyle = 'rgba(0,0,0,0)'
          ctx.fillStyle = css                 // invalid input leaves the previous value
          ctx.fillRect(0, 0, 1, 1)
          const [r, g, b, a] = ctx.getImageData(0, 0, 1, 1).data
          return { r, g, b, a: a / 255 }
        }
        const over = (fg, bg) => ({
          r: fg.r * fg.a + bg.r * (1 - fg.a),
          g: fg.g * fg.a + bg.g * (1 - fg.a),
          b: fg.b * fg.a + bg.b * (1 - fg.a),
          a: 1,
        })
        const backdrop = el => {
          const stack = []
          for (let n = el; n; n = n.parentElement) {
            const c = parse(getComputedStyle(n).backgroundColor)
            if (c.a > 0) stack.push(c)
            if (c.a === 1) break
          }
          let out = { r: 255, g: 255, b: 255, a: 1 }
          for (const layer of stack.reverse()) out = over(layer, out)
          return out
        }
        const lum = ({ r, g, b }) => {
          const f = v => { v /= 255; return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4) }
          return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b)
        }
        let min = Infinity
        const rows = []
        for (const el of document.querySelectorAll('[role="option"] span.rounded-full')) {
          const bg = backdrop(el)
          const fg = over(parse(getComputedStyle(el).color), bg)
          const [hi, lo] = [lum(fg), lum(bg)].sort((a, b) => b - a)
          const ratio = (hi + 0.05) / (lo + 0.05)
          min = Math.min(min, ratio)
          rows.push({
            badge: el.querySelector('[aria-hidden="true"]')?.textContent ?? '?',
            ratio: Math.round(ratio * 100) / 100,
          })
        }
        // A probe that measured nothing must not report a pass.
        return { ratio: rows.length ? Math.round(min * 100) / 100 : 0, seen: rows.length, rows }
      })
      // The class-name checks above read `className`, which is present in the
      // JSX regardless of whether the rule reached the bundle. Stripping the
      // three .border-* rules from dist/ leaves every badge hue-less and the
      // class assertions still pass. Surviving Tailwind's purge is the one
      // property unit tests structurally cannot see, so assert the COMPUTED
      // colour: three distinct hues, none transparent.
      const borders = await page.$$eval('[role="option"] span.rounded-full',
        els => els.map(el => getComputedStyle(el).borderTopColor))
      // The tier hue identifies a state, so WCAG 1.4.11 non-text contrast (3:1)
      // is the bar it has to clear against the surface it sits on — not the
      // 4.5:1 text rule, and not "looks fine on the theme I developed against".
      const borderContrast = await page.evaluate(() => {
        const cv = document.createElement('canvas'); cv.width = cv.height = 1
        const ctx = cv.getContext('2d', { willReadFrequently: true })
        ctx.globalCompositeOperation = 'copy'
        const parse = css => {
          ctx.fillStyle = 'rgba(0,0,0,0)'; ctx.fillStyle = css; ctx.fillRect(0, 0, 1, 1)
          const [r, g, b, a] = ctx.getImageData(0, 0, 1, 1).data
          return { r, g, b, a: a / 255 }
        }
        const over = (f, b) => ({ r: f.r * f.a + b.r * (1 - f.a), g: f.g * f.a + b.g * (1 - f.a), b: f.b * f.a + b.b * (1 - f.a), a: 1 })
        const lum = ({ r, g, b }) => {
          const t = v => { v /= 255; return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4) }
          return 0.2126 * t(r) + 0.7152 * t(g) + 0.0722 * t(b)
        }
        const out = []
        for (const el of document.querySelectorAll('[role="option"] span.rounded-full')) {
          const surface = over(parse(getComputedStyle(el).backgroundColor), { r: 255, g: 255, b: 255, a: 1 })
          const edge = over(parse(getComputedStyle(el).borderTopColor), surface)
          const [hi, lo] = [lum(edge), lum(surface)].sort((a, b) => b - a)
          out.push({ badge: el.querySelector('[aria-hidden="true"]')?.textContent ?? '?', ratio: Math.round(((hi + 0.05) / (lo + 0.05)) * 100) / 100 })
        }
        return out
      })
      for (const b of borderContrast) console.log(`     border ${b.badge.padEnd(6)} contrast ${b.ratio}:1`)
      const worstBorder = Math.min(...borderContrast.map(b => b.ratio))
      check(worstBorder >= 3, `${theme}: worst tier-border contrast ${worstBorder}:1 meets WCAG 1.4.11 3:1`)
      const distinct = [...new Set(borders)]
      check(distinct.length === 3, `${theme}: three distinct border colours actually render (${distinct.join(' | ')})`)
      check(!borders.some(c => /^(transparent$|rgba?\([^)]*[,/]\s*0\s*\))/.test(c)),
        `${theme}: no badge border is transparent (the CSS reached the bundle)`)

      for (const r of worst.rows) console.log(`     badge ${r.badge.padEnd(6)} contrast ${r.ratio}:1`)
      check(worst.seen === Object.keys(EXPECTED).length,
        `contrast probe measured all ${Object.keys(EXPECTED).length} badges (got ${worst.seen})`)
      check(worst.ratio >= 4.5, `worst badge text contrast ${worst.ratio}:1 meets WCAG AA 4.5:1`)
    }

    await context.close()
  }

  await browser.close()
  srv.close()

  if (failures.length) {
    console.error(`\n${failures.length} assertion(s) failed:\n  ${failures.join('\n  ')}`)
    process.exit(1)
  }
  console.log('\nAll assertions passed.')
}

main()
