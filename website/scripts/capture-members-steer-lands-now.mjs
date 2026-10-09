/**
 * Screenshots + recording: a message sent while a crewmate works lands in its
 * chat AT ONCE.
 *
 * Scene: radar's turn runs on the server and its sub-agents are out, but no
 * live chunk or tool frame has reached this tab (the turn is thinking, or was
 * already running when the DM opened). `/api/chat` is held unanswered, so the
 * frame shows only what the client drew on its own: no server echo.
 *
 *   01-sent-<expect>.png   right after Enter
 *   02-sent-<expect>.webm  type, Enter, the second after
 *
 * `--expect=before` asserts the old behaviour (no bubble), `after` the fix.
 *
 * Usage (two shells, from website/):
 *   npx vite --host 127.0.0.1 --port 6833 --strictPort
 *   node scripts/capture-members-steer-lands-now.mjs http://127.0.0.1:6833 ../temp-screenshots/steer-lands-now --expect=after
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { routeMembersApi } from './lib/members-fixtures.mjs'

const positional = process.argv.slice(2).filter(a => !a.startsWith('--'))
const BASE = positional[0] || 'http://127.0.0.1:6833'
const OUT = positional[1] || '../temp-screenshots/steer-lands-now'
const EXPECT = (process.argv.find(a => a.startsWith('--expect=')) || '--expect=after').slice('--expect='.length)
mkdirSync(OUT, { recursive: true })

const THREAD = [
  { role: 'user', content: 'What did you triage tonight?', ts: '2026-08-27T01:00:00Z' },
  { role: 'assistant', content: 'Six new issues so far — walking the open PRs now to see which are already covered.', ts: '2026-08-27T01:00:05Z' },
]
const TEXT = 'Also check whether #4213 is a duplicate of last week\'s report.'

const browser = await chromium.launch()
const ctx = await browser.newContext({ viewport: { width: 1280, height: 820 }, deviceScaleFactor: 1, recordVideo: { dir: `${OUT}/.video`, size: { width: 1280, height: 820 } } })
const page = await ctx.newPage()
await routeMembersApi(page, { key: 'member-radar', title: 'radar', running: true, messages: THREAD })
// Never answered: the frame shows the client's own drawing, no echo.
await page.route(u => new URL(u).pathname === '/api/chat', () => {})
await page.goto(`${BASE}/capture/members-page.html?theme=dark&subagents=1`)
await page.waitForSelector('[data-capture-root]')
await page.getByText('What did you triage tonight?').waitFor()
await page.waitForTimeout(800)
const box = page.locator('[data-chat-pane] textarea').first()
await box.pressSequentially(TEXT, { delay: 25 })
await page.waitForTimeout(500)
await box.press('Enter')
await page.waitForTimeout(400)
const drawn = await page.locator('[data-chat-pane] .chat-container').getByText(TEXT, { exact: true }).count()
const ok = EXPECT === 'before' ? drawn === 0 : drawn === 1
console.log(`${EXPECT}: bubble drawn=${drawn} ${ok ? 'OK' : 'MISMATCH'}`)
await page.screenshot({ path: `${OUT}/01-sent-${EXPECT}.png` })
await page.waitForTimeout(1200)
const video = page.video()
await page.close()
await ctx.close()
await video.saveAs(`${OUT}/02-sent-${EXPECT}.webm`)
await video.delete()
await browser.close()
if (!ok) process.exit(1)
