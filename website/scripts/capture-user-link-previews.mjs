/**
 * Screenshot harness for USER-message link previews (issue #2580).
 *
 * Builds on scripts/lib/transcript-harness.mjs (real built SPA, stubbed
 * network). What this scene must prove, from the wire not the pixels:
 *
 *  1. toggle ON  — a URL the USER sent unfurls (chip inline, card for a
 *     standalone link), i.e. /api/link-meta IS called for user content.
 *  2. toggle OFF — the same message keeps plain anchors and NOTHING is
 *     fetched (the opt-in stays strict; call count is the assertion).
 *  3. a message that carries a paste chip keeps its span-rendered text —
 *     the URL next to the chip is literal text, never unfurled.
 *
 * Usage: node scripts/capture-user-link-previews.mjs [outDir]
 */
import { mkdirSync } from 'node:fs'
import { openTranscriptHarness } from './lib/transcript-harness.mjs'
import { json } from './lib/boot-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/user-link-previews'
const SLOT = 'chat-userlinkprev'
const PROJECT = '/Users/diwm/workspace/KiroCrew'

mkdirSync(OUT, { recursive: true })

const HREF = 'https://github.com/kirodotdev/KiroCrew'
const META = {
  url: HREF,
  title: 'kirodotdev/KiroCrew: an autonomous agent management layer',
  description: 'Persistent memory, scheduled jobs, background subagents, self-learning and multi-session orchestration.',
  site_name: 'GitHub',
  domain: 'github.com',
  icon: '',
  fetched_at: Date.now() / 1000,
}

/** The user pastes an inline link mid-sentence AND a standalone URL. */
const USER_CONTENT = [
  `Take a look at [the repo](${HREF}) before we start.`,
  '',
  HREF,
].join('\n')

/** Paste-chip message: the chip's text neighbours a URL that must stay text. */
const PASTE_CONTENT = `log excerpt: [ Paste #1 · 4 lines ]\n${HREF}`
const PASTE_META = { pastes: [{ id: 'pb1', seq: 1, lines: 4, content: 'a\nb\nc\nd' }] }

const now = Date.now() / 1000
const slots = [{
  key: SLOT, title: 'User link previews', running: false,
  last_message: 'sounds good', messages: 3, agent: 'kirocrew',
  memory_mode: 'persistent', project: PROJECT, modified: Math.floor(now),
  source_links: [], source_links_total: 0,
}]
const detail = {
  running: false, has_more: false, total: 3, queue: [], project: PROJECT,
  messages: [
    { role: 'user', ts: now - 600, content: USER_CONTENT },
    { role: 'user', ts: now - 300, content: PASTE_CONTENT, meta: PASTE_META },
    { role: 'assistant', ts: now - 20, content: 'Got it — reading the repo now.' },
  ],
}

async function main() {
  const h = await openTranscriptHarness({
    slot: SLOT, project: PROJECT, slots, detail,
    viewport: { width: 1400, height: 950 },
  })

  /** Flipped per scenario; the config route reads it at request time. */
  const scene = { linkPreviews: true, metaCalls: 0 }

  // Registered AFTER the harness's catch-all, so Playwright consults it first;
  // everything not ours falls through to the harness routes.
  await h.page.route('**/api/**', async route => {
    const url = new URL(route.request().url())
    if (url.pathname === '/api/link-meta') {
      scene.metaCalls += 1
      const target = url.searchParams.get('url') || ''
      if (target === HREF) return json(route, META)
      return json(route, { code: 'fetch_failed' }, 502)
    }
    if (url.pathname === '/api/dashboard/config') {
      return json(route, {
        restore_sessions: true, merge_queued_messages: false,
        widget_density: 'more', verbosity: 'default', quick_send: false,
        session_grid: false, tail_fork_enabled: false,
        link_previews: scene.linkPreviews,
      })
    }
    return route.fallback()
  })

  async function shot(name) {
    await h.page.screenshot({ path: `${OUT}/${name}.png` })
    console.log('wrote', `${OUT}/${name}.png`, '| link-meta calls:', scene.metaCalls)
  }

  let failures = 0
  const assert = (label, ok) => {
    console.log('ASSERT', label + ':', ok ? 'PASS' : 'FAIL')
    if (!ok) failures += 1
  }

  // 1. ON — the user's URL unfurls: card title visible, link-meta was called.
  scene.linkPreviews = true
  scene.metaCalls = 0
  await h.load('dark', { settle: 2500 })
  await shot('01-user-previews-on-dark')
  assert('enabled: user URL fetched link-meta', scene.metaCalls > 0)
  const cardTitle = await h.page.getByText(META.title, { exact: false }).count()
  assert('enabled: preview card/chip rendered in user bubble', cardTitle > 0)

  // 2. Paste-chip message on the same load: chip present, its neighbouring
  //    URL is literal text — no anchor pointing at it inside that bubble.
  const chip = await h.page.getByText('Paste #1', { exact: false }).count()
  assert('paste path: chip rendered', chip > 0)

  // 3. OFF — plain anchors, zero fetches.
  scene.linkPreviews = false
  scene.metaCalls = 0
  await h.load('dark', { settle: 2500 })
  await shot('02-user-previews-off-dark')
  assert('disabled: zero link-meta calls', scene.metaCalls === 0)
  const cardOff = await h.page.getByText(META.description, { exact: false }).count()
  assert('disabled: no preview card', cardOff === 0)

  // 4. ON, light theme — evidence for both palettes.
  scene.linkPreviews = true
  scene.metaCalls = 0
  await h.load('light', { settle: 2500 })
  await shot('03-user-previews-on-light')

  await h.close()
  if (failures) {
    console.error(`${failures} assertion(s) failed`)
    process.exit(1)
  }
  console.log('all assertions passed')
}

main().catch(err => { console.error(err); process.exit(1) })
