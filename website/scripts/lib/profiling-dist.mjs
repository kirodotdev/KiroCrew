import { fileURLToPath } from 'node:url'

/**
 * Where `scripts/vite.profiling.config.mjs` writes the React profiling build
 * that `scripts/bench-render.mjs` serves. Under node_modules/.cache, so it is
 * gitignored and never staged as the gateway's dist.
 */
export const PROFILING_DIST = fileURLToPath(new URL('../../node_modules/.cache/kc-perf/dist', import.meta.url))
