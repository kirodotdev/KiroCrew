/**
 * Screenshot harness for Discord's shared-channel settings.
 *
 * `discord.allowed_channel_ids` and `discord.auto_thread` were accepted by the
 * save endpoint but unreachable from the dashboard, so the only evidence a
 * reviewer could have had was the code. The three states below are the ones that
 * change what an operator decides:
 *
 *   1. the default (no channels listed, auto-thread on), which is what ships
 *   2. a channel listed with auto-thread on, the working configuration
 *   3. the same channel with auto-thread OFF, where the listed channel becomes
 *      INERT rather than answered in place, and the panel has to say so
 *
 * State 3 is the whole reason the hint exists: the transport returns early when
 * auto-thread is off, so a channel list with the toggle down silently does
 * nothing. Capturing it means the wording cannot drift from the shipped string
 * without this script's output changing too.
 *
 * Runs the REAL built SPA (website/dist) with every /api/** call answered from
 * fixtures. No gateway, no dashboard token, nothing written. Only the network and
 * the localStorage seed are stubbed; the panel code is the shipped code.
 *
 * Usage:
 *   npm run build
 *   node scripts/capture-discord-shared-channels.mjs ../temp-screenshots/discord-channels
 */
import { chromium } from 'playwright'
import { mkdirSync, renameSync } from 'node:fs'
import { join } from 'node:path'

import { serveDist } from './lib/serve-dist.mjs'
import { json, logPageProblems, stubDashboardApi } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/discord-channels'

mkdirSync(OUT, { recursive: true })

/** Mutated between loads; the route closure reads it at call time. */
const scene = { channels: [], autoThread: true, theme: 'dark' }

/** What `GET /api/discord/config` returns, field for field. */
const discordConfig = () => ({
  enabled: true,
  configured: true,
  connected: true,
  read_only: false,
  bot_token_set: true,
  bot_token_preview: 'MTA1…xYz',
  connect_error: '',
  allowed_user_ids: ['284102345871466496'],
  allowed_thread_ids: [],
  allowed_channel_ids: scene.channels,
  auto_thread: scene.autoThread,
  soft_threshold_pct: 80,
  session_folder: '',
})

async function main() {
  const { srv, base } = await serveDist()

  const browser = await chromium.launch()
  const context = await browser.newContext({
    viewport: { width: 1500, height: 1100 },
    // The hint under the toggle is ~12px. A 1x shot renders it too soft to read,
    // and reading it is the point of state 3.
    deviceScaleFactor: 2,
  })
  const page = await context.newPage()
  logPageProblems(page)

  // The panel renders its editable form ONLY on a confirmed governance ALLOW; an
  // unknown policy deliberately hides the config, so without this the capture
  // shows the "policy status unavailable" notice instead of the settings.
  const routeDiscord = async (path, route) => {
    if (path === '/api/discord/config') {
      await json(route, discordConfig())
      return true
    }
    if (path === '/api/governance/channels') {
      await json(route, { slack: true, discord: true, telegram: true, webex: true })
      return true
    }
    return false
  }

  async function load() {
    await stubDashboardApi(page, { theme: scene.theme, extra: routeDiscord })
    await page.goto(`${base}/settings?tab=channels&channel=discord`, {
      waitUntil: 'domcontentloaded',
    })
    await page.waitForTimeout(2600)
  }

  /** Crop from a section heading down, so the section's controls and their
   *  helper text land in one legible frame. */
  async function shotFrom(headingText, name, height) {
    const heading = page.getByText(headingText).first()
    await heading.waitFor({ timeout: 10000 })
    // The section sits below a long panel, so its box is off-viewport until
    // scrolled in, and clipping to an off-screen box fails.
    await heading.scrollIntoViewIfNeeded()
    await page.waitForTimeout(600)
    const box = await heading.boundingBox()
    const x = Math.max(0, box.x - 40)
    const y = Math.max(0, box.y - 30)
    await page.screenshot({
      path: `${OUT}/${name}.png`,
      clip: {
        x,
        y,
        width: Math.min(1500 - x, 1020),
        height: Math.min(1100 - y, height),
      },
    })
    console.log('wrote', `${OUT}/${name}.png`)
  }

  const shot = name => shotFrom('Shared channels', name, 560)

  scene.channels = []
  scene.autoThread = true
  await load()
  await shot('discord-shared-channels-default')

  scene.channels = ['345678901234567890']
  scene.autoThread = true
  await load()
  await shot('discord-shared-channels-listed')

  // The state the hint exists for: listed channels with the toggle down are
  // never answered, because the turn only ever runs in a promoted thread.
  scene.autoThread = false
  await load()
  await shot('discord-shared-channels-autothread-off')

  scene.autoThread = true
  scene.theme = 'light'
  await load()
  await shot('discord-shared-channels-light')

  // The two render toggles live in the Behavior card below, and they are the
  // other half of what became reachable: both were config-only before.
  scene.theme = 'dark'
  await load()
  await shotFrom('Behavior', 'discord-behavior-toggles', 380)

  await page.close()
  await context.close()

  // A recording of the real interaction, because the still frames above are
  // fixture STATES: they show what each configuration looks like but not that
  // typing a channel id and flipping the toggle actually drives the panel. The
  // clip walks one operator through it, ending on the inert-channel warning.
  scene.channels = []
  scene.autoThread = true
  scene.theme = 'dark'
  const filmed = await browser.newContext({
    viewport: { width: 1500, height: 1100 },
    recordVideo: { dir: OUT, size: { width: 1500, height: 1100 } },
  })
  const clip = await filmed.newPage()
  logPageProblems(clip)
  await stubDashboardApi(clip, { theme: scene.theme, extra: routeDiscord })
  await clip.goto(`${base}/settings?tab=channels&channel=discord`, {
    waitUntil: 'domcontentloaded',
  })
  await clip.waitForTimeout(2600)

  const heading = clip.getByText('Shared channels').first()
  await heading.waitFor({ timeout: 10000 })
  await heading.scrollIntoViewIfNeeded()
  await clip.waitForTimeout(900)

  // Type a channel id the way an operator would, one visible keystroke at a
  // time, then commit it with Enter. Enter rather than the Add button because
  // three tag editors on this panel share that label, so a click has to be
  // disambiguated by DOM order, which silently commits nothing when it guesses
  // wrong. The editor treats Enter as its commit, so this exercises the same
  // path a user takes.
  const field = clip.getByPlaceholder('123456789012345678').last()
  await field.click()
  await field.pressSequentially('345678901234567890', { delay: 55 })
  await clip.waitForTimeout(500)
  await field.press('Enter')
  // The chip is the proof the value committed. Without this the clip can end on
  // a toggle flipped over an EMPTY list, where the warning correctly does not
  // render, and the recording would then look like the feature is broken.
  await clip.getByText('345678901234567890').first().waitFor({ timeout: 5000 })
  await clip.waitForTimeout(1100)

  // Flipping the toggle down is the moment the warning appears.
  await clip.getByRole('switch', { name: 'Answer in a new thread' }).click()
  await clip.getByText(/messages in the channels above are ignored/).waitFor({ timeout: 5000 })
  await clip.waitForTimeout(1900)

  await clip.close()
  const video = await clip.video()?.path()
  await filmed.close()
  if (video) {
    const dest = join(OUT, 'discord-shared-channels-walkthrough.webm')
    if (video !== dest) renameSync(video, dest)
    console.log('wrote', dest)
  }

  await browser.close()
  srv.close()
}

main().catch(err => {
  console.error(err)
  process.exit(1)
})
