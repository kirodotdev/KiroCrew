/**
 * Shared page wiring for the split-pane chat capture harnesses: route every
 * /api/** call to the harness's FIXTURES map with the pane-detail fallback,
 * silence the websocket, seed the persisted split layout, and open the app.
 *
 * Extracted from capture-chatpane-queue-edit.mjs when
 * capture-chatpane-upload-error.mjs shipped the identical stanza (the jscpd
 * gate runs at 0% duplication over scripts/).
 *
 * @param context Playwright browser context
 * @param opts.base         app origin to open
 * @param opts.fixtures     path -> body map answered first (after `pre`)
 * @param opts.detailA      slot-detail body for every pane but pane-b
 * @param opts.detailB      slot-detail body for pane-b
 * @param opts.splitLayouts mc-split-layouts object to persist
 * @param opts.json         the harness's json(route, body[, status]) responder
 * @param opts.pre          optional (path, route) handler tried FIRST, for
 *                          harness-specific intercepts (e.g. a 400 upload)
 */
export async function prepareSplitChatPage(context, { base, fixtures, detailA, detailB, splitLayouts, json, pre = null }) {
  const page = await context.newPage()
  await page.routeWebSocket(/\/api\/ws/, () => {})
  await page.route(url => url.pathname.startsWith('/api/'), async route => {
    const path = new URL(route.request().url()).pathname
    if (pre && (await pre(path, route))) return
    if (path in fixtures) return json(route, fixtures[path])
    // Mutations (PATCH/POST/DELETE) get a plain ack -- answering them with a
    // slot-detail body would feed the client a bogus shape mid-interaction.
    if (route.request().method() !== 'GET') return json(route, { ok: true })
    const slotMatch = path.match(/^\/api\/chat\/slots\/([^/]+)/)
    if (slotMatch) return json(route, decodeURIComponent(slotMatch[1]) === 'pane-b' ? detailB : detailA)
    if (path.startsWith('/api/instances')) return json(route, { instances: [], active: '' })
    const objectish = /(config|tips|voice|autonudge|branding|status|usage-summary)/.test(path)
    return json(route, objectish ? {} : [])
  })
  page.on('pageerror', err => console.log('PAGEERROR:', String(err).slice(0, 200)))
  await page.addInitScript((layouts) => {
    localStorage.setItem('mc-theme', 'dark')
    localStorage.setItem('mc-onboarded', '1')
    localStorage.setItem('mc-active-slot', 'pane-a')
    localStorage.setItem('mc-split-layouts', layouts)
  }, JSON.stringify(splitLayouts))
  await page.goto(base + '/', { waitUntil: 'domcontentloaded' })
  return page
}
