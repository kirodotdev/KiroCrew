/**
 * Dedicated production build for the kc-46d84a Remote Crew pane HOST fixture
 * (`playwright-fixtures/pane-host/`). It is built by the incident E2E harness
 * (`test/e2e/test_instance_pane_relay_e2e.py`) into a throwaway output dir the
 * topology hub then serves as the parent origin — it is NEVER part of the normal
 * `npm run build`, so the shipped `dist/` stays a clean stock artifact.
 *
 * The point of a SEPARATE config (rather than a gated input in `vite.config.ts`)
 * is isolation in BOTH directions: the fixture entry never leaks into the
 * production bundle, and the production shell's plugins (the inline importmap,
 * the vendor-runtime and edition seams) never touch the fixture — the parent CSP
 * the topology stamps forbids an inline importmap `<script>`, and the host needs
 * none of the federated-app machinery. What it DOES share with production is the
 * one thing that must not drift: React/context-singleton `dedupe` (so a single
 * React instance backs the store, query client and router) and the `@` alias, so
 * the imported `InstancesViewport` and the relay authorities behave exactly as in
 * the app.
 */
import { fileURLToPath, URL } from 'node:url'
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import path from 'node:path'
import { CONTEXT_SINGLETON_DEDUPE } from './vite.shared'

// The host is always served at the hub root over HTTPS, so root-absolute asset
// refs (`/assets/…`) resolve at the hub origin — no relay relocation, no <base>.
export default defineConfig({
  root: 'playwright-fixtures/pane-host',
  base: '/',
  plugins: [react()],
  resolve: {
    alias: { '@': path.resolve(fileURLToPath(new URL('./src', import.meta.url))) },
    // Identical to vite.config.ts: one React/redux/query/router instance, or the
    // imported production hooks throw "Invalid hook call" / "No QueryClient set".
    dedupe: CONTEXT_SINGLETON_DEDUPE,
  },
  // Referenced by a few production modules (e.g. RUM) that the graph may pull in
  // transitively; define it so evaluation never hits an undefined global.
  define: { __APP_VERSION__: JSON.stringify('0.0.0-kc46-fixture') },
  build: {
    // PANE_HOST_OUT (the E2E harness sets it to a per-run scratch dir) is a
    // plain path; default to website/pane-host-dist for a manual build.
    outDir: process.env.PANE_HOST_OUT
      ? path.resolve(process.env.PANE_HOST_OUT)
      : fileURLToPath(new URL('./pane-host-dist', import.meta.url)),
    emptyOutDir: true,
    // No manifest / precompress / vendor split needed — this is a test host, not
    // a shipped bundle; a single entry chunk graph is all the fixture serves.
    rollupOptions: {
      input: fileURLToPath(new URL('./playwright-fixtures/pane-host/index.html', import.meta.url)),
    },
  },
})
