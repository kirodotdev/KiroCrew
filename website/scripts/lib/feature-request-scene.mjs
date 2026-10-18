/**
 * Shared stubs for the "Request a Feature" screenshot scenes.
 *
 * Both scenes click the header pill in the built SPA and answer its three
 * requests (slot create, seed, send) with stubs, then push the turn's error row
 * over the app's WebSocket. These helpers hold the part they share: the route
 * stubs and the assert/shot bookkeeping.
 */
import { json } from './boot-api.mjs'

/**
 * Answer the pill's requests for `featureRequestSlot`. Registered after the
 * harness's catch-all, so it is consulted first; anything else falls through.
 *
 * The new slot's detail tracks what the gateway would have PERSISTED (the user
 * row on send, then whatever the scene pushes onto the returned `rows`), since
 * the app re-reads the slot when the turn ends. The persisted user row keeps
 * the send's `meta` verbatim (`sendId` and the flow's `featureRequest` stamp),
 * as `chat_handlers.py` does: the stamp is how the transcript recognises the
 * feature-request turn after a refresh or a reload.
 *
 * @returns {Promise<object[]>} the feature-request slot's persisted rows.
 */
export async function routeFeatureRequestSlot(page, { existingSlot, featureRequestSlot, detail }) {
  const key = featureRequestSlot.key
  let created = false
  const rows = []
  await page.route('**/api/**', async route => {
    const req = route.request()
    const path = new URL(req.url()).pathname
    if (path === '/api/chat/slots' && req.method() === 'POST') { created = true; return json(route, featureRequestSlot) }
    if (path === '/api/chat/slots' && req.method() === 'GET') return json(route, created ? [featureRequestSlot, existingSlot] : [existingSlot])
    if (path === `/api/chat/slots/${key}/context`) return json(route, { ok: true })
    if (path.startsWith(`/api/chat/slots/${key}`)) return json(route, { ...detail, total: rows.length, messages: rows })
    // The send: a 2xx JSON receipt with `ok`, which is what "the server accepted
    // the turn" looks like on the wire. The refusal comes later, in the turn.
    if (path === '/api/chat' && req.method() === 'POST') {
      const body = req.postDataJSON()
      rows.push({ role: 'user', ts: Date.now() / 1000, content: body.message, meta: { ...(body.meta || {}), mid: 'u-1' } })
      return json(route, { ok: true })
    }
    return route.fallback()
  })
  return rows
}

/** PASS/FAIL logging and `<out>/<expect>-<name>.png` shots for one scene. */
export function sceneChecks(page, out, expect) {
  let failures = 0
  return {
    assert(label, ok) {
      console.log(`${ok ? 'PASS' : 'FAIL'}: ${label}`)
      if (!ok) failures += 1
    },
    async shot(name) {
      const file = `${out}/${expect}-${name}.png`
      await page.screenshot({ path: file })
      console.log('wrote', file)
    },
    failures: () => failures,
  }
}
