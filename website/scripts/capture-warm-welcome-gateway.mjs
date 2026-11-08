/**
 * Recording + screenshots: the gateway-rendered warm welcome (RFC
 * rfc-delayed-task-brief.md 3.11). While the gateway prepares it, its caption sits
 * inside a reply bubble; the frame that clears `preparing` lands the welcome in
 * the same place (website UX lens 13).
 *
 *   welcome-<width>.webm     the preparing bubble folding into the welcome
 *   01-preparing-<width>.png while preparing
 *   02-landed-<width>.png    the welcome: count line, must-do card with the
 *                            conductor's words and time, changes, Board link
 *
 * Usage (two shells, from website/):
 *   npx vite --host 127.0.0.1 --port 6834 --strictPort
 *   node scripts/capture-warm-welcome-gateway.mjs http://127.0.0.1:6834 ../temp-screenshots/warm-welcome-v4
 */
import { chromium } from 'playwright'
import { mkdirSync, renameSync } from 'node:fs'
import { routeMembersApi } from './lib/members-fixtures.mjs'

const positional = process.argv.slice(2).filter(a => !a.startsWith('--'))
const BASE = positional[0] || 'http://127.0.0.1:6834'
const OUT = positional[1] || '../temp-screenshots/warm-welcome-v4'
mkdirSync(OUT, { recursive: true })

const SLOT = 'member-radar'
const THREAD = [
  { role: 'user', content: '帮我把三个安全修复的 PR 推到全绿。', ts: '2026-11-08T03:00:00Z' },
  { role: 'assistant', content: '好，我开始盯这三个 PR。', ts: '2026-11-08T03:01:00Z' },
]
const BRIEF = {
  version: 4,
  language: 'zh',
  count: 1,
  count_line: '1 件事等你',
  framing: '',
  cards: [{
    kind: 'item',
    title: '选一个合并顺序',
    tags: [{ text: '有冲突', bad: true }, { text: '开着', bad: false }, { text: '有问题', bad: false }],
    pr: { label: 'acme/widgets#7', url: 'https://github.com/acme/widgets/pull/7' },
    read: 'GitHub 05:58',
    checked: '05:58 核实',
    said: {
      by: '建议（05:40）',
      why: '两个修复改了同一个文件，只有你能定谁先合。',
      options: [
        { label: '先合 #7', outcome: '#8 要重新 rebase 一次' },
        { label: '先合 #8', outcome: '#7 的冲突要手动解' },
      ],
      pick: '先合 #7',
      pick_why: '它的检查已经全过',
      pick_label: '推荐',
    },
  }],
  changes_title: '你走后',
  changes: [
    { text: '修复登录超时', tags: [{ text: '2 项失败', bad: true }], source: 'GitHub 05:58' },
    { text: '更新依赖', tags: [{ text: '报完成', bad: false }], source: '任务汇报 05:21' },
  ],
  more: '',
  board: { text: '看板：1 项等你', link: '打开看板 →', conductor: SLOT },
  body: '',
  checks: [],
}
const CONTENT = '1 件事等你 · 选一个合并顺序'

let failed = false
function check(name, ok, detail) {
  console.log(`${name}: ${ok ? 'OK' : 'MISMATCH'} ${detail}`)
  if (!ok) failed = true
}
const fire = (page, detail) => page.evaluate((d) => window.dispatchEvent(new CustomEvent('capture:frame', { detail: d })), detail)

const browser = await chromium.launch()
for (const width of [1280, 320]) {
  const height = width === 320 ? 720 : 820
  const context = await browser.newContext({
    viewport: { width, height },
    deviceScaleFactor: 1,
    recordVideo: { dir: OUT, size: { width, height } },
  })
  const page = await context.newPage()
  await routeMembersApi(page, { key: SLOT, title: 'radar', running: false, messages: THREAD })
  await page.goto(`${BASE}/capture/members-page.html?theme=dark&lng=zh-CN`)
  await page.waitForSelector('[data-capture-root]')
  await page.getByText('好，我开始盯这三个 PR。').first().waitFor()

  await fire(page, { kind: 'preparing', slot: SLOT })
  const bubble = page.getByTestId('chat-pane-preparing-catch-up')
  await bubble.waitFor()
  check(`01-preparing-${width}`, (await bubble.locator('.message-bubble').count()) === 1, 'caption inside a reply bubble')
  await page.waitForTimeout(1200)
  await page.screenshot({ path: `${OUT}/01-preparing-${width}.png` })

  await fire(page, { kind: 'landed', slot: SLOT, text: CONTENT, meta: { kind: 'warm_task_brief_result', fingerprint: 'fp', brief: BRIEF } })
  await page.getByTestId('warm-welcome').first().waitFor()
  await bubble.waitFor({ state: 'detached' })
  check(`02-landed-${width}`, (await page.getByTestId('chat-pane-preparing-catch-up').count()) === 0, 'bubble gone, welcome in its place')
  await page.waitForTimeout(1200)
  await page.screenshot({ path: `${OUT}/02-landed-${width}.png` })

  const video = page.video()
  await context.close()
  if (video) renameSync(await video.path(), `${OUT}/welcome-${width}.webm`)
}
await browser.close()
if (failed) {
  console.error('CAPTURE FAILED: at least one frame did not match its asserted state')
  process.exit(1)
}
console.log('all frames verified')
