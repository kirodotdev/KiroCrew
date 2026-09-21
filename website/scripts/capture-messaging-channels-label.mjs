/**
 * Screenshot harness for the "Messaging Channels" Settings tab label.
 *
 * Runs the REAL built SPA (website/dist) behind the shared loopback static
 * server (`./lib/serve-dist.mjs`), and answers every /api/** call from fixtures
 * via Playwright route interception. No gateway, no dashboard auth, no kiro-cli
 * spawn.
 *
 * Three scenes, each in dark theme:
 *   - wide, English:  /settings/channels — the rail shows the renamed tab.
 *   - wide, zh-CN:    /settings/channels — the same rail reads 聊天平台.
 *   - mobile, English: /settings/channels/slack — the single SubNav back bar
 *                      carries the renamed parent label.
 *
 * Usage: node scripts/capture-messaging-channels-label.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { join } from 'node:path'
import { json, makeFixedApi, handleBootRoute } from './lib/boot-api.mjs'
import { serveDist } from './lib/serve-dist.mjs'

const OUT = process.argv[2] || '/tmp/messaging-channels-label-shots'

mkdirSync(OUT, { recursive: true })

const PROJECT = '/home/user/project'
const fixedApi = makeFixedApi(PROJECT)

const CHANNEL_KEYS = ['slack', 'discord', 'telegram', 'webex', 'wecom', 'feishu', 'teams', 'weixin', 'imessage', 'whatsapp']
const GOVERNANCE = Object.fromEntries(CHANNEL_KEYS.map(k => [k, true]))
const SLACK_CONFIG = {
  connected: false, connect_error: '', configured: false, read_only: false,
  bot_token_set: false, app_token_set: false, bot_token_preview: '', app_token_preview: '',
  owner_id: '', command: '/kiro', allowed_enterprise_ids: [], reactions_enabled: true,
  show_thinking: false, session_folder: '',
}
const NOT_CONNECTED = { connected: false, connect_error: '', configured: false, read_only: false, enabled: false }

const { srv, base } = await serveDist()
const browser = await chromium.launch()

async function shoot({ file, lang, width, height, path, waitFor }) {
  const context = await browser.newContext({ viewport: { width, height }, deviceScaleFactor: 2 })
  const page = await context.newPage()
  await page.routeWebSocket(/\/api\/ws/, () => {})
  await page.route('**/api/**', route => {
    const p = new URL(route.request().url()).pathname
    if (p === '/api/chat/slots') return json(route, [])
    if (p === '/api/governance/channels') return json(route, GOVERNANCE)
    if (p === '/api/slack/config') return json(route, SLACK_CONFIG)
    if (p === '/api/slack/manifest') return json(route, { alias: 'kiro', manifest: '{}', create_url: '' })
    if (/^\/api\/[a-z]+\/config$/.test(p)) return json(route, NOT_CONNECTED)
    return handleBootRoute(route, p, { project: PROJECT, fixedApi })
  })
  await page.addInitScript(l => {
    localStorage.clear()
    localStorage.setItem('mc-theme', 'dark')
    localStorage.setItem('mc-onboarded', '1')
    localStorage.setItem('mc-import-onboarded', '1')
    localStorage.setItem('mc-privacy-acked', '1')
    localStorage.setItem('mc-lang', l)
  }, lang)
  await page.goto(`${base}${path}`, { waitUntil: 'domcontentloaded' })
  await waitFor(page)
  await page.waitForTimeout(400)
  await page.screenshot({ path: join(OUT, file), fullPage: false })
  console.log(`captured ${file}`)
  await context.close()
}

await shoot({
  file: 'wide-en-settings-rail.png', lang: 'en', width: 1400, height: 900, path: '/settings/channels',
  waitFor: p => p.getByRole('button', { name: 'Messaging Channels' }).waitFor({ timeout: 15_000 }),
})
await shoot({
  file: 'wide-zh-settings-rail.png', lang: 'zh-CN', width: 1400, height: 900, path: '/settings/channels',
  waitFor: p => p.getByRole('button', { name: '聊天平台' }).waitFor({ timeout: 15_000 }),
})
await shoot({
  file: 'mobile-en-slack-back-bar.png', lang: 'en', width: 390, height: 844, path: '/settings/channels/slack',
  waitFor: p => p.getByRole('button', { name: 'Messaging Channels' }).waitFor({ timeout: 15_000 }),
})

await browser.close()
srv.close()
