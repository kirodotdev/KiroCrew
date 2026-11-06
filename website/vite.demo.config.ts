/**
 * Static demo build: the real dashboard SPA with the gateway answered in the
 * page (src/demo/), for usability tests that need no gateway at all.
 *
 *   npm run build:demo   ->  website/dist-demo/
 *
 * It reuses the stock config and changes only what a hostless build needs: its
 * own output directory, the demo entry in place of `src/main.tsx`, no app-window
 * entries, and none of the plugins that publish into the gateway's `dist`.
 */
import { defineConfig, type Plugin, type PluginOption } from 'vite'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import base from './vite.config'

/** Plugins that only make sense for a build the gateway serves. */
const GATEWAY_ONLY_PLUGINS = new Set([
  'kirocrew-atomic-publish',
  'kirocrew-token-proxy',
  'kirocrew-bundle-report',
  'app-window-urls',
])

function keepPlugin(p: PluginOption): PluginOption {
  if (Array.isArray(p)) return p.map(keepPlugin)
  if (p && typeof p === 'object' && 'name' in p && GATEWAY_ONLY_PLUGINS.has((p as Plugin).name)) return null
  return p
}

/** Point index.html at the demo entry and drop its service-worker registration:
 *  a cached shell from a previous demo build would otherwise outlive a redeploy. */
function demoEntryPlugin(): Plugin {
  return {
    name: 'kirocrew-demo-entry',
    transformIndexHtml: {
      order: 'pre',
      handler(html) {
        const entry = '<script type="module" src="/src/main.tsx"></script>'
        const register = "navigator.serviceWorker.register('/sw.js')"
        if (!html.includes(entry) || !html.includes(register)) {
          throw new Error('vite.demo.config: index.html no longer has the entry or sw.js registration this build rewrites')
        }
        return html
          .replace(entry, '<script type="module" src="/src/demo/boot.ts"></script>')
          .replace(register, 'Promise.resolve()')
      },
    },
  }
}

/** The gateway serves the brand mark at `/logo.png` (and the legacy
 *  `/static/kirocrew-logo.png`) from its own package; a hostless build ships it. */
function demoLogoPlugin(): Plugin {
  const logo = fileURLToPath(new URL('../src/kiro_crew/static/kirocrew-logo.png', import.meta.url))
  return {
    name: 'kirocrew-demo-logo',
    apply: 'build',
    generateBundle() {
      const source = readFileSync(logo)
      this.emitFile({ type: 'asset', fileName: 'logo.png', source })
      this.emitFile({ type: 'asset', fileName: 'static/kirocrew-logo.png', source })
    },
  }
}

export default defineConfig({
  ...base,
  plugins: [...(base.plugins ?? []).map(keepPlugin), demoEntryPlugin(), demoLogoPlugin()],
  // The stock config proxies a few paths to a local gateway; there is none.
  preview: { ...base.preview, proxy: {} },
  build: {
    ...base.build,
    outDir: './dist-demo',
    emptyOutDir: true,
    rollupOptions: {
      ...base.build?.rollupOptions,
      input: { main: fileURLToPath(new URL('./index.html', import.meta.url)) },
    },
  },
})
