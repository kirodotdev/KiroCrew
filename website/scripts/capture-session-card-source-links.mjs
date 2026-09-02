/**
 * Screenshot harness for "the session-card PR/issue chips can be switched off"
 * (issue #6574).
 *
 * The evidence has to be a COMPARISON of the two states plus the control that
 * moves between them, because the change is a subtraction: a lone shot of a
 * sidebar without chips is indistinguishable from a session that never mentioned
 * a pull request. So frame 1 is the strip as it ships, frame 2 is the same three
 * rows with the switch off, and frames 3-4 are the Settings row that flips it.
 *
 * The sequence is driven by the REAL control: clicking the toggle issues the same
 * `PUT /api/dashboard/config` the dashboard sends, and this harness's
 * `/api/chat/slots` fixture then applies the SAME rule the server applies
 * (`serialize_slots` -> `to_dict`: `source_links: []`, `source_links_total: 0`),
 * so frame 2 photographs the payload shape production would push rather than a
 * hand-blanked one. The backend half -- that the payload really is gated, that
 * extraction is skipped, and that the credentialed status refresh stops -- is
 * pinned by test/test_session_card_source_links_knob.py, not by a still image.
 *
 * Runs the REAL built SPA (website/dist) behind the shared loopback static server
 * with every /api/** call answered from fixtures: no gateway, no dashboard token,
 * no provider CLI. Only the network and the localStorage seed are stubbed.
 *
 * Usage: node scripts/capture-session-card-source-links.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { join } from 'node:path'

import { serveDist } from './lib/serve-dist.mjs'
import { shotSettingRow } from './lib/settings-row-shot.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/session-card-source-links'
const ACTIVE = 'chat-a'
const REPO = 'https://github.com/kirodotdev/KiroCrew'
const pr = n => `${REPO}/pull/${n}`
const issue = n => `${REPO}/issues/${n}`

mkdirSync(OUT, { recursive: true })

const now = Math.floor(Date.now() / 1000)

/**
 * Chip counts stay within three per KIND: that is the serialized budget a real
 * slot ships (`_SERIALIZED_SOURCE_LINKS_PER_SLOT`), so a row crowded with six
 * pull requests would be a shape production never renders.
 */
const SLOTS = [
  {
    key: ACTIVE, title: 'Draft the release notes', running: false, messages: 4,
    agent: 'kirocrew', modified: now, last_ts: '2026-08-30T00:10:00Z', folder_id: '',
    last_message: 'Grouped the entries by area.',
    source_links: [], source_links_total: 0,
  },
  {
    // The reporter's own case: this session only READ someone else's pull
    // request, and the card advertises it like a workstream.
    key: 'chat-b', title: 'Answer the config question', running: false, messages: 9,
    agent: 'kirocrew', modified: now - 600, last_ts: '2026-08-30T00:00:00Z', folder_id: '',
    last_message: 'Quoted the diff from the other team\'s PR.',
    source_links: [
      { provider: 'github', number: 843, url: pr(843), state: 'open', ci: 'passed', kind: 'change', mergeable: 'mergeable', mergeStateStatus: 'clean' },
      { provider: 'github', number: 6574, url: issue(6574), state: 'open', kind: 'issue' },
    ],
    source_links_total: 2,
  },
  {
    key: 'chat-c', title: 'Sweep the native selects', running: false, messages: 24,
    agent: 'kirocrew', modified: now - 1800, last_ts: '2026-08-29T23:30:00Z', folder_id: '',
    last_message: 'Two of these need a rebase before they can land.',
    source_links: [
      { provider: 'github', number: 846, url: pr(846), state: 'open', ci: 'failed', kind: 'change', mergeable: 'mergeable', mergeStateStatus: 'clean' },
      { provider: 'github', number: 847, url: pr(847), state: 'open', ci: 'running', kind: 'change', mergeable: 'mergeable', mergeStateStatus: 'clean' },
      { provider: 'github', number: 848, url: pr(848), state: 'merged', ci: 'passed', kind: 'change', mergeable: 'mergeable', mergeStateStatus: 'clean' },
    ],
    source_links_total: 5,
  },
]

const DASH_CONFIG = {
  restore_sessions: false,
  restore_window_minutes: 30,
  merge_queued_messages: false,
  widget_density: 'more',
  use_builtin_browser: true,
  verbosity: 'default',
  quick_send: false,
  session_grid: false,
  tail_fork_enabled: false,
  link_previews: false,
  mcp_app_panel: false,
  auto_open_git_panel: false,
  folder_suggestions_enabled: true,
}

const detail = { running: false, has_more: false, total: 0, queue: [], messages: [] }

/** The switch's state, shared by the config route and the slots route. */
let chipsEnabled = true

/**
 * The server's own gate, reproduced: `serialize_slots` resolves the switch once
 * and `to_dict` emits an empty strip for every slot when it is off.
 */
const slotsPayload = () =>
  chipsEnabled
    ? SLOTS
    : SLOTS.map(s => ({ ...s, source_links: [], source_links_total: 0 }))

const extra = async (path, route) => {
  if (path === '/api/dashboard/config') {
    if (route.request().method() === 'PUT') {
      const body = JSON.parse(route.request().postData() || '{}')
      if (typeof body.session_card_source_links === 'boolean') {
        chipsEnabled = body.session_card_source_links
      }
      await json(route, { ok: true })
      return true
    }
    await json(route, { ...DASH_CONFIG, session_card_source_links: chipsEnabled })
    return true
  }
  // Re-answered here rather than through the shared `slots` option so the
  // payload follows the switch on every refetch.
  if (path === '/api/chat/slots') return await json(route, slotsPayload()), true
  if (path.startsWith('/api/chat/slots/')) return await json(route, detail), true
  return false
}

const LABEL = 'PR and Issue Chips on Session Cards'

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  const context = await browser.newContext({
    viewport: { width: 1600, height: 900 },
    // The chips are ~10px glyphs plus a number, illegible in a 1x window shot.
    deviceScaleFactor: 2,
  })
  const page = await context.newPage()
  logPageProblems(page)
  await stubDashboardApi(page, {
    theme: 'dark',
    extra,
    localStorageEntries: {
      'mc-lang': 'en',
      'mc-active-slot': ACTIVE,
      'mc-privacy-notice-v1': '1',
      'mc-sidebar-pinned': 'true',
    },
  })

  const openChat = async () => {
    await page.goto(base + '/chat', { waitUntil: 'domcontentloaded' })
    await page.waitForTimeout(2600)
  }

  /** Crop the three session rows -- the region the whole change lives in. */
  const shotRows = async (name) => {
    const first = page.getByText('Draft the release notes', { exact: true }).first()
    const last = page.getByText('Sweep the native selects', { exact: true }).first()
    await first.waitFor({ state: 'visible', timeout: 20000 })
    const a = await first.boundingBox()
    const b = await last.boundingBox()
    const pad = 22
    const x = Math.max(0, a.x - pad * 3)
    const y = Math.max(0, a.y - pad)
    const clip = {
      x,
      y,
      width: Math.max(a.x + a.width, b.x + b.width) - x + pad * 8,
      height: b.y + b.height - y + pad * 3,
    }
    const out = join(OUT, name)
    await page.screenshot({ path: out, clip })
    console.log('wrote', out, `${Math.round(clip.width)}x${Math.round(clip.height)}`)
  }

  const openChatSettings = async () => {
    await page.goto(base + '/settings?tab=chat', { waitUntil: 'domcontentloaded' })
    await page.waitForTimeout(2500)
    const label = page.getByText(LABEL, { exact: true }).first()
    await label.waitFor({ state: 'visible', timeout: 20000 })
    await label.scrollIntoViewIfNeeded()
    await page.waitForTimeout(400)
    return label
  }

  /** Frame the setting's row from the union of its label and its switch. */
  const shotRow = (name) =>
    shotSettingRow(page, {
      label: page.getByText(LABEL, { exact: true }).first(),
      control: page.getByRole('switch', { name: LABEL }),
      outDir: OUT,
      name,
    })

  // 1 -- the strip as it ships today, on the untouched default.
  await openChat()
  const chipCount = await page.getByText('#843', { exact: true }).count()
  if (chipCount < 1) throw new Error('no PR chip rendered: fixture or selector is wrong')
  await shotRows('01-chips-on.png')

  // 2 -- the Settings row that owns the switch, default on.
  await openChatSettings()
  await shotRow('02-settings-toggle-on.png')

  // 3 -- flipped off through the real control, and read back off.
  await page.getByRole('switch', { name: LABEL }).click()
  await page.waitForTimeout(1200)
  if (chipsEnabled !== false) throw new Error('the toggle did not PUT session_card_source_links=false')
  const pressed = await page.getByRole('switch', { name: LABEL }).getAttribute('aria-checked')
  if (pressed !== 'false') throw new Error(`switch reads aria-checked=${pressed} after turning it off`)
  await shotRow('03-settings-toggle-off.png')

  // 4 -- the same three rows with the payload gated: one row of card, no strip.
  await openChat()
  if (await page.getByText('#843', { exact: true }).count()) {
    throw new Error('a PR chip still rendered while the switch was off')
  }
  await shotRows('04-chips-off.png')

  await browser.close()
  srv.close()
}

await main()
