/**
 * Screenshot harness + geometry assertion for issue #5707: ChatPane (split
 * view) now surfaces a REFUSED upload at its own composer.
 *
 * Why this file exists rather than a reused ChatPage capture: `api.uploadFiles`
 * resolves `{ paths: [], error }` on a non-2xx instead of throwing, so the
 * refusal is invisible unless the pane reads `res.error`. ChatPage has had a
 * banner for this all along -- at the TOP of the chat column, above the session
 * header -- and ChatPane's is a DIFFERENT surface: inside the pane, directly
 * above that pane's own ChatInput. A capture of ChatPage's banner therefore
 * photographs a surface this change does not ship, and a reviewer approving
 * placement from it would be approving the wrong pixels.
 *
 * So this harness does not merely photograph. It ASSERTS, against the real
 * built SPA (website/dist) with every /api/** call stubbed and no gateway:
 *
 *   1. the banner is a DESCENDANT of a [data-chat-pane] subtree -- i.e. the
 *      in-pane surface, not the page-level column banner;
 *   2. its bottom sits ABOVE the top of that same pane's ChatInput, and within
 *      GAP_MAX px of it, so it reads as attached to the composer;
 *   3. it is horizontally INSIDE that pane's box, which is what distinguishes
 *      an in-pane banner from a full-width one in a screenshot.
 *
 * Exits non-zero if any of those fail, so a future refactor that moves the
 * banner back up to the page level cannot quietly keep this evidence.
 *
 * Nothing in CI runs this file; the CI-enforced half of the behaviour is
 * src/test/ChatPane.uploadError.test.tsx.
 *
 * Usage: node scripts/capture-chatpane-upload-error.mjs [outDir]
 */
import { chromium } from 'playwright'
import { prepareSplitChatPage } from './lib/prepare-split-chat-page.mjs'
import { mkdirSync, writeFileSync, mkdtempSync, rmSync } from 'node:fs'
import { join } from 'node:path'
import { tmpdir } from 'node:os'
import { serveDist } from './lib/serve-dist.mjs'

const OUT = process.argv[2] || '../temp-screenshots/upload-error-banner'
/** Banner bottom -> ChatInput top. The composer carries a small drag gutter, so
 *  a few px is "flush"; a page-level banner would blow this out by hundreds. */
const GAP_MAX = 24

mkdirSync(OUT, { recursive: true })

const now = Math.floor(Date.now() / 1000)
const REFUSAL = 'Unsupported file type: application/x-msdownload'

const slots = [
  {
    key: 'pane-a', title: 'Design notes', running: false,
    last_message: 'Working through phase one...', messages: 2,
    agent: 'kirocrew', memory_mode: 'persistent', modified: now,
  },
  {
    key: 'pane-b', title: 'Release checklist', running: false,
    last_message: 'Summarized the layout options.', messages: 2,
    agent: 'kirocrew', memory_mode: 'persistent', modified: now - 60,
  },
]

const detail = (a, b) => ({
  running: false, has_more: false, total: 2, queue: [],
  messages: [
    { role: 'user', ts: now - 300, content: a, cls: 'msg msg-user' },
    { role: 'assistant', ts: now - 240, content: b, cls: 'msg msg-assistant' },
  ],
})
const detailA = detail('Compare the two layout options.', 'Option A keeps the sidebar fixed; option B collapses it under 900px.')
const detailB = detail('Attach the installer and check it over.', 'Go ahead and drop it in.')

// Persisted split layout anchored at pane-a: ChatPage auto-enters split mode
// when the active slot anchors a live >= 2-member layout.
const splitLayouts = {
  'pane-a': {
    type: 'split', id: 'seed-split', dir: 'row',
    children: [
      { type: 'leaf', id: 'seed-a', kind: 'session', slot: 'pane-a' },
      { type: 'leaf', id: 'seed-b', kind: 'session', slot: 'pane-b' },
    ],
    sizes: [0.5, 0.5],
  },
}

const json = (route, body, status = 200) => route.fulfill({
  status, contentType: 'application/json', body: JSON.stringify(body),
})

const FIXTURES = {
  '/api/chat/slots': slots,
  '/api/kiro-prerequisite': {
    platform: 'linux', installed: true, authenticated: true, ready: true,
    initial_setup_complete: true, can_auto_install: false, can_login: false,
    repair_required: false, docs_url: '', setup_allowed: false,
    operation: { kind: '', status: 'idle', message: '', detail: '', url: '', error: '' },
  },
  // session_grid gates split view (splitFeatureEnabled in ChatPage); without it
  // the persisted layout is ignored and the app stays in single-chat mode.
  '/api/dashboard/config': { session_grid: true },
}

async function preparePage(context) {
  return prepareSplitChatPage(context, {
    base, fixtures: FIXTURES, detailA, detailB, splitLayouts, json,
    // THE POINT OF THIS HARNESS: the server refuses the upload with a non-2xx
    // carrying an `error`. api.uploadFiles turns that into a RESOLVED
    // { paths: [], error } -- never a throw -- which is exactly the shape that
    // used to vanish in this pane.
    pre: async (path, route) => {
      if (path === '/api/upload/file') { await json(route, { error: REFUSAL }, 400); return true }
      return false
    },
  })
}

const { srv, base } = await serveDist()

async function main() {
  const browser = await chromium.launch()
  const context = await browser.newContext({
    viewport: { width: 1500, height: 950 }, deviceScaleFactor: 2,
  })
  const page = await preparePage(context)

  const panes = page.locator('[data-chat-pane]')
  await panes.first().waitFor({ state: 'visible', timeout: 20000 })
  const paneCount = await panes.count()
  console.log('panes:', paneCount)
  if (paneCount < 2) throw new Error(`expected a 2-pane split, got ${paneCount}`)

  // Drive the SECOND pane so the shot shows one pane refusing while its
  // neighbour is untouched -- that contrast is what makes the surface legible.
  const pane = panes.nth(1)
  // An isolated dir per run: a fixed name under tmpdir() would truncate an
  // existing file of that name.
  const fileDir = mkdtempSync(join(tmpdir(), 'chatpane-upload-'))
  const file = join(fileDir, 'setup-tool.exe')
  writeFileSync(file, 'MZ binary placeholder')

  await page.screenshot({ path: `${OUT}/before-01-split-no-banner.png` })

  await pane.locator('input[type="file"]').first().setInputFiles(file)

  const banner = pane.getByText(new RegExp(REFUSAL.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')))
  await banner.waitFor({ state: 'visible', timeout: 15000 })

  // ---- assertion 1: the banner lives INSIDE a chat pane, not at page level ----
  const insidePane = await banner.first().evaluate(el => !!el.closest('[data-chat-pane]'))
  console.log('banner inside [data-chat-pane]:', insidePane)
  if (!insidePane) throw new Error('banner is not inside a ChatPane -- this is the page-level surface')

  // ---- assertions 2 & 3: attached to THAT pane's composer, and within it ----
  const bannerBox = await banner.first().boundingBox()
  const inputBox = await pane.locator('textarea').first().boundingBox()
  const composerBox = await pane.locator('[data-chat-input-shell], form, textarea').first().boundingBox()
  const paneBox = await pane.boundingBox()
  const gap = inputBox.y - (bannerBox.y + bannerBox.height)
  const withinPane = bannerBox.x >= paneBox.x - 1
    && (bannerBox.x + bannerBox.width) <= (paneBox.x + paneBox.width + 1)
  console.log(`banner=${JSON.stringify(bannerBox)}\ncomposer=${JSON.stringify(composerBox)}`)
  console.log(`gap=${gap.toFixed(1)}px withinPaneX=${withinPane} leftDelta=${(bannerBox.x - composerBox.x).toFixed(1)} rightDelta=${((bannerBox.x + bannerBox.width) - (composerBox.x + composerBox.width)).toFixed(1)}`)
  console.log(`pane=${JSON.stringify(paneBox)}`)
  if (gap < 0) throw new Error(`banner overlaps or sits below the composer (gap ${gap})`)
  if (gap > GAP_MAX) throw new Error(`banner is ${gap.toFixed(1)}px from the composer (max ${GAP_MAX}) -- not attached`)
  if (!withinPane) throw new Error('banner extends outside its pane -- reads as a full-width page banner')

  await page.screenshot({ path: `${OUT}/after-01-refused-upload-banner.png` })
  await pane.screenshot({ path: `${OUT}/after-02-pane-closeup.png` })

  // ---- the dismiss control clears it ----
  await pane.getByRole('button', { name: /dismiss/i }).first().click()
  await banner.first().waitFor({ state: 'detached', timeout: 5000 })
  console.log('dismiss cleared the banner')

  await context.close()
  await browser.close()
  srv.close()
  rmSync(fileDir, { recursive: true, force: true })
  console.log('done ->', OUT)
}

main().catch(err => { console.error(err); srv.close(); process.exit(1) })
