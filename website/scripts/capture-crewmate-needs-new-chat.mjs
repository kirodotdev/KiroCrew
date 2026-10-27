/**
 * Screenshot harness for picking a crewmate with its own memory in a chat that
 * already has messages. The gateway refuses that switch (409
 * `member_memory_requires_new_conversation`); the notice must name the
 * crewmate, say the chat kept its agent, and offer a new chat with it.
 *
 * Serves the built SPA and stubs every API call, so the shot holds dummy data
 * only. Asserts the notice text and the button label, then clicks the button
 * and asserts the create request names the crewmate as a member.
 *
 * Usage: node scripts/capture-crewmate-needs-new-chat.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/crewmate-needs-new-chat'
const ACTIVE = 'chat-active'
const MEMBER = 'release-helper'
mkdirSync(OUT, { recursive: true })

const now = Math.floor(Date.now() / 1000)
const slots = [{
  key: ACTIVE, title: 'Plan the next release', running: false, messages: 2,
  agent: 'kirocrew', agent_kind: 'template', modified: now, last_ts: new Date().toISOString(),
  folder_id: '', last_message: 'Here is the release checklist.', source_links: [], source_links_total: 0,
}]
const catalog = {
  agents: [
    { name: 'kirocrew', selection_kind: 'template', scope: 'global', kiro_agent: 'kirocrew', description: 'Primary Kiro Crew agent.', source: 'builtin', markdown_only: false },
    { name: MEMBER, selection_kind: 'member', display_name: 'Release helper', description: 'Tracks release checklists.', source: 'kirocrew', memory_store: 'member-release-helper', markdown_only: false },
  ],
  default_agent: 'kirocrew',
  session_backend: 'kiro',
  markdown_refused_backends: ['kiro'],
  markdown_spec_backends: ['kas'],
}
const detail = {
  running: false, has_more: false, total: 2, queue: [],
  messages: [
    { role: 'user', content: 'What is left before the release?', ts: new Date().toISOString() },
    { role: 'assistant', content: 'Here is the release checklist.', ts: new Date().toISOString() },
  ],
}
const NOTICE = `${MEMBER} has its own memory, so it can only start a new chat. This chat keeps its current agent.`

function assert(label, ok, extra = '') {
  if (!ok) throw new Error(`${label}${extra ? `: ${extra}` : ''}`)
}

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  const context = await browser.newContext({ viewport: { width: 1300, height: 800 }, deviceScaleFactor: 2 })
  const results = []
  try {
    for (const theme of ['dark', 'light']) {
      const page = await context.newPage()
      logPageProblems(page)
      let created = null
      await stubDashboardApi(page, {
        slots, theme,
        extra: (path, route) => {
          const req = route.request()
          if (path === '/api/agents/catalog') return json(route, catalog), true
          if (path === `/api/chat/slots/${ACTIVE}/agent` && req.method() === 'POST') {
            return route.fulfill({
              status: 409, contentType: 'application/json',
              body: JSON.stringify({ error: 'Open a new conversation to choose member memory.', code: 'member_memory_requires_new_conversation' }),
            }), true
          }
          if (path === '/api/chat/slots' && req.method() === 'POST') {
            created = req.postDataJSON()
            return json(route, { key: 'chat-new', title: '', agent: MEMBER, agent_kind: 'member', messages: [] }), true
          }
          if (path.startsWith('/api/chat/slots/')) return json(route, detail), true
          return false
        },
      })
      await page.addInitScript(slot => {
        localStorage.setItem('mc-active-slot', slot)
        localStorage.setItem('mc-privacy-notice-v1', '1')
      }, ACTIVE)
      await page.goto(base + '/chat', { waitUntil: 'domcontentloaded' })
      const chip = page.locator('button', { hasText: 'kirocrew' }).last()
      await chip.waitFor({ state: 'visible', timeout: 20_000 })
      await chip.click()
      const pop = page.locator('[role="dialog"]').filter({ has: page.locator('[role="listbox"]') }).last()
      await pop.waitFor({ state: 'visible', timeout: 10_000 })
      await pop.getByRole('option', { name: /Release helper/ }).click()
      const notice = page.getByTestId('agent-switch-notice')
      await notice.waitFor({ state: 'visible', timeout: 10_000 })
      assert('notice text', ((await notice.textContent()) ?? '').includes(NOTICE), await notice.textContent())
      const button = page.getByTestId('agent-switch-notice-new-chat')
      assert('button label', ((await button.textContent()) ?? '').trim() === `New chat with ${MEMBER}`)
      await page.waitForTimeout(400)
      const file = `${OUT}/after-notice-${theme}.png`
      await page.screenshot({ path: file })
      results.push(file)
      await button.click()
      await notice.waitFor({ state: 'hidden', timeout: 10_000 })
      assert('create names the crewmate', created?.agent === MEMBER && created?.agent_kind === 'member', JSON.stringify(created))
      await page.close()
    }
  } finally {
    await browser.close()
    srv.close()
  }
  for (const f of results) console.log('wrote', f)
}

main().catch(err => { console.error(err); process.exit(1) })
