// React profiling build of the SPA for the render benchmark
// (scripts/bench-render.mjs). It wraps vite.config.ts and changes three things:
//
//   * react-dom and react-dom/client resolve to react-dom/profiling, which
//     exports createRoot in 18.3 and reports actualDuration / selfBaseDuration
//     and the scheduling-profiler hooks to the DevTools hook.
//   * the bundle is unminified, so the commit walk sees real component names.
//   * VITE_KC_PERF is defined, which turns on the benchmark seams in
//     src/store/index.ts: the dispatch-counting middleware and selector wrapper
//     the benchmark's init script installs, and window.__kcStore. Every other
//     build compiles those seams out.
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

export default {
  ...base,
  define: { ...base.define, 'import.meta.env.VITE_KC_PERF': JSON.stringify('1') },
  resolve: {
    ...base.resolve,
    alias: [
      { find: /^react-dom\/client$/, replacement: RD_PROFILING },
      { find: /^react-dom$/, replacement: RD_PROFILING },
      ...aliasList,
    ],
  },
  build: { ...base.build, outDir: PROFILING_DIST, emptyOutDir: true, minify: false },
}
