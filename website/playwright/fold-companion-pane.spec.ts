import { createServer, type Server } from 'node:http'
import type { AddressInfo } from 'node:net'
import { test, expect, type Page, type APIRequestContext } from '@playwright/test'
import { withFold, withoutFold } from './helpers/fold'

/**
 * Book pose with the side panel (docs/request-for-change/rfc-foldable-viewport-segments.md,
 * "Book pose with the side panel open"), under an emulated fold.
 *
 * Panel closed: the sessions list holds the leading segment and the transcript
 * the trailing one. Panel open: the transcript holds the leading segment, the
 * panel the trailing one, and the sessions list is off screen. Each pane sits
 * inside one segment and none crosses the fold, and a stored bottom dock is
 * drawn at the right without being rewritten.
 */

const SCROLLER = '.chat-container'
const PANEL = '[data-testid="side-panel-column"]'
// The panel itself: the column also holds any fold gap before it.
const PANEL_BODY = `${PANEL} > :first-child`
const SESSIONS = '.kc-vt-sessions'
// The iPhone Duo's inner display in book pose, in a Safari tab (CDP takes a
// whole-pixel fold offset, so the fold sits at 475 rather than 475.5), and a
// device that reports a 20px masked hinge between its segments.
const FOLDS = [
  { name: 'segments that meet', viewport: { width: 951, height: 537 }, offset: 475, mask: 0, rtl: false },
  { name: 'a masked hinge', viewport: { width: 900, height: 600 }, offset: 440, mask: 20, rtl: false },
  // Right to left the leading segment is the right-hand one.
  { name: 'right to left', viewport: { width: 951, height: 537 }, offset: 475, mask: 0, rtl: true },
]
// Sub-pixel layout: a pane may end a fraction of a pixel past its segment.
const EPS = 1

interface Box { left: number; right: number }

async function importChat(request: APIRequestContext): Promise<string> {
  const messages = Array.from({ length: 6 }, (_, i) => ({
    role: i % 2 ? 'assistant' : 'user',
    content: i % 2 ? `Reply ${i}: the diff touches two files.` : `Question ${i}: what changed?`,
    ts: new Date(Date.UTC(2026, 0, 1, 0, 0, i)).toISOString(),
  }))
  const imported = await request.post('/api/chat/slots/import', {
    data: { bundle_version: 1, title: 'Fold companion pane', origin: 'playwright', agent: '', messages },
  })
  expect(imported.status(), `session import failed: ${await imported.text()}`).toBeLessThan(300)
  return ((await imported.json()) as { key: string }).key
}

async function segments(page: Page): Promise<Box[]> {
  return page.evaluate(() => {
    const segs = (window as Window & { viewport?: { segments?: ArrayLike<DOMRect> | null } }).viewport?.segments
    return segs ? Array.from(segs, s => ({ left: s.x, right: s.x + s.width })) : []
  })
}

async function box(page: Page, selector: string): Promise<Box | null> {
  // A query, not a locator: a locator waits for an element that is gone.
  return page.evaluate((sel) => {
    const el = document.querySelector(sel)
    const r = el?.getBoundingClientRect()
    return r && r.width > 0 ? { left: r.left, right: r.right } : null
  }, selector)
}

function inside(pane: Box, seg: Box): boolean {
  return pane.left >= seg.left - EPS && pane.right <= seg.right + EPS
}

function straddles(pane: Box, foldX: number): boolean {
  return pane.left < foldX - EPS && pane.right > foldX + EPS
}

async function togglePanel(page: Page) {
  await page.evaluate(() => window.dispatchEvent(new Event('toggle-activity-panel')))
}

test.describe('Side panel in book pose', () => {
  let key = ''

  test.afterEach(async ({ request }) => {
    if (key) await request.delete(`/api/chat/slots/${encodeURIComponent(key)}`)
    key = ''
  })

  for (const fold of FOLDS) {
    test(`the open panel takes the trailing segment and the transcript the leading one (${fold.name})`, async ({ page, request }) => {
      key = await importChat(request)
      await page.setViewportSize(fold.viewport)
      // A stored bottom dock: the fold draws it at the right and leaves it stored.
      await page.addInitScript(() => { localStorage.setItem('mc-side-panel-dock', 'bottom') })
      await page.goto(`/chat/fold-companion?sid=${encodeURIComponent(key)}`, { waitUntil: 'domcontentloaded' })
      const skip = await withFold(page, { orientation: 'vertical', offset: fold.offset, maskLength: fold.mask })
      test.skip(skip !== null, skip ?? '')
      await expect(page.locator(SCROLLER)).toBeVisible({ timeout: 15_000 })
      // The page sets no direction of its own: the document's `dir` decides,
      // and the resize makes the layout read it again.
      if (fold.rtl) await page.evaluate(() => { document.documentElement.setAttribute('dir', 'rtl'); window.dispatchEvent(new Event('resize')) })

      const [left, right] = await segments(page)
      expect(left && right, 'two segments are reported').toBeTruthy()
      const [lead, trail] = fold.rtl ? [right, left] : [left, right]
      const foldX = left.right
      const foldEnd = right.left

      // Panel closed: the chat page split. The list animates from its stored
      // width to the fold once the fold is reported, so this waits for the
      // list's fold-side edge to reach the fold before reading the transcript.
      const listAtFold = (b: Box | null) => b !== null && inside(b, lead)
        && Math.abs((fold.rtl ? b.left : b.right) - (fold.rtl ? foldEnd : foldX)) <= EPS
      await expect.poll(async () => listAtFold(await box(page, SESSIONS)), { timeout: 10_000 }).toBe(true)
      // The chat page split pads the transcript past a hinge gap only in a
      // later phase, so with a masked hinge its start is checked there.
      if (fold.mask === 0) {
        await expect.poll(async () => {
          const t = await box(page, SCROLLER)
          return t !== null && inside(t, trail)
        }, { timeout: 10_000 }).toBe(true)
      }

      // Panel open: the companion layout.
      await togglePanel(page)
      await expect(page.locator(PANEL)).toBeVisible({ timeout: 10_000 })
      await expect.poll(async () => {
        const t = await box(page, SCROLLER)
        return t !== null && inside(t, lead)
      }, { timeout: 10_000 }).toBe(true)
      await expect.poll(() => box(page, SESSIONS), { timeout: 10_000 }).toBeNull()
      const transcript = (await box(page, SCROLLER))!
      const panel = (await box(page, PANEL_BODY))!
      expect(inside(panel, trail), `panel ${JSON.stringify(panel)} in ${JSON.stringify(trail)}`).toBe(true)
      const panelFoldEdge = fold.rtl ? Math.abs(panel.right - foldX) : Math.abs(panel.left - foldEnd)
      expect(panelFoldEdge, 'the panel starts where the fold ends').toBeLessThanOrEqual(EPS)
      for (const [name, pane] of [['transcript', transcript], ['panel', panel]] as const) {
        expect(straddles(pane, foldX) || straddles(pane, foldEnd), `${name} ${JSON.stringify(pane)} crosses the fold ${foldX}..${foldEnd}`).toBe(false)
      }
      expect(await page.evaluate(() => localStorage.getItem('mc-side-panel-dock'))).toBe('bottom')

      // Panel closed again: the list comes back and the transcript returns.
      await togglePanel(page)
      await expect(page.locator(PANEL)).toHaveCount(0, { timeout: 10_000 })
      await expect.poll(() => box(page, SESSIONS), { timeout: 10_000 }).not.toBeNull()
      if (fold.mask === 0) {
        await expect.poll(async () => {
          const t = await box(page, SCROLLER)
          return t !== null && inside(t, trail)
        }, { timeout: 10_000 }).toBe(true)
      }
    })
  }
})

/**
 * A touch-first phone browser: the phone form unfolded at any width, the
 * desktop form beside a fold. The fold flips the form at the same width, and
 * the side panel must keep its host through it: a remount would drop every
 * live frame in the panel, and what the user typed or drew there with it.
 */
test.describe('Side panel frames across a fold on a touch-first phone', () => {
  test.use({
    viewport: { width: 951, height: 537 },
    isMobile: true,
    hasTouch: true,
    userAgent: 'Mozilla/5.0 (Linux; Android 14; Fold) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Mobile Safari/537.36',
  })
  // One at a time: each run adds its own origin to the shared config.
  test.describe.configure({ mode: 'serial' })
  let key = ''
  let server: Server | null = null
  let added = ''

  async function origins(request: APIRequestContext): Promise<string[]> {
    const config = await (await request.get('/api/config/kirocrew')).json() as { dashboard?: { browser_local_origins?: unknown } }
    const list = config.dashboard?.browser_local_origins
    return Array.isArray(list) ? list.filter((o): o is string => typeof o === 'string') : []
  }

  test.afterEach(async ({ request }) => {
    if (key) await request.delete(`/api/chat/slots/${encodeURIComponent(key)}`)
    key = ''
    if (added) {
      const rest = (await origins(request)).filter(o => o !== added)
      await request.patch('/api/config/kirocrew', { data: { path: 'dashboard.browser_local_origins', value: rest } })
    }
    added = ''
    await new Promise<void>(resolve => (server ? server.close(() => resolve()) : resolve()))
    server = null
  })

  test('a typed value in a preview frame survives folding and unfolding, in the same frame', async ({ page, request }) => {
    test.setTimeout(60_000)
    // A local page with a field, served for the preview and allowed for it.
    server = createServer((_req, res) => {
      res.writeHead(200, { 'Content-Type': 'text/html' })
      res.end('<!doctype html><title>Field</title><input id="field" aria-label="Field">')
    })
    await new Promise<void>(resolve => server!.listen(0, '127.0.0.1', resolve))
    const origin = `127.0.0.1:${(server.address() as AddressInfo).port}`
    const patched = await request.patch('/api/config/kirocrew', { data: { path: 'dashboard.browser_local_origins', value: [...(await origins(request)), origin] } })
    expect(patched.status(), `allowing the preview origin failed: ${await patched.text()}`).toBeLessThan(300)
    added = origin
    key = await importChat(request)
    await page.goto(`/chat/fold-frames?sid=${encodeURIComponent(key)}`, { waitUntil: 'domcontentloaded' })
    await expect(page.locator(SCROLLER)).toBeVisible({ timeout: 15_000 })
    const phone = await page.evaluate(() => document.documentElement.dataset.layoutForm)
    test.skip(phone !== 'phone', `this browser does not take the touch-first phone form (layout form: ${phone})`)

    await togglePanel(page)
    await page.getByRole('button', { name: 'Open side panel tab' }).first().click()
    await page.getByRole('menuitem', { name: /^Preview/ }).first().click()
    const url = page.getByLabel('Preview URL')
    await url.fill(origin)
    await url.press('Enter')
    const frameEl = page.locator('iframe[title="Web preview"]')
    await expect(frameEl).toBeVisible({ timeout: 10_000 })
    const field = page.frameLocator('iframe[title="Web preview"]').locator('#field')
    await field.fill('drawn before the fold')
    // The frame's node and its window, kept on the page to compare later.
    await page.evaluate(() => {
      const el = document.querySelector('iframe[title="Web preview"]') as HTMLIFrameElement
      Object.assign(window, { __foldFrame: el, __foldFrameWin: el.contentWindow })
    })
    const sameFrame = () => page.evaluate(() => {
      const el = document.querySelector('iframe[title="Web preview"]') as HTMLIFrameElement | null
      const w = window as Window & { __foldFrame?: Element; __foldFrameWin?: Window | null }
      return !!el && el === w.__foldFrame && el.isConnected && el.contentWindow === w.__foldFrameWin
    })

    const skip = await withFold(page, { orientation: 'vertical', offset: 475, maskLength: 0 })
    test.skip(skip !== null, skip ?? '')
    await expect.poll(() => page.evaluate(() => document.documentElement.dataset.layoutForm), { timeout: 10_000 }).toBe('desktop')
    // Folded: the panel sits in the trailing segment beside the transcript.
    const [, trail] = await segments(page)
    await expect.poll(async () => {
      const b = await box(page, '[data-testid="side-panel-inline"]')
      return b !== null && inside(b, trail)
    }, { timeout: 10_000 }).toBe(true)
    expect(await sameFrame(), 'the fold kept the same frame node and window').toBe(true)
    await expect(field).toHaveValue('drawn before the fold')

    // Unfolded: the phone form again, the same frame, the value still there.
    expect(await withoutFold(page)).toBeNull()
    await expect.poll(() => page.evaluate(() => document.documentElement.dataset.layoutForm), { timeout: 10_000 }).toBe('phone')
    expect(await sameFrame(), 'the unfold kept the same frame node and window').toBe(true)
    await expect(field).toHaveValue('drawn before the fold')
  })
})
