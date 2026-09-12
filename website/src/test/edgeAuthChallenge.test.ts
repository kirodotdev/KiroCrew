/**
 * Guards for the interposed-proxy auth challenge: the three signals that identify
 * one, and the affordance scan that tells a lapsed session from a block page. No URL
 * from the challenge body is ever followed, and nothing here acts on the document.
 */
import { describe, it, expect, vi, afterEach } from 'vitest'

import {
  hasSignInAffordance,
  isEdgeAuthChallenge,
  noteEdgeAuthChallenge,
} from '../api/edgeAuthChallenge'

const HERE = 'https://dash.example/chat?sid=7'
/** Same-origin, which the affordance scan accepts and nothing ever visits. */
const SIGNIN = 'https://dash.example/gate-auth'

/** Shaped after a real tunnel challenge: a doctype, then a sign-in anchor whose
 *  `redirect` names the request that was refused rather than the user's page. */
const challengePage = (href = `${SIGNIN}?redirect=https%3A%2F%2Fdash.example%2Fapi%2Fhealth`) =>
  `<!DOCTYPE html><html><head><title>Access Required</title></head><body>` +
  `<h1>Access Required</h1><p>This tunnel requires authentication.</p>` +
  `<p><a href="${href}">Sign in</a> to continue.</p></body></html>`

const HTML_TYPE = 'text/html; charset=UTF-8'

/** A WAF / rate-limit block page: same three signals, no sign-in link. */
const BLOCK_PAGE =
  `<!DOCTYPE html><html><head><title>Access denied</title></head><body>` +
  `<h1>Access denied</h1><p>This request was blocked. Ray ID: 8f2a1c</p>` +
  `</body></html>`

/**
 * A real challenge page, trimmed with the hosts renamed. This shape — an inline
 * `<svg>` carrying its own `<style>` — is why extraction does not parse the
 * document: the parse loses the anchor entirely.
 */
const SVG_CHALLENGE =
  `<!DOCTYPE html><html><head><meta charset="utf-8"><title>Access Required</title>\n` +
  `<style>body{font-family:system-ui}.card{background:white}</style></head>\n` +
  `<body><div class="card"><svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" width="128" height="128">\n` +
  `  <style>\n` +
  `    @keyframes blink { 0%, 92%, 100% { transform: scaleY(1); } }\n` +
  `    .eye { animation: blink 4s ease-in-out infinite; transform-origin: center; }\n` +
  `  </style>\n` +
  `  <rect x="0" y="21" width="24" height="3" fill="#5c6370"/>\n` +
  `</svg>\n` +
  `<h1>Access Required</h1><p>This gateway requires authentication.</p>\n` +
  `<p><a href="${SIGNIN}?redirect=https%3A%2F%2Fdash.example%2Fapi%2Fsessions">Sign in</a> to continue.</p>\n` +
  `</div></body></html>\n`


describe('isEdgeAuthChallenge', () => {
  it('matches the 403 HTML page an interposed gate serves', () => {
    expect(isEdgeAuthChallenge(403, HTML_TYPE, challengePage())).toBe(true)
  })

  it('matches a 401 too, the canonical status for an auth challenge', () => {
    // The HTML-document requirement is what keeps the gateway's own JSON 401 out.
    expect(isEdgeAuthChallenge(401, 'text/html', challengePage())).toBe(true)
  })

  it('ignores the content-type parameters and casing', () => {
    expect(isEdgeAuthChallenge(403, 'TEXT/HTML;charset=utf-8', challengePage())).toBe(true)
  })

  // The three negative controls that keep this from firing on the gateway's own
  // denials — each one alone would be enough to misread a real 403.
  it('does not match the gateway JSON denial it must not be confused with', () => {
    expect(isEdgeAuthChallenge(403, 'application/json', '{"error":"invalid signature"}')).toBe(false)
  })

  it('does not match an HTML content-type whose body is not a document', () => {
    expect(isEdgeAuthChallenge(403, HTML_TYPE, '{"error":"nope"}')).toBe(false)
  })

  it('does not match an XML error envelope', () => {
    expect(isEdgeAuthChallenge(403, HTML_TYPE, '<?xml version="1.0"?><Error/>')).toBe(false)
  })

  it('does not match an HTML page on a status that is not a refusal', () => {
    expect(isEdgeAuthChallenge(500, HTML_TYPE, challengePage())).toBe(false)
    expect(isEdgeAuthChallenge(404, HTML_TYPE, challengePage())).toBe(false)
  })

  it('does not match a missing content-type', () => {
    expect(isEdgeAuthChallenge(403, null, challengePage())).toBe(false)
  })
})

describe('the sign-in affordance scan', () => {
  it('skips an unsafe href and still finds the usable link', () => {
    const body =
      `<!DOCTYPE html><html><body><a href="javascript:alert(1)">x</a>` +
      `<a href="${SIGNIN}">Sign in</a></body></html>`
    expect(hasSignInAffordance(body, HERE)).toBe(true)
  })

  it('answers false when the page offers no usable link', () => {
    expect(hasSignInAffordance('<!DOCTYPE html><html><body>Forbidden</body></html>', HERE))
      .toBe(false)
    expect(hasSignInAffordance(challengePage('data:text/html,<b>x</b>'), HERE)).toBe(false)
  })

  it('ignores a non-anchor href, so a stylesheet is not read as a way in', () => {
    const body =
      `<!DOCTYPE html><html><head>` +
      `<link rel="stylesheet" href="https://dash.example/challenge.css">` +
      `</head><body>Forbidden</body></html>`
    expect(hasSignInAffordance(body, HERE)).toBe(false)
  })

  it('reads the href wherever it sits in the anchor start tag', () => {
    expect(hasSignInAffordance(`<a href="${SIGNIN}">x</a>`, HERE)).toBe(true)
    expect(hasSignInAffordance(`<a class="btn" href="${SIGNIN}">x</a>`, HERE)).toBe(true)
    // Elements whose name merely starts with "a" must not match the anchor pattern.
    expect(hasSignInAffordance(`<area href="${SIGNIN}">`, HERE)).toBe(false)
    expect(hasSignInAffordance(`<abbr href="${SIGNIN}">x</abbr>`, HERE)).toBe(false)
  })

  it('refuses smuggled credentials in a link that serializes to our own origin', () => {
    expect(new URL('https://user:pass@dash.example/gate-auth').origin).toBe(new URL(HERE).origin)
    expect(hasSignInAffordance(challengePage('https://user:pass@dash.example/gate-auth'), HERE))
      .toBe(false)
  })

  it('finds the link on a real page whose inline SVG defeats jsdom\'s DOM parse', () => {
    // A fact about JSDOM, not browsers: Chromium 145 finds the anchor. Pins
    // testability here, not a production defect.
    expect(new DOMParser().parseFromString(SVG_CHALLENGE, 'text/html')
      .querySelectorAll('a[href]').length).toBe(0)
    expect(hasSignInAffordance(SVG_CHALLENGE, HERE)).toBe(true)
  })

  it('stops scanning a body that offers nothing usable', () => {
    // A pathological body must not be walked indefinitely: the cap bounds it.
    expect(hasSignInAffordance('<a href="javascript:x">y</a>'.repeat(500), HERE)).toBe(false)
  })
})

describe('hasSignInAffordance', () => {
  it('is true for a link that names authentication, cross-origin included', () => {
    expect(hasSignInAffordance(challengePage(), HERE)).toBe(true)
    expect(hasSignInAffordance(challengePage('https://auth.example/x'), HERE)).toBe(true)
    expect(hasSignInAffordance(challengePage('/gate-auth'), HERE)).toBe(true)
  })

  it('is false for a page offering no usable link', () => {
    expect(hasSignInAffordance(BLOCK_PAGE, HERE)).toBe(false)
    expect(hasSignInAffordance(challengePage('javascript:alert(1)'), HERE)).toBe(false)
    expect(hasSignInAffordance(challengePage('https://user:pass@dash.example/x'), HERE)).toBe(false)
  })

  it('does not count a non-anchor href as an affordance', () => {
    const body =
      `<!DOCTYPE html><html><head>` +
      `<link rel="stylesheet" href="https://dash.example/block.css">` +
      `</head><body><h1>Access denied</h1></body></html>`
    expect(hasSignInAffordance(body, HERE)).toBe(false)
  })
})

describe('the recognised challenge shape', () => {
  it('accepts a 401, the canonical status for an auth challenge', () => {
    // Calibrating on the one captured 403 left an oauth2-proxy-style 401 gate inside
    // the silent failure this module exists to end.
    expect(isEdgeAuthChallenge(401, 'text/html', '<html><body>Sign in</body></html>'))
      .toBe(true)
  })

  it('still refuses a 401 that is not an HTML document, as the gateway sends', () => {
    expect(isEdgeAuthChallenge(401, 'application/json', '{"error":"unauthorized"}'))
      .toBe(false)
  })

  it('still refuses every other status', () => {
    for (const status of [400, 404, 407, 500, 502]) {
      expect(isEdgeAuthChallenge(status, 'text/html', '<html><body>x</body></html>'))
        .toBe(false)
    }
  })

  it('reads a meta refresh as a way in, so it is not called a block page', () => {
    const body = '<html><head><meta http-equiv="refresh" content="0;url=/login">' +
      '</head><body>Redirecting</body></html>'
    expect(hasSignInAffordance(body, 'https://dash.example.com/')).toBe(true)
  })

  it('reads a scripted redirect as a way in', () => {
    const body = '<html><body><script>location.replace("/oauth2/start")</script>' +
      '</body></html>'
    expect(hasSignInAffordance(body, 'https://dash.example.com/')).toBe(true)
  })

  it('still calls a page with neither a link nor a redirect a block page', () => {
    const body = '<html><body><h1>Access denied by policy</h1></body></html>'
    expect(hasSignInAffordance(body, 'https://dash.example.com/')).toBe(false)
  })
})

describe('a hostile body cannot make the scan run away', () => {
  // The pathological input GPT and Opus both named: many unterminated start tags,
  // which made `[^>]+` re-expand to end-of-string at every one of them.
  const unterminated = (tag: string, n: number): string =>
    '<!DOCTYPE html><html><body>' + `<${tag} `.repeat(n)

  it('answers a 200k body of unterminated meta tags well inside a time budget', () => {
    const body = unterminated('meta', 40_000)
    const started = Date.now()
    expect(hasSignInAffordance(body, 'https://dash.example.com/')).toBe(false)
    // The quadratic form does not finish this in seconds, let alone 2s. A generous
    // ceiling keeps the pin about the ALGORITHM rather than the runner's speed.
    expect(Date.now() - started).toBeLessThan(2_000)
  })

  it('answers a 200k body of unterminated anchors just as fast', () => {
    const body = unterminated('a', 40_000)
    const started = Date.now()
    expect(hasSignInAffordance(body, 'https://dash.example.com/')).toBe(false)
    expect(Date.now() - started).toBeLessThan(2_000)
  })

  it('answers a SINGLE meta tag carrying a huge attribute run', () => {
    // The other shape: one terminated tag whose attribute text is enormous and
    // repeats ` http-equiv`, so a non-fixed-width read is quadratic inside it.
    const attrs = ' http-equiv=x'.repeat(20_000)
    const body = `<!DOCTYPE html><html><head><meta${attrs}>`
    const started = Date.now()
    expect(hasSignInAffordance(body, 'https://dash.example.com/')).toBe(false)
    expect(Date.now() - started).toBeLessThan(2_000)
  })

  it('does not read a scripted redirect from past the scan cap', () => {
    const pad = '<!DOCTYPE html><html><body>' + 'x'.repeat(70 * 1024)
    expect(hasSignInAffordance(`${pad}<script>location.href = "/in"</script>`,
      'https://dash.example.com/')).toBe(false)
  })

  it('does not read a redirect from past the scan cap', () => {
    // The cap is the point: a way in buried past 64KB is not found, exactly as an
    // anchor past the cap is not. Being bounded is the property, not being thorough.
    const pad = '<!DOCTYPE html><html><body>' + 'x'.repeat(70 * 1024)
    expect(hasSignInAffordance(pad + '<meta http-equiv="refresh" content="0;/in">',
      'https://dash.example.com/')).toBe(false)
  })

  it('still finds a redirect that sits before the cap', () => {
    // Positive control for the two assertions above: the scanner is not simply
    // blind, which would make every "not found" meaningless.
    const body = '<!DOCTYPE html><html><head><meta http-equiv="refresh" content="0">'
    expect(hasSignInAffordance(body, 'https://dash.example.com/')).toBe(true)
  })

  it('reads an unquoted http-equiv value, as the regex did', () => {
    const body = '<!DOCTYPE html><html><head><meta http-equiv=refresh content=0>'
    expect(hasSignInAffordance(body, 'https://dash.example.com/')).toBe(true)
  })

  it('is not fooled by a tag whose name merely starts with meta', () => {
    const body = '<!DOCTYPE html><html><head><metadata http-equiv="refresh">'
    expect(hasSignInAffordance(body, 'https://dash.example.com/')).toBe(false)
  })

  it('does not read `allocation =` as a redirect', () => {
    const body = '<!DOCTYPE html><html><body><script>allocation = 5</script>'
    expect(hasSignInAffordance(body, 'https://dash.example.com/')).toBe(false)
  })

  it('reads the scripted forms it claims to', () => {
    for (const js of ['location = "/in"', 'location.href = "/in"',
      'location.replace("/in")', 'location.assign("/in")',
      'window.location . href="/in"']) {
      const body = `<!DOCTYPE html><html><body><script>${js}</script>`
      expect(hasSignInAffordance(body, 'https://dash.example.com/')).toBe(true)
    }
  })
})


describe('noteEdgeAuthChallenge', () => {
  afterEach(() => { vi.unstubAllGlobals() })

  it('answers null for a response that is not a challenge', () => {
    expect(noteEdgeAuthChallenge(500, HTML_TYPE, challengePage())).toBeNull()
    expect(noteEdgeAuthChallenge(403, 'application/json', '{}')).toBeNull()
    expect(noteEdgeAuthChallenge(403, HTML_TYPE, 'plain refusal text')).toBeNull()
  })

  it('names a lapse when the page offers a way in', () => {
    expect(noteEdgeAuthChallenge(403, HTML_TYPE, challengePage())).toBe('expired')
    expect(noteEdgeAuthChallenge(401, HTML_TYPE, challengePage())).toBe('expired')
  })

  it('names a block page rather than diagnosing a lapse it cannot see', () => {
    expect(noteEdgeAuthChallenge(403, HTML_TYPE, BLOCK_PAGE)).toBe('no-signin')
  })

  it('answers framed inside a frame, whose sign-in cannot complete nested', () => {
    vi.stubGlobal('window', { location: { href: HERE }, self: {}, top: {} })
    expect(noteEdgeAuthChallenge(403, HTML_TYPE, challengePage())).toBe('framed')
  })

  it('answers framed when reading the ancestor throws, instead of throwing out', () => {
    // The inline comparison this replaced had no try/catch, so a refused ancestor
    // turned a handled refusal into an unhandled exception.
    const win: Record<string, unknown> = { location: { href: HERE }, self: {} }
    Object.defineProperty(win, 'top', {
      get() { throw new DOMException('cross-origin', 'SecurityError') },
    })
    vi.stubGlobal('window', win)
    expect(noteEdgeAuthChallenge(403, HTML_TYPE, challengePage())).toBe('framed')
  })
})

describe('nothing in the challenge body is ever acted on', () => {
  afterEach(() => { vi.unstubAllGlobals() })

  /** Every navigation route a body could name, so a regression trips one of them. */
  const spyWindow = () => {
    const assign = vi.fn(); const replace = vi.fn(); const reload = vi.fn()
    const open = vi.fn()
    vi.stubGlobal('window', {
      location: { href: HERE, assign, replace, reload }, self: null, top: null, open,
      sessionStorage: { getItem: () => { throw new Error('refused') } },
    })
    return { assign, replace, reload, open }
  }

  it('names the lapse and touches no navigation API', () => {
    const spies = spyWindow()
    expect(noteEdgeAuthChallenge(403, HTML_TYPE, challengePage())).toBe('expired')
    for (const spy of Object.values(spies)) expect(spy).not.toHaveBeenCalled()
  })

  it('adopts no session a same-origin body offers it', () => {
    // A body naming a route on our OWN origin is what made following any body URL
    // unsafe. Whatever it is classified as, nothing on the page is acted on: the
    // outcome is only a word handed to the caller for a message.
    const spies = spyWindow()
    expect(noteEdgeAuthChallenge(403, HTML_TYPE,
      challengePage('/projects?applied=7&autoRun=true'))).not.toBeNull()
    for (const spy of Object.values(spies)) expect(spy).not.toHaveBeenCalled()
  })
})

describe('a way OUT is not a way IN', () => {
  /** A real Cloudflare-style block page: the same three signals, ordinary links. */
  const blockPageWithFooter = (href: string) =>
    `<!DOCTYPE html><html><head><title>Access denied</title></head><body>` +
    `<h1>Sorry, you have been blocked</h1>` +
    `<p>Ray ID: 8f2a1c</p><footer><a href="${href}">Privacy Policy</a></footer>` +
    `</body></html>`

  it('a footer link is not a way to sign in', () => {
    // The defect: any safe http(s) URL counted, so a privacy link read as a lapse
    // and `authRequired` stopped retries a block page would have recovered from.
    for (const href of [
      'https://www.example.com/privacypolicy/',
      'https://www.example.com/terms',
      'mailto:abuse@example.com',
      '/status',
      'https://support.example.com/account',
      'https://example.com/help/session-limits',
    ]) {
      expect(hasSignInAffordance(blockPageWithFooter(href), HERE), href).toBe(false)
      expect(noteEdgeAuthChallenge(403, HTML_TYPE, blockPageWithFooter(href)), href)
        .toBe('no-signin')
    }
  })

  it('still recognises the routes a real gate sends you to', () => {
    for (const href of [
      'https://team.example.com/cdn-cgi/access/login?redirect_url=%2Fapi',
      'https://dash.example/oauth2/start?rd=%2Fapi%2Fhealth',
      'https://dash.example/gate-auth',
      'https://idp.example/saml/sso',
      'https://dash.example/?rd=%2Fapi%2Fhealth',
    ]) {
      expect(hasSignInAffordance(blockPageWithFooter(href), HERE), href).toBe(true)
    }
  })

  it('reads a comparison as a comparison, not a redirect', () => {
    // `location ==` navigates nowhere. Counting it as a redirect gave a block page
    // an affordance it does not have.
    for (const script of [
      'if (window.location == expected) render()',
      'if (document.location === "/blocked") report()',
      'const f = location => report(location)',
      'if (window.location.href == cached) return',
    ]) {
      const body = `<!DOCTYPE html><html><body><h1>Blocked</h1>` +
        `<script>${script}</script></body></html>`
      expect(noteEdgeAuthChallenge(403, HTML_TYPE, body), script).toBe('no-signin')
    }
  })

  it('still reads a real scripted redirect as one', () => {
    for (const script of [
      'window.location = "https://idp.example/login"',
      'document.location.href = "/oauth2/start"',
      'location.replace("/cdn-cgi/access/login")',
    ]) {
      const body = `<!DOCTYPE html><html><body><h1>Access Required</h1>` +
        `<script>${script}</script></body></html>`
      expect(noteEdgeAuthChallenge(403, HTML_TYPE, body), script).toBe('expired')
    }
  })
})
