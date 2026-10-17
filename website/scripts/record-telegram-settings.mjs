/**
 * Screen recording of the Telegram panel's new settings being used.
 *
 * The still frames prove the copy renders; this proves the controls WORK — each
 * one flips, the save button posts, and the panel reports the restart the backend
 * asks for. A reviewer judging an off-by-default setting needs to see the on-state
 * reached by a click rather than by a fixture, and a reviewer judging a selector
 * that changes behaviour for existing operators needs to see it round-trip.
 *
 * Runs the REAL built SPA (website/dist) behind the shared `serveDist` server with
 * every /api/** call answered from fixtures. `PUT /api/telegram/config` mutates the
 * fixture the panel re-reads, so the recording shows the round trip rather than an
 * optimistic local flip.
 *
 * Usage: node scripts/record-telegram-settings.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync, renameSync, readdirSync } from 'node:fs'
import { join } from 'node:path'

import { json } from './lib/boot-api.mjs'
import { serveDist } from './lib/serve-dist.mjs'
import { stubDashboardApi } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '/tmp/telegram-settings-video'

mkdirSync(OUT, { recursive: true })

const { srv, base } = await serveDist()
const browser = await chromium.launch()
const context = await browser.newContext({
  viewport: { width: 1280, height: 900 },
  recordVideo: { dir: OUT, size: { width: 1280, height: 900 } },
})
const page = await context.newPage()

// Mutable, so the panel's post-save re-read reflects the writes the clicks made.
const state = {
  show_thinking: false,
  voice_replies: false,
  forum_activation: 'always',
  restart_required: false,
}

const config = () => ({
  connected: true,
  connect_error: '',
  configured: true,
  read_only: false,
  bot_token_set: true,
  bot_token_preview: '12345678:AAH…dqT',
  enabled: true,
  allowed_user_ids: ['123456789'],
  soft_threshold_pct: 80,
  show_thinking: state.show_thinking,
  voice_replies: state.voice_replies,
  // On, so the activation selector renders at all.
  allow_forum: true,
  allowed_forum_chat_ids: ['-1001234567890'],
  forum_activation: state.forum_activation,
  session_folder: '',
})

/** Each branch AWAITS `json()` then returns true; a falsy return means "not handled". */
const extra = async (path, route) => {
  if (path === '/api/telegram/config') {
    if (route.request().method() === 'PUT') {
      const body = JSON.parse(route.request().postData() || '{}')
      for (const key of ['show_thinking', 'voice_replies']) {
        if (typeof body[key] === 'boolean') state[key] = body[key]
      }
      if (typeof body.forum_activation === 'string') state.forum_activation = body.forum_activation
      // Every Telegram field is boot-read, so any real change asks for a restart.
      state.restart_required = true
      await json(route, { ok: true, restart_required: state.restart_required, verify_warning: '' })
      return true
    }
    await json(route, config())
    return true
  }
  if (path === '/api/governance/channels') {
    await json(route, {
      slack: true, discord: true, telegram: true, webex: true,
      wecom: true, teams: true, weixin: true, imessage: true,
    })
    return true
  }
  return false
}

/** Click the settings row whose label contains `needle`. */
async function toggle(needle) {
  // `SettingsToggle` renders the WHOLE row as the clickable, tagged with
  // data-setting-label — a more stable handle than the label text, which can
  // carry a typographic apostrophe the catalog owns.
  const row = page.locator(`[data-setting-label*="${needle}"]`).first()
  await row.waitFor({ timeout: 10000 })
  await row.scrollIntoViewIfNeeded()
  // Held so the recording lingers on the copy that justifies the default.
  await page.waitForTimeout(1600)
  await row.click({ timeout: 5000 })
  await page.waitForTimeout(1200)
}

try {
  await stubDashboardApi(page, { extra })
  await page.goto(`${base}/settings?tab=channels&channel=telegram`, {
    waitUntil: 'domcontentloaded',
  })
  await page.waitForTimeout(2600)

  await toggle('reasoning')
  await toggle('answers')

  // The activation selector: pick the mode that changes behaviour, so the
  // recording shows the one control an existing forum operator has to think about.
  // `SettingsSelect` renders a Radix combobox, NOT a native <select>, so it is
  // driven by opening the trigger and clicking the option rather than by
  // selectOption — which is also what a user actually does, and therefore the
  // interaction worth recording.
  const trigger = page.getByRole('combobox').last()
  await trigger.scrollIntoViewIfNeeded()
  await page.waitForTimeout(1400)
  await trigger.click({ timeout: 8000 })
  await page.waitForTimeout(900)
  await page.getByRole('option', { name: /addressed/i }).click({ timeout: 8000 })
  await page.waitForTimeout(1200)

  await page.getByRole('button', { name: /Save/i }).first().click({ timeout: 8000 })
  // Long enough for the save to land and the restart hint to render.
  await page.waitForTimeout(3500)
} finally {
  await context.close()   // flushes the video file
  await browser.close()
  srv.close()
}

// Playwright names videos by a random id; rename to something a PR body can cite.
for (const name of readdirSync(OUT)) {
  if (name.endsWith('.webm') && name !== 'telegram-settings.webm') {
    renameSync(join(OUT, name), join(OUT, 'telegram-settings.webm'))
  }
}
console.log('wrote', join(OUT, 'telegram-settings.webm'))
