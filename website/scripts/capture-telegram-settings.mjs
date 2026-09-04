/**
 * Screenshot harness for the Telegram panel's three new settings.
 *
 * Runs the REAL built SPA (website/dist) behind the shared `serveDist` server and
 * answers every /api/** call from fixtures through `stubDashboardApi`. No gateway,
 * no dashboard auth, no bot token.
 *
 * Three settings, three reasons a reviewer has to be able to READ the copy rather
 * than take the diff's word for it:
 *
 * - **Post the model's reasoning** — off by default, and the description is what
 *   justifies that: an extra message per turn against Telegram's per-chat rate
 *   budget, which is the same budget the streaming edits already spend.
 * - **Speak the answers** — off by default for the same budget reason plus one
 *   more: text-to-speech may not be installed at all, so the copy has to say the
 *   text reply always lands first.
 * - **Answer in topics** — the one control that CHANGES behaviour for an existing
 *   forum operator, so its hint has to say what "Only when addressed" means
 *   (the bot's @handle, or a reply to one of its own messages) and that a direct
 *   message is answered regardless.
 *
 * All three render from the SHARED `BotChannelPanel` via optional spec entries, so
 * this also proves a channel that omits them does not grow the rows — Discord is
 * captured as that control.
 *
 * Usage: node scripts/capture-telegram-settings.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

import { json } from './lib/boot-api.mjs'
import { serveDist } from './lib/serve-dist.mjs'
import { stubDashboardApi } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '/tmp/telegram-settings-shots'

mkdirSync(OUT, { recursive: true })

/** The Telegram config the panel renders. */
const telegramConfig = (over = {}) => ({
  connected: true,
  connect_error: '',
  configured: true,
  read_only: false,
  bot_token_set: true,
  bot_token_preview: '12345678:AAH…dqT',
  enabled: true,
  allowed_user_ids: ['123456789'],
  soft_threshold_pct: 80,
  show_thinking: false,
  voice_replies: false,
  allow_forum: false,
  allowed_forum_chat_ids: [],
  forum_activation: 'always',
  session_folder: '',
  ...over,
})

const { srv, base } = await serveDist()
const browser = await chromium.launch()

/** Load the settings panel for `channel` with the Telegram config seeded. */
async function loadPanel(over = {}, channel = 'telegram') {
  const context = await browser.newContext({
    viewport: { width: 1500, height: 1100 },
    // The setting descriptions are ~11px; a 1x shot renders them too soft to
    // read, and reading them is the entire point of this capture.
    deviceScaleFactor: 2,
  })
  const page = await context.newPage()

  /** Each branch AWAITS `json()` then returns true; a falsy return means "not handled". */
  const extra = async (path, route) => {
    if (path === '/api/telegram/config') {
      await json(route, telegramConfig(over))
      return true
    }
    // The editable panel renders ONLY on a confirmed per-channel ALLOW; an
    // unknown policy deliberately hides the config, so without this the capture
    // shows the "policy status unavailable" notice instead of the settings.
    if (path === '/api/governance/channels') {
      await json(route, {
        slack: true, discord: true, telegram: true, webex: true,
        wecom: true, teams: true, weixin: true, imessage: true,
      })
      return true
    }
    return false
  }

  await stubDashboardApi(page, { extra })
  await page.goto(`${base}/settings?tab=channels&channel=${channel}`, {
    waitUntil: 'domcontentloaded',
  })
  await page.waitForTimeout(2600)
  return page
}

/** Crop around `anchorText` so the control, its description and its neighbours fit. */
async function shot(page, name, anchorText, height = 460) {
  const row = page.getByText(anchorText).first()
  await row.waitFor({ timeout: 10000 })
  // The controls sit near the bottom of a long panel, so their box is outside
  // the viewport until scrolled in — clipping to an off-screen box fails.
  await row.scrollIntoViewIfNeeded()
  await page.waitForTimeout(600)
  const box = await row.boundingBox()
  const x = Math.max(0, box.x - 48)
  const y = Math.max(0, box.y - 170)
  await page.screenshot({
    path: `${OUT}/${name}.png`,
    clip: { x, y, width: Math.min(1500 - x, 1020), height: Math.min(1100 - y, height) },
  })
  console.log('wrote', `${OUT}/${name}.png`)
}

try {
  // Shipped defaults: both behaviour toggles off, and the copy that justifies it.
  const off = await loadPanel()
  await shot(off, 'telegram-behavior-defaults-off', "Post the model's reasoning")

  // Both opted into, so a reviewer can see the on-state reached by config.
  const on = await loadPanel({ show_thinking: true, voice_replies: true })
  await shot(on, 'telegram-behavior-both-on', "Post the model's reasoning")

  // The activation selector, with the forum section enabled so it renders. This
  // is the control that changes behaviour for an existing operator, so its hint
  // is the copy that matters most here.
  const forum = await loadPanel({
    allow_forum: true,
    allowed_forum_chat_ids: ['-1001234567890'],
    forum_activation: 'mention',
  })
  await shot(forum, 'telegram-forum-activation-mention', 'Answer in topics', 520)

  // A channel that omits the optional spec entries must not grow the rows.
  // Discord is the same shared panel, so this is the shared-panel half of the claim.
  const discord = await loadPanel({}, 'discord')
  for (const absent of ["Post the model's reasoning", 'Speak the answers', 'Answer in topics']) {
    const n = await discord.getByText(absent).count()
    if (n !== 0) throw new Error(`discord grew the "${absent}" row (${n} matches)`)
  }
  await discord.screenshot({ path: `${OUT}/discord-unchanged.png`, fullPage: false })
  console.log('wrote', `${OUT}/discord-unchanged.png`, '(none of the three rows, as expected)')
} finally {
  await browser.close()
  srv.close()
}
