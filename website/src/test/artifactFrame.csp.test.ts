import { describe, it, expect } from 'vitest'
import { buildSrcdoc } from '../lib/widgetSrcdoc'
import { artifactFrame } from '../lib/artifactFrame'

// The document a report opens as, built with the real buildSrcdoc and the same
// options ArtifactBody passes: the crewmate Dashboard page's CSP, with the host's
// own height and comment scripts still injected.

const CDN = ['https://cdn.jsdelivr.net', 'https://cdnjs.cloudflare.com']

function documentFor(goal_report: boolean): string {
  const frame = artifactFrame({ goal_report })
  return buildSrcdoc({
    html: '<section><h1>Week 41</h1><svg viewBox="0 0 10 10"><rect width="5" height="5"/></svg></section>',
    themeVars: {}, mode: 'light', enableComments: true, includeHeightReporter: true,
    offlineScripts: frame.offlineScripts, rewriteBareLinks: frame.rewriteBareLinks,
  })
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

  it('leaves an ordinary artifact with its CDN origins', () => {
    const csp = cspOf(documentFor(false))
    for (const origin of CDN) expect(csp).toContain(origin)
  })
})
