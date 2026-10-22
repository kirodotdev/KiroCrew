import { describe, it, expect } from 'vitest'
import { buildSrcdoc } from '../lib/widgetSrcdoc'
import { artifactFrame } from '../lib/artifactFrame'

// The document a report opens as, built with the real buildSrcdoc and the same
// options ArtifactBody passes: the crewmate Dashboard page's CSP, with the host's
// own height and comment scripts still injected.

const CDN = ['https://cdn.jsdelivr.net', 'https://cdnjs.cloudflare.com']

// The page's own script is built through the DOM, so the fixture is plain data.
function pageScript(): string {
  const script = document.createElement('script')
  script.textContent = 'location.href="https://example.invalid/?d=1"'
  return script.outerHTML
}

const REPORT = '<section><h1>Week 41</h1><svg viewBox="0 0 10 10"><rect width="5" height="5"/></svg></section>'
  + pageScript()
  + '<button onclick="location.href=\'https://example.invalid/\'">go</button>'

// Markup that navigates a frame without a script: a refresh, a hint, and a
// noscript whose content the host and the frame parse differently.
const NAVIGATING = '<section><h1>Week 41</h1></section>'
  + '<meta http-equiv="refresh" content="0;url=https://example.invalid/r">'
  + '<link rel="prefetch" href="https://example.invalid/l">'
  + '<noscript><p title="</noscript><meta http-equiv=refresh content=0;url=https://example.invalid/n>"></p></noscript>'
  + '<div><template shadowrootmode="closed"><a href="https://example.invalid/s">Details</a></template></div>'

function documentFor(goal_report: boolean, html: string = REPORT): string {
  const frame = artifactFrame({ goal_report })
  return buildSrcdoc({
    html,
    themeVars: {}, mode: 'light', enableComments: true, includeHeightReporter: true,
    linkBridge: frame.linkBridge, offlineScripts: frame.offlineScripts, rewriteBareLinks: frame.rewriteBareLinks,
    pageScripts: frame.pageScripts,
  })
}

function scriptSrcOf(csp: string): string {
  return csp.split(';').map((d) => d.trim()).find((d) => d.startsWith('script-src ')) ?? ''
}

function cspOf(doc: string): string {
  const parsed = new DOMParser().parseFromString(doc, 'text/html')
  return parsed.querySelector('meta[http-equiv="Content-Security-Policy"]')?.getAttribute('content') ?? ''
}

describe('a report document', () => {
  it('reaches no network, runs no eval, and loads no CDN script', () => {
    const csp = cspOf(documentFor(true))
    expect(csp).toContain("connect-src 'none'")
    expect(csp).toContain("default-src 'none'")
    expect(csp).toContain('img-src data: blob:')
    expect(csp).not.toContain('unsafe-eval')
    for (const origin of CDN) expect(csp).not.toContain(origin)
  })

  it('keeps the host height reporter and comment bridge', () => {
    const doc = documentFor(true)
    expect(doc).toContain('mc-widget-height')
    expect(doc).toContain('<svg')
  })

  it('runs only the host scripts: every one carries the nonce, no page script does', () => {
    const doc = documentFor(true)
    const scriptSrc = scriptSrcOf(cspOf(doc))
    expect(scriptSrc).not.toContain('unsafe-inline')
    const nonce = /'nonce-([0-9a-f]{32})'/.exec(scriptSrc)?.[1]
    expect(nonce).toBeTruthy()
    const scripts = Array.from(new DOMParser().parseFromString(doc, 'text/html').querySelectorAll('script'))
    const host = scripts.filter((s) => s.getAttribute('nonce') === nonce)
    const page = scripts.filter((s) => s.getAttribute('nonce') !== nonce)
    // Height reporter and comment bridge among the admitted ones.
    expect(host.some((s) => (s.textContent ?? '').includes('mc-widget-height'))).toBe(true)
    expect(host.length).toBeGreaterThanOrEqual(3)
    // The page's own script is present as markup and refused by the CSP.
    expect(page.map((s) => s.textContent)).toEqual(['location.href="https://example.invalid/?d=1"'])
    // A fresh nonce per document, so one read cannot predict the next.
    expect(scriptSrcOf(cspOf(documentFor(true)))).not.toBe(scriptSrc)
  })

  it('drops what moves the frame with no script: refresh meta, link, noscript, shadow root', () => {
    const doc = documentFor(true, NAVIGATING)
    const body = new DOMParser().parseFromString(doc, 'text/html').body
    expect(body.querySelectorAll('meta, link, noscript, template')).toHaveLength(0)
    expect(doc).not.toContain('example.invalid/r')
    expect(doc).not.toContain('example.invalid/s')
    expect(doc).not.toContain('example.invalid/n')
    // The page's own content stays.
    expect(body.querySelector('h1')?.textContent).toBe('Week 41')
  })

  it('makes every link inert with a host listener that carries the nonce', () => {
    const doc = documentFor(true)
    const nonce = /'nonce-([0-9a-f]{32})'/.exec(scriptSrcOf(cspOf(doc)))?.[1]
    const scripts = Array.from(new DOMParser().parseFromString(doc, 'text/html').querySelectorAll('script'))
    const guard = scripts.find((s) => (s.textContent ?? '').includes("window.addEventListener('auxclick',stop,true)"))
    expect(guard?.getAttribute('nonce')).toBe(nonce)
    // Only a click on a link loses its default, so a <details> still opens.
    expect(guard?.textContent).toContain('composedPath')
    expect(guard?.textContent).toContain("l==='a'||l==='area'")
  })

  it('leaves an ordinary artifact its markup and its links', () => {
    const doc = documentFor(false, NAVIGATING)
    expect(new DOMParser().parseFromString(doc, 'text/html').body.querySelector('meta[http-equiv]')).not.toBeNull()
    // No inert-link guard (the link bridge's own auxclick listener is another script).
    expect(doc).not.toContain("window.addEventListener('auxclick',stop,true)")
  })

  it('leaves an ordinary artifact running its own inline script', () => {
    expect(scriptSrcOf(cspOf(documentFor(false)))).toContain("'unsafe-inline'")
  })

  it('leaves an ordinary artifact with its CDN origins', () => {
    const csp = cspOf(documentFor(false))
    for (const origin of CDN) expect(csp).toContain(origin)
  })
})
