/**
 * Guards for an interposed gate's refusal: the three signals that identify one, the two
 * outcomes, and the fact that the body's CONTENTS are never read.
 *
 * The last of those is the point of the module's current shape. An earlier revision
 * scanned the body for anchors, form actions, password fields, meta refreshes and
 * `location=` assignments to tell a lapsed session from a firewall block, and review
 * found four separate classes of page it read backwards. The message is honest for both,
 * so the distinction is gone and so is every reader of the far side's markup.
 */
import { describe, it, expect, vi, afterEach } from 'vitest'

import {
  noteEdgeAuthChallenge,
  edgeChallengeMessage,
  addressNamedByMessage,
  __resetAddressProvenanceForTests,
} from '../api/edgeAuthChallenge'
import { i18next } from '../i18n/index'

const HTML_TYPE = 'text/html; charset=UTF-8'
const HERE = 'https://dash.example/chat?sid=7'

/** A gate's sign-in page, shaped after a real tunnel challenge. */
const CHALLENGE_PAGE =
  '<!DOCTYPE html><html><head><title>Access Required</title></head><body>'
  + '<h1>Access Required</h1><p>This tunnel requires authentication.</p>'
  + '<p><a href="https://dash.example/gate-auth?redirect=%2Fapi">Sign in</a></p>'
  + '</body></html>'

/** A firewall block page: the same three signals, nowhere to sign in. */
const BLOCK_PAGE =
  '<!DOCTYPE html><html><head><title>Access denied</title></head><body>'
  + '<h1>Access denied</h1><p>This request was blocked. Ray ID: 8f2a1c</p>'
  + '</body></html>'

/**
 * A geo-block page whose VISIBLE TEXT contains `Location = US`.
 *
 * The scanner that read `location` assignments ran over the whole body, so this page's
 * prose satisfied it and the reader was told their session had lapsed -- with retries
 * withdrawn -- on the strength of a table cell.
 */
const GEO_BLOCK_PAGE =
  '<!DOCTYPE html><html><head><title>Unavailable</title></head><body>'
  + '<h1>Not available in your region</h1>'
  + '<dl><dt>Location = US</dt><dd>Ray ID: 8f2a1c</dd></dl>'
  + '</body></html>'

/** A top-level document at `HERE`; `self`/`top` match so the frame check reads unframed. */
function asTopLevelDocument(): void {
  const win = { location: { href: HERE, origin: new URL(HERE).origin } } as Record<string, unknown>
  win.self = win
  win.top = win
  vi.stubGlobal('window', win)
}

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
  __resetAddressProvenanceForTests()
})

describe('the three signals a refusal needs', () => {
  it('ignores a status that is not an auth refusal', () => {
    asTopLevelDocument()
    expect(noteEdgeAuthChallenge(500, HTML_TYPE, CHALLENGE_PAGE)).toBeNull()
    expect(noteEdgeAuthChallenge(404, HTML_TYPE, CHALLENGE_PAGE)).toBeNull()
    expect(noteEdgeAuthChallenge(200, HTML_TYPE, CHALLENGE_PAGE)).toBeNull()
  })

  it('ignores a content type that is not markup', () => {
    asTopLevelDocument()
    expect(noteEdgeAuthChallenge(403, 'application/json', '{"error":"nope"}')).toBeNull()
    expect(noteEdgeAuthChallenge(403, null, CHALLENGE_PAGE)).toBeNull()
  })

  it('ignores a body that is not a document, however it is labelled', () => {
    asTopLevelDocument()
    expect(noteEdgeAuthChallenge(403, HTML_TYPE, 'plain refusal text')).toBeNull()
    expect(noteEdgeAuthChallenge(403, HTML_TYPE, '')).toBeNull()
    expect(noteEdgeAuthChallenge(403, HTML_TYPE, '{"error":"nope"}')).toBeNull()
  })

  it('accepts xhtml, which a gate may serve instead', () => {
    asTopLevelDocument()
    expect(noteEdgeAuthChallenge(403, 'application/xhtml+xml', CHALLENGE_PAGE))
      .toBe('challenged')
  })

  it('ignores the shapes the gateway\'s own 401 actually takes', () => {
    // Read at the source: its 401 sinks are JSON bar a few plain-text ones, so an HTML
    // 401 would be a new sink rather than an existing one.
    asTopLevelDocument()
    expect(noteEdgeAuthChallenge(401, 'application/json', '{"error":"unauthenticated"}'))
      .toBeNull()
    expect(noteEdgeAuthChallenge(401, 'text/plain; charset=utf-8', 'Unauthorized')).toBeNull()
  })

  it('names a refusal on either auth status', () => {
    asTopLevelDocument()
    expect(noteEdgeAuthChallenge(403, HTML_TYPE, CHALLENGE_PAGE)).toBe('challenged')
    expect(noteEdgeAuthChallenge(401, HTML_TYPE, CHALLENGE_PAGE)).toBe('challenged')
  })
})

describe('the body\'s contents are never read', () => {
  it('answers the same for a page offering a way in and one offering none', () => {
    // The structural guard. Any difference here means something is reading the far
    // side's markup again, which is what produced four misreadings in review.
    asTopLevelDocument()
    const offered = noteEdgeAuthChallenge(403, HTML_TYPE, CHALLENGE_PAGE)
    const none = noteEdgeAuthChallenge(403, HTML_TYPE, BLOCK_PAGE)
    expect(offered).toBe('challenged')
    expect(none).toBe(offered)
  })

  it('does not read a geo-block page\'s visible text as a redirect', () => {
    asTopLevelDocument()
    expect(noteEdgeAuthChallenge(403, HTML_TYPE, GEO_BLOCK_PAGE)).toBe('challenged')
  })

  it.each([
    ['a password field', '<form><input type="password" name="p"></form>'],
    ['a meta refresh', '<meta http-equiv="refresh" content="0;url=/gate-auth">'],
    ['a scripted redirect', '<script>window.location.href = "/gate-auth"</script>'],
    ['a form posting to a gate', '<form action="/oauth2/start"><button>Go</button></form>'],
    ['a footer link only', '<a href="/status">Status</a>'],
  ])('reaches the same outcome with %s in the body', (_name, markup) => {
    asTopLevelDocument()
    const body = `<!DOCTYPE html><html><body>${markup}</body></html>`
    expect(noteEdgeAuthChallenge(403, HTML_TYPE, body)).toBe('challenged')
  })

  it('handles a hostile body without reading it', () => {
    // Nothing walks the body now, so a megabyte of unterminated tags costs the document
    // test alone. This would have been a quadratic scan before.
    asTopLevelDocument()
    const hostile = `<!DOCTYPE html><html><body>${'<a href="/x"'.repeat(60_000)}`
    const started = Date.now()
    expect(noteEdgeAuthChallenge(403, HTML_TYPE, hostile)).toBe('challenged')
    expect(Date.now() - started).toBeLessThan(1_000)
  })
})

describe('a pane cannot complete a sign-in inside itself', () => {
  it('answers framed inside a frame', () => {
    vi.stubGlobal('window', { location: { href: HERE }, self: {}, top: {} })
    expect(noteEdgeAuthChallenge(403, HTML_TYPE, CHALLENGE_PAGE)).toBe('framed')
  })

  it('answers framed when reading the ancestor throws, instead of throwing out', () => {
    // An inline `self !== top` comparison had no try/catch, so a refused ancestor turned
    // a handled refusal into an unhandled exception.
    const win: Record<string, unknown> = { location: { href: HERE }, self: {} }
    Object.defineProperty(win, 'top', {
      get() { throw new DOMException('cross-origin', 'SecurityError') },
    })
    vi.stubGlobal('window', win)
    expect(noteEdgeAuthChallenge(403, HTML_TYPE, CHALLENGE_PAGE)).toBe('framed')
  })

  it('answers challenged with no document at all', () => {
    vi.stubGlobal('window', undefined)
    expect(noteEdgeAuthChallenge(403, HTML_TYPE, CHALLENGE_PAGE)).toBe('challenged')
  })
})

describe('nothing in the refusal is ever acted on', () => {
  /** Every navigation route a body could name, so a regression trips one of them. */
  const spyWindow = () => {
    const assign = vi.fn(); const replace = vi.fn(); const reload = vi.fn()
    const open = vi.fn()
    const win = {
      location: { href: HERE, origin: new URL(HERE).origin, assign, replace, reload },
      open,
      sessionStorage: { getItem: () => { throw new Error('refused') } },
    } as Record<string, unknown>
    win.self = win
    win.top = win
    vi.stubGlobal('window', win)
    return { assign, replace, reload, open }
  }

  it('names the refusal and touches no navigation API', () => {
    const spies = spyWindow()
    expect(noteEdgeAuthChallenge(403, HTML_TYPE, CHALLENGE_PAGE)).toBe('challenged')
    for (const spy of Object.values(spies)) expect(spy).not.toHaveBeenCalled()
  })

  it('touches nothing for a body that names a destination', () => {
    const spies = spyWindow()
    const body = '<!DOCTYPE html><html><body>'
      + '<meta http-equiv="refresh" content="0;url=https://evil.example/take-over">'
      + '<a href="https://evil.example/gate-auth">Sign in</a>'
      + '<script>window.location.href = "https://evil.example/go"</script>'
      + '</body></html>'
    expect(noteEdgeAuthChallenge(403, HTML_TYPE, body)).toBe('challenged')
    for (const spy of Object.values(spies)) expect(spy).not.toHaveBeenCalled()
  })
})

describe('edgeChallengeMessage', () => {
  it('answers null for a response that was not a refusal', () => {
    expect(edgeChallengeMessage(null)).toBeNull()
  })

  it.each(['challenged', 'framed'] as const)('offers the lapse as a condition for %s', (outcome) => {
    asTopLevelDocument()
    const message = edgeChallengeMessage(outcome) as string
    expect(message).toBeTruthy()
    expect(message).not.toContain('api.client.')
    // Nothing establishes a lapse any more, so no message may open by asserting one.
    expect(message).toMatch(/if a sign-in page appears/i)
    expect(message.trim()).not.toMatch(/^your access proxy/i)
  })

  it('leads the unframed message with the action, and keeps the refusal arm', () => {
    asTopLevelDocument()
    const message = edgeChallengeMessage('challenged') as string
    expect(message.trim()).toMatch(/^reload this browser tab/i)
    expect(message).toMatch(/proxy or firewall/i)
    // "check its logs" left the referent ambiguous between the proxy and the firewall,
    // and read as an instruction to an operator rather than to someone browsing through.
    expect(message).toMatch(/check the proxy.s logs/i)
    expect(message).not.toMatch(/check its logs/i)
  })

  it('tells a pane reader how to get back, not just how to sign in', () => {
    // Withdrawing the retry means nothing refetches once they return, so a message that
    // stops at "signing in here won't work" leaves recovery looking like a failure.
    asTopLevelDocument()
    const framed = edgeChallengeMessage('framed') as string
    expect(framed).toMatch(/then reload this page/i)
  })

  it('sends a pane elsewhere, since reloading it cannot complete a sign-in', () => {
    asTopLevelDocument()
    const framed = edgeChallengeMessage('framed') as string
    expect(framed).toMatch(/new tab/i)
    expect(framed).not.toMatch(/reload this browser tab/i)
    expect(framed).toContain(new URL(HERE).origin)
  })
})

describe('addressNamedByMessage', () => {
  it('answers the origin for the one message that names one', () => {
    asTopLevelDocument()
    const framed = edgeChallengeMessage('framed') as string
    expect(addressNamedByMessage(framed)).toBe(new URL(HERE).origin)
  })

  it('survives a language switch between producing and rendering', () => {
    // A switch remounts the tree and re-evaluates every i18nT (see i18n/t), so the
    // stored message stays in the old locale while a re-derived one arrives in the new.
    asTopLevelDocument()
    const produced = edgeChallengeMessage('framed') as string
    const switched = `${produced} [neu gerendert]`
    vi.spyOn(i18next, 't').mockReturnValue(switched as unknown as string)
    expect(edgeChallengeMessage('framed')).not.toBe(produced)
    // Provenance is remembered, not re-derived, so the first message keeps its address.
    expect(addressNamedByMessage(produced)).toBe(new URL(HERE).origin)
  })

  it('answers null for every other message', () => {
    asTopLevelDocument()
    expect(addressNamedByMessage(edgeChallengeMessage('challenged') as string)).toBeNull()
    expect(addressNamedByMessage('HTTP 403')).toBeNull()
    expect(addressNamedByMessage(`see ${new URL(HERE).origin}/api/x`)).toBeNull()
  })

  it('answers the address the TEXT carries, not the document\'s origin now', () => {
    asTopLevelDocument()
    const framed = edgeChallengeMessage('framed') as string
    // The document moved on; the message still names where it was produced.
    vi.stubGlobal('location', { origin: 'https://elsewhere.example' })
    expect(addressNamedByMessage(framed)).toBe(new URL(HERE).origin)
  })
})
