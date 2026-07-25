// TODO(node18): jsdom pinned to 25.x, vitest to 3.x in package.json — both need Node 22+ for newer majors. Unpin after AL2→AL2023 migration.
import { defineConfig, type Plugin } from 'vite'
/// <reference types="vitest" />
import react from '@vitejs/plugin-react'
import { readFileSync, writeFileSync, existsSync } from 'fs'
import { execSync } from 'child_process'
import http from 'http'
import path from 'path'
import { TAILWIND_RUNTIME_PATH, TAILWIND_RUNTIME_SRC } from './src/lib/vendorPaths'

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
 * Serve the Tailwind v4 browser runtime from the dashboard's own origin at
 * `/vendor/tailwindcss-browser.js`. The sandboxed widget iframe (a null-origin
 * blob) loads Tailwind from here instead of the public cdn.tailwindcss.com,
 * which restricted network environments block — crashing the whole page on
 * artifact render. The file is copied from the tracked @tailwindcss/browser
 * npm dependency at build time (NOT a committed blob), satisfying
 * software-supply-chain policy.
 */
function tailwindRuntimePlugin(): Plugin {
  const RUNTIME_SRC = TAILWIND_RUNTIME_SRC
  const SERVE_PATH = TAILWIND_RUNTIME_PATH
  return {
    name: 'kirocrew-tailwind-runtime',
    // Dev: the build output doesn't exist, so serve straight from node_modules.
    configureServer(server) {
      server.middlewares.use((req, res, next) => {
        if ((req.url || '').split('?')[0] === SERVE_PATH) {
          res.setHeader('Content-Type', 'text/javascript; charset=utf-8')
          res.end(readFileSync(RUNTIME_SRC))
          return
        }
        next()
      })
    },
    // Build: emit into dist/vendor/, served same-origin like the /vendor/*.mjs stubs.
    generateBundle() {
      this.emitFile({
        type: 'asset',
        fileName: TAILWIND_RUNTIME_PATH.replace(/^\//, ''),
        source: readFileSync(RUNTIME_SRC),
      })
    },
  }
}

/**
 * Post-build plugin: replaces %%SW_BUILD_HASH%% in the copied public/sw.js
 * with a stable build-time identifier (version + git SHA). This runs during
 * `vite build` only (not dev server). The public/ directory is copied
 * verbatim by Vite so `define` replacements don't apply to it.
 */
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
        const buildHash = sha ? `${pkg.version}-${sha}` : pkg.version
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
  return {
    name: 'kirocrew-edition-extension',
    enforce: 'pre',
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

export default defineConfig({
  plugins: [react(), tokenProxyPlugin(), appImportMapPlugin(), tailwindRuntimePlugin(), swVersionPlugin(), editionExtensionPlugin()],
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
    environment: 'jsdom',
    setupFiles: './integration/setup.ts',
    css: true,
    pool: 'forks',  // More stable than threads on ARM64 build fleet (avoids ERR_IPC_CHANNEL_CLOSED)
    // Default 5s is too tight for tests that ``await import(...)`` inside the
    // body: under a full concurrent forks run the collect phase can starve the
    // dynamic import past 5s and it times out. 15s gives headroom for
    // load-induced flakes while still failing real hangs.
    testTimeout: 15000,
    include: ['integration/**/*.test.{ts,tsx}', 'src/**/*.test.{ts,tsx}'],
    onConsoleLog: (log) => !log.includes('was not wrapped in act('),
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
      // Proxy app UI bundle file requests to the backend (serves from ~/.kirocrew/apps/)
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
    // The Slack brand mark must remain a physical file. The gateway serves
    // /assets, while an inline SVG would also conflict with security review.
    assetsInlineLimit: (filePath) => (filePath.endsWith('slack-logo.svg') || filePath.endsWith('discord-logo.svg') || filePath.endsWith('telegram-logo.svg') ? false : undefined),
  },
})
