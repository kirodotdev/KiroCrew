import { describe, it, expect } from 'vitest'
import {
  buildMcpAppCsp,
  buildMcpAppSrcdoc,
  buildAllowAttribute,
  sanitizeCspDomain,
  type McpAppRenderPayload,
} from '../lib/mcpAppSrcdoc'

function payload(over: Partial<McpAppRenderPayload> = {}): McpAppRenderPayload {
  return {
    session_key: 'slot-1',
    tool_call_id: 'call-1',
    server: 'excalidraw',
    tool: 'create_view',
    html: '<!doctype html><html><head><title>App</title></head><body>hi</body></html>',
    csp: null,
    permissions: null,
    spool_id: 'uuid-1',
    ...over,
  }
}

describe('sanitizeCspDomain', () => {
  it('accepts a plain https host', () => {
    expect(sanitizeCspDomain('https://esm.sh')).toBe('https://esm.sh')
  })
  it('accepts a wildcard subdomain and a port', () => {
    expect(sanitizeCspDomain('https://*.example.com')).toBe('https://*.example.com')
    expect(sanitizeCspDomain('https://api.example.com:8443')).toBe('https://api.example.com:8443')
  })
  it('trims surrounding whitespace on an otherwise-valid host', () => {
    expect(sanitizeCspDomain('  https://esm.sh  ')).toBe('https://esm.sh')
  })
  it('rejects non-https schemes', () => {
    expect(sanitizeCspDomain('http://evil.com')).toBeNull()
    expect(sanitizeCspDomain('javascript:alert(1)')).toBeNull()
  })
  it('rejects CSP-injection payloads (semicolon / quotes / spaces / directives)', () => {
    expect(sanitizeCspDomain("https://ok.com; script-src *")).toBeNull()
    expect(sanitizeCspDomain('https://ok.com\'')).toBeNull()
    expect(sanitizeCspDomain('https://ok.com"')).toBeNull()
    expect(sanitizeCspDomain('https://ok.com script-src *')).toBeNull()
    expect(sanitizeCspDomain('*')).toBeNull()
    expect(sanitizeCspDomain('')).toBeNull()
  })
  it('rejects non-strings', () => {
    expect(sanitizeCspDomain(null)).toBeNull()
    expect(sanitizeCspDomain(42 as unknown)).toBeNull()
  })
})

describe('buildMcpAppCsp', () => {
  it('returns the strict default policy when csp is null', () => {
    const csp = buildMcpAppCsp(null)
    expect(csp).toContain("default-src 'none'")
    expect(csp).toContain("script-src 'self' 'unsafe-inline'")
    expect(csp).toContain("style-src 'self' 'unsafe-inline'")
    expect(csp).toContain("img-src 'self' data:")
    expect(csp).toContain("font-src 'self' data:")
    expect(csp).toContain("media-src 'self' data:")
    expect(csp).toContain("connect-src 'none'")
    expect(csp).toContain("frame-src 'none'")
    expect(csp).toContain("base-uri 'self'")
    expect(csp.endsWith(';')).toBe(true)
  })

  it('widens resource directives with resourceDomains', () => {
    const csp = buildMcpAppCsp({ resourceDomains: ['https://esm.sh'] })
    expect(csp).toContain("script-src 'self' 'unsafe-inline' https://esm.sh")
    expect(csp).toContain("style-src 'self' 'unsafe-inline' https://esm.sh")
    expect(csp).toContain("img-src 'self' data: https://esm.sh")
    // resourceDomains must NOT open connect-src (stays locked to 'none').
    expect(csp).toContain("connect-src 'none'")
  })

  it('replaces connect-src / frame-src / base-uri from their domain lists', () => {
    const csp = buildMcpAppCsp({
      connectDomains: ['https://api.example.com'],
      frameDomains: ['https://frames.example.com'],
      baseUriDomains: ['https://base.example.com'],
    })
    expect(csp).toContain('connect-src https://api.example.com')
    expect(csp).not.toContain("connect-src 'none'")
    expect(csp).toContain('frame-src https://frames.example.com')
    expect(csp).not.toContain("frame-src 'none'")
    expect(csp).toContain("base-uri 'self' https://base.example.com")
  })

  it('strips injection attempts from a domain before they reach the policy', () => {
    const csp = buildMcpAppCsp({
      resourceDomains: ['https://esm.sh', "https://evil.com;script-src *"],
    })
    // The valid one survives; the injection token is dropped entirely.
    expect(csp).toContain('https://esm.sh')
    expect(csp).not.toContain('evil.com')
    // No extra directive was smuggled in: exactly one script-src directive.
    expect(csp.match(/script-src/g)?.length).toBe(1)
  })

  it('dedupes repeated domains', () => {
    const csp = buildMcpAppCsp({ resourceDomains: ['https://esm.sh', 'https://esm.sh'] })
    expect(csp.match(/https:\/\/esm\.sh/g)?.length).toBeGreaterThan(0)
    // script-src should list esm.sh once, not twice.
    const scriptSrc = csp.split(';').find(d => d.trim().startsWith('script-src')) || ''
    expect(scriptSrc.match(/https:\/\/esm\.sh/g)?.length).toBe(1)
  })
})

describe('buildAllowAttribute', () => {
  it('returns empty string when no permissions', () => {
    expect(buildAllowAttribute(null)).toBe('')
    expect(buildAllowAttribute({})).toBe('')
  })
  it('maps requested permissions to Permissions-Policy tokens', () => {
    expect(buildAllowAttribute({ clipboardWrite: {} })).toBe('clipboard-write')
    // camera/microphone are dropped: unusable on the null-origin sandbox, so
    // advertising them only misleads (see buildAllowAttribute).
    const all = buildAllowAttribute({ camera: {}, microphone: {}, geolocation: {}, clipboardWrite: {} })
    expect(all).toBe('geolocation; clipboard-write')
  })
})

describe('buildMcpAppSrcdoc', () => {
  it('emits the CSP meta before every untrusted byte (after a leading doctype)', () => {
    const out = buildMcpAppSrcdoc(payload())
    // Leading doctype is hoisted (standards mode preserved), meta is next,
    // then our trusted bridge-guard <script>, then ALL app markup.
    expect(out).toMatch(/^<!doctype html><meta http-equiv="Content-Security-Policy"/i)
    const metaEnd = out.indexOf('">') + 2
    const rest = out.slice(metaEnd)
    // Our trusted bridge-guard bootstrap is injected here, then the app markup.
    expect(rest).toContain('__kirocrew_nav__')
    expect(rest.endsWith('<html><head><title>App</title></head><body>hi</body></html>')).toBe(true)
    expect(out).toContain("default-src 'none'")
  })

  it('injects a trusted bridge-guard that signals navigation on pagehide/unload', () => {
    // GPT 5.6 finding: without a pre-`load` signal, a navigated-to page's head
    // script could post tools/call before the host notices the navigation.
    const out = buildMcpAppSrcdoc(payload())
    // Sits AFTER the CSP meta (so the policy governs it) and BEFORE app markup
    // (so its capture-phase listeners register first).
    const metaIdx = out.indexOf('Content-Security-Policy')
    const guardIdx = out.indexOf('__kirocrew_nav__')
    const appIdx = out.indexOf('<title>App</title>')
    expect(metaIdx).toBeGreaterThanOrEqual(0)
    expect(guardIdx).toBeGreaterThan(metaIdx)
    expect(appIdx).toBeGreaterThan(guardIdx)
    expect(out).toContain("addEventListener('pagehide'")
    expect(out).toContain("addEventListener('beforeunload'")
    // Permitted by the emitted policy: script-src carries 'unsafe-inline'.
    expect(out).toMatch(/script-src[^;]*'unsafe-inline'/)
  })

  it('places the meta first when the doc has no doctype', () => {
    const out = buildMcpAppSrcdoc(payload({ html: '<html><body>x</body></html>' }))
    expect(out.startsWith('<meta http-equiv="Content-Security-Policy"')).toBe(true)
    expect(out).toContain('<body>x</body>')
  })

  it('never lets author markup precede the policy (pre-<head> resource attack)', () => {
    // The GPT-flagged attack: resource-loading markup placed AHEAD of <head>
    // used to parse before the spliced-in CSP. Now the meta is byte 0.
    const out = buildMcpAppSrcdoc(payload({
      html: '<img src="https://evil.example/x"><head><title>t</title></head>',
    }))
    expect(out.indexOf('Content-Security-Policy')).toBeLessThan(out.indexOf('<img'))
    expect(out.startsWith('<meta http-equiv="Content-Security-Policy"')).toBe(true)
  })

  it('does not hoist a doctype that is not at the very start', () => {
    // A doctype preceded by anything other than whitespace stays put — only
    // untrusted-free content may ride ahead of the policy.
    const out = buildMcpAppSrcdoc(payload({ html: '<p>x</p><!doctype html><p>y</p>' }))
    expect(out.startsWith('<meta http-equiv="Content-Security-Policy"')).toBe(true)
  })

  it('handles fragments with no doctype/html/head', () => {
    const out = buildMcpAppSrcdoc(payload({ html: '<p>frag</p>' }))
    expect(out.startsWith('<meta http-equiv="Content-Security-Policy"')).toBe(true)
    expect(out).toContain('<p>frag</p>')
  })

  it('locks form-action to none (allow-forms sandbox cannot exfil via submit)', () => {
    const out = buildMcpAppSrcdoc(payload())
    expect(out).toContain("form-action 'none'")
  })

  it('carries per-app resource domains into the injected policy', () => {
    const out = buildMcpAppSrcdoc(payload({ csp: { resourceDomains: ['https://esm.sh'] } }))
    expect(out).toContain('https://esm.sh')
  })
})
