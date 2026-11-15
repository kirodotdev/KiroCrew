/**
 * Screenshot harness: the chat tag picker opens with a focused search box that
 * filters the tag list, and Enter on a name no tag has creates that tag and
 * assigns it to the chat.
 *
 * Drives a session row's right-click menu → "Tags…" on the REAL built SPA
 * behind the shared loopback static server, every /api/** call answered from
 * fixtures (gateway-free). The tag stubs are STATEFUL: a create appends to the
 * tag list and a PUT records the slot's tags, so the refetches that follow show
 * what the server would.
 *
 * MODE=after (default) captures the open picker, the arrow-key highlight, a
 * filtered search that highlights the first match the chat does not have, the
 * arrow moved to a tag the chat has, a new name offering Create, an exact
 * match before and after Enter applied it, the state after Enter
 * created and assigned a tag, the empty state, and a refused create offline.
 * MODE=before captures only the open picker, for a build without the search box
 * (pass that build's dist as DIST). THEME picks dark (default) or light.
 *
 * Usage: DIST=<dist dir> MODE=after THEME=dark node scripts/capture-tag-picker-search.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/tag-picker-search'
const MODE = process.env.MODE || 'after'
const THEME = process.env.THEME || 'dark'
const SLOT = 's1'
mkdirSync(OUT, { recursive: true })

function assert(label, ok, detail = '') {
  console.log(`${label}: ${ok ? 'OK' : 'FAIL'}${detail ? ` — ${detail}` : ''}`)
  if (!ok) throw new Error(`${label} failed${detail ? `: ${detail}` : ''}`)
}

const now = Math.floor(Date.now() / 1000)
const tags = [
  { id: 't-planned', name: 'Planned', color: '#8b5cf6', order: 0, status: true },
  { id: 't-progress', name: 'In progress', color: '#3b82f6', order: 1, status: true },
  { id: 't-review', name: 'Review', color: '#f59e0b', order: 2, status: true },
  { id: 't-done', name: 'Done', color: '#22c55e', order: 3, status: true },
  { id: 't-bug', name: 'Bug', color: '#ef4444', order: 4 },
  { id: 't-frontend', name: 'Frontend', color: '#06b6d4', order: 5 },
  { id: 't-docs', name: 'Docs', color: '#a3a3a3', order: 6 },
  { id: 't-release', name: 'Release notes', color: '#ec4899', order: 7 },
]
const slots = [
  {
    key: SLOT, title: 'Fix the flaky upload test', running: false, messages: 2,
    agent: 'kirocrew', modified: now, last_ts: '2026-11-05T09:00:00Z', folder_id: '',
    last_message: 'Found the race in the retry loop.', tags: ['t-progress', 't-bug'],
    tags_revision: '0000000000000001.00000000000000000001-aa',
  },
  {
    key: 's2', title: 'Draft the release notes', running: false, messages: 2,
    agent: 'kirocrew', modified: now - 600, last_ts: '2026-11-05T08:50:00Z', folder_id: '',
    last_message: 'Grouped the entries by area.', tags: ['t-release'],
  },
]
const detail = {
  running: false, has_more: false, total: 2, queue: [],
  messages: [
    { role: 'user', content: 'The upload test fails about one run in ten.', ts: now - 120 },
    { role: 'assistant', content: 'Found the race in the retry loop.', ts: now - 60 },
  ],
}

const creates = []
const puts = []
let seq = 1
const extra = (path, route) => {
  const method = route.request().method()
  if (path === '/api/chat/tags' && method === 'POST') {
    const body = JSON.parse(route.request().postData() || '{}')
    creates.push(body)
    const row = { id: `t-new-${creates.length}`, name: body.name, color: '#64748b', order: tags.length }
    tags.push(row)
    return json(route, row, 201), true
  }
  if (path === '/api/chat/tags') return json(route, tags), true
  if (path === '/api/chat/tag-columns') return json(route, []), true
  if (path === '/api/chat/slots') return json(route, slots), true
  const m = path.match(/^\/api\/chat\/slots\/([^/]+)\/tags$/)
  if (m && method === 'PUT') {
    const body = JSON.parse(route.request().postData() || '{}')
    puts.push({ slot: decodeURIComponent(m[1]), tags: body.tags })
    const s = slots.find(x => x.key === decodeURIComponent(m[1]))
    const prior = s?.tags_revision ?? null
    seq += 1
    const revision = `0000000000000001.${String(seq).padStart(20, '0')}-aa`
    if (s) { s.tags = body.tags; s.tags_revision = revision }
    return json(route, { ok: true, tags: body.tags, tags_revision: revision, prior_tags_revision: prior }), true
  }
  if (path.startsWith('/api/chat/slots/')) return json(route, detail), true
  return false
}

/** Clip around the picker dialog, padded. */
async function pickerClip(page) {
  const b = await page.getByTestId('slot-tag-picker').boundingBox()
  if (!b) throw new Error('picker not on screen')
  const pad = 28
  return { x: Math.max(0, b.x - pad), y: Math.max(0, b.y - pad), width: b.width + pad * 2, height: b.height + pad * 2 }
}

async function shoot(page, name) {
  await page.waitForTimeout(250)
  await page.screenshot({ path: `${OUT}/${name}-${THEME}.png`, clip: await pickerClip(page) })
  console.log(`wrote ${name}-${THEME}.png`)
}

/** Names of the tag rows, leaving out the Create row (an option too). */
const rowNames = async page =>
  (await page.getByTestId('slot-tag-picker').locator('[role="option"]:not([data-testid="slot-tag-create"])').allInnerTexts()).map(t => t.trim())

/**
 * A fresh page on the same build with its own tag list, for the empty and
 * offline shots. Its POST stub records every create and refuses it, so a test
 * can prove that no create went out. `goOffline` closes the dashboard's socket
 * and every later reconnect, which puts the page in its disconnected state.
 */
async function openScene(browser, base, tagList) {
  const context = await browser.newContext({ viewport: { width: 1400, height: 860 }, deviceScaleFactor: 2 })
  const page = await context.newPage()
  logPageProblems(page)
  const sceneSlots = slots.map(s => ({ ...s, tags: s.tags.filter(id => tagList.some(t => t.id === id)) }))
  const posts = []
  const sockets = []
  let closed = false
  const sceneExtra = (path, route) => {
    const method = route.request().method()
    if (path === '/api/chat/tags' && method === 'POST') {
      posts.push(route.request().postData())
      return json(route, { error: 'no create expected in this scene' }, 500), true
    }
    if (path === '/api/chat/tags') return json(route, tagList), true
    if (path === '/api/chat/tag-columns') return json(route, []), true
    if (path === '/api/chat/slots') return json(route, sceneSlots), true
    if (path.startsWith('/api/chat/slots/')) return json(route, detail), true
    return false
  }
  await stubDashboardApi(page, {
    slots: sceneSlots, theme: THEME, extra: sceneExtra,
    onWebSocket: ws => { sockets.push(ws); if (closed) ws.close() },
    localStorageEntries: { 'mc-active-slot': SLOT, 'mc-privacy-notice-v1': '1', 'mc-sidebar-pinned': 'true' },
  })
  await page.goto(base + '/chat', { waitUntil: 'domcontentloaded' })
  const row = page.locator(`[data-slot-key="${SLOT}"]`).first()
  await row.waitFor({ state: 'visible', timeout: 15_000 })
  await page.waitForTimeout(1500)
  await row.click({ button: 'right' })
  await page.getByRole('menuitem', { name: /^Tags/ }).click()
  const picker = page.getByTestId('slot-tag-picker')
  await picker.waitFor({ state: 'visible', timeout: 5000 })
  await page.waitForTimeout(300)
  const goOffline = async () => {
    closed = true
    for (const s of sockets) s.close()
    await page.waitForTimeout(1000)
  }
  return { page, context, picker, posts, goOffline }
}

async function main() {
  const { srv, base } = await serveDist(process.env.DIST || undefined)
  const browser = await chromium.launch()
  const context = await browser.newContext({ viewport: { width: 1400, height: 860 }, deviceScaleFactor: 2 })
  const page = await context.newPage()
  logPageProblems(page)
  await stubDashboardApi(page, {
    slots, theme: THEME, extra,
    localStorageEntries: { 'mc-active-slot': SLOT, 'mc-privacy-notice-v1': '1', 'mc-sidebar-pinned': 'true' },
  })
  try {
    await page.goto(base + '/chat', { waitUntil: 'domcontentloaded' })
    const row = page.locator(`[data-slot-key="${SLOT}"]`).first()
    await row.waitFor({ state: 'visible', timeout: 15_000 })
    await page.waitForTimeout(1500)

    // Open: right-click the row → Tags…
    await row.click({ button: 'right' })
    const item = page.getByRole('menuitem', { name: /^Tags/ })
    await item.waitFor({ state: 'visible', timeout: 5000 })
    await item.click()
    const picker = page.getByTestId('slot-tag-picker')
    await picker.waitFor({ state: 'visible', timeout: 5000 })
    await page.getByText('Release notes', { exact: true }).last().waitFor({ state: 'visible', timeout: 5000 })

    if (MODE === 'before') {
      await shoot(page, '00-before-open')
      return
    }

    const search = picker.getByPlaceholder('Find or create a tag…')
    // The row the search box names in aria-activedescendant, by its visible text.
    const highlighted = () => search.evaluate(el => {
      const id = el.getAttribute('aria-activedescendant')
      return id ? (document.getElementById(id)?.textContent ?? '').trim() : null
    })
    const focused = () => search.evaluate(el => el === document.activeElement)
    await page.waitForTimeout(200)
    assert('open: the search box has focus', await focused())
    assert('open: every tag is listed', (await rowNames(page)).length === 8, JSON.stringify(await rowNames(page)))
    assert('open: nothing is highlighted', (await highlighted()) === null)
    await shoot(page, '01-open-search-focused')

    // Arrows with an empty search: the highlight walks the list, focus stays put.
    await page.keyboard.press('ArrowDown')
    await page.keyboard.press('ArrowDown')
    assert('arrows: the second row is highlighted', (await highlighted()) === 'In progress', String(await highlighted()))
    assert('arrows: focus stayed in the search box', await focused())
    await shoot(page, '02-arrows-highlight-a-row')
    await page.keyboard.press('Escape')
    await picker.waitFor({ state: 'hidden', timeout: 5000 })
    await row.click({ button: 'right' })
    await page.getByRole('menuitem', { name: /^Tags/ }).click()
    await picker.waitFor({ state: 'visible', timeout: 5000 })
    await page.waitForTimeout(200)

    // Partial match: filters the list and highlights the first matching tag the
    // chat does not have, so Enter adds it rather than create a tag named "re".
    // The chat already has In progress, so the highlight skips it.
    await page.keyboard.type('re')
    const filtered = await rowNames(page)
    assert('filter "re": only matching tags remain', JSON.stringify(filtered) === JSON.stringify(['In progress', 'Review', 'Release notes']), JSON.stringify(filtered))
    assert('filter "re": the first match the chat lacks is highlighted', (await highlighted()) === 'Review', String(await highlighted()))
    await shoot(page, '03-partial-filter')
    // Moving to a tag the chat has puts the Enter hint beside its checkmark,
    // and Enter there removes it.
    await page.keyboard.press('ArrowUp')
    assert('filter "re": one ArrowUp reaches In progress', (await highlighted()) === 'In progress', String(await highlighted()))
    await shoot(page, '04-partial-filter-arrow-to-in-progress')

    await search.fill('flaky')
    assert('new name: no tag rows match', (await rowNames(page)).length === 0)
    assert('new name: Create row reads the typed name', (await picker.getByTestId('slot-tag-create').innerText()).includes('Create “flaky”'))
    await shoot(page, '05-new-name-offers-create')

    // Exact match: no Create row; Enter applies the existing tag.
    await search.fill('review')
    assert('exact match: no Create row', (await picker.getByTestId('slot-tag-create').count()) === 0)
    assert('exact match: Review is highlighted', (await highlighted()) === 'Review')
    await shoot(page, '06-exact-match-highlighted')
    await page.keyboard.press('Enter')
    const review = picker.getByRole('option', { name: 'Review', exact: true })
    await page.waitForTimeout(300)
    assert('exact match: Enter added Review', puts.length === 1 && JSON.stringify(puts[0].tags) === JSON.stringify(['t-progress', 't-bug', 't-review']), JSON.stringify(puts))
    assert('exact match: Review is checked', (await review.getAttribute('aria-selected')) === 'true')
    assert('exact match: search cleared', (await search.inputValue()) === '')
    // The full name of a tag the chat already has highlights nothing, and
    // Enter changes nothing.
    await search.fill('review')
    assert('exact match again: nothing is highlighted', (await highlighted()) === null, String(await highlighted()))
    await page.keyboard.press('Enter')
    await page.waitForTimeout(300)
    assert('exact match again: no write, Review stays on', puts.length === 1 && (await review.getAttribute('aria-selected')) === 'true', JSON.stringify(puts))
    assert('exact match again: the typed text stays', (await search.inputValue()) === 'review')
    await shoot(page, '07-exact-match-enter-applied')

    // Enter on a new name: creates and assigns, search clears.
    await search.fill('flaky')
    await page.keyboard.press('Enter')
    const created = picker.getByRole('option', { name: 'flaky', exact: true })
    await created.waitFor({ state: 'visible', timeout: 5000 })
    await page.waitForTimeout(300)
    assert('enter: one create request named flaky', creates.length === 1 && creates[0].name === 'flaky', JSON.stringify(creates))
    assert('enter: the new tag was assigned to the chat', puts.length === 2 && JSON.stringify(puts[1].tags) === JSON.stringify(['t-progress', 't-bug', 't-review', 't-new-1']), JSON.stringify(puts))
    assert('enter: the new row is checked', (await created.getAttribute('aria-selected')) === 'true')
    assert('enter: search cleared', (await search.inputValue()) === '')
    await shoot(page, '08-after-enter-created-and-assigned')

    // No tags at all: the empty state tells the user to type a name.
    const empty = await openScene(browser, base, [])
    try {
      assert('empty: the empty-state text shows', await empty.picker.getByText('No tags yet. Type a name to create one.').isVisible())
      assert('empty: no rows', (await rowNames(empty.page)).length === 0)
      await shoot(empty.page, '09-empty-no-tags')
    } finally { await empty.context.close() }

    // Offline: search still filters, the Create row is dimmed, and Enter sends
    // no create and keeps the typed text.
    const offline = await openScene(browser, base, tags.filter(t => !t.id.startsWith('t-new-')))
    try {
      await offline.goOffline()
      const offlineSearch = offline.picker.getByPlaceholder('Find or create a tag…')
      await offlineSearch.fill('flaky')
      const create = offline.picker.getByTestId('slot-tag-create')
      assert('offline: the Create row is dimmed', ((await create.getAttribute('class')) ?? '').includes('opacity-40'), String(await create.getAttribute('class')))
      assert('offline: no row carries the Enter hint', (await offline.picker.locator('svg.lucide-corner-down-left').count()) === 0)
      await offlineSearch.press('Enter')
      await offline.page.waitForTimeout(500)
      assert('offline: Enter sent no create', offline.posts.length === 0, JSON.stringify(offline.posts))
      assert('offline: the typed text stays', (await offlineSearch.inputValue()) === 'flaky')
      await shoot(offline.page, '10-offline-create-refused')
    } finally { await offline.context.close() }
    console.log(`\nOK — screenshots in ${OUT}`)
  } finally {
    await browser.close()
    srv.close()
  }
}

main().catch(err => { console.error(err); process.exit(1) })
