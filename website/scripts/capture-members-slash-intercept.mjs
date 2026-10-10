/**
 * Screenshots for client-only slash commands typed in a Crew member's DM.
 *
 * The DM composer offers `/side`, `/btw`, `/kb` and `/onboarding` in its slash
 * menu, like the main chat. Sending one must run it in the browser, never post
 * it to the member as a message. Each frame asserts the wire traffic it saw
 * before it is written, so a frame cannot document the wrong state.
 *
 *   01-kb-<expect>    `/kb deploy runbook` + Enter. after: an inline notice says
 *                     /kb is not available here, the text stays in the
 *                     composer, nothing is posted. before: posted as a message.
 *   02-side-<expect>  `/side what changed?` + Enter. after: the Side tab opens
 *                     and the question goes to the side chat, nothing is posted
 *                     to the member; the side chat's result frames are then
 *                     delivered so the question and answer show there.
 *                     before: posted as a message.
 *   03-menu-after     `/` typed: the menu leaves out `/kb` (the stub serves only the frontend rows), which the DM
 *                     refuses (after only).
 *
 * `--expect=before` flips the assertions so the same script photographs the
 * baseline off the base commit.
 *
 * Usage (two shells, from website/):
 *   npx vite --host 127.0.0.1 --port 6833 --strictPort
 *   node scripts/capture-members-slash-intercept.mjs http://127.0.0.1:6833 ../temp-screenshots/members-slash [--expect=after|before]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { routeMembersApi } from './lib/members-fixtures.mjs'

const positional = process.argv.slice(2).filter(a => !a.startsWith('--'))
const BASE = positional[0] || 'http://127.0.0.1:6833'
const OUT = positional[1] || '../temp-screenshots/members-slash'
const EXPECT = (process.argv.find(a => a.startsWith('--expect=')) || '--expect=after').slice('--expect='.length)
mkdirSync(OUT, { recursive: true })

const THREAD = [
  { role: 'user', content: 'What did you triage tonight?', ts: '2026-08-27T01:00:00Z' },
  { role: 'assistant', content: 'Six new issues so far. Four are covered by open PRs.', ts: '2026-08-27T01:00:05Z' },
]

const browser = await chromium.launch()
let failed = false
function check(name, ok, detail) {
  console.log(`${name}: ${ok ? 'OK' : 'MISMATCH'} ${detail}`)
  if (!ok) failed = true
}

/** Members page on radar's DM, idle. Records every POST the page makes. */
async function openDm() {
  const page = await browser.newPage({ viewport: { width: 1280, height: 820 }, deviceScaleFactor: 1 })
  const posts = []
  page.on('request', r => { if (r.method() === 'POST') posts.push({ path: new URL(r.url()).pathname, body: r.postData() || '' }) })
  await routeMembersApi(page, { key: 'member-radar', title: 'radar', running: false, messages: THREAD })
  await page.goto(`${BASE}/capture/members-page.html?theme=dark`)
  await page.waitForSelector('[data-capture-root]')
  // radar is the member the page opens on, so its DM is already showing.
  await page.getByText('What did you triage tonight?').waitFor()
  return { page, posts, box: page.locator('[data-chat-pane] textarea').first() }
}

const chatPosts = (posts, text) => posts.filter(p => p.path === '/api/chat' && p.body.includes(text)).length

// 01 — /kb in the DM.
{
  const { page, posts, box } = await openDm()
  await box.fill('/kb deploy runbook')
  await box.press('Enter')
  await page.waitForTimeout(600)
  const sent = chatPosts(posts, '/kb deploy runbook')
  if (EXPECT === 'before') {
    check('01-kb BEFORE: posted to the member as text', sent === 1, `chatPosts=${sent}`)
  } else {
    const notice = await page.getByTestId('chat-pane-slash-error').textContent().catch(() => '')
    const kept = await box.inputValue()
    check('01-kb AFTER: notice, text kept, nothing posted', sent === 0 && /\/kb/.test(notice || '') && kept === '/kb deploy runbook', `chatPosts=${sent} notice="${(notice || '').trim()}" composer="${kept}"`)
  }
  await page.screenshot({ path: `${OUT}/01-kb-${EXPECT}.png` })
  await page.close()
}

// 02 — /side in the DM.
{
  const { page, posts, box } = await openDm()
  await box.fill('/side what changed?')
  await box.press('Enter')
  await page.waitForTimeout(900)
  const sent = chatPosts(posts, '/side what changed?')
  if (EXPECT === 'before') {
    check('02-side BEFORE: posted to the member as text', sent === 1, `chatPosts=${sent}`)
  } else {
    const opened = posts.filter(p => p.path === '/api/chat/slots/member-radar/side/open').length
    const asked = posts.filter(p => p.path === '/api/chat/slots/member-radar/side/turn' && p.body.includes('what changed?')).length
    const kept = await box.inputValue()
    check('02-side AFTER: side chat opened and asked, nothing posted, composer cleared', sent === 0 && opened === 1 && asked === 1 && kept === '', `chatPosts=${sent} open=${opened} turn=${asked} composer="${kept}"`)
    // No WebSocket in the harness: deliver the side chat's result frames.
    await page.evaluate(() => window.dispatchEvent(new CustomEvent('capture:frame', { detail: { kind: 'side-echo', slot: 'member-radar', text: 'what changed?' } })))
    await page.getByText('Two PRs merged since your last look').first().waitFor()
    await page.waitForTimeout(300)
  }
  await page.screenshot({ path: `${OUT}/02-side-${EXPECT}.png` })
  await page.close()
}

// 03 — the slash menu in the DM leaves out what the DM refuses.
if (EXPECT === 'after') {
  const { page, box } = await openDm()
  await box.fill('/')
  const list = page.getByRole('listbox').first()
  await list.waitFor()
  const names = await list.locator('[role="option"] .font-mono').allTextContents()
  check('03-menu AFTER: /kb left out, /btw offered', !names.includes('/kb') && names.includes('/btw'), `names=${names.join(',')}`)
  await page.screenshot({ path: `${OUT}/03-menu-after.png` })
  await page.close()
}

await browser.close()
if (failed) {
  console.error('CAPTURE FAILED: at least one frame did not match its asserted state')
  process.exit(1)
}
console.log('all frames verified')
