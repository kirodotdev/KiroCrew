/**
 * Screenshot harness for issue #3584: the four chat-surface action rows that
 * hid behind `opacity-0` + `group-hover/*` with no touch escape hatch.
 *
 * Runs the REAL built SPA (website/dist) through the shared transcript
 * harness, then flips the pointer to `(hover: none)` / `pointer: coarse` via
 * CDP `Emulation.setEmulatedMedia` — the same media query the fix branches
 * on — so every shot shows what a phone/tablet user actually gets: the
 * actions VISIBLE without hover, grown to 40px targets.
 *
 * Four surfaces, four element shots:
 *   1. CodeBlock header (copy button)
 *   2. DiffBlock header (open/split/copy actions)
 *   3. MarkdownRenderer mermaid figure (enlarge button)
 *   4. PinnedMessagesPanel row (copy/link/jump/unpin actions)
 *
 * Usage: node scripts/capture-touch-hover-actions.mjs [outDir]
 */
import { mkdirSync } from 'node:fs'
import { join } from 'node:path'
import { openTranscriptHarness } from './lib/transcript-harness.mjs'
import { json } from './lib/boot-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/touch-hover-actions-3584'
const SLOT = 'chat-touch-3584'
const PROJECT = '/home/user/workspace/KiroCrew'

mkdirSync(OUT, { recursive: true })

const now = Math.floor(Date.now() / 1000)

const CODE_MD = [
  'Here is the helper:',
  '',
  '```ts',
  'export function greet(name: string): string {',
  "  return `Hello, ${name}!`",
  '}',
  '```',
].join('\n')

const DIFF_MD = [
  'And the change:',
  '',
  '```diff',
  '--- a/src/greet.ts',
  '+++ b/src/greet.ts',
  '@@ -1,3 +1,3 @@',
  ' export function greet(name: string): string {',
  "-  return `Hello, ${name}!`",
  "+  return `Hello, ${name}! Welcome back.`",
  ' }',
  '```',
].join('\n')

const MERMAID_MD = [
  'The flow:',
  '',
  '```mermaid',
  'graph TD;A[Request]-->B[Gateway];B-->C[Agent];',
  '```',
].join('\n')

const slots = [{
  key: SLOT,
  title: 'Touch actions evidence',
  running: false,
  last_message: 'Rendered the three block types.',
  messages: 6,
  agent: 'kirocrew',
  memory_mode: 'persistent',
  project: PROJECT,
  modified: now,
  source_links: [],
  source_links_total: 0,
}]

const detail = {
  running: false,
  has_more: false,
  total: 6,
  queue: [],
  project: PROJECT,
  messages: [
    { role: 'user', ts: now - 500, content: 'Show me the helper function.' },
    { role: 'assistant', ts: now - 480, content: CODE_MD },
    { role: 'user', ts: now - 400, content: 'What changed in the last commit?' },
    { role: 'assistant', ts: now - 380, content: DIFF_MD },
    { role: 'user', ts: now - 300, content: 'Draw the request flow.' },
    { role: 'assistant', ts: now - 280, content: MERMAID_MD },
  ],
}

const pins = {
  pins: [
    {
      id: 'pin-1',
      slot_key: SLOT,
      mid: 'm-pin-1',
      message_ts: new Date((now - 480) * 1000).toISOString(),
      role: 'assistant',
      preview: 'Here is the helper: export function greet(name: string) …',
      pinned_at: new Date((now - 200) * 1000).toISOString(),
    },
    {
      id: 'pin-2',
      slot_key: SLOT,
      mid: 'm-pin-2',
      message_ts: new Date((now - 300) * 1000).toISOString(),
      role: 'user',
      preview: 'Draw the request flow.',
      pinned_at: new Date((now - 100) * 1000).toISOString(),
    },
  ],
}

const harness = await openTranscriptHarness({
  slot: SLOT,
  project: PROJECT,
  slots,
  detail,
  viewport: { width: 900, height: 1024 },
  deviceScaleFactor: 2,
  // hasTouch flips `(hover: none)` and `(pointer: coarse)` in Chromium —
  // exactly the media the fix branches on. (CDP Emulation.setEmulatedMedia
  // does NOT support the hover/pointer features; verified empirically.)
  hasTouch: true,
})
const { page } = harness

// The pins list is not part of the harness's fixed fixtures; answer it here.
// Registered BEFORE load(), and page.route runs handlers LIFO, so this wins
// over the harness's catch-all for exactly this path.
await page.route('**/api/chat/pins**', route => json(route, pins))

await harness.load('dark', { selector: '.code-block', settle: 1200 })

// Coherence check: if the touch media are not matching, every shot below would
// silently photograph the hover-device rendering instead of the fix.
const media = await page.evaluate(() => ({
  hover: matchMedia('(hover: none)').matches,
  coarse: matchMedia('(pointer: coarse)').matches,
}))
if (!media.hover || !media.coarse) {
  throw new Error(`touch media not emulated: ${JSON.stringify(media)}`)
}

const shot = async (locator, name) => {
  // Center the element so the floating pinned-prompt bubble and the sticky
  // header never overlap the shot.
  await locator.evaluate(el => el.scrollIntoView({ block: 'center' }))
  await page.waitForTimeout(350)
  await locator.screenshot({ path: join(OUT, name) })
  console.log('captured', name)
}

// 1. CodeBlock — copy button visible + enlarged without any hover.
await shot(page.locator('.code-block').first(), 'code-block-touch.png')

// 2. DiffBlock — open/split/copy actions visible + enlarged.
await shot(page.locator('.diff-block').first(), 'diff-block-touch.png')

// 3. MarkdownRenderer mermaid — enlarge button visible + enlarged. The
//    diagram renders asynchronously; wait for the trigger to exist first.
const enlarge = page.getByRole('button', { name: 'Enlarge diagram' })
await enlarge.waitFor({ state: 'attached', timeout: 20000 })
await shot(
  page.locator('figure').filter({ has: page.locator('svg') }).locator('..').first(),
  'mermaid-enlarge-touch.png',
)

// 4. PinnedMessagesPanel — open it, then shoot the panel with its row
//    actions visible.
await page.getByRole('button', { name: 'Open pinned messages' }).click()
const panel = page.getByRole('region', { name: 'Pinned messages' })
await panel.waitFor({ state: 'visible', timeout: 10000 })
await page.waitForTimeout(500)
await shot(panel, 'pinned-panel-touch.png')

await harness.close()
console.log('done ->', OUT)
