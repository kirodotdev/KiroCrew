/** Real built SPA, synthetic API only. Proves layout and creation/chat continuity
 * without sending a prompt or changing a running gateway. Pass an output dir. */
import assert from 'node:assert/strict'
import { mkdirSync } from 'node:fs'
import { resolve } from 'node:path'
import { chromium } from 'playwright'
import { serveDist } from './lib/serve-dist.mjs'
import { stubDashboardApi, json, KIROCREW_CONFIG_FIXTURE } from './lib/stub-dashboard-api.mjs'

const outputDir = process.argv[2] || process.env.KIROCREW_SCRATCH
if (!outputDir) throw new Error('Provide an output directory or KIROCREW_SCRATCH')
const output = resolve(outputDir)
mkdirSync(output, { recursive: true })
const { srv, base } = await serveDist()
let browser
try {
  browser = await chromium.launch()
  for (const { width, lang, theme } of [
    { width: 1440, lang: 'en', theme: 'light' },
    { width: 1280, lang: 'en', theme: 'light' },
    { width: 1024, lang: 'en', theme: 'light' },
    { width: 1800, lang: 'en', theme: 'light' },
    { width: 390, lang: 'en', theme: 'light' },
    { width: 320, lang: 'en', theme: 'light' },
    { width: 390, lang: 'de', theme: 'dark' },
    { width: 390, lang: 'zh-CN', theme: 'light' },
  ]) {
    const suffix = lang === 'en' ? String(width) : `${width}-${lang}-${theme}`
    const ctx = await browser.newContext({ viewport: { width, height: 1000 }, locale: 'en-US', recordVideo: width === 1440 ? { dir: output } : undefined })
    const page = await ctx.newPage()
    await ctx.route('**/*', route => new URL(route.request().url()).origin === base ? route.continue() : route.abort())
    const shot = async (name) => {
      // Capture after both the layout transition and the final mascot entrance.
      await page.waitForTimeout(1600)
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth), false, `${name}: horizontal overflow`)
      await page.screenshot({ path: resolve(output, `${name}-${suffix}.png`) })
    }
    const failures = []
    page.on('pageerror', e => failures.push(String(e)))
    // Both built-ins, as the backend lists them: the reserved `default`
    // member (ordinary, untouched) and the separate Assistant member.
    const members = [
      { name: 'default', slug: 'default', kiro_agent: 'kirocrew', source: 'builtin', workspace: 'default', memory_store: 'default', slot_key: 'chat-default', bound: true, last_active_ts: 0 },
      { name: 'assistant', slug: 'assistant', kiro_agent: 'kirocrew-assistant', source: 'builtin', workspace: 'default', memory_store: 'default', slot_key: 'chat-assistant', bound: true, last_active_ts: 0 },
    ]
    const slots = [
      { key: 'chat-assistant', title: 'Assistant', agent: 'assistant', messages: 0, running: false, mode: '' },
      { key: 'chat-default', title: 'default', agent: 'default', messages: 0, running: false, mode: '' },
    ]
    const threadReads = []
    const messages = []
    let creates = 0
    let schedules = 0
    let sends = 0
    await stubDashboardApi(page, {
      slots, theme, preserveStorage: true,
      localStorageEntries: { 'mc-preview-crew': '1', 'mc-members-panel-open': '0', 'mc-lang': lang },
      extra: async (path, route) => {
        const respond = async (body) => { await json(route, body); return true }
        const method = route.request().method()
        if (path === '/api/theme/boot') return respond({ mode: theme, theme: '', onboarded: true, import_onboarded: true, privacy_acked: true, crewmates_onboarded: false })
        if (path === '/api/config/kirocrew') return respond({ ...KIROCREW_CONFIG_FIXTURE, dashboard: { crewmate_threads: false } })
        if (path === '/api/members') return respond({ members })
        if (path === '/api/teams') return respond({ teams: [] })
        if (path === '/api/members/assistant/thread') { threadReads.push('assistant'); return respond({ member: 'assistant', slot_key: 'chat-assistant' }) }
        if (path === '/api/members/default/thread') { threadReads.push('default'); return respond({ member: 'default', slot_key: 'chat-default' }) }
        if (path === '/api/chat/slots/chat-default') return respond({ key: 'chat-default', messages: [], running: false, has_more: false, total: 0 })
        if (path.endsWith('/projections')) return respond({ asOfSeq: 0, values: {} })
        if (path === '/api/chat/slots/chat-assistant') return respond({ key: 'chat-assistant', messages, running: false, has_more: false, total: messages.length })
        if (path === '/api/chat' && method === 'POST') {
          sends++
          const body = route.request().postDataJSON()
          messages.push({ role: 'user', content: body.message, timestamp: new Date().toISOString() })
          return respond({ ok: true })
        }
        if (path === '/api/agents/installed') return respond([{ name: 'kirocrew', source: 'kirocrew' }])
        if (path === '/api/agents' && method === 'POST') {
          creates++
          const body = route.request().postDataJSON()
          members.push({ ...members[0], kiro_agent: 'kirocrew', source: 'kirocrew', name: body.name, slug: body.name.toLowerCase(), slot_key: '', bound: false, description: body.description })
          return respond({ ok: true, name: body.name, member_id: 'fixture-member' })
        }
        if (path === '/api/crons') {
          if (method === 'POST') schedules++
          return respond({ jobs: [] })
        }
        return false
      },
    })
    await page.goto(`${base}/members`)
    const welcome = page.getByTestId('assistant-welcome')
    await welcome.waitFor()
    // A bare first visit lands on the Assistant member, never on `default`.
    assert.equal(new URL(page.url()).searchParams.get('member'), 'assistant', 'lands on the Assistant member')
    assert.deepEqual(threadReads.filter(n => n === 'default'), [], 'the default member is not opened')
    await page.waitForFunction(() => document.querySelector('[data-testid="assistant-welcome"]')?.getAttribute('data-state') === 'expanded')
    const composer = page.locator('textarea').first()
    await composer.waitFor()
    await page.evaluate(() => { window.__composer = document.querySelector('textarea'); window.__welcome = document.querySelector('[data-testid="assistant-welcome"]') })
    assert.equal(await page.getByRole('dialog').count(), 0)
    await shot('assistant-welcome')
    const firstSends = width === 1440 ? 1 : 0
    if (firstSends) {
      await composer.fill('Help me plan my week.')
      await composer.press('Enter')
      await page.waitForFunction(() => document.querySelector('[data-testid="assistant-welcome"]')?.getAttribute('data-state') === 'compact')
      assert.equal(await page.evaluate(() => window.__welcome === document.querySelector('[data-testid="assistant-welcome"]')), true)
      await shot('assistant-first-message')
    }
    const openCreation = async () => {
      if (await welcome.getByTestId('assistant-welcome-create').isVisible()) {
        await welcome.getByTestId('assistant-welcome-create').click()
      } else {
        await page.getByTestId('member-add').click()
        await page.getByTestId('member-add-crewmate').click()
      }
    }
    await composer.fill('Keep this unsent draft')
    await openCreation()
    await page.getByTestId('meet-crewmates-goal').fill('Prepare a weekly progress update for my review.')
    await page.getByTestId('meet-crewmates-not-now').click()
    await composer.waitFor({ state: 'visible' })
    assert.equal(await composer.inputValue(), 'Keep this unsent draft')
    assert.equal(await page.evaluate(() => window.__composer === document.querySelector('textarea')), true)
    await openCreation()
    assert.equal(await page.getByTestId('meet-crewmates-goal').inputValue(), 'Prepare a weekly progress update for my review.')
    await shot('assistant-create')
    const panel = page.getByTestId('onboarding-chapter-embedded')
    const visibleGhosts = panel.locator('aside .pointer-events-none:visible')
    assert.equal(await visibleGhosts.count(), width < 1280 ? 0 : 4, 'four original ghosts on wide screens only')
    if (width >= 1280) {
      const brand = await panel.locator('aside span').first().boundingBox()
      const right = await panel.locator('aside .pointer-events-none').nth(1).boundingBox()
      assert.ok(right.x >= brand.x + brand.width, 'right ghost clears the brand lockup')
    }
    if (width >= 1280) {
      const brand = await panel.locator('aside span').first().boundingBox()
      const top = await panel.locator('aside .pointer-events-none').nth(3).boundingBox()
      assert.ok(top.x >= brand.x + brand.width, 'top ghost clears the brand lockup')
    }
    if (width >= 640) {
      const [box, pane] = [await panel.boundingBox(), await page.getByTestId('member-create-guided').boundingBox()]
      assert.ok(Math.abs(box.width - pane.width) <= 1 && Math.abs(box.height - pane.height) <= 1, 'embedded panel fills its pane')
    }
    await page.getByTestId('meet-crewmates-next').click()
    await page.getByTestId('meet-crewmates-name').fill('Scout')
    await page.getByTestId('meet-crewmates-next').click()
    // The footer can enter before the preceding chapter has finished exiting.
    await page.getByTestId('meet-crewmates-step-3').waitFor()
    await page.waitForFunction(() => {
      const step = document.querySelector('[data-testid="meet-crewmates-step-3"]')
      return step && getComputedStyle(step).opacity === '1'
    })
    await page.getByTestId('meet-crewmates-create').click()
    await page.getByTestId('meet-crewmates-ready').waitFor()
    assert.equal(creates, 1)
    assert.equal(schedules, 0)
    assert.equal(sends, firstSends)
    await page.getByTestId('meet-crewmates-done').click()
    await page.getByTestId('member-created-receipt').waitFor()
    assert.equal(await composer.inputValue(), 'Keep this unsent draft')
    assert.equal(await page.evaluate(() => window.__composer === document.querySelector('textarea')), true)
    await shot('assistant-return')
    await composer.fill('Help me prepare the update.')
    await composer.press('Enter')
    await page.waitForFunction(() => document.querySelector('[data-testid="assistant-welcome"]')?.getAttribute('data-state') === 'compact')
    assert.equal(await page.evaluate(() => window.__welcome === document.querySelector('[data-testid="assistant-welcome"]')), true)
    assert.equal(await page.evaluate(() => window.__composer === document.querySelector('textarea')), true)
    await shot('assistant-conversation')
    assert.equal(sends, firstSends + 1)
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth)
    assert.equal(overflow, false, `horizontal page overflow at ${width}px`)
    if (width === 1440) {
      // The reserved `default` member, opened explicitly: its own ordinary
      // chat, under its own name, with no Assistant welcome.
      await page.goto(`${base}/members?member=default`)
      await page.getByTestId('member-title-row').waitFor()
      await page.waitForFunction(() => document.querySelector('[data-testid="member-title-row"]')?.textContent?.includes('default'))
      await page.waitForTimeout(800)
      assert.equal(await page.getByTestId('assistant-welcome').count(), 0, 'default gets no Assistant welcome')
      assert.equal(new URL(page.url()).searchParams.get('member'), 'default', 'an explicit member link is not hijacked')
      await shot('default-member')
    }
    assert.deepEqual(failures, [])
    console.log(`PASS ${width}px: Assistant opens (default untouched); no overlay; one composer; draft survives; create once; no cron; same welcome contracts`)
    await ctx.close()
  }
} finally {
  await browser?.close()
  await new Promise(done => srv.close(done))
}
