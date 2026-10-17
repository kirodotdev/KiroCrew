/**
 * Screenshots + assertions for two ChatEmbed defects, on the isolated entry
 * website/capture/embed-tool-status.html (the real ChatEmbed, gateway-free).
 *
 * The slot read is fulfilled with a RUNNING turn holding:
 *  - a finished call: role 'tool', its result on `meta.output` (the shape the
 *    backend persists -- the row is never re-roled to 'tool_result');
 *  - a call parked on an approval whose command is longer than 150 chars and
 *    ends in a destructive suffix.
 *
 * Assertions:
 *  - the finished call's pill is toned done (text-ok) and does not spin;
 *  - the approval preview carries the whole command, suffix included;
 *  - the finished call is not titled with the live `Running:` verb;
 *  - the crew chat window's list (`?surface=crew`, ChatMessageList as
 *    CrewChatWindow mounts it) previews the whole command too.
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6812 --strictPort   # in another shell
 *   node scripts/capture-embed-tool-status.mjs http://127.0.0.1:6812 <out-dir> [label]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6812'
const OUT = process.argv[3] || '../temp-screenshots/embed-tool-status'
const LABEL = process.argv[4] || 'after'
const ASSERT = LABEL === 'after'
mkdirSync(OUT, { recursive: true })

const SUFFIX = '&& rm -rf ./build-cache'
const LONG_COMMAND =
  'aws s3 sync ./dist s3://example-assets-beta/app/ --exclude "*.map" --exclude "*.log" ' +
  '--cache-control "public,max-age=31536000,immutable" --profile example --region us-west-2 ' +
  SUFFIX

const TRANSCRIPT = {
  messages: [
    { role: 'user', content: 'Build the app and upload it to the beta bucket.', cls: '', ts: '2026-10-13T17:00:00Z' },
    {
      role: 'tool', content: '🔧 Running: npx vite build', cls: '', ts: '2026-10-13T17:00:05Z',
      meta: { tool_call_id: 'tc-build', output: 'vite v8.1.4 building for production...\n✓ 412 modules transformed.\n✓ built in 3.21s' },
    },
    {
      role: 'tool', content: '🔧 Running: aws s3 sync ./dist s3://example-assets-beta/app/', cls: '', ts: '2026-10-13T17:00:09Z',
      meta: { tool_call_id: 'tc-sync' },
    },
    {
      role: 'permission', content: '', cls: '', ts: '2026-10-13T17:00:10Z',
      meta: { approval_id: 'ap-sync', tool_call_id: 'tc-sync', tool_input: { command: LONG_COMMAND }, is_shell: true },
    },
  ],
  running: true,
  title: 'Example app · deploy',
  has_more: false,
}

const browser = await chromium.launch()
const page = await browser.newPage({ viewport: { width: 760, height: 660 } })
await page.route('**/api/chat/slots/**', route => route.fulfill({ json: TRANSCRIPT }))
await page.route('**/api/chat', route => route.fulfill({ status: 200, body: '' }))
let failures = 0

await page.goto(`${BASE}/capture/embed-tool-status.html?theme=dark`, { waitUntil: 'networkidle' })
await page.getByText('npx vite build', { exact: false }).first().waitFor()
await page.locator('pre').filter({ hasText: 'aws s3 sync' }).first().waitFor()

const pill = page.getByRole('button', { name: /npx vite build/ })
const pillClass = (await pill.getAttribute('class')) || ''
const spins = await pill.locator('.animate-spin').count()
console.log(`[${LABEL}] finished pill: done-tone=${pillClass.includes('text-ok')} spinning=${spins > 0}`)
if (ASSERT && (!pillClass.includes('text-ok') || spins > 0)) {
  console.error('FAIL: finished call is not shown as done')
  failures++
}

const preview = (await page.locator('pre').filter({ hasText: 'aws s3 sync' }).first().textContent()) || ''
console.log(`[${LABEL}] approval preview: ${preview.length} chars, suffix shown=${preview.includes(SUFFIX)}`)
if (ASSERT && preview !== LONG_COMMAND) {
  console.error('FAIL: approval preview is not the whole command')
  failures++
}

// The finished row's title matches the main chat's, not the live `Running:` verb.
const pillText = ((await pill.textContent()) || '').trim()
console.log(`[${LABEL}] finished pill label: ${JSON.stringify(pillText)}`)
if (ASSERT && /^Running:/.test(pillText)) {
  console.error('FAIL: finished call still titled with the live "Running:" verb')
  failures++
}

await page.screenshot({ path: `${OUT}/${LABEL}.png`, fullPage: true })

// Crew chat window: the same list + approval row, mounted as CrewChatWindow does.
const crew = await browser.newPage({ viewport: { width: 760, height: 660 } })
await crew.route(u => new URL(u).pathname.startsWith('/api/'), route => route.fulfill({ json: {} }))
await crew.addInitScript(t => { window.__captureTranscript = t }, TRANSCRIPT)
await crew.goto(`${BASE}/capture/embed-tool-status.html?theme=dark&surface=crew`, { waitUntil: 'networkidle' })
const crewPre = crew.locator('pre').filter({ hasText: 'aws s3 sync' }).first()
await crewPre.waitFor()
const crewPreview = (await crewPre.textContent()) || ''
console.log(`[${LABEL}] crew window preview: ${crewPreview.length} chars, suffix shown=${crewPreview.includes(SUFFIX)}`)
if (ASSERT && crewPreview !== LONG_COMMAND) {
  console.error('FAIL: crew window approval preview is not the whole command')
  failures++
}
await crew.mouse.move(740, 640)
await crew.screenshot({ path: `${OUT}/${LABEL}-crew.png`, fullPage: true })

await browser.close()
if (failures) {
  console.error(`${failures} assertion failure(s)`)
  process.exit(1)
}
console.log('ALL GREEN')
