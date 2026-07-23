// TODO(node18): jsdom pinned to 25.x, vitest to 3.x in package.json — both need Node 22+ for newer majors. Unpin after AL2→AL2023 migration.
import { defineConfig, type Plugin } from 'vite'
/// <reference types="vitest" />
import react from '@vitejs/plugin-react'
import { readFileSync, writeFileSync } from 'fs'
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

export default defineConfig({
  plugins: [react(), tokenProxyPlugin(), appImportMapPlugin(), tailwindRuntimePlugin(), swVersionPlugin()],
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
    },
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
      provider: 'istanbul',
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
