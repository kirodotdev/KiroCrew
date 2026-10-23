/**
 * Screenshot harness for "a comment on text in a fenced code block is marked in
 * the markdown preview" (issue #21837).
 *
 * Runs the REAL built SPA (website/dist) behind the shared loopback static
 * server with every /api/** call answered from fixtures: no gateway, no token.
 * A markdown file tab is seeded through the tab store's localStorage bucket,
 * with two saved comments in `mc-comment-drafts`: one on prose, one on
 * `mycli rm NAME` inside the second code block (the same words also sit in the
 * prose and in the first block, so a quote-only lookup would mark the wrong one).
 *
 * Frames:
 *   01-code-comment-marked   both comments underlined, each in its own place
 *   02-code-comment-hover    the pointer on the code mark shows the comment text
 *
 * Usage: node scripts/capture-21837-code-block-comment-mark.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { join } from 'node:path'

import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/21837-code-block-comment-mark'
const ACTIVE = 'code-comment-mark'
const FILE = '/home/dev/notes/cleanup.md'
const BODY = [
  '# Clean up a stale install',
  '',
  'Run mycli rm NAME by hand only when the steps below fail. Check the list first, with care.',
  '',
  '```bash',
  'mycli ls',
  'mycli rm NAME --dry-run',
  '```',
  '',
  'Then remove it for real:',
  '',
  '```bash',
  'mycli ls --all',
  'mycli rm NAME --force',
  '```',
  '',
].join('\n')
// Block 1 (the second unit), source line 2, column 0, packed as `codePassagePosition` packs it.
const CODE_POSITION = 1 * 1e12 + 2 * 1e5 + 0
const COMMENTS = [
  { id: 'prose1', anchor: 'with care', text: 'Which list: ls or ls --all?' },
  { id: 'code1', anchor: 'mycli rm NAME', text: 'Is --force needed here?', inCode: true, codePosition: CODE_POSITION },
]

mkdirSync(OUT, { recursive: true })
const now = Math.floor(Date.now() / 1000)
const SLOTS = [{
  key: ACTIVE, title: 'Review the cleanup steps', running: false, messages: 2, agent: 'kirocrew',
  memory_mode: 'persistent', modified: now, last_ts: '2026-10-22T17:00:00Z', folder_id: '',
  last_message: 'Opened it in the side panel.', source_links: [], source_links_total: 0,
}]
const detail = {
  running: false, has_more: false, total: 2, queue: [],
  messages: [
    { role: 'user', ts: now - 40, content: `Open \`${FILE}\` so I can review it.` },
    { role: 'assistant', ts: now - 30, content: 'Opened it in the side panel.' },
  ],
}

async function fileRoutes(path, route) {
  if (path === '/api/chat/slots') { await json(route, SLOTS); return true }
  if (path.startsWith('/api/chat/slots/')) { await json(route, detail); return true }
  if (path === '/api/file-read') {
    await route.fulfill({ status: 200, headers: { 'content-type': 'text/markdown; charset=utf-8', 'x-file-binary': 'false' }, body: BODY })
    return true
  }
  if (path === '/api/file-watch') { await route.abort(); return true }
  if (path === '/api/file-diff') { await json(route, { diff: '', original: BODY, status: 'clean' }); return true }
  return false
}

/** The painted comment ranges: text, whether inside a shadow root, and the centre of the first box. */
function paintedComments() {
  const hl = CSS.highlights.get('mc-comment')
  return hl ? [...hl].map(r => {
    const box = r.getClientRects()[0]
    return { text: r.toString(), inCode: r.startContainer.getRootNode() instanceof ShadowRoot, x: box ? box.left + box.width / 2 : -1, y: box ? box.top + box.height / 2 : -1 }
  }) : []
}

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  try {
    const context = await browser.newContext({ viewport: { width: 1500, height: 950 }, deviceScaleFactor: 2 })
    const page = await context.newPage()
    logPageProblems(page)
    await stubDashboardApi(page, {
      theme: 'dark',
      slots: SLOTS,
      extra: fileRoutes,
      localStorageEntries: {
        'mc-lang': 'en',
        'mc-active-slot': ACTIVE,
        'mc-privacy-notice-v1': '1',
        'mc-sidebar-pinned': 'true',
        'kirocrew:comment-hint-dismissed': '1',
        'mc-comment-drafts': JSON.stringify({ [FILE]: COMMENTS }),
        [`mc-panel-tabs:${ACTIVE}`]: JSON.stringify({
          activeId: `file:${FILE}`,
          tabs: [{ id: `file:${FILE}`, kind: 'file', title: 'cleanup.md', path: FILE }],
        }),
        [`mc-activity-open:${ACTIVE}`]: 'true',
      },
    })
    await page.goto(base + '/chat', { waitUntil: 'domcontentloaded' })
    await page.getByRole('heading', { name: 'Clean up a stale install' }).waitFor({ state: 'visible', timeout: 30000 })
    // Wait for the highlighted code and both marks: the swap from the plain copy repaints the code mark.
    let painted = []
    for (let i = 0; i < 60; i++) {
      painted = await page.evaluate(paintedComments)
      if (painted.length === 2 && painted.some(p => p.inCode)) break
      await page.waitForTimeout(250)
    }
    console.log('painted:', JSON.stringify(painted))
    const code = painted.find(p => p.inCode)
    const prose = painted.find(p => !p.inCode)
    if (!code || code.text !== 'mycli rm NAME' || !prose || prose.text !== 'with care') throw new Error('the two comments are not painted in their places')
    await page.waitForTimeout(400)
    const strip = await page.locator('.side-panel-strip').first().boundingBox()
    if (!strip) throw new Error('side panel strip not found')
    const clip = { x: Math.max(0, strip.x - 4), y: 0, width: Math.min(1500 - Math.max(0, strip.x - 4), strip.width + 8), height: 620 }
    await page.screenshot({ path: join(OUT, '01-code-comment-marked.png'), clip })
    console.log('wrote 01-code-comment-marked.png')

    await page.mouse.move(code.x, code.y)
    const tip = page.locator('.mc-comment-tooltip')
    await tip.waitFor({ state: 'visible', timeout: 5000 })
    const tipText = await tip.textContent()
    if (tipText !== 'Is --force needed here?') throw new Error(`tooltip says ${JSON.stringify(tipText)}`)
    await page.screenshot({ path: join(OUT, '02-code-comment-hover.png'), clip })
    console.log('wrote 02-code-comment-hover.png')
    await context.close()
  } finally {
    await browser.close()
    srv.close()
  }
}

main().catch(err => { console.error(err); process.exit(1) })
