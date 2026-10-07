/**
 * Verify `/agent <name>` in the BUILT SPA, gateway-free: typed at the composer
 * it goes to Crew's agent selector (POST /api/chat/slots/{slot}/agent as a
 * template pick) instead of being sent as a turn, the agent chip follows, and a
 * refused name shows on the composer's error notice with the composer text kept.
 * The switch line in the main chat is the row the gateway appends for the
 * request's `announce` flag, delivered here as the `chat_message` frame its
 * `slot.append` broadcasts.
 * The split-pane frames show the transcript lines
 * the backend's `/agent` handler appends (`_handle_agent_command` in
 * chat_runner.py), served as fixtures since this run has no gateway.
 * Every frame is written only after the assertions describing it pass.
 *
 * Usage: npm run build && node scripts/capture-agent-slash-switch.mjs [output-directory]
 */
import { chromium, expect } from '@playwright/test'
import { mkdirSync } from 'node:fs'
import { join } from 'node:path'
import { serveDist } from './lib/serve-dist.mjs'
import { stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'
import { stubSplitPanes } from './lib/split-pane-fixture.mjs'

const out = process.argv[2] || join(process.env.KIROCREW_SCRATCH || 'temp-screenshots', 'agent-slash-switch')
mkdirSync(out, { recursive: true })
const { srv, base } = await serveDist()
const SLOT = 'chat-agent-slash'
const slots = () => [{ key: SLOT, title: '/agent switch', running: false, messages: 2, agent: 'kirocrew', project: '/workspace', last_ts: new Date().toISOString() }]
const row = { kiro_agent: '', workspace: 'default', memory_store: 'default' }
const AGENTS = {
  agents: [{ name: 'kirocrew', source: 'builtin', ...row }, { name: 'fable', source: 'user', ...row }],
  default_agent: 'kirocrew',
}

const SWITCHED = '🔄 Switched to agent: fable'

async function preparePage(page, theme) {
  const errors = []
  const posts = []
  let wsServer = null
  page.on('pageerror', e => errors.push(e.message))
  await stubDashboardApi(page, {
    theme, slots: slots(), folders: [], localStorageEntries: { 'mc-lang': 'en' },
    extra: async (path, route) => {
      const req = route.request()
      if (req.method() === 'POST') posts.push({ path, body: req.postDataJSON?.() ?? null })
      if (path === '/api/agents' || path === '/api/chat/agents') { await json(route, AGENTS); return true }
      if (path === `/api/chat/slots/${SLOT}/agent` && req.method() === 'POST') {
        const body = req.postDataJSON()
        if (body.agent === 'fable') {
          await json(route, { ok: true, agent: 'fable', agent_kind: 'template', workspace: 'default' })
          // What `slot.append` broadcasts for the announced switch.
          if (body.announce === true && wsServer) {
            wsServer.send(JSON.stringify({ type: 'chat_message', data: {
              slot: SLOT, role: 'assistant', content: SWITCHED, cls: 'msg msg-a',
              ts: new Date().toISOString(), meta: { mid: 'switch-1' },
            } }))
          }
        } else {
          // The gateway's real refusal for a stated kind nothing answers.
          await json(route, { error: 'the selected agent choice is not available', code: 'agent_choice_unavailable' }, 409)
        }
        return true
      }
      return false
    },
  })
  // Registered after the stub's swallow-all route, so this one serves the socket.
  await page.routeWebSocket(/\/api\/ws/, ws => { wsServer = ws })
  return { errors, posts }
}

/** Every frame goes through here, after its assertions. */
const shoot = (page, name) => page.screenshot({ path: join(out, name) })

const results = []
const browser = await chromium.launch({ headless: true })
try {
  for (const theme of ['dark', 'light']) {
    const ctx = await browser.newContext({ viewport: { width: 1400, height: 860 }, deviceScaleFactor: 1 })
    const page = await ctx.newPage()
    page.setDefaultTimeout(12000)
    const { errors, posts } = await preparePage(page, theme)
    await page.goto(`${base}/chat`)
    await page.waitForSelector('[data-slot-key]', { timeout: 20000 })
    const composer = page.getByLabel('Message input')
    const turnPosts = () => posts.filter(p => p.path === '/api/chat')
    const agentPosts = () => posts.filter(p => p.path === `/api/chat/slots/${SLOT}/agent`)

    // 1. Switch: the selector endpoint gets a template pick, no turn is sent,
    //    the composer clears and the agent chip names the new agent.
    await composer.click()
    await composer.fill('/agent fable')
    await expect(composer).toHaveValue('/agent fable')
    await expect(page.getByRole('button', { name: /kirocrew/i }).first()).toBeVisible()
    await shoot(page, `${theme}-1-typed.png`)
    await composer.press('Enter')
    await expect.poll(() => agentPosts().length).toBe(1)
    expect(agentPosts()[0].body).toEqual({ agent: 'fable', agent_kind: 'template', announce: true })
    await expect(composer).toHaveValue('')
    await expect(page.getByText('Switched to agent: fable')).toBeVisible()
    await expect(page.getByRole('button', { name: /fable/i }).first()).toBeVisible()
    expect(turnPosts()).toEqual([])
    await page.waitForTimeout(300)
    await shoot(page, `${theme}-2-switched.png`)

    // 2. Refusal: the notice carries the gateway's reason, the composer keeps
    //    the command, and still no turn is sent.
    await composer.fill('/agent fabel')
    await composer.press('Enter')
    await expect.poll(() => agentPosts().length).toBe(2)
    const notice = page.getByTestId('refused-press-error')
    await expect(notice).toContainText("Couldn't switch agent")
    await expect(notice).toContainText('the selected agent choice is not available')
    await expect(composer).toHaveValue('/agent fabel')
    expect(turnPosts()).toEqual([])
    await page.waitForTimeout(300)
    await shoot(page, `${theme}-3-refused.png`)

    expect(errors).toEqual([])
    await ctx.close()

    // 3. Split pane: the two replies the backend's handler appends, one pane
    //    switched and one refused.
    {
      const sctx = await browser.newContext({ viewport: { width: 1400, height: 860 }, deviceScaleFactor: 1 })
      const spage = await sctx.newPage()
      spage.setDefaultTimeout(12000)
      const serrors = []
      spage.on('pageerror', e => serrors.push(e.message))
      const LEFT = 'pane-switched'
      const RIGHT = 'pane-refused'
      const ts = '2026-10-07T03:00:00Z'
      const paneSlot = (key, title) => ({ key, title, running: false, messages: 2, agent: 'kirocrew', created: ts, last_ts: ts, folder_id: '' })
      const REFUSED = '⚠️ Could not switch to agent `fabel`: the selected agent choice is not available'
      await stubSplitPanes(spage, {
        theme,
        slots: [paneSlot(LEFT, 'Switch in a pane'), paneSlot(RIGHT, 'Typo in a pane')],
        transcripts: {
          [LEFT]: [
            { role: 'user', content: '/agent fable', ts, meta: { mid: 'l-1' } },
            { role: 'assistant', content: SWITCHED, ts, meta: { mid: 'l-2' } },
          ],
          [RIGHT]: [
            { role: 'user', content: '/agent fabel', ts, meta: { mid: 'r-1' } },
            { role: 'assistant', content: REFUSED, ts, meta: { mid: 'r-2' } },
          ],
        },
        layout: {
          [LEFT]: {
            type: 'split', id: 'sp-1', dir: 'row', sizes: [0.5, 0.5],
            children: [
              { type: 'leaf', id: 'lf-1', kind: 'session', slot: LEFT },
              { type: 'leaf', id: 'lf-2', kind: 'session', slot: RIGHT },
            ],
          },
        },
      })
      await spage.goto(`${base}/chat/${LEFT}`)
      const panes = spage.locator('[data-chat-pane]')
      await panes.first().waitFor()
      await expect(panes).toHaveCount(2)
      await expect(panes.nth(0).getByText('Switched to agent: fable')).toBeVisible()
      await expect(panes.nth(1).getByText('the selected agent choice is not available')).toBeVisible()
      expect(serrors).toEqual([])
      await spage.waitForTimeout(400)
      await shoot(spage, `${theme}-4-split-pane-replies.png`)
      await sctx.close()
    }
    results.push(`${theme}: ok`)
  }
} finally {
  await browser.close()
  srv.close()
}
console.log(results.join('\n'))
console.log('DONE', out)
