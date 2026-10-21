/**
 * Screenshot harness for #22990 — the YAML-frontmatter metadata strip in the
 * file preview.
 *
 * Runs the REAL built SPA (website/dist) on a loopback static server with every
 * /api/** call answered from fixtures — no gateway, no AWS account, no token —
 * exactly the technique of capture-aws-control-preview-renderers.mjs, because
 * the AWS-control drive preview renders through the dashboard's own
 * `ContentRenderer`, the single surface this PR changes. A markdown drive object
 * reads the same way it reads in the file side panel, so a frontmatter document
 * shows the real metadata strip above the real rendered body.
 *
 * It captures the strip in BOTH themes and in BOTH expanded and collapsed
 * states (the two evidence gaps the UX review called out: the heading label and
 * the collapsed state were not visible in the first screenshots), and it asserts
 * the RENDERED DOM — the strip, its heading label text, the key/value rows, the
 * aria-expanded toggle — so a stale dist fails the process instead of exiting 0
 * with the wrong pane in the PNGs.
 *
 * Usage: node scripts/capture-22990-frontmatter-strip.mjs <outDir>
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { json, handleBootRoute } from './lib/boot-api.mjs'

const OUT = process.argv[2] || '/tmp/kc-22990-frontmatter'
mkdirSync(OUT, { recursive: true })

// ---- fixtures -------------------------------------------------------------
const ACCOUNT = '217681647555'
const ACCOUNTS = {
  supported: true,
  accounts: [{
    account: ACCOUNT, name: 'personal', health: 'ok',
    profiles: [{ name: 'personal', kind: 'credential-process', region: 'us-west-2', account: ACCOUNT, default: true, identityOk: true }],
  }],
  totals: { accounts: 1, profiles: 1, profilesHealthy: 1 },
}
const CONSENT = (service) => ({
  service, serviceLabel: service === 's3' ? 'Amazon S3 (cloud drive storage)' : 'AWS Cost Explorer',
  granted: true, region: 'us-west-2', credentialSource: 'profile personal', account: ACCOUNT,
  identityResolved: true, revokedOnAccountChange: false,
  grant: { account: ACCOUNT, region: 'us-west-2', profile: 'personal', granted_at: '2026-08-28T00:00:00+00:00' },
})
const DRIVE = {
  exists: true, bucket: 'kirocrew-drive-7f3a91c4', region: 'us-west-2',
  usage: { bytes: 44677427, objects: 1, sections: { drive: { objects: 1, bytes: 13210 }, library: { objects: 0, bytes: 0 }, backup: { objects: 0, bytes: 0 } } },
}
const LISTING = {
  folders: [],
  files: [{ key: 'incident-retro.md', size: 1320, modified: '2026-10-20T09:12:00Z' }],
}

// An Open-Knowledge-Format document that OPENS with a YAML frontmatter block —
// the exact shape this ecosystem authors constantly (skills, agents, steering).
// The body heading repeats the `title` on purpose: that is the case the review
// called out where an unpadded body would anchor a comment into the frontmatter.
const MD_WITH_FRONTMATTER = `---
title: Quarterly incident retrospective
status: draft for review
owner: reliability-guild
tags: [retrospective, reliability, postmortem]
okf_version: "0.1"
reviewers:
  - alex
  - sam
---
# Quarterly incident retrospective

**Status:** draft for review

## Summary

Three incidents this quarter traced back to the same cause: a config reload
that races the health check. The fix lands the reload behind the readiness
gate so a half-applied config never serves traffic.

## Timeline

| When | What | Impact |
|---|---|---|
| 09:02 | config reload begins | none |
| 09:03 | health check flaps | 2m of 503s |
| 09:05 | reload completes | recovered |

> Action item: gate the reload behind readiness before the next release.
`

const BASE = '/api/apps/aws-control'

// The theme the boot response should report; set per openPreview() call so
// `/api/theme/boot` drives useTheme to the mode we are photographing.
let currentTheme = 'dark'

async function answer(route) {
  const url = new URL(route.request().url())
  const path = url.pathname
  if (path.endsWith('/accounts')) return json(route, ACCOUNTS)
  if (path === '/api/aws/consent') return json(route, CONSENT(url.searchParams.get('service') || 's3'))
  const app = path.startsWith(BASE) ? path.slice(BASE.length) : ''
  if (/^\/drive\/[^/]+\/preview$/.test(app)) {
    return json(route, { content: MD_WITH_FRONTMATTER, truncated: false, redacted: false })
  }
  if (/^\/drive\/[^/]+\/list$/.test(app)) return json(route, LISTING)
  if (/^\/drive\/[^/]+$/.test(app)) return json(route, DRIVE)
  if (/^\/costs\/[^/]+$/.test(app)) return json(route, { monthToDate: 2.25, currency: 'USD', fetchedAt: new Date().toISOString(), fresh: true, consentMissing: false })
  if (/^\/library\/[^/]+$/.test(app)) return json(route, { artifacts: [] })
  if (/^\/backup\/[^/]+$/.test(app)) return json(route, { nightly: false, runs: {}, remote: null, jobs: {} })
  if (app === '/profiles/available') return json(route, { supported: true, profiles: [], max: 20 })
  if (app.startsWith('/shares')) return json(route, { shares: [] })
  return handleBootRoute(route, path, { project: '/workspace', theme: currentTheme })
}

// ---- run ------------------------------------------------------------------
const { srv: server, base } = await serveDist()
// The default headless_shell binary is not installed here, but the full
// "Chrome for Testing" build is — point at it and run headless.
const CHROMIUM_BIN = process.env.KC_CHROMIUM_BIN || chromium.executablePath()
const browser = await chromium.launch({ executablePath: CHROMIUM_BIN, headless: true })
const failures = []
const check = (label, ok, detail) => {
  console.log(`ASSERT ${label} ${ok ? 'ok' : `MISMATCH ${detail ?? ''}`}`)
  if (!ok) failures.push(`${label}: ${detail ?? 'failed'}`)
}

const STRIP = '[data-testid="md-frontmatter"]'

/** Open the markdown preview in the given theme and return the page. */
async function openPreview(theme) {
  currentTheme = theme
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 }, deviceScaleFactor: 2 })
  await page.route('**/api/**', answer)
  await page.route('**/api/ws', (route) => route.abort())
  page.on('pageerror', (err) => console.log('PAGEERROR:', (err.stack || String(err)).slice(0, 400)))
  await page.addInitScript((mode) => {
    try {
      localStorage.setItem('mc-onboarded', '1')
      localStorage.setItem('mc-import-onboarded', '1')
      localStorage.setItem('mc-privacy-acked', '1')
      // useTheme reads the mode PREFERENCE from `mc-theme` ('dark'|'light'|'system');
      // seed it so the app resolves to the requested mode on first paint.
      localStorage.setItem('mc-theme', mode)
    } catch { /* sandboxed frame */ }
    // index.html hard-codes data-theme="dark" before React boots; overwrite it
    // so there is no dark flash before useTheme applies the resolved mode. The
    // default palette is `kiro`, so data-theme is `kiro-<mode>` and data-mode is
    // the bare mode (what the stylesheet and the preview key their tokens on).
    try {
      const el = document.documentElement
      el.dataset.theme = `kiro-${mode}`
      el.dataset.mode = mode
    } catch { /* no document yet */ }
  }, theme)
  await page.goto(`${base}/aws-control`, { waitUntil: 'domcontentloaded' })
  await page.waitForTimeout(1200)
  await page.locator('[data-testid="rail-files"]').click()
  await page.waitForTimeout(900)
  await page.locator('[data-testid="drive-preview-open"]').nth(0).click()
  await page.locator('[data-testid="drive-preview-text"]').waitFor({ state: 'visible', timeout: 5000 })
  // The resolved mode must actually be the one we asked for, or the "light"
  // shots would silently render dark (the data-theme/data-mode defaults).
  const resolved = await page.evaluate(() => document.documentElement.dataset.mode)
  check(`${theme}: app resolved to ${theme} mode`, resolved === theme, `data-mode=${resolved}`)
  await page.waitForTimeout(500)
  return page
}

for (const theme of ['light', 'dark']) {
  const page = await openPreview(theme)

  // The strip is present, expanded, and shows its heading label + rows.
  const probe = await page.locator('[data-testid="drive-preview-text"]').evaluate((el) => {
    const strip = el.querySelector('[data-testid="md-frontmatter"]')
    const toggle = strip?.querySelector('button')
    return {
      hasStrip: !!strip,
      expanded: toggle?.getAttribute('aria-expanded'),
      headingText: toggle?.textContent?.trim() || '',
      keys: Array.from(strip?.querySelectorAll('dt') || []).map((d) => d.textContent),
      bodyH1: el.querySelectorAll('h1').length,
      bodyTable: el.querySelectorAll('table').length,
      // the raw `title:` line must NOT be in the rendered body prose
      rawFence: (el.querySelector('.msg-content')?.textContent || '').includes('okf_version:'),
    }
  })
  check(`${theme}: metadata strip present`, probe.hasStrip)
  check(`${theme}: strip starts expanded`, probe.expanded === 'true', `aria-expanded=${probe.expanded}`)
  check(`${theme}: heading label shows a field count`, /\d+\s+field/i.test(probe.headingText), `heading="${probe.headingText}"`)
  check(`${theme}: lists the frontmatter keys`, probe.keys.includes('title') && probe.keys.includes('tags'), `keys=${probe.keys.join(',')}`)
  check(`${theme}: body heading still renders`, probe.bodyH1 === 1, `h1=${probe.bodyH1}`)
  check(`${theme}: body table still renders`, probe.bodyTable === 1, `table=${probe.bodyTable}`)
  check(`${theme}: raw frontmatter is not in the body prose`, !probe.rawFence)

  await page.screenshot({ path: `${OUT}/after-expanded-${theme}.png`, fullPage: false })
  console.log(`shot after-expanded-${theme}`)

  // Collapse the strip (the UX review's second evidence gap) and re-shoot.
  await page.locator(`${STRIP} button`).click()
  await page.waitForTimeout(300)
  const collapsed = await page.locator(STRIP).evaluate((strip) => {
    const toggle = strip.querySelector('button')
    return { expanded: toggle?.getAttribute('aria-expanded'), rows: strip.querySelectorAll('dd').length }
  })
  check(`${theme}: strip collapses on click`, collapsed.expanded === 'false' && collapsed.rows === 0, `expanded=${collapsed.expanded} rows=${collapsed.rows}`)
  await page.screenshot({ path: `${OUT}/after-collapsed-${theme}.png`, fullPage: false })
  console.log(`shot after-collapsed-${theme}`)

  await page.close()
}

await browser.close()
server.close()
if (failures.length) {
  console.log(`\nFAILED (${failures.length}):`)
  for (const f of failures) console.log(`  - ${f}`)
  process.exit(1)
}
console.log('\nall assertions passed')
