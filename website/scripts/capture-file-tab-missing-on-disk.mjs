/**
 * Screenshot harness for "a file tab whose file is gone keeps the last copy on
 * screen under a banner" (PR #14262, round 4; issue #14236).
 *
 * The side panel re-reads a file tab when the tab becomes visible or its stream
 * reports a change. When that read answers 404 the tab used to swap the
 * document for a "file not found" placeholder, hiding a buffer that may be the
 * only copy left of a file deleted outside the dashboard. Now the document
 * stays, and a banner over it says what it is and what to do with it.
 *
 * Two frames, because a lone English frame does not prove the banner comes
 * from the catalog: frame 1 is the tab in English, frame 2 the same tab in
 * Chinese.
 *
 * Runs the REAL built SPA (website/dist) behind the shared loopback static
 * server with every /api/** call answered from fixtures: no gateway, no
 * dashboard token. The file tab is seeded through the tab store's own
 * localStorage bucket; `/api/file-read` answers the hydration with the body and
 * every later read with 404 (the file is deleted meanwhile); the tab's
 * `/api/file-watch` stream delivers one frame with a change, which is what
 * makes the panel read the disk again and find the file gone.
 *
 * Usage: node scripts/capture-file-tab-missing-on-disk.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { join } from 'node:path'

import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/file-tab-missing-on-disk'
const ACTIVE = 'file-tab-missing'
const FILE = '/home/dev/notes/release-checklist.md'
const BODY = [
  '# Release checklist',
  '',
  '- [x] Bump the version in `pyproject.toml`',
  '- [x] Regenerate the changelog',
  '- [ ] Tag the release',
  '- [ ] Publish the wheel',
  '',
  'Keep this list in step with the pipeline stages; the tag step runs only after the changelog is reviewed.',
  '',
].join('\n')

mkdirSync(OUT, { recursive: true })

const now = Math.floor(Date.now() / 1000)

const SLOTS = [{
  key: ACTIVE,
  title: 'Review the release checklist',
  running: false,
  messages: 2,
  agent: 'kirocrew',
  memory_mode: 'persistent',
  modified: now,
  last_ts: '2026-09-26T21:00:00Z',
  folder_id: '',
  last_message: 'Opened it in the side panel.',
  source_links: [],
  source_links_total: 0,
}]

const detail = {
  running: false,
  has_more: false,
  total: 2,
  queue: [],
  messages: [
    { role: 'user', ts: now - 40, content: `Open \`${FILE}\` so I can go through it.` },
    { role: 'assistant', ts: now - 30, content: 'Opened it in the side panel.' },
  ],
}

/** Route the two file endpoints per page: the first read answers with the
 *  body (the hydration that opens the tab), every later one 404s -- the file
 *  was deleted outside the dashboard meanwhile. The stream delivers ONE frame
 *  with a change, then ends; `useFileWatch` closes a stream that errors, so
 *  there is no reconnect loop to feed. */
function fileRoutes({ allMissing = false, partialSeed = false, quietStream = false, failAfter = null } = {}) {
  let reads = 0
  let streams = 0
  return async (path, route) => {
    if (path === '/api/chat/slots') { await json(route, SLOTS); return true }
    if (path.startsWith('/api/chat/slots/')) { await json(route, detail); return true }
    if (path === '/api/file-read') {
      reads++
      if (failAfter != null && reads > failAfter) {
        // The gateway can no longer read the file (not a 404): what the
        // close-time check meets when the backend is down.
        await route.fulfill({ status: 500, contentType: 'application/json', body: JSON.stringify({ error: 'read failed' }) })
      } else if ((reads === 1 || (failAfter != null && reads <= failAfter)) && !allMissing) {
        // `partialSeed`: the opening read was cut at the gateway's cap, so the
        // tab carries a partial copy from the start (`X-Truncated`).
        await route.fulfill({ status: 200, headers: { 'content-type': 'text/markdown; charset=utf-8', 'x-file-binary': 'false', ...(partialSeed ? { 'x-truncated': 'true' } : {}) }, body: BODY })
      } else {
        await route.fulfill({ status: 404, contentType: 'application/json', body: JSON.stringify({ error: 'not found' }) })
      }
      return true
    }
    if (path === '/api/file-watch') {
      streams++
      if (streams === 1 && !quietStream) {
        await route.fulfill({
          status: 200,
          headers: { 'content-type': 'text/event-stream', 'cache-control': 'no-cache' },
          body: `data: ${JSON.stringify({ content: BODY + '\n- [ ] Announce\n', mtime: now })}\n\n`,
        })
      } else {
        await route.abort()
      }
      return true
    }
    if (path === '/api/file-diff') { await json(route, { diff: '', original: BODY, status: 'clean' }); return true }
    return false
  }
}

async function captureLocale(browser, base, lang, expectBanner, expectActions, name, confirmName, confirmTitle, denyClipboard = false, dirtyPass = false, opts = {}) {
  // opts.routes: fileRoutes options for this pass. opts.closeOnly: no banner is
  // expected -- go straight to Escape and capture the dialog (title
  // `confirmTitle`, body `expectBanner`). opts.seedDirty: restore the tab with
  // unsaved edits. opts.downloadName: click the banner's Download and assert
  // the browser is handed this file name. opts.afterConfirm === false: stop at
  // the confirm frame (no Cancel / copy / narrow-layout frames).
  const seedDirty = dirtyPass || !!opts.seedDirty
  const context = await browser.newContext({
    viewport: { width: 1500, height: 950 },
    // The banner text is 12px; 1x renders it soft on GitHub.
    deviceScaleFactor: 2,
  })
  const page = await context.newPage()
  if (denyClipboard) {
    // No clipboard API and a refusing execCommand: what a plain-HTTP or
    // sandboxed document sees, and the case the copy button must report.
    await page.addInitScript(() => {
      Object.defineProperty(navigator, 'clipboard', { configurable: true, value: undefined })
      document.execCommand = () => false
    })
  }
  logPageProblems(page)
  await stubDashboardApi(page, {
    theme: 'dark',
    slots: SLOTS,
    extra: fileRoutes({ allMissing: dirtyPass, ...(opts.routes ?? {}) }),
    localStorageEntries: {
      'mc-lang': lang,
      'mc-active-slot': ACTIVE,
      'mc-privacy-notice-v1': '1',
      'mc-sidebar-pinned': 'true',
      [`mc-panel-tabs:${ACTIVE}`]: JSON.stringify({
        activeId: `file:${FILE}`,
        tabs: [{
          id: `file:${FILE}`, kind: 'file', title: 'release-checklist.md', path: FILE,
          ...(seedDirty ? { content: BODY + '\n- [ ] Announce\n', savedContent: BODY } : {}),
        }],
      }),
      [`mc-activity-open:${ACTIVE}`]: 'true',
    },
  })
  await page.goto(base + '/chat', { waitUntil: 'domcontentloaded' })
  if (dirtyPass || opts.closeOnly) {
    // The tab was restored with unsaved edits (its stored content is ahead of
    // its saved baseline) and its file is gone. A dirty tab reads nothing on
    // its own, so the panel first learns the file is gone from the close-time
    // check, and the prompt names the file AND the edits. `closeOnly` passes
    // use the same door for the other close-time verdict: a read that FAILED,
    // which asks "close without checking the disk?" instead of refusing.
    await page.getByRole('heading', { name: 'Release checklist' }).waitFor({ state: 'visible', timeout: 30000 })
    if (seedDirty) await page.locator('[aria-hidden="false"]').filter({ hasText: 'Unsaved changes' }).first().waitFor({ state: 'attached', timeout: 10000 })
    else await page.waitForTimeout(800)
    await page.evaluate(() => { document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true })) })
    const dialog = page.getByRole('dialog')
    await dialog.waitFor({ state: 'visible', timeout: 10000 })
    console.log('dialog text:', JSON.stringify((await dialog.textContent())?.slice(0, 300)))
    await dialog.getByText(confirmTitle, { exact: true }).waitFor({ state: 'visible', timeout: 5000 })
    await dialog.getByText(expectBanner, { exact: true }).waitFor({ state: 'visible', timeout: 5000 })
    await dialog.getByTestId('markdown-panel-dialog-download').waitFor({ state: 'visible', timeout: 5000 })
    await page.waitForTimeout(300)
    const out = join(OUT, name)
    await page.screenshot({ path: out })
    console.log('wrote', out)
    await context.close()
    return
  }
  // The assertion the frame exists for: the banner, in the locale's own words,
  // OVER the document -- the checklist heading must still be on screen.
  const banner = page.getByTestId('markdown-panel-missing-file')
  await banner.waitFor({ state: 'visible', timeout: 30000 })
  const text = await banner.textContent()
  if (!text || !text.includes(expectBanner)) throw new Error(`banner text ${JSON.stringify(text)} does not carry ${JSON.stringify(expectBanner)}`)
  // The two exits ride IN the banner (Copy content, Download), so the frame
  // shows the way out beside the sentence that names it.
  for (const label of expectActions) {
    await banner.getByRole('button', { name: label, exact: true }).waitFor({ state: 'visible', timeout: 10000 })
  }
  const heading = page.getByRole('heading', { name: 'Release checklist' })
  await heading.waitFor({ state: 'visible', timeout: 10000 })
  await page.waitForTimeout(400)

  // Crop to the side panel: from the tab strip's left edge to the viewport's
  // right edge, full height.
  const strip = await page.locator('.side-panel-strip').first().boundingBox()
  if (!strip) throw new Error('side panel strip not found')
  const clip = { x: Math.max(0, strip.x - 4), y: 0, width: Math.min(1500 - Math.max(0, strip.x - 4), strip.width + 8), height: 950 }
  const out = join(OUT, name)
  await page.screenshot({ path: out, clip })
  console.log('wrote', out)
  const full = join(OUT, name.replace('.png', '-full.png'))
  await page.screenshot({ path: full })
  console.log('wrote', full)
  if (opts.downloadName) {
    // A partial copy goes out under a name that says so: the browser is handed
    // `<file>.partial`, never the file's own name.
    const [download] = await Promise.all([
      page.waitForEvent('download', { timeout: 10000 }),
      banner.getByRole('button', { name: 'Download', exact: true }).click(),
    ])
    const suggested = download.suggestedFilename()
    if (suggested !== opts.downloadName) throw new Error(`download named ${JSON.stringify(suggested)}, expected ${JSON.stringify(opts.downloadName)}`)
    await download.cancel().catch(() => {})
  }
  if (confirmName) {
    // The one exit that discards the copy: Escape (or a close request) opens
    // the last-copy confirmation. The panel's Escape guard listens on the
    // document, so the key is dispatched there -- with nothing focused, a
    // pressed key would go to whatever the page focuses on keypress.
    await page.evaluate(() => { document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true })) })
    const dialog = page.getByRole('dialog')
    try {
      await dialog.waitFor({ state: 'visible', timeout: 10000 })
    } catch (err) {
      const focused = await page.evaluate(() => document.activeElement && document.activeElement.outerHTML.slice(0, 200))
      throw new Error(`no confirm dialog after Escape; focused: ${focused}; ${err.message}`)
    }
    await dialog.getByText(confirmTitle, { exact: true }).waitFor({ state: 'visible', timeout: 5000 })
    // The rescue rides in the dialog: a Download action beside the sentence.
    await dialog.getByTestId('markdown-panel-dialog-download').waitFor({ state: 'visible', timeout: 5000 })
    await page.waitForTimeout(300)
    const confirmOut = join(OUT, confirmName)
    await page.screenshot({ path: confirmOut })
    console.log('wrote', confirmOut)
    if (opts.afterConfirm === false) { await context.close(); return }
    // Decline, then the two copy answers: "Copied" on the button, and the
    // notice a refused clipboard raises (the clipboard is disabled in this
    // context by `denyClipboard`).
    await page.getByRole('dialog').getByRole('button', { name: 'Cancel', exact: true }).click()
    await page.getByRole('dialog').waitFor({ state: 'hidden', timeout: 5000 })
    await banner.getByRole('button', { name: 'Copy content', exact: true }).click()
    if (denyClipboard) {
      await page.getByTestId('markdown-panel-action-error').waitFor({ state: 'visible', timeout: 5000 })
      const notice = await page.getByTestId('markdown-panel-action-error').textContent()
      if (!notice || !notice.includes('Couldn’t copy')) throw new Error(`copy-failed notice ${JSON.stringify(notice)}`)
      const failedOut = join(OUT, confirmName.replace('03b-close', '05-copy-failed').replace('-confirm', ''))
      await page.screenshot({ path: failedOut, clip })
      console.log('wrote', failedOut)
    } else {
      await banner.getByRole('button', { name: 'Copied', exact: true }).waitFor({ state: 'visible', timeout: 3000 })
      const copiedOut = join(OUT, confirmName.replace('03-close', '04-copied').replace('-confirm', ''))
      await page.screenshot({ path: copiedOut, clip })
      console.log('wrote', copiedOut)
      // Narrow full-screen layout: the banner keeps its gutters small and wraps
      // its actions under the sentence, so nothing overflows at 360px.
      await page.setViewportSize({ width: 360, height: 800 })
      await page.getByTestId('markdown-panel-more-options').first().click()
      await page.getByRole('menuitem', { name: 'Full screen', exact: true }).click()
      const fsBanner = page.getByTestId('markdown-panel-missing-file').last()
      await fsBanner.waitFor({ state: 'visible', timeout: 10000 })
      for (const label of expectActions) {
        await fsBanner.getByRole('button', { name: label, exact: true }).waitFor({ state: 'visible', timeout: 5000 })
      }
      const box = await fsBanner.boundingBox()
      if (!box || box.x + box.width > 360 + 1) throw new Error(`narrow banner overflows: ${JSON.stringify(box)}`)
      await page.waitForTimeout(300)
      const narrowOut = join(OUT, confirmName.replace('03-close-last-copy-confirm', '07-missing-banner-narrow-fullscreen'))
      await page.screenshot({ path: narrowOut })
      console.log('wrote', narrowOut)
    }
  }
  await context.close()
}

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  const TITLE = 'Discard the file\u2019s remaining contents?'
  try {
    await captureLocale(browser, base, 'en', 'nothing else holds its contents', ['Copy content', 'Download'], '01-missing-banner-en.png',
      '03-close-last-copy-confirm-en.png', TITLE)
    // Same flow with the clipboard denied: its frames carry their own names so
    // they never overwrite the normal run's.
    await captureLocale(browser, base, 'en', 'nothing else holds its contents', ['Copy content', 'Download'], '01b-missing-banner-en.png',
      '03b-close-last-copy-confirm-en-noclip.png', TITLE, true)
    await captureLocale(browser, base, 'en',
      'release-checklist.md is not on disk and this tab holds unsaved edits; nothing else holds its contents. To keep it, download it first.',
      [], '06-close-dirty-confirm-en.png', undefined, TITLE, false, true)
    await captureLocale(browser, base, 'zh-CN', '再无其他留存', ['复制内容', '下载'], '02-missing-banner-zh-CN.png')
    // A PARTIAL copy: the opening read was cut at the gateway's cap, then the
    // file was deleted. The banner says so, Download is named `.partial`, and
    // the close asks in the partial wording.
    await captureLocale(browser, base, 'en', 'this tab holds only part of it', ['Copy content', 'Download'], '08-missing-banner-partial-en.png',
      '09-close-partial-confirm-en.png', TITLE, false, false,
      { routes: { partialSeed: true, quietStream: true }, downloadName: 'release-checklist.md.partial', afterConfirm: false })
    // The close-time check itself FAILED (the gateway cannot read the file):
    // the panel asks instead of refusing the close -- clean tab, then dirty.
    await captureLocale(browser, base, 'en',
      'release-checklist.md could not be read just now, so this tab may hold the file\u2019s remaining contents. To keep them, download the file first.',
      [], '10-close-unverified-confirm-en.png', undefined, 'Close without checking the disk?', false, false,
      { closeOnly: true, routes: { quietStream: true, failAfter: 2 } })
    await captureLocale(browser, base, 'en',
      'release-checklist.md could not be read just now and this tab holds unsaved edits. To keep them, download the file first.',
      [], '11-close-unverified-dirty-confirm-en.png', undefined, 'Close without checking the disk?', false, false,
      { closeOnly: true, seedDirty: true, routes: { quietStream: true, failAfter: 0 } })
  } finally {
    await browser.close()
    srv.close()
  }
}

await main()
