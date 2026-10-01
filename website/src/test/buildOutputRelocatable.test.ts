/**
 * Regression tests for the build-output relocation guard
 * (`scripts/check-build-output.mjs`) — the asset half of the relocatable
 * dashboard runtime (R1-A).
 *
 * test-audit authoring gate:
 *  1. Observable behavior: the analyzers accept a build whose emitted chunks/CSS
 *     self-locate and whose index.html carries the root-absolute `/assets/`
 *     marker with no `<base>`, and reject one where an asset URL is baked
 *     root-absolute, the entry is relative, a `<base>` appears, or the manifest
 *     records a non-relative path. A final case runs them against the REAL
 *     `dist/` when a build is present.
 *  2. Credible regression: if the emitted graph stopped self-locating (a chunk
 *     baking `/assets/x.js`, or a `<base href>` slipping in), the same bundle
 *     could no longer be served under the capability prefix without rewriting
 *     minified JS — exactly the property the relay depends on.
 *  3. Existing coverage gap: nothing else parses the built output to prove asset
 *     relocatability; the runtime-seam guard only covers source URL call sites.
 *  4. Production seam: the analyzers are pure functions exported from the guard
 *     script; no test-only production seam is introduced.
 */
import { describe, it, expect } from 'vitest'
import { existsSync, readFileSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import {
  analyzeIndexHtml,
  scanEmittedAsset,
  analyzeManifest,
  findBuildViolations,
} from '../../scripts/check-build-output.mjs'

const WEBSITE_DIR = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..')
const DIST_DIR = path.join(WEBSITE_DIR, 'dist')

const ENTRY = '<script type="module" crossorigin src="/assets/main-abc123.js"></script>'
const PRELOAD = '<link rel="modulepreload" crossorigin href="/assets/vendor-react-def456.js">'

describe('analyzeIndexHtml — the stable base marker', () => {
  it('accepts a root-absolute entry + modulepreload with no <base>', () => {
    expect(analyzeIndexHtml(`<head>${PRELOAD}</head><body>${ENTRY}</body>`)).toEqual([])
  })

  it('rejects a <base> element (it would retarget SVG url(#id) + #hash)', () => {
    const v = analyzeIndexHtml(`<head><base href="/instance-pane/K/">${ENTRY}</head>`)
    expect(v).toHaveLength(1)
    expect(v[0]).toContain('<base>')
  })

  it('rejects a relative entry src (breaks a deep-route reload)', () => {
    const v = analyzeIndexHtml('<script type="module" src="./assets/main-abc.js"></script>')
    expect(v.some((x) => x.includes('root-absolute'))).toBe(true)
  })

  it('rejects a relative modulepreload href', () => {
    const v = analyzeIndexHtml(
      `${ENTRY}<link rel="modulepreload" href="./assets/vendor-x.js">`,
    )
    expect(v.some((x) => x.includes('modulepreload'))).toBe(true)
  })

  it('leaves in-document SVG fragments and #hash anchors intact (no <base> ⇒ no violation)', () => {
    // The whole reason the marker is an absolute path and not a <base>: these
    // references must keep resolving against the document, not a base href.
    const html =
      `<head>${ENTRY}</head><body>` +
      '<svg><rect fill="url(#grad)"/><use href="#icon"/></svg>' +
      '<a href="#section-2">jump</a></body>'
    expect(analyzeIndexHtml(html)).toEqual([])
  })
})

describe('scanEmittedAsset — no emitted asset escapes the prefix', () => {
  it('flags a JS chunk that bakes a root-absolute asset URL', () => {
    expect(scanEmittedAsset('assets/x.js', 'import("/assets/lazy-abc123.js")')).toHaveLength(1)
    expect(scanEmittedAsset('assets/x.js', 'new URL("/assets/w-def.js",import.meta.url)')).toHaveLength(1)
  })

  it('accepts a JS chunk that self-locates (relative sibling refs)', () => {
    expect(scanEmittedAsset('assets/x.js', 'import("./lazy-abc123.js")')).toEqual([])
    expect(scanEmittedAsset('assets/x.js', 'new URL("hljsWorker-x.js",import.meta.url)')).toEqual([])
  })

  it('does NOT flag a bare /assets/ substring that is not an asset URL', () => {
    // staleShellHeal.ts: a DOM selector + an includes() check. `/assets/` here is
    // followed by a quote or `)`, not a filename — it matches under any prefix.
    expect(scanEmittedAsset('assets/x.js', 'querySelector(`[src*="/assets/"]`)')).toEqual([])
    expect(scanEmittedAsset('assets/x.js', "runningEntry.includes('/assets/')")).toEqual([])
    // An extensionless app endpoint (pptx-maker `/assets/provision`) is not an
    // emitted-asset URL either.
    expect(scanEmittedAsset('assets/x.js', 'V(`/assets/provision${f}`)')).toEqual([])
  })

  it('flags absolute url(/…) in CSS but accepts relative url(./…) / url(../…)', () => {
    expect(scanEmittedAsset('assets/x.css', '@font-face{src:url(/fonts/a.woff2)}')).toHaveLength(1)
    expect(scanEmittedAsset('assets/x.css', '@font-face{src:url(./a-h.woff2)}')).toEqual([])
    expect(scanEmittedAsset('assets/x.css', '@font-face{src:url(../fonts/b.woff2)}')).toEqual([])
  })
})

describe('analyzeManifest — the emitted graph is output-relative', () => {
  it('accepts relative file/css/asset paths', () => {
    expect(
      analyzeManifest({
        'index.html': { file: 'assets/main-abc.js', css: ['assets/main-abc.css'], isEntry: true },
        '_vendor.js': { file: 'assets/vendor-def.js', assets: ['assets/logo-x.svg'] },
      }),
    ).toEqual([])
  })

  it('rejects a leading-slash or scheme-qualified path', () => {
    expect(analyzeManifest({ e: { file: '/assets/main-abc.js' } })).toHaveLength(1)
    expect(analyzeManifest({ e: { file: 'assets/x.js', css: ['/assets/x.css'] } })).toHaveLength(1)
    expect(analyzeManifest({ e: { file: 'https://cdn.example/x.js' } })).toHaveLength(1)
  })
})

describe('findBuildViolations — against the REAL build when present', () => {
  const built = existsSync(path.join(DIST_DIR, 'index.html'))
  it.runIf(built)('the emitted dist/ is fully prefix-relocatable', () => {
    expect(findBuildViolations(DIST_DIR)).toEqual([])
  })
  it.skipIf(built)('(skipped: no dist/ — run `npm run build` to exercise the real-output proof)', () => {
    // Placeholder so the suite reports the skip explicitly rather than vanishing.
    expect(existsSync(DIST_DIR)).toBe(false)
  })

  it('the real index.html, when present, carries the marker and no <base>', () => {
    if (!built) return
    const html = readFileSync(path.join(DIST_DIR, 'index.html'), 'utf8')
    expect(analyzeIndexHtml(html)).toEqual([])
    expect(/<base(\s|>)/i.test(html)).toBe(false)
  })
})
