/**
 * Capture Settings → Chat → Selectable Models for the harness model-picker PR.
 * The list now titles each row on the backend's advertised display name
 * (model.displayName) instead of the raw id, with the id kept as the stored
 * value. This renders the REAL SettingsPage/ChatPanel against the real theme;
 * every /api/ call is answered from the fixtures below, so no gateway is used.
 * It also captures the in-chat model picker (the composer's model chip with the
 * picker open) from capture/chat-model-picker-native-names.html, dark and light.
 *
 * Usage: node scripts/capture-harness-model-picker.mjs [base-url] [out-dir] [shot-name]
 * Needs a Vite dev server serving website/ (e.g. `npx vite --host 127.0.0.1 --port 5199`).
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const base = process.argv[2] || 'http://127.0.0.1:5199'
const out = process.argv[3] || '../temp-screenshots/harness-model-picker'
mkdirSync(out, { recursive: true })

// What Claude Code's ACP bridge advertises: bare families carry a current name,
// versioned ids carry their own. This is the exact shape GET /api/models returns.
const claudeModels = [
  { model_name: 'auto', description: 'Always visible in chat model pickers' },
  { model_name: 'opus', display_name: 'Opus 5.5', description: 'For complex work and everyday tasks' },
  { model_name: 'fable', display_name: 'Fable 5.1', description: 'For your toughest challenges' },
  { model_name: 'sonnet', display_name: 'Sonnet 5.5', description: 'Most efficient for simpler tasks' },
  { model_name: 'claude-opus-5', display_name: 'Opus 5', description: 'Best for everyday, complex tasks' },
  { model_name: 'claude-sonnet-5', display_name: 'Sonnet 5', description: 'Efficient for routine tasks' },
  { model_name: 'claude-fable-5', display_name: 'Fable 5', description: 'Most capable for your hardest tasks' },
  { model_name: 'claude-mythos-5-1', display_name: 'Mythos 5.1', description: '' },
  { model_name: 'claude-haiku-4-5-20251001', display_name: 'Haiku 4.5', description: 'Fastest for quick answers' },
]

const codexModels = [
  { model_name: 'auto', description: 'Always visible in chat model pickers' },
  { model_name: 'gpt-6.1-sol', display_name: '6.1 Sol', description: 'Latest workhorse model for coding and everyday work.' },
  { model_name: 'gpt-6-astra', display_name: '6 Astra', description: 'Frontier intelligence for the most demanding work.' },
  { model_name: 'gpt-6-luna', display_name: '6 Luna', description: 'Fast and affordable model for easier tasks.' },
  { model_name: 'gpt-daybreak-blue-latest', display_name: 'Daybreak Blue', description: 'Latest frontier agentic coding model.' },
]

const json = (route, body, code = 200) => route.fulfill({
  status: code, contentType: 'application/json', body: JSON.stringify(body),
})

const shots = [
  { name: 'selectable-models-claude', theme: 'dark', models: claudeModels },
  { name: 'selectable-models-claude-light', theme: 'light', models: claudeModels },
  { name: 'selectable-models-codex', theme: 'dark', models: codexModels },
]

const browser = await chromium.launch()
try {
  for (const s of shots) {
    if (process.argv[4] && process.argv[4] !== s.name) continue
    const page = await browser.newPage({ viewport: { width: 1360, height: 960 }, deviceScaleFactor: 1.4 })
    const errors = []
    page.on('pageerror', e => errors.push(String(e)))
    await page.route(url => new URL(url).pathname.startsWith('/api/'), route => {
      const p = new URL(route.request().url()).pathname
      if (p === '/api/models') return json(route, s.models)
      if (p === '/api/config/kirocrew') return json(route, { agent: { model: 'auto', reasoning_effort: '' }, dashboard: {} })
      if (p === '/api/config/schema') return json(route, { entries: [] })
      if (p === '/api/dashboard/config') return json(route, { model_picker_hidden_models: [], model_picker_configured: false, verbosity: 'default', default_memory_mode: 'persistent', widget_density: 'more' })
      if (p === '/api/agents') return json(route, { agents: [], default_agent: 'default' })
      if (p === '/api/agents/resolved-model') return json(route, { model: '', pinned: false })
      if (p === '/api/apps') return json(route, [])
      if (p === '/api/tips/status') return json(route, { enabled_config: true, opted_out: false, cadence_hours: 24 })
      if (p === '/api/feature-videos/status') return json(route, { enabled: false })
      return json(route, {})
    })
    // Navigate straight to the Chat panel's Model subtab (query param, not a path).
    await page.goto(`${base}/capture/settings-agent-harness.html?theme=${s.theme}&path=${encodeURIComponent('/settings?tab=chat&sub=models')}`)
    // Selectable Models is a Radix Popover on that subtab. Scroll to its label,
    // click the trigger right after it, then shoot the open list so the row
    // labels (the native names) are visible.
    const label = page.getByText('Selectable Models', { exact: true }).first()
    await label.waitFor({ timeout: 20000 })
    await label.scrollIntoViewIfNeeded()
    // The Selectable Models trigger shows the summary "All models (N)". Click it
    // to open the popover, not the Default Model dropdown above it.
    const trigger = page.getByRole('button', { name: /All models/ }).first()
    await trigger.click({ timeout: 8000 }).catch(async () => {
      await page.getByText(/All models \(\d+\)/).first().click({ timeout: 4000 }).catch(() => {})
    })
    const marker = s.name.includes('codex') ? '6.1 Sol' : 'Opus 5.5'
    await page.getByText(marker, { exact: false }).first().waitFor({ timeout: 10000 }).catch(() => {})
    await page.waitForTimeout(500)
    await page.screenshot({ path: `${out}/${s.name}.png`, animations: 'disabled' })
    console.log(`${s.name}: captured${errors.length ? ` (page errors: ${errors.join(' | ')})` : ''}`)
    await page.close()
  }
  // The in-chat picker: the composer's model chip with the picker open beside it,
  // listing the backend's own names (Opus 5.5, ...). Isolated capture page, no /api mocks.
  for (const { name, theme } of [{ name: 'in-chat-picker-claude', theme: 'dark' }, { name: 'in-chat-picker-claude-light', theme: 'light' }]) {
    if (process.argv[4] && process.argv[4] !== name) continue
    const page = await browser.newPage({ viewport: { width: 1360, height: 960 }, deviceScaleFactor: 1.4 })
    const errors = []
    page.on('pageerror', e => errors.push(String(e)))
    await page.goto(`${base}/capture/chat-model-picker-native-names.html?theme=${theme}`)
    await page.getByText('Opus 5.5', { exact: false }).first().waitFor({ state: 'visible', timeout: 10000 })
    await page.screenshot({ path: `${out}/${name}.png`, animations: 'disabled' })
    console.log(`${name}: captured${errors.length ? ` (page errors: ${errors.join(' | ')})` : ''}`)
    await page.close()
  }
} finally {
  await browser.close()
}
