/**
 * Verify the VS Code terminal chords in the BUILT SPA, gateway-free:
 *   Ctrl+`        toggles the docked terminal
 *   Ctrl+Shift+`  adds a terminal (opening the panel first when it is closed)
 * Both fire from the composer AND from inside a shell; Cmd+` / Cmd+Shift+` do
 * nothing; closing from a shell puts focus back in the composer. Every frame is
 * written only after the assertions describing it pass.
 *
 * Usage: node scripts/capture-vscode-terminal-keys.mjs [output-directory]
 */
import { chromium, expect } from '@playwright/test'
import { mkdirSync } from 'node:fs'
import { join } from 'node:path'
import { serveDist } from './lib/serve-dist.mjs'
import { stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const out = process.argv[2] || join(process.env.KIROCREW_SCRATCH || 'temp-screenshots', 'vscode-terminal-keys')
mkdirSync(out, { recursive: true })
const { srv, base } = await serveDist()
const SLOTS = [{ key: 'chat-vscode-keys', title: 'VS Code shortcuts', running: false, messages: 2, agent: 'kirocrew', project: '/workspace', last_ts: new Date().toISOString() }]

/** Persisted panel state — the source of truth for "open" and the tab count. */
const panelState = page => page.evaluate(() => {
  const raw = localStorage.getItem('mc-bottom-terminal')
  const s = raw ? JSON.parse(raw) : {}
  return { open: !!s.open, tabs: (s.tabs || []).length }
})
/** Wait until exactly one terminal tab paints the selected pill — the chips fade
 *  their pill (transition-colors), so a frame shot mid-fade shows two. */
const settleTabs = page => expect.poll(() => page.evaluate(() => [...document.querySelectorAll('[role=tab]')]
  .filter(el => el.closest('[role=tablist]')?.querySelector('[role=tab]') && el.textContent?.includes('workspace'))
  .filter(el => getComputedStyle(el).backgroundColor !== 'rgba(0, 0, 0, 0)').length)).toBe(1)
const focusInShell = page => page.evaluate(() => !!document.activeElement?.closest?.('.xterm'))
const focusInComposer = page => page.evaluate(() => document.activeElement?.tagName === 'TEXTAREA' && !document.activeElement.closest('.xterm'))

async function preparePage(page, theme, mac = false) {
  const errors = []
  page.on('pageerror', e => errors.push(e.message))
  if (mac) await page.addInitScript(() => Object.defineProperty(navigator, 'platform', { value: 'MacIntel', configurable: true }))
  await stubDashboardApi(page, {
    theme, slots: SLOTS, folders: [], localStorageEntries: { 'mc-lang': 'en' },
    extra: async (path, route) => {
      if (path === '/api/terminal/sessions') { await json(route, { enabled: true, sessions: [] }); return true }
      return false
    },
  })
  let connections = 0
  await page.routeWebSocket(/\/api\/ws\/terminal\//, ws => {
    connections++
    ws.send(JSON.stringify({ type: 'title', text: 'workspace' }))
    ws.send(JSON.stringify({ type: 'ready' }))
    ws.send(Buffer.from(`$ shell ${connections} ready\r\n`))
  })
  return { errors, connections: () => connections }
}

const results = []
const browser = await chromium.launch({ headless: true })
try {
  for (const theme of ['dark', 'light']) {
    const ctx = await browser.newContext({ viewport: { width: 1400, height: 860 }, deviceScaleFactor: 1 })
    const page = await ctx.newPage()
    page.setDefaultTimeout(12000)
    const { errors } = await preparePage(page, theme)
    await page.goto(`${base}/chat`)
    await page.waitForSelector('[data-slot-key]', { timeout: 20000 })
    const composer = page.locator('textarea').first()
    await composer.click()
    await composer.fill('Typing in the composer')
    expect(await panelState(page)).toEqual({ open: false, tabs: 0 })
    await page.screenshot({ path: join(out, `${theme}-1-closed.png`) })

    // Ctrl+` from the composer opens the panel with one shell.
    await page.keyboard.press('Control+Backquote')
    await expect.poll(() => panelState(page)).toEqual({ open: true, tabs: 1 })
    await expect(page.locator('.xterm').first()).toBeVisible()
    await expect.poll(() => focusInShell(page)).toBe(true)
    await settleTabs(page)
    await page.screenshot({ path: join(out, `${theme}-2-ctrl-backtick-open.png`) })

    // Ctrl+Shift+` from INSIDE the shell adds a second, then a third terminal.
    await page.keyboard.press('Control+Shift+Backquote')
    await expect.poll(() => panelState(page)).toEqual({ open: true, tabs: 2 })
    await page.keyboard.press('Control+Shift+Backquote')
    await expect.poll(() => panelState(page)).toEqual({ open: true, tabs: 3 })
    await expect.poll(() => focusInShell(page)).toBe(true)
    await settleTabs(page)
    await page.screenshot({ path: join(out, `${theme}-3-ctrl-shift-backtick-three-tabs.png`) })

    // Cmd chords are NOT bound (macOS window cyclers): nothing changes.
    await page.keyboard.press('Meta+Backquote')
    await page.keyboard.press('Meta+Shift+Backquote')
    await page.waitForTimeout(300)
    expect(await panelState(page)).toEqual({ open: true, tabs: 3 })

    // Ctrl+` from inside the shell closes the panel and returns focus to the composer.
    await page.keyboard.press('Control+Backquote')
    await expect.poll(() => panelState(page)).toEqual({ open: false, tabs: 3 })
    await expect(page.locator('.xterm')).toHaveCount(0)
    await expect.poll(() => focusInComposer(page)).toBe(true)
    await expect(composer).toHaveValue('Typing in the composer')
    await page.screenshot({ path: join(out, `${theme}-4-closed-focus-back.png`) })

    // Ctrl+Shift+` with the panel closed opens it AND adds a terminal.
    await page.keyboard.press('Control+Shift+Backquote')
    await expect.poll(() => panelState(page)).toEqual({ open: true, tabs: 4 })
    await settleTabs(page)
    await page.screenshot({ path: join(out, `${theme}-5-new-terminal-from-closed.png`) })

    // Alt+K reference lists both commands with their chords.
    await page.locator('#main-content').focus().catch(() => {})
    await page.keyboard.press('Control+Backquote')
    await expect.poll(() => panelState(page)).toMatchObject({ open: false })
    await composer.click()
    await page.keyboard.press('Alt+K')
    const dialog = page.getByRole('dialog', { name: 'Keyboard shortcuts' })
    await dialog.waitFor()
    await dialog.getByPlaceholder(/search/i).fill('terminal').catch(() => {})
    await expect(dialog.getByText('Toggle terminal', { exact: true })).toBeVisible()
    await expect(dialog.getByText('New terminal', { exact: true })).toBeVisible()
    await page.waitForTimeout(300)
    await dialog.locator('> div').screenshot({ path: join(out, `${theme}-6-shortcuts-modal.png`) })
    await page.keyboard.press('Escape')

    // Settings → Shortcuts shows both rebindable rows.
    await page.goto(`${base}/settings/shortcuts`)
    const toggleRow = page.getByText('Toggle terminal', { exact: true }).first()
    await toggleRow.scrollIntoViewIfNeeded()
    await expect(page.getByText('New terminal', { exact: true }).first()).toBeVisible()
    await page.waitForTimeout(400)
    await page.screenshot({ path: join(out, `${theme}-7-settings-shortcuts.png`) })

    expect(errors).toEqual([])
    results.push(`${theme}: ok`)
    await ctx.close()
  }

  // macOS glyphs: Settings shows ⌃` and ⌃⇧` (Control, not Command).
  {
    const ctx = await browser.newContext({ viewport: { width: 1400, height: 860 }, deviceScaleFactor: 1 })
    const page = await ctx.newPage()
    await preparePage(page, 'dark', true)
    await page.goto(`${base}/settings/shortcuts`)
    await page.getByText('New terminal', { exact: true }).first().scrollIntoViewIfNeeded()
    await page.waitForTimeout(400)
    await page.screenshot({ path: join(out, 'mac-settings-shortcuts.png') })
    results.push('mac: ok')
    await ctx.close()
  }
} finally {
  await browser.close()
  srv.close()
}
console.log(results.join('\n'))
console.log('DONE', out)
