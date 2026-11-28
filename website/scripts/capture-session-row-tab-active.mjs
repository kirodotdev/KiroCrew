/**
 * Screenshot harness for the "Switch to Session Tabs on Cmd/Ctrl-Click" toggle
 * in Settings -> Chat -> Sessions (issue #19835).
 *
 * The toggle stores `sessionRowTabActive` in mc-chat-config. Off, the default,
 * Cmd/Ctrl-click on a sidebar session row queues a background tab, as the
 * 2026-10-17 sidebar decision says. On, it opens the session as the active tab.
 * The click behaviour itself is pinned in src/test/ChatSidebar.openInTab.test.tsx;
 * this harness shoots the Settings row in both states.
 *
 * Runs the REAL built SPA (website/dist) behind this folder's shared
 * `lib/serve-dist.mjs`, answering every /api/** call from fixtures. No gateway,
 * no dashboard auth, no kiro-cli spawn.
 *
 * Shots:
 *  1. Chat -> Sessions, toggle OFF -- the shipped default.
 *  2. Chat -> Sessions, toggle ON -- after one click.
 *  3. The same row with its info tip open, showing the hint text.
 *
 * Each shot is clipped to the Sessions card around the toggle. The shared
 * fixture server answers the model list and the starred-bots read with empty
 * bodies, so the full page also shows two load-error notices that have nothing
 * to do with this toggle.
 *
 * Labels are read from the CATALOGS, so a key rename breaks the capture loudly
 * instead of silently screenshotting the wrong element.
 *
 * Usage: node scripts/capture-session-row-tab-active.mjs [outDir]
 */
import { mkdirSync, readFileSync } from 'node:fs'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { openSettingsPage } from './lib/settings-capture.mjs'

const OUT = process.argv[2] || '../temp-screenshots/session-row-tab-active'
const LOCALES = fileURLToPath(new URL('../src/i18n/locales/', import.meta.url))

mkdirSync(OUT, { recursive: true })

const manual = JSON.parse(readFileSync(LOCALES + 'en.manual.json', 'utf-8'))
const LABEL = manual.pages?.settings?.chatPanel?.session_row_tab_active
if (!LABEL) {
  throw new Error('catalog key missing -- pages.settings.chatPanel.session_row_tab_active renamed?')
}

const { browser, page, srv, base } = await openSettingsPage({ tab: 'chat' })
await page.goto(`${base}/settings/chat/sessions`, { waitUntil: 'domcontentloaded' })

const toggle = page.getByRole('switch', { name: LABEL })
await toggle.waitFor({ state: 'visible', timeout: 15_000 })
await toggle.scrollIntoViewIfNeeded()
await page.waitForTimeout(600)

/** The card from its left edge to the switch column, a few rows each side. */
async function cardClip() {
  const label = await page.getByText(LABEL, { exact: true }).boundingBox()
  const sw = await toggle.boundingBox()
  if (!label || !sw) throw new Error('toggle row not laid out')
  const x = Math.max(0, label.x - 32)
  const y = Math.max(0, sw.y - 220)
  return { x, y, width: sw.x + sw.width + 32 - x, height: 460 }
}

if (await toggle.getAttribute('aria-checked') !== 'false') throw new Error('toggle is not off by default')
await page.screenshot({ path: join(OUT, '01-off-default.png'), clip: await cardClip() })
console.log('captured 01-off-default.png')

await toggle.click()
await page.waitForTimeout(500)
const stored = await page.evaluate(() => JSON.parse(localStorage.getItem('mc-chat-config') || '{}').sessionRowTabActive)
if (stored !== true) throw new Error(`toggle did not store sessionRowTabActive: ${stored}`)
await page.screenshot({ path: join(OUT, '02-on.png'), clip: await cardClip() })
console.log('captured 02-on.png (mc-chat-config.sessionRowTabActive = true)')

const HINT_START = manual.pages.settings.chatPanel.session_row_tab_active_desc.split('{{mod}}')[1].slice(0, 20)
const tip = toggle.locator('xpath=ancestor::*[.//*[@data-settings-hint]][1]').locator('[data-settings-hint] button')
await tip.hover()
await page.getByText(HINT_START).first().waitFor({ state: 'visible', timeout: 5_000 })
await page.waitForTimeout(300)
await page.screenshot({ path: join(OUT, '03-hint.png'), clip: await cardClip() })
console.log('captured 03-hint.png (info tip open)')

await browser.close()
srv.close()
