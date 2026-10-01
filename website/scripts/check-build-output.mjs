/**
 * Build-output guard for the relocatable dashboard runtime (R1-A), asset half.
 *
 * NOTE: no `#!` shebang — this module's analyzers are imported by
 * `src/test/buildOutputRelocatable.test.ts`, and a src-imported script that
 * opens with a shebang breaks the Windows Vite transform (see
 * `src/test/scriptsShebang.test.ts`). It runs as `node scripts/check-build-output.mjs`.
 *
 * The runtime seam (`src/lib/dashboardRuntime.ts`) relocates the SPA's own HTTP,
 * WebSocket, router and navigation URLs. This guard proves the OTHER half — the
 * emitted asset graph — is relocatable too: the same `dist/` can be served at the
 * origin root (`direct`) or under a capability prefix (`/instance-pane/<cap>/`,
 * the future relay) with the relay only ever rewriting `index.html` (an HTML
 * transform), never the content-hashed, minified JavaScript.
 *
 * It parses the REAL build (`dist/index.html` + `dist/.vite/manifest.json` + the
 * emitted `dist/assets/*.{js,css}`) and asserts:
 *
 *  1. STABLE BASE MARKER: the entry `<script type="module">` and every
 *     `modulepreload` link in `index.html` are ROOT-ABSOLUTE `/assets/…`. Direct
 *     mode serves them byte-identically to the previous `base:'/'` output; the
 *     relay rewrites the single leading `/` to the capability prefix. A relative
 *     (`./…`) entry would resolve against a deep route's directory on reload and
 *     404 — the regression this guards.
 *  2. NO `<base>` ELEMENT: a `<base href>` would retarget in-document SVG
 *     `url(#id)` fragment references and `#hash` anchors, silently breaking
 *     gradients/masks/clip-paths and same-page links. The marker is an absolute
 *     path prefix precisely so no `<base>` is needed.
 *  3. NO EMITTED ASSET ESCAPES: no emitted `.js`/`.css` bakes in a root-absolute
 *     content-hashed asset URL (`/assets/<file>.<ext>`, `url(/…)`). Every
 *     chunk-to-chunk import, dynamic import, worker `new URL(...)`, and CSS
 *     `url()` must be relative so it self-locates under whatever prefix served
 *     the entry. A bare `/assets/` substring with no filename (the entry-script
 *     DOM selector in `staleShellHeal.ts`) is not an asset URL and does not
 *     count — it matches under any prefix.
 *  4. RELOCATABLE MANIFEST: every manifest `file`, and every path in each
 *     record's `css`/`assets` arrays, is output-relative (no leading `/`, no
 *     URL scheme) and stays within the build.
 *
 * Usage:
 *   node scripts/check-build-output.mjs            # gate (exit 1 on a violation)
 *   node scripts/check-build-output.mjs <distDir>  # explicit dist directory
 *
 * Run AFTER `vite build`. The pure analyzers below are exported and unit-tested
 * (`src/test/buildOutputRelocatable.test.ts`) against fixtures and, when a real
 * `dist/` is present, against the true output.
 */
import { readFileSync, readdirSync, existsSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import path from 'node:path'

const WEBSITE_DIR = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')

/** Prefixes of the content-hashed, Vite-emitted asset graph that MUST self-locate. */
const EMITTED_PREFIX = 'assets'

/**
 * A baked root-absolute asset URL: a leading-slash `/assets/<file>.<ext>` sitting
 * in a string/template/`url()` context. Requires a filename WITH an extension, so
 * a bare `"/assets/"` substring (a DOM selector) or an extensionless API-ish path
 * (`/assets/provision`) does not match — only a real emitted-asset URL does.
 */
const ABS_ASSET_IN_JS_RE = /["'`(]\/assets\/[A-Za-z0-9._-]+\.[A-Za-z0-9]+/g
/** Any absolute `url(/…)` in CSS (relative `./` / `../` are the required form). */
const ABS_URL_IN_CSS_RE = /url\(\s*['"]?\/(?!\/)/g

/** Entry + modulepreload hrefs must be root-absolute `/assets/…` (the marker). */
export function analyzeIndexHtml(html) {
  const violations = []

  if (/<base(\s|>)/i.test(html)) {
    violations.push(
      'index.html contains a <base> element — it retargets in-document SVG url(#id) ' +
        'fragments and #hash anchors. Use the root-absolute asset marker instead.',
    )
  }

  const entry = /<script\b[^>]*\btype=["']module["'][^>]*\bsrc=["']([^"']+)["']/i.exec(html)
  if (!entry) {
    violations.push('index.html has no <script type="module" src="…"> entry to check.')
  } else if (!entry[1].startsWith('/' + EMITTED_PREFIX + '/')) {
    violations.push(
      `entry module src is "${entry[1]}" — must be root-absolute "/${EMITTED_PREFIX}/…" ` +
        '(the stable, relay-rewritable base marker), not relative.',
    )
  }

  const preloadRe = /<link\b[^>]*\brel=["']modulepreload["'][^>]*\bhref=["']([^"']+)["']/gi
  let m
  while ((m = preloadRe.exec(html)) !== null) {
    if (!m[1].startsWith('/' + EMITTED_PREFIX + '/')) {
      violations.push(
        `modulepreload href "${m[1]}" is not root-absolute "/${EMITTED_PREFIX}/…" — ` +
          'it would resolve against a deep route directory on reload.',
      )
    }
  }
  return violations
}

/** Scan one emitted asset's text for a root-absolute self-reference. */
export function scanEmittedAsset(rel, text) {
  const violations = []
  if (rel.endsWith('.js')) {
    const hits = text.match(ABS_ASSET_IN_JS_RE)
    if (hits) {
      violations.push(`${rel}: baked root-absolute asset URL(s) ${[...new Set(hits)].join(', ')}`)
    }
  } else if (rel.endsWith('.css')) {
    if (ABS_URL_IN_CSS_RE.test(text)) {
      violations.push(`${rel}: absolute url(/…) — CSS asset refs must be relative (./ or ../)`)
    }
  }
  return violations
}

/** Every manifest file/css/asset path must be output-relative and stay in-build. */
export function analyzeManifest(manifest) {
  const violations = []
  const check = (p, where) => {
    if (typeof p !== 'string') return
    if (p.startsWith('/') || /^[a-z]+:/i.test(p)) {
      violations.push(`manifest ${where}: "${p}" is not output-relative`)
    }
  }
  for (const [key, record] of Object.entries(manifest)) {
    if (!record || typeof record !== 'object') continue
    check(record.file, `${key}.file`)
    for (const c of record.css ?? []) check(c, `${key}.css`)
    for (const a of record.assets ?? []) check(a, `${key}.assets`)
  }
  return violations
}

/** Run every analyzer against a real dist directory. */
export function findBuildViolations(distDir) {
  const violations = []
  const indexPath = path.join(distDir, 'index.html')
  const manifestPath = path.join(distDir, '.vite', 'manifest.json')

  if (!existsSync(indexPath)) return [`no build at ${distDir} (missing index.html)`]
  violations.push(...analyzeIndexHtml(readFileSync(indexPath, 'utf8')))

  if (existsSync(manifestPath)) {
    violations.push(...analyzeManifest(JSON.parse(readFileSync(manifestPath, 'utf8'))))
  } else {
    violations.push(`no manifest at ${manifestPath} (set build.manifest: true)`)
  }

  const assetsDir = path.join(distDir, EMITTED_PREFIX)
  if (existsSync(assetsDir)) {
    for (const name of readdirSync(assetsDir)) {
      if (!/\.(js|css)$/.test(name)) continue
      const rel = `${EMITTED_PREFIX}/${name}`
      violations.push(...scanEmittedAsset(rel, readFileSync(path.join(assetsDir, name), 'utf8')))
    }
  }
  return violations
}

function main() {
  const distArg = process.argv[2]
  const distDir = distArg ? path.resolve(distArg) : path.join(WEBSITE_DIR, 'dist')
  const violations = findBuildViolations(distDir)
  if (violations.length > 0) {
    console.error('Build-output guard failed — an emitted asset is not prefix-relocatable:\n')
    for (const v of violations) console.error('  - ' + v)
    console.error(
      '\nEmitted chunks/CSS/workers must self-locate (relative), and index.html entry refs\n' +
        'must be the root-absolute /assets/ marker with no <base>. See vite.config.ts base/\n' +
        'renderBuiltUrl and scripts/check-build-output.mjs.',
    )
    process.exit(1)
  }
  console.log('Build-output guard passed.')
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  main()
}
