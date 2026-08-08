import { describe, it, expect } from 'vitest'
import {
  parseBrandingConfig,
  applyBrandingToHtml,
  escapeHtml,
  SHELL_OVERLAY_ALLOWLIST,
} from '../../scripts/lib/editionShell.mjs'

// A trimmed copy of the real shell — the tags the seam patches, in the real order.
const SHELL = [
  '<html><head>',
  '<meta name="theme-color" content="#0d0f12" />',
  '<title>Kiro Crew</title>',
  '</head><body></body></html>',
].join('\n')

describe('parseBrandingConfig', () => {
  it('accepts the documented keys', () => {
    expect(parseBrandingConfig('{"title": "Acme Crew", "themeColor": "#0055aa"}')).toEqual({
      title: 'Acme Crew',
      themeColor: '#0055aa',
    })
  })

  it('accepts a partial config', () => {
    expect(parseBrandingConfig('{"title": "Acme Crew"}')).toEqual({ title: 'Acme Crew' })
  })

  it('accepts an empty object — both keys are optional', () => {
    expect(parseBrandingConfig('{}')).toEqual({})
  })

  it('rejects malformed JSON loudly', () => {
    expect(() => parseBrandingConfig('{oops')).toThrow(/not valid JSON/)
  })

  it('rejects a non-object payload', () => {
    expect(() => parseBrandingConfig('["title"]')).toThrow(/JSON object/)
    expect(() => parseBrandingConfig('null')).toThrow(/JSON object/)
  })

  it('rejects an unknown key — the typo guard', () => {
    // A typoed key silently no-oping would ship a stock title with a green build.
    expect(() => parseBrandingConfig('{"titel": "Acme"}')).toThrow(/unknown key 'titel'/)
  })

  it('rejects non-string and empty values', () => {
    expect(() => parseBrandingConfig('{"title": 3}')).toThrow(/non-empty string/)
    expect(() => parseBrandingConfig('{"title": "  "}')).toThrow(/non-empty string/)
  })
})

describe('applyBrandingToHtml', () => {
  it('patches the title text', () => {
    const out = applyBrandingToHtml(SHELL, { title: 'Acme Crew' })
    expect(out).toContain('<title>Acme Crew</title>')
    expect(out).not.toContain('<title>Kiro Crew</title>')
  })

  it('preserves attributes on a future <title> tag', () => {
    const out = applyBrandingToHtml('<title lang="en">Kiro Crew</title>', { title: 'Acme Crew' })
    expect(out).toBe('<title lang="en">Acme Crew</title>')
  })

  it('patches the theme-color content', () => {
    const out = applyBrandingToHtml(SHELL, { themeColor: '#0055aa' })
    expect(out).toContain('content="#0055aa"')
    expect(out).not.toContain('#0d0f12')
  })

  it('tolerates reprinted attribute order on the meta tag', () => {
    // transformIndexHtml may see the tag after another transform reprinted it.
    const reprinted = '<meta content="#0d0f12" name="theme-color">'
    const out = applyBrandingToHtml(reprinted, { themeColor: '#0055aa' })
    expect(out).toContain('#0055aa')
  })

  it('HTML-escapes interpolated values', () => {
    // nosemgrep: javascript.lang.security.audit.unknown-value-with-script-tag.unknown-value-with-script-tag — the "attack" string is a hardcoded fixture; the assertion is that it comes out escaped
    const out = applyBrandingToHtml(SHELL, { title: 'A<script>x</script>' })
    expect(out).toContain('&lt;script&gt;')
    expect(out).not.toContain('<script>x')
  })

  it('inserts replacement-pattern metacharacters literally', () => {
    // String.replace would expand $1/$& in a replacement STRING — a title
    // like "AI for $1" must come through byte-for-byte, not as a capture ref.
    const out = applyBrandingToHtml(SHELL, { title: 'AI for $1 & co. $&' })
    expect(out).toContain('<title>AI for $1 &amp; co. $&amp;</title>')
    const color = applyBrandingToHtml(SHELL, { themeColor: '$&' })
    expect(color).toContain('content="$&amp;"')
  })

  it('leaves untouched fields alone', () => {
    const out = applyBrandingToHtml(SHELL, { title: 'Acme Crew' })
    expect(out).toContain('content="#0d0f12"')
  })

  it('fails loudly when a targeted tag is missing', () => {
    // If upstream restructures the shell, the edition build must break — a
    // quiet stock title on a green build is the failure mode the seam bans.
    expect(() => applyBrandingToHtml('<html></html>', { title: 'X' })).toThrow(/no <title>/)
    expect(() => applyBrandingToHtml('<html></html>', { themeColor: '#fff' })).toThrow(
      /no <meta name="theme-color">/
    )
  })
})

describe('escapeHtml', () => {
  it('escapes the five HTML metacharacters', () => {
    expect(escapeHtml(`<&>"'`)).toBe('&lt;&amp;&gt;&quot;&#39;')
  })
})

describe('SHELL_OVERLAY_ALLOWLIST', () => {
  it('covers exactly the pre-boot shell assets', () => {
    // Widening this list widens what an edition can overwrite in dist — the
    // test makes that a conscious, reviewed change rather than a drive-by.
    expect([...SHELL_OVERLAY_ALLOWLIST].sort()).toEqual([
      'icon-192.png',
      'icon-512.png',
      'manifest.json',
    ])
  })
})
