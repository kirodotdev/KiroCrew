/** Browser evidence for the registered-action guide. Real SPA, synthetic API;
 * no real agent, MCP server, credentials or configuration writes are involved.
 * The MCP action only hands off to the product's native add form: the run
 * fails on any MCP write, and on a pre-filled form. */
import assert from 'node:assert/strict'
import { mkdirSync } from 'node:fs'
import { resolve } from 'node:path'
import { chromium } from 'playwright'
import { serveDist } from './lib/serve-dist.mjs'
import { stubDashboardApi, json, KIROCREW_CONFIG_FIXTURE } from './lib/stub-dashboard-api.mjs'

const out = process.argv[2] || process.env.KIROCREW_SCRATCH
if (!out) throw new Error('Provide an output directory')
mkdirSync(out, { recursive: true })
const { srv, base } = await serveDist()
let browser
try {
  browser = await chromium.launch()
  for (const width of [1440, 390, 320]) {
    const ctx = await browser.newContext({ viewport: { width, height: 1000 }, locale: 'en-US', recordVideo: { dir: out } })
    await ctx.route('**/*', route => new URL(route.request().url()).origin === base ? route.continue() : route.abort())
    let guide = {
      guide_id: `fixture-${width}`, slot_key: 'chat-assistant', status: 'offered', revision: 1,
      owner_tab: null, action_index: 0, step_index: 0, reason: '',
      expires_at: Date.now() / 1000 + 1800, lease_expires_at: null,
      actions: [
        { id: 'crewmate.create', params: { name: 'Scout', goal: 'Prepare a weekly project update for my review.' } },
        { id: 'mcp.open_add', params: {} },
      ],
    }
    // Both built-ins: the reserved `default` member (ordinary, untouched) and
    // the separate Assistant member the guide was offered in.
    const members = [
      { name: 'default', slug: 'default', bound: true, slot_key: 'chat-default', kiro_agent: 'kirocrew', source: 'builtin', workspace: 'default', memory_store: 'default' },
      { name: 'assistant', slug: 'assistant', bound: true, slot_key: 'chat-assistant', kiro_agent: 'kirocrew-assistant', source: 'builtin', workspace: 'default', memory_store: 'default' },
    ]
    const threadReads = []
    const sockets = []
    const pages = []
    let saves = 0
    const mcpWrites = []
    const broadcast = () => sockets.forEach(s => s.send(JSON.stringify({ type: 'guide_update', data: { guide } })))
    const setup = async page => {
      pages.push(page)
      page.on('pageerror', e => { throw e })
      await stubDashboardApi(page, {
        slots: [
          { key: 'chat-assistant', title: 'Assistant', agent: 'assistant', messages: 0, running: false },
          { key: 'chat-default', title: 'default', agent: 'default', messages: 0, running: false },
        ],
        theme: 'light', preserveStorage: true,
        localStorageEntries: { 'mc-preview-crew': '1', 'mc-members-panel-open': '0', 'mc-lang': 'en' },
        extra: async (path, route) => {
          const reply = async (v, status = 200) => { await json(route, v, status); return true }
          if (path === '/api/theme/boot') return reply({ mode: 'light', onboarded: true, import_onboarded: true, privacy_acked: true })
          if (path === '/api/config/kirocrew') return reply({ ...KIROCREW_CONFIG_FIXTURE, dashboard: { crewmate_threads: false } })
          if (path === '/api/members') return reply({ members })
          if (path === '/api/teams') return reply({ teams: [] })
          if (path === '/api/members/assistant/thread') { threadReads.push('assistant'); return reply({ member: 'assistant', slot_key: 'chat-assistant' }) }
          if (path === '/api/members/default/thread') { threadReads.push('default'); return reply({ member: 'default', slot_key: 'chat-default' }) }
          if (path.endsWith('/projections')) return reply({ asOfSeq: 0, values: {} })
          if (path === '/api/chat/slots/chat-assistant') return reply({ key: 'chat-assistant', messages: [], running: false, has_more: false, total: 0 })
          if (path === '/api/agents/installed') return reply([{ name: 'kirocrew', source: 'kirocrew' }])
          if (path.startsWith('/api/mcp') && route.request().method() !== 'GET') {
            mcpWrites.push(`${route.request().method()} ${path}`)
            return reply({ error: 'the guide must never write MCP configuration' }, 500)
          }
          if (path === '/api/mcp') return reply([])
          if (path === '/api/crons') return reply({ jobs: [] })
          if (path === '/api/guide/pending') return reply({ guides: guide.status === 'completed' ? [] : [guide] })
          if (path.startsWith('/api/guide/')) {
            const data = route.request().postDataJSON()
            if (data.revision !== guide.revision) return reply({ error: 'stale revision' }, 409)
            if (path.endsWith('/claim')) {
              if (guide.owner_tab && guide.owner_tab !== data.tab_id && !data.take_over) return reply({ error: 'other tab' }, 409)
              guide = { ...guide, owner_tab: data.tab_id, status: 'active', revision: guide.revision + 1 }
            } else {
              assert.equal(data.tab_id, guide.owner_tab)
              if (path.endsWith('/progress')) {
                assert.equal(data.action_index, guide.action_index)
                assert.equal(data.step_index, guide.step_index)
                assert.equal(data.outcome, 'observed')
                guide = { ...guide, step_index: guide.step_index + 1, revision: guide.revision + 1 }
                // The MCP action's last step is the native form being open.
                if (guide.action_index === 1 && guide.step_index === 2) guide = { ...guide, status: 'completed', owner_tab: null }
              }
            }
            await reply(guide)
            broadcast()
            return true
          }
          if (path === '/api/agents' && route.request().method() === 'POST') {
            const headers = route.request().headers()
            assert.equal(headers['x-guide-id'], guide.guide_id)
            assert.equal(headers['x-guide-tab'], guide.owner_tab)
            assert.equal(headers['x-guide-revision'], String(guide.revision))
            const data = route.request().postDataJSON()
            saves++
            assert.equal(guide.step_index, 2)
            members.push({ ...members[1], kiro_agent: 'kirocrew', source: 'kirocrew', name: data.name, slug: data.name.toLowerCase(), slot_key: '', bound: false })
            await reply({ ok: true, name: data.name, member_id: 'fixture-scout' })
            guide.actions[0].result = { name: data.name, member_id: 'fixture-scout' }
            guide = { ...guide, action_index: 1, step_index: 0, revision: guide.revision + 1 }
            broadcast()
            return true
          }
          return false
        },
      })
      await page.routeWebSocket(/\/api\/ws/, ws => { sockets.push(ws) })
    }
    const page = await ctx.newPage()
    await setup(page)
    await page.goto(`${base}/members`)
    await page.getByTestId('guide-start').waitFor()
    assert.equal(await page.getByTestId('onboarding-chapter-embedded').count(), 0)
    // The guide is offered in the Assistant member's chat; `default` is never opened.
    assert.equal(new URL(page.url()).searchParams.get('member'), 'assistant', 'lands on the Assistant member')
    assert.equal(threadReads.includes('default'), false, 'the default member is not opened')
    const shot = async name => {
      await page.waitForTimeout(500)
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false)
      await page.screenshot({ path: resolve(out, `${name}-${width}.png`) })
    }
    await shot('guide-offer')
    await page.getByTestId('guide-start').click()
    await page.getByTestId('guide-arrow').waitFor()
    if (width === 1440) {
      const other = await ctx.newPage()
      await setup(other)
      await other.goto(`${base}/members`)
      await other.getByTestId('guide-take-over').waitFor()
      assert.equal(await other.getByTestId('onboarding-chapter-embedded').count(), 0)
      await page.bringToFront()
    }
    await shot('guide-goal')
    await page.getByTestId('meet-crewmates-next').click()
    await page.getByTestId('meet-crewmates-name').waitFor()
    await page.waitForFunction(() => document.querySelector('[data-testid="guide-step-text"]')?.textContent?.includes('name'))
    await page.getByTestId('meet-crewmates-next').click()
    await page.getByTestId('meet-crewmates-create').waitFor()
    await page.waitForTimeout(600)
    await shot('guide-create')
    await page.getByTestId('meet-crewmates-create').click()
    await page.getByTestId('meet-crewmates-ready').waitFor()
    assert.equal(saves, 1)
    await page.getByTestId('guide-continue').click()
    // Step 1: the existing Connections page opens on Services; the arrow is on
    // the existing MCP Servers tab and nothing opens by itself.
    await page.waitForURL(u => u.pathname === '/capabilities' && u.searchParams.get('tab') === 'mcp')
    await page.locator('[data-guide-anchor="mcp.servers-tab"]').waitFor()
    await page.getByTestId('guide-arrow').waitFor()
    assert.equal(await page.locator('[data-guide-anchor="mcp.custom-form"]').count(), 0)
    await shot('guide-mcp-tab')
    await page.locator('[data-guide-anchor="mcp.servers-tab"]').click()
    // Step 2: the arrow moves to the existing Add Custom button.
    await page.locator('[data-guide-anchor="mcp.add-custom"]').waitFor()
    await page.waitForFunction(() => document.querySelector('[data-testid="guide-step-text"]')?.textContent?.includes('Add Custom'))
    assert.equal(await page.locator('[data-guide-anchor="mcp.custom-form"]').count(), 0)
    await shot('guide-mcp-add-custom')
    await page.locator('[data-guide-anchor="mcp.add-custom"]').click()
    // The human opened the native form; the guide ends there, with nothing filled in.
    const form = page.locator('[data-guide-anchor="mcp.custom-form"]')
    await form.waitFor()
    assert.equal(await form.locator('textarea').inputValue(), '')
    // Only the form's own Enable checkbox, in its native default (off); no
    // server-name field, because nothing was pasted in.
    assert.equal(await form.locator('input:not([type="checkbox"])').count(), 0)
    assert.equal(await form.locator('input[type="checkbox"]').isChecked(), false)
    await page.getByText('Guide complete', { exact: true }).waitFor()
    assert.equal(await page.getByTestId('guide-saved-server').count(), 0)
    await shot('guide-mcp-native-form')
    assert.deepEqual(mcpWrites, [])
    console.log(`PASS ${width}px: explicit start, sequential actions, attributed crewmate save, MCP hand-off to the native form with zero MCP writes`)
    await ctx.close()
  }
} finally {
  await browser?.close()
  await new Promise(done => srv.close(done))
}
