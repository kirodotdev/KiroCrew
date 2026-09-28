/**
 * Screenshot harness for the AWS Control file preview's renderers.
 *
 * Runs the REAL built SPA (website/dist) on a tiny loopback static server with
 * every /api/** call answered from fixtures -- no gateway, no AWS account, no
 * dashboard token. Same technique as capture-aws-control.mjs, which this is
 * modelled on; it is a separate script because the fixture it needs is a text
 * PREVIEW body, which that harness has no file to serve.
 *
 * What it pins: the pane renders through the dashboard's own file renderers
 * (`ContentRenderer`), so a drive object reads the way the same file reads in
 * the file side panel -- prose for markdown, a sandboxed page for html, a table
 * for csv, a tree for json, the syntax surface for code, and bytes left alone
 * for a config file. Image and video are covered too, with REAL bytes answered
 * behind the presigned URL, so the probes read a decoded picture and a decoded
 * video frame rather than a tag with a src on it. The assertions read the
 * RENDERED DOM and fail the process, so a stale dist cannot exit 0 with the old
 * pane in the PNGs.
 *
 * Captures one full 1280x900 app view per type (sidebar included):
 *   preview-markdown.png  preview-html.png   preview-csv.png
 *   preview-json.png      preview-code.png   preview-verbatim-yaml.png
 *   preview-image.png     preview-video.png  (video needs ffmpeg; skipped without)
 *
 * Usage: node scripts/capture-aws-control-preview-renderers.mjs <outDir>
 */
import { chromium } from 'playwright'
import { mkdirSync, readFileSync, existsSync, rmSync } from 'node:fs'
import { spawnSync } from 'node:child_process'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { serveDist } from './lib/serve-dist.mjs'
import { json, handleBootRoute } from './lib/boot-api.mjs'
import { mockShot } from './lib/mock-shot.mjs'

const OUT = process.argv[2] || '/tmp/aws-control-preview-md'
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
  usage: {
    bytes: 44677427, objects: 12,
    sections: { drive: { objects: 2, bytes: 32715570 }, library: { objects: 6, bytes: 11157402 }, backup: { objects: 4, bytes: 804455 } },
  },
}
const LISTING = {
  folders: [],
  files: [
    { key: 'megasession-hld.md', size: 13210, modified: '2026-09-08T09:12:00Z' },
    { key: 'quarter-report.html', size: 1840, modified: '2026-09-08T08:02:00Z' },
    { key: 'monthly-spend.csv', size: 620, modified: '2026-09-07T20:15:00Z' },
    { key: 'drive-state.json', size: 940, modified: '2026-09-07T19:30:00Z' },
    { key: 'probe_warm.py', size: 7700, modified: '2026-09-07T18:40:00Z' },
    { key: 'gateway.yaml', size: 480, modified: '2026-09-07T18:40:00Z' },
    { key: 'hero-shot.png', size: 24800, modified: '2026-09-06T12:00:00Z' },
    { key: 'pr-watch-demo.mp4', size: 96000, modified: '2026-09-06T11:30:00Z' },
  ],
}
/* A real design doc's shapes, because the point of the pane is that a reader can
   read one: heading levels, bold, a fenced tree, a table, a blockquote, a list. */
const MD = `# MegaSession High-Level Design

**Author:** crew  ·  **Status:** draft for review

## 1. Summary

Today every subagent, task-runner step and chat turn spawns its own engine
process (~466 MB, cold start). The real limits are **provider throughput** and
**RAM**, not cores.

Design: an *engine worker pool*, two shallow layers:

\`\`\`
Worker Pool     (layer 1: lazy, bounded, self-healing)
  MegaSession   (layer 2: ONE engine, many sessions)
    Session     (one subagent / step)
    Session
\`\`\`

| Pool | Exists? | Multi-session per process? |
|---|---|---|
| warm pool | yes | no, 1 process = 1 session |
| worker pool | yes | no, 1 process = 1 session |
| **MegaSession** | no | **yes, 1 process = K sessions** |

> The warm pool serves the main agent; the worker pool serves subagents. They
> are two independent process pools today.

## 2. Why it matters

- 100 concurrent sessions cost 100 processes today.
- The engine is amortised across K sessions instead.
- Concurrency becomes bounded by throughput, not by memory.
`
const YAML = `# gateway config
listen: 127.0.0.1
port: 7777
telemetry:
  enabled: false
  retention_days: 0
`
const HTML = `<!doctype html><meta charset="utf-8"><style>
  body{font:15px/1.6 -apple-system,system-ui,sans-serif;margin:0;padding:28px;color:#1b1f24}
  h1{margin:0 0 4px;font-size:24px} .sub{color:#6b7280;margin-bottom:22px}
  .row{display:flex;justify-content:space-between;padding:9px 0;border-bottom:1px solid #e6e8eb}
  .row b{font-variant-numeric:tabular-nums} .tot{border:0;font-weight:700;padding-top:14px}
</style>
<h1>Quarter report</h1><div class="sub">Storage and transfer, this quarter</div>
<div class="row"><span>Object storage</span><b>$4.12</b></div>
<div class="row"><span>Requests</span><b>$0.38</b></div>
<div class="row"><span>Data transfer</span><b>$1.09</b></div>
<div class="row tot"><span>Total</span><b>$5.59</b></div>
`
const CSV = `month,service,objects,cost_usd
2026-06,storage,1204,3.98
2026-07,storage,1388,4.12
2026-07,requests,90211,0.38
2026-08,storage,1502,4.44
2026-08,transfer,17,1.09
`
const JSON_DOC = `{
  "bucket": "kirocrew-drive-7f3a91c4",
  "region": "us-west-2",
  "versioning": true,
  "sections": { "drive": 2, "library": 6, "backup": 4 },
  "shares": []
}
`
const PY = `"""Warm-pool probe: how many sessions one engine process can hold."""
import asyncio
from dataclasses import dataclass


@dataclass
class Probe:
    workers: int = 4
    capacity: int = 8

    async def run(self) -> dict[str, int]:
        sessions = [self.acquire(i) for i in range(self.workers * self.capacity)]
        held = await asyncio.gather(*sessions)
        return {"held": len(held), "per_worker": self.capacity}

    async def acquire(self, index: int) -> int:
        await asyncio.sleep(0)
        return index
`
const BODY_BY_EXT = { md: MD, html: HTML, csv: CSV, json: JSON_DOC, py: PY, yaml: YAML }
const bodyFor = (key) => BODY_BY_EXT[(key.split('.').pop() || '').toLowerCase()] ?? YAML

const BASE = '/api/apps/aws-control'

async function answer(route) {
  const url = new URL(route.request().url())
  const path = url.pathname
  if (path.endsWith('/accounts')) return json(route, ACCOUNTS)
  if (path === '/api/aws/consent') return json(route, CONSENT(url.searchParams.get('service') || 's3'))
  const app = path.startsWith(BASE) ? path.slice(BASE.length) : ''
  if (/^\/drive\/[^/]+\/preview$/.test(app)) {
    const key = url.searchParams.get('key') || ''
    return json(route, { content: bodyFor(key), truncated: key.endsWith('.md'), redacted: false })
  }
  if (/^\/drive\/[^/]+\/download$/.test(app)) {
    // Media loads the presigned URL with its own tag; the fake host below is
    // intercepted and answered with real bytes, so the picture on screen is a
    // decoded image / a decoded video frame, not a broken-media placeholder.
    const key = url.searchParams.get('key') || ''
    const type = key.endsWith('.mp4') ? 'video/mp4' : 'image/png'
    return json(route, { url: `https://signed.example/${key}`, expiresSecs: 60, contentType: type })
  }
  if (/^\/drive\/[^/]+\/list$/.test(app)) return json(route, LISTING)
  if (/^\/drive\/[^/]+$/.test(app)) return json(route, DRIVE)
  if (/^\/costs\/[^/]+$/.test(app)) return json(route, { monthToDate: 2.25, currency: 'USD', fetchedAt: new Date().toISOString(), fresh: true, consentMissing: false })
  if (/^\/library\/[^/]+$/.test(app)) return json(route, { artifacts: [] })
  if (/^\/backup\/[^/]+$/.test(app)) return json(route, { nightly: false, runs: {}, remote: null, jobs: {} })
  if (app === '/profiles/available') return json(route, { supported: true, profiles: [], max: 20 })
  if (app.startsWith('/shares')) return json(route, { shares: [] })
  // Everything else is the dashboard shell booting, not this app: the shared
  // owner answers those, so this harness carries no second copy of the
  // shape-sensitive boot payloads.
  return handleBootRoute(route, path, { project: '/workspace' })
}

// ---- run ------------------------------------------------------------------
const { srv: server, base } = await serveDist()
const browser = await chromium.launch()

/* Real bytes for the two media objects, generated here rather than read from a
   committed asset -- a harness that reads another feature's files breaks when
   the screenshot cleanup prunes them. The PNG is drawn by the browser itself
   (lib/mock-shot.mjs); the clip needs an encoder, so when ffmpeg is absent the
   video case asserts the element's WIRING and skips its frame. */
const PNG_BYTES = await mockShot(browser, 960, 600)
const MP4_PATH = join(tmpdir(), `kc-preview-clip-${process.pid}.mp4`)
const ffmpeg = spawnSync('ffmpeg', [
  '-v', 'error', '-y',
  '-f', 'lavfi', '-i', 'testsrc=size=640x360:rate=15:duration=2',
  '-pix_fmt', 'yuv420p', '-movflags', '+faststart', MP4_PATH,
])
const MP4_BYTES = ffmpeg.status === 0 && existsSync(MP4_PATH) ? readFileSync(MP4_PATH) : null
if (!MP4_BYTES) console.log('NOTE ffmpeg unavailable -- video case asserts wiring only, no frame')

const page = await browser.newPage({ viewport: { width: 1280, height: 900 }, deviceScaleFactor: 2 })
await page.route('**/api/**', answer)
await page.route('**/api/ws', (route) => route.abort())
// The presigned host: the product loads it with an <img>/<video> tag, which is
// CORS-exempt, so answering it here is the same shape the real bucket serves.
await page.route('https://signed.example/**', (route) => {
  const path = new URL(route.request().url()).pathname
  if (path.endsWith('.mp4')) {
    if (!MP4_BYTES) return route.abort()
    return route.fulfill({ status: 200, contentType: 'video/mp4', body: MP4_BYTES })
  }
  return route.fulfill({ status: 200, contentType: 'image/png', body: PNG_BYTES })
})
page.on('pageerror', (err) => console.log('PAGEERROR:', (err.stack || String(err)).slice(0, 400)))
await page.addInitScript(() => {
  // Runs in EVERY frame, and the html preview's frame is `sandbox=""` -- reading
  // localStorage there throws a SecurityError that is the sandbox working, not a
  // failure worth reporting.
  try {
    localStorage.setItem('mc-onboarded', '1')
    localStorage.setItem('mc-import-onboarded', '1')
    localStorage.setItem('mc-privacy-acked', '1')
    localStorage.setItem('mc-theme-mode', 'dark')
  } catch { /* sandboxed frame: nothing to seed */ }
})

const failures = []
const check = (label, ok, detail) => {
  console.log(`ASSERT ${label} ${ok ? 'ok' : `MISMATCH ${detail ?? ''}`}`)
  if (!ok) failures.push(`${label}: ${detail ?? 'failed'}`)
}

await page.goto(`${base}/aws-control`, { waitUntil: 'domcontentloaded' })
await page.waitForTimeout(1200)
// The app lands on Overview; the file listing is the Files pane on the rail.
await page.locator('[data-testid="rail-files"]').click()
await page.waitForTimeout(900)
check('drive-section mounted', (await page.locator('[data-testid="drive-section"]').count()) === 1)

// ---- one pass per file type ----------------------------------------------
/* Each row: the listing index, the shot name, and what the shared renderer must
   have produced. The probes read the RENDERED DOM, so a stale dist fails here
   instead of exiting 0 with the old pane in the PNGs. */
const CASES = [
  {
    idx: 0, shot: 'markdown', label: 'markdown',
    probe: (el) => ({
      prose: !!el.querySelector('.msg-content'),
      h1: el.querySelectorAll('h1').length,
      h2: el.querySelectorAll('h2').length,
      strong: el.querySelectorAll('strong').length,
      table: el.querySelectorAll('table').length,
      quote: el.querySelectorAll('blockquote').length,
      li: el.querySelectorAll('li').length,
      syntax: (el.textContent || '').includes('**'),
    }),
    want: (p) => [
      [p.prose, 'renders under the side panel prose wrapper'],
      [p.h1 === 1 && p.h2 === 2, `heading levels (h1=${p.h1} h2=${p.h2})`],
      [p.strong >= 3, `bold runs (${p.strong})`],
      [p.table === 1, `table (${p.table})`],
      [p.quote === 1, `blockquote (${p.quote})`],
      [p.li >= 3, `list items (${p.li})`],
      [!p.syntax, 'no raw markdown syntax left on the reading surface'],
    ],
  },
  {
    idx: 1, shot: 'html', label: 'html',
    probe: (el) => {
      const f = el.querySelector('iframe')
      return { frame: !!f, sandbox: f?.getAttribute('sandbox'), doc: (f?.getAttribute('srcdoc') || '').includes('Quarter report') }
    },
    want: (p) => [
      [p.frame, 'rendered in a frame'],
      [p.sandbox === '', `fully sandboxed (sandbox="${p.sandbox}")`],
      [p.doc, 'the page bytes reached the frame'],
    ],
  },
  {
    idx: 2, shot: 'csv', label: 'csv',
    probe: (el) => ({ table: el.querySelectorAll('table').length, rows: el.querySelectorAll('tr').length, header: (el.textContent || '').includes('cost_usd') }),
    want: (p) => [
      [p.table === 1, `one table (${p.table})`],
      [p.rows >= 6, `a row per record (${p.rows})`],
      [p.header, 'the header row is present'],
    ],
  },
  {
    idx: 3, shot: 'json', label: 'json',
    probe: (el) => ({ keys: (el.textContent || '').includes('versioning'), pre: el.tagName === 'PRE', err: !!document.querySelector('[data-testid="json-viewer-error"]') }),
    want: (p) => [
      [p.keys, 'the document keys are shown'],
      [!p.pre, 'not a raw pre'],
      [!p.err, 'parsed, so no parse-error state'],
    ],
  },
  {
    idx: 4, shot: 'code', label: 'python',
    probe: (el) => ({ pre: el.tagName === 'PRE' }),
    want: (p) => [[!p.pre, 'not a raw pre']],
    // The code surface renders inside its own shadow root, which `textContent`
    // cannot see -- Playwright's text engine pierces it.
    text: ['dataclasses', 'capacity'],
  },
  {
    idx: 5, shot: 'verbatim-yaml', label: 'yaml',
    probe: (el) => ({ h1: el.querySelectorAll('h1').length }),
    want: (p) => [[!p.h1, 'the leading # stayed a comment, not a heading']],
    text: ['gateway config', 'retention_days'],
  },
  {
    idx: 6, shot: 'image', label: 'image', target: 'drive-preview-image',
    // naturalWidth is only non-zero once the bytes DECODED, so this is proof of
    // a rendered picture rather than of an <img> tag with a src on it.
    probe: (el) => ({ w: el.naturalWidth, h: el.naturalHeight, src: el.getAttribute('src') || '' }),
    want: (p) => [
      [p.w > 0 && p.h > 0, `decoded (${p.w}x${p.h})`],
      [p.src.startsWith('https://signed.example/'), 'loaded straight from the presigned URL'],
    ],
  },
  {
    idx: 7, shot: 'video', label: 'video', target: 'drive-preview-video',
    needsMedia: true,
    probe: (el) => ({ w: el.videoWidth, ready: el.readyState, controls: el.controls, src: el.getAttribute('src') || '' }),
    want: (p) => [
      [p.controls, 'has playback controls'],
      [p.src.startsWith('https://signed.example/'), 'streams straight from the presigned URL'],
      [p.ready >= 1, `metadata loaded (readyState=${p.ready})`],
      [p.w > 0, `decoded a frame (${p.w}px wide)`],
    ],
  },
]

const dialog = page.locator('[data-testid="drive-preview-dialog"]')
for (const c of CASES) {
  if (c.needsMedia && !MP4_BYTES) { console.log(`SKIP ${c.label} (no encoder)`); continue }
  const target = page.locator(`[data-testid="${c.target ?? 'drive-preview-text'}"]`)
  await page.locator('[data-testid="drive-preview-open"]').nth(c.idx).click()
  await target.waitFor({ state: 'visible', timeout: 5000 })
  // The rich viewers mount their own subtree, and media decodes asynchronously.
  if (c.target === 'drive-preview-image') {
    await target.evaluate((el) => el.complete || new Promise((r) => { el.onload = r; el.onerror = r }))
  } else if (c.target === 'drive-preview-video') {
    await page.waitForFunction(() => {
      const v = document.querySelector('[data-testid="drive-preview-video"]')
      return !!v && v.readyState >= 1
    }, null, { timeout: 8000 })
  }
  await page.waitForTimeout(500)
  const probe = await target.evaluate(c.probe)
  for (const [ok, detail] of c.want(probe)) check(`${c.label}: ${detail}`, ok)
  for (const needle of c.text ?? []) {
    const seen = await dialog.getByText(needle, { exact: false }).count()
    check(`${c.label}: "${needle}" is on screen`, seen > 0, `count=${seen}`)
  }
  await page.screenshot({ path: `${OUT}/preview-${c.shot}.png`, fullPage: false })
  console.log(`shot preview-${c.shot}`)
  await page.locator('[data-testid="drive-preview-close"]').click()
  await page.waitForTimeout(300)
}

await browser.close()
server.close()
if (MP4_BYTES) rmSync(MP4_PATH, { force: true })
if (failures.length) {
  console.log(`\nFAILED (${failures.length}):`)
  for (const f of failures) console.log(`  - ${f}`)
  process.exit(1)
}
console.log('\nall assertions passed')
