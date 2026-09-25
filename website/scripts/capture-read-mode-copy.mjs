/**
 * Screenshot of Mochi's Read-mode approval row, for the copy fix that made the
 * Read-mode description name shell commands instead of promising MCP reads.
 *
 * The picker half of that change already has a harness
 * (`capture-approval-mode-discover.mjs`, scene `spotlight`). The other half is
 * `apps.mochi.settingsPanel.trust_reads_desc`, which renders in the Trust
 * section of the REAL shipped settings entry — so this drives
 * `src/apps/mochi/settings.html` itself rather than a new isolated entry, and
 * only answers the two GETs that entry needs before it leaves its loading
 * state (`/settings`, `/stats`). The Trust section is reached by clicking the
 * REAL left-rail item, so the frame documents shipped wiring.
 *
 * Both scenes ASSERT the rendered string before writing, so a frame cannot
 * document the wrong copy: the row must read "read-only shell commands" and
 * must NOT read the old "read-only operations".
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6871 --strictPort   # in another shell
 *   node scripts/capture-read-mode-copy.mjs http://127.0.0.1:6871 ../temp-screenshots/read-mode-copy
 */
import { chromium } from 'playwright'
import { mkdirSync, readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'

const BASE = process.argv[2] || 'http://127.0.0.1:6871'
const OUT = process.argv[3] || '../temp-screenshots/read-mode-copy'
mkdirSync(OUT, { recursive: true })

// Derived from the catalog, never hardcoded: a renamed key must fail loudly
// instead of silently screenshotting whatever is at that position.
const LOCALES = fileURLToPath(new URL('../src/i18n/locales/', import.meta.url))
const en = JSON.parse(readFileSync(`${LOCALES}en.json`, 'utf-8'))
const EXPECT = en.apps.mochi.settingsPanel.trust_reads_desc
const TRUST_LABEL = en.apps.mochi.settingsPanel.trust
if (!EXPECT || !TRUST_LABEL) throw new Error('catalog is missing the Mochi trust keys')

const SETTINGS = {
  petInstance: 'self',
  mode: 'quiet',
  catPreset: null,
  chatAlwaysOnTop: true,
}
const STATS = { thinkingMs: 0, messages: 0, moods: {} }

const browser = await chromium.launch()
const page = await browser.newPage({ viewport: { width: 900, height: 760 } })

// Matched on the PATHNAME, not a glob: `**/api/**` also catches the dev
// server's own `/src/api/*.ts` module requests, which then arrive as JSON and
// leave the page blank.
await page.route(
  url => new URL(url).pathname.startsWith('/api/'),
  async route => {
    const body = new URL(route.request().url()).pathname.endsWith('/stats') ? STATS : SETTINGS
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) })
  },
)

await page.goto(`${BASE}/src/apps/mochi/settings.html`, { waitUntil: 'load' })
await page.getByText(TRUST_LABEL, { exact: true }).first().click()

const row = page.getByText(EXPECT, { exact: true })
await row.waitFor({ state: 'visible', timeout: 15000 })
const stale = await page.getByText('read-only operations').count()
if (stale) throw new Error('the old "read-only operations" copy is still rendered')

await page.screenshot({ path: `${OUT}/01-mochi-read-mode-row.png` })
console.log(`01-mochi-read-mode-row: OK row=1 stale=${stale}`)
console.log(`  rendered: ${(await row.textContent())?.trim()}`)

await browser.close()
