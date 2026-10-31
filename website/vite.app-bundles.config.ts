/**
 * Builds the UI bundles of builtin apps that ship one, into the Python package.
 *
 * A builtin app is a directory under `src/kiro_crew/apps/builtins/`, and its
 * dashboard side is served the way any installed app's is: an ESM bundle under
 * the app's `ui/` directory, mounted by `AppHost` through the dashboard's import
 * map, which resolves `react`, `lucide-react`, `@tanstack/react-query` and the
 * `@kirocrew/app-sdk` modules to the host's own copies (`public/vendor/*.mjs`).
 * So a bundle built here must leave those as bare imports, which is what the
 * `external` list below does; everything else it uses is bundled in.
 *
 * The SOURCE stays in this website tree (`src/apps/<app>/`), so the same
 * typecheck, lint, i18n and test gates that cover the dashboard cover it. Only
 * the output crosses into the package, as `ui/dist/<name>.mjs` next to the
 * app's `app.json`, where the `apps/builtins` ui glob in `setup.cfg` ships it
 * and builtin registration copies it into the app's install directory.
 *
 * Run by `npm run build` after the dashboard build, and alone by
 * `npm run build:app-bundles`.
 */
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import path from 'path'

/** One entry per bundle a builtin ships. `app` is the package directory name. */
const BUNDLES: { app: string; name: string; entry: string }[] = [
  { app: 'code_intel', name: 'panel', entry: 'src/apps/code-intel/panel.tsx' },
]

/** The modules the host's import map provides; a bundle must not carry its own. */
const HOST_MODULES = [
  'react',
  'react-dom',
  'react-dom/client',
  'react/jsx-runtime',
  '@kirocrew/app-sdk',
  '@kirocrew/app-sdk/ui',
  '@tanstack/react-query',
  'lucide-react',
]

const only = process.env.APP_BUNDLE
const selected = only ? BUNDLES.filter(b => b.app === only) : BUNDLES
if (only && selected.length === 0) {
  throw new Error(`APP_BUNDLE=${only} names no bundle; known: ${BUNDLES.map(b => b.app).join(', ')}`)
}
if (selected.length !== 1) {
  // One bundle per build: vite's lib mode takes one `outDir`, and each app's
  // output lands in its own package directory. `npm run build:app-bundles`
  // loops over the list.
  throw new Error('set APP_BUNDLE=<app> to build one bundle (see package.json build:app-bundles)')
}
const [bundle] = selected

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
    },
  },
  define: {
    'process.env.NODE_ENV': JSON.stringify('production'),
  },
  // The dashboard's `public/` is not this bundle's: without this vite would copy
  // the whole of it (service worker, vendor stubs, icons) next to the bundle.
  publicDir: false,
  build: {
    outDir: path.resolve(__dirname, `../src/kiro_crew/apps/builtins/${bundle.app}/ui/dist`),
    emptyOutDir: true,
    sourcemap: false,
    minify: true,
    lib: {
      entry: path.resolve(__dirname, bundle.entry),
      formats: ['es'],
      fileName: () => `${bundle.name}.mjs`,
    },
    rollupOptions: {
      external: HOST_MODULES,
      output: {
        // No code splitting: the static route serves one file per declared entry.
        inlineDynamicImports: true,
      },
    },
  },
})
