/**
 * Screenshot harness for the markdown-only agent badge.
 *
 * A template defined only in markdown (`kas-md-probe`) is listed in the chat
 * agent pop-up while the chat runs on kiro-cli, which loads JSON agents only.
 * The row must wear a compact "Kiro Agent only" pill whose hover text explains why,
 * and must stay selectable. A second scene runs the same catalog on a chat
 * whose harness loads markdown (`session_backend: "kas"`): no pill.
 *
 * Captured in dark and light themes. Asserts the pill text, its title and the
 * row's selectability, so a blank or wrong frame fails the run instead of
 * passing as evidence.
 *
 * Usage: node scripts/capture-11161-markdown-only-badge.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/11161-markdown-only-badge'
const ACTIVE = 'chat-active'
mkdirSync(OUT, { recursive: true })

const now = Math.floor(Date.now() / 1000)
const slots = [{
  key: ACTIVE, title: 'Probe the markdown agent', running: false, messages: 0,
  agent: 'kirocrew', agent_kind: 'template', modified: now, last_ts: new Date().toISOString(),
  folder_id: '', last_message: '', source_links: [], source_links_total: 0,
}]

const template = (name, description, markdownOnly = false) => ({
  name, selection_kind: 'template', scope: 'global', kiro_agent: name,
  description, source: 'builtin', markdown_only: markdownOnly,
})
const catalog = sessionBackend => ({
  agents: [
    template('kirocrew', 'Primary Kiro Crew agent.'),
    template('kas-md-probe', 'A minimal markdown-format probe agent.', true),
    template('reviewer', 'Reviews pull requests.'),
  ],
  default_agent: 'kirocrew',
  session_backend: sessionBackend,
  markdown_refused_backends: ['kiro'],
  markdown_spec_backends: ['kas'],
})

const TOOLTIP = "This agent won't start here. It's defined in markdown, and Kiro CLI runs JSON agents only. Use Kiro Agent, or add a JSON file for it."

function assert(label, ok, detail = '') {
  if (!ok) throw new Error(`${label}${detail ? `: ${detail}` : ''}`)
}

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 1 })
  const results = []
  try {
    for (const theme of ['dark', 'light']) {
      for (const backend of ['kiro', 'kas']) {
        const page = await context.newPage()
        logPageProblems(page)
        const detail = { running: false, has_more: false, total: 0, queue: [], messages: [] }
        await stubDashboardApi(page, {
          slots, theme,
          extra: (path, route) => {
            if (path === '/api/agents/catalog') return json(route, catalog(backend)), true
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
        await page.waitForTimeout(400)
        const badges = pop.getByTestId('markdown-only-badge')
        if (backend === 'kiro') {
          assert('one pill', (await badges.count()) === 1, `found ${await badges.count()}`)
          assert('pill text', (await badges.first().evaluate(el => el.firstChild?.textContent ?? '')).trim() === 'Kiro Agent only')
          assert('screen-reader reason', ((await badges.first().textContent()) ?? '').includes(TOOLTIP))
          assert('pill title', (await badges.first().getAttribute('title')) === TOOLTIP)
          const option = badges.first().locator('xpath=ancestor::*[@role="option"][1]')
          assert('row selectable', (await option.getAttribute('aria-disabled')) === null)
          assert('row is kas-md-probe', ((await option.textContent()) ?? '').includes('kas-md-probe'))
        } else {
          assert('no pill on a markdown-capable harness', (await badges.count()) === 0)
        }
        const file = `${OUT}/chat-picker-${backend}-${theme}.png`
        await pop.screenshot({ path: file })
        results.push(file)
        await page.close()
      }

      // The form picker (AgentSelector): Schedule's new-job dialog. It sends no
      // chat key, so the catalog answers for a new session on agent.acp_backend.
      const page = await context.newPage()
      logPageProblems(page)
      await stubDashboardApi(page, {
        slots, theme,
        extra: (path, route) => {
          if (path === '/api/agents/catalog') return json(route, catalog('kiro')), true
          return false
        },
      })
      await page.addInitScript(() => localStorage.setItem('mc-privacy-notice-v1', '1'))
      await page.goto(base + '/schedule', { waitUntil: 'domcontentloaded' })
      const create = page.getByRole('button', { name: 'Create your first job' })
      await create.waitFor({ state: 'visible', timeout: 20_000 })
      await create.click()
      const trigger = page.getByRole('dialog').getByLabel('Switch agent')
      await trigger.waitFor({ state: 'visible', timeout: 10_000 })
      await trigger.click()
      const list = page.getByRole('dialog', { name: 'Agent list' })
      await list.waitFor({ state: 'visible', timeout: 10_000 })
      await page.waitForTimeout(400)
      const formBadges = list.getByTestId('markdown-only-badge')
      assert('one pill in the form picker', (await formBadges.count()) === 1, `found ${await formBadges.count()}`)
      assert('form pill title', (await formBadges.first().getAttribute('title')) === TOOLTIP)
      const formFile = `${OUT}/form-picker-kiro-${theme}.png`
      await list.screenshot({ path: formFile })
      results.push(formFile)
      await page.close()
    }
  } finally {
    await browser.close()
    srv.close()
  }
  for (const f of results) console.log('wrote', f)
}

main().catch(err => { console.error(err); process.exit(1) })
