/**
 * Screenshot harness for the composer spellcheck toggle in Settings → Chat
 * (issue #10776).
 *
 * The PR adds one Settings control, "Spell Check Message Input", in the Chat
 * tab's Composer section. Toggling it off sets `spellCheck={false}` on the
 * message composer input, so the browser draws no red misspelled-word
 * underlines. The row renders in any client (browser tab or desktop shell), so
 * the harness needs no shell bridge to make it appear.
 *
 * Runs the REAL built SPA (website/dist) behind this folder's shared
 * `lib/serve-dist.mjs`, answering every /api/** call from fixtures. No gateway,
 * no dashboard auth, no kiro-cli spawn.
 *
 * This shoots the SETTINGS row only. The red squiggle itself is drawn by
 * Chromium's spellcheck engine, which ships no dictionaries in headless, so it
 * never renders in a screenshot regardless of the attribute -- a composer frame
 * would silently show no underline in EITHER state and prove nothing. The DOM
 * evidence that the attribute follows the toggle lives in
 * src/test/ChatInput.spellcheck.test.tsx instead.
 *
 * Shots:
 *  1. Chat -> Composer, toggle ON  -- the shipped default (spellcheck on), shown
 *     beside Merge queued messages so its home row is visible in one frame.
 *  2. Chat -> Composer, toggle OFF -- after one click, the state the reporter is
 *     asking for. The row is browser-local (persisted to mc-chat-config), so
 *     nothing on the wire would prove it; the switch state in the frame does.
 *
 * Labels are read from the CATALOGS, so a key rename breaks the capture loudly
 * instead of silently screenshotting the wrong element.
 *
 * Usage: node scripts/capture-settings-spellcheck.mjs [outDir]
 */
import { mkdirSync, readFileSync } from 'node:fs'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { openSettingsPage } from './lib/settings-capture.mjs'

const OUT = process.argv[2] || '../temp-screenshots/settings-spellcheck'
const LOCALES = fileURLToPath(new URL('../src/i18n/locales/', import.meta.url))

mkdirSync(OUT, { recursive: true })

const manual = JSON.parse(readFileSync(LOCALES + 'en.manual.json', 'utf-8'))
const SPELLCHECK = manual.pages?.settings?.chatPanel?.spellcheck_input
const MERGE_QUEUED = manual.pages?.settings?.chatPanel?.merge_queued_messages
if (!SPELLCHECK) {
  throw new Error('catalog key missing -- pages.settings.chatPanel.spellcheck_input renamed?')
}

const { browser, page, srv } = await openSettingsPage({ tab: 'chat' })

const toggle = page.getByRole('switch', { name: SPELLCHECK })
await toggle.waitFor({ state: 'visible', timeout: 15_000 })
await page.waitForTimeout(600)

// Frame the row in its home: it sits under Composer, below Merge queued
// messages, so scroll that sibling into view when it exists.
if (MERGE_QUEUED) {
  await page.getByRole('switch', { name: MERGE_QUEUED }).scrollIntoViewIfNeeded().catch(() => {})
} else {
  await toggle.scrollIntoViewIfNeeded()
}
await page.waitForTimeout(300)

// Spellcheck-on is the shipped default (ChatSettings DEFAULTS.spellcheck = true),
// so a fresh profile shows the toggle on. ChatSettings.test.tsx pins the
// default; here we only capture the frame.
await page.screenshot({ path: join(OUT, '01-chat-composer-spellcheck-on.png'), fullPage: false })
console.log('captured 01-chat-composer-spellcheck-on.png')

await toggle.click()
// Let the switch settle before the shot. The write to mc-chat-config and the
// aria-checked flip are pinned by the unit test, not re-asserted here.
await page.waitForTimeout(500)
await page.screenshot({ path: join(OUT, '02-chat-composer-spellcheck-off.png'), fullPage: false })
console.log('captured 02-chat-composer-spellcheck-off.png')

await browser.close()
srv.close()
