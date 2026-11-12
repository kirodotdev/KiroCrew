/**
 * Evidence for #28621: a bot's Dashboard tab saves the page as one HTML file that
 * opens offline and looks like the tab.
 *
 * Drives the REAL MembersPage (capture/members-page.html) gateway-free. The live page
 * is a fixture standing in for a composed template: Tailwind utility classes, theme
 * variables, a bound field and an inline script that draws a bar. For each theme the
 * harness clicks Download, shoots the confirm, confirms, catches the file, then opens
 * that file in a fresh context where EVERY request is refused, and shoots it there.
 *
 *   node scripts/capture-28621-dashboard-download.mjs http://127.0.0.1:<port> <outdir>
 */
import { chromium } from 'playwright'
import { mkdirSync, readFileSync } from 'node:fs'
import { pathToFileURL } from 'node:url'
import { MEMBERS } from './lib/members-fixtures.mjs'

const BASE = process.argv[2]
const OUT = process.argv[3] || '../temp-screenshots/dash-download'
if (!BASE) throw new Error('usage: capture-28621-dashboard-download.mjs <base-url> [outdir]')
mkdirSync(OUT, { recursive: true })

const PAGE = `
<div class="p-4 space-y-3">
  <h1 class="text-lg font-semibold text-[var(--text-strong)]">Release watch</h1>
  <div class="grid grid-cols-3 gap-3">
    <div class="rounded-lg border border-[var(--border)] bg-[var(--card)] p-3">
      <div class="text-2xl font-semibold" data-dashboard-field="open">7</div>
      <div class="text-xs text-[var(--muted)]">Open items</div>
    </div>
    <div class="rounded-lg border border-[var(--border)] bg-[var(--card)] p-3">
      <div class="text-2xl font-semibold">2</div><div class="text-xs text-[var(--muted)]">Need you</div>
    </div>
    <div class="rounded-lg border border-[var(--border)] bg-[var(--card)] p-3">
      <div class="text-2xl font-semibold">1</div><div class="text-xs text-[var(--muted)]">Blocked</div>
    </div>
  </div>
  <div class="rounded-lg border border-[var(--border)] bg-[var(--card)] p-3">
    <div class="text-xs text-[var(--muted)] mb-2">Done this week</div>
    <div id="bar" class="h-3 rounded bg-[var(--accent)]" style="width:0"></div>
    <div id="drawn" class="text-xs text-[var(--muted)] mt-2"></div>
  </div>
</div>
<script>
  document.getElementById('bar').style.width = '64%';
  document.getElementById('drawn').textContent = 'Drawn by the page script: 64%';
  parent.postMessage({type:'kirocrew-dashboard:ready'},'*');
</script>`

const browser = await chromium.launch()
let failures = 0
const check = (label, ok) => {
  console.log(`${label} => ${ok ? 'OK' : 'FAIL'}`)
  if (!ok) failures++
}

async function open(theme) {
  const context = await browser.newContext({ viewport: { width: 1360, height: 820 }, acceptDownloads: true })
  const page = await context.newPage()
  page.on('pageerror', e => { console.error('pageerror:', e.message); failures++ })
  await page.route(u => new URL(u).pathname.startsWith('/api/'), async route => {
    const url = new URL(route.request().url())
    const path = url.pathname
    const json = (body, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) })
    if (path === '/api/members') return json({ members: MEMBERS, default_agent: 'kirocrew' })
    if (path === '/api/crons') return json({ jobs: [] })
    if (path === '/api/webhooks') return json({ tokens: [] })
    if (path === '/api/teams') return json({ teams: [] })
    if (path === '/api/agents') return json({ agents: [], default_agent: 'kirocrew' })
    if (path === '/api/sandbox-doc' && route.request().method() === 'POST') {
      const { html } = JSON.parse(route.request().postData() || '{}')
      return json({ url: 'data:text/html;base64,' + Buffer.from(html).toString('base64') })
    }
    if (/^\/api\/members\/[^/]+\/dashboard$/.test(path)) {
      return json({
        instance_version: 3, template: { id: 'release-watch', version: 1 }, html: PAGE,
        rendered_html: PAGE, manifest: { id: 'release-watch', version: 1, fields: {} },
        state: 'live', scripts: true,
      })
    }
    const thread = path.match(/^\/api\/members\/([^/]+)\/thread$/)
    if (thread) {
      const slug = decodeURIComponent(thread[1])
      return json({ slot_key: `member-${slug}`, slug, member: slug, created: false })
    }
    if (/^\/api\/members\/[^/]+\/activity$/.test(path)) return json({ slug: 'radar', member: 'radar', capped: false, entries: [] })
    if (/^\/api\/chat\/slots\/[^/]+$/.test(path)) return json({ key: 'member-radar', title: 'radar', running: false, messages: [] })
    if (/\/api\/chat\/(tags|pins|folders|tag-columns)$/.test(path)) return route.fulfill({ status: 200, contentType: 'application/json', body: '[]' })
    const isList = /commands|skills|agents$|sessions|files|history|models|artifacts|folders|slots$/.test(path)
    return route.fulfill({ status: 200, contentType: 'application/json', body: isList ? '[]' : '{}' })
  })
  const route = encodeURIComponent('/members?member=radar')
  await page.goto(`${BASE}/capture/members-page.html?theme=${theme}&nav=1&route=${route}`)
  await page.waitForSelector('[data-capture-root]')
  return { context, page }
}

let n = 0
const shot = async (target, name) => {
  n += 1
  const file = `${OUT}/${String(n).padStart(2, '0')}-${name}.png`
  await target.screenshot({ path: file })
  return file
}

for (const theme of ['dark', 'light']) {
  const { context, page } = await open(theme)
  await page.getByTestId('crew-dashboard-iframe').waitFor({ state: 'visible', timeout: 15000 })
  await page.frameLocator('[data-testid="crew-dashboard-iframe"]').getByText('Drawn by the page script').waitFor({ timeout: 15000 })
  const button = page.getByTestId('crew-dashboard-download')
  const opacity = () => button.evaluate(el => Number(getComputedStyle(el).opacity))
  const settled = async (want) => page.waitForFunction(
    ([w]) => Number(getComputedStyle(document.querySelector('[data-testid="crew-dashboard-download"]')).opacity) === w,
    [want], { timeout: 5000 },
  )
  // At rest the button is hidden and takes no row: the page starts at the panel's top.
  await page.mouse.move(5, 400)
  await settled(0)
  const frameTop = await page.getByTestId('crew-dashboard-iframe').evaluate(el => el.getBoundingClientRect().top)
  const hostTop = await page.getByTestId('crew-dashboard-frame').evaluate(el => el.getBoundingClientRect().top)
  check(`${theme}: hidden at rest`, (await opacity()) === 0)
  check(`${theme}: no row of its own`, Math.abs(frameTop - hostTop) < 1)
  await shot(page, `${theme}-tab-rest`)

  // Hovering the page itself (the iframe) shows it in the corner.
  const box = await page.getByTestId('crew-dashboard-iframe').boundingBox()
  await page.mouse.move(box.x + box.width / 2, box.y + 200)
  await settled(1)
  check(`${theme}: shown on hover over the page`, (await opacity()) === 1)
  await shot(page, `${theme}-tab-hover`)

  await button.click()
  const dialog = page.getByRole('dialog')
  await dialog.waitFor({ state: 'visible', timeout: 5000 })
  check(`${theme}: confirm says the chat is not included`, /without the chat\. Anyone you send the file to/.test((await dialog.textContent()) || ''))
  // Shot once the dialog's open transition has finished, not mid fade.
  await page.waitForFunction(() => {
    const el = document.querySelector('[role="dialog"]')
    return !!el && el.getAnimations({ subtree: true }).every(a => a.playState !== 'running')
      && Number(getComputedStyle(el).opacity) === 1
  }, null, { timeout: 5000 })
  await shot(page, `${theme}-confirm`)

  const [download] = await Promise.all([
    page.waitForEvent('download', { timeout: 15000 }),
    dialog.getByRole('button', { name: 'Download', exact: true }).click(),
  ])
  const name = download.suggestedFilename()
  check(`${theme}: file is named after the bot`, /^.+ \d{4}-\d{2}-\d{2}\.html$/.test(name))
  const saved = `${OUT}/${theme}-${name.replace(/\s+/g, '_')}`
  await download.saveAs(saved)
  const html = readFileSync(saved, 'utf8')
  check(`${theme}: no script loaded by URL`, !/<script[^>]*\ssrc=/.test(html))
  check(`${theme}: runtime inlined`, html.length > 100_000)
  console.log(`${theme}: ${name} ${(html.length / 1024).toFixed(0)} KB`)
  await context.close()

  // The saved file, opened with the network refused: anything it still fetched would fail.
  const offline = await browser.newContext({ viewport: { width: 900, height: 420 } })
  let fetched = 0
  await offline.route(u => u.protocol !== 'file:', r => { fetched++; return r.abort() })
  const filePage = await offline.newPage()
  await filePage.goto(pathToFileURL(saved).href)
  // A scripted page is saved inside a sandboxed frame, as the tab shows it.
  const sandbox = await filePage.locator('iframe').getAttribute('sandbox')
  check(`${theme}: saved page runs in an allow-scripts-only frame`, sandbox === 'allow-scripts')
  const inner = filePage.frameLocator('iframe')
  await inner.getByText('Drawn by the page script: 64%').waitFor({ timeout: 10000 })
  const width = await inner.locator('#bar').evaluate(el => el.getBoundingClientRect().width)
  const radius = await inner.locator('.rounded-lg').first().evaluate(el => getComputedStyle(el).borderTopLeftRadius)
  check(`${theme}: offline file ran its script`, width > 100)
  check(`${theme}: offline file has Tailwind styles`, radius === '8px')
  check(`${theme}: offline file fetched nothing`, fetched === 0)
  await shot(filePage, `${theme}-saved-file-offline`)
  await offline.close()
}

// The two failure notices, in place over the page. The tab's frame loads the runtime as
// a <script>; only the download reads it with fetch(), so only that read is broken here:
// refused for "download failed", answered with more than the 10 MB cap for "over 10 MB".
for (const [kind, text] of [['failed', 'Download failed. Try again.'], ['too-large', 'This dashboard is over 10 MB']]) {
  for (const theme of ['dark', 'light']) {
    const { context, page } = await open(theme)
    await page.route('**/vendor/tailwindcss-browser.js', (route) => {
      if (route.request().resourceType() !== 'fetch') return route.fallback()
      if (kind === 'failed') return route.abort()
      return route.fulfill({ status: 200, contentType: 'text/javascript', body: 'x'.repeat(11 * 1024 * 1024) })
    })
    await page.frameLocator('[data-testid="crew-dashboard-iframe"]').getByText('Drawn by the page script').waitFor({ timeout: 15000 })
    const box = await page.getByTestId('crew-dashboard-iframe').boundingBox()
    await page.mouse.move(box.x + box.width / 2, box.y + 200)
    await page.getByTestId('crew-dashboard-download').click()
    const dialog = page.getByRole('dialog')
    await dialog.waitFor({ state: 'visible', timeout: 5000 })
    await dialog.getByRole('button', { name: 'Download', exact: true }).click()
    const notice = page.getByTestId('crew-dashboard-download-error')
    await notice.waitFor({ state: 'visible', timeout: 15000 })
    check(`${theme} ${kind}: notice says so`, ((await notice.textContent()) || '').includes(text))
    await dialog.waitFor({ state: 'hidden', timeout: 5000 })
    await shot(page, `${theme}-error-${kind}`)
    await notice.getByRole('button', { name: /dismiss/i }).click()
    check(`${theme} ${kind}: notice can be dismissed`, (await page.getByTestId('crew-dashboard-download-error').count()) === 0)
    await context.close()
  }
}

await browser.close()
if (failures) {
  console.error(`${failures} assertion(s) failed`)
  process.exit(1)
}
console.log(`done - evidence in ${OUT}/`)
