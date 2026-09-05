// Test DOM is happy-dom (see `test.environment` below). It replaced jsdom to
// drop the transitively-deprecated whatwg-encoding dep; happy-dom also needs
// only Node>=20 (CI's version). happy-dom does REAL network I/O for iframe
// navigation + eager <script src> loading; that is neutralized in the msw
// layer — the catch-all fallback handler in integration/mocks/server.ts answers
// otherwise-unmatched requests before any dial — with happy-dom's official
// disable-loading settings (below) as defense-in-depth. See both notes there.
import { fileURLToPath, URL } from 'node:url'
import { defineConfig, type Plugin } from 'vite'
/// <reference types="vitest" />
import react from '@vitejs/plugin-react'
import { readFileSync, writeFileSync, existsSync, readdirSync } from 'fs'
import { execSync } from 'child_process'
import http from 'http'
import path from 'path'
import {
  MERMAID_RUNTIME_PATH,
  MERMAID_RUNTIME_SRC,
  TAILWIND_RUNTIME_PATH,
  TAILWIND_RUNTIME_SRC,
} from './src/lib/vendorPaths'
import { precompressPlugin } from './scripts/precompress.mjs'
import {
  parseBrandingConfig,
  applyBrandingToHtml,
  SHELL_OVERLAY_ALLOWLIST,
  verifyOverlayBytes,
} from './scripts/lib/editionShell.mjs'

/** Shape produced by parseBrandingConfig (editionShell.mjs is untyped .mjs). */
type EditionBranding = { title?: string; themeColor?: string }

const pkg = JSON.parse(readFileSync('./package.json', 'utf-8'))
const backendPort = process.env.KIROCREW_PORT || 5476

/**
 * Dev-only plugin: when the browser hits `/?token=xxx`, proxy that request
 * to the backend so the `mc_token` cookie gets set, then redirect back to
 * the Vite dev server without the token param.
 */
function tokenProxyPlugin(): Plugin {
  return {
    name: 'kirocrew-token-proxy',
    configureServer(server) {
      server.middlewares.use((req, res, next) => {
        const url = new URL(req.url || '/', `http://localhost:3000`)
        if (url.pathname === '/' && url.searchParams.has('token')) {
          // Forward the request to the backend to validate token & get Set-Cookie
          const backendUrl = `http://localhost:${backendPort}/?token=${url.searchParams.get('token')}`
          http.get(backendUrl, (backendRes) => {
            // Grab Set-Cookie headers from the backend response
            const cookies = backendRes.headers['set-cookie']
            if (cookies) {
              res.setHeader('Set-Cookie', cookies)
            }
            // Redirect to clean URL so Vite serves the SPA
            res.writeHead(302, { Location: '/' })
            res.end()
            backendRes.resume()
          }).on('error', () => {
            // Backend unreachable — fall through to Vite
            next()
          })
          return
        }
        next()
      })
    },
  }
}

/**
 * Build-time plugin: injects a <script type="importmap"> into index.html
 * that maps bare module specifiers to vendor stubs in /vendor/*.mjs.
 *
 * The stubs are hand-written files in public/vendor/ that read from
 * window.__kirocrew_modules (registered by shared-modules.ts at startup).
 * This approach is bundler-agnostic — stubs never go through Rollup,
 * so exports are never renamed or tree-shaken.
 */
function appImportMapPlugin(): Plugin {
  return {
    name: 'kirocrew-app-importmap',
    enforce: 'post',
    transformIndexHtml: {
      order: 'post',
      handler(html) {
        const importMap = {
          imports: {
            'react': '/vendor/react.mjs',
            'react-dom': '/vendor/react-dom.mjs',
            'react-dom/client': '/vendor/react-dom-client.mjs',
            'react/jsx-runtime': '/vendor/react-jsx-runtime.mjs',
            '@kirocrew/app-sdk': '/vendor/kirocrew-app-sdk.mjs',
            '@kirocrew/app-sdk/ui': '/vendor/kirocrew-ui.mjs',
            'lucide-react': '/vendor/lucide-react.mjs',
          },
        }
        const tag = `<script type="importmap">${JSON.stringify(importMap)}</script>`
        return html.replace('<head>', `<head>\n  ${tag}`)
      },
    },
  }
}

/**
 * Serve third-party browser runtimes from the dashboard's own origin under
 * `/vendor/`. Each is copied from its tracked npm dependency at build time (NOT
 * a committed blob), satisfying software-supply-chain policy.
 *
 * Both entries exist for the same reason: a sandboxed null-origin iframe cannot
 * be allowed to reach a public CDN.
 *   - Tailwind v4 replaces cdn.tailwindcss.com, which restricted network
 *     environments block — crashing the whole page on artifact render.
 *   - Mermaid backs the Meetings sketch artist, whose frame is served with
 *     `connect-src 'none'`; same-origin is the only way that frame can draw a
 *     diagram, so the app renders offline and a prompt-injected document has no
 *     network egress to exfiltrate meeting content through.
 *
 * One table-driven plugin rather than one plugin per runtime: the dev-serve and
 * build-emit halves are identical apart from the paths.
 */
function vendorRuntimePlugin(): Plugin {
  const RUNTIMES: ReadonlyArray<{ servePath: string; src: string }> = [
    { servePath: TAILWIND_RUNTIME_PATH, src: TAILWIND_RUNTIME_SRC },
    { servePath: MERMAID_RUNTIME_PATH, src: MERMAID_RUNTIME_SRC },
  ]
  return {
    name: 'kirocrew-vendor-runtimes',
    // Dev: the build output doesn't exist, so serve straight from node_modules.
    configureServer(server) {
      server.middlewares.use((req, res, next) => {
        const url = (req.url || '').split('?')[0]
        const hit = RUNTIMES.find((r) => r.servePath === url)
        if (!hit) return next()
        res.setHeader('Content-Type', 'text/javascript; charset=utf-8')
        // The consumers of these two files are sandboxed srcdoc iframes — null
        // origin — whose <script> tags load with crossorigin="anonymous", which
        // makes this header MANDATORY (without it the load hard-fails at the
        // CORS layer and dev-mode widgets/sketches render unstyled). Vite's own
        // cors default cannot supply it: since Vite 6.2 the default is a
        // localhost-origin allowlist that `Origin: null` does not match. Scoped
        // to exactly these two public runtime files, mirroring the gateway's
        // /vendor/-only grant in production.
        res.setHeader('Access-Control-Allow-Origin', '*')
        res.end(readFileSync(hit.src))
      })
    },
    // Build: emit into dist/vendor/, served same-origin like the /vendor/*.mjs stubs.
    generateBundle() {
      for (const runtime of RUNTIMES) {
        this.emitFile({
          type: 'asset',
          fileName: runtime.servePath.replace(/^\//, ''),
          source: readFileSync(runtime.src),
        })
      }
    },
  }
}

/**
 * Post-build plugin: replaces %%SW_BUILD_HASH%% in the copied public/sw.js
 * with a stable build-time identifier (version + git SHA). This runs during
 * `vite build` only (not dev server). The public/ directory is copied
 * verbatim by Vite so `define` replacements don't apply to it.
 */
/**
 * Self-host Excalidraw's canvas fonts. Without `window.EXCALIDRAW_ASSET_PATH`
 * the library resolves its lazily-loaded text-tool fonts (Excalifont, Xiaolai,
 * …) against a third-party CDN (esm.sh) — verified by a network probe against
 * a live pod — which breaks air-gapped dashboards and violates the same
 * no-network rule that `lib/excalidrawScene.ts` documents for the read-only
 * renderer. SketchDialog sets the asset path to `/vendor/excalidraw/`; this
 * plugin makes that path real: build emits every font file from the npm
 * package into `dist/vendor/excalidraw/fonts/**`, and the dev server serves
 * the same files straight from node_modules.
 */
function excalidrawFontsPlugin(): Plugin {
  const FONTS_ROOT = path.resolve(__dirname, 'node_modules/@excalidraw/excalidraw/dist/prod/fonts')
  const SERVE_PREFIX = '/vendor/excalidraw/fonts/'
  const listFonts = (): string[] => {
    const out: string[] = []
    for (const family of readdirSync(FONTS_ROOT)) {
      for (const file of readdirSync(path.join(FONTS_ROOT, family))) {
        out.push(`${family}/${file}`)
      }
    }
    return out
  }
  return {
    name: 'kirocrew-excalidraw-fonts',
    configureServer(server) {
      server.middlewares.use((req, res, next) => {
        const url = (req.url || '').split('?')[0]
        if (!url.startsWith(SERVE_PREFIX)) return next()
        const rel = decodeURIComponent(url.slice(SERVE_PREFIX.length))
        // Path-traversal guard: the joined path must stay under FONTS_ROOT.
        const abs = path.resolve(FONTS_ROOT, rel)
        if (!abs.startsWith(FONTS_ROOT + path.sep) || !existsSync(abs)) return next()
        res.setHeader('Content-Type', 'font/woff2')
        res.end(readFileSync(abs))
      })
    },
    generateBundle() {
      for (const rel of listFonts()) {
        this.emitFile({
          type: 'asset',
          fileName: `vendor/excalidraw/fonts/${rel}`,
          source: readFileSync(path.join(FONTS_ROOT, rel)),
        })
      }
    },
  }
}

function swVersionPlugin(): Plugin {
  return {
    name: 'kirocrew-sw-version',
    apply: 'build',
    closeBundle() {
      const swPath = path.resolve(__dirname, 'dist/sw.js')
      try {
        let content = readFileSync(swPath, 'utf-8')
        if (!content.includes('%%SW_BUILD_HASH%%')) {
          // Already injected by an earlier pass (vite may run multiple
          // rollup passes per build; dist/sw.js can also be a previous
          // build's output when this pass doesn't copy publicDir).
          // Idempotent skip — but if the const line is missing entirely,
          // the placeholder was renamed/removed in sw.js: fail loudly.
          if (!/const CACHE_VERSION = '[^'%]+'/.test(content)) {
            throw new Error(
              'swVersionPlugin: neither placeholder %%SW_BUILD_HASH%% nor an injected CACHE_VERSION found in dist/sw.js'
            )
          }
          return
        }
        // Use version + git SHA for reproducibility: identical source = identical hash.
        // Falls back to version alone if git is unavailable (CI edge case).
        let sha = ''
        try { sha = execSync('git rev-parse --short HEAD', { encoding: 'utf-8' }).trim() } catch {}
        // A dirty tree means HEAD does not describe these bytes: two builds
        // from different uncommitted states would stamp the SAME version, so
        // the service worker never byte-changes and clients keep the previous
        // deploy's shell cache alive. Suffix a timestamp so every dirty build
        // is its own cache generation; clean builds stay reproducible.
        let dirty = ''
        try {
          if (execSync('git status --porcelain', { encoding: 'utf-8' }).trim()) {
            dirty = `-dev${Date.now().toString(36)}`
          }
        } catch {}
        const buildHash = (sha ? `${pkg.version}-${sha}` : pkg.version) + dirty
        content = content.replace('%%SW_BUILD_HASH%%', buildHash)
        writeFileSync(swPath, content)
      } catch (e: unknown) {
        // Only tolerate sw.js not existing (library mode, test builds).
        // Anything else is a real bug — surface it.
        if ((e as NodeJS.ErrnoException).code !== 'ENOENT') throw e
      }
    },
  }
}

/**
 * Edition-extension seam: resolves the virtual module `virtual:kirocrew-edition`
 * — imported once by `src/extensions.ts` — to a downstream edition's own
 * composition-root module, WITHOUT the edition having to overlay/shadow any core
 * file.
 *
 * - `KIROCREW_EDITION_DIR` unset (the stock OSS build): resolves to an INERT
 *   empty module (`export {}`), so the stock build registers nothing and is
 *   byte-identical to having no seam at all.
 * - `KIROCREW_EDITION_DIR=<abs path>` set (a downstream edition build): resolves
 *   to `<dir>/extensions.tsx` (or `.ts`) — the edition's own file, living in the
 *   edition's own repo. Its `register*()` calls + component imports compile into
 *   the SPA through the SAME vite/rollup pass as the core, so the edition never
 *   copies a core file. The edition dir is added to the watch/allow list so its
 *   sources resolve.
 *
 * This is the frontend analogue of the backend CPP seam: one core, two editions,
 * the core never importing an edition — the edition is injected by config at
 * build time, never by shadowing `main.tsx`/`extensions.ts`.
 */
function editionExtensionPlugin(): Plugin {
  const VIRTUAL_ID = 'virtual:kirocrew-edition'
  const RESOLVED_ID = '\0' + VIRTUAL_ID
  const editionDir = process.env.KIROCREW_EDITION_DIR
  // FAIL-CLOSED by default: composing a downstream edition (which compiles that
  // edition's proprietary sources into website/dist — the dist staged into the
  // public OSS wheel) requires an EXPLICIT opt-in, KIROCREW_ALLOW_EDITION=1.
  // Every pipeline — including release/publish — is therefore protected by
  // default with NO "remember to set a guard var" dependency: an inherited
  // KIROCREW_EDITION_DIR without the opt-in FAILS THE BUILD rather than
  // silently contaminating a public artifact (a one-way door — a published
  // release cannot be unpublished). Only the edition's own build.sh sets the
  // opt-in. Unsetting the opt-in can never weaken this; forgetting to set it
  // only ever fails safe (stock).
  if (editionDir && process.env.KIROCREW_ALLOW_EDITION !== '1') {
    throw new Error(
      `KIROCREW_EDITION_DIR is set to '${editionDir}' but KIROCREW_ALLOW_EDITION=1 is not. ` +
        'Edition composition is opt-in (fail-closed) so a stray env var cannot contaminate a ' +
        'stock/release build. Set KIROCREW_ALLOW_EDITION=1 in the edition build, or unset ' +
        'KIROCREW_EDITION_DIR for a stock build.'
    )
  }
  // Resolve the edition's composition root eagerly so a MISCONFIGURED dir
  // (set but missing the file) fails the build loudly rather than silently
  // degrading to the stock SPA — a silent degrade would ship an edition build
  // with none of its edition behavior.
  let editionEntry: string | null = null
  if (editionDir) {
    const abs = path.resolve(editionDir)
    const candidate = ['extensions.tsx', 'extensions.ts'].map((f) => path.join(abs, f)).find(existsSync)
    if (!candidate) {
      throw new Error(
        `KIROCREW_EDITION_DIR is set to '${editionDir}' but no extensions.tsx/.ts exists there. ` +
          'Unset it for the stock build, or point it at the edition composition root.'
      )
    }
    editionEntry = candidate
    // Loud, unmissable self-identification: an inherited KIROCREW_EDITION_DIR
    // would otherwise SILENTLY compile a downstream edition's (proprietary)
    // sources into website/dist — which is staged into the Python package. In
    // this public OSS repo that is an IP-contamination hazard with no trace, so
    // every edition-mode build/test run must announce itself in local + CI logs.
    console.warn(
      `\n[kirocrew-edition] ⚠ BUILDING WITH EDITION COMPOSITION ROOT: ${editionEntry}\n` +
        '[kirocrew-edition] the resulting dist is EDITION-composed, NOT a stock OSS build. ' +
        'Unset KIROCREW_EDITION_DIR for a stock build.\n'
    )
  }
  // Pre-boot shell branding, resolved eagerly like the composition root so a
  // malformed branding.json fails the build at startup, not mid-bundle. The
  // eager read also means a dev server restart is needed after editing
  // branding.json — consistent with how the composition root itself resolves.
  // Optional: an edition without one keeps the stock title/theme-color.
  let branding: EditionBranding = {}
  let overlayFiles: string[] = []
  if (editionEntry) {
    const abs = path.dirname(editionEntry)
    const brandingPath = path.join(abs, 'branding.json')
    if (existsSync(brandingPath)) {
      try {
        branding = parseBrandingConfig(readFileSync(brandingPath, 'utf-8'))
      } catch (e: unknown) {
        throw new Error(`[kirocrew-edition] ${brandingPath}: ${(e as Error).message}`)
      }
    }
    // Only allowlisted shell assets overlay the stock public/ copies; anything
    // else in the edition's public/ fails the build (fail-loud beats a file
    // that looks deployed but never ships). Deliberately flat: a subdirectory
    // is rejected like any other stray entry, so the allowlist stays a list of
    // exact filenames rather than a path-matching scheme. OS junk dotfiles
    // (.DS_Store and friends) are skipped, not rejected — the OS creates them
    // behind the author's back, so failing on them would be noise, not safety.
    const publicDir = path.join(abs, 'public')
    if (existsSync(publicDir)) {
      const entries = readdirSync(publicDir, { withFileTypes: true }).filter(
        (d) => !d.name.startsWith('.')
      )
      const strays = entries.filter(
        (d) => !d.isFile() || !SHELL_OVERLAY_ALLOWLIST.includes(d.name)
      )
      if (strays.length > 0) {
        throw new Error(
          `[kirocrew-edition] ${publicDir} contains entries outside the shell-overlay allowlist ` +
            `(${SHELL_OVERLAY_ALLOWLIST.join(', ')}), or non-files: ` +
            `${strays.map((d) => d.name).join(', ')}. ` +
            'The allowlist is what keeps an edition from overwriting index.html, sw.js, or vendor/*.'
        )
      }
      overlayFiles = entries.map((d) => d.name)
    }
  }
  return {
    name: 'kirocrew-edition-extension',
    enforce: 'pre',
    // Patch the pre-boot shell (<title>, <meta name="theme-color">) from the
    // edition's branding.json. registerThemeBranding() can only retitle the tab
    // after React mounts; this covers what the browser shows before that (and
    // what a PWA install dialog samples). order:'pre' runs before Vite's own
    // HTML transform reprints the tags.
    transformIndexHtml: {
      order: 'pre',
      handler(html: string, ctx: { path: string }) {
        if (!branding.title && !branding.themeColor) return html
        // Multi-page build: app panels (src/apps/*/panel.html) come through
        // this hook too. The pre-boot shell is the root index.html only.
        if (ctx.path !== '/index.html') return html
        return applyBrandingToHtml(html, branding)
      },
    },
    // Overlay the allowlisted shell assets (PWA manifest + icons) edition-wins.
    // Emitted through the bundler so the files are visible to other plugins,
    // rather than copied over dist after the fact. An emitted asset with a
    // pinned fileName takes precedence over the same-named publicDir copy
    // (verified against this config on Vite 8 / Rolldown; the negative case —
    // publicDir winning — would surface immediately as stock bytes in dist).
    // Build-only by nature of generateBundle: the dev server keeps serving the
    // stock public/ files, which the seam docs call out as a known limitation.
    generateBundle() {
      for (const file of overlayFiles) {
        this.emitFile({
          type: 'asset',
          fileName: file,
          source: readFileSync(path.join(path.dirname(editionEntry as string), 'public', file)),
        })
      }
    },
    // Enforce the emitFile-over-publicDir precedence generateBundle relies on.
    // The overlay above assumes an emitted asset with a pinned fileName wins
    // over the same-named publicDir copy; that is empirical (Vite 8 / Rolldown),
    // not a documented bundler contract. writeBundle runs AFTER the bundle is
    // written to disk, so it sees what actually landed: byte-compare each
    // overlaid dist file against its edition source and fail loud on a mismatch
    // or a missing file. A future bundler upgrade that flips the precedence
    // would silently ship stock icons/manifest on a green build — and edition
    // builds run downstream, outside this repo's CI, so this check is the only
    // thing that would catch it. No-op for stock builds (overlayFiles is empty).
    writeBundle(options) {
      if (overlayFiles.length === 0) return
      verifyOverlayBytes({
        distDir: options.dir ?? path.resolve('dist'),
        editionPublicDir: path.join(path.dirname(editionEntry as string), 'public'),
        overlayFiles,
        readFile: readFileSync,
        join: path.join,
      })
    },
    config() {
      if (editionDir) {
        // Let vite's dev server serve/resolve files from outside the project
        // root (the edition dir lives in a sibling repo). ADD the edition dir to
        // the allow list — include the core project root explicitly because
        // providing a custom `server.fs.allow` DISABLES vite's workspace-root
        // auto-detection (per the vite docs), which would otherwise stop core
        // `website/` files from resolving in dev.
        return { server: { fs: { allow: [__dirname, path.resolve(editionDir)] } } }
      }
      return {}
    },
    resolveId(id) {
      if (id === VIRTUAL_ID) return RESOLVED_ID
      return null
    },
    load(id) {
      if (id !== RESOLVED_ID) return null
      if (editionEntry) {
        // Re-export the edition's composition root so its module-load
        // side effects (the register*() calls) run exactly once. Emit a
        // forward-slash path: on Windows editionEntry contains backslashes
        // (path.resolve/join), which are invalid escape sequences in a JS
        // import specifier — normalize to posix separators.
        const spec = editionEntry.split(path.sep).join('/')
        return `import ${JSON.stringify(spec)}\nexport {}\n`
      }
      // Stock OSS build: inert.
      return 'export {}\n'
    },
  }
}

/**
 * Debug-only plugin: writes `dist/bundle-report.json` describing what the build
 * emitted and which packages contribute the weight.
 *
 * Inert unless the build runs in `analyze` mode (`vite build --mode analyze`,
 * wired up as `npm run analyze`), so a normal `npm run build` is byte-for-byte
 * unaffected and never pays the walk. Mode is read here rather than by turning
 * the whole config into a function, to keep this to one entry in `plugins`.
 *
 * Note there is no analyzer dependency: Rollup already reports the rendered size
 * of every module it emitted, so the numbers are computed from data the bundler
 * hands us. That keeps a build-time package (and its transitive tree) out of the
 * repo for something it can already answer.
 */
function bundleReportPlugin(): Plugin {
  const REPORT_MODE = 'analyze'
  let active = false
  return {
    name: 'kirocrew-bundle-report',
    apply: 'build',
    configResolved(resolved) {
      active = resolved.mode === REPORT_MODE
    },
    async generateBundle(_options, bundle) {
      if (!active) return
      // Imported lazily so a normal build never loads the helper at all.
      const { summarizeBundle } = await import('./scripts/lib/bundleReport.mjs')
      const summary = summarizeBundle(bundle)
      // Emitted through Rollup rather than written directly so it lands in the
      // configured outDir wherever that points.
      this.emitFile({
        type: 'asset',
        fileName: 'bundle-report.json',
        source: JSON.stringify(summary, null, 2),
      })
    },
  }
}

/**
 * App window entries: discovery + dev-server URL rewrite.
 *
 * An app may ship standalone HTML windows at `src/apps/<app>/<name>.html`
 * (separate Vite bundles, loaded by a shell window rather than the SPA
 * router). Discovery is the filesystem — there is no registration list to
 * keep in sync. Each entry is served at `/app-windows/<app>/<name>.html`:
 * `dashboard/server.py` registers that route in production from the same
 * convention, and the plugin below answers it in dev, so exactly one URL
 * contract holds across dev, production and the loading shell's tests.
 *
 * The URL keeps the `<app>` / `<name>` boundary the filesystem has, so the
 * rewrite is a straight prefix swap. The earlier flat `/<app>-<name>.html`
 * could not do that: with hyphens legal in both halves the split was a GUESS,
 * and this middleware used to try each hyphen position and take the first file
 * that existed — silently serving another app's window rather than refusing.
 *
 * A rewrite rather than a redirect: the shell loads these into a
 * BrowserWindow, and a 30x would leave the window's URL pointing somewhere
 * other than what the caller asked for, which instance-switch logic
 * compares against.
 */
const APP_WINDOWS_ROOT = fileURLToPath(new URL('./src/apps', import.meta.url))
/** Keep in sync with `APP_WINDOW_URL_PREFIX` in `dashboard/server.py`. */
const APP_WINDOW_URL_PREFIX = 'app-windows'

function appWindowEntries(): Record<string, string> {
  const entries: Record<string, string> = {}
  if (!existsSync(APP_WINDOWS_ROOT)) return entries
  for (const dirent of readdirSync(APP_WINDOWS_ROOT, { withFileTypes: true })) {
    if (!dirent.isDirectory()) continue
    const dir = path.join(APP_WINDOWS_ROOT, dirent.name)
    for (const file of readdirSync(dir)) {
      if (!file.endsWith('.html')) continue
      const name = file.slice(0, -'.html'.length)
      entries[`${APP_WINDOW_URL_PREFIX}/${dirent.name}/${name}`] = path.join(dir, file)
    }
  }
  return entries
}

function appWindowUrls(): Plugin {
  return {
    name: 'app-window-urls',
    configureServer(server) {
      server.middlewares.use((req, _res, next) => {
        const [reqPath, query] = (req.url ?? '').split('?')
        // Two bounded segments, no dots and no slashes inside either, so nothing
        // resembling `..` or a nested path can be spelled; existence under
        // src/apps/ is the second gate.
        const match = new RegExp(
          `^/${APP_WINDOW_URL_PREFIX}/([a-z0-9_-]+)/([a-z0-9_-]+)\\.html$`,
        ).exec(reqPath)
        if (match) {
          const [, app, name] = match
          if (existsSync(path.join(APP_WINDOWS_ROOT, app, `${name}.html`))) {
            req.url = `/src/apps/${app}/${name}.html${query ? `?${query}` : ''}`
          }
        }
        next()
      })
    },
  }
}

export default defineConfig({
  plugins: [react(), tokenProxyPlugin(), appImportMapPlugin(), vendorRuntimePlugin(), excalidrawFontsPlugin(), swVersionPlugin(), editionExtensionPlugin(), bundleReportPlugin(), appWindowUrls(), precompressPlugin()],
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
    },
    // Force a SINGLE instance of every CONTEXT-CARRYING singleton across the
    // bundle. A KIROCREW_EDITION_DIR in a separate repo may resolve these from
    // ITS OWN node_modules; a second copy binds an edition component's hooks to
    // a DIFFERENT context instance than the core's providers — "Invalid hook
    // call" (react), "No QueryClient set" / null router context / silently empty
    // data (the rest) — only at runtime, only in the out-of-repo edition build.
    // Dedupe the libraries the core's provider tree owns; harmless in the stock
    // single-node_modules build. (See website/AGENTS.md — edition peer-dep rule.)
    dedupe: [
      'react',
      'react-dom',
      'react-redux',
      'react-router',
      'react-router-dom',
      '@tanstack/react-query',
      'framer-motion',
    ],
  },
  define: {
    __APP_VERSION__: JSON.stringify(pkg.version),
  },
  test: {
    globals: true,
    environment: 'happy-dom',
    // Pin TZ so date/time assertions are deterministic regardless of the
    // contributor's system timezone. CI runs in UTC; without this, tests that
    // compare Intl.DateTimeFormat output against toLocale*() defaults diverge
    // on single-digit hours visible only outside UTC (e.g. Pacific/Kiritimati).
    env: { TZ: 'UTC' },
    // happy-dom (unlike jsdom) actively NAVIGATES iframes and LOADS <script src>.
    // WidgetFrame renders a live <iframe src={blobUrl}> whose page carries a
    // same-origin <script src=".../tailwindcss-browser.js">, which happy-dom
    // would fetch over the network (ECONNREFUSED spam + an unclean socket
    // teardown that can crash the fork worker). The PRIMARY guard is the msw
    // catch-all fallback in integration/mocks/server.ts (answers those requests
    // with an empty 200 before any dial). These settings are cheap
    // DEFENSE-IN-DEPTH via happy-dom's OFFICIAL config API (not a reach into its
    // internals): if a request ever slips past msw, happy-dom still declines to
    // load it. We test the DOM/serialization contract, never the sandboxed
    // widget runtime, so disabling iframe nav + sub-resource loading + JS eval
    // costs nothing.
    environmentOptions: {
      happyDOM: {
        // Serve the test document on the gateway's real default port. happy-dom
        // otherwise defaults to localhost:3000, which is one of the Web Preview
        // panel's own dev-server quick-picks — and the panel refuses to frame a
        // target on the dashboard's own port (it can only ever be this gateway,
        // which forbids being embedded). Matching production keeps "the
        // dashboard" and "a dev server" distinguishable in tests.
        url: 'http://localhost:6776/',
        settings: {
          disableIframePageLoading: true,
          disableJavaScriptFileLoading: true,
          disableJavaScriptEvaluation: true,
          disableCSSFileLoading: true,
          // Resolve a declined resource load as a silent success instead of
          // rejecting with a NotSupportedError. happy-dom runs the load
          // asynchronously on DOM insertion, so that rejection is orphaned —
          // it escapes the test that inserted the node and vitest counts it as
          // a run-level unhandled error, failing the whole shard even when every
          // assertion passed. We assert the serialized DOM, never the sandboxed
          // widget runtime, so a no-op success preserves the contract under test.
          handleDisabledFileLoadingAsSuccess: true,
          // Never follow a link or form navigation over the network. An
          // un-intercepted `<a href>` click — e.g. an artifact anchor a test
          // clicks with no onArtifactOpen handler — otherwise makes happy-dom
          // dial the document origin for real; that fetch outlives the test and
          // its ECONNREFUSED lands after msw teardown as another orphaned
          // rejection. Disabling navigation still falls back to setting the URL
          // (no dial), so tests that read location after a click keep working.
          navigation: {
            disableMainFrameNavigation: true,
            disableChildFrameNavigation: true,
          },
        },
      },
    },
    setupFiles: './integration/setup.ts',
    css: true,
    pool: 'forks',  // More stable than threads on ARM64 build fleet (avoids ERR_IPC_CHANNEL_CLOSED)
    // Bound fork memory. Without a cap, vitest spawns one worker per core
    // (os.availableParallelism); on a high-core fleet the aggregate RSS of that
    // many full Node heaps — each retaining v8 coverage maps + happy-dom DOM
    // state across the ~950 files it touches — exceeds host RAM under
    // ``--coverage``. The kernel then OOM-kills a worker, which vitest surfaces
    // as "Worker exited unexpectedly" (1 unhandled error → the job fails) even
    // though every test passed. maxWorkers caps concurrency regardless of core
    // count, and the per-worker --max-old-space-size gives each fork a heap
    // ceiling that fails a genuine leak loudly instead of dragging the host down.
    // (Vitest 4 pool rework: these are top-level, not poolOptions; minWorkers
    // was removed — only maxWorkers has effect.)
    //
    // 3, not 2: `ubuntu-latest` has 4 vCPU (this workflow's own backend job
    // documents "4 cores via -n auto"), so 2 left half of every shard's runner
    // idle. 3 is exactly what vitest itself would pick there when uncapped
    // (`max(availableParallelism - 1, 1)` in run mode), leaving a core for the
    // main process — so this raises throughput without oversubscribing, and the
    // cap still protects the high-core fleet the paragraph above is about.
    //
    // MEASURED on 6 of the heaviest files with --coverage, same input each time:
    //   maxWorkers 2 -> 106.7s, peak 2001 MB total
    //   maxWorkers 3 ->  81.2s, peak 2674 MB total   <- chosen
    //   maxWorkers 4 ->  83.3s, peak 3475 MB total
    // 3 is both the fastest and cheaper in memory than 4; peak single-fork RSS
    // was ~1.0-1.4 GB against the 3072 MB per-fork heap ceiling below.
    maxWorkers: 3,
    // Node 25 enables its process-global experimental Web Storage in forks but
    // gives those workers no persistence path, emitting one
    // "--localstorage-file ... without a valid path" warning per test file.
    // happy-dom supplies the window-scoped Storage this suite actually tests,
    // and setup.ts replaces it with a deterministic in-memory implementation,
    // so disable the unrelated Node global instead of sharing a disk file
    // across concurrent workers.
    execArgv: ['--max-old-space-size=3072', '--no-experimental-webstorage'],
    // Default 5s is too tight for tests that ``await import(...)`` inside the
    // body: under a full concurrent forks run the collect phase can starve the
    // dynamic import past 5s and it times out. 15s gives headroom for
    // load-induced flakes while still failing real hangs.
    testTimeout: 15000,
    include: ['integration/**/*.test.{ts,tsx}', 'src/**/*.test.{ts,tsx}'],
    onConsoleLog: (log) =>
      !log.includes('was not wrapped in act(') &&
      // TipCard's useTipTrigger issues tipsStatus/tipsNext queries; the 232
      // tests that vi.mock('../api/client') without stubbing those two fields
      // leave queryFn undefined, so React Query logs "No queryFn was passed"
      // ~44x per file. Pure noise — the component under test is never TipCard.
      !log.includes('No queryFn was passed') &&
      // Insurance for the defense-in-depth path above: if a widget iframe
      // <script>/page load ever reaches happy-dom's disable-loading settings
      // (rather than being answered by the msw fallback first), happy-dom logs a
      // NotSupportedError to console.error. That decline is INTENDED, not a
      // failure — suppress it so widget tests don't spew expected exceptions.
      !log.includes('loading is disabled'),
    // Coverage emitted when ``vitest run --coverage`` is passed (see the
    // ``test:website`` script in package.json). Off in watch mode to keep
    // local iteration snappy.
    //
    // ``cobertura-coverage.xml`` is the filename the CI coverage tool scans
    // for in the build artifacts; ``lcov.info`` is a fallback for tools that
    // read lcov natively (codecov, etc.). Output lands under ``build/`` so the
    // build includes it in the published artifact tree — the default
    // ``./coverage/`` would be outside the packaged output and the coverage
    // tool would never see it.
    coverage: {
      provider: 'v8',
      reporter: ['text', 'cobertura', 'lcov'],
      reportsDirectory: './build/coverage',
      include: ['src/**/*.{ts,tsx}'],
      exclude: [
        'src/test/**',
        'src/**/*.test.{ts,tsx}',
        'src/**/*.d.ts',
        // Pure type declarations: no runtime statements exist to cover, so
        // measuring them yields a phantom 0% row that trips the per-file floor.
        'src/types/reviewFix.ts',
        'src/vite-env.d.ts',
      ],
    },
  },
  server: {
    port: 3000,
    proxy: {
      '/api': {
        target: `http://localhost:${backendPort}`,
        ws: true,
        changeOrigin: true, // Backend validates Host header for CSRF; without this, dev proxy sends localhost:3000
      },
      // Proxy app UI bundle file requests to the backend (serves from ~/.kiro/crew/apps/)
      // Only matches /apps/{name}/ui/* — not /apps (React Router page)
      '^/apps/[^/]+/ui/': {
        target: `http://localhost:${backendPort}`,
        changeOrigin: true,
      },
      // Proxy app API requests to the backend (reverse proxy to app backends)
      '^/apps/[^/]+/api/': {
        target: `http://localhost:${backendPort}`,
        changeOrigin: true,
      },
      // Vendor shims are served from the build output in production;
      // in dev mode, Vite serves them directly from src/vendor/ via the
      // multi-entry input config, so no proxy needed.
      '/logo.png': `http://localhost:${backendPort}`,
      '/static/kirocrew-logo.png': `http://localhost:${backendPort}`,
    },
  },
  build: {
    outDir: './dist',
    emptyOutDir: true,
    // The vendor split below extracts the heaviest eager libs into their own
    // chunks, but two are irreducibly large: Monaco's `editor.main` (~3.81MB,
    // the code-editor engine — already lazy-loaded) and the app-core `index`
    // chunk (~3.79MB). Both are gzip-served (~1MB each). Set the ceiling just
    // above the current max (3810KB) — NOT a round headroom number — so the
    // window in which a NEW oversized chunk could slip in undetected is as
    // small as physically possible. TRADEOFF (accept knowingly): this is a
    // single global knob, so it cannot distinguish "known-large" from "new
    // regression" — a new chunk up to ~3.81MB would not warn HERE. That residual
    // gap is covered in CI by the per-chunk gate (scripts/check-bundle-size.mjs,
    // run against the analyze-mode build): explicit ceilings for the known-large
    // chunks, a 500KB default for everything else. This knob stays anyway as the
    // only signal a plain local `npm run build` prints; the honest local
    // alternatives — a 500KB limit (a permanent false-positive that trains
    // developers to ignore it) or splitting Monaco's monolithic core (not
    // feasible) — are worse. Lower this the moment `editor.main`/`index` shrink;
    // do NOT raise it without first splitting the chunk that forced the raise.
    chunkSizeWarningLimit: 3810,
    // The Slack brand mark must remain a physical file. The gateway serves
    // /assets, while an inline SVG would also conflict with security review.
    assetsInlineLimit: (filePath) => (filePath.endsWith('slack-logo.svg') || filePath.endsWith('discord-logo.svg') || filePath.endsWith('telegram-logo.svg') ? false : undefined),
    rollupOptions: {
      // Multi-entry: the dashboard SPA plus every app window entry
      // discovered under src/apps/<app>/<name>.html (see appWindowEntries —
      // the same convention dashboard/server.py serves in production). The
      // entries live INSIDE each app's folder so an app stays one
      // self-contained folder and can be lifted out without hunting for
      // stragglers; their SERVED urls are `/app-windows/<app>/<name>.html` in both dev
      // and production.
      input: {
        main: fileURLToPath(new URL('./index.html', import.meta.url)),
        ...appWindowEntries(),
      },
      output: {
        // Split the heaviest eager vendor libraries out of the ~6MB main
        // `index` chunk into named, long-term-cacheable vendor chunks. This
        // silences the >500kB chunk-size warning HONESTLY (the app core is
        // genuinely large) and improves cache hit rate: a bump to one lib no
        // longer busts the whole main bundle's content hash.
        //
        // Only SPECIFIC packages are matched — never a blanket
        // `return 'vendor'` for all node_modules, which would force Rollup to
        // pull mermaid/monaco's already-dynamic (lazy) chunks back into an
        // eager vendor chunk and regress load time. Monaco (editor.main +
        // *.worker) and mermaid diagrams already emit their own lazy chunks;
        // leave them alone.
        manualChunks(id) {
          if (!id.includes('node_modules')) return
          // React + all context-carrying singletons in ONE chunk so a single
          // module instance is guaranteed and provider/init ordering is
          // preserved (mirrors resolve.dedupe above). Splitting these apart
          // risks "Invalid hook call" / "No QueryClient set".
          if (/[\\/]node_modules[\\/](react|react-dom|scheduler|react-redux|@reduxjs|redux|redux-thunk|react-router|react-router-dom|@tanstack[\\/]react-query|@tanstack[\\/]query-core|framer-motion)[\\/]/.test(id)) {
            return 'vendor-react'
          }
          // d3 in its OWN chunk: MemoryGraphTab + KnowledgeGraph both defer it
          // with `import('d3')` (only their type imports are eager), so d3 is a
          // deliberate lazy boundary. Grouping it with the eager sigma/graphology
          // stack below would pull d3 into that eager chunk and defeat the lazy
          // load. Keep it separate so `import('d3')` stays its own async chunk.
          if (/[\\/]node_modules[\\/](d3|d3-[^\\/]+|internmap|delaunator|robust-predicates)[\\/]/.test(id)) {
            return 'vendor-d3'
          }
          // ForceAtlas2 + Louvain are physics-only: KnowledgeGraph defers both
          // with `import('graphology-layout-forceatlas2')` / `.../worker` and
          // `import('graphology-communities-louvain')`, reached only when physics
          // is toggled on or the one-shot mount layout runs. Route them to their
          // OWN lazy chunk ahead of the broad graphology rule below, so the eager
          // vendor-graph chunk (sigma + core graphology, loaded for every graph
          // view) does not carry code the default physics-off view never touches.
          if (/[\\/]node_modules[\\/](graphology-layout-forceatlas2|graphology-communities-louvain)[\\/]/.test(id)) {
            return 'vendor-graph-physics'
          }
          // Graph/network visualization stack (vis-network, vis-data, sigma,
          // graphology, cytoscape) — large and only used by graph views.
          if (/[\\/]node_modules[\\/](vis-network|vis-data|vis-util|sigma|graphology|graphology-[^\\/]+|cytoscape)[\\/]/.test(id)) {
            return 'vendor-graph'
          }
          // Markdown/math/syntax rendering (katex, highlight.js, and the
          // remark/rehype/unified pipeline).
          if (/[\\/]node_modules[\\/](katex|highlight\.js|lowlight|refractor|react-markdown|remark-[^\\/]+|rehype-[^\\/]+|mdast-[^\\/]+|hast-[^\\/]+|micromark[^\\/]*|unified|unist-[^\\/]+)[\\/]/.test(id)) {
            return 'vendor-markdown'
          }
          // The YAML document parser, reached only by the skill editor's
          // frontmatter round-trip (`SkillForm.tsx`). Bucketed like every other
          // vendor library here because the App chunk is meant to hold FIRST-PARTY
          // code -- its budget comment in scripts/check-bundle-size.mjs says as
          // much -- and it is the ceiling that ordinary feature PRs trip. Measured
          // 93.7 KB raw / 29 KB gzip, far under the gate's 500 KB default, so it
          // needs no CHUNK_BUDGETS entry of its own. The leading separator keeps
          // this off `js-yaml`, which is mermaid's and belongs in mermaid's chunk.
          if (/[\\/]node_modules[\\/]yaml[\\/]/.test(id)) {
            return 'vendor-yaml'
          }
          // Routed out because this change's sidebar growth pushed the App chunk past the
          // ceiling the `yaml` note above names; eager at every use site, no lazy boundary to defeat.
          if (/[\\/]node_modules[\\/]dompurify[\\/]/.test(id)) {
            return 'vendor-dompurify'
          }
        },
      },
    },
  },
})
