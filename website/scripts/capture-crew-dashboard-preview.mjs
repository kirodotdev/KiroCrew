/**
 * Evidence for the dashboard preview link: the link `dashboard_preview` hands a crewmate opens
 * the Members page with the Dashboard tab on the STAGED page, under a preview band.
 *
 * Drives the existing isolated entry website/capture/members-page.html (the REAL
 * MembersPage) with ?route= set to the exact link `instance.preview_url` builds.
 * Gateway-free: every /api/ call is answered here. The staged page is a fixture
 * standing in for a composed catalog template; the frame mints it through the real
 * sandbox path (the mint is answered with a blob URL of the html the host built).
 *
 *   npx vite --host 127.0.0.1 --port 6841 --strictPort   # in another shell
 *   node scripts/capture-crew-dashboard-preview.mjs http://127.0.0.1:6841 ../temp-screenshots/dash-preview
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { MEMBERS } from './lib/members-fixtures.mjs'

const BASE = process.argv[2] || 'http://127.0.0.1:6841'
const OUT = process.argv[3] || '../temp-screenshots/dash-preview'
mkdirSync(OUT, { recursive: true })

const STAGED = `<!doctype html><html><head><style>
  body{font:14px/1.45 system-ui,sans-serif;margin:0;padding:16px;color:var(--text);background:var(--bg)}
  h1{font-size:18px;margin:0 0 4px} .sub{color:var(--muted);margin:0 0 14px}
  .grid{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin-bottom:14px}
  .card{border:1px solid var(--border);border-radius:8px;padding:10px}
  .n{font-size:22px;font-weight:600} .l{color:var(--muted);font-size:12px}
  li{margin:4px 0}
</style></head><body>
  <h1>Project report</h1><p class="sub">Staged template: project-report v2</p>
  <div class="grid">
    <div class="card"><div class="n">4</div><div class="l">Workstreams</div></div>
    <div class="card"><div class="n">2</div><div class="l">Need you</div></div>
    <div class="card"><div class="n">1</div><div class="l">Open PR</div></div>
  </div>
  <ul><li>Weekly metrics refresh: on track</li><li>Query cost review: waiting on you</li><li>Schema change: in review</li></ul>
  <script>parent.postMessage({type:'kirocrew-dashboard:ready'},'*')</script>
</body></html>`

const browser = await chromium.launch()
let failures = 0
const check = (label, ok) => {
  console.log(`${label} => ${ok ? 'OK' : 'FAIL'}`)
  if (!ok) failures++
}

async function open({ theme, staged = true, viewport = { width: 1360, height: 820 } }) {
  const page = await browser.newPage({ viewport, deviceScaleFactor: 1 })
  page.on('pageerror', e => {
    console.error('pageerror:', e.message)
    failures++
  })
  const dashboardReads = []
  await page.route(u => new URL(u).pathname.startsWith('/api/'), async route => {
    const url = new URL(route.request().url())
    const path = url.pathname
    const json = (body, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) })
    if (path === '/api/members') return json({ members: MEMBERS, default_agent: 'kirocrew' })
    if (path === '/api/crons') return json({ jobs: [] })
    if (path === '/api/webhooks') return json({ tokens: [] })
    if (path === '/api/agents') return json({ agents: [], default_agent: 'kirocrew' })
    if (path === '/api/sandbox-doc' && route.request().method() === 'POST') {
      // A data: URL of exactly what the host built, so the frame shows the real srcdoc.
      const { html } = JSON.parse(route.request().postData() || '{}')
      return json({ url: 'data:text/html;base64,' + Buffer.from(html).toString('base64') })
    }
    if (/^\/api\/members\/[^/]+\/dashboard$/.test(path)) {
      dashboardReads.push(url.search)
      if (url.searchParams.get('preview') === '1') {
        if (!staged) return json({ error: 'nothing is staged to preview', code: 'no_preview' }, 404)
        return json({
          instance_version: 1, template: { id: 'project-report', version: 2 }, html: STAGED,
          rendered_html: STAGED, manifest: { id: 'project-report', version: 2, fields: {} },
          state: 'live', preview: true,
        })
      }
      return json({ error: 'the capture serves only the preview read', code: 'capture' }, 500)
    }
    const thread = path.match(/^\/api\/members\/([^/]+)\/thread$/)
    if (thread) {
      const slug = decodeURIComponent(thread[1])
      return json({ slot_key: `member-${slug}`, slug, member: slug, created: false })
    }
    if (/^\/api\/members\/[^/]+\/activity$/.test(path)) return json({ slug: 'radar', member: 'radar', capped: false, entries: [] })
    if (/^\/api\/chat\/slots\/[^/]+$/.test(path)) {
      return json({ key: 'member-radar', title: 'radar', running: false, messages: [
        { role: 'user', content: 'Can I see a dashboard that tracks my workstreams?', ts: '2026-10-10T01:00:00Z' },
        { role: 'assistant', content: 'Staged for a look -- nothing has changed yet. [Open the dashboard preview](/members?member=radar&dashboard=preview), then tell me whether to keep it.', ts: '2026-10-10T01:00:05Z' },
      ] })
    }
    if (/\/api\/chat\/(tags|pins|folders|tag-columns)$/.test(path)) return route.fulfill({ status: 200, contentType: 'application/json', body: '[]' })
    const isList = /commands|skills|agents$|sessions|files|history|models|artifacts|folders|slots$/.test(path)
    return route.fulfill({ status: 200, contentType: 'application/json', body: isList ? '[]' : '{}' })
  })
  // Panel CLOSED before the link is opened: the link itself must open it.
  // try: init scripts also run in the sandboxed dashboard frame, which has no storage.
  await page.addInitScript(() => {
    try { localStorage.setItem('mc-members-panel-open', '0') } catch { /* sandboxed frame */ }
  })
  const route = encodeURIComponent('/members?member=radar&dashboard=preview')
  await page.goto(`${BASE}/capture/members-page.html?theme=${theme}&nav=1&route=${route}`)
  await page.waitForSelector('[data-capture-root]')
  return { page, dashboardReads }
}

for (const theme of ['dark', 'light']) {
  const { page, dashboardReads } = await open({ theme })
  const band = page.getByTestId('crew-dashboard-preview-band')
  await band.waitFor({ state: 'visible', timeout: 15000 })
  check(`${theme}: preview band names the crewmate`, /radar made this draft/.test((await band.textContent()) || ''))
  await page.getByTestId('crew-dashboard-iframe').waitFor({ state: 'visible', timeout: 15000 })
  const frame = page.frameLocator('[data-testid="crew-dashboard-iframe"]')
  await frame.getByText('Project report').waitFor({ timeout: 15000 })
  check(`${theme}: frame shows the staged page`, true)
  check(`${theme}: the read asked for the preview`, dashboardReads.some(q => q.includes('preview=1') && q.includes('member=radar')))
  check(`${theme}: URL is the preview link`, /dashboard=preview/.test((await page.locator('[data-capture-url-text]').textContent()) || ''))
  await page.screenshot({ path: `${OUT}/preview-${theme}.png` })
  await page.close()
}

// Phone width, where the panel is a full-width overlay: the band must wrap so the
// sentence keeps a readable width and the button drops to its own line.
{
  const { page } = await open({ theme: 'dark', viewport: { width: 390, height: 844 } })
  const band = page.getByTestId('crew-dashboard-preview-band')
  await band.waitFor({ state: 'visible', timeout: 15000 })
  const text = await band.locator('p').boundingBox()
  check('narrow: sentence keeps a readable width', !!text && text.width >= 180)
  const exit = await page.getByTestId('crew-dashboard-preview-exit').boundingBox()
  check('narrow: button does not overlap the sentence', !!text && !!exit && (exit.y >= text.y + text.height - 1 || exit.x >= text.x + text.width - 1))
  await page.screenshot({ path: `${OUT}/preview-narrow-dark.png` })
  await page.close()
}

{
  const { page } = await open({ theme: 'dark', staged: false })
  const none = page.getByTestId('crew-dashboard-preview-none')
  await none.waitFor({ state: 'visible', timeout: 15000 })
  check('nothing-staged: notice shown', /There's no draft to show/.test((await none.textContent()) || ''))
  check('nothing-staged: no error retry', (await page.getByTestId('crew-dashboard-error-retry').count()) === 0)
  await page.screenshot({ path: `${OUT}/preview-none-dark.png` })
  await page.close()
}

await browser.close()
if (failures) {
  console.error(`${failures} assertion(s) failed`)
  process.exit(1)
}
console.log(`done - evidence in ${OUT}/`)
