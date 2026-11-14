/** Fixture-only screenshots of the real built Settings UI; no gateway or memories. */
import { chromium, expect } from '@playwright/test'
import { mkdirSync } from 'node:fs'
import { resolve } from 'node:path'
import { serveDist } from './lib/serve-dist.mjs'
import { stubDashboardApi, json, KIROCREW_CONFIG_FIXTURE } from './lib/stub-dashboard-api.mjs'

const out = resolve(process.argv[2] || '../../demo/screens')
mkdirSync(out, { recursive: true, mode: 0o700 })
const { srv, base } = await serveDist()
console.log('Fixture-only listener:', base)
let browser
try {
  browser = await chromium.launch()
  const page = await browser.newPage({ viewport: { width: 1440, height: 1000 }, locale: 'en-US', reducedMotion: 'reduce' })
  const errors = []
  page.on('pageerror', error => errors.push(error.message))
  await page.route('**/*', route => {
    if (new URL(route.request().url()).origin === base) return route.continue()
    return route.abort()
  })
  let enabled = false
  await stubDashboardApi(page, { extra: async (path, route) => {
    if (path === '/api/config/kirocrew') {
      if (route.request().method() === 'PATCH') {
        const body = route.request().postDataJSON()
        expect(body.path).toBe('agent.orchestrator_memory')
        enabled = body.value
        await json(route, { ok: true })
      } else {
        await json(route, { ...KIROCREW_CONFIG_FIXTURE, agent: { ...KIROCREW_CONFIG_FIXTURE.agent, acp_backend: 'kas', orchestrator_memory: enabled } })
      }
      return true
    }
    if (path === '/api/config/schema') {
      await json(route, { entries: [
        { path: 'agent.acp_backend', type: 'enum', enumValues: ['kas', 'kiro', 'claude'] },
        { path: 'agent.orchestrator_memory', type: 'boolean', defaultValue: false },
      ] })
      return true
    }
    if (path === '/api/acp-backends') {
      await json(route, { backends: ['kas', 'kiro', 'claude'].map(id => ({ id, policy_id: id, selectable: true, installed: 'installed', missing_components: [], install_command: '', restart_required: false })) })
      return true
    }
    if (path === '/api/kas-login') {
      await json(route, { authenticated: true, usable: true, expired: false, refresh_rejected: false, identity: 'demo', provider: 'BuilderId', has_refresh_token: true })
      return true
    }
    return false
  } })
  await page.goto(`${base}/settings/agent`, { waitUntil: 'domcontentloaded' })
  const toggle = page.getByRole('switch', { name: 'Orchestrator memory' })
  await expect(toggle).toBeVisible()
  await expect(toggle).toHaveAttribute('aria-checked', 'false')
  const row = page.locator('[data-setting-key="agent.orchestrator_memory"]')
  await expect(row.locator('..').getByText('Preview', { exact: true })).toBeVisible()
  await expect(page.getByTestId('kiro-sign-in-fold-state')).toBeVisible()
  await page.screenshot({ path: resolve(out, 'fixture-orchestrator-memory-off.png') })
  await toggle.click()
  await expect(toggle).toHaveAttribute('aria-checked', 'true')
  await page.reload({ waitUntil: 'domcontentloaded' })
  await expect(toggle).toHaveAttribute('aria-checked', 'true')
  await expect(page.getByTestId('kiro-sign-in-fold-state')).toBeVisible()
  await page.screenshot({ path: resolve(out, 'fixture-orchestrator-memory-on.png') })
  await row.locator('[data-settings-hint] button').click()
  await expect(page.getByRole('tooltip')).toHaveText("Uses memories saved in other Kiro clients alongside Kiro Crew's existing memory. Kiro memories are read-only here. Kiro Crew's learning behavior and each Bot's separate memory are unchanged. Kiro memories are available in Incognito chats, but excluded from Temporary chats.")
  await expect(toggle).toHaveAttribute('aria-checked', 'true')
  await page.screenshot({ path: resolve(out, 'fixture-orchestrator-memory-details.png') })
  await page.keyboard.press('Escape')
  for (const width of [390, 320]) {
    await page.setViewportSize({ width, height: 900 })
    await toggle.scrollIntoViewIfNeeded()
    await expect(toggle).toBeVisible()
    const box = await row.boundingBox()
    expect(box.x).toBeGreaterThanOrEqual(0)
    expect(box.x + box.width).toBeLessThanOrEqual(width)
    await page.screenshot({ path: resolve(out, `fixture-orchestrator-memory-${width}.png`) })
  }
  expect(errors).toEqual([])
  console.log(`Fixture UI screenshots saved to ${out}; toggle off/on/reload and 390/320px checked.`)
} finally {
  await browser?.close()
  await new Promise(resolveClose => srv.close(resolveClose))
}
