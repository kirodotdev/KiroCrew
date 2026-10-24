// React profiling build of the SPA for the render benchmark
// (scripts/bench-render.mjs). It wraps vite.config.ts and changes three things:
//
//   * react-dom and react-dom/client resolve to react-dom/profiling, which
//     exports createRoot in 18.3 and reports actualDuration / selfBaseDuration
//     and the scheduling-profiler hooks to the DevTools hook.
//   * the bundle is unminified, so the commit walk sees real component names.
//   * store/index.ts gets two seams the benchmark's init script fills in:
//     window.__kcPerfMw (a middleware that counts dispatches) and
//     window.__kcSelWrap (wraps each useAppSelector selector to count calls).
//     With neither defined the store behaves exactly as in the prod build.
//     window.__kcStore exposes the store so the benchmark can check that the
//     frames it sent reached state.
//
// Output goes to node_modules/.cache/kc-perf/dist, never website/dist, so a
// profiling build cannot be staged or served by a gateway.
//
//   node ./node_modules/vite/bin/vite.js build --config scripts/vite.profiling.config.mjs
import { fileURLToPath } from 'node:url'
import base from '../vite.config.ts'
import { PROFILING_DIST } from './lib/profiling-dist.mjs'

const RD_PROFILING = fileURLToPath(new URL('../node_modules/react-dom/profiling.js', import.meta.url))

const prevAlias = base.resolve?.alias ?? {}
const aliasList = Array.isArray(prevAlias)
  ? prevAlias
  : Object.entries(prevAlias).map(([find, replacement]) => ({ find, replacement }))

const STORE_FILE = /[\\/]website[\\/]src[\\/]store[\\/]index\.ts$/
const storeSeams = {
  name: 'kc-perf-store-seams',
  enforce: 'pre',
  transform(code, id) {
    if (!STORE_FILE.test(id)) return null
    const out = code
      .replace(
        'export const store = configureStore({\n  reducer: {',
        'export const store = configureStore({\n'
          + '  middleware: (gdm) => (typeof window !== "undefined" && (window as any).__kcPerfMw ? gdm().concat((window as any).__kcPerfMw) : gdm()),\n'
          + '  reducer: {',
      )
      .replace(
        'export const useAppSelector = useSelector.withTypes<RootState>()',
        'const __rawUseAppSelector = useSelector.withTypes<RootState>()\n'
          + 'export const useAppSelector = ((sel: any, eq?: any) => __rawUseAppSelector('
          + 'typeof window !== "undefined" && (window as any).__kcSelWrap ? (window as any).__kcSelWrap(sel) : sel, eq)) as typeof __rawUseAppSelector',
      ) + '\nif (typeof window !== "undefined") (window as any).__kcStore = store\n'
    // Fail the build rather than ship a bench that silently counts nothing.
    if (!out.includes('__kcPerfMw') || !out.includes('__kcSelWrap')) {
      throw new Error('kc-perf-store-seams: website/src/store/index.ts no longer matches; update scripts/vite.profiling.config.mjs')
    }
    return out
  },
}

export default {
  ...base,
  resolve: {
    ...base.resolve,
    alias: [
      { find: /^react-dom\/client$/, replacement: RD_PROFILING },
      { find: /^react-dom$/, replacement: RD_PROFILING },
      ...aliasList,
    ],
  },
  build: { ...base.build, outDir: PROFILING_DIST, emptyOutDir: true, minify: false },
  plugins: [storeSeams, ...(base.plugins || [])],
}
