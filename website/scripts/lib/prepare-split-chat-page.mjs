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
 * @param opts.theme        `mc-theme` to persist before load ('dark' by default)
 * @param opts.pre          optional (path, route) handler tried FIRST, for
 *                          harness-specific intercepts (e.g. a 400 upload)
 */
export async function prepareSplitChatPage(context, { base, fixtures, detailA, detailB, splitLayouts, json, pre = null, theme = 'dark' }) {
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
  await page.addInitScript(({ layouts, theme }) => {
    localStorage.setItem('mc-theme', theme)
    localStorage.setItem('mc-onboarded', '1')
    localStorage.setItem('mc-active-slot', 'pane-a')
    localStorage.setItem('mc-split-layouts', layouts)
  }, { layouts: JSON.stringify(splitLayouts), theme })
  await page.goto(base + '/', { waitUntil: 'domcontentloaded' })
  return page
}

/**
 * The boilerplate every split-pane harness used to re-declare verbatim: the
 * persisted two-pane layout, the JSON route responder, the fixture map the app
 * boots from, and the per-frame assertion logger. Shared by
 * capture-chatpane-selection-quote-ask.mjs, capture-queue-edit-multiline.mjs
 * and (the checker) capture-members-selection-quote-ask.mjs; the jscpd gate
 * runs at 0% duplication over scripts/, so a new harness imports these rather
 * than pasting the stanza.
 */

/** Two session leaves, pane-a over pane-b, split 50/50 under pane-a's key. */
export const TWO_PANE_SPLIT_LAYOUTS = {
  'pane-a': {
    type: 'split', id: 'seed-split', dir: 'col',
    children: [
      { type: 'leaf', id: 'seed-a', kind: 'session', slot: 'pane-a' },
      { type: 'leaf', id: 'seed-b', kind: 'session', slot: 'pane-b' },
    ],
    sizes: [0.5, 0.5],
  },
}

/** Answer a Playwright route with a JSON body. */
export const jsonResponder = (route, body, status = 200) =>
  route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) })

/**
 * The minimum fixture map a split-pane harness boots from: the slot list, a
 * "ready, nothing to install" prerequisite verdict, and the session grid on.
 * Spread it and add harness-specific paths.
 */
export function splitPaneFixtures(slots) {
  return {
    '/api/chat/slots': slots,
    '/api/kiro-prerequisite': {
      platform: 'linux', installed: true, authenticated: true, ready: true,
      initial_setup_complete: true, can_auto_install: false, can_login: false,
      repair_required: false, docs_url: '', setup_allowed: false,
      operation: { kind: '', status: 'idle', message: '', detail: '', url: '', error: '' },
    },
    '/api/dashboard/config': { session_grid: true },
  }
}

/**
 * Per-frame assertion logger. Returns `check(name, ok, detail)`, which prints
 * one OK/MISMATCH line, and `failed()`, true once any check has mismatched, so
 * the harness can exit non-zero after writing every frame it could.
 */
export function makeChecker() {
  let anyFailed = false
  return {
    check(name, ok, detail) {
      console.log(`${name}: ${ok ? 'OK' : 'MISMATCH'} ${detail}`)
      if (!ok) anyFailed = true
      return ok
    },
    failed: () => anyFailed,
  }
}
