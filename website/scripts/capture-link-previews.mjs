/**
 * Screenshot harness for link previews (inline chip + block card).
 *
 * Runs the REAL built SPA (website/dist) against a static file server with every
 * /api/** call intercepted by Playwright and answered from fixtures. No gateway,
 * no credential, and — importantly for this feature — NO outbound network: the
 * `/api/link-meta` responses are stubbed, so capturing evidence never actually
 * fetches google.com or amazon.com from this machine.
 *
 * The client code under test is unmodified; only the network is stubbed. That
 * makes the gating observable rather than assumed: the harness COUNTS calls to
 * /api/link-meta, so "nothing is fetched while the toggle is off" and "nothing
 * is fetched while the message is still streaming" are asserted from the wire,
 * not inferred from the pixels.
 *
 * Usage: node scripts/capture-link-previews.mjs <baseUrl> <outDir>
 */
import { chromium } from 'playwright'
import { mkdirSync, readFileSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6802'
const OUT = process.argv[3] || '../temp-screenshots/link-previews'
const SLOT = 'chat-linkprev'
const PROJECT = '/Users/diwm/workspace/KiroCrew'

mkdirSync(OUT, { recursive: true })

/**
 * The REAL favicon bytes each site serves, captured once into a text fixture.
 *
 * The endpoint under test inlines the bytes it fetched verbatim
 * (`build_icon_data_uri` = upstream content-type + base64 of the raw body), so
 * the icon a user sees is the site's own icon at whatever size it ships. An
 * earlier version of this harness generated flat colour squares instead, and
 * the screenshots read as though the feature reduced every favicon to a solid
 * block — the fixture has to carry real icons or the evidence lies.
 */
const FAVICONS = JSON.parse(
  readFileSync(new URL('./fixtures/link-preview-favicons.json', import.meta.url), 'utf8'),
).icons
const ICON = {
  amazon: FAVICONS.amazon.dataUri,
  google: FAVICONS.google.dataUri,
  github: FAVICONS.github.dataUri,
}

/** Keyed by the exact URL the model wrote, mirroring the endpoint's JSON. */
const META = {
  'https://www.amazon.com': {
    url: 'https://www.amazon.com',
    title: 'Amazon.com. Spend less. Smile more.',
    description: 'Free shipping on millions of items. Get the best of shopping and entertainment with Prime.',
    site_name: 'Amazon',
    domain: 'amazon.com',
    icon: ICON.amazon,
    fetched_at: Date.now() / 1000,
  },
  'https://www.google.com': {
    url: 'https://www.google.com',
    title: 'Google',
    description: 'Search the world’s information, including webpages, images and videos.',
    site_name: 'Google',
    domain: 'google.com',
    icon: ICON.google,
    fetched_at: Date.now() / 1000,
  },
  'https://github.com/kirodotdev/KiroCrew': {
    url: 'https://github.com/kirodotdev/KiroCrew',
    title: 'kirodotdev/KiroCrew: an autonomous agent management layer',
    description: 'Persistent memory, scheduled jobs, background subagents, self-learning and multi-session orchestration.',
    site_name: 'GitHub',
    domain: 'github.com',
    icon: ICON.github,
    fetched_at: Date.now() / 1000,
  },
  // No icon at all: the favicon box must hold its space rather than reflowing
  // the title, and no broken-image glyph may appear.
  'https://example.org/rfc/9110': {
    url: 'https://example.org/rfc/9110',
    title: 'RFC 9110: HTTP Semantics',
    description: 'This document describes the overall architecture of HTTP, establishes common terminology, and defines aspects of the protocol.',
    site_name: '',
    domain: 'example.org',
    icon: '',
    fetched_at: Date.now() / 1000,
  },
}

const CONTENT = [
  'Two of the big ones: shopping is [Amazon](https://www.amazon.com) and search is',
  '[Google](https://www.google.com) — both linked inline, mid-sentence.',
  '',
  'https://github.com/kirodotdev/KiroCrew',
  '',
  'A link with no favicon available, still inline: https://example.org/rfc/9110 — the',
  'icon box holds its width so the title does not shift.',
  '',
  'Never unfurled: `artifact://abcd1234`, a local path `/Users/diwm/notes.md`, and an',
  'in-app route [the artifact](/artifacts/link-unfurl-notes).',
].join('\n')

const STREAMING_CONTENT = 'Still typing the URL: https://www.goo'

const slots = [{
  key: SLOT,
  title: 'Link previews',
  running: false,
  last_message: 'Two of the big ones…',
  messages: 2,
  agent: 'kirocrew',
  memory_mode: 'persistent',
  project: PROJECT,
  modified: Math.floor(Date.now() / 1000),
  source_links: [],
  source_links_total: 0,
}]

const detail = (assistant, running = false, role = 'assistant') => ({
  running,
  has_more: false,
  total: 2,
  queue: [],
  project: PROJECT,
  messages: [
    { role: 'user', ts: Date.now() / 1000 - 600, content: '给我显示 google 和 amazon 的链接' },
    { role, ts: Date.now() / 1000 - 20, content: assistant },
  ],
})

/** Flipped per scenario. */
const scene = {
  theme: 'dark',
  linkPreviews: true,
  metaStatus: 200,
  assistant: CONTENT,
  running: false,
  role: 'assistant',
  metaCalls: 0,
}

const json = (route, body, status = 200) => route.fulfill({
  status, contentType: 'application/json', body: JSON.stringify(body),
})

/**
 * Boot-time endpoints the app shell iterates before it will render anything.
 *
 * A table rather than a chain of `if (path === …) return json(…)`: the chain
 * form is byte-for-byte the same boilerplate every capture harness needs, and
 * the repo's jscpd gate runs at a 0% duplication threshold, so the second
 * harness to copy it fails the build. Values are thunks because a few of them
 * read `scene`, which changes between scenarios.
 */
const BOOT_STUBS = {
  '/api/status': () => ({ sessions: 1, crons: 0, lessons: 0, uptime: 120, version: 'dev' }),
  '/api/notifications': () => ({ notifications: [], unread: 0 }),
  '/api/auth/me': () => ({ user: 'owner', app: '' }),
  '/api/models': () => ({ models: [], default: 'auto' }),
  '/api/themes': () => ({ themes: [], installed: [] }),
  '/api/theme/boot': () => ({ mode: scene.theme, theme: '' }),
  '/api/dashboard/branding': () => ({ bot_name: 'Kiro', avatar: '' }),
  '/api/recent-projects': () => ({ dirs: [PROJECT] }),
  '/api/chat/nav/resolve-links': () => ({ summaries: [] }),
}

async function main() {
  const browser = await chromium.launch()
  const context = await browser.newContext({
    viewport: { width: 1500, height: 950 },
    // Chip/card type is 12–13px; a 1x shot renders it soft.
    deviceScaleFactor: 2,
  })
  const page = await context.newPage()

  await page.routeWebSocket(/\/api\/ws/, () => {})

  await page.route('**/api/**', async route => {
    const url = new URL(route.request().url())
    const path = url.pathname
    if (path === '/api/link-meta') {
      scene.metaCalls += 1
      const target = url.searchParams.get('url') || ''
      if (scene.metaStatus !== 200) return json(route, { code: 'fetch_failed' }, scene.metaStatus)
      const hit = META[target]
      if (!hit) return json(route, { code: 'fetch_failed' }, 502)
      return json(route, hit)
    }
    if (path === '/api/dashboard/config') {
      return json(route, {
        restore_sessions: true, merge_queued_messages: false, widget_density: 'more',
        verbosity: 'default', quick_send: false, session_grid: false,
        tail_fork_enabled: false, link_previews: scene.linkPreviews,
      })
    }
    if (path === '/api/chat/slots') return json(route, slots)
    if (path.startsWith('/api/chat/slots/')) return json(route, detail(scene.assistant, scene.running, scene.role))
    // KiroPrerequisiteGate reads status.operation.status on boot; an array-shaped
    // stub throws inside the app-shell ErrorBoundary and nothing renders at all.
    if (path === '/api/kiro-prerequisite') return json(route, {
      platform: 'darwin', installed: true, authenticated: true, ready: true,
      initial_setup_complete: true, can_auto_install: false, can_login: false,
      repair_required: false, docs_url: '', setup_allowed: true,
      operation: { kind: 'none', status: 'idle', message: '' },
    })
    if (path.startsWith('/api/instances')) return json(route, { instances: [], active: '' })
    const boot = BOOT_STUBS[path]
    if (boot) return json(route, boot())
    const objectish = /(config|tips|voice|autonudge|branding|status|usage-summary)/.test(path)
    if (objectish) return json(route, {})
    return json(route, [])
  })

  page.on('response', r => {
    if (r.status() >= 400) console.log('HTTP', r.status(), new URL(r.url()).pathname)
  })
  page.on('pageerror', err => console.log('PAGEERROR:', String(err).slice(0, 300)))
  page.on('console', msg => {
    if (msg.type() === 'error') console.log('CONSOLE:', msg.text().slice(0, 300))
  })

  async function load(route = '/') {
    await page.addInitScript(t => {
      localStorage.clear()
      localStorage.setItem('mc-theme', t)
      localStorage.setItem('mc-onboarded', '1')
      localStorage.setItem('mc-active-slot', 'chat-linkprev')
    }, scene.theme)
    await page.goto(BASE + route, { waitUntil: 'domcontentloaded' })
    await page.waitForTimeout(3000)
  }

  async function shot(name) {
    await page.screenshot({ path: `${OUT}/${name}.png` })
    console.log('wrote', `${OUT}/${name}.png`, '| link-meta calls so far:', scene.metaCalls)
  }

  /** Tight crop on the assistant message body — the whole story for this feature. */
  async function crop(name) {
    // Anchor the crop on the message's own first line rather than a container
    // class: the transcript wrapper carries no stable hook, and a missed
    // selector silently degrades to a full-window shot that reads as evidence.
    const anchor = page.getByText('Two of the big ones', { exact: false }).first()
    const alt = page.getByText('Still typing the URL', { exact: false }).first()
    const target = await anchor.count() ? anchor : alt
    const box = await target.count() ? await target.boundingBox() : null
    if (box) {
      const x = Math.max(0, box.x - 28)
      await page.screenshot({
        path: `${OUT}/${name}.png`,
        clip: { x, y: Math.max(0, box.y - 28), width: Math.min(1500 - x, 1080), height: 300 },
      })
      console.log('wrote', `${OUT}/${name}.png`, '(crop)')
      return
    }
    await shot(name)
  }

  // 1. Toggle ON, dark — chips inline, card for the standalone link.
  scene.linkPreviews = true
  await load()
  await shot('01-on-dark')
  await crop('02-on-dark-crop')
  const onCalls = scene.metaCalls
  console.log('ASSERT enabled fetched:', onCalls > 0 ? 'PASS' : 'FAIL', `(${onCalls} calls)`)

  // 2. Toggle OFF — every link stays the anchor it is today, and NOTHING is
  //    fetched. The call count is the evidence, not the pixels.
  scene.linkPreviews = false
  scene.metaCalls = 0
  await load()
  await shot('03-off-dark')
  await crop('04-off-dark-crop')
  console.log('ASSERT disabled fetched nothing:', scene.metaCalls === 0 ? 'PASS' : `FAIL (${scene.metaCalls})`)

  // 3. Endpoint failing (blocked host, timeout, non-HTML) degrades to plain
  //    anchors rather than an error state in the transcript.
  scene.linkPreviews = true
  scene.metaStatus = 502
  scene.metaCalls = 0
  await load()
  await crop('05-endpoint-failed-crop')
  console.log('ASSERT failure degrades:', scene.metaCalls > 0 ? 'PASS (tried, fell back)' : 'FAIL (never tried)')

  // 4. Still streaming: a half-typed URL must not be fetched.
  scene.metaStatus = 200
  scene.assistant = STREAMING_CONTENT
  scene.running = true
  // ChatPage derives isStreaming from `m.role === 'streaming'`, not from the
  // slot's running flag -- a fixture using role 'assistant' would render the
  // completed state and wrongly show the gate letting a fetch through.
  scene.role = 'streaming'
  scene.metaCalls = 0
  await load()
  await crop('06-streaming-crop')
  console.log('ASSERT streaming fetched nothing:', scene.metaCalls === 0 ? 'PASS' : `FAIL (${scene.metaCalls})`)

  // 5. Light-theme parity.
  scene.assistant = CONTENT
  scene.running = false
  scene.role = 'assistant'
  scene.theme = 'light'
  scene.metaCalls = 0
  await load()
  await shot('07-on-light')
  await crop('08-on-light-crop')

  // 6. What Quote / Ask / Copy actually receive for an unfurled link. This drives
  //    the REAL app path — a live selection, the selection toolbar, its Copy
  //    action — rather than reading `getSelection().toString()`, because that raw
  //    string is exactly what is wrong: it carries the fetched title and no URL.
  //    The rule being verified is that the quoted text is the URL the MODEL
  //    wrote.
  scene.theme = 'dark'
  scene.metaCalls = 0
  await load()
  await page.evaluate(() => {
    window.__copied = []
    Object.defineProperty(navigator, 'clipboard', {
      configurable: true,
      value: { writeText: async (t) => { window.__copied.push(t) } },
    })
  })
  // Drag with the real mouse. Verified against the alternatives: a synthetic
  // Range + dispatched mouseup leaves the toolbar at the origin (it anchors to
  // the last mouse point), and neither triple-click nor double-click produces a
  // selection here at all — only a genuine press-move-release does.
  // Target the paragraph that actually holds a chip. Matching on the sentence
  // text instead picks the session-list card in the sidebar, whose `last_message`
  // preview starts with the same words — dragging there selects nothing.
  const para = page.locator('p:has(a[data-unfurl-url])').first()
  const pbox = await para.boundingBox()
  if (!pbox) throw new Error('selection target not found')
  const lineY = pbox.y + 8
  await page.mouse.move(pbox.x + 5, lineY)
  await page.mouse.down()
  await page.mouse.move(pbox.x + pbox.width * 0.8, lineY, { steps: 20 })
  await page.mouse.up()
  await page.waitForTimeout(600)
  const rawSelection = await page.evaluate(() => window.getSelection()?.toString() ?? '')
  if (!rawSelection.trim()) throw new Error('selection gesture selected nothing — cannot verify')

  // Scope to the selection toolbar. A bare name='Copy' also matches the message
  // footer's "Copy link to message", which copies a chat permalink — clicking
  // that would make this scenario pass for entirely the wrong reason.
  const bar = page.getByRole('button', { name: 'Quote' }).first().locator('..')
  const copyBtn = bar.getByRole('button', { name: 'Copy', exact: true })
  if (!(await copyBtn.count())) throw new Error('selection toolbar Copy not found')
  const cbox = await copyBtn.boundingBox()
  if (cbox) {
    const x = Math.max(0, cbox.x - 340)
    await page.screenshot({
      path: `${OUT}/13-inline-selection-toolbar.png`,
      clip: { x, y: Math.max(0, cbox.y - 30), width: Math.min(1500 - x, 820), height: 150 },
    })
    console.log('wrote', `${OUT}/13-inline-selection-toolbar.png`, '(crop)')
  }
  await copyBtn.click()
  await page.waitForTimeout(300)
  const copied = await page.evaluate(() => window.__copied.join('\n'))
  console.log('SELECTION raw browser text:', JSON.stringify(rawSelection))
  console.log('SELECTION what Copy handed over:', JSON.stringify(copied))
  const urls = copied.match(/https?:\/\/\S+/g) || []
  const modelUrls = urls.filter((u) => !u.includes('127.0.0.1'))
  console.log(
    'ASSERT quoted text carries the model-written URL:',
    modelUrls.length ? `PASS (${modelUrls.join(' ')})` : 'FAIL — no model URL in the text handed to Copy/Quote',
  )
  console.log(
    'ASSERT fetched title is not what gets quoted:',
    copied.includes('Spend less') ? 'FAIL — title leaked into the quote' : 'PASS',
  )

  // 6b. The same rule for a selection spanning a BLOCK CARD, which the earlier
  //     round never actually exercised — the drag above covers only inline chips,
  //     and that gap hid two defects: a card's rendered text carries newlines its
  //     textContent does not, so the old title-matching substitution never fired
  //     for it at all; and the card's real paragraph breaks were absorbed as if
  //     they were the spurious ones a chip injects. Both are only visible in a
  //     real browser, so they are asserted here rather than in jsdom.
  await page.evaluate(() => { window.__copied = []; window.getSelection()?.removeAllRanges() })
  await page.waitForTimeout(300)
  const endPara = page.getByText('icon box holds its width', { exact: false }).first()
  await endPara.scrollIntoViewIfNeeded()
  await page.waitForTimeout(400)
  // Re-measure BOTH ends after the scroll. Reusing the box measured before it
  // put the press outside the transcript, and the drag then selected nothing.
  const startBox = await para.boundingBox()
  const ebox = await endPara.boundingBox()
  if (!startBox || !ebox) throw new Error('cross-card selection targets not found')
  await page.mouse.move(startBox.x + 5, startBox.y + 8)
  await page.mouse.down()
  await page.mouse.move(ebox.x + ebox.width * 0.5, ebox.y + 8, { steps: 30 })
  await page.mouse.up()
  await page.waitForTimeout(600)
  const rawSpan = await page.evaluate(() => window.getSelection()?.toString() ?? '')
  if (!rawSpan.trim()) throw new Error('cross-card selection selected nothing')
  const bar2 = page.getByRole('button', { name: 'Quote' }).first().locator('..')
  const copy2 = bar2.getByRole('button', { name: 'Copy', exact: true })
  await copy2.click()
  await page.waitForTimeout(300)
  const spanned = await page.evaluate(() => window.__copied.join('\n'))
  console.log('CROSS-CARD raw browser text:', JSON.stringify(rawSpan))
  console.log('CROSS-CARD what Copy handed over:', JSON.stringify(spanned))
  const wantUrls = ['https://www.amazon.com', 'https://www.google.com', 'https://github.com/kirodotdev/KiroCrew']
  const missing = wantUrls.filter((u) => !spanned.includes(u))
  console.log(
    'ASSERT every link in the span became its model-written URL:',
    missing.length ? `FAIL — missing ${missing.join(' ')}` : 'PASS (all 3)',
  )
  console.log(
    'ASSERT the card keeps its paragraph break:',
    /\n\s*\n\s*https:\/\/github\.com/.test(spanned)
      ? 'PASS'
      : 'FAIL — the block card was merged into the surrounding paragraph',
  )

  // 6c. The chip's own copy button — the affordance that restores what raw URL
  //     text always allowed. Scoped to the chip's container so this cannot pass
  //     by clicking the card's button or the message footer's.
  await page.evaluate(() => { window.__copied = [] })
  const chipCopy = page.locator('p:has(a[data-unfurl-url]) button[aria-label^="Copy URL of"]').first()
  if (!(await chipCopy.count())) throw new Error('chip copy button not found')
  await chipCopy.click()
  await page.waitForTimeout(300)
  const chipCopied = await page.evaluate(() => window.__copied.join('\n'))
  console.log('CHIP BUTTON handed over:', JSON.stringify(chipCopied))
  console.log(
    'ASSERT the chip copy button yields the raw URL:',
    /^https?:\/\/\S+$/.test(chipCopied.trim()) ? 'PASS' : 'FAIL — not a bare URL',
  )
  const chipBox = await chipCopy.boundingBox()
  if (chipBox) {
    const x = Math.max(0, chipBox.x - 300)
    await page.screenshot({
      path: `${OUT}/14-chip-copy-button.png`,
      clip: { x, y: Math.max(0, chipBox.y - 24), width: Math.min(1500 - x, 700), height: 90 },
    })
    console.log('wrote', `${OUT}/14-chip-copy-button.png`, '(crop)')
  }

  // 6d. A selection confined INSIDE a chip's title. `cloneContents()` reveals
  //     only descendants, so this range holds no unfurl element and the fast path
  //     used to hand back the fetched title. Dragged rather than double-clicked:
  //     only a genuine press-move-release produces a selection in this app.
  await page.evaluate(() => { window.__copied = []; window.getSelection()?.removeAllRanges() })
  await page.waitForTimeout(300)
  const titleSpan = page.locator('p:has(a[data-unfurl-url]) a[data-unfurl-url] span.truncate').first()
  const tbox = await titleSpan.boundingBox()
  if (!tbox) throw new Error('chip title span not found')
  const midY = tbox.y + tbox.height / 2
  // Double-click, not drag: an <a> is draggable by default, so pressing inside a
  // chip and moving starts an HTML5 link drag instead of extending a selection.
  // Double-clicking a word is the gesture actually available on a link.
  await page.mouse.dblclick(tbox.x + 12, midY)
  await page.waitForTimeout(600)
  let insideRaw = await page.evaluate(() => window.getSelection()?.toString() ?? '')
  if (!insideRaw.trim()) {
    await page.mouse.move(tbox.x + 3, midY)
    await page.mouse.down()
    await page.mouse.move(tbox.x + Math.min(60, tbox.width - 4), midY, { steps: 12 })
    await page.mouse.up()
    await page.waitForTimeout(600)
    insideRaw = await page.evaluate(() => window.getSelection()?.toString() ?? '')
  }
  console.log('INSIDE-CHIP raw browser text:', JSON.stringify(insideRaw))
  const bar3 = page.getByRole('button', { name: 'Quote' }).first().locator('..')
  const copy3 = bar3.getByRole('button', { name: 'Copy', exact: true })
  if (!insideRaw.trim() || !(await copy3.count())) {
    // Not a failure of the fix: no gesture in this app produces a selection that
    // stays inside the anchor, so the path is reachable programmatically (and by
    // engines that allow it) rather than by mouse here. The unit tests CASE E/F
    // lock the behaviour; this only records that the browser could not reproduce it.
    console.log('NOTE inside-chip selection not reachable by mouse in this app —',
      'no selection or no toolbar; covered by unit tests CASE E/F instead')
  } else {
    await copy3.click()
    await page.waitForTimeout(300)
    const insideCopied = await page.evaluate(() => window.__copied.join('\n'))
    console.log('INSIDE-CHIP what Copy handed over:', JSON.stringify(insideCopied))
    console.log(
      'ASSERT a selection inside a chip still yields the URL:',
      /^https?:\/\/\S+$/.test(insideCopied.trim())
        ? 'PASS'
        : 'FAIL — handed over the fetched title instead of the URL',
    )
  }

  // 7. The Settings toggle itself, including the copy that states the tradeoff.
  //    Navigated by clicking, not by URL: the static file server has no SPA
  //    fallback, so a direct GET /settings returns the server's own 404 page.
  scene.theme = 'dark'
  await load()
  await page.getByRole('button', { name: /^Settings$/ }).first().click()
  await page.waitForTimeout(1200)
  const chatTab = page.getByRole('button', { name: /^Chat$/ }).first()
  if (await chatTab.count()) { await chatTab.click(); await page.waitForTimeout(900) }
  const toggle = page.getByText('Link Previews', { exact: false }).first()
  if (await toggle.count()) {
    await toggle.scrollIntoViewIfNeeded()
    await page.waitForTimeout(400)
    const box = await toggle.boundingBox()
    if (box) {
      const x = Math.max(0, box.x - 40)
      await page.screenshot({
        path: `${OUT}/09-settings-toggle.png`,
        clip: { x, y: Math.max(0, box.y - 60), width: Math.min(1500 - x, 1000), height: 220 },
      })
      console.log('wrote', `${OUT}/09-settings-toggle.png`, '(crop)')
    }
  } else {
    console.log('ASSERT settings toggle visible: FAIL (copy not found)')
  }
  await shot('10-settings-full')

  await browser.close()
}

main().catch(err => { console.error(err); process.exit(1) })
