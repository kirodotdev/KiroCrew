/**
 * Screenshots for #29472: typing `$` in a message's Edit & Resend field opens
 * the composer's skill menu.
 *
 * Drives the REAL ChatPage through the existing isolated entry
 * (capture/regenerate-preserve-reply.html), with `/api/skills` answered by a
 * fixed list. The script opens Edit & Resend from the message's More menu,
 * types a `$` token, and asserts before each PNG:
 *   menu-<theme>.png    the skill menu is open under the edit field, listing
 *                       the matching skill
 *   picked-<theme>.png  Enter put `$oncall-handover ` into the field, the menu
 *                       is closed and the editor is still open (nothing sent)
 * With `--before`, run against a checkout without the change, it takes one
 * frame per theme (before-<theme>.png) and asserts no menu opened.
 *
 * Starts its own loopback vite dev server.
 *
 * Usage (in website/):
 *   node scripts/capture-29472-edit-skill-picker.mjs ../temp-screenshots/29472 [--before]
 */
import { chromium } from 'playwright'
import { createServer } from 'vite'
import { mkdirSync } from 'node:fs'

const OUT = process.argv[2] || '../temp-screenshots/29472-edit-skill-picker'
const BEFORE = process.argv.includes('--before')
mkdirSync(OUT, { recursive: true })

const SKILLS = [
  { key: 'team/oncall-handover', name: 'oncall-handover', description: 'Write the on-call handover report', source: 'kirocrew' },
  { key: 'ticket-pull', name: 'ticket-pull', description: 'Pull my open tickets', source: 'kirocrew' },
]
const USER = { role: 'user', content: 'Summarize what changed in the release branch this week.', cls: '', ts: '2026-10-24T14:00:00Z', meta: { mid: 'm-user-1' } }
const REPLY = { role: 'assistant', content: 'Three changes landed this week:\n\n- the retry budget moved to config\n- the health check now times out after 5s\n- two flaky tests were quarantined', cls: '', ts: '2026-10-24T14:00:20Z', meta: { mid: 'm-reply-1' } }

const server = await createServer({
  configFile: './vite.config.ts',
  server: { host: '127.0.0.1', port: 5199, strictPort: false },
  logLevel: 'warn',
})
await server.listen()
const BASE = server.resolvedUrls?.local?.[0]?.replace(/\/$/, '') || `http://127.0.0.1:${server.config.server.port}`

const { LD_LIBRARY_PATH: _mise, ...browserEnv } = process.env
const browser = await chromium.launch({ env: browserEnv })
let failures = 0
const check = (label, ok) => { console.log(`${label} -> ${ok ? 'ok' : 'FAIL'}`); if (!ok) failures++ }

async function run(theme) {
  const ctx = await browser.newContext({ viewport: { width: 1100, height: 720 }, deviceScaleFactor: 1 })
  const page = await ctx.newPage()
  const errors = []
  page.on('pageerror', e => errors.push(String(e)))
  const state = { resends: 0 }
  await page.route(u => new URL(u).pathname.startsWith('/api/'), route => {
    const path = new URL(route.request().url()).pathname
    if (path === '/api/skills') return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(SKILLS) })
    if (path.endsWith('/edit-resend')) { state.resends++; return route.fulfill({ status: 200, contentType: 'application/json', body: '{"ok":true}' }) }
    if (/^\/api\/chat\/slots\/[^/]+$/.test(path)) {
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ key: 'slot-a', title: 'release notes', running: false, has_more: false, total: 2, messages: [USER, REPLY] }) })
    }
    if (/\/api\/chat\/(tags|pins|folders|tag-columns)$/.test(path)) return route.fulfill({ status: 200, contentType: 'application/json', body: '[]' })
    const isList = /commands|agents$|sessions|files|history|models|artifacts|folders|slots$/.test(path)
    return route.fulfill({ status: 200, contentType: 'application/json', body: isList ? '[]' : '{}' })
  })
  await page.goto(`${BASE}/capture/regenerate-preserve-reply.html?theme=${theme}`)
  await page.getByText('two flaky tests were quarantined').first().waitFor()
  await page.waitForTimeout(300)

  const bubble = page.getByText('Summarize what changed in the release branch this week.').first()
  await bubble.hover()
  await page.getByTestId('user-more-actions').first().click()
  await page.getByRole('menuitem', { name: 'Edit & Resend' }).click()
  const field = page.getByRole('textbox', { name: 'Edit message' })
  await field.waitFor()
  await field.press('End')
  await field.pressSequentially(' Use $onc', { delay: 20 })
  await page.waitForTimeout(400)

  const option = page.getByRole('option').filter({ hasText: '$oncall-handover' })
  if (BEFORE) {
    check(`${theme} before: no skill menu`, (await page.getByRole('listbox').count()) === 0)
    await page.locator('[data-capture-root]').screenshot({ path: `${OUT}/before-${theme}.png` })
  } else {
    await option.waitFor()
    check(`${theme} menu: skill listed`, await option.isVisible())
    check(`${theme} menu: editor still open`, await field.isVisible())
    await page.locator('[data-capture-root]').screenshot({ path: `${OUT}/menu-${theme}.png` })
    await field.press('Enter')
    await page.waitForTimeout(300)
    check(`${theme} picked: token inserted`, (await field.inputValue()).endsWith('Use $oncall-handover '))
    check(`${theme} picked: menu closed`, (await page.getByRole('listbox').count()) === 0)
    check(`${theme} picked: nothing resent`, state.resends === 0)
    await page.locator('[data-capture-root]').screenshot({ path: `${OUT}/picked-${theme}.png` })
  }
  if (errors.length) { console.error(errors.join('\n')); failures++ }
  await ctx.close()
}

for (const theme of ['dark', 'light']) await run(theme)

await browser.close()
await server.close()
if (failures) { console.error(`\n${failures} assertion(s) failed.`); process.exit(1) }
console.log('\nall states render as specified.')
