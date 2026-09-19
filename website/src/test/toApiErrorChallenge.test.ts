/**
 * The app-bundle error factory sees an interposed proxy's challenge too.
 *
 * `toApiError` is the other `ApiError` factory — 12 call sites across five app
 * modules, whose `/apps/<app>/api/…` requests traverse the same proxy. It read only
 * the gateway's `X-Auth-Required` header, so the identical refusal reached those
 * bundles as a bare `HTTP 403`. It deliberately does NOT journal or raise the
 * stale-owner prompt; detection is side-effect-free, so it does not breach that.
 */
import { describe, it, expect } from 'vitest'
import { toApiError } from '../api/apiError'
import { edgeChallengeMessage } from '../api/edgeAuthChallenge'

const html = (inner: string): Response =>
  new Response(`<!DOCTYPE html><html><body>${inner}</body></html>`, {
    status: 403,
    headers: { 'content-type': 'text/html; charset=UTF-8' },
  })

const CHALLENGE = '<h1>Access Required</h1>'
  + '<p><a href="https://dash.example/gate-auth?redirect=%2Fapi">Sign in</a></p>'
const BLOCK = '<h1>Sorry, you have been blocked</h1><p>Ray ID: 8f2a1c</p>'
  + '<footer><a href="/authors/jane">Authors</a></footer>'

describe('toApiError', () => {
  it('names a lapsed proxy instead of the bare status', async () => {
    const err = await toApiError(html(CHALLENGE))
    expect(err.status).toBe(403)
    expect(err.message).toMatch(/access proxy/i)
    expect(err.message).not.toMatch(/^HTTP 403$/)
    expect(err.authRequired).toBe(true)
  })

  it('treats a page with no way in exactly like one that offers one', async () => {
    const offered = await toApiError(html(CHALLENGE))
    const none = await toApiError(html(BLOCK))
    expect(none.message).toBe(offered.message)
    expect(none.authRequired).toBe(true)
    expect(none.edgeChallenge).toBe(true)
  })

  it('leaves an ordinary refusal exactly as it was', async () => {
    const err = await toApiError(new Response(JSON.stringify({ error: 'nope' }), {
      status: 500, headers: { 'content-type': 'application/json' },
    }))
    expect(err.message).not.toMatch(/access proxy|sign-in page/i)
  })

  it('still honours the gateway header on its own', async () => {
    const err = await toApiError(new Response('{"error":"bad signature"}', {
      status: 403,
      headers: { 'content-type': 'application/json', 'X-Auth-Required': 'true' },
    }))
    expect(err.authRequired).toBe(true)
  })

  it('does not call the gateway\'s OWN denial page a proxy lapse', async () => {
    // Shaped after the gateway's non-API denial, whose inline `location.href`
    // satisfies the affordance scan; its own header is what settles it.
    const gatewayDenial = '<!DOCTYPE html><html><head><title>Access Denied</title></head>'
      + "<body><h1>Access Denied</h1><input id='u' type='text' placeholder='Paste token'>"
      + "<button onclick='go()'>Connect</button><script>function go(){"
      + "window.location.href=window.location.protocol+'//'+window.location.host"
      + "+'?token='+encodeURIComponent(t)}</script></body></html>"
    const err = await toApiError(new Response(gatewayDenial, {
      status: 403,
      headers: { 'content-type': 'text/html; charset=UTF-8', 'X-Auth-Required': 'true' },
    }))
    expect(err.message).not.toMatch(/access proxy/i)
    expect(err.authRequired).toBe(true)
    // The retry the gateway's silent refresh rides on must survive.
    expect(err.edgeChallenge).toBe(false)
  })
})

describe('edgeChallengeMessage', () => {
  it('carries an address in the framed message, which a pane cannot otherwise show', () => {
    const framed = edgeChallengeMessage('framed')
    expect(framed).toBeTruthy()
    expect(framed as string).toMatch(/https?:\/\/\S+/)
    // What it may not do is tell the reader to OPEN "this page": a pane reader cannot see
    // the address bar. Telling them to RELOAD it names something they can see.
    expect(framed as string).not.toMatch(/open this page/i)
  })

  it('answers null for a response that was not a challenge', () => {
    expect(edgeChallengeMessage(null)).toBeNull()
  })
})
