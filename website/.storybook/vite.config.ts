import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import { CONTEXT_SINGLETON_DEDUPE } from '../vite.shared'

const here = path.dirname(fileURLToPath(import.meta.url))

/**
 * The Vite config the story bundle is built with. Deliberately NOT the app's
 * `vite.config.ts` (see `.storybook/main.ts` for why). It carries only the two
 * pieces a component needs to resolve at all — the `@` source alias and the
 * context-carrying singleton dedupe list, imported from the same module the app
 * build reads. PostCSS and Tailwind are picked up from the package root
 * automatically, so the design tokens in `src/index.css` render exactly as they
 * do in the dashboard.
 */
export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      '@': path.resolve(here, '../src'),
    },
    dedupe: CONTEXT_SINGLETON_DEDUPE,
  },
})
